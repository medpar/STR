# ================================================
# File: /gpio_controller.py
# ================================================
"""
Background monitor for a physical push‑button and an LED on a Raspberry Pi.

• Short press toggles recording in realtime_client.
• LED lit while recording.
• **MODIFIED:** Assumes button uses a pull-down resistor (ACTIVE_HIGH=True).

Requires:  sudo apt install python3-rpi.gpio
Will silently self‑disable on non‑Pi machines or if disabled in config.
"""

from __future__ import annotations
import threading
import time
import logging
import sys

import config # Import config directly

log = logging.getLogger("gpio")

try:
    # Ensure GPIO library is available and enabled
    if config.ENABLE_GPIO:
        import RPi.GPIO as GPIO  # type: ignore
        log.info("RPi.GPIO library loaded successfully.")
    else:
        GPIO = None
        log.info("GPIO disabled by configuration (ENABLE_GPIO=False).")
except (ImportError, RuntimeError) as e:
    GPIO = None
    # Log specific error if import failed but GPIO was expected
    if config.ENABLE_GPIO:
        log.warning(f"RPi.GPIO import failed: {e} - GPIO support disabled.")
    else:
         # This case should not happen if config.ENABLE_GPIO is False, but log just in case
         log.info("RPi.GPIO not available (or not an RPi) - GPIO support disabled.")


class GPIOController:
    def __init__(self, start_cb, stop_cb):
        self.start_cb = start_cb
        self.stop_cb = stop_cb
        self.recording = False
        self._thread = None
        self._stop_event = threading.Event()

        # Check availability again, considering both library import and config flag
        if not (GPIO and config.ENABLE_GPIO):
            log.warning("GPIOController inactive (GPIO library not available or disabled in config).")
            self.available = False
            return

        self.available = True
        self.button_pin = config.GPIO_BUTTON_PIN
        self.led_pin = config.GPIO_LED_PIN
        # Determine pull resistor based on ACTIVE_HIGH setting
        self.active_state = GPIO.HIGH if config.BUTTON_ACTIVE_HIGH else GPIO.LOW
        self.inactive_state = GPIO.LOW if config.BUTTON_ACTIVE_HIGH else GPIO.HIGH
        self.pull_resistor = GPIO.PUD_DOWN if config.BUTTON_ACTIVE_HIGH else GPIO.PUD_UP

        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False) # Suppress warnings about channel usage
            GPIO.setup(self.button_pin, GPIO.IN, pull_up_down=self.pull_resistor)
            GPIO.setup(self.led_pin, GPIO.OUT)
            GPIO.output(self.led_pin, GPIO.LOW) # Ensure LED is off initially
            log.info(f"GPIO pins configured: Button={self.button_pin} (Active State: {self.active_state}, Pull: {self.pull_resistor}), LED={self.led_pin}")
        except Exception as e:
            log.exception(f"Error setting up GPIO pins: {e}")
            self.available = False
            GPIO.cleanup() # Attempt cleanup if setup failed
            return

        # Start monitoring thread only if setup was successful
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("GPIO controller thread started.")

    def _loop(self):
        """Monitors the button state and triggers callbacks."""
        log.debug(f"GPIO loop started. Monitoring pin {self.button_pin} for state {self.active_state}.")
        last_state = self.inactive_state
        debounce_time = 0.05  # 50ms debounce time
        last_press_time = 0

        while not self._stop_event.is_set():
            try:
                current_state = GPIO.input(self.button_pin)

                if current_state == self.active_state and last_state == self.inactive_state:
                    # Potential press detected (transition to active)
                    if time.time() - last_press_time > debounce_time:
                         log.debug("Button press detected (debounced).")
                         self._toggle()
                         last_press_time = time.time()
                         # Wait for release (or timeout) to prevent multiple triggers
                         release_start_time = time.time()
                         while GPIO.input(self.button_pin) == self.active_state and not self._stop_event.is_set():
                             if time.time() - release_start_time > 2.0: # 2 second timeout if stuck
                                 log.warning("Button seems stuck in pressed state.")
                                 break
                             time.sleep(0.02)
                         log.debug("Button released (or timeout).")
                         last_state = self.inactive_state # Ensure state reset after release check
                         continue # Skip rest of loop iteration

                last_state = current_state
                time.sleep(0.02) # Check interval

            except RuntimeError as e:
                 # Handle potential errors if GPIO access is lost mid-run
                 if "Not initialized" in str(e) or "missing edge detection" in str(e):
                     log.error(f"GPIO runtime error in loop (may need restart): {e}")
                     break # Exit loop on critical GPIO error
                 else:
                     log.exception(f"Unexpected runtime error in GPIO loop: {e}")
                     time.sleep(1) # Wait a bit before retrying
            except Exception as e:
                log.exception(f"Unexpected error in GPIO loop: {e}")
                time.sleep(1) # Wait before retrying

        log.info("GPIO monitoring loop finished.")
        # Ensure LED is off when loop exits
        try:
             if self.available and GPIO:
                  GPIO.output(self.led_pin, GPIO.LOW)
        except Exception as e:
             log.warning(f"Could not turn off LED during GPIO loop exit: {e}")


    def _toggle(self):
        """Toggles the recording state and LED."""
        if not self.recording:
            log.info("GPIO Toggle: Requesting START recording.")
            if self.start_cb:
                try:
                    self.start_cb() # Call the start callback (e.g., realtime_client.start_talking)
                    GPIO.output(self.led_pin, GPIO.HIGH)
                    self.recording = True
                    log.debug("GPIO Toggle: Start successful, LED ON.")
                except Exception as e:
                    log.error(f"GPIO Toggle: Error calling start_cb: {e}")
                    # Ensure LED is off if start failed
                    GPIO.output(self.led_pin, GPIO.LOW)
                    self.recording = False
            else:
                 log.warning("GPIO Toggle: start_cb not defined.")

        else:
            log.info("GPIO Toggle: Requesting STOP recording.")
            if self.stop_cb:
                try:
                    self.stop_cb() # Call the stop callback (e.g., realtime_client.stop_talking)
                    # State change happens after callback completes
                    GPIO.output(self.led_pin, GPIO.LOW)
                    self.recording = False
                    log.debug("GPIO Toggle: Stop successful, LED OFF.")
                except Exception as e:
                    log.error(f"GPIO Toggle: Error calling stop_cb: {e}")
                    # Keep LED on if stop failed? Or turn off? Let's turn off.
                    GPIO.output(self.led_pin, GPIO.LOW)
                    self.recording = False # Assume stopped even if callback failed? Maybe safer.
            else:
                log.warning("GPIO Toggle: stop_cb not defined.")


    def cleanup(self):
        """Stops the monitoring thread and cleans up GPIO resources."""
        log.info("Cleaning up GPIOController...")
        self._stop_event.set() # Signal the loop thread to stop
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0) # Wait for thread to exit
            if self._thread.is_alive():
                 log.warning("GPIOController thread did not stop cleanly.")

        if self.available and GPIO:
            try:
                GPIO.cleanup([self.button_pin, self.led_pin])
                log.info("GPIO pins cleaned up.")
            except Exception as e:
                log.error(f"Error during GPIO cleanup: {e}")
        self.available = False