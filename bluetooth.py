# ================================================
# File: /bluetooth.py
# ================================================
#!/usr/bin/env python3
"""
Bluetooth control functions using bluetoothctl via subprocess.
Corrected command execution method.
"""

import subprocess
import logging
import time

log = logging.getLogger("bluetooth")

def _run_bt_command(command: str, timeout: int = 10) -> tuple[bool, str]:
    """Runs a command within bluetoothctl using stdin piping."""
    input_commands = f"{command}\nexit\n"
    log.info("Running bluetoothctl command: %s", command)

    try:
        process = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        stdout, stderr = process.communicate(input=input_commands, timeout=timeout)

        if process.returncode != 0:
            log.error("bluetoothctl command '%s' failed with code %d", command, process.returncode)
            error_output = stderr.strip() if stderr else stdout.strip()
            log.error("Output: %s", error_output)
            if "Waiting to connect" in error_output or "bluetoothd" in error_output:
                 return False, "Command failed: Cannot connect to bluetoothd. Is the Bluetooth service running?"
            return False, f"Command failed: {error_output or 'Unknown error'}"

        log.info("bluetoothctl command '%s' successful.", command)
        log.debug("Stdout: %s", stdout.strip())
        return True, stdout.strip()

    except subprocess.TimeoutExpired:
        log.error("bluetoothctl command '%s' timed out after %d seconds.", command, timeout)
        try:
            process.kill()
            stdout, stderr = process.communicate()
        except Exception: # Ignore errors during cleanup after timeout
            pass
        return False, f"Command timed out."
    except FileNotFoundError:
        log.error("bluetoothctl command not found. Is bluez installed and in PATH?")
        return False, "bluetoothctl not found. Install bluez package."
    except Exception as e:
        log.exception("Error running bluetoothctl command '%s'", command)
        return False, f"Exception: {e}"

def set_discoverable(enable: bool, duration: int = 180) -> tuple[bool, str]:
    """Enable or disable Bluetooth discoverability."""
    state = "on" if enable else "off"
    if enable:
        success_timeout, msg_timeout = _run_bt_command(f"discoverable-timeout {duration}")
        if not success_timeout:
            log.warning("Failed to set discoverable timeout (%s). Proceeding to set discoverable state.", msg_timeout)

    success, msg = _run_bt_command(f"discoverable {state}")
    final_message = f"Discoverable set to {state}."
    if not success:
        final_message += f" Error: {msg}"
    return success, final_message

def set_pairable(enable: bool) -> tuple[bool, str]:
    """Enable or disable Bluetooth pairing."""
    state = "on" if enable else "off"
    success, msg = _run_bt_command(f"pairable {state}")
    final_message = f"Pairable set to {state}."
    if not success:
        final_message += f" Error: {msg}"
    return success, final_message

def get_bluetooth_status() -> dict:
    """Get basic Bluetooth status (powered, discoverable, pairable) using 'show'."""
    status = {
        "powered": None,
        "discoverable": None,
        "pairable": None,
        "error": None,
        "controller_info": None # Keep for debugging if needed
    }
    try:
        log.debug("Attempting to run 'show' command...")
        success_show, output_show = _run_bt_command("show", timeout=5)
        status["controller_info"] = output_show # Store output regardless of success

        if not success_show:
            # If the command failed, use the error message from _run_bt_command
            status["error"] = f"Failed to get controller info: {output_show}"
            log.warning("get_bluetooth_status: _run_bt_command failed.")
            # Return immediately as parsing is not possible
            return status

        # --- Start of parsing ---
        log.debug("Parsing 'show' command output...")
        try:
            lines = output_show.splitlines()
            found_powered = False
            found_discoverable = False
            found_pairable = False
            for line in lines:
                line = line.strip()
                if line.startswith("Powered:"):
                    status["powered"] = "yes" in line.lower()
                    found_powered = True
                elif line.startswith("Discoverable:"):
                    status["discoverable"] = "yes" in line.lower()
                    found_discoverable = True
                elif line.startswith("Pairable:"):
                    status["pairable"] = "yes" in line.lower()
                    found_pairable = True

            # Check if all expected fields were found during parsing
            if not (found_powered and found_discoverable and found_pairable):
                log.warning("Could not parse all status fields from 'show' output. Output was:\n%s", output_show)
                # Don't set status['error'] here, as the command succeeded.
                # The frontend will see None for missing fields.

            status["error"] = None # Explicitly set error to None if command succeeded and parsing finished
            log.debug("Successfully parsed 'show' output.")

        except Exception as parse_exc:
            # Catch errors specifically during parsing
            log.exception("Error parsing bluetooth 'show' output.")
            status["error"] = f"Error parsing status: {parse_exc}"
            # Reset potentially partially parsed fields
            status["powered"] = None
            status["discoverable"] = None
            status["pairable"] = None
        # --- End of parsing ---

    except Exception as e:
        # Catch errors occurring before or after _run_bt_command/parsing
        log.exception("Unexpected error in get_bluetooth_status")
        status["error"] = f"Exception fetching status: {e}"

    log.info("Bluetooth status fetched: %s", {k: v for k, v in status.items() if k != 'controller_info'})
    return status

# Example usage remains the same
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s")
    log.info("Testing Bluetooth functions...")
    # ... (rest of the standalone test) ...
    print("\nGetting initial status:")
    print(get_bluetooth_status())
    time.sleep(1)

    print("\nEnabling discoverable and pairable...")
    success_d, msg_d = set_discoverable(True)
    print(f"Set Discoverable: {success_d}, Message: {msg_d}")
    time.sleep(1)
    success_p, msg_p = set_pairable(True)
    print(f"Set Pairable: {success_p}, Message: {msg_p}")
    time.sleep(1)

    print("\nGetting status after enabling:")
    print(get_bluetooth_status())
    print("\nDevice should be discoverable/pairable...")
    print("Sleeping for 10 seconds...")
    time.sleep(10)

    print("\nDisabling discoverable and pairable...")
    success_d, msg_d = set_discoverable(False)
    print(f"Set Discoverable: {success_d}, Message: {msg_d}")
    time.sleep(1)
    success_p, msg_p = set_pairable(False)
    print(f"Set Pairable: {success_p}, Message: {msg_p}")
    time.sleep(1)

    print("\nGetting final status:")
    print(get_bluetooth_status())