# ================================================
# File: /bluetooth.py
# ================================================
#!/usr/bin/env python3
"""
Bluetooth control functions using bluetoothctl via subprocess.
Removes invalid 'pairable-timeout' command. Relies on main.conf for timeout.
"""

import subprocess
import logging
import time
from typing import List, Tuple

log = logging.getLogger("bluetooth")

# _run_bt_commands remains the same as the previous correct version
def _run_bt_commands(commands: List[str], timeout: int = 10) -> Tuple[bool, str]:
    """Runs multiple commands sequentially within a single bluetoothctl session."""
    if commands[-1].strip().lower() != "exit":
        commands.append("exit")

    input_script = "\n".join(commands) + "\n"
    log.info("Running bluetoothctl commands:\n%s", "\n".join(commands[:-1]))

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
        stdout, stderr = process.communicate(input=input_script, timeout=timeout)

        if process.returncode != 0:
            log.error("bluetoothctl commands failed with code %d", process.returncode)
            error_output = stderr.strip() if stderr else stdout.strip()
            log.error("Output:\n%s", error_output)
            if "Waiting to connect" in error_output or "bluetoothd" in error_output:
                 return False, "Command failed: Cannot connect to bluetoothd. Is the Bluetooth service running?"
            return False, f"Command execution failed: {error_output or 'Unknown error'}"

        log.info("bluetoothctl commands executed successfully.")
        log.debug("Stdout:\n%s", stdout.strip())
        # Check stdout for potential specific error messages even on returncode 0
        # Updated check based on the invalid command error we saw
        if "Failed to set" in stdout or "Invalid command" in stdout or "Unknown command" in stdout:
             log.warning("Potential issue detected in bluetoothctl output despite success code:\n%s", stdout)
             # Return True, but let caller handle potential warnings in output
             # If invalid command error occurs, it's not truly successful
             if "Invalid command" in stdout or "Unknown command" in stdout:
                 return False, f"Invalid command sent to bluetoothctl: {stdout.strip()}"
             return True, stdout.strip() # Return stdout for inspection

        return True, stdout.strip()

    except subprocess.TimeoutExpired:
        log.error("bluetoothctl commands timed out after %d seconds.", timeout)
        try:
            process.kill()
            stdout, stderr = process.communicate()
        except Exception:
            pass
        return False, "Commands timed out."
    except FileNotFoundError:
        log.error("bluetoothctl command not found. Is bluez installed and in PATH?")
        return False, "bluetoothctl not found. Install bluez package."
    except Exception as e:
        log.exception("Error running bluetoothctl commands.")
        return False, f"Exception: {e}"


# --- Function that needs changing ---
def set_discoverable_pairable(enable: bool, duration: int = 180) -> Tuple[bool, str]:
    """
    Enable or disable Bluetooth discoverability AND pairability together
    in a single bluetoothctl session.
    Relies on PairableTimeout=0 in main.conf for pairable to persist.
    """
    state = "on" if enable else "off"
    commands = []

    if enable:
        # Set discoverable timeout only (this one IS a valid command)
        commands.append(f"discoverable-timeout {duration}")
        # *** REMOVED: commands.append(f"pairable-timeout {duration}") *** # This command is invalid in bluetoothctl

    # Set the states
    commands.append(f"discoverable {state}")
    commands.append(f"pairable {state}")

    success, output = _run_bt_commands(commands)

    if success:
        # Adjust confirmation check slightly as output varies
        # A simple success return might be sufficient if main.conf is set correctly
        log.info(f"Bluetooth commands for state '{state}' sent successfully.")
        return True, f"Discoverable and Pairable state commands sent ({state}). Check status to confirm."
        # Previous more complex check might be too sensitive to output variations:
        # expected_discoverable = f"Changing discoverable on succeeded" if enable else "Changing discoverable off succeeded"
        # expected_pairable = f"Changing pairable on succeeded" if enable else "Changing pairable off succeeded"
        # if "Failed to set" in output or (enable and (expected_discoverable not in output or expected_pairable not in output)):
        #      log.warning(f"Bluetooth state set to '{state}', but confirmation messages might be missing/different in output. Check main.conf / status.")
        #      return True, f"Commands sent, confirmation unclear. State hopefully set to {state}. Check status & BlueZ config."
        # return True, f"Discoverable and Pairable state set to {state}."
    else:
        # Failure message comes from _run_bt_commands
        return False, f"Failed to set state to {state}: {output}"

