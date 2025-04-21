#!/usr/bin/env python3
"""
Realtime speech-to-speech for STR.
Handles audio input/output. Plays back API audio at its native rate.
"""

from __future__ import annotations
import os, ssl, json, base64, asyncio, threading, logging, time, sys
import pyaudio, numpy as np, websockets
from typing import Callable

# GPIO Handling - Conditional Import
try:
    import RPi.GPIO as GPIO
    # Check if running on RPi platform
    _IS_RPI = False
    if sys.platform == "linux":
         try:
              with open('/proc/cpuinfo', 'r') as f:
                   cpuinfo = f.read()
                   if 'Raspberry Pi' in cpuinfo or 'BCM' in cpuinfo: # Broader check
                       _IS_RPI = True
              if _IS_RPI:
                  # Basic check to see if lib works - avoid unnecessary cleanup here
                  # We might need GPIO later for the button
                  pass
         except Exception:
              _IS_RPI = False
              GPIO = None # Ensure GPIO object is None if setup fails
    HAS_GPIO = _IS_RPI and GPIO is not None
except ImportError:
    HAS_GPIO = False
    GPIO = None # Ensure GPIO is None if import fails

from config import (
    MIC_DEVICE_INDEX, MIC_SAMPLE_RATE, MIC_CHANNELS, MIC_CHUNK, MIC_NORMALISE,
    GPIO_BUTTON_PIN, GPIO_LED_PIN, BUTTON_ACTIVE_HIGH, ENABLE_GPIO,
    OPENAI_MODEL_REALTIME, OPENAI_MODEL_TRANSCRIPTION,
    OUTPUT_SAMPLE_RATE, DAC_PYAUDIO_INDEX, PLAYBACK_CHUNK
)

# Use standard basicConfig, config.py will also log its values
# If app.py already sets this up, these lines might be redundant but shouldn't harm.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s",
)
log = logging.getLogger("realtime")

# --- Define Expected Rates ---
# Rate expected *from* OpenAI Realtime API (Check OpenAI Docs)
# Set to 16000 based on common usage and WebSocket request. **VERIFY THIS**
# If OpenAI sends a different rate, playback will be incorrect.
API_AUDIO_OUTPUT_RATE = 16000
# Rate to send *to* OpenAI Realtime API (Usually 16k or 24k, check docs)
# We specify 24k in the websocket request, but let's try 16k for input matching whisper std.
API_AUDIO_INPUT_RATE = 16000 # Changed to 16k for simplicity, ensure websocket req matches if needed

INSTRUCTIONS = (
    "You are a professional radio broadcaster. Provide a natural, "
    "broadcast-style answer in Spanish from Spain. Use European format."
)


