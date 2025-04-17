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
    # Prepare the command sequence to be sent to bluetoothctl's stdin
    # Ensure newline termination for each command and exit
    input_commands = f"{command}\nexit\n"
    log.info("Running bluetoothctl command: %s", command)

    try:
        # Start bluetoothctl as a subprocess
        process = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,  # Work with text streams
            bufsize=1, # Line buffered
            universal_newlines=True # Ensure text mode works across platforms
        )

        # Send the commands to bluetoothctl's stdin and close stdin
        stdout, stderr = process.communicate(input=input_commands, timeout=timeout)

        # Check the return code
        if process.returncode != 0:
            log.error("bluetoothctl command '%s' failed with code %d", command, process.returncode)
            # Combine stderr and stdout for error message, prioritize stderr
            error_output = stderr.strip() if stderr else stdout.strip()
            log.error("Output: %s", error_output)
            # Check for common issues
            if "Waiting to connect" in error_output or "bluetoothd" in error_output:
                 return False, "Command failed: Cannot connect to bluetoothd. Is the Bluetooth service running?"
            return False, f"Command failed: {error_output or 'Unknown error'}"

        log.info("bluetoothctl command '%s' successful.", command)
        log.debug("Stdout: %s", stdout.strip())
        return True, stdout.strip() # Return the stripped stdout

    except subprocess.TimeoutExpired:
        log.error("bluetoothctl command '%s' timed out after %d seconds.", command, timeout)
        process.kill() # Ensure the process is terminated
        stdout, stderr = process.communicate() # Capture any remaining output
        return False, f"Command timed out. Output: {stderr or stdout}"
    except FileNotFoundError:
        log.error("bluetoothctl command not found. Is bluez installed and in PATH?")
        return False, "bluetoothctl not found. Install bluez package."
    except Exception as e:
        log.exception("Error running bluetoothctl command '%s'", command)
        return False, f"Exception: {e}"

def set_discoverable(enable: bool, duration: int = 180) -> tuple[bool, str]:
    """Enable or disable Bluetooth discoverability."""
    state = "on" if enable else "off"
    # Set duration if enabling - This needs to happen *before* turning discoverable on
    # within the same bluetoothctl session, or handled differently.
    # For simplicity, we'll just set discoverable state here.
    # Advanced usage might require chaining commands within one _run_bt_command call.
    # Note: Setting discoverable-timeout might not persist reliably this way.
    # Consider managing timeout via systemd/dbus if persistence is needed.

    # We will try setting timeout first, but its success isn't critical for basic discoverability
    if enable:
        success_timeout, msg_timeout = _run_bt_command(f"discoverable-timeout {duration}")
        if not success_timeout:
            log.warning("Failed to set discoverable timeout (%s). Proceeding to set discoverable state.", msg_timeout)
            # Don't return False here, just warn.

    # Now set discoverable state
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
        "controller_info": None
    }
    try:
        success_show, output_show = _run_bt_command("show", timeout=5)

        if success_show:
            status["controller_info"] = output_show # Store full output for debugging
            # Parse the output lines for status
            lines = output_show.splitlines()
            for line in lines:
                line = line.strip()
                if line.startswith("Powered:"):
                    status["powered"] = "yes" in line.lower()
                elif line.startswith("Discoverable:"):
                    status["discoverable"] = "yes" in line.lower()
                elif line.startswith("Pairable:"):
                    status["pairable"] = "yes" in line.lower()
            # Check if any status key is still None, indicating parsing might have failed
            if status["powered"] is None or status["discoverable"] is None or status["pairable"] is None:
                 log.warning("Could not parse all status fields from 'show' output.")
                 # Keep error as None unless a specific error occurred during run
                 status["error"] = status.get("error") # Preserve potential connection error
            else:
                 status["error"] = None # Explicitly set error to None if parsing seems ok

        else:
            # The error message from _run_bt_command is already logged
            status["error"] = f"Failed to get controller info: {output_show}"
            status["controller_info"] = output_show # Store error output

    except Exception as e:
        log.exception("Error getting Bluetooth status")
        status["error"] = f"Exception fetching status: {e}"

    log.info("Bluetooth status fetched: %s", {k: v for k, v in status.items() if k != 'controller_info'}) # Log concise status
    return status

# Example usage (can be run standalone for testing)
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s")
    log.info("Testing Bluetooth functions...")

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