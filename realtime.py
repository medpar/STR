# ================================================
# File: /realtime.py
# ================================================
#!/usr/bin/env python3
"""
Realtime transcription and response using OpenAI Realtime API.
Based on the provided OpenAI example, integrated into the Flask app structure.
Handles audio recording, WebSocket communication, and triggers callbacks for text/audio.
**MODIFIED:** Records mic at NATIVE_MIC_RATE and resamples down to API_AUDIO_RATE.
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
import numpy as np # Needed for resampling
import resampy # Needed for resampling

from config import (
    OPENAI_MODEL_REALTIME,
    MIC_DEVICE_INDEX,
    MIC_CHUNK,
    BUTTON_ACTIVE_HIGH
)
from audio_manager import play_audio, terminate_pyaudio_instance as terminate_audio_manager_pyaudio

# Load API Key
from dotenv import load_dotenv
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

log = logging.getLogger(__name__) # Use logger instance

# --- Constants ---
REALTIME_API_URL = "wss://api.openai.com/v1/realtime"
NATIVE_MIC_RATE = 48000 # <<< Record microphone at this rate (as requested)
API_AUDIO_RATE = 24000  # <<< Resample to this rate for OpenAI API
AUDIO_CHANNELS = 1
AUDIO_FORMAT = pyaudio.paInt16 # 16-bit PCM
AUDIO_FORMAT_STR = "pcm16" # For session config
# Calculate audio width based on format
try:
    _pa_temp = pyaudio.PyAudio()
    AUDIO_WIDTH = _pa_temp.get_sample_size(AUDIO_FORMAT)
    _pa_temp.terminate()
except Exception as e:
    log.warning(f"Could not get sample size from PyAudio, defaulting to 2: {e}")
    AUDIO_WIDTH = 2 # Default for paInt16

TEMP_AUDIO_DIR = os.path.join(os.path.dirname(__file__), "audio_files", "temp_realtime")
os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)

class AudioHandler:
    """
    Handles audio input using PyAudio specifically for the RealtimeClient.
    Records at the NATIVE_MIC_RATE (e.g., 48kHz).
    Does NOT handle playback (delegated to audio_manager).
    """
    def __init__(self, device_index=None):
        self.p = None
        self.stream = None
        self.device_index = device_index
        self.chunk_size = MIC_CHUNK
        self.format = AUDIO_FORMAT
        self.channels = AUDIO_CHANNELS
        self.native_rate = NATIVE_MIC_RATE # <<< Use native rate for opening stream
        self._is_recording = False
        self._lock = threading.Lock()
        log.info(f"AudioHandler initialized for device index {self.device_index} (Native Rate: {self.native_rate} Hz)")

    def _initialize_pyaudio(self):
        with self._lock:
            if self.p is None:
                log.debug("Initializing PyAudio instance for AudioHandler.")
                self.p = pyaudio.PyAudio()

    def start_recording(self):
        """Start the audio input stream at the native rate."""
        self._initialize_pyaudio()
        with self._lock:
            if self._is_recording:
                log.warning("Recording already active.")
                return True
            if self.stream is not None:
                log.warning("Stream exists but not marked as recording. Closing existing stream.")
                self._close_stream_safe()

            # <<< Use self.native_rate here
            log.info(f"Attempting to start audio stream (Device: {self.device_index}, Rate: {self.native_rate} Hz)")
            try:
                self.stream = self.p.open(
                    format=self.format,
                    channels=self.channels,
                    rate=self.native_rate, # <<< Use native rate
                    input=True,
                    frames_per_buffer=self.chunk_size,
                    input_device_index=self.device_index
                )
                self._is_recording = True
                log.info("Audio input stream started successfully.")
                return True
            except OSError as e:
                # Log specific PyAudio errors if possible
                log.exception(f"PyAudio OSError opening stream on device {self.device_index} at {self.native_rate}Hz: {e}")
                self.stream = None
                self._is_recording = False
                return False
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
        """Read a single chunk of audio (at native rate) if recording."""
        with self._lock:
            if not self._is_recording or not self.stream or not self.stream.is_active():
                return None
            try:
                data = self.stream.read(self.chunk_size, exception_on_overflow=False)
                return data
            except OSError as e:
                if "Input overflowed" in str(e):
                    log.warning("Audio input overflow detected.")
                # Check for other common ALSA/Input errors
                elif "Input/output error" in str(e) or "-9988" in str(e):
                    log.error(f"Audio input I/O error: {e}. Stopping recording.")
                    # Trigger stop cleanly from here if possible, or signal error state
                    self._is_recording = False # Mark as stopped
                    self._close_stream_safe()
                    # How to notify RealtimeClient? Needs better error handling.
                    return None
                else:
                    log.error(f"Error reading audio chunk: {e}")
                return None
            except Exception as e:
                log.exception(f"Unexpected error reading audio chunk: {e}")
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
    **MODIFIED:** Resamples audio from NATIVE_MIC_RATE to API_AUDIO_RATE before sending.
    """
    def __init__(self, instructions, voice, mic_index, on_text_delta, on_audio_chunk, on_response_done, on_status_update, on_user_transcription):
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
        self.on_user_transcription = on_user_transcription

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

        # <<< Session config uses API rate for input/output specification
        self.session_config = {
            "modalities": ["audio", "text"],
            "instructions": self.instructions,
            "voice": self.voice,
            "input_audio_format": AUDIO_FORMAT_STR,
            "output_audio_format": AUDIO_FORMAT_STR,
            #"input_audio_sampling_rate": API_AUDIO_RATE, # <<< Rate we SEND to API
            #"output_audio_sampling_rate": API_AUDIO_RATE,# <<< Rate we RECEIVE from API
            "turn_detection": None, # Manual turn detection
            "input_audio_transcription": {
                "model": "whisper-1" # Use default whisper
            },
            "temperature": 0.7 # Example temperature
        }

        if not self.api_key:
            log.error("OPENAI_API_KEY not found in environment variables.")
            self.on_status_update("Error: OpenAI API Key missing.")
            raise ValueError("OpenAI API Key is required.")

    # --- Public Methods (Thread-Safe) ---
    # start_background_loop, stop_background_loop, start_talking, stop_talking, send_text
    # ... (These methods remain unchanged) ...
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
            # Ensure the future is awaited or handled if run_coroutine_threadsafe returns one
            future = asyncio.run_coroutine_threadsafe(self._async_cleanup(), self.loop)
            try:
                future.result(timeout=5.0) # Wait for cleanup to finish with timeout
            except TimeoutError:
                log.warning("Timeout waiting for async cleanup task to finish.")
            except Exception as e:
                log.error(f"Error during async cleanup execution: {e}")


        # Wait for the thread to finish
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                log.warning("Background loop thread did not exit cleanly.")
            self._thread = None
        log.info("RealtimeClient background loop stopped.")


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
                # Close pending tasks before closing loop
                tasks = asyncio.all_tasks(self.loop)
                for task in tasks:
                    task.cancel()
                # Wait for tasks to cancel
                async def gather_cancelled():
                     await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    self.loop.run_until_complete(gather_cancelled())
                except Exception as ex_cancel:
                    log.error(f"Error cancelling tasks during loop close: {ex_cancel}")

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
                    open_timeout=10,
                    close_timeout=10,
                    ping_interval=20, # Add keepalive pings
                    ping_timeout=10
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
                log.error("Connection attempt or ping timed out.")
            except Exception as e:
                log.exception("Unexpected error during WebSocket connection/reception:")

            finally:
                self.ws = None
                self._connected.clear()
                if self._recording.is_set():
                    log.warning("Disconnected while recording was active. Forcing stop.")
                    # Use run_coroutine_threadsafe if _main_async_logic might finish
                    # while another thread calls stop_talking. But if it's only
                    # called internally, direct await is fine.
                    # Let's assume direct await is okay here.
                    await self._async_stop_talking(force_stop=True)
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
                if event.get("type") != "input_audio_buffer.append":
                     log.debug(f"Event sent - type: {event.get('type', 'N/A')}")
            except websockets.exceptions.ConnectionClosed:
                log.warning("Cannot send event, WebSocket connection closed.")
                self._connected.clear()
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
            self._connected.clear()
        except asyncio.CancelledError:
             log.info("Receive loop cancelled.")
        except Exception as e:
            log.exception("Unexpected error in receive loop:")
            self._connected.clear()

    async def _handle_event(self, event):
        """Handle incoming events from the WebSocket server."""
        event_type = event.get("type")

        if event_type == "error":
            error_msg = event.get("error", {}).get("message", "Unknown error")
            log.error(f"Error event received: {error_msg}")
            self.on_status_update(f"Error: {error_msg}")
        elif event_type == "response.text.delta":
            delta = event.get("delta", "")
            self.on_text_delta(delta)
        elif event_type == "response.audio.delta":
            # Received audio is at API_AUDIO_RATE (24kHz)
            audio_b64 = event.get("delta")
            if audio_b64:
                try:
                    audio_data = base64.b64decode(audio_b64)
                    self.on_audio_chunk(audio_data) # Pass 24kHz data to callback
                except Exception as e:
                    log.error(f"Error decoding audio delta: {e}")
        elif event_type == "response.audio.done":
            log.info("Audio response complete (at API rate).")
            self.on_response_done() # Signal completion
            if not self._recording.is_set():
                self.on_status_update("Ready.")
        elif event_type == "response.done":
            log.debug("Response generation completed (text/other).")
            self.on_response_done()
            if not self._recording.is_set():
                 self.on_status_update("Ready.")
        # ... (other event types remain the same) ...
        elif event_type == "conversation.item.created":
            item = event.get("item", {})
            if item.get("type") == "message" and item.get("role") == "user":
                content = item.get("content", [])
                if content and content[0].get("type") == "input_text": # Assuming transcription is input_text
                    transcribed_text = content[0].get("text")
                    if transcribed_text and self.on_user_transcription:
                        try:
                            self.on_user_transcription(transcribed_text)
                            log.info(f"User transcription received: {transcribed_text[:50]}...")
                        except Exception as e:
                            log.error(f"Error calling on_user_transcription callback: {e}")
            # Original pass for other item.created types or if conditions not met
            else: # Added else to keep the informational log for other item types
                log.debug(f"conversation.item.created: type={item.get('type')}, role={item.get('role')}")
                pass # Informational if not user message with transcription
        elif event_type == "input_audio_buffer.speech_started":
            log.debug("Server VAD: Speech started")
        elif event_type == "input_audio_buffer.speech_stopped":
            log.debug("Server VAD: Speech stopped")


    async def _async_start_talking(self):
        """Async logic to start recording and the sender task."""
        if self._recording.is_set(): return

        log.info("Async: Starting audio recording...")
        if not self.audio_handler.start_recording(): # Tries to start at NATIVE_MIC_RATE
             log.error("Async: Failed to start audio hardware.")
             self.on_status_update("Error: Mic unavailable.")
             return

        self._recording.set()
        # Start the task that reads, resamples, and sends audio chunks
        self._audio_sender_task = asyncio.create_task(self._audio_sender_worker())
        log.info("Async: Audio sender worker started.")

    async def _async_stop_talking(self, force_stop=False):
        """Async logic to stop recording, sender task, and commit buffer."""
        if not self._recording.is_set() and not force_stop:
             log.warning("Async: Stop requested but not recording.")
             return

        log.info("Async: Stopping audio recording...")
        self._recording.clear() # Signal sender task to stop

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

        self.audio_handler.stop_recording() # Stop hardware
        log.debug("Async: Audio hardware stopped.")

        if not force_stop and self._connected.is_set(): # Only commit if connected
            await self._send_event({"type": "input_audio_buffer.commit"})
            log.info("Async: Audio buffer committed.")
            await self._send_event({"type": "response.create"})
            log.info("Async: response.create sent after audio commit.")
        elif force_stop:
             log.warning("Async: Forced stop, buffer not committed.")
             self.on_status_update("Ready.") # Update status after forced stop
        elif not self._connected.is_set():
            log.warning("Async: Cannot commit buffer, WebSocket not connected.")
            self.on_status_update("Disconnected.") # Reflect disconnected state


    async def _audio_sender_worker(self):
        """Async task to continuously read, RESAMPLE, and send audio chunks."""
        log.debug("Audio sender worker running (Resampling {} -> {} Hz)...".format(NATIVE_MIC_RATE, API_AUDIO_RATE))
        resample_filter = 'kaiser_fast' # Or 'kaiser_best' for higher quality

        while self._recording.is_set():
            # 1. Read chunk at native rate
            chunk_native = self.audio_handler.read_chunk()

            if chunk_native:
                try:
                    # 2. Convert bytes to numpy array (int16)
                    samples_native = np.frombuffer(chunk_native, dtype=np.int16)

                    # 3. Resample using resampy
                    samples_api_rate = resampy.resample(
                        samples_native,
                        sr_orig=NATIVE_MIC_RATE,
                        sr_new=API_AUDIO_RATE,
                        filter=resample_filter,
                        axis=0 # Ensure correct axis for 1D array
                    )

                    # 4. Convert resampled numpy array (float) back to int16 bytes
                    chunk_api_rate = samples_api_rate.astype(np.int16).tobytes()

                    # 5. Encode the RESAMPLED chunk to base64
                    base64_chunk = base64.b64encode(chunk_api_rate).decode('utf-8')

                    # 6. Send the RESAMPLED chunk
                    await self._send_event({
                        "type": "input_audio_buffer.append",
                        "audio": base64_chunk
                    })
                except Exception as e:
                    log.exception(f"Error resampling/encoding/sending audio chunk: {e}")
                    # Consider stopping on error?
                    # self._recording.clear()
                    # break
            else:
                # No chunk available (or error reading), sleep briefly
                await asyncio.sleep(0.005)

            # Yield control briefly
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
        await self._send_event({"type": "response.create"})
        log.info("Async: Text sent and response.create requested.")
        self.on_status_update("Processing...")

    async def _async_cleanup(self):
        """Async cleanup tasks to be run in the loop before stopping."""
        log.info("Async: Cleaning up RealtimeClient...")
        if self._recording.is_set():
             await self._async_stop_talking(force_stop=True)

        # Cancel tasks safely
        tasks_to_cancel = [self._receive_task, self._audio_sender_task]
        for task in tasks_to_cancel:
            if task and not task.done():
                task.cancel()
                try:
                    await task # Allow task to handle cancellation
                except asyncio.CancelledError:
                    log.debug(f"Task {task.get_name()} cancelled successfully.")
                except Exception as e:
                    log.error(f"Error during task cancellation ({task.get_name()}): {e}")

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
    """Saves raw PCM16 audio data (expected at API_AUDIO_RATE) to a temporary WAV file."""
    if not audio_data:
        return None
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filepath = os.path.join(TEMP_AUDIO_DIR, f"realtime_{timestamp}.wav")

        with wave.open(filepath, 'wb') as wf:
            wf.setnchannels(AUDIO_CHANNELS)
            wf.setsampwidth(AUDIO_WIDTH)
            wf.setframerate(API_AUDIO_RATE) # <<< Save WAV at the rate we received (24kHz)
            wf.writeframes(audio_data)
        log.info(f"Saved temporary realtime audio (at {API_AUDIO_RATE} Hz) to: {filepath}")
        return filepath
    except Exception as e:
        log.exception(f"Error saving temporary WAV file: {e}")
        return None

# --- Placeholder for INSTRUCTIONS ---
INSTRUCTIONS = f"""
"Provide a natural, radio broadcast-style answer without any URLs, links, or references in your response. Always use web search to find the most recent information. Answer in castillian spanish. Use european format for all dates and units. Your response should always be in plain text, DO NOT use markdown. Answer very very briefly in maximum one paragraph. Your very first message must be only the word Started."
"""