class AudioHandler:
    def __init__(self, device_index: int):
        self.device_index = device_index
        self.channels = MIC_CHANNELS
        self.chunk = MIC_CHUNK
        self.fmt = pyaudio.paInt16
        self.p = pyaudio.PyAudio() # Instance for both input and output in this class
        self.input_rate = 0 # Initialize

        # Determine actual input rate
        if MIC_SAMPLE_RATE and MIC_SAMPLE_RATE != 0:
            self.input_rate = MIC_SAMPLE_RATE
            log.info(f"Using configured MIC_SAMPLE_RATE: {self.input_rate} Hz")
        else:
            try:
                info = self.p.get_device_info_by_index(self.device_index)
                self.input_rate = int(info["defaultSampleRate"])
                log.info(f"Using default sample rate for mic index {self.device_index} ('{info.get('name', 'N/A')}'): {self.input_rate} Hz")
            except Exception as e:
                log.error(f"Could not get default sample rate for mic index {self.device_index}: {e}. Falling back to 48000 Hz.")
                self.input_rate = 48000 # Common fallback

        if self.input_rate <= 0:
             log.error(f"FATAL: Invalid microphone input rate determined: {self.input_rate}. Cannot proceed.")
             raise ValueError(f"Invalid microphone input rate: {self.input_rate}")

        log.info(f"Mic Config: Index={self.device_index}, Actual Input Rate={self.input_rate} Hz -> Target API Input Rate={API_AUDIO_INPUT_RATE} Hz (Resampling if needed)")
        log.info(f"Playback Config: Expected API Output Rate={API_AUDIO_OUTPUT_RATE} Hz -> Playback at Native Rate on Device Index={DAC_PYAUDIO_INDEX} (or default)")

        self.input_stream = None
        self.recording = False

    def start_input(self):
        if self.input_stream and self.input_stream.is_active():
            log.warning("Input stream already active. Stopping first.")
            self.stop_input() # Ensure clean state

        try:
            log.info(f"Opening mic stream: Index={self.device_index}, Rate={self.input_rate} Hz, Channels={self.channels}, Format={self.fmt}, Chunk={self.chunk}")
            self.input_stream = self.p.open(
                format=self.fmt,
                channels=self.channels,
                rate=self.input_rate,
                input=True,
                frames_per_buffer=self.chunk,
                input_device_index=self.device_index,
                stream_callback=None, # Using blocking read instead
            )
            # Double check if stream is active after opening
            if not self.input_stream.is_active():
                 # Sometimes open might succeed but stream isn't immediately active
                 time.sleep(0.1)
                 if not self.input_stream.is_active():
                      raise OSError(f"Mic stream failed to become active after opening on device index {self.device_index}")

            self.recording = True
            log.info("🎙️ Mic ON (Stream opened and active)")
        except Exception as e:
             log.exception(f"FATAL: Failed to open microphone input stream on device index {self.device_index}: {e}")
             self.recording = False
             if self.input_stream: # Close if partially opened
                  try: self.input_stream.close()
                  except Exception: pass
             self.input_stream = None # Ensure stream is None on failure
             # Terminate PyAudio if mic fails completely? Maybe too drastic.
             # if self.p:
             #      try: self.p.terminate()
             #      except Exception: pass
             #      self.p = None
             raise # Re-raise the exception to signal failure


    def read_chunk(self) -> bytes | None:
        if not self.recording or not self.input_stream or not self.input_stream.is_active():
            return None
        try:
            raw = self.input_stream.read(self.chunk, exception_on_overflow=False)
            if not raw:
                return None

            audio = np.frombuffer(raw, dtype=np.int16)

            # Normalization (Optional)
            if MIC_NORMALISE:
                peak = np.max(np.abs(audio))
                if peak > 100: # Only normalize if there's significant signal
                    gain = int(0.9 * 32767 / peak)
                    if gain > 1:
                        audio = np.clip(audio * gain, -32768, 32767).astype(np.int16)

            # Resampling for API Input (if necessary) - Keep this part
            target_api_rate = API_AUDIO_INPUT_RATE
            if self.input_rate != target_api_rate:
                if self.input_rate < target_api_rate:
                     log.warning(f"Mic input rate ({self.input_rate} Hz) is lower than API input rate ({target_api_rate} Hz). Upsampling needed.")
                     # Implement upsampling if needed, otherwise quality suffers
                     factor = target_api_rate / self.input_rate
                     num_samples_in = len(audio)
                     num_samples_out = int(num_samples_in * factor)
                     if num_samples_in > 0 and num_samples_out > 0:
                         idx_orig = np.arange(num_samples_in)
                         idx_new = np.linspace(0, num_samples_in - 1, num_samples_out)
                         audio = np.interp(idx_new, idx_orig, audio).astype(np.int16)
                         log.debug(f"Upsampled mic audio to {len(audio)} samples for API.")
                     else:
                         log.warning("Cannot upsample zero-length audio chunk.")
                         return None
                elif self.input_rate > target_api_rate:
                    log.debug(f"Resampling mic audio from {self.input_rate} Hz to {target_api_rate} Hz for API input...")
                    factor = self.input_rate / target_api_rate
                    num_samples_in = len(audio)
                    num_samples_out = int(num_samples_in / factor)
                    if num_samples_in == 0 or num_samples_out == 0:
                         log.warning("Cannot downsample zero-length audio chunk.")
                         return None
                    idx_orig = np.arange(num_samples_in)
                    idx_new = np.linspace(0, num_samples_in - 1, num_samples_out)
                    audio = np.interp(idx_new, idx_orig, audio).astype(np.int16)
                    log.debug(f" -> Downsampled to {len(audio)} samples.")

            return audio.tobytes()

        except OSError as e:
            if "Input overflowed" in str(e):
                log.warning("Mic input overflow detected. Skipping chunk.")
                return None
            else:
                log.error(f"Mic read OS error: {e}")
                self.stop_input() # Stop recording on other OS errors
                return None
        except Exception as e:
            log.exception(f"Unexpected error reading mic chunk: {e}")
            self.stop_input() # Stop recording on unexpected errors
            return None

    def stop_input(self):
        if self.recording:
            self.recording = False # Signal read loop to stop
            log.info("Attempting to stop mic input stream...")
            time.sleep(0.05) # Allow read loop to potentially finish current read

        if self.input_stream:
            try:
                # Check if active before stopping
                if self.input_stream.is_active():
                    self.input_stream.stop_stream()
                self.input_stream.close()
                log.info("🎙️ Mic OFF (Stream closed)")
            except Exception as e:
                log.error(f"Error closing mic input stream: {e}")
            finally:
                self.input_stream = None # Ensure stream is cleared

    def play(self, data: bytes):
        """
        Play API audio (received as raw PCM bytes) at its NATIVE format.
        Assumes input format matches API_AUDIO_OUTPUT_RATE, mono, 16-bit.
        Attempts specified DAC_PYAUDIO_INDEX first, then falls back to default.
        NO RESAMPLING OR CHANNEL CONVERSION IS DONE HERE.
        """
        if not data:
            log.warning("AudioHandler.play called with empty data.")
            return

        # Define input format based on API expectation (MUST BE ACCURATE)
        in_rate = API_AUDIO_OUTPUT_RATE
        in_channels = 1 # OpenAI Realtime usually sends mono
        in_width = 2    # OpenAI Realtime usually sends 16-bit PCM
        in_format = self.p.get_format_from_width(in_width) # Should be paInt16

        def _playback():
            stream = None
            p_playback = self.p # Use the shared PyAudio instance

            try:
                log.debug(f"Received {len(data)} bytes of audio data (Expected Native Format: {in_rate}Hz, {in_channels}Ch, {in_width*8}-bit).")

                # Ensure data length is multiple of sample width * channels
                frame_size = in_width * in_channels
                if len(data) % frame_size != 0:
                     remainder = len(data) % frame_size
                     log.warning(f"Received audio data length ({len(data)}) is not multiple of frame size ({frame_size}). Truncating last {remainder} bytes.")
                     data = data[:-remainder]

                if not data:
                     log.warning("No audio data left after truncation. Skipping playback.")
                     return

                # --- NO RESAMPLING / FORMAT CONVERSION ---
                # The data is played back exactly as received.

                # --- Attempt to Open Stream (Primary DAC first, then Fallback) ---
                # Open the stream with the NATIVE format received from the API
                target_device_index = DAC_PYAUDIO_INDEX
                stream = None
                opened_device_info = "Unknown" # For logging

                try:
                    device_info = p_playback.get_device_info_by_index(target_device_index)
                    opened_device_info = f"Configured DAC: Index={target_device_index}, Name='{device_info.get('name', 'N/A')}'"
                    log.info(f"Realtime Play: Attempting {opened_device_info} (Native Rate: {in_rate} Hz, {in_channels} Ch)")
                    stream = p_playback.open(
                        format=in_format,
                        channels=in_channels,
                        rate=in_rate, # Use the NATIVE API rate
                        output=True,
                        output_device_index=target_device_index,
                        frames_per_buffer=PLAYBACK_CHUNK,
                    )
                    log.info(f"Realtime Play: Successfully opened {opened_device_info}")

                except Exception as e_dac:
                    log.warning(f"Realtime Play: Failed {opened_device_info}: {e_dac}. Trying default.")
                    try:
                        default_output_info = p_playback.get_default_output_device_info()
                        default_output_index = default_output_info['index']
                        opened_device_info = f"Default Output: Index={default_output_index}, Name='{default_output_info.get('name', 'N/A')}'"
                        log.info(f"Realtime Play: Attempting {opened_device_info} (Native Rate: {in_rate} Hz, {in_channels} Ch)")
                        stream = p_playback.open(
                            format=in_format,
                            channels=in_channels,
                            rate=in_rate, # Use the NATIVE API rate
                            output=True,
                            output_device_index=None, # Let PyAudio choose default
                            frames_per_buffer=PLAYBACK_CHUNK,
                        )
                        log.info(f"Realtime Play: Successfully opened {opened_device_info}")
                    except Exception as e_default:
                        log.error(f"FATAL: Realtime Play: Failed to open both specified DAC and default output: {e_default}")
                        return # Cannot play

                # --- Play Audio ---
                log.info(f"Realtime Play: Playing {len(data)} bytes at {in_rate} Hz...")
                # Write data in chunks (optional, could write all at once if chunks are small)
                # Writing the whole chunk received from API might be simpler here
                stream.write(data)
                # If API sends very large chunks, chunking the write might be better:
                # data_idx = 0
                # chunk_size_bytes = PLAYBACK_CHUNK * in_channels * in_width # Rough estimate
                # while data_idx < len(data):
                #      chunk_to_write = data[data_idx : data_idx + chunk_size_bytes]
                #      stream.write(chunk_to_write)
                #      data_idx += len(chunk_to_write)

                stream.stop_stream() # Wait for buffer to finish playing THIS chunk
                log.info("Realtime Play: Finished playing chunk.")

            except Exception as e:
                log.exception(f"Realtime Playback thread error: {e}")
            finally:
                if stream is not None:
                    try:
                        # Ensure stopped before closing
                        if stream.is_active(): stream.stop_stream()
                        stream.close()
                        log.debug("Realtime playback stream closed.")
                    except Exception as e_close:
                        log.error(f"Error closing realtime playback stream: {e_close}")
                # Do not terminate the shared PyAudio instance here

        # Start playback in a daemon thread
        threading.Thread(target=_playback, daemon=True).start()

    def close(self):
        """Clean up PyAudio resources."""
        log.info("Closing AudioHandler...")
        self.stop_input() # Ensure input stream is stopped and closed
        # Output streams are handled in their own threads. Terminate the shared PyAudio instance.
        if self.p:
            try:
                self.p.terminate()
                log.info("PyAudio instance terminated.")
                self.p = None # Clear the instance
            except Exception as e:
                log.error(f"Error terminating PyAudio instance: {e}")


