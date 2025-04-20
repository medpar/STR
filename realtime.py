#!/usr/bin/env python3
"""
Realtime speech-to-speech for STR.

• Records from USB mic at configured rate (or device default), downsamples to 24 kHz for OpenAI streaming if necessary.
• Streams audio or typed text to OpenAI Realtime API and plays back at OUTPUT_SAMPLE_RATE Hz stereo.
• Toggles recording via web buttons or physical push‑button (GPIO), lights LED while recording.
"""

from __future__ import annotations
import os, ssl, json, base64, asyncio, threading, logging, time # Added time
import pyaudio, numpy as np, websockets
from typing import Callable # Added Callable

# GPIO Handling - Conditional Import
try:
    import RPi.GPIO as GPIO
    # Check if running on RPi platform, GPIO might be importable elsewhere but unusable
    try:
        # A quick check that will likely fail on non-RPi systems with RPi.GPIO stub installed
        GPIO.setmode(GPIO.BCM)
        GPIO.cleanup() # Clean up any previous state just in case
        _IS_RPI = True
    except Exception:
        _IS_RPI = False
        GPIO = None # Ensure GPIO object is None if setup fails
    HAS_GPIO = _IS_RPI
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

API_SAMPLE_RATE = 24000  # OpenAI Realtime expects 24 kHz mono

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

        log.info(f"Mic Config: Index={self.device_index}, Input Rate={self.input_rate} Hz, Target API Rate={API_SAMPLE_RATE} Hz")
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
            self.recording = True
            log.info("🎙️ Mic ON (Stream opened)")
        except Exception as e:
             log.exception(f"FATAL: Failed to open microphone input stream on device index {self.device_index}: {e}")
             self.recording = False
             self.input_stream = None # Ensure stream is None on failure
             # Re-raise or handle appropriately? For now, log and prevent recording.
             # raise # Optionally re-raise if startup should fail completely

    def read_chunk(self) -> bytes | None:
        if not self.recording or not self.input_stream or not self.input_stream.is_active():
            # log.debug("read_chunk called while not recording or stream inactive.") # Can be noisy
            return None
        try:
            raw = self.input_stream.read(self.chunk, exception_on_overflow=False)
            # Basic check if data is empty (might happen during stop)
            if not raw:
                return None

            audio = np.frombuffer(raw, dtype=np.int16)

            # Normalization (Optional)
            if MIC_NORMALISE:
                peak = np.max(np.abs(audio))
                # Avoid division by zero and excessive amplification of silence
                if peak > 100: # Only normalize if there's significant signal
                    gain = int(0.9 * 32767 / peak)
                    if gain > 1: # Only apply gain, don't attenuate here
                        audio = np.clip(audio * gain, -32768, 32767).astype(np.int16)
                # Else: keep audio as is if very quiet or silent

            # Downsampling for API (if necessary)
            if self.input_rate != API_SAMPLE_RATE:
                if self.input_rate < API_SAMPLE_RATE:
                     log.warning(f"Mic input rate ({self.input_rate} Hz) is lower than API rate ({API_SAMPLE_RATE} Hz). Upsampling not implemented, might affect quality.")
                     # Basic upsampling (repeat samples) - consider libraries like resampy for quality
                     # factor = API_SAMPLE_RATE // self.input_rate
                     # audio = np.repeat(audio, factor) # Very basic
                     # For now, send as is, OpenAI might handle it or quality degrades.
                elif self.input_rate > API_SAMPLE_RATE:
                    factor = self.input_rate / API_SAMPLE_RATE
                    if factor == int(factor): # Simple decimation for integer factors
                         audio = audio[::int(factor)]
                    else: # Use interpolation for non-integer factors (more computationally intensive)
                         log.debug(f"Non-integer downsampling needed ({self.input_rate} -> {API_SAMPLE_RATE}). Using interpolation.")
                         idx_orig = np.arange(len(audio))
                         target_len = int(len(audio) / factor)
                         idx_new = np.linspace(0, len(audio) - 1, target_len)
                         audio = np.interp(idx_new, idx_orig, audio).astype(np.int16)

            return audio.tobytes()

        except OSError as e:
            # Specific handling for Input overflowed error which might be recoverable
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
            # Short delay to allow the read loop to potentially finish its current read
            time.sleep(0.05)

        if self.input_stream:
            try:
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
        Play API audio (received as 24kHz mono PCM16).
        Resamples to OUTPUT_SAMPLE_RATE stereo, attempts DAC, falls back to default.
        Runs playback in a separate thread.
        """
        if not data:
            log.warning("AudioHandler.play called with empty data.")
            return

        def _playback():
            stream = None
            try:
                # --- Prepare Data ---
                # Input: 24kHz, 1-channel, 16-bit PCM
                in_rate = API_SAMPLE_RATE
                in_channels = 1
                in_width = 2 # 16-bit
                samples = np.frombuffer(data, dtype=np.int16)

                # --- Resample ---
                # Target: OUTPUT_SAMPLE_RATE Hz
                out_rate = OUTPUT_SAMPLE_RATE
                if in_rate != out_rate:
                    log.debug(f"Resampling API audio from {in_rate} Hz to {out_rate} Hz.")
                    idx_orig = np.arange(len(samples))
                    target_len = int(len(samples) * out_rate / in_rate)
                    idx_new = np.linspace(0, len(samples) - 1, target_len)
                    samples = np.interp(idx_new, idx_orig, samples).astype(np.int16)

                # --- Convert to Stereo ---
                # Target: 2 channels
                out_channels = 2
                if out_channels == 2:
                    stereo_samples = np.repeat(samples, 2) # Simple mono -> stereo duplication
                else: # If target was mono (unlikely for playback)
                    stereo_samples = samples

                # --- Format for Output ---
                out_width = 2 # Target 16-bit
                out_fmt = self.p.get_format_from_width(out_width)
                output_data = stereo_samples.tobytes()

                # --- Attempt to Open Stream (Primary DAC first, then Fallback) ---
                target_device_info = None
                try:
                    target_device_info = self.p.get_device_info_by_index(DAC_PYAUDIO_INDEX)
                    log.info(f"Realtime Play: Attempting DAC Index={DAC_PYAUDIO_INDEX}, Name='{target_device_info.get('name', 'N/A')}'")
                    stream = self.p.open(
                        format=out_fmt,
                        channels=out_channels,
                        rate=out_rate,
                        output=True,
                        output_device_index=DAC_PYAUDIO_INDEX,
                        frames_per_buffer=PLAYBACK_CHUNK,
                    )
                    log.info(f"Realtime Play: Successfully opened DAC Index={DAC_PYAUDIO_INDEX}")

                except Exception as e_dac:
                    log.warning(f"Realtime Play: Failed DAC (Index={DAC_PYAUDIO_INDEX}): {e_dac}. Trying default.")
                    target_device_info = None
                    try:
                        default_output_info = self.p.get_default_output_device_info()
                        default_output_index = default_output_info['index']
                        log.info(f"Realtime Play: Attempting Default Index={default_output_index}, Name='{default_output_info.get('name', 'N/A')}'")
                        stream = self.p.open(
                            format=out_fmt,
                            channels=out_channels,
                            rate=out_rate,
                            output=True,
                            frames_per_buffer=PLAYBACK_CHUNK,
                        )
                        log.info(f"Realtime Play: Successfully opened Default Index={default_output_index}")
                    except Exception as e_default:
                        log.error(f"FATAL: Realtime Play: Failed to open both specified DAC and default output: {e_default}")
                        return # Cannot play

                # --- Play Audio ---
                log.info(f"Realtime Play: Playing {len(output_data)} bytes...")
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

        # Start playback in a daemon thread so it doesn't block the main loop
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

        self.audio = AudioHandler(mic_index) # Handles mic input and audio playback
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
        self._connect_task = asyncio.run_coroutine_threadsafe(self.ensure_connected(), self.loop)
        self._connect_task.result() # Block until initial connection attempt finishes

        self._recv_task: asyncio.Task | None = None

        # GPIO Setup (only if enabled and available)
        self.gpio_enabled = HAS_GPIO and ENABLE_GPIO
        if self.gpio_enabled:
            self._setup_gpio()
        else:
            log.info("GPIO not available or disabled by config; hardware button/LED inactive.")

    async def ensure_connected(self):
        """Connects or reconnects the WebSocket."""
        async with self._connection_lock:
            if self.ws and self.ws.open:
                log.info("WebSocket already connected.")
                return

            if self.ws and not self.ws.closed:
                log.warning("WebSocket exists but is not open. Closing before reconnecting.")
                await self.ws.close()

            log.info("Attempting WebSocket connection...")
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

            try:
                self.ws = await websockets.connect(
                    self.URL,
                    extra_headers={"Authorization": f"Bearer {self.api_key}"},
                    ssl=ssl_ctx,
                    open_timeout=10, # Add timeout
                )
                await self.ws.send(json.dumps({
                    "model": self.MODEL,
                    "language": "es", # Specify language
                    "stream": True, # Enable streaming
                    "voice": self.voice,
                    "response_format": {"audio_format": "pcm_16000"}, # OpenAI currently outputs 16kHz for realtime? Let's try. Adjust AudioHandler.play if needed. *** CHECK DOCS ***
                    # If API still outputs 24kHz, change above to pcm_24000 and keep AudioHandler as is.
                    # If API outputs 16kHz, change AudioHandler.play `in_rate` to 16000.
                    "input_format": {"encoding": "pcm_16", "sample_rate": API_SAMPLE_RATE}, # Send 24kHz
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
                     log.error(f"Unexpected message after connect: {session_data}")
                     await self.ws.close()
                     self.ws = None
                     self.session_id = None

            except websockets.exceptions.InvalidStatusCode as e:
                 log.error(f"WebSocket connection failed (Invalid Status Code): {e.status_code} {e.reason}")
                 self.ws = None
                 self.session_id = None
                 # Consider adding retry logic here
            except Exception as e:
                 log.exception(f"WebSocket connection failed: {e}")
                 self.ws = None
                 self.session_id = None
                 # Consider adding retry logic here

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
                    # Handle binary audio data if the API sends it directly (unlikely with current settings)
                    log.debug("Received binary data (unexpected with PCM format requested)")
                    # self.audio.play(message) # If API directly sends playable audio bytes
        except websockets.exceptions.ConnectionClosedOK:
            log.info("WebSocket connection closed normally.")
        except websockets.exceptions.ConnectionClosedError as e:
            log.error(f"WebSocket connection closed with error: {e.code} {e.reason}")
            # Attempt reconnect?
            await self.close_connection() # Ensure clean state
            # Optional: Schedule reconnect attempt
            # asyncio.create_task(self.ensure_connected())
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

        if event_type == "audio": # Assuming format is {"event": "audio", "chunk": "base64_encoded_pcm"}
            audio_chunk_b64 = ev.get("chunk")
            if audio_chunk_b64:
                try:
                    audio_chunk = base64.b64decode(audio_chunk_b64)
                    self._audio_buf += audio_chunk
                    # Play in larger chunks? Or immediately? Playing immediately might be choppy.
                    # Let's buffer a bit. Adjust buffer size as needed.
                    if len(self._audio_buf) > 4096: # Play ~170ms chunks (at 24kHz)
                       self.audio.play(self._audio_buf)
                       self._audio_buf = b""
                except Exception as e:
                    log.error(f"Error decoding/buffering audio chunk: {e}")
            else:
                 log.warning(f"Received audio event with no chunk: {ev}")

        elif event_type == "text": # Assuming {"event": "text", "text": "..."}
             text_delta = ev.get("text")
             if text_delta:
                  self._text_buf += text_delta
                  # Do we broadcast deltas or only final text? Let's broadcast final for now.
             else:
                  log.warning(f"Received text event with no text: {ev}")

        elif event_type == "transcript": # Handle transcriptions if needed
             transcript_text = ev.get("text")
             is_final = ev.get("final", False)
             log.info(f"Transcript{' (Final)' if is_final else ''}: {transcript_text}")
             # Optionally display transcripts in UI

        elif event_type == "session_terminates":
             log.info(f"WebSocket session terminated by server. Reason: {ev.get('reason', 'N/A')}")
             await self.close_connection()

        elif event_type == "error":
            log.error(f"API Error received: {ev.get('message', 'No message')}")
            # Consider closing connection on severe errors

        elif event_type == "latency": # Handle latency info if provided
             log.debug(f"Latency Info: {ev}")

        elif event_type == "speech_end" or ev.get("final"): # Heuristic end-of-response
             # Play any remaining buffered audio
             if self._audio_buf:
                  log.debug(f"Playing remaining audio buffer ({len(self._audio_buf)} bytes)")
                  self.audio.play(self._audio_buf)
                  self._audio_buf = b""
             # Broadcast final text
             if self._text_buf and self.on_text:
                  log.info(f"Broadcasting final text: {self._text_buf}")
                  self.on_text(self._text_buf)
                  self._text_buf = "" # Clear buffer

        else:
            log.warning(f"Unhandled WebSocket event type: {event_type} - Data: {ev}")


    async def _mic_stream_sender(self):
        """Reads from mic and sends audio data over WebSocket."""
        if not self.ws or not self.ws.open:
            log.error("Cannot start mic stream: WebSocket not connected.")
            self._rec_flag.clear() # Ensure flag is off
            return

        self.audio.start_input() # Open the mic stream
        if not self.audio.recording: # Check if mic failed to open
            log.error("Mic stream failed to start. Aborting sender task.")
            self._rec_flag.clear()
            return

        log.info("Microphone stream sending loop started.")
        while self._rec_flag.is_set():
            chunk = self.audio.read_chunk()
            if chunk:
                if self.ws and self.ws.open:
                    try:
                        #log.debug(f"WS SEND: Audio chunk {len(chunk)} bytes") # Debug
                        # Send as binary frame for efficiency if API supports it, else base64 encode
                        # await self.ws.send(chunk) # If binary is supported
                        await self.ws.send(json.dumps({
                             "event": "audio_chunk", # Or appropriate event name
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
                # Small sleep if read_chunk returns None (e.g., overflow or end of stream)
                await asyncio.sleep(0.01)

        log.info("Microphone stream sending loop finished.")
        self.audio.stop_input() # Ensure mic is stopped

        # Optionally send an "end of audio" signal if required by API
        # if self.ws and self.ws.open:
        #     try:
        #         await self.ws.send(json.dumps({"event": "end_of_audio"}))
        #         log.info("Sent end_of_audio signal.")
        #     except Exception as e:
        #         log.error(f"Failed to send end_of_audio signal: {e}")

    async def _send_text_async(self, text: str):
        """Sends a text message over the WebSocket."""
        if not self.ws or not self.ws.open:
            log.error(f"Cannot send text '{text[:20]}...': WebSocket not connected.")
            # Optionally: try reconnecting first
            # await self.ensure_connected()
            # if not self.ws or not self.ws.open: return
            return

        log.info(f"Sending text message: {text}")
        try:
            await self.ws.send(json.dumps({
                "event": "text_input", # Or the correct event name expected by the API
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
            log.info("Mic stream sender task started.")
            # Update GPIO LED if enabled
            if self.gpio_enabled:
                try:
                    GPIO.output(GPIO_LED_PIN, GPIO.HIGH)
                except Exception as e:
                    log.error(f"GPIO Error setting LED HIGH: {e}")
        else:
            log.warning("Start talking requested, but already recording.")

    def stop_talking(self):
        """Stops the microphone recording and streaming process."""
        if self._rec_flag.is_set():
            log.info("Requesting stop talking...")
            self._rec_flag.clear() # Signal the sender loop to stop
            # The sender loop will call audio.stop_input() upon exiting
            log.info("Mic stream sender task signaled to stop.")
            # Update GPIO LED if enabled
            if self.gpio_enabled:
                 try:
                     GPIO.output(GPIO_LED_PIN, GPIO.LOW)
                 except Exception as e:
                     log.error(f"GPIO Error setting LED LOW: {e}")
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
         if self._recv_task and not self._recv_task.done():
              self._recv_task.cancel()
              try:
                  await self._recv_task
              except asyncio.CancelledError:
                  pass # Expected
              except Exception as e:
                  log.error(f"Error during receive task cancellation: {e}")
              self._recv_task = None

         if self.ws and not self.ws.closed:
             try:
                 await self.ws.close()
                 log.info("WebSocket connection closed.")
             except Exception as e:
                 log.error(f"Error closing WebSocket: {e}")
         self.ws = None
         self.session_id = None

    def close(self):
        """Shuts down the client, closing audio resources and WebSocket."""
        log.info("Shutting down RealtimeClient...")
        self.stop_talking() # Ensure recording stops

        # Close WebSocket connection from the main thread via the loop
        if self.loop.is_running():
             future = asyncio.run_coroutine_threadsafe(self.close_connection(), self.loop)
             try:
                 future.result(timeout=5) # Wait for close to complete
             except TimeoutError:
                 log.warning("Timeout waiting for WebSocket close.")
             except Exception as e:
                 log.error(f"Error waiting for WebSocket close: {e}")

        # Stop the event loop thread
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
            # self._loop_thread.join(timeout=2) # Wait for thread to finish
            # if self._loop_thread.is_alive():
            #      log.warning("Event loop thread did not stop gracefully.")

        # Close audio resources
        self.audio.close()

        # Cleanup GPIO
        if self.gpio_enabled:
            log.info("Cleaning up GPIO...")
            try:
                # Ensure LED is off
                GPIO.output(GPIO_LED_PIN, GPIO.LOW)
                GPIO.cleanup([GPIO_BUTTON_PIN, GPIO_LED_PIN])
                log.info("GPIO cleanup successful.")
            except Exception as e:
                log.error(f"Error during GPIO cleanup: {e}")

        log.info("RealtimeClient shutdown complete.")


    # --- GPIO Handling ---
    def _setup_gpio(self):
        """Sets up GPIO pins for button and LED."""
        log.info("Setting up GPIO...")
        try:
            GPIO.setmode(GPIO.BCM)
            # Set up LED pin
            GPIO.setup(GPIO_LED_PIN, GPIO.OUT, initial=GPIO.LOW)
            # Set up Button pin with pull-up or pull-down resistor
            # If button connects pin to GND when pressed (Active LOW): Use PUD_UP
            # If button connects pin to 3.3V when pressed (Active HIGH): Use PUD_DOWN
            pull_resistor = GPIO.PUD_UP if not BUTTON_ACTIVE_HIGH else GPIO.PUD_DOWN
            GPIO.setup(GPIO_BUTTON_PIN, GPIO.IN, pull_up_down=pull_resistor)

            # Add event detection for the button press
            # Detect the edge corresponding to the button being pressed
            edge_detection = GPIO.FALLING if not BUTTON_ACTIVE_HIGH else GPIO.RISING
            GPIO.add_event_detect(
                GPIO_BUTTON_PIN,
                edge_detection,
                callback=self._button_callback,
                bouncetime=300 # Debounce time in milliseconds
            )
            log.info(f"GPIO setup complete. Button Pin: {GPIO_BUTTON_PIN} (Active High: {BUTTON_ACTIVE_HIGH}, Edge: {'Falling' if edge_detection == GPIO.FALLING else 'Rising'}), LED Pin: {GPIO_LED_PIN}")
            # GPIO event detection runs in its own thread managed by RPi.GPIO
        except Exception as e:
            log.exception(f"Failed to setup GPIO: {e}")
            self.gpio_enabled = False # Disable GPIO if setup fails

    def _button_callback(self, channel):
        """Callback function executed on button press detected by GPIO event."""
        # This callback runs in a separate thread created by RPi.GPIO
        # Avoid blocking operations here.
        log.info(f"Button press detected on channel {channel}.")
        if self._rec_flag.is_set():
             # Using call_soon_threadsafe to interact with methods controlling state/asyncio tasks
             self.loop.call_soon_threadsafe(self.stop_talking)
        else:
             self.loop.call_soon_threadsafe(self.start_talking)

    # Remove _poll_button method as event detection is now used.


# Example Usage (for testing directly)
if __name__ == "__main__":
    log.info("Starting RealtimeClient test...")
    logging.getLogger().setLevel(logging.DEBUG) # Enable debug logs for testing

    def handle_broadcast(msg: str):
        print(f"\n>>> BROADCAST RECEIVED: {msg}\n")

    try:
        client = RealtimeClient(
            instructions=INSTRUCTIONS,
            voice="nova",
            on_text=handle_broadcast
        )

        print("\nRealtime Client Initialized.")
        print("Commands: start, stop, text <your message>, quit")

        while True:
            try:
                cmd = input("Enter command: ").strip().lower()
                if cmd == "start":
                    client.start_talking()
                elif cmd == "stop":
                    client.stop_talking()
                elif cmd.startswith("text "):
                     message = cmd[5:].strip()
                     if message:
                         client.send_text(message)
                     else:
                         print("Please provide text after 'text '")
                elif cmd == "quit":
                    break
                else:
                    print("Unknown command.")
            except EOFError: # Handle Ctrl+D
                break
            except KeyboardInterrupt: # Handle Ctrl+C
                 break

    except Exception as e:
        log.exception(f"Error during RealtimeClient test: {e}")
    finally:
        if 'client' in locals() and client:
            print("Shutting down client...")
            client.close()
        print("RealtimeClient test finished.")