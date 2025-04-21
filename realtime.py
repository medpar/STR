# ================================================
# File: /realtime.py
# ================================================
#!/usr/bin/env python3
"""
Realtime transcription and response using OpenAI Realtime API.
Based on the provided OpenAI example, integrated into the Flask app structure.
Handles audio recording, WebSocket communication, and triggers callbacks for text/audio.
"""

import asyncio
import websockets
import json
import pyaudio
import base64
import logging
import os
import ssl
import threading
import time
import queue
import wave # For saving temporary audio files
from datetime import datetime

from config import (
    OPENAI_MODEL_REALTIME,
    MIC_DEVICE_INDEX,
    MIC_CHUNK,
    BUTTON_ACTIVE_HIGH # Import this to determine pull resistor setup
)
from audio_manager import play_audio, terminate_pyaudio_instance as terminate_audio_manager_pyaudio

# Load API Key
from dotenv import load_dotenv
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

log = logging.getLogger(__name__) # Use logger instance

# Constants matching the example and typical API expectations
REALTIME_API_URL = "wss://api.openai.com/v1/realtime"
AUDIO_RATE = 24000  # Rate expected by OpenAI Realtime API
AUDIO_CHANNELS = 1
AUDIO_FORMAT = pyaudio.paInt16
AUDIO_FORMAT_STR = "pcm16" # For session config
AUDIO_WIDTH = pyaudio.PyAudio().get_sample_size(AUDIO_FORMAT)

TEMP_AUDIO_DIR = os.path.join(os.path.dirname(__file__), "audio_files", "temp_realtime")
os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)

class AudioHandler:
    """
    Handles audio input using PyAudio specifically for the RealtimeClient.
    Records at the rate required by the API (24kHz Mono).
    Does NOT handle playback (delegated to audio_manager).
    """
    def __init__(self, device_index=None):
        self.p = None
        self.stream = None
        self.device_index = device_index
        self.chunk_size = MIC_CHUNK # Use chunk size from config
        self.format = AUDIO_FORMAT
        self.channels = AUDIO_CHANNELS
        self.rate = AUDIO_RATE
        self._is_recording = False
        self._lock = threading.Lock()
        log.info(f"AudioHandler initialized for device index {self.device_index} (Rate: {self.rate} Hz)")

    def _initialize_pyaudio(self):
        with self._lock:
            if self.p is None:
                log.debug("Initializing PyAudio instance for AudioHandler.")
                self.p = pyaudio.PyAudio()

    def start_recording(self):
        """Start the audio input stream."""
        self._initialize_pyaudio()
        with self._lock:
            if self._is_recording:
                log.warning("Recording already active.")
                return True
            if self.stream is not None:
                log.warning("Stream exists but not marked as recording. Closing existing stream.")
                self._close_stream_safe()

            log.info(f"Attempting to start audio stream (Device: {self.device_index}, Rate: {self.rate} Hz)")
            try:
                self.stream = self.p.open(
                    format=self.format,
                    channels=self.channels,
                    rate=self.rate,
                    input=True,
                    frames_per_buffer=self.chunk_size,
                    input_device_index=self.device_index
                )
                self._is_recording = True
                log.info("Audio input stream started successfully.")
                return True
            except Exception as e:
                log.exception(f"Failed to open audio input stream on device {self.device_index}: {e}")
                self.stream = None
                self._is_recording = False
                return False

    def stop_recording(self):
        """Stop the audio input stream."""
        with self._lock:
            if not self._is_recording:
                log.debug("Recording already stopped.")
                return
            self._close_stream_safe()
            self._is_recording = False
            log.info("Audio input stream stopped.")

    def _close_stream_safe(self):
        """Safely stop and close the PyAudio stream."""
        if self.stream:
            try:
                if self.stream.is_active():
                    self.stream.stop_stream()
                self.stream.close()
                log.debug("Audio stream closed.")
            except Exception as e:
                log.error(f"Error closing audio stream: {e}")
            finally:
                self.stream = None

    def read_chunk(self):
        """Read a single chunk of audio if recording."""
        with self._lock:
            if not self._is_recording or not self.stream or not self.stream.is_active():
                # log.debug("Stream is not active or recording has stopped, cannot read chunk.")
                return None
            try:
                data = self.stream.read(self.chunk_size, exception_on_overflow=False)
                return data
            except OSError as e:
                if "Input overflowed" in str(e):
                    log.warning("Audio input overflow detected.")
                else:
                    log.error(f"Error reading audio chunk: {e}")
                return None
            except Exception as e:
                log.exception(f"Unexpected error reading audio chunk: {e}")
                # Consider stopping recording on unexpected errors
                # self._is_recording = False
                # self._close_stream_safe()
                return None

    def cleanup(self):
        """Clean up resources by stopping the stream and terminating PyAudio."""
        log.debug("Cleaning up AudioHandler resources.")
        self.stop_recording()
        with self._lock:
            if self.p:
                try:
                    self.p.terminate()
                    log.debug("PyAudio instance terminated for AudioHandler.")
                except Exception as e:
                    log.error(f"Error terminating PyAudio for AudioHandler: {e}")
                finally:
                    self.p = None


