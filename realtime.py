import os
import logging
import asyncio
import websockets
import json
import base64
import threading
import time
from dotenv import load_dotenv
from audio_manager import play_audio
from tempfile import NamedTemporaryFile
import wave

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

INSTRUCTIONS = """
You are a professional radio broadcaster. Provide a natural, broadcast-style answer. Answer in spanish from Spain. Use european format for all dates and units."""

class RealtimeClient:
    def __init__(self, instructions, voice="alloy"):
        self.url = "wss://api.openai.com/v1/realtime"
        self.model = "gpt-4o-realtime-preview-2024-10-01"
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            logger.error("OPENAI_API_KEY not found in .env")
            raise ValueError("OPENAI_API_KEY not found")
        self.ws = None
        self.audio_buffer = b''
        self.instructions = instructions
        self.voice = voice
        self.session_config = {
            "modalities": ["audio", "text"],
            "instructions": self.instructions,
            "voice": self.voice,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "turn_detection": None,
            "input_audio_transcription": {"model": "whisper-1"},
            "temperature": 0.6
        }
        # SSL context
        import ssl
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE
        # Asyncio loop
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.run_loop, daemon=True)
        self.thread.start()
        # Connect to WebSocket
        asyncio.run_coroutine_threadsafe(self.connect(), self.loop).result()
        # Start receive_events
        self.receive_task = asyncio.run_coroutine_threadsafe(self.receive_events(), self.loop)

    def run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def connect(self):
        logger.info(f"Connecting to WebSocket: {self.url}")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "realtime=v1"
        }
        self.ws = await websockets.connect(
            f"{self.url}?model={self.model}",
            extra_headers=headers,
            ssl=self.ssl_context
        )
        logger.info("Connected to OpenAI Realtime API")
        # Configure session
        await self.send_event({
            "type": "session.update",
            "session": self.session_config
        })
        logger.info("Session configured")

    async def send_event(self, event):
        await self.ws.send(json.dumps(event))
        logger.debug(f"Sent event: {event['type']}")

    async def receive_events(self):
        try:
            async for message in self.ws:
                event = json.loads(message)
                await self.handle_event(event)
        except websockets.ConnectionClosed as e:
            logger.error(f"WebSocket connection closed: {e}")
        except Exception as e:
            logger.error(f"Error in receive_events: {e}")

    async def handle_event(self, event):
        event_type = event.get("type")
        logger.debug(f"Received event: {event_type}")
        if event_type == "error":
            logger.error(f"Error: {event['error']['message']}")
        elif event_type == "response.text.delta":
            print(event["delta"], end="", flush=True)
        elif event_type == "response.audio.delta":
            audio_data = base64.b64decode(event["delta"])
            self.audio_buffer += audio_data
        elif event_type == "response.audio.done":
            if self.audio_buffer:
                with NamedTemporaryFile(delete=False, suffix=".wav") as temp_wav:
                    with wave.open(temp_wav.name, 'wb') as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)  # 16-bit
                        wf.setframerate(24000)
                        wf.writeframes(self.audio_buffer)
                    play_audio(temp_wav.name)
                self.audio_buffer = b''
        elif event_type == "response.done":
            logger.debug("Response done")
        else:
            logger.debug(f"Unhandled event: {event_type}")

    def send_audio_chunk(self, audio_data):
        base64_audio = base64.b64encode(audio_data).decode('utf-8')
        event = {
            "type": "input_audio_buffer.append",
            "audio": base64_audio
        }
        asyncio.run_coroutine_threadsafe(self.send_event(event), self.loop)

    def commit_and_respond(self):
        asyncio.run_coroutine_threadsafe(self.send_event({"type": "input_audio_buffer.commit"}), self.loop)
        asyncio.run_coroutine_threadsafe(self.send_event({"type": "response.create"}), self.loop)