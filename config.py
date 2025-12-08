"""
Configuration file for AI Teacher system
All settings in one place for easy modification
"""

import os

# ==================== CHROME SETTINGS ====================
CHROMEDRIVER_PATH = "/usr/local/bin/chromedriver"
CHROME_USER_DATA_ROOT = os.path.expanduser("~/.config/google-chrome/AutoDebugProfile")
PROFILE_DIR_NAME = "Default"
CHROME_USER_PROFILE = os.path.join(CHROME_USER_DATA_ROOT, PROFILE_DIR_NAME)

CHROME_BIN_CANDIDATES = [
    "google-chrome",
    "google-chrome-stable",
    "/usr/bin/google-chrome",
    "chromium",
    "chromium-browser",
    "/snap/bin/chromium"
]

DEBUG_PORT = 9222
DEBUG_ADDR = f"127.0.0.1:{DEBUG_PORT}"

# ==================== URLS ====================
URLS = {
    "akool": "https://akool.com/apps/streaming-avatar/edit",
    "stt": "https://www.speechtexter.com/",
    "chatgpt": "https://chatgpt.com/",
    "nativecamp": "https://nativecamp.net/teacher/login"
}

# URL list in order (for opening windows)
URLS_LIST = [
    URLS["akool"],
    URLS["stt"],
    URLS["chatgpt"],
    URLS["nativecamp"]
]

# ==================== WINDOW POSITIONS ====================
# Format: (x, y, width, height)
# Optimized for 1366x768 screen with Ubuntu dock
WINDOW_POSITIONS = [
    (0, 0, 960, 540),      # Window 0: Akool (Top-left)
    (960, 0, 960, 540),    # Window 1: SpeechTexter (Top-right)
    (0, 540, 960, 540),    # Window 2: ChatGPT (Bottom-left)
    (960, 540, 960, 540)   # Window 3: Native Camp (Bottom-right)
]

# ==================== ROUTER SETTINGS ====================
ROUTER_HOST = "127.0.0.1"
ROUTER_PORT = 5000
ROUTER_URL = f"http://{ROUTER_HOST}:{ROUTER_PORT}"

# ==================== AUDIO SETTINGS ====================
# Virtual microphone names (will be created)
VIRTUAL_MIC_A = "VirtualMic_Student"  # For student voice → STT
VIRTUAL_MIC_B = "VirtualMic_Teacher"  # For AI voice → Native Camp

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