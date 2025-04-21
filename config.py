#!/usr/bin/env python3
"""
Centralised STR hardware/audio/GPIO settings (plus vector‑store and AI‑model
configuration).

⚠️  CHANGE:  OUTPUT_SAMPLE_RATE is now chosen automatically from the DAC’s own
             reported *defaultSampleRate* (as returned by PyAudio/ALSA).  
             You can still override it with the environment variable
             OUTPUT_SAMPLE_RATE if you really need a different value.

This fixes the “suena lento y grave” problem you heard: the code was playing
audio at a rate that didn’t match what the PCM5102 driver was actually using.
By querying the driver’s preferred rate first and resampling everything to that
rate, timing and pitch are correct.
"""

import os
import sys
import logging

# ------------------------------------------------------------------#
# Logging
# ------------------------------------------------------------------#
log_config = logging.getLogger("config")
if not log_config.hasHandlers():          # first module that sets the logger?
    log_config.setLevel(logging.INFO)
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)5s | %(name)s | %(message)s")
    )
    log_config.addHandler(_h)
    log_config.propagate = False
else:                                     # already configured by main app
    log_config.setLevel(logging.INFO)

# ------------------------------------------------------------------#
# Tiny helper to locate devices by name
# ------------------------------------------------------------------#
def find_device_by_name_fragment(p, fragments, *, is_input=True, threshold=0):
    """
    Return (index, name) of the first device whose name contains one of the
    *fragments* (case‑insensitive) **and** that has at least *threshold*
    channels for the requested direction.
    """
    chan_field = "maxInputChannels" if is_input else "maxOutputChannels"
    for idx in range(p.get_device_count()):
        try:
            info = p.get_device_info_by_index(idx)
            if info.get(chan_field, 0) < threshold:
                continue
            name = info.get("name", "").lower()
            if any(frag in name for frag in fragments):
                return idx, info["name"]
        except Exception as e:
            log_config.debug(f"PyAudio query failed for device {idx}: {e}")
    return None

# ------------------------------------------------------------------#
# Defaults / env‑vars
# ------------------------------------------------------------------#
DEFAULT_MIC_INDEX  = 1
DEFAULT_DAC_INDEX  = 0
ENV_MIC_INDEX      = os.getenv("MIC_DEVICE_INDEX")
ENV_DAC_INDEX      = os.getenv("DAC_PYAUDIO_INDEX")

detected_mic_name  = "N/A"
detected_dac_name  = "N/A"
mic_detection_method  = "unknown"
dac_detection_method  = "unknown"
detected_dac_rate     = 48000            # will be updated below if possible

# ------------------------------------------------------------------#
# Detect audio devices
# ------------------------------------------------------------------#
try:
    import pyaudio

    _pa = pyaudio.PyAudio()

    # ----- microphone -----
    if ENV_MIC_INDEX is not None:
        try:
            mic_idx   = int(ENV_MIC_INDEX)
            mic_info  = _pa.get_device_info_by_index(mic_idx)
            if mic_info["maxInputChannels"] > 0:
                detected_mic_name     = mic_info["name"]
                mic_detection_method  = "env‑var"
            else:
                raise ValueError("not an input device")
        except Exception as e:
            log_config.warning(f"MIC_DEVICE_INDEX invalid ({e}); falling back.")
            mic_idx = None
    else:
        mic_idx = None

    if mic_idx is None:
        mic_detection_method = "auto‑detect"
        res = find_device_by_name_fragment(
            _pa, ["usb", "microphone"], is_input=True, threshold=1
        )
        if res:
            mic_idx, detected_mic_name = res
            mic_detection_method += ":name"
        else:
            try:
                mic_idx             = _pa.get_default_input_device_info()["index"]
                detected_mic_name   = _pa.get_device_info_by_index(mic_idx)["name"]
                mic_detection_method += ":default"
            except Exception:
                mic_idx = DEFAULT_MIC_INDEX
                try:
                    detected_mic_name = _pa.get_device_info_by_index(mic_idx)["name"]
                except Exception:
                    detected_mic_name = f"Index {mic_idx}"
                mic_detection_method = "fallback"

    # ----- DAC / output -----
    if ENV_DAC_INDEX is not None:
        try:
            dac_idx   = int(ENV_DAC_INDEX)
            dac_info  = _pa.get_device_info_by_index(dac_idx)
            if dac_info["maxOutputChannels"] > 0:
                detected_dac_name    = dac_info["name"]
                detected_dac_rate    = int(round(dac_info.get("defaultSampleRate", 48000)))
                dac_detection_method = "env‑var"
            else:
                raise ValueError("not an output device")
        except Exception as e:
            log_config.warning(f"DAC_PYAUDIO_INDEX invalid ({e}); falling back.")
            dac_idx = None
    else:
        dac_idx = None

    if dac_idx is None:
        dac_detection_method = "auto‑detect"
        res = find_device_by_name_fragment(
            _pa,
            ["snd_rpi_hifiberry_dac", "pcm5102", "hifiberry", "audioinjector"],
            is_input=False,
            threshold=1,
        )
        if not res:
            res = find_device_by_name_fragment(
                _pa, ["speaker", "headphones", "usb audio", "dac"], is_input=False, threshold=1
            )
        if res:
            dac_idx, detected_dac_name = res
            dac_detection_method += ":name"
        else:
            try:
                default_info       = _pa.get_default_output_device_info()
                dac_idx            = default_info["index"]
                detected_dac_name  = default_info["name"]
                dac_detection_method += ":default"
            except Exception:
                dac_idx = DEFAULT_DAC_INDEX
                try:
                    detected_dac_name = _pa.get_device_info_by_index(dac_idx)["name"]
                except Exception:
                    detected_dac_name = f"Index {dac_idx}"
                dac_detection_method = "fallback"

        # Whatever method gave us *dac_idx*, fetch its preferred rate
        try:
            dac_info         = _pa.get_device_info_by_index(dac_idx)
            detected_dac_rate = int(round(dac_info.get("defaultSampleRate", 48000)))
        except Exception:
            detected_dac_rate = 48000

    _pa.terminate()