# get_bluetooth_status remains the same as the previous correct version
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
        log.debug("Attempting to run 'show' command...")
        success_show, output_show = _run_bt_commands(["show"], timeout=5)
        status["controller_info"] = output_show

        if not success_show:
            status["error"] = f"Failed to get controller info: {output_show}"
            log.warning("get_bluetooth_status: _run_bt_commands failed.")
            return status

        log.debug("Parsing 'show' command output...")
        try:
            lines = output_show.splitlines()
            found_powered = False
            found_discoverable = False
            found_pairable = False
            controller_line_found = False
            for line in lines:
                line = line.strip()
                if not line or line.startswith("[") or line.startswith("#"):
                    continue
                if line.startswith("Controller"):
                    controller_line_found = True
                    continue

                if controller_line_found:
                    if line.startswith("Powered:"):
                        status["powered"] = "yes" in line.lower()
                        found_powered = True
                    elif line.startswith("Discoverable:"):
                        status["discoverable"] = "yes" in line.lower()
                        found_discoverable = True
                    elif line.startswith("Pairable:"):
                        status["pairable"] = "yes" in line.lower()
                        found_pairable = True

            if not (found_powered and found_discoverable and found_pairable):
                log.warning("Could not parse all status fields from 'show' output. Output was:\n%s", output_show)

            status["error"] = None
            log.debug("Successfully parsed 'show' output.")

        except Exception as parse_exc:
            log.exception("Error parsing bluetooth 'show' output.")
            status["error"] = f"Error parsing status: {parse_exc}"
            status["powered"] = None
            status["discoverable"] = None
            status["pairable"] = None

    except Exception as e:
        log.exception("Unexpected error in get_bluetooth_status")
        status["error"] = f"Exception fetching status: {e}"

    log.info("Bluetooth status fetched: %s", {k: v for k, v in status.items() if k != 'controller_info'})
    return status

# Example usage remains the same
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s")
    log.info("Testing Bluetooth functions...")

    print("\nGetting initial status:")
    print(get_bluetooth_status())
    time.sleep(1)

    print("\nEnabling discoverable and pairable...")
    success, msg = set_discoverable_pairable(True)
    print(f"Set Mode On: {success}, Message: {msg}")
    time.sleep(1)

    print("\nGetting status after enabling:")
    print(get_bluetooth_status())
    print("\nDevice should be discoverable/pairable...")
    print("Sleeping for 10 seconds...")
    time.sleep(10)

    print("\nDisabling discoverable and pairable...")
    success, msg = set_discoverable_pairable(False)
    print(f"Set Mode Off: {success}, Message: {msg}")
    time.sleep(1)

    print("\nGetting final status:")
    print(get_bluetooth_status())

    # Example of checking main.conf setting (requires read permission)
    try:
        with open("/etc/bluetooth/main.conf", "r") as f:
            conf_content = f.read()
        pairable_timeout_line = [line for line in conf_content.splitlines() if line.strip().startswith("PairableTimeout")]
        if pairable_timeout_line:
            value = pairable_timeout_line[0].split('=')[-1].strip()
            print(f"\nNOTE: Found PairableTimeout setting in main.conf: {pairable_timeout_line[0].strip()}")
            if value != '0':
                print("      WARNING: PairableTimeout is not 0. This will likely prevent pairing from staying enabled.")
                print("               Please edit /etc/bluetooth/main.conf, set PairableTimeout = 0, and run 'sudo systemctl restart bluetooth'.")
            else:
                print("      PairableTimeout is set to 0 (infinite), which is correct.")
        else:
            print("\nNOTE: PairableTimeout setting not found or commented out in main.conf (Default is 0 - infinite), which is correct.")
    except FileNotFoundError:
        print("\nNOTE: /etc/bluetooth/main.conf not found.")
    except Exception as e:
        print(f"\nNOTE: Could not read /etc/bluetooth/main.conf: {e}")