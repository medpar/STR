"""
Background monitor for a physical push‑button and an LED on a Raspberry Pi.

• Short press toggles recording in realtime_client.
• LED lit while recording.

Requires:  sudo apt install python3-rpi.gpio
Will silently self‑disable on non‑Pi machines.
"""

from __future__ import annotations
import threading
import time
import logging
import sys

import config

log = logging.getLogger("gpio")

try:
    import RPi.GPIO as GPIO  # type: ignore
except (ImportError, RuntimeError):
    GPIO = None
    log.warning("RPi.GPIO not available – GPIO support disabled")


class GPIOController:
    def __init__(self, start_cb, stop_cb):
        self.start_cb = start_cb
        self.stop_cb = stop_cb
        self.recording = False
        if not (GPIO and config.ENABLE_GPIO):
            log.info("GPIO disabled")
            self.available = False
            return
        self.available = True
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(config.BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(config.LED_PIN, GPIO.OUT)
        GPIO.output(config.LED_PIN, GPIO.LOW)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("GPIO controller running (button=%d  led=%d)",
                 config.BUTTON_PIN, config.LED_PIN)

    # ------------------------------------------------------ #
    def _loop(self):
        last_state = GPIO.input(config.BUTTON_PIN)
        debounce = 0
        while True:
            cur = GPIO.input(config.BUTTON_PIN)
            if cur != last_state:
                debounce = time.time()
                last_state = cur
            # detect stable falling edge (>40 ms)
            if (last_state == GPIO.LOW
                    and time.time() - debounce > 0.04):     # button pressed
                self._toggle()
                while GPIO.input(config.BUTTON_PIN) == GPIO.LOW:
                    time.sleep(0.05)  # wait for release
            time.sleep(0.02)

    def _toggle(self):
        if not self.recording:
            self.start_cb()
            GPIO.output(config.LED_PIN, GPIO.HIGH)
            self.recording = True
        else:
            self.stop_cb()
            GPIO.output(config.LED_PIN, GPIO.LOW)
            self.recording = False

    # ------------------------------------------------------ #
    def cleanup(self):
        if self.available:
            GPIO.cleanup()
