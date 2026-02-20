from config import (
    AUDIO_SEGMENT_SECONDS,
    AUTOLOAD_EXTENSION,
    CHROMEDRIVER_PATH,
    CHROME_BIN_CANDIDATES,
    CHROME_EXTRA_FLAGS,
    CHROME_STARTUP_WAIT,
    CHROME_USER_DATA_ROOT,
    CLASS_CHROME_USER_DATA_ROOT,
    CLASS_DEBUG_ADDR,
    CLASS_DEBUG_PORT,
    CLASS_PROFILE_DIR_NAME,
    CLASS_PULSE_SINK,
    CLASS_USE_SEPARATE_PROFILE,
    DEBUG_ADDR,
    DEBUG_PORT,
    EXTENSION_DIR,
    LOCAL_CFT_CHROME_BIN,
    PROFILE_DIR_NAME,
    ROUTER_URL,
    STT_CHROME_USER_DATA_ROOT,
    STT_DEBUG_ADDR,
    STT_DEBUG_PORT,
    STT_PROFILE_DIR_NAME,
    STT_PULSE_SOURCE,
    STT_USE_SEPARATE_PROFILE,
    TEACHER_CHROME_USER_DATA_ROOT,
    TEACHER_DEBUG_ADDR,
    TEACHER_DEBUG_PORT,
    TEACHER_PROFILE_DIR_NAME,
    TEACHER_PULSE_SINK,
    TEACHER_PULSE_SOURCE,
    TEACHER_USE_SEPARATE_PROFILE,
    URLS,
    WINDOW_OPEN_DELAY,
    WINDOW_POSITION_DELAY,
)

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

try:
    from teacher_media_bridge import ensure_pulse_sink_and_source
except Exception:
    ensure_pulse_sink_and_source = None


# page mappings
window_handles_by_role = {
    "teacher": None,
    "stt": None,
    "ai": None,
    "class": None
}

# Best-effort X11 window IDs captured per role.
window_xids_by_role = {
    "teacher": None,
    "stt": None,
    "ai": None,
    "class": None,
}

# Tab placement: Ubuntu-style snap layout for 3 tabs:
# left half + right-top + right-bottom.
TAB_ROLE_GRID_SLOTS = (
    ("ai", 0),     # left half
    ("class", 1),  # right-top
    ("stt", 2),    # right-bottom
)

ROLE_URL_KEY_BY_ROLE = {
    "teacher": "akool",
    "stt": "stt",
    "ai": "chatgpt",
    "class": "nativecamp",
}

DEFAULT_GRID_WORKAREA = (0, 0, 1920, 1080)
TAB_LAYOUT_BUFFER_MULTIPLIER = 2.0
GUI_BREATH_SECONDS = 0.08
STRICT_STAGED_SINGLE_PROFILE_FLOW = True
# Simpler fallback mode:
# open all role windows on launcher workspace + 1 and keep manual window layout.
SINGLE_NEXT_WORKSPACE_MODE = True
# Hard bypass for workspace automation. Keep launch flow only.
SIMPLE_LAUNCH_ONLY = True
# Open the teacher tab on this workspace index (0-based).
TEACHER_OPEN_WORKSPACE = 3
# Buffer after successful workspace focus changes.
WORKSPACE_SWITCH_BUFFER_SECONDS = 0.08
ROLE_LAYOUT_STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".runtime",
    "role_window_layout.json",
)
_workspace_policy_restore = None


def _reset_role_window_handles():
    for key in list(window_handles_by_role.keys()):
        window_handles_by_role[key] = None
    for key in list(window_xids_by_role.keys()):
        window_xids_by_role[key] = None


def _flow_log(message):
    print(f"[prepare][flow] {message}")


def _flow_breath(label="buffer"):
    _flow_log(f"{label}: sleeping {GUI_BREATH_SECONDS:.1f}s")
    time.sleep(max(0.0, float(GUI_BREATH_SECONDS)))