class RealtimeClient:
    """
    Client for interacting with the OpenAI Realtime API via WebSocket.
    Manages connection, event handling, and audio streaming using AudioHandler.
    Uses asyncio for WebSocket communication running in a separate thread.
    Communicates with the main Flask app via callbacks.
    """
    def __init__(self, instructions, voice, mic_index, on_text_delta, on_audio_chunk, on_response_done, on_status_update):
        self.instructions = instructions
        self.voice = voice
        self.mic_index = mic_index
        self.api_key = OPENAI_API_KEY
        self.model = OPENAI_MODEL_REALTIME
        self.url = f"{REALTIME_API_URL}?model={self.model}"

        # Callbacks to Flask app
        self.on_text_delta = on_text_delta
        self.on_audio_chunk = on_audio_chunk
        self.on_response_done = on_response_done
        self.on_status_update = on_status_update

        self.audio_handler = AudioHandler(device_index=self.mic_index)
        self.ws = None
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

        self.loop = None
        self._thread = None
        self._running = threading.Event()
        self._connected = threading.Event()
        self._recording = threading.Event()
        self._audio_sender_task = None
        self._receive_task = None

        self.session_config = {
            "modalities": ["audio", "text"],
            "instructions": self.instructions,
            "voice": self.voice,
            "input_audio_format": AUDIO_FORMAT_STR,
            "output_audio_format": AUDIO_FORMAT_STR,
            #"input_audio_sampling_rate": AUDIO_RATE,
            #"output_audio_sampling_rate": AUDIO_RATE,
            "turn_detection": None, # Manual turn detection
            "input_audio_transcription": {
                "model": "whisper-1" # Use default whisper
            },
            "temperature": 0.7 # Example temperature
        }

        if not self.api_key:
            log.error("OPENAI_API_KEY not found in environment variables.")
            self.on_status_update("Error: OpenAI API Key missing.")
            # Prevent startup if key is missing
            raise ValueError("OpenAI API Key is required.")

    # --- Public Methods (Thread-Safe) ---

    def start_background_loop(self):
        """Starts the asyncio event loop in a separate thread."""
        if self._thread is not None and self._thread.is_alive():
            log.warning("Background loop already running.")
            return
        log.info("Starting RealtimeClient background asyncio loop...")
        self._running.set()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop_background_loop(self):
        """Stops the asyncio event loop and cleans up."""
        if not self._running.is_set():
            log.info("Background loop already stopped.")
            return
        log.info("Stopping RealtimeClient background loop...")
        self._running.clear() # Signal loop to stop

        if self.loop and self.loop.is_running():
            # Schedule cleanup task in the loop
            asyncio.run_coroutine_threadsafe(self._async_cleanup(), self.loop)

        # Wait for the thread to finish
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                log.warning("Background loop thread did not exit cleanly.")
            self._thread = None
        log.info("RealtimeClient background loop stopped.")
        # Explicitly terminate audio manager's PyAudio if needed
        # terminate_audio_manager_pyaudio()


    def start_talking(self):
        """Starts audio recording and streaming. (Called from Flask/GPIO thread)"""
        if not self._running.is_set() or not self.loop:
            log.error("Cannot start talking: background loop not running.")
            self.on_status_update("Error: Realtime service not running.")
            return
        if not self._connected.is_set():
             log.error("Cannot start talking: not connected to WebSocket.")
             self.on_status_update("Error: Not connected.")
             return
        if self._recording.is_set():
             log.warning("Already recording.")
             return

        log.info("Request received to start talking...")
        self.on_status_update("Recording...")
        # Schedule the async start logic in the client's event loop
        asyncio.run_coroutine_threadsafe(self._async_start_talking(), self.loop)

    def stop_talking(self):
        """Stops audio recording/streaming and commits buffer. (Called from Flask/GPIO thread)"""
        if not self._running.is_set() or not self.loop:
            log.error("Cannot stop talking: background loop not running.")
            return
        if not self._recording.is_set():
             log.warning("Not currently recording.")
             return

        log.info("Request received to stop talking...")
        self.on_status_update("Processing...")
        # Schedule the async stop logic in the client's event loop
        asyncio.run_coroutine_threadsafe(self._async_stop_talking(), self.loop)

    def send_text(self, text):
        """Sends a text message. (Called from Flask thread)"""
        if not self._running.is_set() or not self.loop:
            log.error("Cannot send text: background loop not running.")
            self.on_status_update("Error: Realtime service not running.")
            return
        if not self._connected.is_set():
             log.error("Cannot send text: not connected to WebSocket.")
             self.on_status_update("Error: Not connected.")
             return
        if self._recording.is_set():
            log.warning("Cannot send text while recording audio.")
            self.on_status_update("Error: Cannot send text while recording.")
            return

        log.info(f"Request received to send text: '{text[:50]}...'")
        # Schedule the async send logic
        asyncio.run_coroutine_threadsafe(self._async_send_text(text), self.loop)

    # --- Internal Asyncio Methods (Run in background loop) ---

    def _run_loop(self):
        """The main function executed in the background thread."""
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._main_async_logic())
        except Exception as e:
            log.exception("Exception in RealtimeClient background loop:")
        finally:
            if self.loop and self.loop.is_running():
                self.loop.close()
            self.loop = None
            log.info("RealtimeClient asyncio loop finished.")

    async def _main_async_logic(self):
        """Core asyncio logic: connect, receive, handle reconnections."""
        while self._running.is_set():
            try:
                log.info("Attempting WebSocket connection...")
                self.on_status_update("Connecting...")
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "OpenAI-Beta": "realtime=v1"
                }
                async with websockets.connect(
                    self.url,
                    extra_headers=headers,
                    ssl=self.ssl_context,
                    open_timeout=10, # Add timeout
                    close_timeout=10
                ) as ws:
                    self.ws = ws
                    self._connected.set()
                    log.info("WebSocket connected successfully.")
                    self.on_status_update("Connected. Ready.")

                    # Configure session
                    await self._send_event({
                        "type": "session.update",
                        "session": self.session_config
                    })
                    log.info("Session configured.")

                    # Send initial create response to start listening
                    # Important: Do this *after* session config
                    await self._send_event({"type": "response.create"})
                    log.debug("Sent initial response.create.")

                    # Start receiving events
                    self._receive_task = asyncio.create_task(self._receive_events())
                    await self._receive_task # Keep connection alive

            except websockets.exceptions.ConnectionClosedOK:
                log.info("WebSocket connection closed normally.")
            except websockets.exceptions.ConnectionClosedError as e:
                log.error(f"WebSocket connection closed with error: {e.code} {e.reason}")
            except ConnectionRefusedError:
                 log.error("Connection refused. Check network or server status.")
            except ssl.SSLError as e:
                log.error(f"SSL Error during connection: {e}")
            except asyncio.TimeoutError:
                log.error("Connection attempt timed out.")
            except Exception as e:
                log.exception("Unexpected error during WebSocket connection/reception:")

            finally:
                # Cleanup before potential retry
                self.ws = None
                self._connected.clear()
                if self._recording.is_set(): # If disconnected while recording
                    log.warning("Disconnected while recording was active. Stopping recording.")
                    await self._async_stop_talking(force_stop=True) # Force stop without commit
                if self._receive_task and not self._receive_task.done():
                    self._receive_task.cancel()
                self.on_status_update("Disconnected.")

                if self._running.is_set():
                    log.info("Attempting reconnect in 5 seconds...")
                    await asyncio.sleep(5)
                else:
                    log.info("Shutdown signaled, not reconnecting.")


    async def _send_event(self, event):
        """Send an event to the WebSocket server (internal)."""
        if self.ws and self._connected.is_set():
            try:
                await self.ws.send(json.dumps(event))
                # Avoid logging every audio chunk append
                if event.get("type") != "input_audio_buffer.append":
                     log.debug(f"Event sent - type: {event.get('type', 'N/A')}")
            except websockets.exceptions.ConnectionClosed:
                log.warning("Cannot send event, WebSocket connection closed.")
                self._connected.clear() # Mark as disconnected
            except Exception as e:
                log.exception(f"Error sending WebSocket event: {e}")
        else:
            log.warning("Cannot send event, WebSocket not connected.")

    async def _receive_events(self):
        """Continuously receive events from the WebSocket server."""
        if not self.ws: return
        try:
            async for message in self.ws:
                try:
                    event = json.loads(message)
                    await self._handle_event(event)
                except json.JSONDecodeError:
                    log.error(f"Failed to decode JSON message: {message}")
                except Exception as e:
                    log.exception("Error handling received event:")
        except websockets.exceptions.ConnectionClosedOK:
            log.info("Receive loop ended: WebSocket connection closed normally.")
        except websockets.exceptions.ConnectionClosedError as e:
            log.error(f"Receive loop ended: WebSocket connection closed with error: {e.code} {e.reason}")
            self._connected.clear() # Ensure disconnected state
        except asyncio.CancelledError:
             log.info("Receive loop cancelled.")
        except Exception as e:
            log.exception("Unexpected error in receive loop:")
            self._connected.clear() # Ensure disconnected state

    async def _handle_event(self, event):
        """Handle incoming events from the WebSocket server."""
        event_type = event.get("type")
        # log.debug(f"Received event type: {event_type}") # Too verbose for audio delta

        if event_type == "error":
            error_msg = event.get("error", {}).get("message", "Unknown error")
            log.error(f"Error event received: {error_msg}")
            self.on_status_update(f"Error: {error_msg}")
        elif event_type == "response.text.delta":
            delta = event.get("delta", "")
            # print(delta, end="", flush=True) # Debugging: print to console
            self.on_text_delta(delta)
        elif event_type == "response.audio.delta":
            audio_b64 = event.get("delta")
            if audio_b64:
                try:
                    audio_data = base64.b64decode(audio_b64)
                    self.on_audio_chunk(audio_data)
                except Exception as e:
                    log.error(f"Error decoding audio delta: {e}")
        elif event_type == "response.audio.done":
            log.info("Audio response complete.")
            self.on_response_done() # Signal completion
            # Optionally, set status back to Ready if not recording
            if not self._recording.is_set():
                self.on_status_update("Ready.")
        elif event_type == "response.done":
            log.debug("Response generation completed (text/other).")
            # This might come after audio.done, handle idempotently
            self.on_response_done()
            if not self._recording.is_set():
                 self.on_status_update("Ready.") # Set status here too
        elif event_type == "conversation.item.created":
            # log.debug(f"Conversation item created: {event.get('item')}")
            pass # Informational
        elif event_type == "input_audio_buffer.speech_started":
            log.debug("Server VAD: Speech started") # Only if server VAD enabled
        elif event_type == "input_audio_buffer.speech_stopped":
            log.debug("Server VAD: Speech stopped") # Only if server VAD enabled
        # else:
        #     log.debug(f"Unhandled event type: {event_type} | Content: {event}")


    async def _async_start_talking(self):
        """Async logic to start recording and the sender task."""
        if self._recording.is_set(): return # Already recording

        log.info("Async: Starting audio recording...")
        if not self.audio_handler.start_recording():
             log.error("Async: Failed to start audio hardware.")
             self.on_status_update("Error: Mic unavailable.")
             return

        self._recording.set()

        # Start the task that sends audio chunks
        self._audio_sender_task = asyncio.create_task(self._audio_sender_worker())
        log.info("Async: Audio sender worker started.")

    async def _async_stop_talking(self, force_stop=False):
        """Async logic to stop recording, sender task, and commit buffer."""
        if not self._recording.is_set() and not force_stop:
             log.warning("Async: Stop requested but not recording.")
             return

        log.info("Async: Stopping audio recording...")
        self._recording.clear() # Signal sender task to stop

        # Wait for the sender task to finish
        if self._audio_sender_task and not self._audio_sender_task.done():
            try:
                log.debug("Async: Waiting for audio sender worker to finish...")
                await asyncio.wait_for(self._audio_sender_task, timeout=1.0)
                log.debug("Async: Audio sender worker finished.")
            except asyncio.TimeoutError:
                log.warning("Async: Timeout waiting for audio sender worker. Cancelling.")
                self._audio_sender_task.cancel()
            except asyncio.CancelledError:
                 log.info("Async: Audio sender worker was cancelled.")
            except Exception as e:
                 log.exception("Async: Error waiting for audio sender task.")
        self._audio_sender_task = None

        # Stop the audio hardware
        self.audio_handler.stop_recording()
        log.debug("Async: Audio hardware stopped.")

        if not force_stop:
            # Commit the audio buffer ONLY if not a forced stop (e.g., due to disconnect)
            await self._send_event({"type": "input_audio_buffer.commit"})
            log.info("Async: Audio buffer committed.")

            # Send response.create to get AI response
            await self._send_event({"type": "response.create"})
            log.info("Async: response.create sent after audio commit.")
        else:
             log.warning("Async: Forced stop, buffer not committed.")
             # Ensure status reflects readiness after forced stop
             self.on_status_update("Ready.")


    async def _audio_sender_worker(self):
        """Async task to continuously read audio chunks and send them."""
        log.debug("Audio sender worker running...")
        while self._recording.is_set():
            chunk = self.audio_handler.read_chunk()
            if chunk:
                try:
                    base64_chunk = base64.b64encode(chunk).decode('utf-8')
                    await self._send_event({
                        "type": "input_audio_buffer.append",
                        "audio": base64_chunk
                    })
                except Exception as e:
                    log.error(f"Error encoding/sending audio chunk: {e}")
                    # Should we stop recording on send error? Maybe.
                    # self._recording.clear() # Example: stop on error
                    # break
            else:
                # No chunk available, sleep briefly to avoid busy-waiting
                await asyncio.sleep(0.005) # Small sleep
            # Yield control briefly even if chunk was sent
            await asyncio.sleep(0.001)
        log.debug("Audio sender worker finished.")


    async def _async_send_text(self, text):
        """Async logic to send text message."""
        log.info(f"Async: Sending text: '{text[:50]}...'")
        event = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": text
                }]
            }
        }
        await self._send_event(event)
        # Important: Send response.create *after* sending the text item
        await self._send_event({"type": "response.create"})
        log.info("Async: Text sent and response.create requested.")
        self.on_status_update("Processing...") # Update status after sending

    async def _async_cleanup(self):
        """Async cleanup tasks to be run in the loop before stopping."""
        log.info("Async: Cleaning up RealtimeClient...")
        if self._recording.is_set():
             await self._async_stop_talking(force_stop=True) # Force stop recording if active

        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
        if self._audio_sender_task and not self._audio_sender_task.done():
            self._audio_sender_task.cancel()

        if self.ws:
            try:
                await self.ws.close()
                log.info("Async: WebSocket closed.")
            except Exception as e:
                log.error(f"Async: Error closing WebSocket: {e}")
            finally:
                self.ws = None
        self.audio_handler.cleanup()
        log.info("Async: Cleanup complete.")

# --- Helper to save audio data ---
def save_temp_wav(audio_data):
    """Saves raw PCM16 audio data to a temporary WAV file."""
    if not audio_data:
        return None
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filepath = os.path.join(TEMP_AUDIO_DIR, f"realtime_{timestamp}.wav")

        with wave.open(filepath, 'wb') as wf:
            wf.setnchannels(AUDIO_CHANNELS)
            wf.setsampwidth(AUDIO_WIDTH)
            wf.setframerate(AUDIO_RATE)
            wf.writeframes(audio_data)
        log.info(f"Saved temporary realtime audio to: {filepath}")
        return filepath
    except Exception as e:
        log.exception(f"Error saving temporary WAV file: {e}")
        return None

# --- Placeholder for INSTRUCTIONS (can be loaded from elsewhere if needed) ---
# INSTRUCTIONS = "You are a helpful assistant." # Example instruction
INSTRUCTIONS = f"""
You are a professional radio broadcaster. Provide a natural, broadcast-style answer.
Answer in spanish from Spain. Use european format for all dates and units.
Your very first message must be only the words: Realtime mode started.
Answer in short and concise sentences. Keep responses brief.
"""