# --- WebSocket Client ---
class RealtimeClient:
    URL = "wss://api.openai.com/v1/realtime/sessions" # Use sessions endpoint
    MODEL = OPENAI_MODEL_REALTIME

    def __init__(
        self,
        instructions: str,
        voice: str = "nova", # Changed default voice example
        mic_index: int = MIC_DEVICE_INDEX,
        on_text: Callable[[str], None] | None = None,
    ):
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not set")

        self.instructions = instructions
        self.voice = voice
        self.on_text = on_text

        try:
            # Use a separate PyAudio instance for the RealtimeClient's AudioHandler
            # to potentially avoid conflicts if audio_manager is used concurrently.
            # This adds slight overhead but might increase stability.
            # If conflicts aren't observed, could revert to sharing one.
            self.audio = AudioHandler(mic_index) # Handles mic input and audio playback
        except ValueError as e: # Catch rate error from AudioHandler init
             log.error(f"Failed to initialize AudioHandler: {e}")
             raise # Re-raise to prevent client from starting incorrectly
        except Exception as e:
             log.error(f"Unexpected error initializing AudioHandler: {e}")
             raise

        self._audio_buf = b"" # Buffer for incoming audio delta (if needed, currently unused)
        self._text_buf = ""   # Buffer for incoming text delta
        self._rec_flag = threading.Event() # Controls the mic recording loop

        self.loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._loop_thread.start()
        log.info("Asyncio event loop started in a separate thread.")

        self.ws: websockets.WebSocketClientProtocol | None = None
        self.session_id: str | None = None
        self._connection_lock = asyncio.Lock() # Prevent concurrent connection attempts
        self._recv_task: asyncio.Task | None = None

        # Block until initial connection attempt finishes - Moved into start() or similar?
        # Blocking here can hang the main thread if connection fails badly.
        # Let's attempt connection asynchronously and check status later.
        # We will call ensure_connected before operations that need it.
        # future = asyncio.run_coroutine_threadsafe(self.ensure_connected(), self.loop)
        # try:
        #      future.result(timeout=15) # Wait with timeout
        #      if not self.ws or not self.ws.open:
        #           raise ConnectionError("Initial WebSocket connection failed.")
        # except TimeoutError:
        #      log.error("Timeout waiting for initial WebSocket connection.")
        #      # Don't raise here, allow app to potentially retry or show error state
        #      # raise ConnectionError("Timeout during initial WebSocket connection.")
        # except Exception as e:
        #      log.error(f"Error during initial WebSocket connection: {e}")
        #      # Don't raise here
        #      # raise ConnectionError(f"Initial WebSocket connection failed: {e}")


        # GPIO Setup (only if enabled and available)
        self.gpio_enabled = HAS_GPIO and ENABLE_GPIO and GPIO is not None
        if self.gpio_enabled:
            self._setup_gpio()
        else:
            log.info("GPIO not available or disabled by config; hardware button/LED inactive.")

        log.info(f"RealtimeClient configured for API Input: {API_AUDIO_INPUT_RATE}Hz, Expected API Output: {API_AUDIO_OUTPUT_RATE}Hz")

    async def ensure_connected(self):
        """Connects or reconnects the WebSocket. Returns True on success, False on failure."""
        async with self._connection_lock:
            if self.ws and self.ws.open:
                log.debug("WebSocket already connected.") # Less noisy
                return True

            if self.ws and not self.ws.closed:
                log.warning("WebSocket exists but is not open/closed state unclear. Closing before reconnecting.")
                try:
                     await self.ws.close()
                except Exception: pass # Ignore errors during close if reconnecting anyway
            self.ws = None # Ensure it's None before reconnect attempt
            self.session_id = None # Clear session ID

            log.info("Attempting WebSocket connection...")
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

            # *** CRITICAL: Ensure requested format matches API_AUDIO_OUTPUT_RATE ***
            # Format string like "pcm_16" or "pcm_24"
            requested_api_output_format = f"pcm_{API_AUDIO_OUTPUT_RATE // 1000}"
            # *** CRITICAL: Ensure input format sample_rate matches API_AUDIO_INPUT_RATE ***
            # We are now using 16k for input. The encoding 'pcm_16' refers to bit depth.
            requested_api_input_format = {"encoding": "pcm_16", "sample_rate": API_AUDIO_INPUT_RATE}

            log.info(f"Requesting API Output Format: {requested_api_output_format}")
            log.info(f"Sending API Input Format: {requested_api_input_format['sample_rate']}Hz, {requested_api_input_format['encoding']}")

            try:
                self.ws = await websockets.connect(
                    self.URL,
                    extra_headers={"Authorization": f"Bearer {self.api_key}"},
                    ssl=ssl_ctx,
                    open_timeout=10, # Add timeout
                    close_timeout=5,
                    ping_interval=20, # Keep connection alive
                    ping_timeout=10
                )
                log.info("WebSocket connection established. Sending session configuration.")
                await self.ws.send(json.dumps({
                    "model": self.MODEL,
                    "language": "es", # Specify language
                    "stream": True, # Enable streaming
                    "voice": self.voice,
                    "response_format": {"audio_format": requested_api_output_format}, # Request the rate we expect
                    "input_format": requested_api_input_format, # Tell API what we are sending
                    "instructions": self.instructions,
                }))

                # Wait for session_begin message
                session_begin = await asyncio.wait_for(self.ws.recv(), timeout=10)
                session_data = json.loads(session_begin)

                if session_data.get("event") == "session_begins":
                     self.session_id = session_data.get("session_id")
                     log.info(f"WebSocket session ready. Session ID: {self.session_id}")
                     # Start receive loop only after successful connection
                     if self._recv_task and not self._recv_task.done():
                         log.warning("Cancelling existing receive task before starting new one.")
                         self._recv_task.cancel()
                         try: await self._recv_task # Allow cancellation to complete
                         except asyncio.CancelledError: pass
                     self._recv_task = asyncio.create_task(self._recv_loop())
                     log.info("Receive loop task created.")
                     return True # Connection successful
                else:
                     log.error(f"Unexpected message after connect (expected session_begins): {session_data}")
                     await self.ws.close()
                     self.ws = None
                     self.session_id = None
                     return False # Connection failed

            except websockets.exceptions.InvalidStatusCode as e:
                 log.error(f"WebSocket connection failed (Invalid Status Code): {e.status_code} {e.body}")
            except asyncio.TimeoutError:
                 log.error("WebSocket connection or session begin timed out.")
            except websockets.exceptions.ConnectionClosed as e:
                 log.error(f"WebSocket connection closed during setup: {e.code} {e.reason}")
            except OSError as e:
                 log.error(f"WebSocket connection failed (OS Error): {e}")
            except Exception as e:
                 log.exception(f"WebSocket connection failed: {e}")

            # Ensure cleanup on failure
            if self.ws and not self.ws.closed:
                 try: await self.ws.close()
                 except Exception: pass
            self.ws = None
            self.session_id = None
            return False # Connection failed

    async def _recv_loop(self):
        if not self.ws:
            log.error("Receive loop cannot start: WebSocket is not connected.")
            return # Should not happen if started correctly

        log.info("WebSocket receive loop started.")
        try:
            async for message in self.ws:
                #log.debug(f"WS RECV: Type={type(message)}, Len={len(message) if isinstance(message, (str, bytes)) else 'N/A'}") # Debug
                if isinstance(message, str):
                    try:
                        ev = json.loads(message)
                        await self._handle_event(ev)
                    except json.JSONDecodeError:
                        log.warning(f"Received non-JSON message: {message[:100]}...")
                elif isinstance(message, bytes):
                     log.warning(f"Received unexpected binary data ({len(message)} bytes). Ignoring.")
        except websockets.exceptions.ConnectionClosedOK:
            log.info("WebSocket connection closed normally (ClosedOK).")
        except websockets.exceptions.ConnectionClosedError as e:
            log.error(f"WebSocket connection closed with error (ClosedError): {e.code} {e.reason}")
        except websockets.exceptions.ConnectionClosed as e: # Catch generic ConnectionClosed
            log.warning(f"WebSocket connection closed unexpectedly: {e.code} {e.reason}")
        except asyncio.CancelledError:
             log.info("Receive loop cancelled.")
        except Exception as e:
            log.exception(f"WebSocket receive loop error: {e}")
        finally:
            log.info("WebSocket receive loop finished.")
            # Don't set self.ws = None here, let ensure_connected handle state checking.
            # If the loop ends due to closure, self.ws.open will be false.

    async def _handle_event(self, ev: dict):
        """Handles JSON events received from the WebSocket."""
        event_type = ev.get("event")

        if event_type == "audio":
            audio_chunk_b64 = ev.get("chunk")
            if audio_chunk_b64:
                try:
                    audio_chunk = base64.b64decode(audio_chunk_b64)
                    #log.debug(f"Received audio chunk: {len(audio_chunk)} bytes - playing immediately.")
                    # Play the chunk without buffering
                    self.audio.play(audio_chunk)
                except Exception as e:
                    log.error(f"Error decoding/playing audio chunk: {e}")
            # else:
                 # log.warning(f"Received audio event with no chunk: {ev}") # Can be noisy

        elif event_type == "text":
             text_delta = ev.get("text")
             if text_delta is not None: # Check for None explicitly
                  # Broadcasting deltas might be too noisy for UI, buffer for final.
                  self._text_buf += text_delta
             # else:
                  # log.warning(f"Received text event with no text: {ev}") # Can be noisy

        elif event_type == "transcript": # Handle transcriptions if needed
             transcript_text = ev.get("text")
             is_final = ev.get("final", False)
             log.info(f"Transcript{' (Final)' if is_final else ''}: {transcript_text}")
             # Optionally display transcripts in UI / use for context

        elif event_type == "speech_end":
             log.info("Received speech_end event.")
             # Broadcast any accumulated text
             if self._text_buf and self.on_text:
                  log.info(f"Broadcasting final text on speech_end: {self._text_buf}")
                  self.on_text(self._text_buf)
                  self._text_buf = "" # Clear buffer
             else:
                  # If no text was buffered but speech_end occurs, maybe signal readiness?
                  if not self._text_buf and self.on_text:
                      self.on_text("") # Send empty string to signal end? Or handle in UI.
                  self._text_buf = "" # Ensure buffer is clear

        elif event_type == "session_terminates":
             log.warning(f"WebSocket session terminated by server. Reason: {ev.get('reason', 'N/A')}")
             await self.close_connection(reason="Session terminated by server") # Close our side

        elif event_type == "error":
            log.error(f"API Error received: {ev.get('message', 'No message')}")
            # Optionally close connection on severe errors, e.g. auth errors
            if "authentication failed" in ev.get('message', '').lower():
                 await self.close_connection(reason="API Authentication Error")

        elif event_type == "latency": # Handle latency info if provided
             log.debug(f"Latency Info: {ev}")

        else:
            # Ignore session_begins here as it's handled in connect
            if event_type != "session_begins":
                 log.warning(f"Unhandled WebSocket event type: {event_type} - Data: {ev}")


    async def _mic_stream_sender(self):
        """Reads from mic and sends audio data over WebSocket."""
        log.info("Attempting to start mic stream sender...")
        # Ensure connection before starting mic
        if not await self.ensure_connected():
            log.error("Cannot start mic stream sender: WebSocket connection failed.")
            self._rec_flag.clear() # Ensure flag is off if connection failed
            # Signal UI error?
            return

        # Now self.ws should be valid if ensure_connected returned True
        if not self.ws or not self.ws.open:
             log.error("Cannot start mic stream sender: WebSocket is not open after ensure_connected.")
             self._rec_flag.clear()
             return

        try:
            self.audio.start_input() # Open the mic stream
        except Exception as e:
            log.error(f"Mic stream failed to start in sender task: {e}")
            self._rec_flag.clear()
            # Signal UI error?
            return

        log.info("Microphone stream sending loop started.")
        try:
            while self._rec_flag.is_set():
                # Check connection at the start of each loop iteration
                if not self.ws or not self.ws.open:
                    log.warning("WebSocket closed during mic stream. Stopping sender.")
                    self._rec_flag.clear()
                    break

                chunk = self.audio.read_chunk()
                if chunk:
                    try:
                        #log.debug(f"WS SEND: Audio chunk {len(chunk)} bytes") # Debug
                        await self.ws.send(json.dumps({
                             "event": "audio_chunk",
                             "chunk": base64.b64encode(chunk).decode('utf-8')
                        }))
                    except websockets.exceptions.ConnectionClosed:
                        log.warning("WebSocket closed while trying to send audio. Stopping mic stream.")
                        self._rec_flag.clear() # Signal loop to stop
                        break
                    except Exception as e:
                         log.exception(f"Error sending audio chunk: {e}")
                         self._rec_flag.clear() # Stop on error
                         break
                else:
                    # Small sleep if read_chunk returns None (e.g., overflow or just no data)
                    # Prevents tight loop on no data
                    await asyncio.sleep(0.01)

        except Exception as e:
             log.exception(f"Error in mic stream sending loop: {e}")
        finally:
            log.info("Microphone stream sending loop finished.")
            self.audio.stop_input() # Ensure mic is stopped

            # Send end_of_audio signal if connection is still open
            if self.ws and self.ws.open:
                try:
                    log.info("Sending end_of_audio signal.")
                    await self.ws.send(json.dumps({"event": "end_of_audio"}))
                except websockets.exceptions.ConnectionClosed:
                     log.warning("Could not send end_of_audio: WebSocket closed.")
                except Exception as e:
                    log.error(f"Failed to send end_of_audio signal: {e}")


    async def _send_text_async(self, text: str):
        """Sends a text message over the WebSocket."""
        log.info(f"Attempting to send text: '{text[:30]}...'")
        # Ensure connection before sending
        if not await self.ensure_connected():
             log.error(f"Cannot send text '{text[:30]}...': WebSocket connection failed.")
             # Signal UI error?
             return

        if not self.ws or not self.ws.open:
            log.error(f"Cannot send text '{text[:30]}...': WebSocket not open after ensure_connected.")
            return

        log.info(f"Sending text message via WebSocket: {text}")
        try:
            # Ensure text buffer is clear if sending explicit text
            self._text_buf = ""
            await self.ws.send(json.dumps({
                "event": "text_input", # Verify this event name with OpenAI docs
                "text": text
            }))
            log.info("Text message sent successfully.")
        except websockets.exceptions.ConnectionClosed:
             log.error(f"Failed to send text message: WebSocket closed.")
             # No need to close here, recv loop or next ensure_connected will handle it.
        except Exception as e:
             log.exception(f"Failed to send text message: {e}")

    def start_talking(self):
        """Starts the microphone recording and streaming process."""
        if not self._rec_flag.is_set():
            log.info("Requesting start talking...")
            self._rec_flag.set()
            # Run the sender coroutine in the event loop
            asyncio.run_coroutine_threadsafe(self._mic_stream_sender(), self.loop)
            log.info("Mic stream sender task scheduled.")
            # Update GPIO LED if enabled
            if self.gpio_enabled:
                try:
                    # Ensure pin is configured before using
                    if GPIO_LED_PIN > 0:
                        GPIO.output(GPIO_LED_PIN, GPIO.HIGH)
                except Exception as e: log.error(f"GPIO Error setting LED HIGH: {e}")
        else:
            log.warning("Start talking requested, but already recording.")

    def stop_talking(self):
        """Stops the microphone recording and streaming process."""
        if self._rec_flag.is_set():
            log.info("Requesting stop talking...")
            self._rec_flag.clear() # Signal the sender loop to stop
            # The sender loop calls audio.stop_input() and sends end_of_audio
            log.info("Mic stream sender task signaled to stop.")
            # Update GPIO LED if enabled
            if self.gpio_enabled:
                 try:
                    # Ensure pin is configured before using
                    if GPIO_LED_PIN > 0:
                        GPIO.output(GPIO_LED_PIN, GPIO.LOW)
                 except Exception as e: log.error(f"GPIO Error setting LED LOW: {e}")
        else:
            log.warning("Stop talking requested, but not currently recording.")

    def send_text(self, text: str):
        """Sends a text message asynchronously."""
        if not text:
            log.warning("Send text requested with empty message.")
            return
        if not self.loop.is_running():
             log.error("Cannot send text: Asyncio loop is not running.")
             return
        # Run the text sender coroutine in the event loop
        asyncio.run_coroutine_threadsafe(self._send_text_async(text), self.loop)

    async def close_connection(self, reason="Client initiated close"):
         """Closes the WebSocket connection gracefully."""
         log.info(f"Closing WebSocket connection ({reason})...")

         # Stop recording if active
         if self._rec_flag.is_set():
             log.warning("Stopping active recording during connection close.")
             self.stop_talking() # This will signal the sender loop

         # Cancel receive task first
         if self._recv_task and not self._recv_task.done():
              log.debug("Cancelling receive task...")
              self._recv_task.cancel()
              try:
                  await asyncio.wait_for(self._recv_task, timeout=1.0) # Give it time to cancel
              except asyncio.CancelledError:
                  log.debug("Receive task successfully cancelled.")
              except asyncio.TimeoutError:
                   log.warning("Timeout waiting for receive task to cancel.")
              except Exception as e:
                  log.error(f"Error awaiting receive task cancellation: {e}")
         self._recv_task = None

         # Close WebSocket
         ws_to_close = self.ws
         # Mark as None immediately to prevent race conditions with ensure_connected
         self.ws = None
         self.session_id = None

         if ws_to_close and not ws_to_close.closed:
             log.debug("Sending close frame to WebSocket...")
             try:
                 await asyncio.wait_for(ws_to_close.close(reason=reason), timeout=3.0)
                 log.info("WebSocket connection closed gracefully.")
             except asyncio.TimeoutError:
                  log.warning("Timeout waiting for WebSocket close frame response.")
             except Exception as e:
                 log.error(f"Error closing WebSocket: {e}")
         else:
             log.debug("WebSocket already closed or not initialized.")


    def close(self):
        """Shuts down the client, closing audio resources and WebSocket."""
        log.info("Shutting down RealtimeClient...")

        # Ensure recording stops (calls stop_talking)
        if self._rec_flag.is_set():
            self.stop_talking()
            # Give a brief moment for stop signal to propagate if needed
            time.sleep(0.1)

        # Close WebSocket connection from the main thread via the loop
        if self.loop.is_running():
             log.debug("Scheduling WebSocket close via event loop...")
             future = asyncio.run_coroutine_threadsafe(self.close_connection(reason="Client shutdown"), self.loop)
             try:
                 future.result(timeout=5) # Wait for close task to complete
                 log.debug("WebSocket close task completed.")
             except TimeoutError:
                 log.warning("Timeout waiting for WebSocket close task.")
             except Exception as e:
                 log.error(f"Error waiting for WebSocket close task: {e}")

        # Stop the event loop thread
        if self.loop.is_running():
            log.debug("Stopping asyncio event loop...")
            self.loop.call_soon_threadsafe(self.loop.stop)
            # Don't necessarily join, let daemon thread exit
            # self._loop_thread.join(timeout=2)
            log.debug("Event loop stop scheduled.")

        # Close audio resources (this also terminates its PyAudio instance)
        if self.audio:
            self.audio.close()
            self.audio = None # Clear reference

        # Cleanup GPIO
        if self.gpio_enabled and GPIO:
            log.info("Cleaning up GPIO...")
            try:
                # Remove event detection first if button pin was valid
                if GPIO_BUTTON_PIN > 0:
                    try: GPIO.remove_event_detect(GPIO_BUTTON_PIN)
                    except RuntimeError as e: # Catch if channel wasn't set up for edge detection
                         log.debug(f"Ignoring GPIO remove_event_detect error (likely not set): {e}")
                    except Exception as e:
                         log.error(f"Error removing GPIO event detect: {e}")

                # Ensure LED is off if valid
                if GPIO_LED_PIN > 0:
                    try: GPIO.output(GPIO_LED_PIN, GPIO.LOW)
                    except Exception as e: log.error(f"GPIO Error setting LED LOW during cleanup: {e}")

                # Cleanup pins used
                pins_to_cleanup = []
                if GPIO_BUTTON_PIN > 0: pins_to_cleanup.append(GPIO_BUTTON_PIN)
                if GPIO_LED_PIN > 0: pins_to_cleanup.append(GPIO_LED_PIN)
                if pins_to_cleanup:
                     log.debug(f"Calling GPIO.cleanup for pins: {pins_to_cleanup}")
                     GPIO.cleanup(pins_to_cleanup)
                else:
                     # Call general cleanup if specific pins weren't identified but GPIO was thought enabled
                     log.debug("Calling general GPIO.cleanup()")
                     GPIO.cleanup()

                log.info("GPIO cleanup successful.")
            except Exception as e:
                log.error(f"Error during GPIO cleanup: {e}")
        elif ENABLE_GPIO and not HAS_GPIO:
             # Log if GPIO was enabled in config but failed to load/init
             log.warning("GPIO was enabled in config but library/hardware init failed earlier. No cleanup needed.")


        log.info("RealtimeClient shutdown complete.")


    # --- GPIO Handling ---
    def _setup_gpio(self):
        """Sets up GPIO pins for button and LED."""
        # Check library again just in case
        if not GPIO:
            log.warning("GPIO setup skipped: Library not available.")
            self.gpio_enabled = False
            return

        # Check if pins are configured (simple check for > 0)
        button_pin_valid = GPIO_BUTTON_PIN > 0
        led_pin_valid = GPIO_LED_PIN > 0

        if not (button_pin_valid or led_pin_valid):
             log.warning("GPIO setup skipped: Neither Button nor LED pin is configured (must be > 0).")
             self.gpio_enabled = False
             return

        log.info("Setting up GPIO...")
        try:
            # Explicitly set mode just before setup
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False) # Suppress warnings like 'channel already in use'

            # Set up LED pin if valid
            if led_pin_valid:
                GPIO.setup(GPIO_LED_PIN, GPIO.OUT, initial=GPIO.LOW)
                log.info(f"  - LED Pin ({GPIO_LED_PIN}) setup as OUT.")
            else:
                 log.info("  - LED Pin not configured.")

            # Set up Button pin if valid
            if button_pin_valid:
                pull_resistor = GPIO.PUD_UP if not BUTTON_ACTIVE_HIGH else GPIO.PUD_DOWN
                GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN, pull_up_down=pull_resistor)
                log.info(f"  - Button Pin ({GPIO_BUTTON_PIN}) setup as IN (Pull: {'UP' if pull_resistor == GPIO.PUD_UP else 'DOWN'}).")

                # Add event detection for the button press
                edge_detection = GPIO.FALLING if not BUTTON_ACTIVE_HIGH else GPIO.RISING
                # Clear any previous detection on this pin first
                try: GPIO.remove_event_detect(GPIO_BUTTON_PIN)
                except Exception: pass # Ignore if not set
                GPIO.add_event_detect(
                    GPIO_BUTTON_PIN,
                    edge_detection,
                    callback=self._button_callback,
                    bouncetime=300 # Debounce time in milliseconds
                )
                log.info(f"  - Button event detection added (Edge: {'Falling' if edge_detection == GPIO.FALLING else 'Rising'}).")
            else:
                 log.info("  - Button Pin not configured.")

            self.gpio_enabled = True # Mark as enabled only if setup succeeds
            log.info("GPIO setup complete.")

        except Exception as e:
            log.exception(f"Failed to setup GPIO: {e}")
            self.gpio_enabled = False # Disable GPIO functionality if setup fails
            # Attempt cleanup if setup failed partially
            try: GPIO.cleanup()
            except Exception: pass

    def _button_callback(self, channel):
        """Callback function executed on button press detected by GPIO event."""
        # Basic check to ensure it's the configured button pin triggering
        if channel != GPIO_BUTTON_PIN:
             log.warning(f"GPIO callback triggered on unexpected channel {channel}. Ignoring.")
             return

        log.info(f"Button press detected on channel {channel}.")
        if not self.loop or not self.loop.is_running():
            log.warning("GPIO callback triggered, but asyncio loop is not running. Action skipped.")
            return

        # Use call_soon_threadsafe as this callback runs in a separate thread managed by RPi.GPIO
        if self._rec_flag.is_set():
             log.debug("Button toggling: Requesting stop talking.")
             self.loop.call_soon_threadsafe(self.stop_talking)
        else:
             log.debug("Button toggling: Requesting start talking.")
             self.loop.call_soon_threadsafe(self.start_talking)