def _gsettings_get(schema, key):
    if not shutil.which("gsettings"):
        return None
    try:
        proc = subprocess.run(
            ["gsettings", "get", str(schema), str(key)],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    value = str(proc.stdout or "").strip()
    return value if value else None


def _gsettings_set(schema, key, value_literal):
    if not shutil.which("gsettings"):
        return False, "gsettings not installed"
    try:
        proc = subprocess.run(
            ["gsettings", "set", str(schema), str(key), str(value_literal)],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except Exception as e:
        return False, str(e)
    if proc.returncode == 0:
        return True, ""
    detail = (proc.stderr or proc.stdout or "").strip()
    return False, detail or "gsettings set failed"


def _parse_gsettings_bool(raw):
    token = str(raw or "").strip().lower()
    if token == "true":
        return True
    if token == "false":
        return False
    return None


def _parse_gsettings_int(raw):
    try:
        return int(str(raw or "").strip())
    except Exception:
        return None


def _apply_static_workspace_policy(base_workspace=None, target_offset=1):
    """
    GNOME dynamic workspaces can reorder/remove empty workspaces during runtime.
    For deterministic automation, force static workspace count while Selenium runs.
    """
    global _workspace_policy_restore

    desktop = str(os.environ.get("XDG_CURRENT_DESKTOP", "")).lower()
    if "gnome" not in desktop:
        return {"applied": False, "reason": "non-gnome desktop"}
    if not shutil.which("gsettings"):
        return {"applied": False, "reason": "gsettings missing"}

    required_count = 3
    try:
        base_ws = int(base_workspace) if base_workspace is not None else 0
        offset = int(target_offset)
        required_count = max(2, base_ws + offset + 1)
    except Exception:
        required_count = 3

    dynamic_schema = "org.gnome.mutter"
    dynamic_key = "dynamic-workspaces"
    num_schema = "org.gnome.desktop.wm.preferences"
    num_key = "num-workspaces"

    dynamic_raw = _gsettings_get(dynamic_schema, dynamic_key)
    num_raw = _gsettings_get(num_schema, num_key)
    dynamic_value = _parse_gsettings_bool(dynamic_raw)
    num_value = _parse_gsettings_int(num_raw)

    desired_num = max(required_count, num_value or 0)
    changes = []
    restore = {}

    if dynamic_value is True:
        ok, detail = _gsettings_set(dynamic_schema, dynamic_key, "false")
        if ok:
            changes.append("dynamic-workspaces=false")
            restore["dynamic"] = dynamic_raw
        else:
            _flow_log(f"workspace-policy warning: failed disabling dynamic-workspaces ({detail})")

    if num_value is None or num_value < desired_num:
        ok, detail = _gsettings_set(num_schema, num_key, str(int(desired_num)))
        if ok:
            changes.append(f"num-workspaces={int(desired_num)}")
            restore["num"] = num_raw
        else:
            _flow_log(f"workspace-policy warning: failed setting num-workspaces ({detail})")

    if restore and _workspace_policy_restore is None:
        _workspace_policy_restore = restore

    active_ws, ws_count = _wmctrl_active_workspace()
    if ws_count is not None and int(ws_count) < int(required_count):
        _flow_log(
            f"workspace-policy warning: visible workspace count={ws_count} "
            f"is still below required={required_count}"
        )

    if changes:
        _flow_log(
            "workspace-policy applied: "
            f"{', '.join(changes)} (required_count={required_count}, active_ws={active_ws})"
        )
    return {
        "applied": bool(changes),
        "required_count": int(required_count),
        "active_workspace": int(active_ws) if active_ws is not None else None,
        "visible_workspace_count": int(ws_count) if ws_count is not None else None,
        "changes": tuple(changes),
    }


def restore_workspace_policy():
    global _workspace_policy_restore
    restore = _workspace_policy_restore
    if not isinstance(restore, dict) or not restore:
        return False

    dynamic_schema = "org.gnome.mutter"
    dynamic_key = "dynamic-workspaces"
    num_schema = "org.gnome.desktop.wm.preferences"
    num_key = "num-workspaces"

    restored = []
    dynamic_raw = restore.get("dynamic")
    if dynamic_raw is not None:
        ok, detail = _gsettings_set(dynamic_schema, dynamic_key, dynamic_raw)
        if ok:
            restored.append(f"{dynamic_key}={dynamic_raw}")
        else:
            _flow_log(f"workspace-policy restore warning: {dynamic_key} failed ({detail})")

    num_raw = restore.get("num")
    if num_raw is not None:
        ok, detail = _gsettings_set(num_schema, num_key, num_raw)
        if ok:
            restored.append(f"{num_key}={num_raw}")
        else:
            _flow_log(f"workspace-policy restore warning: {num_key} failed ({detail})")

    _workspace_policy_restore = None
    if restored:
        _flow_log("workspace-policy restored: " + ", ".join(restored))
        return True
    return False


def _coerce_rect_dict(raw):
    if not isinstance(raw, dict):
        return None
    try:
        x = int(raw.get("x"))
        y = int(raw.get("y"))
        w = int(raw.get("width"))
        h = int(raw.get("height"))
    except Exception:
        return None
    if w < 120 or h < 120:
        return None
    return {"x": x, "y": y, "width": w, "height": h}


def _load_saved_role_layouts():
    path = ROLE_LAYOUT_STATE_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    out = {}
    for role in ("teacher", "stt", "ai", "class"):
        rect = _coerce_rect_dict(payload.get(role))
        if rect:
            out[role] = rect
    return out


def _write_saved_role_layouts(layouts):
    if not isinstance(layouts, dict):
        return False
    path = ROLE_LAYOUT_STATE_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(layouts, fh, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _next_workspace_from_base(base_workspace):
    if base_workspace is None:
        return None
    try:
        base_ws = int(base_workspace)
    except Exception:
        return None
    target_ws = base_ws + 1
    active_ws, ws_count = _wmctrl_active_workspace()
    if ws_count is not None:
        try:
            ws_count = int(ws_count)
        except Exception:
            ws_count = None
    if ws_count is not None and target_ws >= ws_count:
        if _wmctrl_ensure_workspace_count(target_ws + 1):
            return target_ws
        # GNOME dynamic workspaces can still accept moving a window to +1 even
        # when desktop-count changes are not explicitly acknowledged.
        _flow_log(
            f"next-workspace warning: requested target={target_ws} "
            f"beyond current_count={ws_count}; proceeding with target anyway"
        )
    return target_ws


def _load_expected_extension_target_suffixes():
    # Detect our extension's background target from manifest so diagnostics don't
    # confuse built-in extensions (e.g. Hangouts) with AutoTeacher.
    suffixes = {"/background.js"}
    manifest_path = os.path.join(EXTENSION_DIR or "", "manifest.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        background = manifest.get("background") or {}
        service_worker = background.get("service_worker")
        page = background.get("page")
        for value in (service_worker, page):
            if isinstance(value, str) and value.strip():
                normalized = value.strip().replace("\\", "/")
                if not normalized.startswith("/"):
                    normalized = "/" + normalized
                suffixes.add(normalized)
    except Exception:
        pass
    return tuple(sorted(suffixes))


EXPECTED_EXTENSION_TARGET_SUFFIXES = _load_expected_extension_target_suffixes()


def _is_our_extension_target(target):
    url = str((target or {}).get("url", "")).strip()
    if not url.startswith("chrome-extension://"):
        return False
    return any(url.endswith(suffix) for suffix in EXPECTED_EXTENSION_TARGET_SUFFIXES)


class LaunchedEnvironment:
    """
    Wrapper so launcher_gui can still call .quit() once while we may own
    multiple webdriver connections (main + optional teacher/class/stt).
    """

    def __init__(self, main_driver, teacher_driver=None, class_driver=None, stt_driver=None):
        self.main_driver = main_driver
        self.teacher_driver = teacher_driver if teacher_driver is not main_driver else None
        self.class_driver = class_driver if class_driver is not main_driver else None
        self.stt_driver = stt_driver if stt_driver is not main_driver else None

    def __getattr__(self, name):
        return getattr(self.main_driver, name)

    def quit(self):
        errors = []
        for label, drv in (
            ("stt", self.stt_driver),
            ("class", self.class_driver),
            ("teacher", self.teacher_driver),
            ("main", self.main_driver),
        ):
            if drv is None:
                continue
            try:
                drv.quit()
            except Exception as e:
                errors.append(f"{label}.quit failed: {e}")

        if errors:
            print("[prepare] environment quit warnings:", " | ".join(errors))


def resolve_chrome_binary():
    for name in CHROME_BIN_CANDIDATES:
        path = shutil.which(name) or (name if os.path.exists(name) else None)
        if path:
            return path
    return None


def is_tcp_port_open(host, port, timeout=0.3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _listening_pids_for_port(port):
    pids = set()
    checks = [
        ["lsof", "-t", "-i", f"TCP:{port}", "-sTCP:LISTEN"],
        ["fuser", "-n", "tcp", str(port)],
    ]
    for cmd in checks:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except Exception:
            continue
        out = " ".join(
            part
            for part in (proc.stdout or "", proc.stderr or "")
            if isinstance(part, str)
        )
        for token in out.replace("/", " ").replace(":", " ").split():
            if token.isdigit():
                pids.add(int(token))
    return sorted(pids)


def _pids_for_debug_port(debug_port):
    try:
        port = int(debug_port)
    except Exception:
        return tuple()
    pids = [int(pid) for pid in _listening_pids_for_port(port) if isinstance(pid, int) and int(pid) > 0]
    return tuple(sorted(set(pids)))


def _expected_pids_for_role(role, role_driver, main_driver, teacher_driver=None, class_driver=None, stt_driver=None):
    if role == "teacher":
        if teacher_driver is not None and role_driver is teacher_driver and role_driver is not main_driver:
            return _pids_for_debug_port(TEACHER_DEBUG_PORT)
        return _pids_for_debug_port(DEBUG_PORT)
    if role == "class":
        if class_driver is not None and role_driver is class_driver and role_driver is not main_driver:
            return _pids_for_debug_port(CLASS_DEBUG_PORT)
        return _pids_for_debug_port(DEBUG_PORT)
    if role == "stt":
        if stt_driver is not None and role_driver is stt_driver and role_driver is not main_driver:
            return _pids_for_debug_port(STT_DEBUG_PORT)
        return _pids_for_debug_port(DEBUG_PORT)
    return _pids_for_debug_port(DEBUG_PORT)


def _pids_using_user_data_dir(user_data_dir):
    target = os.path.realpath(str(user_data_dir or "")).strip()
    if not target:
        return []
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.5,
        )
    except Exception:
        return []

    me = os.getpid()
    out = proc.stdout or ""
    pids = set()
    for line in out.splitlines():
        raw = str(line or "").strip()
        if not raw:
            continue
        parts = raw.split(None, 1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except Exception:
            continue
        if pid <= 0 or pid == me:
            continue
        args = parts[1] if len(parts) > 1 else ""
        lowered = args.lower()
        if "chrome" not in lowered and "chromium" not in lowered:
            continue
        if f"--user-data-dir={target}" in args:
            pids.add(pid)
            continue
        if "--user-data-dir" in args and target in args:
            pids.add(pid)
            continue
    return sorted(pids)


def _terminate_chrome_for_user_data_dir(user_data_dir, timeout_s=4.0):
    pids = _pids_using_user_data_dir(user_data_dir)
    if not pids:
        return True
    print(f"[prepare] profile cleanup: terminating pids for user-data-dir={user_data_dir}: {pids}")

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    deadline = time.time() + max(0.2, float(timeout_s))
    while time.time() < deadline:
        still = [pid for pid in pids if os.path.exists(f"/proc/{pid}")]
        if not still:
            return True
        time.sleep(0.12)

    still = [pid for pid in pids if os.path.exists(f"/proc/{pid}")]
    for pid in still:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    time.sleep(0.2)
    still = [pid for pid in pids if os.path.exists(f"/proc/{pid}")]
    if still:
        print(f"[prepare] profile cleanup warning: some pids survived for {user_data_dir}: {still}")
        return False
    return True


def _clear_profile_session_state(user_data_dir, profile_dir):
    profile_path = os.path.join(str(user_data_dir or ""), str(profile_dir or "Default"))
    removed = []

    # Legacy session files.
    for name in ("Current Session", "Current Tabs", "Last Session", "Last Tabs"):
        path = os.path.join(profile_path, name)
        if os.path.exists(path):
            try:
                os.remove(path)
                removed.append(path)
            except Exception:
                pass

    # Newer Chrome stores session state under Default/Sessions.
    sessions_dir = os.path.join(profile_path, "Sessions")
    if os.path.isdir(sessions_dir):
        try:
            for name in os.listdir(sessions_dir):
                if not (name.startswith("Session_") or name.startswith("Tabs_")):
                    continue
                path = os.path.join(sessions_dir, name)
                if os.path.isfile(path):
                    try:
                        os.remove(path)
                        removed.append(path)
                    except Exception:
                        pass
        except Exception:
            pass

    # Mark profile as cleanly exited so Chrome won't force crash-restore flow.
    prefs_path = os.path.join(profile_path, "Preferences")
    if os.path.isfile(prefs_path):
        try:
            with open(prefs_path, "r", encoding="utf-8") as fh:
                prefs = json.load(fh)
            profile_obj = prefs.get("profile")
            if not isinstance(profile_obj, dict):
                profile_obj = {}
                prefs["profile"] = profile_obj
            profile_obj["exited_cleanly"] = True
            profile_obj["exit_type"] = "Normal"
            with open(prefs_path, "w", encoding="utf-8") as fh:
                json.dump(prefs, fh, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            pass

    if removed:
        print(
            f"[prepare] session cleanup: removed {len(removed)} restore files "
            f"for profile={profile_path}"
        )


def terminate_debug_port_owner(port, timeout_s=4.0):
    pids = _listening_pids_for_port(port)
    if not pids:
        return True

    for pid in pids:
        try:
            os.kill(pid, 15)  # SIGTERM
        except Exception:
            pass

    deadline = time.time() + max(0.1, float(timeout_s))
    while time.time() < deadline:
        if not is_tcp_port_open("127.0.0.1", port):
            return True
        time.sleep(0.15)

    # Last resort
    pids = _listening_pids_for_port(port)
    for pid in pids:
        try:
            os.kill(pid, 9)  # SIGKILL
        except Exception:
            pass

    time.sleep(0.2)
    return not is_tcp_port_open("127.0.0.1", port)


def _fetch_debug_targets(debug_port, timeout=0.8):
    url = f"http://127.0.0.1:{debug_port}/json/list"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []


def wait_for_extension_target(debug_port, timeout_s=4.0):
    deadline = time.time() + max(0.5, float(timeout_s))
    while time.time() < deadline:
        targets = _fetch_debug_targets(debug_port, timeout=0.8)
        if any(_is_our_extension_target(t) for t in targets):
            return True
        time.sleep(0.2)
    return False


def launch_chrome_with_debug(
    *,
    debug_port,
    user_data_dir,
    profile_dir,
    kill_existing=False,
    label="chrome",
    env_overrides=None,
):
    if kill_existing:
        try:
            _terminate_chrome_for_user_data_dir(user_data_dir, timeout_s=4.5)
        except Exception as e:
            print(f"[prepare] {label}: profile cleanup warning for {user_data_dir}: {e}")
        try:
            _clear_profile_session_state(user_data_dir, profile_dir)
        except Exception as e:
            print(
                f"[prepare] {label}: session cleanup warning for "
                f"{user_data_dir}/{profile_dir}: {e}"
            )

    if is_tcp_port_open("127.0.0.1", debug_port):
        if not kill_existing:
            if AUTOLOAD_EXTENSION and EXTENSION_DIR and not wait_for_extension_target(debug_port, timeout_s=1.5):
                print(f"[prepare] {label}: debug port already open but no extension target visible on :{debug_port}.")
            return True
        print(f"[prepare] {label}: restarting existing debug port :{debug_port} owner.")
        if not terminate_debug_port_owner(debug_port):
            print(f"[prepare] {label}: failed to clear existing process on :{debug_port}.")
            return False

    chrome_bin = resolve_chrome_binary()
    if not chrome_bin:
        print(f"[prepare] {label}: Chrome not found.")
        return False

    cmd = [
        chrome_bin,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={user_data_dir}",
        f"--profile-directory={profile_dir}",
        "--no-first-run",
        "--disable-first-run-ui",
        "--new-window",
    ]
    cmd.extend(CHROME_EXTRA_FLAGS)
    if AUTOLOAD_EXTENSION and EXTENSION_DIR:
        cmd.extend([
            f"--disable-extensions-except={EXTENSION_DIR}",
            f"--load-extension={EXTENSION_DIR}",
        ])

    env = os.environ.copy()
    if isinstance(env_overrides, dict):
        for k, v in env_overrides.items():
            if v is None:
                continue
            env[str(k)] = str(v)

    print(
        f"[prepare] {label}: launching chrome on :{debug_port} "
        f"bin={chrome_bin} "
        f"profile={user_data_dir}/{profile_dir} "
        f"extension={'on' if AUTOLOAD_EXTENSION and EXTENSION_DIR else 'off'}"
    )
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

    deadline = time.time() + 12
    while time.time() < deadline:
        if is_tcp_port_open("127.0.0.1", debug_port):
            if AUTOLOAD_EXTENSION and EXTENSION_DIR:
                if wait_for_extension_target(debug_port, timeout_s=3.5):
                    print(f"[prepare] {label}: extension target detected on :{debug_port}.")
                else:
                    targets = _fetch_debug_targets(debug_port, timeout=1.0)
                    ext_urls = [
                        str(t.get("url", ""))
                        for t in targets
                        if str(t.get("url", "")).startswith("chrome-extension://")
                    ]
                    cft_hint = ""
                    try:
                        if LOCAL_CFT_CHROME_BIN and os.path.exists(LOCAL_CFT_CHROME_BIN):
                            cft_hint = f" local_cft={LOCAL_CFT_CHROME_BIN}"
                    except Exception:
                        cft_hint = ""
                    print(
                        f"[prepare] {label}: warning: AutoTeacher extension target not found on :{debug_port}; "
                        f"seen_extension_targets={ext_urls}{cft_hint}"
                    )
            return True
        time.sleep(0.3)

    print(f"[prepare] {label}: debug port {debug_port} failed to open.")
    return False


def connect_webdriver(debug_addr):
    opts = Options()
    opts.add_experimental_option("debuggerAddress", debug_addr)
    if CHROMEDRIVER_PATH:
        service = Service(CHROMEDRIVER_PATH)
        try:
            return webdriver.Chrome(service=service, options=opts)
        except Exception as exc:
            print(f"ChromeDriver at {CHROMEDRIVER_PATH} failed ({exc}); falling back to Selenium Manager.")

    original_path = os.environ.get("PATH", "")
    path_parts = original_path.split(os.pathsep) if original_path else []
    filtered_parts = [p for p in path_parts if not os.path.isfile(os.path.join(p, "chromedriver"))]
    if filtered_parts != path_parts:
        os.environ["PATH"] = os.pathsep.join(filtered_parts)
        print("Ignoring chromedriver in PATH to let Selenium Manager fetch a compatible version.")
    try:
        return webdriver.Chrome(options=opts)
    finally:
        os.environ["PATH"] = original_path


def _collapse_to_single_window(driver, label="chrome"):
    """
    Keep only one window for deterministic role->window mapping.
    Chrome profiles can restore prior tabs/windows, which breaks layout logic.
    """
    try:
        handles = list(driver.window_handles)
    except Exception:
        return

    if not handles:
        return

    keep = handles[0]
    closed = 0
    for h in handles[1:]:
        try:
            driver.switch_to.window(h)
            driver.close()
            closed += 1
        except Exception as e:
            print(f"[prepare] {label}: failed closing extra window: {e}")

    try:
        driver.switch_to.window(keep)
    except Exception as e:
        print(f"[prepare] {label}: failed switching to primary window: {e}")

    if closed > 0:
        print(f"[prepare] {label}: collapsed restored windows, closed={closed}.")


def _best_effort_close_handle(driver, handle, preserve_handle=None):
    if driver is None or not handle:
        return False
    try:
        handles = list(driver.window_handles)
    except Exception:
        return False
    if handle not in handles:
        return False
    if len(handles) <= 1:
        return False

    closed = False
    try:
        driver.switch_to.window(handle)
        driver.close()
        closed = True
    except Exception:
        closed = False

    try:
        remaining = list(driver.window_handles)
    except Exception:
        remaining = []
    if preserve_handle and preserve_handle in remaining:
        try:
            driver.switch_to.window(preserve_handle)
        except Exception:
            pass
    elif remaining:
        try:
            driver.switch_to.window(remaining[0])
        except Exception:
            pass
    return closed


def _open_new_window_handle(driver):
    before = []
    try:
        before = list(driver.window_handles)
    except Exception:
        before = []
    before_set = set(before)

    try:
        driver.switch_to.new_window("window")
        return driver.current_window_handle, "selenium_new_window"
    except Exception:
        pass

    try:
        driver.execute_cdp_cmd("Target.createTarget", {"url": "about:blank", "newWindow": True})
        deadline = time.time() + 2.5
        while time.time() < deadline:
            try:
                handles = list(driver.window_handles)
            except Exception:
                handles = []
            new_handles = [h for h in handles if h not in before_set]
            if new_handles:
                driver.switch_to.window(new_handles[-1])
                return driver.current_window_handle, "cdp_new_window"
            time.sleep(0.08)
    except Exception:
        pass

    try:
        driver.execute_script(
            "window.open('about:blank','_blank','popup=yes,width=1280,height=720');"
        )
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                handles = list(driver.window_handles)
            except Exception:
                handles = []
            new_handles = [h for h in handles if h not in before_set]
            if new_handles:
                driver.switch_to.window(new_handles[-1])
                return driver.current_window_handle, "window_open_popup"
            time.sleep(0.08)
    except Exception:
        pass

    # Last resort: ask Chrome itself to open a new top-level window.
    try:
        chrome_bin = resolve_chrome_binary()
        if chrome_bin:
            subprocess.Popen(
                [chrome_bin, "--new-window", "about:blank"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.time() + 2.5
            while time.time() < deadline:
                try:
                    handles = list(driver.window_handles)
                except Exception:
                    handles = []
                new_handles = [h for h in handles if h not in before_set]
                if new_handles:
                    driver.switch_to.window(new_handles[-1])
                    return driver.current_window_handle, "chrome_cli_new_window"
                time.sleep(0.08)
    except Exception:
        pass

    return None, "failed"


def _open_role_page(driver, role, url, use_current_window=False):
    if use_current_window:
        driver.switch_to.window(driver.window_handles[0])
        driver.get(url)
        current_handle = driver.current_window_handle
        window_handles_by_role[role] = current_handle
        try:
            print(f"[prepare] opened role={role} target={url} actual={driver.current_url}")
        except Exception:
            pass
        return

    method = ""
    handle, method = _open_new_window_handle(driver)
    if not handle:
        try:
            driver.execute_script("window.open('about:blank', '_blank');")
            driver.switch_to.window(driver.window_handles[-1])
            method = "tab_fallback"
        except Exception:
            method = "failed"
    if not handle and method == "failed":
        try:
            if driver.window_handles:
                driver.switch_to.window(driver.window_handles[-1])
                method = "last_window_fallback"
        except Exception:
            pass
    driver.get(url)

    current_handle = driver.current_window_handle
    window_handles_by_role[role] = current_handle
    try:
        print(
            f"[prepare] opened role={role} target={url} "
            f"actual={driver.current_url} method={method}"
        )
    except Exception:
        pass


def _log_role_handle_diagnostics():
    role_order = ("teacher", "stt", "ai", "class")
    parts = []
    handle_to_roles = {}
    xid_to_roles = {}
    for role in role_order:
        handle = window_handles_by_role.get(role)
        xid = window_xids_by_role.get(role)
        shown = str(handle or "-")
        parts.append(f"{role}={shown} xid={xid or '-'}")
        if handle:
            key = str(handle)
            handle_to_roles.setdefault(key, []).append(role)
        if xid:
            xid_to_roles.setdefault(str(xid), []).append(role)

    print("[prepare] role handles: " + ", ".join(parts))
    duplicates = [roles for roles in handle_to_roles.values() if len(roles) > 1]
    if duplicates:
        for roles in duplicates:
            print(f"[prepare] role handle warning: multiple roles share one handle: {roles}")
    xid_duplicates = [roles for roles in xid_to_roles.values() if len(roles) > 1]
    if xid_duplicates:
        for roles in xid_duplicates:
            print(f"[prepare] role xid warning: multiple roles share one X11 window: {roles}")


def _open_roles_staged_in_main_profile(main_driver, base_workspace=None):
    """
    Exact requested sequence:
    1) Open ai/class/stt on the current/base workspace.
    2) Move focus to TEACHER_OPEN_WORKSPACE and open teacher last.
    """
    role_sequence = (
        ("ai", "chatgpt"),
        ("class", "nativecamp"),
        ("stt", "stt"),
        ("teacher", "akool"),
    )
    _flow_log(
        "stage=open_all_on_base "
        f"base_workspace={base_workspace} order={[role for role, _url_key in role_sequence]}"
    )

    for idx, (role, url_key) in enumerate(role_sequence):
        if role == "teacher":
            required_count = int(TEACHER_OPEN_WORKSPACE) + 1
            if not _wmctrl_ensure_workspace_count(required_count):
                _flow_log(
                    f"teacher-open warning: workspace count not confirmed for target={TEACHER_OPEN_WORKSPACE}"
                )
            switched, detail = _wmctrl_switch_workspace(TEACHER_OPEN_WORKSPACE)
            if switched:
                _flow_log(f"teacher-open: switched to workspace={TEACHER_OPEN_WORKSPACE} before opening teacher")
            else:
                _flow_log(
                    f"teacher-open warning: failed switching to workspace={TEACHER_OPEN_WORKSPACE} ({detail})"
                )
        elif base_workspace is not None:
            _wmctrl_switch_workspace(base_workspace)
        use_current = idx == 0
        _open_role_page(
            main_driver,
            role,
            URLS[url_key],
            use_current_window=use_current,
        )
        target_ws = TEACHER_OPEN_WORKSPACE if role == "teacher" else base_workspace
        moved_note = "skip"
        if target_ws is not None:
            expected_pids = _pids_for_debug_port(DEBUG_PORT)
            moved = _move_role_window_to_workspace(
                role,
                main_driver,
                window_handles_by_role.get(role),
                int(target_ws),
                expected_pids=expected_pids,
            )
            moved_note = "ok" if moved else "failed"
        _flow_log(
            f"opened role={role} url_key={url_key} use_current_window={use_current} "
            f"target_ws={target_ws} move={moved_note}"
        )
        if role == "teacher":
            ensure_ok, ensure_note = _ensure_teacher_window_maximized(
                main_driver,
                window_handles_by_role.get("teacher"),
                expected_pids=_pids_for_debug_port(DEBUG_PORT),
            )
            level = "ok" if ensure_ok else "warn"
            _flow_log(f"teacher-open: maximize_{level}={ensure_note}")
            if not ensure_ok:
                fallback_ok, fallback_note = _maximize_window_via_webdriver(
                    main_driver,
                    window_handles_by_role.get("teacher"),
                )
                fb_level = "ok" if fallback_ok else "warn"
                _flow_log(f"teacher-open: webdriver_maximize_{fb_level}={fallback_note}")
                if fallback_ok:
                    ensure_ok, ensure_note = _ensure_teacher_window_maximized(
                        main_driver,
                        window_handles_by_role.get("teacher"),
                        expected_pids=_pids_for_debug_port(DEBUG_PORT),
                    )
                    verify_level = "ok" if ensure_ok else "warn"
                    _flow_log(f"teacher-open: maximize_verify_{verify_level}={ensure_note}")
            if base_workspace is not None:
                tabs_ws = int(base_workspace)
                switched_tabs, detail_tabs = _wmctrl_switch_workspace(tabs_ws)
                if switched_tabs:
                    _flow_log(f"post-teacher: switched to tabs workspace={tabs_ws} for restore checks")
                else:
                    _flow_log(
                        f"post-teacher warning: failed switching to tabs workspace={tabs_ws} ({detail_tabs})"
                    )

                expected_pids = _pids_for_debug_port(DEBUG_PORT)
                for tab_role in ("ai", "class", "stt"):
                    restore_ok, restore_note = _ensure_role_window_restored_down(
                        tab_role,
                        main_driver,
                        window_handles_by_role.get(tab_role),
                        expected_pids=expected_pids,
                    )
                    level = "ok" if restore_ok else "warn"
                    _flow_log(f"tabs-restore role={tab_role} status_{level}={restore_note}")

                _flow_log(f"tabs-layout: arranging 2x2 quarters on workspace={tabs_ws}")
                try:
                    arrange_windows(
                        main_driver,
                        teacher_driver=None,
                        class_driver=None,
                        stt_driver=None,
                        tabs_workspace=tabs_ws,
                    )
                except Exception as e:
                    _flow_log(f"tabs-layout warning: arrange failed ({e})")

                final_switched, final_detail = _wmctrl_switch_workspace(tabs_ws)
                if final_switched:
                    _flow_log(f"post-teacher: final_focus_workspace={tabs_ws}")
                else:
                    _flow_log(
                        f"post-teacher warning: final focus switch failed workspace={tabs_ws} ({final_detail})"
                    )
            else:
                final_switched, final_detail = _wmctrl_switch_workspace(TEACHER_OPEN_WORKSPACE)
                if final_switched:
                    _flow_log(f"teacher-open: final_focus_workspace={TEACHER_OPEN_WORKSPACE}")
                else:
                    _flow_log(
                        f"teacher-open warning: final focus switch failed workspace={TEACHER_OPEN_WORKSPACE} ({final_detail})"
                    )
        _flow_breath(f"post-open role={role}")


def open_main_pages(driver, include_teacher=True, include_class=False, include_stt=False):
    if include_teacher:
        _open_role_page(driver, "teacher", URLS["akool"], use_current_window=True)
        time.sleep(WINDOW_OPEN_DELAY)
        _open_role_page(driver, "ai", URLS["chatgpt"], use_current_window=False)
        time.sleep(WINDOW_OPEN_DELAY)
    else:
        _open_role_page(driver, "ai", URLS["chatgpt"], use_current_window=True)
        time.sleep(WINDOW_OPEN_DELAY)

    if include_class:
        _open_role_page(driver, "class", URLS["nativecamp"], use_current_window=False)
        time.sleep(WINDOW_OPEN_DELAY)

    if include_stt:
        _open_role_page(driver, "stt", URLS["stt"], use_current_window=False)
        time.sleep(WINDOW_OPEN_DELAY)


def open_class_page_separate():
    ok = launch_chrome_with_debug(
        debug_port=CLASS_DEBUG_PORT,
        user_data_dir=CLASS_CHROME_USER_DATA_ROOT,
        profile_dir=CLASS_PROFILE_DIR_NAME,
        kill_existing=True,
        label="class",
        env_overrides={"PULSE_SINK": CLASS_PULSE_SINK},
    )
    if not ok:
        return None

    time.sleep(CHROME_STARTUP_WAIT)
    class_driver = connect_webdriver(CLASS_DEBUG_ADDR)
    _collapse_to_single_window(class_driver, label="class")
    _open_role_page(class_driver, "class", URLS["nativecamp"], use_current_window=True)
    return class_driver


def open_stt_page_separate():
    ok = launch_chrome_with_debug(
        debug_port=STT_DEBUG_PORT,
        user_data_dir=STT_CHROME_USER_DATA_ROOT,
        profile_dir=STT_PROFILE_DIR_NAME,
        kill_existing=True,
        label="stt",
        env_overrides={"PULSE_SOURCE": STT_PULSE_SOURCE},
    )
    if not ok:
        return None

    time.sleep(CHROME_STARTUP_WAIT)
    stt_driver = connect_webdriver(STT_DEBUG_ADDR)
    _collapse_to_single_window(stt_driver, label="stt")
    _open_role_page(stt_driver, "stt", URLS["stt"], use_current_window=True)
    return stt_driver


def open_teacher_page_separate():
    if callable(ensure_pulse_sink_and_source):
        try:
            preflight = ensure_pulse_sink_and_source(TEACHER_PULSE_SINK, TEACHER_PULSE_SOURCE)
            if not preflight.get("ready"):
                print(f"[prepare] teacher pulse preflight not ready: {preflight}")
        except Exception as e:
            print(f"[prepare] teacher pulse preflight failed: {e}")

    ok = launch_chrome_with_debug(
        debug_port=TEACHER_DEBUG_PORT,
        user_data_dir=TEACHER_CHROME_USER_DATA_ROOT,
        profile_dir=TEACHER_PROFILE_DIR_NAME,
        kill_existing=True,
        label="teacher",
        env_overrides={"PULSE_SINK": TEACHER_PULSE_SINK},
    )
    if not ok:
        return None

    time.sleep(CHROME_STARTUP_WAIT)
    teacher_driver = connect_webdriver(TEACHER_DEBUG_ADDR)
    _collapse_to_single_window(teacher_driver, label="teacher")
    _open_role_page(teacher_driver, "teacher", URLS["akool"], use_current_window=True)
    return teacher_driver


def _wmctrl_workspace_workarea(target_ws=None):
    if not shutil.which("wmctrl"):
        return None
    try:
        proc = subprocess.run(
            ["wmctrl", "-d"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except Exception:
        return None

    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None

    line_by_idx = {}
    active_idx = None
    for ln in lines:
        cols = ln.split()
        if not cols:
            continue
        try:
            idx = int(cols[0])
        except Exception:
            continue
        line_by_idx[idx] = ln
        if len(cols) > 1 and cols[1] == "*":
            active_idx = idx

    selected_idx = active_idx
    if target_ws is not None:
        try:
            requested = int(target_ws)
            if requested in line_by_idx:
                selected_idx = requested
        except Exception:
            pass
    if selected_idx is None:
        return None

    selected_line = line_by_idx.get(selected_idx)
    if not selected_line:
        return None

    m = re.search(r"WA:\s*(-?\d+),(-?\d+)\s+(\d+)x(\d+)", selected_line, re.IGNORECASE)
    if not m:
        return None
    try:
        x = int(m.group(1))
        y = int(m.group(2))
        w = int(m.group(3))
        h = int(m.group(4))
    except Exception:
        return None
    if w < 320 or h < 240:
        return None
    return (x, y, w, h)


def _build_grid_cells(workarea):
    try:
        x = int(workarea[0])
        y = int(workarea[1])
        w = int(workarea[2])
        h = int(workarea[3])
    except Exception:
        x, y, w, h = DEFAULT_GRID_WORKAREA

    if w < 320 or h < 240:
        x, y, w, h = DEFAULT_GRID_WORKAREA

    # Use wmctrl WA (workarea) to avoid GNOME panels/docks, then build
    # snap-like regions: left half + right-top + right-bottom.
    left_w = max(1, w // 2)
    right_w = max(1, w - left_w)
    top_h = max(1, h // 2)
    bottom_h = max(1, h - top_h)
    return (
        (x, y, left_w, h),
        (x + left_w, y, right_w, top_h),
        (x + left_w, y + top_h, right_w, bottom_h),
    )


def _inflate_tab_rect(rect, workarea, multiplier=2.0):
    try:
        x = int(rect[0])
        y = int(rect[1])
        w = int(rect[2])
        h = int(rect[3])
        wx = int(workarea[0])
        wy = int(workarea[1])
        ww = int(workarea[2])
        wh = int(workarea[3])
    except Exception:
        return rect

    if w <= 1 or h <= 1 or ww <= 1 or wh <= 1:
        return rect

    m = max(0.0, float(multiplier))
    # Expand by a frame/gap buffer; multiplier=2.0 doubles this expansion.
    base_pad_x = max(8, int(w * 0.03))
    base_pad_y = max(10, int(h * 0.04))
    pad_x = int(base_pad_x * m)
    pad_y = int(base_pad_y * m)

    left = max(wx, x - pad_x)
    top = max(wy, y - pad_y)
    right = min(wx + ww, x + w + pad_x)
    bottom = min(wy + wh, y + h + pad_y)

    new_w = max(1, right - left)
    new_h = max(1, bottom - top)
    return (left, top, new_w, new_h)


def arrange_windows(main_driver, teacher_driver=None, class_driver=None, stt_driver=None, tabs_workspace=None):
    """
    Arrange non-teacher roles in Ubuntu-like snap layout:
    one left half + two right stacked regions.
    Teacher is intentionally excluded from this sizing pass.
    """
    role_to_driver = {
        "teacher": teacher_driver or main_driver,
        "stt": stt_driver or main_driver,
        "ai": main_driver,
        "class": class_driver or main_driver,
    }

    workarea = _wmctrl_workspace_workarea(tabs_workspace) or DEFAULT_GRID_WORKAREA
    cells = _build_grid_cells(workarea)
    print(
        "[prepare] tab snap layout: "
        f"tabs_ws={tabs_workspace}, workarea={workarea}, "
        "slots=0(left-half),1(right-top),2(right-bottom), "
        f"buffer_multiplier={TAB_LAYOUT_BUFFER_MULTIPLIER:.1f}"
    )

    for role, slot_idx in TAB_ROLE_GRID_SLOTS:
        handle = window_handles_by_role.get(role)
        driver = role_to_driver.get(role)
        if not handle or driver is None:
            print(f"[prepare] tab layout skipped for role={role}: missing handle/driver.")
            continue
        if slot_idx < 0 or slot_idx >= len(cells):
            print(f"[prepare] tab layout skipped for role={role}: invalid slot={slot_idx}.")
            continue

        x, y, w, h = cells[slot_idx]
        x, y, w, h = _inflate_tab_rect(
            (x, y, w, h),
            workarea,
            multiplier=TAB_LAYOUT_BUFFER_MULTIPLIER,
        )
        wid = str(window_xids_by_role.get(role) or "").strip() or None
        if wid and _wmctrl_window_row_by_id(wid) is None:
            wid = None
        if not wid:
            try:
                wid, _rect, _title = _resolve_role_window_id(
                    driver,
                    handle,
                    attempts=8,
                    sleep_s=0.12,
                )
            except Exception:
                wid = None
        if wid:
            window_xids_by_role[role] = str(wid)
            ok_rect, note_rect = _wmctrl_set_window_rect(wid, x, y, w, h)
            if ok_rect:
                print(
                    f"[prepare] tab layout applied role={role} "
                    f"slot={slot_idx} rect={x},{y} {w}x{h} via=wmctrl ({note_rect})."
                )
                time.sleep(WINDOW_POSITION_DELAY)
                continue
            print(f"[prepare] tab layout wmctrl fallback role={role}: {note_rect}")

        try:
            driver.switch_to.window(handle)
            try:
                driver.set_window_rect(x=int(x), y=int(y), width=int(w), height=int(h))
            except Exception:
                driver.set_window_position(int(x), int(y))
                driver.set_window_size(int(w), int(h))
            print(
                f"[prepare] tab layout applied role={role} "
                f"slot={slot_idx} rect={x},{y} {w}x{h} via=webdriver."
            )
        except Exception as e:
            print(f"[prepare] tab layout error role={role}: {e}")
        time.sleep(WINDOW_POSITION_DELAY)


def _role_to_driver_map(main_driver, teacher_driver=None, class_driver=None, stt_driver=None):
    return {
        "teacher": teacher_driver or main_driver,
        "stt": stt_driver or main_driver,
        "ai": main_driver,
        "class": class_driver or main_driver,
    }


def apply_saved_role_layout(main_driver, teacher_driver=None, class_driver=None, stt_driver=None):
    layouts = _load_saved_role_layouts()
    if not layouts:
        print("[prepare] saved role layout: none found.")
        return False

    role_to_driver = _role_to_driver_map(
        main_driver,
        teacher_driver=teacher_driver,
        class_driver=class_driver,
        stt_driver=stt_driver,
    )
    applied = []
    for role in ("stt", "ai", "class", "teacher"):
        rect = layouts.get(role)
        handle = window_handles_by_role.get(role)
        role_driver = role_to_driver.get(role)
        if not rect or not handle or role_driver is None:
            continue
        try:
            role_driver.switch_to.window(handle)
            try:
                role_driver.set_window_rect(
                    x=int(rect["x"]),
                    y=int(rect["y"]),
                    width=int(rect["width"]),
                    height=int(rect["height"]),
                )
            except Exception:
                role_driver.set_window_position(int(rect["x"]), int(rect["y"]))
                role_driver.set_window_size(int(rect["width"]), int(rect["height"]))
            applied.append(role)
            time.sleep(max(0.05, float(WINDOW_POSITION_DELAY)))
        except Exception as e:
            print(f"[prepare] saved role layout apply failed role={role}: {e}")

    if applied:
        print(f"[prepare] saved role layout applied: {applied}")
        return True
    print("[prepare] saved role layout: no matching role windows to apply.")
    return False


def save_current_role_layout(driver_or_env):
    if driver_or_env is None:
        return False

    main_driver = getattr(driver_or_env, "main_driver", driver_or_env)
    teacher_driver = getattr(driver_or_env, "teacher_driver", None)
    class_driver = getattr(driver_or_env, "class_driver", None)
    stt_driver = getattr(driver_or_env, "stt_driver", None)
    role_to_driver = _role_to_driver_map(
        main_driver,
        teacher_driver=teacher_driver,
        class_driver=class_driver,
        stt_driver=stt_driver,
    )

    layouts = _load_saved_role_layouts()
    updated = False
    for role in ("stt", "ai", "class", "teacher"):
        handle = window_handles_by_role.get(role)
        role_driver = role_to_driver.get(role)
        if not handle or role_driver is None:
            continue
        try:
            role_driver.switch_to.window(handle)
            rect = role_driver.get_window_rect()
            clean_rect = _coerce_rect_dict(rect)
            if not clean_rect:
                continue
            layouts[role] = clean_rect
            updated = True
        except Exception as e:
            print(f"[prepare] saved role layout capture failed role={role}: {e}")

    if not updated:
        print("[prepare] saved role layout: nothing captured.")
        return False
    ok = _write_saved_role_layouts(layouts)
    if ok:
        print(f"[prepare] saved role layout written: {ROLE_LAYOUT_STATE_PATH}")
        return True
    print(f"[prepare] saved role layout write failed: {ROLE_LAYOUT_STATE_PATH}")
    return False


def _wmctrl_active_workspace():
    if not shutil.which("wmctrl"):
        return None, None
    try:
        proc = subprocess.run(
            ["wmctrl", "-d"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except Exception:
        return None, None

    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None, None

    active = None
    for ln in lines:
        cols = ln.split()
        if not cols:
            continue
        try:
            idx = int(cols[0])
        except Exception:
            continue
        if len(cols) > 1 and cols[1] == "*":
            active = idx
            break
    return active, len(lines)


def _xprop_workspace_count():
    if not shutil.which("xprop"):
        return None
    try:
        proc = subprocess.run(
            ["xprop", "-root", "_NET_NUMBER_OF_DESKTOPS"],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.5,
        )
    except Exception:
        return None
    out = str(proc.stdout or "")
    m = re.search(r"_NET_NUMBER_OF_DESKTOPS\(CARDINAL\)\s*=\s*(\d+)", out)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _observed_workspace_count():
    _active, wmctrl_count = _wmctrl_active_workspace()
    xprop_count = _xprop_workspace_count()
    counts = []
    for raw in (wmctrl_count, xprop_count):
        try:
            val = int(raw)
        except Exception:
            continue
        if val > 0:
            counts.append(val)
    if not counts:
        return None
    return max(counts)


def _wmctrl_ensure_workspace_count(min_count):
    if not shutil.which("wmctrl"):
        return False
    try:
        min_count = int(min_count)
    except Exception:
        return False
    if min_count <= 0:
        return False

    count_before = _observed_workspace_count()
    if count_before is not None and int(count_before) >= min_count:
        return True

    def _try_wmctrl_resize():
        try:
            proc = subprocess.run(
                ["wmctrl", "-n", str(min_count)],
                capture_output=True,
                text=True,
                check=False,
                timeout=2.0,
            )
        except Exception:
            return False
        return proc.returncode == 0

    _try_wmctrl_resize()

    # First pass: fast path for WMs that honor wmctrl -n directly.
    deadline = time.time() + 1.2
    while time.time() < deadline:
        count_after = _observed_workspace_count()
        if count_after is not None and int(count_after) >= min_count:
            return True
        time.sleep(0.08)

    # Fallback for GNOME/Mutter dynamic mode where wmctrl -n often "succeeds"
    # without actually extending visible desktops.
    _flow_log(
        "workspace-count fallback: wmctrl -n did not reach "
        f"{min_count}; applying static workspace policy"
    )
    base_guess = max(0, int(min_count) - 2)
    _apply_static_workspace_policy(base_workspace=base_guess, target_offset=1)
    _try_wmctrl_resize()

    deadline = time.time() + 2.0
    while time.time() < deadline:
        count_after = _observed_workspace_count()
        if count_after is not None and int(count_after) >= min_count:
            return True
        time.sleep(0.08)
    final_count = _observed_workspace_count()
    _flow_log(
        "workspace-count warning: requested="
        f"{min_count}, observed={final_count}"
    )
    return False


def _wmctrl_switch_workspace(target_ws):
    if not shutil.which("wmctrl"):
        return False, "wmctrl not installed"
    try:
        proc = subprocess.run(
            ["wmctrl", "-s", str(int(target_ws))],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except Exception as e:
        return False, str(e)

    if proc.returncode == 0:
        time.sleep(max(0.0, float(WORKSPACE_SWITCH_BUFFER_SECONDS)))
        return True, ""
    detail = (proc.stderr or proc.stdout or "").strip()
    return False, detail or "wmctrl switch failed"


def _wmctrl_list_windows():
    if not shutil.which("wmctrl"):
        return []
    try:
        proc = subprocess.run(
            ["wmctrl", "-lpGx"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.5,
        )
    except Exception:
        return []

    out = proc.stdout or ""
    rows = []
    for line in out.splitlines():
        # Format:
        # 0x03a00007  0  19301 0 0 960 540 google-chrome.Google-chrome host Title
        parts = line.split(None, 9)
        if len(parts) < 9:
            continue
        wid = parts[0]
        try:
            desktop = int(parts[1])
            pid = int(parts[2])
            x = int(parts[3])
            y = int(parts[4])
            w = int(parts[5])
            h = int(parts[6])
        except Exception:
            continue

        wm_class = parts[7]
        host = parts[8]
        title = parts[9] if len(parts) > 9 else ""
        rows.append({
            "id": wid,
            "desktop": desktop,
            "pid": pid,
            "x": x,
            "y": y,
            "width": w,
            "height": h,
            "wm_class": wm_class,
            "host": host,
            "title": title,
        })
    return rows


def _wmctrl_window_row_by_id(wid):
    if wid is None:
        return None
    for row in _wmctrl_list_windows():
        if _window_id_equals(row.get("id"), wid):
            return row
    return None


def _xdotool_active_chrome_window_id(expected_pids=None):
    if not shutil.which("xdotool"):
        return None

    preferred_pid_set = set()
    if isinstance(expected_pids, (list, tuple, set)):
        for raw_pid in expected_pids:
            try:
                pid = int(raw_pid)
            except Exception:
                continue
            if pid > 0:
                preferred_pid_set.add(pid)

    try:
        proc = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.2,
        )
        active_raw = str(proc.stdout or "").strip()
        active_int = int(active_raw)
        if active_int <= 0:
            return None
        active_wid = hex(active_int)
    except Exception:
        return None

    row = _wmctrl_window_row_by_id(active_wid)
    if isinstance(row, dict):
        wm_class = str(row.get("wm_class", "")).lower()
        if "chrome" not in wm_class and "chromium" not in wm_class:
            return None
        if preferred_pid_set:
            try:
                row_pid = int(row.get("pid", 0))
            except Exception:
                row_pid = 0
            if row_pid not in preferred_pid_set:
                return None
        row_id = row.get("id")
        if row_id:
            return str(row_id)

    if preferred_pid_set:
        return None

    try:
        cls_proc = subprocess.run(
            ["xdotool", "getwindowclassname", str(active_int)],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.2,
        )
        cls = str(cls_proc.stdout or "").strip().lower()
        if "chrome" not in cls and "chromium" not in cls:
            return None
    except Exception:
        return None
    return str(active_wid)


def _resolve_x11_window_id_for_rect_via_xwininfo(rect):
    if not shutil.which("xwininfo") or not isinstance(rect, dict):
        return None
    try:
        tx = int(rect.get("x", 0))
        ty = int(rect.get("y", 0))
        tw = int(rect.get("width", 0))
        th = int(rect.get("height", 0))
    except Exception:
        return None
    if tw < 120 or th < 120:
        return None

    try:
        proc = subprocess.run(
            ["xwininfo", "-root", "-tree"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.5,
        )
    except Exception:
        return None

    out = proc.stdout or ""
    # Example line prefix:
    # 0x4e00007 "Title": ("google-chrome" "Google-chrome")  960x540+0+0  +0+0
    rx = re.compile(
        r"^\s*(0x[0-9a-f]+)\s+.*?\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)\s",
        re.IGNORECASE,
    )
    best_id = None
    best_score = None
    for line in out.splitlines():
        m = rx.search(line)
        if not m:
            continue
        wid = m.group(1)
        try:
            w = int(m.group(2))
            h = int(m.group(3))
            x = int(m.group(4))
            y = int(m.group(5))
        except Exception:
            continue

        # Ignore windows far from target geometry.
        if abs(x - tx) > 180 or abs(y - ty) > 180:
            continue
        if abs(w - tw) > 260 or abs(h - th) > 260:
            continue

        score = abs(x - tx) + abs(y - ty) + abs(w - tw) + abs(h - th)
        if best_score is None or score < best_score:
            best_score = score
            best_id = wid
    return best_id


def _resolve_x11_window_id_for_rect(rect, title_hint="", preferred_pids=None):
    if not isinstance(rect, dict):
        return None
    try:
        tx = int(rect.get("x", 0))
        ty = int(rect.get("y", 0))
        tw = int(rect.get("width", 0))
        th = int(rect.get("height", 0))
    except Exception:
        return None
    if tw < 120 or th < 120:
        return None

    hint = str(title_hint or "").strip().lower()
    hint_tokens = [tok for tok in re.split(r"\s+", hint) if len(tok) >= 4][:4]
    preferred_pid_set = set()
    if isinstance(preferred_pids, (list, tuple, set)):
        for raw_pid in preferred_pids:
            try:
                pid = int(raw_pid)
            except Exception:
                continue
            if pid > 0:
                preferred_pid_set.add(pid)

    rows = list(_wmctrl_list_windows())
    if preferred_pid_set:
        pid_rows = []
        for row in rows:
            try:
                row_pid = int(row.get("pid", 0))
            except Exception:
                row_pid = 0
            if row_pid in preferred_pid_set:
                pid_rows.append(row)
        if pid_rows:
            rows = pid_rows

    best_id = None
    best_score = None
    relaxed_id = None
    relaxed_score = None
    for row in rows:
        wm_class = str(row.get("wm_class", "")).lower()
        if "chrome" not in wm_class and "chromium" not in wm_class:
            continue

        x = int(row.get("x", 0))
        y = int(row.get("y", 0))
        w = int(row.get("width", 0))
        h = int(row.get("height", 0))
        if w < 120 or h < 120:
            continue
        dx = abs(x - tx)
        dy = abs(y - ty)
        dw = abs(w - tw)
        dh = abs(h - th)

        score = dx + dy + dw + dh
        if hint:
            title = str(row.get("title", "")).lower()
            if title and hint in title:
                score -= 80
            elif title and any(tok in title for tok in hint_tokens):
                score -= 24
            else:
                score += 40

        # Strict pass: prefer windows that are near the Selenium geometry.
        if dx <= 220 and dy <= 220 and dw <= 320 and dh <= 320:
            if best_score is None or score < best_score:
                best_score = score
                best_id = row.get("id")
            continue

        # Relaxed pass: keep a fallback candidate for compositors/scaling setups
        # where reported window geometry can drift outside strict bounds.
        relaxed_penalty = max(0, dx - 220) + max(0, dy - 220) + max(0, dw - 320) + max(0, dh - 320)
        relaxed = score + (2 * relaxed_penalty)
        if relaxed_score is None or relaxed < relaxed_score:
            relaxed_score = relaxed
            relaxed_id = row.get("id")

    if best_id:
        return str(best_id)
    if relaxed_id:
        return str(relaxed_id)

    # Fallback for environments where wmctrl row geometry is not enough.
    return _resolve_x11_window_id_for_rect_via_xwininfo(rect)


def _resolve_role_window_id(role_driver, handle, attempts=10, sleep_s=0.25, expected_pids=None):
    last_rect = None
    last_title = ""
    attempts = max(1, int(attempts))
    for idx in range(attempts):
        try:
            role_driver.switch_to.window(handle)
            try:
                # Helps make the current role tab become the active tab/window title
                # before we map Selenium handle -> X11 window id.
                role_driver.execute_cdp_cmd("Page.bringToFront", {})
            except Exception:
                pass
            last_rect = role_driver.get_window_rect()
            last_title = str(role_driver.title or "").strip()
        except Exception:
            return None, last_rect, last_title

        wid = _resolve_x11_window_id_for_rect(
            last_rect,
            title_hint=last_title,
            preferred_pids=expected_pids,
        )
        if wid:
            return wid, last_rect, last_title

        active_wid = _xdotool_active_chrome_window_id(expected_pids=expected_pids)
        if active_wid:
            return active_wid, last_rect, last_title

        if idx < attempts - 1:
            time.sleep(max(0.05, float(sleep_s)))
    return None, last_rect, last_title


def _resolve_teacher_window_id(role_driver, handle, attempts=10, sleep_s=0.25, expected_pids=None):
    return _resolve_role_window_id(
        role_driver,
        handle,
        attempts=attempts,
        sleep_s=sleep_s,
        expected_pids=expected_pids,
    )


def _prime_role_window_xids(main_driver, teacher_driver=None, class_driver=None, stt_driver=None, timeout_s=6.0):
    role_to_driver = {
        "teacher": teacher_driver or main_driver,
        "stt": stt_driver or main_driver,
        "ai": main_driver,
        "class": class_driver or main_driver,
    }
    pending = []
    for role in ("stt", "ai", "class", "teacher"):
        handle = window_handles_by_role.get(role)
        role_driver = role_to_driver.get(role)
        if not handle or role_driver is None:
            continue
        pending.append(role)

    if not pending:
        return

    _flow_log(f"stage=prime_role_xids pending={pending}")
    deadline = time.time() + max(0.5, float(timeout_s))
    unresolved = set(pending)
    while unresolved and time.time() < deadline:
        for role in list(unresolved):
            handle = window_handles_by_role.get(role)
            role_driver = role_to_driver.get(role)
            if not handle or role_driver is None:
                unresolved.discard(role)
                continue
            expected_pids = _expected_pids_for_role(
                role,
                role_driver,
                main_driver,
                teacher_driver=teacher_driver,
                class_driver=class_driver,
                stt_driver=stt_driver,
            )
            wid, _rect, _title = _resolve_role_window_id(
                role_driver,
                handle,
                attempts=1,
                sleep_s=0.0,
                expected_pids=expected_pids,
            )
            if wid:
                window_xids_by_role[role] = str(wid)
                unresolved.discard(role)
                _flow_log(f"primed role={role} xid={wid}")

        if unresolved:
            time.sleep(0.10)

    if unresolved:
        _flow_log(f"prime_role_xids unresolved={sorted(unresolved)}")


def _duplicate_role_groups_by_xid(roles):
    by_xid = {}
    for role in roles:
        xid = str(window_xids_by_role.get(role) or "").strip()
        if not xid:
            continue
        by_xid.setdefault(xid, []).append(role)
    return [group for group in by_xid.values() if len(group) > 1]


def _duplicate_role_groups_by_handle(roles):
    by_handle = {}
    for role in roles:
        handle = str(window_handles_by_role.get(role) or "").strip()
        if not handle:
            continue
        by_handle.setdefault(handle, []).append(role)
    return [group for group in by_handle.values() if len(group) > 1]


def _ensure_role_handles_distinct(main_driver, roles=("stt", "ai", "class", "teacher"), max_rounds=3):
    role_order = tuple(roles)
    max_rounds = max(1, int(max_rounds))
    for round_idx in range(1, max_rounds + 1):
        duplicates = _duplicate_role_groups_by_handle(role_order)
        if not duplicates:
            if round_idx > 1:
                _flow_log(f"duplicate role handles resolved after round={round_idx - 1}")
            return True

        _flow_log(
            f"duplicate role handles detected round={round_idx}/{max_rounds} groups={duplicates}; "
            "reopening duplicate roles"
        )
        changed = False
        for group in duplicates:
            for role in group[1:]:
                url_key = ROLE_URL_KEY_BY_ROLE.get(role)
                if not url_key:
                    continue
                old_handle = window_handles_by_role.get(role)
                _open_role_page(main_driver, role, URLS[url_key], use_current_window=False)
                new_handle = window_handles_by_role.get(role)
                if old_handle and new_handle and old_handle != new_handle:
                    _best_effort_close_handle(main_driver, old_handle, preserve_handle=new_handle)
                _flow_log(
                    f"reopened role={role} old_handle={old_handle} "
                    f"new_handle={new_handle}"
                )
                _flow_breath(f"post-handle-reopen role={role}")
                changed = True
        if not changed:
            break

    leftovers = _duplicate_role_groups_by_handle(role_order)
    if leftovers:
        _flow_log(f"warning: duplicate role handles remain={leftovers}")
        return False
    _flow_log("duplicate role handles resolved")
    return True


def _ensure_staged_tab_windows_distinct(main_driver, teacher_driver=None, class_driver=None, stt_driver=None):
    tab_roles = ("stt", "ai", "class")
    max_rounds = 3
    for round_idx in range(1, max_rounds + 1):
        _prime_role_window_xids(
            main_driver,
            teacher_driver=teacher_driver,
            class_driver=class_driver,
            stt_driver=stt_driver,
            timeout_s=3.0,
        )
        duplicates = _duplicate_role_groups_by_xid(tab_roles)
        if not duplicates:
            if round_idx > 1:
                _flow_log(f"duplicate tab xids resolved after round={round_idx - 1}")
            return

        _flow_log(
            f"duplicate tab xids detected round={round_idx}/{max_rounds} groups={duplicates}; "
            "reopening duplicate roles"
        )
        changed = False
        for group in duplicates:
            for role in group[1:]:
                url_key = ROLE_URL_KEY_BY_ROLE.get(role)
                if not url_key:
                    continue
                old_handle = window_handles_by_role.get(role)
                _open_role_page(main_driver, role, URLS[url_key], use_current_window=False)
                new_handle = window_handles_by_role.get(role)
                if old_handle and new_handle and old_handle != new_handle:
                    _best_effort_close_handle(main_driver, old_handle, preserve_handle=new_handle)
                _flow_log(
                    f"reopened role={role} old_handle={old_handle} "
                    f"new_handle={new_handle}"
                )
                _flow_breath(f"post-reopen role={role}")
                changed = True
        if not changed:
            break

    _prime_role_window_xids(
        main_driver,
        teacher_driver=teacher_driver,
        class_driver=class_driver,
        stt_driver=stt_driver,
        timeout_s=4.0,
    )
    leftovers = _duplicate_role_groups_by_xid(tab_roles)
    if leftovers:
        _flow_log(f"warning: duplicate tab xids remain after reopen={leftovers}")
    else:
        _flow_log("duplicate tab xids resolved")


def _xdotool_move_window_to_workspace(wid, target_ws):
    if not shutil.which("xdotool"):
        return False, "xdotool not installed"

    wid_s = str(wid or "").strip()
    try:
        wid_num = int(wid_s, 16) if wid_s.lower().startswith("0x") else int(wid_s)
    except Exception:
        return False, f"invalid window id: {wid_s!r}"

    try:
        proc = subprocess.run(
            ["xdotool", "set_desktop_for_window", str(wid_num), str(int(target_ws))],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except Exception as e:
        return False, str(e)

    if proc.returncode == 0:
        return True, ""
    detail = (proc.stderr or proc.stdout or "").strip()
    return False, detail or "xdotool error"


def _wmctrl_move_window_to_workspace(wid, target_ws):
    if not shutil.which("wmctrl"):
        return False, "wmctrl not installed"
    try:
        proc = subprocess.run(
            ["wmctrl", "-i", "-r", str(wid), "-t", str(int(target_ws))],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except Exception as e:
        return False, str(e)

    if proc.returncode == 0:
        return True, ""
    detail = (proc.stderr or proc.stdout or "").strip()
    return False, detail or "wmctrl error"


def _wmctrl_set_window_rect(wid, x, y, w, h):
    wid_s = str(wid or "").strip()
    if not wid_s:
        return False, "missing window id"
    if not shutil.which("wmctrl"):
        return False, "wmctrl not installed"

    x = int(x)
    y = int(y)
    w = max(1, int(w))
    h = max(1, int(h))
    spec = f"0,{x},{y},{w},{h}"
    last_detail = ""

    for _attempt in range(2):
        try:
            proc = subprocess.run(
                ["wmctrl", "-i", "-r", wid_s, "-e", spec],
                capture_output=True,
                text=True,
                check=False,
                timeout=2.0,
            )
        except Exception as e:
            return False, str(e)

        if proc.returncode != 0:
            last_detail = (proc.stderr or proc.stdout or "").strip() or "wmctrl set rect failed"
            continue

        # Let Mutter apply geometry, then verify.
        time.sleep(0.04)
        row = _wmctrl_window_row_by_id(wid_s)
        if not isinstance(row, dict):
            return True, "set (verify unavailable)"

        try:
            rx = int(row.get("x", 0))
            ry = int(row.get("y", 0))
            rw = int(row.get("width", 0))
            rh = int(row.get("height", 0))
        except Exception:
            return True, "set (verify parse failed)"

        # Allow small WM decoration/border tolerances.
        if abs(rx - x) <= 12 and abs(ry - y) <= 12 and abs(rw - w) <= 24 and abs(rh - h) <= 24:
            return True, "set+verified"

        last_detail = f"verify mismatch got={rx},{ry} {rw}x{rh} want={x},{y} {w}x{h}"

    return False, last_detail or "wmctrl set rect failed"


def _xprop_window_state_atoms(wid):
    wid_s = str(wid or "").strip()
    if not wid_s or not shutil.which("xprop"):
        return None
    try:
        proc = subprocess.run(
            ["xprop", "-id", wid_s, "_NET_WM_STATE"],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.8,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    text = str(proc.stdout or "")
    return set(re.findall(r"_NET_WM_STATE_[A-Z_]+", text))


def _wmctrl_set_window_maximized(wid):
    wid_s = str(wid or "").strip()
    if not wid_s:
        return False, "missing window id"
    if not shutil.which("wmctrl"):
        return False, "wmctrl not installed"
    try:
        proc = subprocess.run(
            ["wmctrl", "-i", "-r", wid_s, "-b", "add,maximized_vert,maximized_horz"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except Exception as e:
        return False, str(e)
    if proc.returncode == 0:
        return True, ""
    detail = (proc.stderr or proc.stdout or "").strip()
    return False, detail or "wmctrl maximize failed"


def _wmctrl_restore_window_from_maximized(wid):
    wid_s = str(wid or "").strip()
    if not wid_s:
        return False, "missing window id"
    if not shutil.which("wmctrl"):
        return False, "wmctrl not installed"
    try:
        proc = subprocess.run(
            ["wmctrl", "-i", "-r", wid_s, "-b", "remove,maximized_vert,maximized_horz"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except Exception as e:
        return False, str(e)
    if proc.returncode == 0:
        return True, ""
    detail = (proc.stderr or proc.stdout or "").strip()
    return False, detail or "wmctrl restore failed"


def _window_is_maximized(wid):
    states = _xprop_window_state_atoms(wid)
    if states is None:
        return None
    wanted = {"_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ"}
    return wanted.issubset(states)


def _window_is_restored_down(wid):
    states = _xprop_window_state_atoms(wid)
    if states is None:
        return None
    if "_NET_WM_STATE_FULLSCREEN" in states:
        return False
    if "_NET_WM_STATE_HIDDEN" in states:
        return False
    wanted = {"_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ"}
    if wanted.issubset(states):
        return False
    return True


def _ensure_teacher_window_maximized(role_driver, handle, expected_pids=None):
    if role_driver is None or not handle:
        return False, "missing teacher driver/handle"

    wid = str(window_xids_by_role.get("teacher") or "").strip() or None
    if wid and _wmctrl_window_workspace_by_id(wid) is None:
        wid = None
    if not wid:
        wid, _rect, _title = _resolve_teacher_window_id(
            role_driver,
            handle,
            attempts=10,
            sleep_s=0.16,
            expected_pids=expected_pids,
        )
    if not wid:
        return False, "teacher X11 window not found"

    restored_down = _window_is_restored_down(wid)
    if restored_down is False:
        max_state = _window_is_maximized(wid)
        if max_state is True:
            window_xids_by_role["teacher"] = str(wid)
            return True, "already-maximized"
        states = _xprop_window_state_atoms(wid) or set()
        if "_NET_WM_STATE_FULLSCREEN" in states:
            window_xids_by_role["teacher"] = str(wid)
            return True, "already-fullscreen"
        window_xids_by_role["teacher"] = str(wid)
        return True, "not-restored-down (skip maximize)"

    max_state = _window_is_maximized(wid)
    if max_state is True:
        window_xids_by_role["teacher"] = str(wid)
        return True, "already-maximized"

    ok, detail = _wmctrl_set_window_maximized(wid)
    if not ok:
        return False, detail or "wmctrl maximize failed"

    # Let Mutter apply state before we verify.
    time.sleep(0.06)
    max_after = _window_is_maximized(wid)
    window_xids_by_role["teacher"] = str(wid)
    if max_after is False:
        return False, "maximize verify failed"
    if max_after is True:
        return True, "maximized"
    return True, "maximized (verification unavailable)"


def _ensure_role_window_restored_down(role, role_driver, handle, expected_pids=None):
    if role_driver is None or not handle:
        return False, "missing role driver/handle"

    wid = str(window_xids_by_role.get(role) or "").strip() or None
    if wid and _wmctrl_window_workspace_by_id(wid) is None:
        wid = None
    if not wid:
        wid, _rect, _title = _resolve_role_window_id(
            role_driver,
            handle,
            attempts=8,
            sleep_s=0.14,
            expected_pids=expected_pids,
        )
    if not wid:
        return False, f"{role} X11 window not found"

    max_state = _window_is_maximized(wid)
    if max_state is not True:
        window_xids_by_role[role] = str(wid)
        return True, "already-restored"

    ok, detail = _wmctrl_restore_window_from_maximized(wid)
    if not ok:
        return False, detail or "wmctrl restore failed"

    time.sleep(0.05)
    max_after = _window_is_maximized(wid)
    window_xids_by_role[role] = str(wid)
    if max_after is True:
        return False, "restore verify failed"
    if max_after is False:
        return True, "restored-down"
    return True, "restored-down (verification unavailable)"


def _maximize_window_via_webdriver(role_driver, handle):
    if role_driver is None or not handle:
        return False, "missing driver/handle"
    try:
        role_driver.switch_to.window(handle)
    except Exception as e:
        return False, f"switch failed: {e}"
    try:
        role_driver.maximize_window()
        return True, "webdriver-maximize"
    except Exception as e:
        return False, str(e)


def _window_id_to_int(wid):
    wid_s = str(wid or "").strip().lower()
    if not wid_s:
        return None
    try:
        if wid_s.startswith("0x"):
            return int(wid_s, 16)
        return int(wid_s)
    except Exception:
        return None


def _window_id_equals(lhs, rhs):
    li = _window_id_to_int(lhs)
    ri = _window_id_to_int(rhs)
    if li is not None and ri is not None:
        return li == ri
    return str(lhs or "").strip().lower() == str(rhs or "").strip().lower()


def _wmctrl_window_workspace_by_id(wid):
    if wid is None:
        return None
    row = _wmctrl_window_row_by_id(wid)
    if isinstance(row, dict):
        try:
            return int(row.get("desktop"))
        except Exception:
            return None
    return None


def _move_role_window_to_workspace(
    role,
    role_driver,
    handle,
    target_ws,
    expected_pids=None,
    pre_resolved_wid=None,
):
    if role_driver is None or not handle:
        return False

    wid = str(pre_resolved_wid or "").strip() or None
    rect = None
    title_hint = ""
    if wid and _wmctrl_window_workspace_by_id(wid) is None:
        _flow_log(f"role={role} cached xid={wid} not visible anymore; resolving again")
        wid = None

    if not wid:
        wid, rect, title_hint = _resolve_role_window_id(
            role_driver,
            handle,
            attempts=12,
            sleep_s=0.2,
            expected_pids=expected_pids,
        )
    if not wid:
        rect_note = ""
        if isinstance(rect, dict):
            rect_note = (
                f" rect={rect.get('x')},{rect.get('y')} "
                f"{rect.get('width')}x{rect.get('height')}"
            )
        title_note = f" title={title_hint!r}" if title_hint else ""
        print(
            f"[prepare] {role} workspace move skipped: X11 window id not found."
            f"{rect_note}{title_note}"
        )
        return False

    target_ws = int(target_ws)
    max_attempts = 3
    move_ok = False
    moved_via = "wmctrl"
    last_detail = ""
    last_seen_ws = None

    for attempt in range(1, max_attempts + 1):
        moved, detail = _wmctrl_move_window_to_workspace(wid, target_ws)
        moved_via = "wmctrl"
        if not moved:
            moved, xd_detail = _xdotool_move_window_to_workspace(wid, target_ws)
            moved_via = "xdotool"
            if not moved:
                last_detail = (
                    f"{detail or 'wmctrl error'}; xdotool fallback failed: {xd_detail}"
                )
                if attempt < max_attempts:
                    time.sleep(0.12)
                    continue
                print(f"[prepare] {role} workspace move failed: {last_detail}")
                return False

        current_ws = _wmctrl_window_workspace_by_id(wid)
        last_seen_ws = current_ws
        if current_ws is None or int(current_ws) == target_ws:
            move_ok = True
            break

        last_detail = f"{moved_via} reported success but window is on workspace {current_ws}"
        if attempt < max_attempts:
            time.sleep(0.12)

    if not move_ok:
        print(
            f"[prepare] {role} workspace move failed after {max_attempts} attempts: "
            f"{last_detail or 'unknown error'}"
        )
        return False

    verify_note = f"verified={last_seen_ws}" if last_seen_ws is not None else "verified=unavailable"
    print(
        f"[prepare] {role} moved to workspace {target_ws} "
        f"(window={wid}, via={moved_via}, {verify_note})."
    )
    window_xids_by_role[role] = str(wid)
    return True


def place_roles_on_workspace_layout(main_driver, teacher_driver=None, class_driver=None, stt_driver=None, base_workspace=None):
    """
    Workspace layout (relative to base workspace):
    - stt/ai/class: base + 1
    - teacher: base + 2
    """
    if not shutil.which("wmctrl"):
        print("[prepare] workspace layout skipped: wmctrl not installed.")
        return None

    active_ws, ws_count = _wmctrl_active_workspace()
    if base_workspace is not None:
        try:
            active_ws = int(base_workspace)
        except Exception:
            pass
    if active_ws is None:
        print("[prepare] workspace layout skipped: cannot detect base workspace.")
        return None

    tabs_ws = int(active_ws) + 1
    teacher_ws = int(active_ws) + 2
    required_count = teacher_ws + 1

    if ws_count is not None and teacher_ws >= int(ws_count):
        if _wmctrl_ensure_workspace_count(required_count):
            ws_count = required_count
        else:
            fallback_ws = max(0, int(ws_count) - 1)
            if fallback_ws <= int(active_ws):
                print(
                    "[prepare] workspace layout warning: no additional workspace available "
                    f"(base={active_ws}, total={ws_count})."
                )
                return None
            tabs_ws = min(tabs_ws, fallback_ws)
            teacher_ws = fallback_ws
            print(
                "[prepare] workspace layout warning: could not extend workspace count; "
                f"using tabs={tabs_ws}, teacher={teacher_ws}."
            )

    role_to_driver = {
        "teacher": teacher_driver or main_driver,
        "stt": stt_driver or main_driver,
        "ai": main_driver,
        "class": class_driver or main_driver,
    }

    target_ws_by_role = {
        "stt": tabs_ws,
        "ai": tabs_ws,
        "class": tabs_ws,
        "teacher": teacher_ws,
    }
    print(
        "[prepare] workspace rules: "
        f"base={active_ws}, stt/ai/class={tabs_ws}, teacher={teacher_ws}."
    )
    if STRICT_STAGED_SINGLE_PROFILE_FLOW:
        _ensure_staged_tab_windows_distinct(
            main_driver,
            teacher_driver=teacher_driver,
            class_driver=class_driver,
            stt_driver=stt_driver,
        )
        _log_role_handle_diagnostics()

    moved_roles = []
    failed_roles = []

    _flow_log(f"stage=move_tabs_to_next_workspace tabs_ws={tabs_ws}")
    for role in ("stt", "ai", "class"):
        handle = window_handles_by_role.get(role)
        role_driver = role_to_driver.get(role)
        target_ws_role = target_ws_by_role.get(role, tabs_ws)
        expected_pids = _expected_pids_for_role(
            role,
            role_driver,
            main_driver,
            teacher_driver=teacher_driver,
            class_driver=class_driver,
            stt_driver=stt_driver,
        )
        if expected_pids:
            print(f"[prepare] role={role} expected chrome pids={list(expected_pids)}")

        pre_wid = window_xids_by_role.get(role)
        ok = _move_role_window_to_workspace(
            role,
            role_driver,
            handle,
            target_ws_role,
            expected_pids=expected_pids,
            pre_resolved_wid=pre_wid,
        )
        if not ok:
            _flow_log(f"retry move role={role} without cached xid")
            ok = _move_role_window_to_workspace(
                role,
                role_driver,
                handle,
                target_ws_role,
                expected_pids=expected_pids,
                pre_resolved_wid=None,
            )
        if not ok and STRICT_STAGED_SINGLE_PROFILE_FLOW and role_driver is main_driver:
            url_key = ROLE_URL_KEY_BY_ROLE.get(role)
            if url_key:
                _flow_log(f"role={role} move failed; reopening role window and retrying move")
                old_handle = window_handles_by_role.get(role)
                _open_role_page(main_driver, role, URLS[url_key], use_current_window=False)
                new_handle = window_handles_by_role.get(role)
                if old_handle and new_handle and old_handle != new_handle:
                    _best_effort_close_handle(main_driver, old_handle, preserve_handle=new_handle)
                _flow_breath(f"post-reopen-before-move role={role}")
                _prime_role_window_xids(
                    main_driver,
                    teacher_driver=teacher_driver,
                    class_driver=class_driver,
                    stt_driver=stt_driver,
                    timeout_s=2.5,
                )
                pre_wid = window_xids_by_role.get(role)
                ok = _move_role_window_to_workspace(
                    role,
                    role_driver,
                    window_handles_by_role.get(role),
                    target_ws_role,
                    expected_pids=expected_pids,
                    pre_resolved_wid=pre_wid,
                )
                if not ok:
                    ok = _move_role_window_to_workspace(
                        role,
                        role_driver,
                        window_handles_by_role.get(role),
                        target_ws_role,
                        expected_pids=expected_pids,
                        pre_resolved_wid=None,
                    )
        if ok:
            moved_roles.append(role)
        else:
            failed_roles.append(role)

    _flow_breath("after moving 3 tabs")
    _flow_log(f"stage=move_teacher_to_workspace teacher_ws={teacher_ws}")

    teacher_role_driver = role_to_driver.get("teacher")
    teacher_expected_pids = _expected_pids_for_role(
        "teacher",
        teacher_role_driver,
        main_driver,
        teacher_driver=teacher_driver,
        class_driver=class_driver,
        stt_driver=stt_driver,
    )
    if teacher_expected_pids:
        print(f"[prepare] role=teacher expected chrome pids={list(teacher_expected_pids)}")

    teacher_pre_wid = window_xids_by_role.get("teacher")
    teacher_ok = _move_role_window_to_workspace(
        "teacher",
        teacher_role_driver,
        window_handles_by_role.get("teacher"),
        teacher_ws,
        expected_pids=teacher_expected_pids,
        pre_resolved_wid=teacher_pre_wid,
    )
    if not teacher_ok:
        _flow_log("retry move role=teacher without cached xid")
        teacher_ok = _move_role_window_to_workspace(
            "teacher",
            teacher_role_driver,
            window_handles_by_role.get("teacher"),
            teacher_ws,
            expected_pids=teacher_expected_pids,
            pre_resolved_wid=None,
        )
    if teacher_ok:
        moved_roles.append("teacher")
    else:
        failed_roles.append("teacher")

    _flow_breath("after moving teacher")

    print(
        "[prepare] workspace layout applied: "
        f"base={active_ws}, tabs_ws={tabs_ws}, teacher_ws={teacher_ws}, total_ws={ws_count}, "
        f"moved={','.join(moved_roles) or '-'}, failed={','.join(failed_roles) or '-'}"
    )
    return {
        "base_workspace": int(active_ws),
        "tabs_workspace": int(tabs_ws),
        "teacher_workspace": int(teacher_ws),
        "total_workspace_count": int(ws_count) if ws_count is not None else None,
        "moved_roles": tuple(moved_roles),
        "failed_roles": tuple(failed_roles),
    }


def move_teacher_to_next_workspace(main_driver, teacher_driver=None, base_workspace=None):
    """
    Keep normal startup, but place the teacher window on workspace (current + 1).
    Best-effort only; if wmctrl/xwininfo are unavailable we log and continue.
    """
    role_driver = teacher_driver or main_driver
    handle = window_handles_by_role.get("teacher")
    if role_driver is None or not handle:
        print("[prepare] teacher workspace move skipped: teacher handle/driver missing.")
        return

    active_ws, ws_count = _wmctrl_active_workspace()
    if base_workspace is not None:
        try:
            active_ws = int(base_workspace)
        except Exception:
            pass
    if active_ws is None:
        print("[prepare] teacher workspace move skipped: cannot detect active workspace.")
        return

    target_ws = int(active_ws) + 1
    if ws_count is not None and target_ws >= int(ws_count):
        if _wmctrl_ensure_workspace_count(target_ws + 1):
            ws_count = target_ws + 1
        else:
            target_ws = max(0, int(ws_count) - 1)

    expected_pids = ()
    if teacher_driver is not None and role_driver is teacher_driver and role_driver is not main_driver:
        expected_pids = _pids_for_debug_port(TEACHER_DEBUG_PORT)
    else:
        expected_pids = _pids_for_debug_port(DEBUG_PORT)
    _move_role_window_to_workspace(
        "teacher",
        role_driver,
        handle,
        target_ws,
        expected_pids=expected_pids,
    )


def _focus_role_handle(role, role_driver, handle):
    if role_driver is None or not handle:
        return False
    try:
        role_driver.switch_to.window(handle)
        try:
            role_driver.execute_cdp_cmd("Page.bringToFront", {})
        except Exception:
            pass
        try:
            title = str(role_driver.title or "").strip()
        except Exception:
            title = ""
        print(f"[prepare] focus role={role} handle={handle} title={title!r}")
        return True
    except Exception as e:
        print(f"[prepare] focus role={role} failed: {e}")
        return False


def _move_role_window_focus_cycle(
    role,
    role_driver,
    handle,
    target_ws,
    base_workspace=None,
    expected_pids=None,
):
    if role_driver is None or not handle:
        return False

    if base_workspace is not None:
        _wmctrl_switch_workspace(base_workspace)
        time.sleep(0.10)

    if not _focus_role_handle(role, role_driver, handle):
        return False
    time.sleep(0.10)

    wid = _xdotool_active_chrome_window_id(expected_pids=expected_pids)
    rect = None
    title_hint = ""
    if not wid:
        wid, rect, title_hint = _resolve_role_window_id(
            role_driver,
            handle,
            attempts=4,
            sleep_s=0.12,
            expected_pids=expected_pids,
        )
    if not wid:
        rect_note = ""
        if isinstance(rect, dict):
            rect_note = (
                f" rect={rect.get('x')},{rect.get('y')} "
                f"{rect.get('width')}x{rect.get('height')}"
            )
        title_note = f" title={title_hint!r}" if title_hint else ""
        print(f"[prepare] focus-cycle move skipped role={role}: no xid.{rect_note}{title_note}")
        return False

    window_xids_by_role[role] = str(wid)
    moved, detail = _wmctrl_move_window_to_workspace(wid, target_ws)
    moved_via = "wmctrl"
    if not moved:
        moved, xd_detail = _xdotool_move_window_to_workspace(wid, target_ws)
        moved_via = "xdotool"
        if not moved:
            print(
                f"[prepare] focus-cycle move failed role={role}: "
                f"{detail or 'wmctrl error'}; xdotool fallback failed: {xd_detail}"
            )
            return False

    verify_ws = _wmctrl_window_workspace_by_id(wid)
    if verify_ws is not None and int(verify_ws) != int(target_ws):
        # Retry once after switching to the target workspace.
        _wmctrl_switch_workspace(target_ws)
        time.sleep(0.08)
        moved2, detail2 = _wmctrl_move_window_to_workspace(wid, target_ws)
        moved2_via = "wmctrl"
        if not moved2:
            moved2, detail2 = _xdotool_move_window_to_workspace(wid, target_ws)
            moved2_via = "xdotool"
        verify_ws = _wmctrl_window_workspace_by_id(wid)
        if not moved2 or (verify_ws is not None and int(verify_ws) != int(target_ws)):
            print(
                f"[prepare] focus-cycle move verify failed role={role}: "
                f"window={wid}, expected_ws={target_ws}, seen_ws={verify_ws}, "
                f"detail={detail2}, via={moved2_via}"
            )
            if base_workspace is not None:
                _wmctrl_switch_workspace(base_workspace)
                time.sleep(0.08)
            return False
        moved_via = moved2_via

    print(
        f"[prepare] focus-cycle moved role={role} window={wid} "
        f"target_ws={target_ws} via={moved_via} verify={verify_ws}"
    )
    if base_workspace is not None:
        _wmctrl_switch_workspace(base_workspace)
        time.sleep(0.08)
    return True


def move_all_roles_to_next_workspace(main_driver, teacher_driver=None, class_driver=None, stt_driver=None, base_workspace=None):
    if not shutil.which("wmctrl"):
        print("[prepare] move_all_roles_to_next_workspace skipped: wmctrl not installed.")
        return None

    if base_workspace is None:
        active_ws, _ws_count = _wmctrl_active_workspace()
        base_workspace = active_ws
    target_ws = _next_workspace_from_base(base_workspace)
    if target_ws is None:
        print("[prepare] move_all_roles_to_next_workspace skipped: could not resolve target workspace.")
        return None

    role_to_driver = _role_to_driver_map(
        main_driver,
        teacher_driver=teacher_driver,
        class_driver=class_driver,
        stt_driver=stt_driver,
    )
    moved_roles = []
    failed_roles = []
    _flow_log(f"single_next_workspace_mode: moving all roles to workspace={target_ws}")
    for role in ("stt", "ai", "class", "teacher"):
        role_driver = role_to_driver.get(role)
        handle = window_handles_by_role.get(role)
        expected_pids = _expected_pids_for_role(
            role,
            role_driver,
            main_driver,
            teacher_driver=teacher_driver,
            class_driver=class_driver,
            stt_driver=stt_driver,
        )
        ok = _move_role_window_focus_cycle(
            role,
            role_driver,
            handle,
            target_ws,
            base_workspace=base_workspace,
            expected_pids=expected_pids,
        )
        if not ok and role_driver is main_driver:
            url_key = ROLE_URL_KEY_BY_ROLE.get(role)
            if url_key:
                _flow_log(f"focus-cycle retry: reopening role={role} before move")
                old_handle = window_handles_by_role.get(role)
                _open_role_page(main_driver, role, URLS[url_key], use_current_window=False)
                new_handle = window_handles_by_role.get(role)
                if old_handle and new_handle and old_handle != new_handle:
                    _best_effort_close_handle(main_driver, old_handle, preserve_handle=new_handle)
                _flow_breath(f"post-focus-cycle-reopen role={role}")
                ok = _move_role_window_focus_cycle(
                    role,
                    role_driver,
                    window_handles_by_role.get(role),
                    target_ws,
                    base_workspace=base_workspace,
                    expected_pids=expected_pids,
                )
        if ok:
            moved_roles.append(role)
        else:
            failed_roles.append(role)
        time.sleep(0.10)

    switched, detail = _wmctrl_switch_workspace(target_ws)
    if not switched:
        print(
            "[prepare] move_all_roles_to_next_workspace warning: "
            f"failed switching to target workspace {target_ws} ({detail})."
        )

    print(
        "[prepare] move_all_roles_to_next_workspace result: "
        f"base={base_workspace}, target={target_ws}, "
        f"moved={','.join(moved_roles) or '-'}, failed={','.join(failed_roles) or '-'}"
    )
    return {
        "base_workspace": int(base_workspace) if base_workspace is not None else None,
        "target_workspace": int(target_ws),
        "moved_roles": tuple(moved_roles),
        "failed_roles": tuple(failed_roles),
    }


def notify_extension_init():
    payload = {"type": "init", "source": "prepare.py", "audio_segment_seconds": AUDIO_SEGMENT_SECONDS}
    for receiver in ("ai", "teacher", "class", "stt"):
        try:
            req = urllib.request.Request(
                f"{ROUTER_URL}/send_message",
                data=json.dumps({
                    "from": "system",
                    "to": receiver,
                    "message": payload
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                resp.read()
        except Exception as e:
            print(f"Failed to notify {receiver}: {e}")


def launch_environment(base_workspace=None):
    # Reset stale role handles from previous launches so role layout routines
    # never targets closed windows.
    _reset_role_window_handles()
    startup_workspace, _ = _wmctrl_active_workspace()
    if base_workspace is not None:
        try:
            startup_workspace = int(base_workspace)
        except Exception:
            pass
    if not SIMPLE_LAUNCH_ONLY:
        _apply_static_workspace_policy(
            startup_workspace,
            target_offset=1 if SINGLE_NEXT_WORKSPACE_MODE else 2,
        )

    def _pin_launch_workspace(label):
        if SIMPLE_LAUNCH_ONLY:
            return
        if startup_workspace is None:
            return
        switched, detail = _wmctrl_switch_workspace(startup_workspace)
        if switched:
            print(f"[prepare] launch staging: switched to base workspace {startup_workspace} before {label}.")
        else:
            print(
                f"[prepare] launch staging warning: failed switching to base workspace "
                f"{startup_workspace} before {label} ({detail})."
            )

    _pin_launch_workspace("main chrome launch")
    ok = launch_chrome_with_debug(
        debug_port=DEBUG_PORT,
        user_data_dir=CHROME_USER_DATA_ROOT,
        profile_dir=PROFILE_DIR_NAME,
        kill_existing=True,
        label="main",
    )
    if not ok:
        return None

    time.sleep(CHROME_STARTUP_WAIT)
    main_driver = connect_webdriver(DEBUG_ADDR)
    _collapse_to_single_window(main_driver, label="main")

    time.sleep(0.5)
    teacher_driver = None
    class_driver = None
    stt_driver = None
    role_open_workspace = startup_workspace
    if SINGLE_NEXT_WORKSPACE_MODE:
        _flow_log(
            "single_next_workspace_mode: opening all role windows on launcher workspace "
            f"base={role_open_workspace}"
        )

    if STRICT_STAGED_SINGLE_PROFILE_FLOW:
        if TEACHER_USE_SEPARATE_PROFILE or CLASS_USE_SEPARATE_PROFILE or STT_USE_SEPARATE_PROFILE:
            _flow_log(
                "strict staged flow: forcing single-profile launch for all roles "
                "(teacher/class/stt separate-profile flags ignored)."
            )
        _open_roles_staged_in_main_profile(main_driver, base_workspace=role_open_workspace)
    else:
        open_main_pages(
            main_driver,
            include_teacher=not TEACHER_USE_SEPARATE_PROFILE,
            include_class=not CLASS_USE_SEPARATE_PROFILE,
            include_stt=not STT_USE_SEPARATE_PROFILE,
        )

    if SIMPLE_LAUNCH_ONLY:
        try:
            _prime_role_window_xids(
                main_driver,
                teacher_driver=teacher_driver,
                class_driver=class_driver,
                stt_driver=stt_driver,
                timeout_s=2.5,
            )
        except Exception as e:
            _flow_log(f"simple_launch_only xid pre-prime warning: {e}")
        notify_extension_init()
        env = LaunchedEnvironment(
            main_driver,
            teacher_driver=teacher_driver,
            class_driver=class_driver,
            stt_driver=stt_driver,
        )
        print("🚀 Environment ready — simple launch only (workspace automation disabled).")
        return env

    if SINGLE_NEXT_WORKSPACE_MODE:
        _flow_breath("after opening all 4 roles")
        _ensure_role_handles_distinct(
            main_driver,
            roles=("stt", "ai", "class", "teacher"),
            max_rounds=3,
        )
        _flow_breath("after ensuring distinct role handles")
        _prime_role_window_xids(
            main_driver,
            teacher_driver=teacher_driver,
            class_driver=class_driver,
            stt_driver=stt_driver,
            timeout_s=4.0,
        )
        _log_role_handle_diagnostics()
        move_all_roles_to_next_workspace(
            main_driver,
            teacher_driver=teacher_driver,
            class_driver=class_driver,
            stt_driver=stt_driver,
            base_workspace=startup_workspace,
        )
        _flow_breath("after moving all roles to next workspace")
        _flow_log("single_next_workspace_mode: applying saved manual role layout")
        apply_saved_role_layout(
            main_driver,
            teacher_driver=teacher_driver,
            class_driver=class_driver,
            stt_driver=stt_driver,
        )
        notify_extension_init()

        env = LaunchedEnvironment(
            main_driver,
            teacher_driver=teacher_driver,
            class_driver=class_driver,
            stt_driver=stt_driver,
        )
        print(
            "🚀 Environment ready — prepare.py done! "
            f"(single_next_workspace_mode, open_ws={role_open_workspace})"
        )
        return env

    _flow_breath("after opening all 4 roles")
    _prime_role_window_xids(
        main_driver,
        teacher_driver=teacher_driver,
        class_driver=class_driver,
        stt_driver=stt_driver,
        timeout_s=6.0,
    )
    if STRICT_STAGED_SINGLE_PROFILE_FLOW:
        _ensure_staged_tab_windows_distinct(
            main_driver,
            teacher_driver=teacher_driver,
            class_driver=class_driver,
            stt_driver=stt_driver,
        )
    _log_role_handle_diagnostics()

    layout_info = place_roles_on_workspace_layout(
        main_driver,
        teacher_driver=teacher_driver,
        class_driver=class_driver,
        stt_driver=stt_driver,
        base_workspace=startup_workspace,
    )
    tabs_workspace = None
    if isinstance(layout_info, dict):
        tabs_workspace = layout_info.get("tabs_workspace")
    arrange_windows(
        main_driver,
        teacher_driver=teacher_driver,
        class_driver=class_driver,
        stt_driver=stt_driver,
        tabs_workspace=tabs_workspace,
    )
    notify_extension_init()

    env = LaunchedEnvironment(
        main_driver,
        teacher_driver=teacher_driver,
        class_driver=class_driver,
        stt_driver=stt_driver,
    )

    if STRICT_STAGED_SINGLE_PROFILE_FLOW:
        parts = [
            "main:teacher+stt+ai+class(staged)",
            "teacher:main-staged",
            "class:main-staged",
            "stt:main-staged",
        ]
    else:
        main_roles = "ai"
        if not TEACHER_USE_SEPARATE_PROFILE or teacher_driver is None:
            main_roles = "teacher+ai"
        parts = [
            f"main:{main_roles}",
            f"teacher:{'separate' if teacher_driver is not None else 'main-fallback'}",
            f"class:{'separate' if class_driver is not None else 'main-fallback'}",
            f"stt:{'separate' if stt_driver is not None else 'main-fallback'}",
        ]
    print(f"🚀 Environment ready — prepare.py done! ({', '.join(parts)})")
    return env
