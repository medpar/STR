# ================================================
# File: /bluetooth.py
# ================================================
#!/usr/bin/env python3
"""
Bluetooth control functions using bluetoothctl via subprocess.
"""

import subprocess
import logging
import time

log = logging.getLogger("bluetooth")

def _run_bt_command(command: str, timeout: int = 10) -> tuple[bool, str]:
    """Runs a command within bluetoothctl."""
    cmd_sequence = f"echo -e '{command}\\nexit' | bluetoothctl"
    log.info("Running bluetoothctl command: %s", command)
    try:
        process = subprocess.run(
            cmd_sequence,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False # Don't raise exception on non-zero exit code immediately
        )
        stdout = process.stdout.strip()
        stderr = process.stderr.strip()

        if process.returncode != 0:
            log.error("bluetoothctl command '%s' failed with code %d", command, process.returncode)
            log.error("Stderr: %s", stderr)
            return False, f"Command failed: {stderr or stdout or 'Unknown error'}"

        log.info("bluetoothctl command '%s' successful.", command)
        log.debug("Stdout: %s", stdout)
        return True, stdout

    except subprocess.TimeoutExpired:
        log.error("bluetoothctl command '%s' timed out after %d seconds.", command, timeout)
        return False, "Command timed out"
    except Exception as e:
        log.exception("Error running bluetoothctl command '%s'", command)
        return False, f"Exception: {e}"

def set_discoverable(enable: bool, duration: int = 180) -> tuple[bool, str]:
    """Enable or disable Bluetooth discoverability."""
    state = "on" if enable else "off"
    # Set duration if enabling
    if enable:
        success, msg = _run_bt_command(f"discoverable-timeout {duration}")
        if not success:
            # Don't stop if timeout fails, just warn
            log.warning("Failed to set discoverable timeout: %s", msg)
            # return False, f"Failed to set discoverable timeout: {msg}" # Or just warn and continue
    # Now set discoverable state
    success, msg = _run_bt_command(f"discoverable {state}")
    return success, f"Discoverable set to {state}. {msg}"

def set_pairable(enable: bool) -> tuple[bool, str]:
    """Enable or disable Bluetooth pairing."""
    state = "on" if enable else "off"
    success, msg = _run_bt_command(f"pairable {state}")
    return success, f"Pairable set to {state}. {msg}"

def get_bluetooth_status() -> dict:
    """Get basic Bluetooth status (powered, discoverable, pairable)."""
    # This is a bit more complex as bluetoothctl is interactive.
    # We can try fetching properties using specific commands.
    status = {
        "powered": None,
        "discoverable": None,
        "pairable": None,
        "error": None,
        "controller_info": None
    }
    try:
        # Check power status
        success_show, output_show = _run_bt_command("show", timeout=5)
        if success_show:
            status["controller_info"] = output_show # Store full output
            status["powered"] = "Powered: yes" in output_show
            status["discoverable"] = "Discoverable: yes" in output_show
            status["pairable"] = "Pairable: yes" in output_show
        else:
            status["error"] = f"Failed to get controller info: {output_show}"

    except Exception as e:
        log.exception("Error getting Bluetooth status")
        status["error"] = f"Exception fetching status: {e}"

    log.info("Bluetooth status fetched: %s", status)
    return status

# Example usage (can be run standalone for testing)
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    log.info("Testing Bluetooth functions...")

    print("\nGetting initial status:")
    print(get_bluetooth_status())

    print("\nEnabling discoverable and pairable...")
    set_discoverable(True)
    time.sleep(1)
    set_pairable(True)
    time.sleep(1)

    print("\nGetting status after enabling:")
    print(get_bluetooth_status())
    print("\nDevice should be discoverable/pairable for 3 minutes...")
    print("Sleeping for 10 seconds...")
    time.sleep(10)

    print("\nDisabling discoverable and pairable...")
    set_discoverable(False)
    time.sleep(1)
    set_pairable(False)
    time.sleep(1)

    print("\nGetting final status:")
    print(get_bluetooth_status())