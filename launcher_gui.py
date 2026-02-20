#!/usr/bin/env python3
"""
AutoTeacher Launcher (Tkinter)

Starts/stops:
- Local router server: route.py (Flask on 127.0.0.1:5000)
- Selenium environment: prepare.launch_environment()

Includes:
- Pipeline mapper timeline (captured -> transcribed -> sent/dropped)
- Text/audio injection controls
- Audio replay + mapper export
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    from config import (
        ROUTER_HOST as _CFG_ROUTER_HOST,
        ROUTER_PORT as _CFG_ROUTER_PORT,
        TEACHER_MEDIA_AUTOSTART as _CFG_TEACHER_MEDIA_AUTOSTART,
        TEACHER_MEDIA_AUTOSTART_MAX_WAIT_S as _CFG_TEACHER_MEDIA_AUTOSTART_MAX_WAIT_S,
        TEACHER_MEDIA_AUTOSTART_REQUIRE_WINDOW_ID as _CFG_TEACHER_MEDIA_AUTOSTART_REQUIRE_WINDOW_ID,
        TEACHER_MEDIA_AUTOSTART_RETRY_INTERVAL_S as _CFG_TEACHER_MEDIA_AUTOSTART_RETRY_INTERVAL_S,
        TEACHER_MEDIA_PREWARM as _CFG_TEACHER_MEDIA_PREWARM,
        TEACHER_CAM_ENABLED as _CFG_TEACHER_CAM_ENABLED,
        TEACHER_CAM_FPS as _CFG_TEACHER_CAM_FPS,
        TEACHER_CAM_HEIGHT as _CFG_TEACHER_CAM_HEIGHT,
        TEACHER_CAM_WIDTH as _CFG_TEACHER_CAM_WIDTH,
    )
except Exception:
    _CFG_ROUTER_HOST, _CFG_ROUTER_PORT = "127.0.0.1", 5000
    _CFG_TEACHER_MEDIA_AUTOSTART = True
    _CFG_TEACHER_MEDIA_PREWARM = True
    _CFG_TEACHER_MEDIA_AUTOSTART_RETRY_INTERVAL_S = 1.5
    _CFG_TEACHER_MEDIA_AUTOSTART_MAX_WAIT_S = 90
    _CFG_TEACHER_MEDIA_AUTOSTART_REQUIRE_WINDOW_ID = True
    _CFG_TEACHER_CAM_ENABLED = False
    _CFG_TEACHER_CAM_FPS, _CFG_TEACHER_CAM_WIDTH, _CFG_TEACHER_CAM_HEIGHT = 30, 960, 540

try:
    from teacher_media_bridge import TeacherMediaBridge
except Exception:
    TeacherMediaBridge = None

ROUTER_HOST = str(_CFG_ROUTER_HOST or "127.0.0.1")
ROUTER_PORT = int(_CFG_ROUTER_PORT or 5000)
ROUTER_BASE = f"http://{ROUTER_HOST}:{ROUTER_PORT}"
TEACHER_CAM_ENABLED = bool(_CFG_TEACHER_CAM_ENABLED)
TEACHER_CAM_FPS = int(_CFG_TEACHER_CAM_FPS or 30)
TEACHER_CAM_WIDTH = int(_CFG_TEACHER_CAM_WIDTH or 960)
TEACHER_CAM_HEIGHT = int(_CFG_TEACHER_CAM_HEIGHT or 540)
TEACHER_MEDIA_AUTOSTART = bool(_CFG_TEACHER_MEDIA_AUTOSTART)
TEACHER_MEDIA_PREWARM = bool(_CFG_TEACHER_MEDIA_PREWARM)
TEACHER_MEDIA_AUTOSTART_RETRY_INTERVAL_S = max(0.2, float(_CFG_TEACHER_MEDIA_AUTOSTART_RETRY_INTERVAL_S or 1.5))
TEACHER_MEDIA_AUTOSTART_MAX_WAIT_S = max(3.0, float(_CFG_TEACHER_MEDIA_AUTOSTART_MAX_WAIT_S or 90))
TEACHER_MEDIA_AUTOSTART_REQUIRE_WINDOW_ID = bool(_CFG_TEACHER_MEDIA_AUTOSTART_REQUIRE_WINDOW_ID)
# EWMH desktop index (0-based). `1` means the second workspace.
LAUNCHER_START_WORKSPACE = 1
# Keep at least this many workspaces on every launch.
# Preserve at least 5 workspaces as requested.
MIN_WORKSPACE_FLOOR = 5
# Before Selenium launch, move focus here so tabs open on this workspace.
FINAL_FOCUS_WORKSPACE = 2
# Buffer after successful workspace focus changes.
WORKSPACE_SWITCH_BUFFER_SECONDS = 0.08
LAUNCHER_TICK_INTERVAL_MS = 1200
PIPELINE_POLL_INTERVAL_MS = 2000
PIPELINE_STATUS_REFRESH_EVERY = 4
TEACHER_STATUS_POLL_INTERVAL_S = 2.5


def is_tcp_port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _pids_listening_on_port(port: int):
    pids = set()
    checks = [
        ["lsof", "-t", "-i", f"TCP:{int(port)}", "-sTCP:LISTEN"],
        ["fuser", "-n", "tcp", str(int(port))],
    ]
    for cmd in checks:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except Exception:
            continue
        out = " ".join(
            part for part in ((proc.stdout or ""), (proc.stderr or "")) if isinstance(part, str)
        )
        for token in out.replace("/", " ").replace(":", " ").split():
            if token.isdigit():
                pids.add(int(token))
    return sorted(pids)


def _terminate_port_listener(port: int, timeout_s: float = 4.0) -> bool:
    pids = _pids_listening_on_port(port)
    if not pids:
        return True
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    deadline = time.time() + max(0.2, float(timeout_s))
    while time.time() < deadline:
        if not is_tcp_port_open("127.0.0.1", int(port)):
            return True
        time.sleep(0.12)
    pids = _pids_listening_on_port(port)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    time.sleep(0.2)
    return not is_tcp_port_open("127.0.0.1", int(port))


def _gsettings_get(schema: str, key: str):
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
    val = str(proc.stdout or "").strip()
    return val if val else None


def _gsettings_set(schema: str, key: str, value_literal: str):
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
    except Exception as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, ""
    detail = (proc.stderr or proc.stdout or "").strip()
    return False, detail or "gsettings set failed"


def _ensure_static_workspace_floor(min_count: int):
    try:
        min_count = int(min_count)
    except Exception:
        return False, None, "invalid min_count"
    if min_count <= 0:
        return False, None, "invalid min_count"

    desktop = str(os.environ.get("XDG_CURRENT_DESKTOP", "")).lower()
    if "gnome" not in desktop:
        count = _observed_workspace_count()
        ok = count is not None and int(count) >= min_count
        return ok, count, "non-gnome desktop"

    dynamic_schema = "org.gnome.mutter"
    dynamic_key = "dynamic-workspaces"
    num_schema = "org.gnome.desktop.wm.preferences"
    num_key = "num-workspaces"

    dynamic_raw = _gsettings_get(dynamic_schema, dynamic_key)
    num_raw = _gsettings_get(num_schema, num_key)

    if dynamic_raw == "true":
        _gsettings_set(dynamic_schema, dynamic_key, "false")

    desired_num = min_count
    try:
        current_num = int(str(num_raw or "").strip())
        if current_num > desired_num:
            desired_num = current_num
    except Exception:
        pass
    _gsettings_set(num_schema, num_key, str(int(desired_num)))

    # Nudge EWMH and verify observed count.
    _wmctrl_ensure_workspace_count(min_count)
    deadline = time.time() + 2.0
    while time.time() < deadline:
        count = _observed_workspace_count()
        if count is not None and int(count) >= min_count:
            return True, int(count), ""
        time.sleep(0.08)
    count = _observed_workspace_count()
    return False, count, "workspace floor not reached"


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


def _wmctrl_ensure_workspace_count(min_count: int) -> bool:
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
    if proc.returncode != 0:
        return False

    deadline = time.time() + 1.2
    while time.time() < deadline:
        count_after = _observed_workspace_count()
        if count_after is not None and int(count_after) >= min_count:
            return True
        time.sleep(0.08)
    return False


def _wmctrl_window_workspace_by_title(title_substring: str):
    if not shutil.which("wmctrl"):
        return None
    target = str(title_substring or "").strip().lower()
    if not target:
        return None
    try:
        proc = subprocess.run(
            ["wmctrl", "-l"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except Exception:
        return None

    for line in (proc.stdout or "").splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            ws = int(parts[1])
        except Exception:
            continue
        title = str(parts[3] or "").strip().lower()
        if target in title:
            return ws
    return None


def _wmctrl_move_window_by_title_to_workspace(title_substring: str, target_ws: int):
    if not shutil.which("wmctrl"):
        return False, "wmctrl not installed"
    target = str(title_substring or "").strip()
    if not target:
        return False, "empty title"
    try:
        proc = subprocess.run(
            ["wmctrl", "-r", target, "-t", str(int(target_ws))],
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
    return False, detail or "wmctrl move failed"


def _wmctrl_switch_workspace(target_ws: int):
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


def _now_hms() -> str:
    return time.strftime("%H:%M:%S")


def _compact_text(value, max_len: int = 220) -> str:
    try:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        else:
            text = str(value)
    except Exception:
        text = str(value)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _format_route_log_line(raw_line: str):
    prefix = "[route_log] "
    if not isinstance(raw_line, str) or not raw_line.startswith(prefix):
        return None

    try:
        payload = json.loads(raw_line[len(prefix):])
    except Exception:
        return None

    event = str(payload.get("event") or "event")
    level = str(payload.get("level") or "info").upper()
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        data = {"value": data}

    if event == "client_log_entry":
        entry = data.get("entry")
        if isinstance(entry, dict):
            entry_event = entry.get("event") or "event"
            role = entry.get("role") or "unknown"
            entry_level = str(entry.get("level") or "info").upper()
            entry_data = entry.get("data") or {}
            return entry_level, f"{role}::{entry_event} {_compact_text(entry_data)}"
        return level, f"{event} {_compact_text(data)}"

    if event == "send_message":
        return level, (
            f"router send {data.get('from')} -> {data.get('to')} "
            f"kind={data.get('kind')} id={data.get('message_id')} "
            f"text_len={data.get('text_len')}"
        )

    if event == "lesson_package_expanded":
        return level, (
            f"lesson package expanded book={data.get('book_type')} "
            f"package={data.get('package_id')} text_len={data.get('text_len')}"
        )

    if event == "enqueue":
        return level, (
            f"enqueue to={data.get('to')} from={data.get('from')} "
            f"kind={data.get('kind')} queue={data.get('queue_len')}"
        )

    if event == "audio_bridge_ready":
        return level, (
            f"audio bridge ready sink={data.get('sink_name')} "
            f"source={data.get('source_name')}"
        )

    if event == "audio_segment_captured":
        return level, (
            f"captured segment={data.get('segment_id')} "
            f"run={data.get('flow_run_id')} audio={data.get('audio_ref')}"
        )

    if event in ("stt_segment_finalized", "student_response_sent", "student_response_dropped_noise"):
        return level, (
            f"{event} segment={data.get('segment_id')} run={data.get('flow_run_id')} "
            f"text_len={data.get('text_len')}"
        )

    if event in ("injection_audio_played", "injection_text_sent"):
        return level, f"{event} {_compact_text(data)}"

    if event == "walkie_info":
        tls_state = "ready" if data.get("tls_ready") else "not-ready"
        return level, (
            f"walkie info mode={data.get('class_walkie_mode')} tls={tls_state} "
            f"receiver={data.get('receiver_local_url')} tx={data.get('transmitter_lan_url')}"
        )

    if event in ("walkie_session_created", "walkie_session_joined", "walkie_session_closed", "walkie_session_expired"):
        return level, (
            f"{event} session={data.get('session_id')} pair={data.get('pair_code')} "
            f"run={data.get('flow_run_id')}"
        )

    if event in ("walkie_signal_offer", "walkie_signal_answer", "walkie_ptt_state", "walkie_signal_rejected"):
        return level, f"{event} {_compact_text(data)}"

    if event == "get_messages":
        return level, f"dequeue receiver={data.get('receiver')} count={data.get('count')}"

    if level in ("WARN", "ERROR"):
        return level, f"{event} {_compact_text(data)}"

    return None


@dataclass
class ServerState:
    proc: subprocess.Popen | None = None
    reader_thread: threading.Thread | None = None


def _safe_terminate_process(proc: subprocess.Popen, timeout_s: float = 3.0) -> bool:
    try:
        if proc.poll() is not None:
            return True
    except Exception:
        return True

    try:
        proc.terminate()
    except Exception:
        pass

    deadline = time.time() + max(0.1, float(timeout_s))
    while time.time() < deadline:
        try:
            if proc.poll() is not None:
                return True
        except Exception:
            return True
        time.sleep(0.1)

    try:
        proc.kill()
    except Exception:
        pass

    try:
        return proc.poll() is not None
    except Exception:
        return True


def _run_with_timeout(fn, timeout_s: float) -> bool:
    done = {"ok": False}

    def runner():
        try:
            fn()
        finally:
            done["ok"] = True

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout=max(0.0, float(timeout_s)))
    return done["ok"]


def main() -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, ttk
    except Exception as exc:
        print("Tkinter is not available in this Python environment.")
        print("Install it, e.g. on Ubuntu/Debian: sudo apt-get install python3-tk")
        print(f"Import error: {exc}")
        return 1

    class App:
        def __init__(self, root: tk.Tk):
            self.root = root
            self.root.title("AutoTeacher Launcher")
            self.root.geometry("1280x860")

            self.server = ServerState()
            self.driver = None
            self._launcher_workspace = None
            self._launcher_title_hint = "AutoTeacher Launcher"
            self._launcher_workspace_ready = False

            self.server_status = tk.StringVar(value="stopped")
            self.selenium_status = tk.StringVar(value="stopped")
            self.port_status = tk.StringVar(value="unknown")
            self.pipeline_status = tk.StringVar(value="bridge=unknown")
            self.teacher_media_status = tk.StringVar(value="disabled")
            self.inject_run_id = tk.StringVar(value=self._next_auto_log_run_id())
            self.inject_text = tk.StringVar(value="")
            self.inject_audio_path = tk.StringVar(value="")

            self._teacher_bridge = None
            self._teacher_bridge_last_status = {}
            self._teacher_status_poll_ts = 0.0
            self._pipeline_poll_seq = 0
            self._teacher_media_op_lock = threading.Lock()
            self._teacher_autostart_cancel = threading.Event()
            self._teacher_autostart_thread = None
            self._teacher_autostart_active = False
            self._init_teacher_bridge()

            self._mapper_rows_by_segment = {}
            self._mapper_records_by_segment = {}
            self._mapper_hydrated = False

            self._build_ui(ttk, filedialog)
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)
            self.root.after(700, self._place_launcher_on_next_workspace_once)

            atexit.register(self._atexit_cleanup)
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    signal.signal(sig, self._signal_handler)
                except Exception:
                    pass

            self._tick()
            self._poll_pipeline()

        def _next_auto_log_run_id(self) -> str:
            max_idx = 0
            rx = re.compile(r"^log(\d+)(?:[\.-]|$)", re.IGNORECASE)
            try:
                for name in os.listdir(os.path.join(BASE_DIR, "logs")):
                    m = rx.match(str(name or ""))
                    if not m:
                        continue
                    try:
                        idx = int(m.group(1))
                    except Exception:
                        continue
                    if idx > max_idx:
                        max_idx = idx
            except Exception:
                pass
            return f"log{max_idx + 1}"

        def _rotate_auto_run_id(self):
            rid = self._next_auto_log_run_id()
            self.inject_run_id.set(rid)
            self.log(f"Run ID set automatically: {rid}")

        def _build_ui(self, ttk, filedialog):
            self._filedialog = filedialog

            frm = ttk.Frame(self.root, padding=12)
            frm.pack(fill="both", expand=True)

            header = ttk.Label(frm, text="AutoTeacher", font=("Arial", 16, "bold"))
            header.pack(anchor="w")

            status_row = ttk.Frame(frm)
            status_row.pack(fill="x", pady=(10, 8))

            ttk.Label(status_row, text="Router:").grid(row=0, column=0, sticky="w")
            ttk.Label(status_row, textvariable=self.server_status).grid(row=0, column=1, sticky="w", padx=(8, 24))

            ttk.Label(status_row, text="Port 5000:").grid(row=0, column=2, sticky="w")
            ttk.Label(status_row, textvariable=self.port_status).grid(row=0, column=3, sticky="w", padx=(8, 24))

            ttk.Label(status_row, text="Selenium:").grid(row=0, column=4, sticky="w")
            ttk.Label(status_row, textvariable=self.selenium_status).grid(row=0, column=5, sticky="w", padx=(8, 24))

            ttk.Label(status_row, text="Pipeline:").grid(row=0, column=6, sticky="w")
            ttk.Label(status_row, textvariable=self.pipeline_status).grid(row=0, column=7, sticky="w", padx=(8, 0))

            ttk.Label(status_row, text="Teacher Media:").grid(row=1, column=0, sticky="w", pady=(6, 0))
            ttk.Label(status_row, textvariable=self.teacher_media_status).grid(
                row=1, column=1, columnspan=7, sticky="w", padx=(8, 0), pady=(6, 0)
            )

            btn_row = ttk.Frame(frm)
            btn_row.pack(fill="x", pady=(0, 10))

            ttk.Button(btn_row, text="Start All", command=self.start_all).pack(side="left")
            ttk.Button(btn_row, text="Stop All", command=self.stop_all).pack(side="left", padx=(8, 0))

            ttk.Separator(btn_row, orient="vertical").pack(side="left", fill="y", padx=10)

            ttk.Button(btn_row, text="Start Router", command=self.start_router).pack(side="left")
            ttk.Button(btn_row, text="Stop Router", command=self.stop_router).pack(side="left", padx=(8, 0))

            ttk.Separator(btn_row, orient="vertical").pack(side="left", fill="y", padx=10)

            ttk.Button(btn_row, text="Start Selenium", command=self.start_selenium).pack(side="left")
            ttk.Button(btn_row, text="Stop Selenium", command=self.stop_selenium).pack(side="left", padx=(8, 0))

            ttk.Separator(btn_row, orient="vertical").pack(side="left", fill="y", padx=10)

            ttk.Button(btn_row, text="Start Teacher Media", command=self.start_teacher_media).pack(side="left")
            ttk.Button(btn_row, text="Stop Teacher Media", command=self.stop_teacher_media).pack(side="left", padx=(8, 0))

            mapper_controls = ttk.LabelFrame(frm, text="Pipeline Mapper / Injection", padding=10)
            mapper_controls.pack(fill="x", pady=(0, 8))

            ttk.Label(mapper_controls, text="Run ID (auto)").grid(row=0, column=0, sticky="w")
            ttk.Label(mapper_controls, textvariable=self.inject_run_id).grid(row=0, column=1, sticky="w", padx=(6, 12))

            ttk.Label(mapper_controls, text="Inject Text").grid(row=0, column=2, sticky="w")
            ttk.Entry(mapper_controls, width=48, textvariable=self.inject_text).grid(row=0, column=3, sticky="we", padx=(6, 6))
            ttk.Button(mapper_controls, text="Inject Text", command=self.inject_student_text).grid(row=0, column=4, sticky="w")

            ttk.Label(mapper_controls, text="Audio WAV").grid(row=1, column=0, sticky="w", pady=(8, 0))
            ttk.Entry(mapper_controls, width=64, textvariable=self.inject_audio_path).grid(row=1, column=1, columnspan=3, sticky="we", padx=(6, 6), pady=(8, 0))
            ttk.Button(mapper_controls, text="Browse", command=self.browse_audio).grid(row=1, column=4, sticky="w", pady=(8, 0))
            ttk.Button(mapper_controls, text="Inject Audio", command=self.inject_student_audio).grid(row=1, column=5, sticky="w", padx=(8, 0), pady=(8, 0))
            ttk.Button(mapper_controls, text="Replay Selected", command=self.replay_selected_audio).grid(row=1, column=6, sticky="w", padx=(8, 0), pady=(8, 0))
            ttk.Button(mapper_controls, text="Export Mapper JSON", command=self.export_mapper_json).grid(row=1, column=7, sticky="w", padx=(8, 0), pady=(8, 0))

            mapper_controls.grid_columnconfigure(3, weight=1)

            mapper_frame = ttk.Frame(frm)
            mapper_frame.pack(fill="both", expand=True, pady=(0, 8))

            cols = ("time", "run_id", "segment_id", "audio_file", "transcript", "sent_status")
            self.mapper_tree = ttk.Treeview(mapper_frame, columns=cols, show="headings", height=14)
            self.mapper_tree.heading("time", text="Time")
            self.mapper_tree.heading("run_id", text="Run ID")
            self.mapper_tree.heading("segment_id", text="Segment ID")
            self.mapper_tree.heading("audio_file", text="Audio File")
            self.mapper_tree.heading("transcript", text="Transcript")
            self.mapper_tree.heading("sent_status", text="Status")
            self.mapper_tree.column("time", width=86, stretch=False)
            self.mapper_tree.column("run_id", width=130, stretch=False)
            self.mapper_tree.column("segment_id", width=220, stretch=False)
            self.mapper_tree.column("audio_file", width=300, stretch=False)
            self.mapper_tree.column("transcript", width=400, stretch=True)
            self.mapper_tree.column("sent_status", width=110, stretch=False)
            self.mapper_tree.pack(side="left", fill="both", expand=True)

            mapper_scroll = ttk.Scrollbar(mapper_frame, command=self.mapper_tree.yview)
            mapper_scroll.pack(side="right", fill="y")
            self.mapper_tree["yscrollcommand"] = mapper_scroll.set

            log_frame = ttk.LabelFrame(frm, text="Launcher / Router Log", padding=6)
            log_frame.pack(fill="both", expand=True)

            log_controls = ttk.Frame(log_frame)
            log_controls.pack(fill="x", pady=(0, 6))
            ttk.Button(log_controls, text="Copy Selected", command=self.copy_log_selected).pack(side="left")
            ttk.Button(log_controls, text="Copy All", command=self.copy_log_all).pack(side="left", padx=(8, 0))

            log_body = ttk.Frame(log_frame)
            log_body.pack(fill="both", expand=True)

            self.log_text = tk.Text(log_body, height=12, wrap="word")
            self.log_text.pack(side="left", fill="both", expand=True)
            self.log_text.configure(state="disabled")

            self._log_menu = tk.Menu(self.root, tearoff=0)
            self._log_menu.add_command(label="Copy selected", command=self.copy_log_selected)
            self._log_menu.add_command(label="Copy all", command=self.copy_log_all)

            self.log_text.bind("<Button-3>", self._show_log_menu)
            self.log_text.bind("<Control-c>", self._on_log_copy)
            self.log_text.bind("<Control-C>", self._on_log_copy)
            self.log_text.bind("<Control-a>", self._on_log_select_all)
            self.log_text.bind("<Control-A>", self._on_log_select_all)

            scroll = ttk.Scrollbar(log_body, command=self.log_text.yview)
            scroll.pack(side="right", fill="y")
            self.log_text["yscrollcommand"] = scroll.set

            self.log("Ready.")

        def log(self, msg: str, level: str = "INFO"):
            line = f"{_now_hms()} [{level}] {msg}\n"
            print(line, end="")

            def append():
                try:
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", line)
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                except Exception:
                    pass

            try:
                self.root.after(0, append)
            except Exception:
                pass

        def _clipboard_set(self, text: str):
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
                self.root.update_idletasks()
                return True
            except Exception as exc:
                self.log(f"Clipboard copy failed: {exc}", "ERROR")
                return False

        def _get_log_all_text(self) -> str:
            try:
                return str(self.log_text.get("1.0", "end-1c"))
            except Exception:
                return ""

        def copy_log_selected(self):
            text = ""
            try:
                text = str(self.log_text.get("sel.first", "sel.last"))
            except Exception:
                text = ""
            if not text:
                text = self._get_log_all_text()
            if not text:
                return
            self._clipboard_set(text)

        def copy_log_all(self):
            text = self._get_log_all_text()
            if not text:
                return
            self._clipboard_set(text)

        def _on_log_copy(self, _event=None):
            self.copy_log_selected()
            return "break"

        def _on_log_select_all(self, _event=None):
            try:
                self.log_text.tag_add("sel", "1.0", "end-1c")
                self.log_text.mark_set("insert", "1.0")
                self.log_text.see("insert")
            except Exception:
                pass
            return "break"

        def _show_log_menu(self, event):
            try:
                self._log_menu.tk_popup(event.x_root, event.y_root)
            finally:
                try:
                    self._log_menu.grab_release()
                except Exception:
                    pass
            return "break"

        def _workspace_base_for_selenium(self):
            ws = _wmctrl_window_workspace_by_title(self._launcher_title_hint)
            if ws is not None:
                self._launcher_workspace = ws
                return ws
            if self._launcher_workspace is not None:
                return self._launcher_workspace
            active_ws, _ = _wmctrl_active_workspace()
            return active_ws

        def _place_launcher_on_next_workspace_once(self):
            if self._launcher_workspace_ready:
                return
            ok_floor, count_floor, detail_floor = _ensure_static_workspace_floor(MIN_WORKSPACE_FLOOR)
            if ok_floor:
                self.log(
                    f"Workspace preflight: static floor={MIN_WORKSPACE_FLOOR} ready (count={count_floor})."
                )
            else:
                self.log(
                    f"Workspace preflight warning: floor={MIN_WORKSPACE_FLOOR} "
                    f"not confirmed (count={count_floor}, detail={detail_floor}).",
                    "WARN",
                )

            active_ws, ws_count = _wmctrl_active_workspace()
            if active_ws is None:
                self.log("Workspace setup skipped: wmctrl workspace info unavailable.", "WARN")
                return

            target_ws = int(LAUNCHER_START_WORKSPACE)
            if ws_count is not None and target_ws >= int(ws_count):
                if _wmctrl_ensure_workspace_count(target_ws + 1):
                    ws_count = target_ws + 1
                else:
                    fallback_ws = max(0, int(ws_count) - 1)
                    if fallback_ws <= int(active_ws):
                        self._launcher_workspace = int(active_ws)
                        self.log(
                            "Workspace setup warning: no next workspace available "
                            f"(active={active_ws}, total={ws_count}).",
                            "WARN",
                        )
                        self.log("Workspace setup will retry at Selenium start.", "WARN")
                        return
                    target_ws = fallback_ws

            try:
                self.root.update_idletasks()
            except Exception:
                pass

            moved, move_detail = _wmctrl_move_window_by_title_to_workspace(self._launcher_title_hint, target_ws)
            switched, switch_detail = _wmctrl_switch_workspace(target_ws)
            detected_ws = _wmctrl_window_workspace_by_title(self._launcher_title_hint)
            if detected_ws is not None:
                self._launcher_workspace = detected_ws
            else:
                self._launcher_workspace = target_ws if moved else int(active_ws)

            if moved:
                self.log(
                    "Workspace setup: launcher moved "
                    f"from {active_ws} to {self._launcher_workspace}."
                )
            else:
                self.log(
                    "Workspace setup warning: launcher move failed "
                    f"(target={target_ws}, detail={move_detail}).",
                    "WARN",
                )

            if not switched:
                self.log(
                    f"Workspace setup warning: failed switching to workspace {target_ws} ({switch_detail}).",
                    "WARN",
                )

            if detected_ws is not None:
                self._launcher_workspace_ready = int(detected_ws) == int(target_ws)
            else:
                self._launcher_workspace_ready = bool(moved)
            if not self._launcher_workspace_ready:
                self.log("Workspace setup incomplete; will retry at Selenium start.", "WARN")

        def _init_teacher_bridge(self):
            if not TEACHER_CAM_ENABLED:
                self.teacher_media_status.set("disabled (TEACHER_CAM_ENABLED=False)")
                return
            if TeacherMediaBridge is None:
                self.teacher_media_status.set("unavailable (import error)")
                self.log("Teacher media bridge import failed. Check teacher_media_bridge.py dependencies.", "ERROR")
                return
            try:
                self._teacher_bridge = TeacherMediaBridge()
                self.teacher_media_status.set("stopped")
                self.log("Teacher media bridge initialized (manual Start/Stop).")
            except Exception as exc:
                self._teacher_bridge = None
                self.teacher_media_status.set("init_error")
                self.log(f"Teacher media bridge init failed: {exc}", "ERROR")

        def _cancel_teacher_autostart(self, reason: str = "unspecified"):
            try:
                self._teacher_autostart_cancel.set()
            except Exception:
                pass
            self._teacher_autostart_active = False

            t = self._teacher_autostart_thread
            if t is not None and t.is_alive():
                self.log(f"teacher_media_autostart_cancel reason={reason}")

        def _prewarm_teacher_media_blocking(self, source: str = "unknown") -> bool:
            if not TEACHER_MEDIA_PREWARM:
                return False
            self.log(f"teacher_media_prewarm_start source={source}")

            if not TEACHER_CAM_ENABLED:
                self.log("teacher_media_prewarm_failed reason=teacher_cam_disabled", "WARN")
                return False
            if self._teacher_bridge is None:
                self.log("teacher_media_prewarm_failed reason=bridge_unavailable", "WARN")
                return False

            try:
                with self._teacher_media_op_lock:
                    status = self._teacher_bridge.ensure_ready()
            except Exception as exc:
                self.log(f"teacher_media_prewarm_failed reason=exception error={exc}", "WARN")
                return False

            status = status if isinstance(status, dict) else {}
            self._teacher_bridge_last_status = status
            self.teacher_media_status.set(self._teacher_status_text(status))

            if status.get("ready"):
                self.log(
                    "teacher_media_prewarm_ready "
                    f"dev={status.get('cam_device')} sink={status.get('sink_name')} source={status.get('source_name')}"
                )
                return True

            err = status.get("last_error") or "not_ready"
            self.log(f"teacher_media_prewarm_failed reason={err}", "WARN")
            return False

        def _start_teacher_autostart_worker(self, source: str = "unknown"):
            if not TEACHER_MEDIA_AUTOSTART:
                return
            self._cancel_teacher_autostart(reason="restart")
            self._teacher_autostart_cancel = threading.Event()
            self._teacher_autostart_active = True

            def runner():
                try:
                    self._auto_start_teacher_media_blocking(source=source)
                finally:
                    self._teacher_autostart_active = False

            t = threading.Thread(
                target=runner,
                daemon=True,
                name="teacher_media_autostart",
            )
            self._teacher_autostart_thread = t
            t.start()

        def _auto_start_teacher_media_blocking(self, source: str = "unknown"):
            if not TEACHER_CAM_ENABLED:
                return
            if self._teacher_bridge is None:
                self.log("teacher_media_autostart_failed_timeout reason=bridge_unavailable attempts=0", "WARN")
                return

            started = time.time()
            attempts = 0
            last_error = None
            timeout_s = float(TEACHER_MEDIA_AUTOSTART_MAX_WAIT_S)
            require_window_id = bool(TEACHER_MEDIA_AUTOSTART_REQUIRE_WINDOW_ID)
            window_id_grace_s = min(max(8.0, timeout_s * 0.28), 24.0)
            if require_window_id:
                has_xwininfo = bool(shutil.which("xwininfo"))
                has_wmctrl = bool(shutil.which("wmctrl"))
                if not (has_xwininfo and has_wmctrl):
                    require_window_id = False
                    self.log(
                        "teacher_media_autostart_retry "
                        f"attempt=0 reason=window_id_requirement_relaxed xwininfo={has_xwininfo} wmctrl={has_wmctrl}",
                        "WARN",
                    )

            while (time.time() - started) < timeout_s:
                if self._teacher_autostart_cancel.is_set():
                    return

                attempts += 1
                capture_rect = self._teacher_capture_rect(allow_focus_switch=False)
                window_id = capture_rect.get("window_id") if isinstance(capture_rect, dict) else None
                self.log(
                    "teacher_media_autostart_attempt "
                    f"source={source} attempt={attempts} window_id={window_id or '-'}"
                )

                if require_window_id and not window_id:
                    elapsed_s = time.time() - started
                    if elapsed_s >= window_id_grace_s:
                        require_window_id = False
                        self.log(
                            "teacher_media_autostart_retry "
                            f"attempt={attempts} reason=window_id_grace_elapsed elapsed_s={elapsed_s:.1f}",
                            "WARN",
                        )
                        # Continue this same attempt using safe fallback capture.
                    else:
                        last_error = "missing_window_id"
                        self.log(
                            "teacher_media_autostart_retry "
                            f"attempt={attempts} reason={last_error} wait_s={TEACHER_MEDIA_AUTOSTART_RETRY_INTERVAL_S}",
                            "WARN",
                        )
                        if self._teacher_autostart_cancel.wait(TEACHER_MEDIA_AUTOSTART_RETRY_INTERVAL_S):
                            return
                        continue

                ok = self._start_teacher_media_blocking(source="autostart", capture_rect=capture_rect)
                if ok:
                    self.log(
                        "teacher_media_autostart_ready "
                        f"source={source} attempts={attempts}"
                    )
                    return

                status = self._teacher_bridge_last_status if isinstance(self._teacher_bridge_last_status, dict) else {}
                last_error = status.get("last_error") or "start_failed"
                self.log(
                    "teacher_media_autostart_retry "
                    f"attempt={attempts} reason={last_error} wait_s={TEACHER_MEDIA_AUTOSTART_RETRY_INTERVAL_S}",
                    "WARN",
                )
                if self._teacher_autostart_cancel.wait(TEACHER_MEDIA_AUTOSTART_RETRY_INTERVAL_S):
                    return

            self.log(
                "teacher_media_autostart_failed_timeout "
                f"source={source} attempts={attempts} last_error={last_error or 'unknown'}",
                "WARN",
            )

        def _teacher_status_text(self, status: dict) -> str:
            if not isinstance(status, dict):
                return "unknown"
            running = bool(status.get("running"))
            cam_state = "running" if running else "stopped"
            if status.get("last_error"):
                cam_state = "error" if not running else cam_state
            mic_state = "ready" if (status.get("sink_exists") and status.get("source_exists")) else "not_ready"
            rect = status.get("capture_rect") or {}
            try:
                rect_text = f"{int(rect.get('x', 0))},{int(rect.get('y', 0))} {int(rect.get('width', 0))}x{int(rect.get('height', 0))}"
            except Exception:
                rect_text = f"0,0 {TEACHER_CAM_WIDTH}x{TEACHER_CAM_HEIGHT}"
            text = (
                f"cam={cam_state} mic={mic_state} "
                f"dev={status.get('cam_device')} "
                f"fps={status.get('fps')} rect={rect_text}"
            )
            if status.get("last_error"):
                text += f" err={status.get('last_error')}"
            return text

        def _poll_teacher_bridge_status(self, force=False):
            if self._teacher_bridge is None:
                return
            now = time.time()
            if not force and now - self._teacher_status_poll_ts < TEACHER_STATUS_POLL_INTERVAL_S:
                return
            self._teacher_status_poll_ts = now
            try:
                status = self._teacher_bridge.status()
                self._teacher_bridge_last_status = status
                self.teacher_media_status.set(self._teacher_status_text(status))
            except Exception as exc:
                self.teacher_media_status.set(f"error: {exc}")

        def _router_json(self, method: str, path: str, payload=None, timeout_s: float = 2.0):
            url = f"{ROUTER_BASE}{path}"
            data = None
            headers = {}
            if payload is not None:
                data = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url=url, data=data, method=method.upper(), headers=headers)
            with urllib.request.urlopen(req, timeout=max(0.2, float(timeout_s))) as resp:
                raw = resp.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def _router_supports_walkie(self) -> bool:
            try:
                info = self._router_json("GET", "/walkie/api/info", timeout_s=1.0)
                return isinstance(info, dict) and isinstance(info.get("walkie"), dict)
            except Exception:
                return False

        def _format_ts(self, ts_ms):
            try:
                sec = float(ts_ms) / 1000.0
                return time.strftime("%H:%M:%S", time.localtime(sec))
            except Exception:
                return _now_hms()

        def _upsert_mapper_segment(self, segment_id: str, **updates):
            if not segment_id:
                return
            sid = str(segment_id)
            rec = self._mapper_records_by_segment.get(sid)
            if rec is None:
                rec = {
                    "segment_id": sid,
                    "time": _now_hms(),
                    "ts": int(time.time() * 1000),
                    "run_id": "",
                    "audio_file": "",
                    "transcript": "",
                    "sent_status": "created",
                }
                self._mapper_records_by_segment[sid] = rec
            for k, v in updates.items():
                if v is not None:
                    rec[k] = v
            if updates.get("ts") is not None:
                rec["time"] = self._format_ts(rec.get("ts"))

            values = (
                rec.get("time") or "",
                rec.get("run_id") or "",
                rec.get("segment_id") or "",
                rec.get("audio_file") or "",
                rec.get("transcript") or "",
                rec.get("sent_status") or "",
            )
            row_id = self._mapper_rows_by_segment.get(sid)
            if row_id:
                try:
                    self.mapper_tree.item(row_id, values=values)
                except Exception:
                    row_id = None
            if not row_id:
                row_id = self.mapper_tree.insert("", "end", values=values)
                self._mapper_rows_by_segment[sid] = row_id

        def _ingest_pipeline_snapshot_segment(self, seg: dict):
            sid = str(seg.get("segment_id") or "")
            if not sid:
                return
            transcript = seg.get("text") or ""
            status = seg.get("sent_status") or seg.get("status") or "unknown"
            self._upsert_mapper_segment(
                sid,
                ts=seg.get("updated_ts") or seg.get("created_ts"),
                run_id=seg.get("flow_run_id") or "",
                audio_file=seg.get("audio_ref") or "",
                transcript=transcript,
                sent_status=status,
            )

        def _process_pipeline_event(self, entry: dict):
            if not isinstance(entry, dict):
                return
            event = str(entry.get("event") or "")
            data = entry.get("data") or {}
            if not isinstance(data, dict):
                data = {}
            ts = entry.get("ts")

            if event in ("audio_segment_captured", "stt_segment_finalized", "student_response_sent", "student_response_dropped_noise"):
                sid = str(data.get("segment_id") or "")
                if sid:
                    status_by_event = {
                        "audio_segment_captured": "captured",
                        "stt_segment_finalized": "transcribed",
                        "student_response_sent": "sent",
                        "student_response_dropped_noise": "dropped",
                    }
                    self._upsert_mapper_segment(
                        sid,
                        ts=ts,
                        run_id=data.get("flow_run_id") or "",
                        audio_file=data.get("audio_ref") or "",
                        transcript=data.get("text") or self._mapper_records_by_segment.get(sid, {}).get("transcript") or "",
                        sent_status=status_by_event.get(event, "unknown"),
                    )

            if event == "injection_text_sent":
                sid = str(data.get("segment_id") or "")
                if sid:
                    self._upsert_mapper_segment(
                        sid,
                        ts=ts,
                        run_id=data.get("flow_run_id") or "",
                        transcript=data.get("text") or "",
                        sent_status="sent" if not data.get("dropped") else "dropped",
                    )

            if event == "injection_audio_played":
                sid = str(data.get("segment_id") or "")
                if sid:
                    self._upsert_mapper_segment(
                        sid,
                        ts=ts,
                        run_id=data.get("flow_run_id") or "",
                        audio_file=data.get("wav_path") or "",
                        sent_status="captured",
                    )

        def _poll_pipeline(self):
            self._pipeline_poll_seq += 1
            try:
                should_refresh_status = (
                    (not self._mapper_hydrated)
                    or (self._pipeline_poll_seq % max(1, int(PIPELINE_STATUS_REFRESH_EVERY)) == 0)
                )
                if should_refresh_status:
                    status = self._router_json("GET", "/pipeline_status")
                    bridge = (status or {}).get("audio_bridge") or {}
                    bridge_state = "ready" if bridge.get("ready") else "not_ready"
                    self.pipeline_status.set(
                        f"{bridge_state} sink={bridge.get('sink_name')} source={bridge.get('source_name')}"
                    )

                    if not self._mapper_hydrated:
                        for seg in (status or {}).get("segments") or []:
                            if isinstance(seg, dict):
                                self._ingest_pipeline_snapshot_segment(seg)
                        self._mapper_hydrated = True
            except Exception as exc:
                self.pipeline_status.set(f"error: {exc}")

            try:
                logs = self._router_json("GET", "/get_logs?clear=1")
                for entry in (logs or {}).get("events") or []:
                    self._process_pipeline_event(entry)
            except Exception:
                pass

            try:
                self.root.after(PIPELINE_POLL_INTERVAL_MS, self._poll_pipeline)
            except Exception:
                pass

        def browse_audio(self):
            try:
                path = self._filedialog.askopenfilename(
                    title="Select WAV file for injection",
                    filetypes=[("WAV audio", "*.wav"), ("All files", "*.*")]
                )
            except Exception as exc:
                self.log(f"Audio file picker failed: {exc}", "ERROR")
                return
            if path:
                self.inject_audio_path.set(path)

        def inject_student_text(self):
            text = str(self.inject_text.get() or "").strip()
            if not text:
                self.log("Inject text is empty.", "WARN")
                return
            payload = {
                "text": text,
                "flow_run_id": str(self.inject_run_id.get() or "").strip() or None,
                "injected_by": "launcher",
            }
            try:
                resp = self._router_json("POST", "/inject/student_text", payload=payload)
                self.log(f"Inject text sent. response={_compact_text(resp)}")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace") if exc else ""
                self.log(f"Inject text failed HTTP {exc.code}: {body}", "ERROR")
            except Exception as exc:
                self.log(f"Inject text failed: {exc}", "ERROR")

        def inject_student_audio(self):
            wav_path = str(self.inject_audio_path.get() or "").strip()
            if not wav_path:
                self.log("No WAV selected for audio injection.", "WARN")
                return
            payload = {
                "wav_path": wav_path,
                "flow_run_id": str(self.inject_run_id.get() or "").strip() or None,
                "injected_by": "launcher",
            }
            try:
                resp = self._router_json("POST", "/inject/student_audio", payload=payload, timeout_s=6.0)
                self.log(f"Inject audio sent. response={_compact_text(resp)}")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace") if exc else ""
                self.log(f"Inject audio failed HTTP {exc.code}: {body}", "ERROR")
            except Exception as exc:
                self.log(f"Inject audio failed: {exc}", "ERROR")

        def replay_selected_audio(self):
            selected = self.mapper_tree.selection()
            if not selected:
                self.log("No mapper row selected.", "WARN")
                return
            row_id = selected[0]
            vals = self.mapper_tree.item(row_id, "values")
            if not vals or len(vals) < 4:
                self.log("Selected row is missing audio data.", "WARN")
                return
            audio_path = str(vals[3] or "").strip()
            if not audio_path:
                self.log("Selected row has no audio file.", "WARN")
                return
            if not os.path.isabs(audio_path):
                audio_path = os.path.abspath(os.path.join(BASE_DIR, audio_path))
            if not os.path.isfile(audio_path):
                self.log(f"Audio file not found: {audio_path}", "ERROR")
                return

            player_cmd = None
            if shutil.which("ffplay"):
                player_cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", audio_path]
            elif shutil.which("paplay"):
                player_cmd = ["paplay", audio_path]
            elif shutil.which("aplay"):
                player_cmd = ["aplay", audio_path]

            if not player_cmd:
                self.log("No audio player found (ffplay/paplay/aplay).", "ERROR")
                return

            try:
                subprocess.Popen(player_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.log(f"Replay started: {audio_path}")
            except Exception as exc:
                self.log(f"Replay failed: {exc}", "ERROR")

        def export_mapper_json(self):
            out_dir = os.path.join(BASE_DIR, "logs")
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, f"mapper-export-{int(time.time() * 1000)}.json")
            items = list(self._mapper_records_by_segment.values())
            items.sort(key=lambda x: int(x.get("ts") or 0))
            payload = {
                "created_ts": int(time.time() * 1000),
                "count": len(items),
                "segments": items,
            }
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=True, indent=2)
                self.log(f"Mapper exported: {path}")
            except Exception as exc:
                self.log(f"Mapper export failed: {exc}", "ERROR")

        def _normalize_capture_rect(self, rect_obj):
            fallback = (0, 0, TEACHER_CAM_WIDTH, TEACHER_CAM_HEIGHT)
            fx, fy, fw, fh = fallback
            if not isinstance(rect_obj, dict):
                return {"x": int(fx), "y": int(fy), "width": int(fw), "height": int(fh)}
            try:
                x = int(rect_obj.get("x", fx))
            except Exception:
                x = int(fx)
            try:
                y = int(rect_obj.get("y", fy))
            except Exception:
                y = int(fy)
            try:
                w = int(rect_obj.get("width", fw))
            except Exception:
                w = int(fw)
            try:
                h = int(rect_obj.get("height", fh))
            except Exception:
                h = int(fh)
            return {"x": max(0, x), "y": max(0, y), "width": max(64, w), "height": max(64, h)}

        def _x11_geometry_for_window_id(self, window_id) -> dict | None:
            if not shutil.which("xwininfo"):
                return None
            try:
                if isinstance(window_id, str):
                    wid = int(window_id.strip(), 0)
                else:
                    wid = int(window_id)
            except Exception:
                return None
            if wid <= 0:
                return None

            try:
                proc = subprocess.run(
                    ["xwininfo", "-id", hex(wid)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=2.2,
                )
                out = proc.stdout or ""
            except Exception:
                return None
            if proc.returncode != 0:
                return None

            width = height = None
            abs_x = abs_y = None
            for raw in out.splitlines():
                line = raw.strip()
                m = re.match(r"^Width:\s*(\d+)\s*$", line, re.IGNORECASE)
                if m:
                    try:
                        width = int(m.group(1))
                    except Exception:
                        width = None
                    continue
                m = re.match(r"^Height:\s*(\d+)\s*$", line, re.IGNORECASE)
                if m:
                    try:
                        height = int(m.group(1))
                    except Exception:
                        height = None
                    continue
                m = re.match(r"^Absolute upper-left X:\s*(-?\d+)\s*$", line, re.IGNORECASE)
                if m:
                    try:
                        abs_x = int(m.group(1))
                    except Exception:
                        abs_x = None
                    continue
                m = re.match(r"^Absolute upper-left Y:\s*(-?\d+)\s*$", line, re.IGNORECASE)
                if m:
                    try:
                        abs_y = int(m.group(1))
                    except Exception:
                        abs_y = None
                    continue

            if not width or not height:
                return None
            return {
                "x": int(abs_x) if abs_x is not None else 0,
                "y": int(abs_y) if abs_y is not None else 0,
                "width": int(width),
                "height": int(height),
            }

        def _resolve_x11_capture_window_id_for_rect(self, rect: dict, title_hint: str = "") -> str | None:
            if not isinstance(rect, dict):
                return None
            hint = str(title_hint or "").strip().lower()
            if not hint:
                hint = "akool"

            # First choice: reuse prepare.py's relaxed resolver (wmctrl + xwininfo fallback).
            try:
                from prepare import _resolve_x11_window_id_for_rect as _prepare_resolve
                wid = _prepare_resolve(rect, title_hint=hint, preferred_pids=None)
                if wid:
                    return str(wid)
            except Exception:
                pass

            if not shutil.which("xwininfo"):
                return None
            try:
                tx = int(rect.get("x", 0))
                ty = int(rect.get("y", 0))
                tw = int(rect.get("width", 0))
                th = int(rect.get("height", 0))
            except Exception:
                return None

            try:
                proc = subprocess.run(
                    ["xwininfo", "-root", "-tree"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=2.5,
                )
                out = proc.stdout or ""
            except Exception:
                return None

            best_id = None
            best_score = None
            rx_with_title = re.compile(
                r'^\s*(0x[0-9a-f]+)\s+"([^"]*)".*?\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)\s',
                re.IGNORECASE,
            )
            rx_plain = re.compile(r"^\s*(0x[0-9a-f]+)\s+.*?\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)\s", re.IGNORECASE)
            hint_tokens = [tok for tok in re.split(r"\s+", hint) if len(tok) >= 4][:4]
            for line in out.splitlines():
                m = rx_with_title.search(line)
                title = ""
                if m:
                    wid = m.group(1)
                    title = str(m.group(2) or "").strip().lower()
                    w_idx = 3
                else:
                    m = rx_plain.search(line)
                    if not m:
                        continue
                    wid = m.group(1)
                    w_idx = 2
                try:
                    w = int(m.group(w_idx))
                    h = int(m.group(w_idx + 1))
                    x = int(m.group(w_idx + 2))
                    y = int(m.group(w_idx + 3))
                except Exception:
                    continue
                if w < 120 or h < 120:
                    continue

                score = abs(x - tx) + abs(y - ty) + abs(w - tw) + abs(h - th)
                if hint:
                    if title and hint in title:
                        score -= 90
                    elif title and any(tok in title for tok in hint_tokens):
                        score -= 30
                    else:
                        score += 35
                if best_score is None or score < best_score:
                    best_score = score
                    best_id = wid

            if not best_id:
                return None

            # Resolve to the largest child window (usually the actual client area, no WM decorations).
            try:
                proc2 = subprocess.run(
                    ["xwininfo", "-id", best_id, "-children"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=2.5,
                )
                out2 = proc2.stdout or ""
            except Exception:
                return best_id

            child_best_id = None
            child_best_area = 0
            for line in out2.splitlines():
                m = rx_with_title.search(line)
                if m:
                    cid = m.group(1)
                    w_idx = 3
                else:
                    m = rx_plain.search(line)
                    if not m:
                        continue
                    cid = m.group(1)
                    w_idx = 2
                try:
                    cw = int(m.group(w_idx))
                    ch = int(m.group(w_idx + 1))
                except Exception:
                    continue
                if cw < 200 or ch < 200:
                    continue
                area = cw * ch
                if area > child_best_area:
                    child_best_area = area
                    child_best_id = cid

            return child_best_id or best_id

        def _teacher_capture_rect(self, allow_focus_switch: bool = True):
            fallback = (0, 0, TEACHER_CAM_WIDTH, TEACHER_CAM_HEIGHT)
            rect = {"x": fallback[0], "y": fallback[1], "width": fallback[2], "height": fallback[3]}
            title_hint = "akool"
            cached_window_id = None
            try:
                from prepare import window_xids_by_role  # local import avoids startup cycles
                cached_window_id = (window_xids_by_role or {}).get("teacher")
            except Exception:
                cached_window_id = None
            if cached_window_id:
                rect["window_id"] = str(cached_window_id)
                geom = self._x11_geometry_for_window_id(cached_window_id)
                if geom:
                    n = self._normalize_capture_rect(geom)
                    n["window_id"] = str(cached_window_id)
                    return n
                return rect

            env = self.driver
            if env is None:
                return rect

            teacher_driver = getattr(env, "teacher_driver", None)
            if teacher_driver is not None:
                try:
                    rect = self._normalize_capture_rect(teacher_driver.get_window_rect())
                    try:
                        t = str(teacher_driver.title or "").strip()
                        if t:
                            title_hint = t
                    except Exception:
                        pass
                    win_id = self._resolve_x11_capture_window_id_for_rect(rect, title_hint=title_hint)
                    if win_id:
                        rect["window_id"] = win_id
                        try:
                            from prepare import window_xids_by_role
                            window_xids_by_role["teacher"] = str(win_id)
                        except Exception:
                            pass
                        geom = self._x11_geometry_for_window_id(win_id)
                        if geom:
                            n = self._normalize_capture_rect(geom)
                            n["window_id"] = str(win_id)
                            return n
                    return rect
                except Exception:
                    pass

            if not allow_focus_switch:
                win_id = self._resolve_x11_capture_window_id_for_rect(rect, title_hint=title_hint)
                if win_id:
                    rect["window_id"] = win_id
                    try:
                        from prepare import window_xids_by_role
                        window_xids_by_role["teacher"] = str(win_id)
                    except Exception:
                        pass
                    geom = self._x11_geometry_for_window_id(win_id)
                    if geom:
                        n = self._normalize_capture_rect(geom)
                        n["window_id"] = str(win_id)
                        return n
                return rect

            main_driver = getattr(env, "main_driver", env)
            prev_handle = None
            target_handle = None
            try:
                prev_handle = main_driver.current_window_handle
            except Exception:
                prev_handle = None
            try:
                from prepare import window_handles_by_role  # local import avoids startup cycles
                target_handle = (window_handles_by_role or {}).get("teacher")
                if target_handle:
                    main_driver.switch_to.window(target_handle)
                    try:
                        t = str(main_driver.title or "").strip()
                        if t:
                            title_hint = t
                    except Exception:
                        pass
                    rect = self._normalize_capture_rect(main_driver.get_window_rect())
                    win_id = self._resolve_x11_capture_window_id_for_rect(rect, title_hint=title_hint)
                    if win_id:
                        rect["window_id"] = win_id
                        try:
                            from prepare import window_xids_by_role
                            window_xids_by_role["teacher"] = str(win_id)
                        except Exception:
                            pass
                        geom = self._x11_geometry_for_window_id(win_id)
                        if geom:
                            n = self._normalize_capture_rect(geom)
                            n["window_id"] = str(win_id)
                            return n
                    return rect
            except Exception:
                pass
            finally:
                if prev_handle and target_handle and str(prev_handle) != str(target_handle):
                    try:
                        main_driver.switch_to.window(prev_handle)
                    except Exception:
                        pass

            win_id = self._resolve_x11_capture_window_id_for_rect(rect, title_hint=title_hint)
            if win_id:
                rect["window_id"] = win_id
                try:
                    from prepare import window_xids_by_role
                    window_xids_by_role["teacher"] = str(win_id)
                except Exception:
                    pass
                geom = self._x11_geometry_for_window_id(win_id)
                if geom:
                    n = self._normalize_capture_rect(geom)
                    n["window_id"] = str(win_id)
                    return n
            return rect

        def _start_teacher_media_blocking(self, source: str = "manual", capture_rect: dict | None = None):
            if not TEACHER_CAM_ENABLED:
                self.log("Teacher media is disabled in config (TEACHER_CAM_ENABLED=False).", "WARN")
                return False
            if self._teacher_bridge is None:
                self.log("Teacher media bridge is unavailable.", "ERROR")
                return False

            capture_rect = capture_rect if isinstance(capture_rect, dict) else self._teacher_capture_rect()
            win_id = capture_rect.get("window_id") if isinstance(capture_rect, dict) else None
            win_id_note = f" window_id={win_id}" if win_id else ""
            self.log(
                "Teacher media start requested "
                f"source={source} "
                f"rect={capture_rect['x']},{capture_rect['y']} {capture_rect['width']}x{capture_rect['height']} "
                f"fps={TEACHER_CAM_FPS}{win_id_note}"
            )
            try:
                with self._teacher_media_op_lock:
                    result = self._teacher_bridge.start(capture_rect)
            except Exception as exc:
                self.teacher_media_status.set(f"error: {exc}")
                self.log(f"Teacher media error: {exc}", "ERROR")
                return False

            status = result.get("status") if isinstance(result, dict) else {}
            self._teacher_bridge_last_status = status if isinstance(status, dict) else {}
            self.teacher_media_status.set(self._teacher_status_text(self._teacher_bridge_last_status))
            if result.get("ok"):
                self.log(
                    "Teacher media ready "
                    f"dev={self._teacher_bridge_last_status.get('cam_device')} "
                    f"sink={self._teacher_bridge_last_status.get('sink_name')} "
                    f"source={self._teacher_bridge_last_status.get('source_name')}"
                )
                self.log(f"Teacher ffmpeg: {_compact_text(self._teacher_bridge_last_status.get('ffmpeg_cmd') or '', 320)}")
                return True
            else:
                err = result.get("error") or self._teacher_bridge_last_status.get("last_error") or "unknown_error"
                self.log(f"Teacher media error: {err}", "ERROR")
                return False

        def _stop_teacher_media_blocking(self, source: str = "manual"):
            if self._teacher_bridge is None:
                return
            self.log(f"Teacher media stop requested. source={source}")
            try:
                with self._teacher_media_op_lock:
                    result = self._teacher_bridge.stop()
            except Exception as exc:
                self.teacher_media_status.set(f"error: {exc}")
                self.log(f"Teacher media stop failed: {exc}", "ERROR")
                return
            status = result.get("status") if isinstance(result, dict) else {}
            self._teacher_bridge_last_status = status if isinstance(status, dict) else {}
            self.teacher_media_status.set(self._teacher_status_text(self._teacher_bridge_last_status))
            self.log("Teacher media stopped.")

        def _tick(self):
            try:
                port_open = is_tcp_port_open(ROUTER_HOST, ROUTER_PORT)
                self.port_status.set("open" if port_open else "closed")
            except Exception:
                self.port_status.set("unknown")

            try:
                p = self.server.proc
                if p and p.poll() is None:
                    self.server_status.set(f"running (pid {p.pid})")
                else:
                    self.server_status.set("stopped")
            except Exception:
                self.server_status.set("unknown")

            self.selenium_status.set("running" if self.driver is not None else "stopped")
            self._poll_teacher_bridge_status(force=False)

            try:
                self.root.after(LAUNCHER_TICK_INTERVAL_MS, self._tick)
            except Exception:
                pass

        def _signal_handler(self, *_args):
            self.log("Signal received, shutting down...", "WARN")
            try:
                self.stop_all()
            finally:
                try:
                    self.root.destroy()
                except Exception:
                    pass

        def _atexit_cleanup(self):
            try:
                self._cancel_teacher_autostart(reason="atexit")
                self._stop_teacher_media_blocking(source="atexit")
            except Exception:
                pass
            try:
                self._stop_router_blocking()
            except Exception:
                pass

        def _start_router_blocking(self):
            if is_tcp_port_open(ROUTER_HOST, ROUTER_PORT):
                managed_running = bool(self.server.proc and self.server.proc.poll() is None)
                has_walkie = self._router_supports_walkie()
                if has_walkie:
                    self.log(f"Router already listening on {ROUTER_HOST}:{ROUTER_PORT} (skipping start).")
                    return

                self.log(
                    f"Port {ROUTER_PORT} is occupied by a stale router/service (walkie endpoints missing). Restarting...",
                    "WARN",
                )

                if managed_running:
                    self._stop_router_blocking()
                else:
                    ok = _terminate_port_listener(ROUTER_PORT, timeout_s=4.0)
                    if not ok:
                        self.log(f"Could not free port {ROUTER_PORT}.", "ERROR")
                        return

            self.log("Starting router server (route.py)...")
            proc = subprocess.Popen(
                [sys.executable, "-u", os.path.join(BASE_DIR, "route.py")],
                cwd=BASE_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self.server.proc = proc

            def drain():
                try:
                    for line in proc.stdout:
                        raw = line.rstrip("\n")
                        formatted = _format_route_log_line(raw)
                        if formatted:
                            lvl, msg = formatted
                            self.log(msg, lvl)
                        else:
                            self.log(raw, "ROUTER")
                except Exception:
                    pass

            t = threading.Thread(target=drain, daemon=True)
            t.start()
            self.server.reader_thread = t

            deadline = time.time() + 6.0
            while time.time() < deadline:
                if is_tcp_port_open(ROUTER_HOST, ROUTER_PORT):
                    self.log("Router is up.")
                    return
                if proc.poll() is not None:
                    self.log("Router exited early. Check logs above.", "ERROR")
                    return
                time.sleep(0.2)

            self.log("Router did not open port in time (still may be starting).", "WARN")

        def _stop_router_blocking(self):
            proc = self.server.proc
            self.server.proc = None
            if not proc:
                if is_tcp_port_open(ROUTER_HOST, ROUTER_PORT):
                    self.log(f"Port {ROUTER_PORT} is open but no managed router process exists. Trying to stop listener...", "WARN")
                    ok = _terminate_port_listener(ROUTER_PORT, timeout_s=3.0)
                    self.log(
                        "External router listener stopped." if ok else "Could not stop external router listener.",
                        "INFO" if ok else "WARN",
                    )
                return

            if proc.poll() is not None:
                self.log("Router already stopped.")
                return

            self.log("Stopping router server...")
            ok = _safe_terminate_process(proc, timeout_s=3.0)
            self.log("Router stopped." if ok else "Router did not stop cleanly.", "WARN" if not ok else "INFO")

        def _start_selenium_blocking(self):
            if self.driver is not None:
                self.log("Selenium already running (skipping start).")
                return

            self.log("Starting Selenium environment (prepare.py)...")
            try:
                self._cancel_teacher_autostart(reason="start_selenium")
                ok_floor, count_floor, detail_floor = _ensure_static_workspace_floor(MIN_WORKSPACE_FLOOR)
                if ok_floor:
                    self.log(
                        f"Workspace preflight: static floor={MIN_WORKSPACE_FLOOR} ready (count={count_floor})."
                    )
                else:
                    self.log(
                        f"Workspace preflight warning: floor={MIN_WORKSPACE_FLOOR} "
                        f"not confirmed (count={count_floor}, detail={detail_floor}).",
                        "WARN",
                    )
                switched, switch_detail = _wmctrl_switch_workspace(FINAL_FOCUS_WORKSPACE)
                if switched:
                    self.log(f"Pre-launch focus moved to workspace {FINAL_FOCUS_WORKSPACE}.")
                else:
                    self.log(
                        f"Pre-launch focus switch failed for workspace {FINAL_FOCUS_WORKSPACE} ({switch_detail}).",
                        "WARN",
                    )
                if TEACHER_MEDIA_PREWARM:
                    self._prewarm_teacher_media_blocking(source="selenium_prelaunch")
                from prepare import launch_environment
                driver = launch_environment(base_workspace=FINAL_FOCUS_WORKSPACE)
                if driver is None:
                    self.log("Selenium start failed (launch_environment returned None).", "ERROR")
                    return
                self.driver = driver
                self.log("Selenium environment ready.")
                if TEACHER_MEDIA_AUTOSTART:
                    self._start_teacher_autostart_worker(source="selenium_ready")
            except Exception as exc:
                self.log(f"Selenium start failed: {exc}", "ERROR")

        def _stop_selenium_blocking(self):
            self._cancel_teacher_autostart(reason="stop_selenium")
            try:
                self._stop_teacher_media_blocking(source="stop_selenium")
            except Exception:
                pass

            driver = self.driver
            self.driver = None
            if driver is None:
                return

            try:
                from prepare import save_current_role_layout
                save_current_role_layout(driver)
            except Exception as exc:
                self.log(f"Window layout save skipped: {exc}", "WARN")

            self.log("Stopping Selenium (driver.quit)...")

            def quit_driver():
                try:
                    driver.quit()
                except Exception:
                    pass

            finished = _run_with_timeout(quit_driver, timeout_s=8.0)
            if finished:
                self.log("Selenium stopped.")
            else:
                self.log("driver.quit() timed out; Chrome may still be running.", "WARN")

            try:
                from prepare import restore_workspace_policy
                if restore_workspace_policy():
                    self.log("Workspace policy restored.")
            except Exception as exc:
                self.log(f"Workspace policy restore skipped: {exc}", "WARN")

        def _run_bg(self, fn, label: str):
            def worker():
                try:
                    fn()
                except Exception as exc:
                    self.log(f"{label} failed: {exc}", "ERROR")
                finally:
                    self._tick()

            threading.Thread(target=worker, daemon=True).start()

        # Button handlers (async)
        def start_router(self):
            self._run_bg(self._start_router_blocking, "start_router")

        def stop_router(self):
            self._run_bg(self._stop_router_blocking, "stop_router")

        def start_selenium(self):
            if self.driver is None:
                self._rotate_auto_run_id()
            self._run_bg(self._start_selenium_blocking, "start_selenium")

        def stop_selenium(self):
            self._run_bg(self._stop_selenium_blocking, "stop_selenium")

        def start_teacher_media(self):
            def _manual_start():
                self._cancel_teacher_autostart(reason="manual_start")
                self._start_teacher_media_blocking(source="manual")
            self._run_bg(_manual_start, "start_teacher_media")

        def stop_teacher_media(self):
            def _manual_stop():
                self._cancel_teacher_autostart(reason="manual_stop")
                self._stop_teacher_media_blocking(source="manual")
            self._run_bg(_manual_stop, "stop_teacher_media")

        def start_all(self):
            if self.driver is None:
                self._rotate_auto_run_id()
            def seq():
                self._start_router_blocking()
                self._start_selenium_blocking()
            self._run_bg(seq, "start_all")

        def stop_all(self):
            def seq():
                self._cancel_teacher_autostart(reason="stop_all")
                self._stop_selenium_blocking()
                self._stop_router_blocking()
            self._run_bg(seq, "stop_all")

        def on_close(self):
            self.log("Closing window. Shutting down...", "WARN")
            self._cancel_teacher_autostart(reason="on_close")
            try:
                self._stop_selenium_blocking()
            except Exception:
                pass
            try:
                self._stop_router_blocking()
            except Exception:
                pass
            self.root.destroy()

    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
