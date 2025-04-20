#!/usr/bin/env python3
"""
Realtime speech-to-speech for STR.
Handles audio input/output ensuring correct sample rates.
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
                  GPIO.setmode(GPIO.BCM) # Basic check to see if lib works
                  GPIO.cleanup() # Clean up any previous state
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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s",
)
log = logging.getLogger("realtime")

# --- Define Expected Rates ---
# Rate expected *from* OpenAI Realtime API (Check OpenAI Docs for gpt-4o-mini realtime!)
# Set to 16000 based on previous attempts, **VERIFY THIS**
API_AUDIO_OUTPUT_RATE = 16000
# Rate to send *to* OpenAI Realtime API (Usually 16k or 24k, check docs)
API_AUDIO_INPUT_RATE = 24000

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


        log.info(f"Mic Config: Index={self.device_index}, Input Rate={self.input_rate} Hz -> Target API Input Rate={API_AUDIO_INPUT_RATE} Hz")
        log.info(f"Playback Config: Target API Output Rate={API_AUDIO_OUTPUT_RATE} Hz -> Target Device Rate={OUTPUT_SAMPLE_RATE} Hz")

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

            # Downsampling for API (if necessary)
            target_api_rate = API_AUDIO_INPUT_RATE
            if self.input_rate != target_api_rate:
                if self.input_rate < target_api_rate:
                     log.warning(f"Mic input rate ({self.input_rate} Hz) is lower than API input rate ({target_api_rate} Hz). Upsampling not implemented, might affect quality.")
                     # Send as is for now
                elif self.input_rate > target_api_rate:
                    log.debug(f"Resampling mic audio from {self.input_rate} Hz to {target_api_rate} Hz for API input...")
                    factor = self.input_rate / target_api_rate
                    num_samples_in = len(audio)
                    num_samples_out = int(num_samples_in / factor)
                    if num_samples_in == 0 or num_samples_out == 0:
                         log.warning("Cannot resample zero-length audio chunk.")
                         return None
                    idx_orig = np.arange(num_samples_in)
                    idx_new = np.linspace(0, num_samples_in - 1, num_samples_out)
                    audio = np.interp(idx_new, idx_orig, audio).astype(np.int16)
                    log.debug(f" -> Resampled to {len(audio)} samples.")

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
        Play API audio (received as raw PCM bytes).
        Assumes input is API_AUDIO_OUTPUT_RATE Hz, mono, 16-bit.
        Resamples to OUTPUT_SAMPLE_RATE stereo 16-bit, attempts DAC, falls back.
        """
        if not data:
            log.warning("AudioHandler.play called with empty data.")
            return

        # Define input format based on API expectation
        in_rate = API_AUDIO_OUTPUT_RATE # **** CRITICAL: Use the correct rate ****
        in_channels = 1 # API usually sends mono
        in_width = 2 # API usually sends 16-bit

        # Define target output format from config
        target_rate = OUTPUT_SAMPLE_RATE
        target_channels = 2 # Force Stereo
        target_width = 2 # Force 16-bit (paInt16)

        def _playback():
            stream = None
            p_playback = None # Use a separate PyAudio instance for thread safety? Maybe overkill. Using self.p for now.
            try:
                p_playback = self.p # Or pyaudio.PyAudio() if issues arise

                # --- Prepare Data ---
                log.debug(f"Received {len(data)} bytes of audio data (Expected Format: {in_rate}Hz, {in_channels}Ch, {in_width*8}-bit).")
                # Ensure data length is multiple of sample width
                if len(data) % in_width != 0:
                     log.warning(f"Received audio data length ({len(data)}) is not multiple of sample width ({in_width}). Truncating.")
                     data = data[:len(data) - (len(data) % in_width)]

                samples = np.frombuffer(data, dtype=np.int16) # Assumes 16-bit input

                # --- Resample ---
                resampled_samples = samples # Default if no resampling needed
                if in_rate != target_rate:
                    log.debug(f"Resampling API audio from {in_rate} Hz to {target_rate} Hz using np.interp...")
                    num_samples_in = len(samples)
                    num_samples_out = int(num_samples_in * target_rate / in_rate)

                    if num_samples_in == 0 or num_samples_out == 0:
                         log.warning("Cannot resample zero-length audio.")
                         return # Exit playback thread

                    idx_orig = np.arange(num_samples_in) # Original sample indices
                    idx_new = np.linspace(0, num_samples_in - 1, num_samples_out) # Target sample indices
                    resampled_samples = np.interp(idx_new, idx_orig, samples).astype(np.int16)
                    log.debug(f" -> Resampled from {num_samples_in} to {len(resampled_samples)} samples.")
                else:
                    log.debug("API audio rate matches target rate. No resampling needed.")

                # --- Convert to Stereo ---
                stereo_samples = resampled_samples # Default if target is mono
                if target_channels == 2:
                    log.debug("Converting audio to stereo.")
                    # Ensure input was mono before repeating
                    if in_channels != 1:
                         log.warning(f"Input audio had {in_channels} channels, expected 1 for stereo conversion. Taking first channel if interleaved.")
                         # This assumes interleaved data if in_channels > 1
                         resampled_samples = resampled_samples[::in_channels]

                    stereo_samples = np.repeat(resampled_samples, 2) # Simple mono -> stereo duplication
                elif target_channels == 1 and in_channels > 1:
                     log.warning(f"Target is mono but input had {in_channels} channels. Taking first channel.")
                     stereo_samples = resampled_samples[::in_channels] # Extract first channel


                # --- Format for Output ---
                out_fmt = p_playback.get_format_from_width(target_width) # Should be paInt16
                output_data = stereo_samples.tobytes()

                # --- Attempt to Open Stream (Primary DAC first, then Fallback) ---
                target_device_index = DAC_PYAUDIO_INDEX
                stream = None
                try:
                    device_info = p_playback.get_device_info_by_index(target_device_index)
                    log.info(f"Realtime Play: Attempting DAC Index={target_device_index}, Name='{device_info.get('name', 'N/A')}' (Rate: {target_rate} Hz)")
                    stream = p_playback.open(
                        format=out_fmt,
                        channels=target_channels,
                        rate=target_rate, # Use the TARGET rate
                        output=True,
                        output_device_index=target_device_index,
                        frames_per_buffer=PLAYBACK_CHUNK,
                    )
                    log.info(f"Realtime Play: Successfully opened DAC Index={target_device_index}")

                except Exception as e_dac:
                    log.warning(f"Realtime Play: Failed DAC (Index={target_device_index}): {e_dac}. Trying default.")
                    try:
                        default_output_info = p_playback.get_default_output_device_info()
                        default_output_index = default_output_info['index']
                        log.info(f"Realtime Play: Attempting Default Index={default_output_index}, Name='{default_output_info.get('name', 'N/A')}' (Rate: {target_rate} Hz)")
                        stream = p_playback.open(
                            format=out_fmt,
                            channels=target_channels,
                            rate=target_rate, # Use the TARGET rate
                            output=True,
                            output_device_index=None, # Let PyAudio choose default
                            frames_per_buffer=PLAYBACK_CHUNK,
                        )
                        log.info(f"Realtime Play: Successfully opened Default Index={default_output_index}")
                    except Exception as e_default:
                        log.error(f"FATAL: Realtime Play: Failed to open both specified DAC and default output: {e_default}")
                        return # Cannot play

                # --- Play Audio ---
                log.info(f"Realtime Play: Playing {len(output_data)} bytes at {target_rate} Hz...")
                stream.write(output_data)
                stream.stop_stream() # Wait for buffer to finish
                log.info("Realtime Play: Finished.")

            except Exception as e:
                log.exception(f"Realtime Playback thread error: {e}")
            finally:
                if stream is not None:
                    try:
                        if stream.is_active(): stream.stop_stream()
                        stream.close()
                        log.debug("Realtime playback stream closed.")
                    except Exception as e_close:
                        log.error(f"Error closing realtime playback stream: {e_close}")
                # Terminate the separate PyAudio instance if it was created
                # if p_playback and p_playback != self.p:
                #     p_playback.terminate()

        # Start playback in a daemon thread
        threading.Thread(target=_playback, daemon=True).start()

    def close(self):
        """Clean up PyAudio resources."""
        log.info("Closing AudioHandler...")
        self.stop_input() # Ensure input stream is stopped and closed
        # Output streams are handled in their own threads, but terminate the main PyAudio instance
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
            self.audio = AudioHandler(mic_index) # Handles mic input and audio playback
        except ValueError as e: # Catch rate error from AudioHandler init
             log.error(f"Failed to initialize AudioHandler: {e}")
             raise # Re-raise to prevent client from starting incorrectly

        self._audio_buf = b"" # Buffer for incoming audio delta
        self._text_buf = ""   # Buffer for incoming text delta
        self._rec_flag = threading.Event() # Controls the mic recording loop

        self.loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._loop_thread.start()
        log.info("Asyncio event loop started in a separate thread.")

        self.ws: websockets.WebSocketClientProtocol | None = None
        self.session_id: str | None = None
        self._connection_lock = asyncio.Lock() # Prevent concurrent connection attempts
        # Block until initial connection attempt finishes
        future = asyncio.run_coroutine_threadsafe(self.ensure_connected(), self.loop)
        try:
             future.result(timeout=15) # Wait with timeout
             if not self.ws or not self.ws.open:
                  raise ConnectionError("Initial WebSocket connection failed.")
        except TimeoutError:
             log.error("Timeout waiting for initial WebSocket connection.")
             raise ConnectionError("Timeout during initial WebSocket connection.")
        except Exception as e:
             log.error(f"Error during initial WebSocket connection: {e}")
             raise ConnectionError(f"Initial WebSocket connection failed: {e}")


        self._recv_task: asyncio.Task | None = None

        # GPIO Setup (only if enabled and available)
        self.gpio_enabled = HAS_GPIO and ENABLE_GPIO and GPIO is not None
        if self.gpio_enabled:
            self._setup_gpio()
        else:
            log.info("GPIO not available or disabled by config; hardware button/LED inactive.")

        log.info(f"RealtimeClient configured for API Input: {API_AUDIO_INPUT_RATE}Hz, API Output: {API_AUDIO_OUTPUT_RATE}Hz")

    async def ensure_connected(self):
        """Connects or reconnects the WebSocket."""
        async with self._connection_lock:
            if self.ws and self.ws.open:
                log.debug("WebSocket already connected.") # Less noisy
                return

            if self.ws and not self.ws.closed:
                log.warning("WebSocket exists but is not open. Closing before reconnecting.")
                try:
                     await self.ws.close()
                except Exception: pass # Ignore errors during close if reconnecting anyway

            log.info("Attempting WebSocket connection...")
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

            # *** CRITICAL: Ensure requested format matches API_AUDIO_OUTPUT_RATE ***
            # Construct format string like "pcm_16" or "pcm_24"
            requested_api_output_format = f"pcm_{API_AUDIO_OUTPUT_RATE // 1000}"
            requested_api_input_format = {"encoding": "pcm_16", "sample_rate": API_AUDIO_INPUT_RATE}

            log.info(f"Requesting API Output Format: {requested_api_output_format}")
            log.info(f"Sending API Input Format: {requested_api_input_format['sample_rate']}Hz")


            try:
                self.ws = await websockets.connect(
                    self.URL,
                    extra_headers={"Authorization": f"Bearer {self.api_key}"},
                    ssl=ssl_ctx,
                    open_timeout=10, # Add timeout
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
                         self._recv_task.cancel()
                     self._recv_task = asyncio.create_task(self._recv_loop())
                else:
                     log.error(f"Unexpected message after connect (expected session_begins): {session_data}")
                     await self.ws.close()
                     self.ws = None
                     self.session_id = None

            except websockets.exceptions.InvalidStatusCode as e:
                 log.error(f"WebSocket connection failed (Invalid Status Code): {e.status_code} {e.body}")
                 self.ws = None
                 self.session_id = None
            except asyncio.TimeoutError:
                 log.error("WebSocket connection/session begin timed out.")
                 self.ws = None
                 self.session_id = None
            except Exception as e:
                 log.exception(f"WebSocket connection failed: {e}")
                 if self.ws and not self.ws.closed: # Try to close if partially opened
                      try: await self.ws.close()
                      except Exception: pass
                 self.ws = None
                 self.session_id = None


    async def _recv_loop(self):
        if not self.ws:
            log.error("Receive loop started without a WebSocket connection.")
            return
        log.info("WebSocket receive loop started.")
        try:
            async for message in self.ws:
                #log.debug(f"WS RECV: {message[:100]}") # Debug: log received messages
                if isinstance(message, str):
                    try:
                        ev = json.loads(message)
                        await self._handle_event(ev)
                    except json.JSONDecodeError:
                        log.warning(f"Received non-JSON message: {message[:100]}...")
                elif isinstance(message, bytes):
                     log.warning(f"Received unexpected binary data ({len(message)} bytes).")
        except websockets.exceptions.ConnectionClosedOK:
            log.info("WebSocket connection closed normally.")
        except websockets.exceptions.ConnectionClosedError as e:
            log.error(f"WebSocket connection closed with error: {e.code} {e.reason}")
            await self.close_connection() # Ensure clean state
        except asyncio.CancelledError:
             log.info("Receive loop cancelled.")
        except Exception as e:
            log.exception(f"WebSocket receive loop error: {e}")
        finally:
            log.info("WebSocket receive loop finished.")
            self.ws = None # Mark connection as closed
            self.session_id = None

    async def _handle_event(self, ev: dict):
        """Handles JSON events received from the WebSocket."""
        event_type = ev.get("event")

        if event_type == "audio":
            audio_chunk_b64 = ev.get("chunk")
            if audio_chunk_b64:
                try:
                    audio_chunk = base64.b64decode(audio_chunk_b64)
                    #log.debug(f"Received audio chunk: {len(audio_chunk)} bytes")
                    # Immediately play the chunk - buffering can cause delays/desync
                    self.audio.play(audio_chunk)
                except Exception as e:
                    log.error(f"Error decoding/playing audio chunk: {e}")
            else:
                 log.warning(f"Received audio event with no chunk: {ev}")

        elif event_type == "text":
             text_delta = ev.get("text")
             if text_delta is not None: # Check for None explicitly
                  # Broadcasting deltas might be too noisy for UI, buffer for final.
                  # If real-time transcription display is needed, handle 'transcript' events.
                  self._text_buf += text_delta
             else:
                  log.warning(f"Received text event with no text: {ev}")

        elif event_type == "transcript": # Handle transcriptions if needed
             transcript_text = ev.get("text")
             is_final = ev.get("final", False)
             log.info(f"Transcript{' (Final)' if is_final else ''}: {transcript_text}")
             # Optionally display transcripts in UI

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
             log.info(f"WebSocket session terminated by server. Reason: {ev.get('reason', 'N/A')}")
             await self.close_connection()

        elif event_type == "error":
            log.error(f"API Error received: {ev.get('message', 'No message')}")
            # Optionally close connection on severe errors

        elif event_type == "latency": # Handle latency info if provided
             log.debug(f"Latency Info: {ev}")

        else:
            # Ignore session_begins here as it's handled in connect
            if event_type != "session_begins":
                 log.warning(f"Unhandled WebSocket event type: {event_type} - Data: {ev}")


    async def _mic_stream_sender(self):
        """Reads from mic and sends audio data over WebSocket."""
        await self.ensure_connected() # Ensure connection before starting
        if not self.ws or not self.ws.open:
            log.error("Cannot start mic stream: WebSocket not connected after ensure_connected.")
            self._rec_flag.clear() # Ensure flag is off
            # Possibly signal UI error?
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
                chunk = self.audio.read_chunk()
                if chunk:
                    if self.ws and self.ws.open:
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
                         log.warning("WebSocket closed unexpectedly during mic stream. Stopping.")
                         self._rec_flag.clear()
                         break
                else:
                    # Small sleep if read_chunk returns None (e.g., overflow or just no data)
                    await asyncio.sleep(0.01)
        except Exception as e:
             log.exception(f"Error in mic stream sending loop: {e}")
        finally:
            log.info("Microphone stream sending loop finished.")
            self.audio.stop_input() # Ensure mic is stopped

            # Send end_of_audio signal - check if API requires this
            if self.ws and self.ws.open:
                try:
                    log.info("Sending end_of_audio signal.")
                    await self.ws.send(json.dumps({"event": "end_of_audio"}))
                except Exception as e:
                    log.error(f"Failed to send end_of_audio signal: {e}")


    async def _send_text_async(self, text: str):
        """Sends a text message over the WebSocket."""
        await self.ensure_connected() # Ensure connection before sending
        if not self.ws or not self.ws.open:
            log.error(f"Cannot send text '{text[:20]}...': WebSocket not connected.")
            # Signal UI error?
            return

        log.info(f"Sending text message: {text}")
        try:
            # Ensure text buffer is clear if sending explicit text
            self._text_buf = ""
            await self.ws.send(json.dumps({
                "event": "text_input", # Check API for correct event name
                "text": text
            }))
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
                try: GPIO.output(GPIO_LED_PIN, GPIO.HIGH)
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
                 try: GPIO.output(GPIO_LED_PIN, GPIO.LOW)
                 except Exception as e: log.error(f"GPIO Error setting LED LOW: {e}")
        else:
            log.warning("Stop talking requested, but not currently recording.")

    def send_text(self, text: str):
        """Sends a text message asynchronously."""
        if not text:
            log.warning("Send text requested with empty message.")
            return
        # Run the text sender coroutine in the event loop
        asyncio.run_coroutine_threadsafe(self._send_text_async(text), self.loop)

    async def close_connection(self):
         """Closes the WebSocket connection gracefully."""
         log.info("Closing WebSocket connection...")
         # Cancel receive task first
         if self._recv_task and not self._recv_task.done():
              self._recv_task.cancel()
              try: await self._recv_task
              except asyncio.CancelledError: pass # Expected
              except Exception as e: log.error(f"Error during receive task cancellation: {e}")
         self._recv_task = None

         # Close WebSocket
         ws_to_close = self.ws
         self.ws = None # Mark as None immediately to prevent further use
         self.session_id = None
         if ws_to_close and not ws_to_close.closed:
             try:
                 await ws_to_close.close()
                 log.info("WebSocket connection closed.")
             except Exception as e:
                 log.error(f"Error closing WebSocket: {e}")


    def close(self):
        """Shuts down the client, closing audio resources and WebSocket."""
        log.info("Shutting down RealtimeClient...")
        self.stop_talking() # Ensure recording stops

        # Close WebSocket connection from the main thread via the loop
        if self.loop.is_running():
             future = asyncio.run_coroutine_threadsafe(self.close_connection(), self.loop)
             try: future.result(timeout=5) # Wait for close to complete
             except TimeoutError: log.warning("Timeout waiting for WebSocket close.")
             except Exception as e: log.error(f"Error waiting for WebSocket close: {e}")

        # Stop the event loop thread
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
            # Don't necessarily join, let daemon thread exit
            # self._loop_thread.join(timeout=2)

        # Close audio resources
        self.audio.close()

        # Cleanup GPIO
        if self.gpio_enabled and GPIO:
            log.info("Cleaning up GPIO...")
            try:
                # Remove event detection first
                try: GPIO.remove_event_detect(GPIO_BUTTON_PIN)
                except Exception: pass # Ignore if not set up
                # Ensure LED is off
                try: GPIO.output(GPIO_LED_PIN, GPIO.LOW)
                except Exception: pass
                # Cleanup pins used
                pins_to_cleanup = []
                if GPIO_BUTTON_PIN > 0: pins_to_cleanup.append(GPIO_BUTTON_PIN)
                if GPIO_LED_PIN > 0: pins_to_cleanup.append(GPIO_LED_PIN)
                if pins_to_cleanup:
                     GPIO.cleanup(pins_to_cleanup)
                log.info("GPIO cleanup successful.")
            except Exception as e:
                log.error(f"Error during GPIO cleanup: {e}")

        log.info("RealtimeClient shutdown complete.")


    # --- GPIO Handling ---
    def _setup_gpio(self):
        """Sets up GPIO pins for button and LED."""
        if not (GPIO and GPIO_BUTTON_PIN > 0 and GPIO_LED_PIN > 0):
             log.warning("GPIO setup skipped: Library not available or pins not configured (>0).")
             self.gpio_enabled = False
             return

        log.info("Setting up GPIO...")
        try:
            GPIO.setmode(GPIO.BCM)
            # Set up LED pin
            GPIO.setup(GPIO_LED_PIN, GPIO.OUT, initial=GPIO.LOW)
            # Set up Button pin
            pull_resistor = GPIO.PUD_UP if not BUTTON_ACTIVE_HIGH else GPIO.PUD_DOWN
            GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN, pull_up_down=pull_resistor)

            # Add event detection for the button press
            edge_detection = GPIO.FALLING if not BUTTON_ACTIVE_HIGH else GPIO.RISING
            GPIO.add_event_detect(
                GPIO_BUTTON_PIN,
                edge_detection,
                callback=self._button_callback,
                bouncetime=300 # Debounce time in milliseconds
            )
            log.info(f"GPIO setup complete. Button Pin: {GPIO_BUTTON_PIN} (Edge: {'Falling' if edge_detection == GPIO.FALLING else 'Rising'}), LED Pin: {GPIO_LED_PIN}")

        except Exception as e:
            log.exception(f"Failed to setup GPIO: {e}")
            self.gpio_enabled = False # Disable GPIO if setup fails

    def _button_callback(self, channel):
        """Callback function executed on button press detected by GPIO event."""
        # Debounce check (optional, RPi.GPIO bouncetime should handle it)
        # time.sleep(0.05) # Short delay
        # current_state = GPIO.input(channel)
        # expected_state = GPIO.LOW if not BUTTON_ACTIVE_HIGH else GPIO.HIGH
        # if current_state != expected_state: return # Ignore if state bounced back

        log.info(f"Button press detected on channel {channel}.")
        if not self.loop.is_running():
            log.warning("GPIO callback triggered, but asyncio loop is not running.")
            return

        if self._rec_flag.is_set():
             self.loop.call_soon_threadsafe(self.stop_talking)
        else:
             self.loop.call_soon_threadsafe(self.start_talking)