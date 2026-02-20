"""
Configuration file for AI Teacher system
All settings in one place for easy modification
"""

import os

# ==================== CHROME SETTINGS ====================
# Optional: set CHROMEDRIVER_PATH to force a specific driver.
# If unset, Selenium Manager will download a compatible driver automatically.
CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH")
CHROME_USER_DATA_ROOT = os.path.expanduser("~/.config/google-chrome/AutoDebugProfile")
PROFILE_DIR_NAME = "Default"
CHROME_USER_PROFILE = os.path.join(CHROME_USER_DATA_ROOT, PROFILE_DIR_NAME)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXTENSION_DIR = os.path.join(BASE_DIR, "AutoTeacherExtension")
AUTOLOAD_EXTENSION = os.path.isdir(EXTENSION_DIR)
LOCAL_CFT_CHROME_BIN = os.path.join(BASE_DIR, ".tools", "cft", "chrome-linux64", "chrome")

# TEMP_WALKIE_MODE: temporary test mode that replaces NativeCamp class page
# with a local walkie receiver page for phone->class audio injection tests.
CLASS_WALKIE_MODE = True

# NativeCamp class URL preserved for normal operation.
NATIVECAMP_CLASS_URL = "https://nativecamp.net/teacher/lesson-tutorial"

# Walkie receiver URL used by the class tab when CLASS_WALKIE_MODE is enabled.
WALKIE_TLS_PORT = 5443
CLASS_WALKIE_RECEIVER_URL = f"https://127.0.0.1:{WALKIE_TLS_PORT}/walkie/receiver"

# Local HTTPS for phone microphone capture (required by most mobile browsers).
WALKIE_ENABLE_TLS = True
WALKIE_TLS_CERT_PATH = os.path.join(BASE_DIR, "certs", "walkie-cert.pem")
WALKIE_TLS_KEY_PATH = os.path.join(BASE_DIR, "certs", "walkie-key.pem")
WALKIE_SESSION_TTL_SECONDS = 1800

# Optional: isolate STT in a separate Chrome profile so it can keep different
# microphone/site permissions from teacher/ai/class.
STT_USE_SEPARATE_PROFILE = True
STT_CHROME_USER_DATA_ROOT = os.path.expanduser("~/.config/google-chrome/AutoDebugProfileSTT")
STT_PROFILE_DIR_NAME = "Default"
CLASS_USE_SEPARATE_PROFILE = True
CLASS_CHROME_USER_DATA_ROOT = os.path.expanduser("~/.config/google-chrome/AutoDebugProfileClass")
CLASS_PROFILE_DIR_NAME = "Default"
TEACHER_USE_SEPARATE_PROFILE = True
TEACHER_CHROME_USER_DATA_ROOT = os.path.expanduser("~/.config/google-chrome/AutoDebugProfileTeacher")
TEACHER_PROFILE_DIR_NAME = "Default"

CHROME_BIN_CANDIDATES = [
    LOCAL_CFT_CHROME_BIN,
    "google-chrome",
    "google-chrome-stable",
    "/usr/bin/google-chrome",
    "chromium",
    "chromium-browser",
    "/snap/bin/chromium"
]

# Extra Chrome flags for media/autoplay so pages can start AudioContext without gestures
CHROME_EXTRA_FLAGS = [
    "--autoplay-policy=no-user-gesture-required",
    # Keep unpacked extension loading stable on newer Chrome builds.
    "--enable-unsafe-extension-debugging",
    "--disable-features=MediaSessionService,DisableLoadExtensionCommandLineSwitch,CalculateNativeWinOcclusion",
    # Keep rendering/capture stable even when windows/tabs are occluded or in the background.
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    # TEMP_WALKIE_MODE: allow local self-signed HTTPS receiver page during tests.
    "--ignore-certificate-errors",
]

DEBUG_PORT = 9222
DEBUG_ADDR = f"127.0.0.1:{DEBUG_PORT}"
STT_DEBUG_PORT = 9223
STT_DEBUG_ADDR = f"127.0.0.1:{STT_DEBUG_PORT}"
CLASS_DEBUG_PORT = 9224
CLASS_DEBUG_ADDR = f"127.0.0.1:{CLASS_DEBUG_PORT}"
TEACHER_DEBUG_PORT = 9225
TEACHER_DEBUG_ADDR = f"127.0.0.1:{TEACHER_DEBUG_PORT}"

# ==================== URLS ====================
URLS = {
    "akool": "https://akool.com/apps/streaming-avatar/edit",
    "stt": "https://www.speechtexter.com/",
    "chatgpt": "https://chatgpt.com/",
    # TEMP_WALKIE_MODE: when enabled, class tab opens local receiver page.
    "nativecamp": CLASS_WALKIE_RECEIVER_URL if CLASS_WALKIE_MODE else NATIVECAMP_CLASS_URL
}

# ==================== ROUTER SETTINGS ====================
ROUTER_HOST = "127.0.0.1"
ROUTER_PORT = 5000
ROUTER_URL = f"http://{ROUTER_HOST}:{ROUTER_PORT}"

# ==================== AUDIO SETTINGS ====================
# Virtual microphone names (will be created)
VIRTUAL_MIC_A = "VirtualMic_Student"  # For student voice → STT
VIRTUAL_MIC_B = "VirtualMic_Teacher"  # For AI voice → Native Camp
CLASS_PULSE_SINK = "at_class_sink"
STT_PULSE_SOURCE = "student_voice"
TEACHER_PULSE_SINK = "at_teacher_sink"
TEACHER_PULSE_SOURCE = "teacher_voice"
AUDIO_SEGMENT_SECONDS = 4.0

# Teacher media bridge (virtual camera + virtual mic).
TEACHER_CAM_ENABLED = True
TEACHER_CAM_VIDEO_NR = 9
TEACHER_CAM_DEVICE = f"/dev/video{TEACHER_CAM_VIDEO_NR}"
TEACHER_CAM_LABEL = "teacher_cam"
TEACHER_CAM_FPS = 30
TEACHER_CAM_WIDTH = 960
TEACHER_CAM_HEIGHT = 540
TEACHER_CAPTURE_DISPLAY = ":0.0"
TEACHER_MEDIA_AUTOSTART = True
TEACHER_MEDIA_PREWARM = True
TEACHER_MEDIA_AUTOSTART_RETRY_INTERVAL_S = 1.5
TEACHER_MEDIA_AUTOSTART_MAX_WAIT_S = 90
TEACHER_MEDIA_AUTOSTART_REQUIRE_WINDOW_ID = True

# Audio settings
AUDIO_SAMPLE_RATE = 44100
AUDIO_CHANNELS = 2

# ==================== TIMING SETTINGS ====================
# Delays in seconds
CHROME_STARTUP_WAIT = 1.0
WINDOW_OPEN_DELAY = 0.6
WINDOW_POSITION_DELAY = 0.2
MESSAGE_SEND_DELAY = 0.3

# ==================== EXTENSION SETTINGS ====================
# Page identifiers (must match content.js)
PAGE_NAMES = {
    "login": "login",
    "home": "home",
    "class": "class",
    "AI": "AI",
    "teacher": "teacher",
    "stt": "stt"
}

# ==================== DEBUG SETTINGS ====================
DEBUG_MODE = True  # Enable detailed logging
VERBOSE_OUTPUT = True  # Print detailed position info
