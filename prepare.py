# prepare.py

from config import (
    CHROMEDRIVER_PATH,
    CHROME_USER_DATA_ROOT,
    PROFILE_DIR_NAME,
    CHROME_BIN_CANDIDATES,
    DEBUG_PORT,
    DEBUG_ADDR,
    URLS_LIST,
    WINDOW_POSITIONS,
    CHROME_STARTUP_WAIT,
    WINDOW_OPEN_DELAY,
    WINDOW_POSITION_DELAY,
)

import os
import time
import shutil
import subprocess
import socket
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options

import sys



# page mappings
window_mapping = {
    "teacher": None,
    "stt": None,
    "ai": None,
    "class": None
}
print("HEllo")

def find_chrome_bin():
    for name in CHROME_BIN_CANDIDATES:
        path = shutil.which(name) or (name if os.path.exists(name) else None)
        if path:
            return path
    return None


def is_port_open(host, port, timeout=0.3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def start_chrome_with_debug():
    if is_port_open("127.0.0.1", DEBUG_PORT):
        return True

    subprocess.run(["pkill", "-f", "chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-f", "chromium"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)

    chrome_bin = find_chrome_bin()
    if not chrome_bin:
        print("Chrome not found.")
        return False

    cmd = [
        chrome_bin,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={CHROME_USER_DATA_ROOT}",
        f"--profile-directory={PROFILE_DIR_NAME}",
        "--no-first-run",
        "--disable-first-run-ui",
        "--new-window",
    ]

    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + 10
    while time.time() < deadline:
        if is_port_open("127.0.0.1", DEBUG_PORT):
            return True
        time.sleep(0.3)

    print("Chrome debug port failed to open.")
    return False


def attach_selenium():
    opts = Options()
    opts.add_experimental_option("debuggerAddress", DEBUG_ADDR)
    service = Service(CHROMEDRIVER_PATH)
    return webdriver.Chrome(service=service, options=opts)

# Open pages
def open_urls(driver):
    driver.switch_to.window(driver.window_handles[0])
    driver.get(URLS_LIST[0])
    current_handle = driver.current_window_handle
    window_mapping["teacher"] = current_handle

    time.sleep(WINDOW_OPEN_DELAY)

    win_names = ['stt', 'ai', 'class']
    for url, name in zip(URLS_LIST[1:], win_names):
        try:
            driver.switch_to.new_window("window")
            driver.get(url)
            current_handle = driver.current_window_handle
            window_mapping[name] = current_handle
        except:
            driver.execute_script("window.open('about:blank', '_blank');")
            driver.switch_to.window(driver.window_handles[-1])
            driver.get(url)
        time.sleep(WINDOW_OPEN_DELAY)
    print(window_mapping)


def position_windows(driver):
    for i, (x, y, w, h) in enumerate(WINDOW_POSITIONS):
        try:
            driver.switch_to.window(driver.window_handles[i])
            driver.set_window_position(x, y)
            driver.set_window_size(w, h)
        except Exception as e:
            print(f"Positioning error for window {i}: {e}")
        time.sleep(WINDOW_POSITION_DELAY)



def prepare_environment():
    if not start_chrome_with_debug():
        return None  # indicate failure

    time.sleep(CHROME_STARTUP_WAIT)
    driver = attach_selenium()

    time.sleep(0.5)
    open_urls(driver)
    position_windows(driver)

    print("🚀 Environment ready — prepare.py done!")
    return driver  # give control back to main (driver stays alive in Chrome)