except Exception as e:
    log_config.error(f"PyAudio initialisation failed: {e}")
    mic_idx = int(ENV_MIC_INDEX) if ENV_MIC_INDEX else DEFAULT_MIC_INDEX
    dac_idx = int(ENV_DAC_INDEX) if ENV_DAC_INDEX else DEFAULT_DAC_INDEX
    detected_mic_name = f"Index {mic_idx}"
    detected_dac_name = f"Index {dac_idx}"
    mic_detection_method = dac_detection_method = "fallback"
    detected_dac_rate = 48000     # safe default

# ------------------------------------------------------------------#
# Public configuration constants
# ------------------------------------------------------------------#
# Mic
MIC_DEVICE_INDEX    = mic_idx
MIC_SAMPLE_RATE     = int(os.getenv("MIC_SAMPLE_RATE", "0"))     # 0 = PyAudio default
MIC_CHANNELS        = int(os.getenv("MIC_CHANNELS", "1"))
MIC_CHUNK           = int(os.getenv("MIC_CHUNK", "1024"))
MIC_NORMALISE       = os.getenv("MIC_NORMALISE", "1") == "1"

# Playback
DAC_PYAUDIO_INDEX   = dac_idx
PLAYBACK_CHUNK      = 1024

# >>> here’s the important bit: we honour OUTPUT_SAMPLE_RATE env‑var **or**
# >>> fall back to whatever the driver reported (detected_dac_rate)
OUTPUT_SAMPLE_RATE  = int(
    os.getenv("OUTPUT_SAMPLE_RATE", str(detected_dac_rate))
)

# ------------------------------------------------------------------#
# GPIO, vector‑store, model names … (unchanged from your original file)
# ------------------------------------------------------------------#
GPIO_BUTTON_PIN        = int(os.getenv("GPIO_BUTTON_PIN", "17"))
GPIO_LED_PIN           = int(os.getenv("GPIO_LED_PIN", "27"))
BUTTON_ACTIVE_HIGH     = os.getenv("BUTTON_ACTIVE_HIGH", "True")

_IS_RPI = False
if sys.platform == "linux":
    try:
        with open("/proc/cpuinfo") as _f:
            _IS_RPI = any(k in _f.read() for k in ("Raspberry Pi", "BCM27"))
        if _IS_RPI:
            import RPi.GPIO
    except Exception:
        _IS_RPI = False

ENABLE_GPIO_ENV        = os.getenv("ENABLE_GPIO", "True").lower() in ("true", "1", "yes")
ENABLE_GPIO            = _IS_RPI and ENABLE_GPIO_ENV

VECTOR_STORE_ID        = os.getenv(
    "VECTOR_STORE_ID", "vs_6800e568d74c8191927351dc5afbfd81"
)

OPENAI_MODEL_FILE_QA   = os.getenv("OPENAI_MODEL_FILE_QA", "gpt-4.1-mini")
OPENAI_MODEL_AGENT     = os.getenv("OPENAI_MODEL_AGENT", "gpt-4.1-mini")
OPENAI_MODEL_REALTIME  = os.getenv("OPENAI_MODEL_REALTIME", "gpt-4o-realtime-preview")
OPENAI_MODEL_TRANSCRIPTION = os.getenv("OPENAI_MODEL_TRANSCRIPTION", "whisper-1")

# ------------------------------------------------------------------#
# Final summary to the log
# ------------------------------------------------------------------#
log_config.info("--- Configuration --------------------------------")
log_config.info(f"Platform: {'RPi' if _IS_RPI else sys.platform}")
log_config.info(f"Mic   : index={MIC_DEVICE_INDEX}  ({detected_mic_name})  via {mic_detection_method}")
log_config.info(
    f"DAC   : index={DAC_PYAUDIO_INDEX} ({detected_dac_name})  via {dac_detection_method}"
)
log_config.info(f"DAC default sample‑rate reported by ALSA: {detected_dac_rate} Hz")
log_config.info(f"OUTPUT_SAMPLE_RATE in use           : {OUTPUT_SAMPLE_RATE} Hz")
log_config.info(f"GPIO enabled: {ENABLE_GPIO}")
log_config.info("--------------------------------------------------")
