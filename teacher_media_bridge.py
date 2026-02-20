from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from typing import Any


def _now_ms() -> int:
    return int(time.time() * 1000)


def _run_cmd(cmd: list[str], timeout_s: float = 3.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(0.5, float(timeout_s)),
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except Exception as exc:
        return 1, "", str(exc)


def _x11_window_geometry(window_id: int) -> dict[str, int] | None:
    """
    Best-effort X11 geometry lookup for a window id via xwininfo.
    Returns width/height and absolute x/y when available.
    """
    try:
        wid = int(window_id)
    except Exception:
        return None
    if wid <= 0:
        return None

    # xwininfo accepts both decimal and 0x... ids; pass hex for readability.
    code, out, _err = _run_cmd(["xwininfo", "-id", hex(wid)], timeout_s=2.2)
    if code != 0:
        return None

    width = height = None
    abs_x = abs_y = None
    for raw in (out or "").splitlines():
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


def _x11_display_size(display_value: str) -> tuple[int, int] | None:
    display = str(display_value or "").strip()
    if not display:
        return None
    if "+" in display:
        display = display.split("+", 1)[0].strip()
    if not display:
        return None

    # Prefer xdpyinfo because it is available in most X11 setups.
    code, out, _err = _run_cmd(["xdpyinfo", "-display", display], timeout_s=2.2)
    if code == 0 and out:
        for raw in out.splitlines():
            line = raw.strip()
            m = re.search(r"dimensions:\s*(\d+)x(\d+)\s+pixels", line, re.IGNORECASE)
            if not m:
                continue
            try:
                w = int(m.group(1))
                h = int(m.group(2))
            except Exception:
                continue
            if w >= 64 and h >= 64:
                return (w, h)

    # Fallback to xrandr current-mode marker.
    code, out, _err = _run_cmd(["xrandr", "--display", display], timeout_s=2.2)
    if code == 0 and out:
        for raw in out.splitlines():
            if "*" not in raw:
                continue
            m = re.search(r"(\d+)x(\d+)", raw)
            if not m:
                continue
            try:
                w = int(m.group(1))
                h = int(m.group(2))
            except Exception:
                continue
            if w >= 64 and h >= 64:
                return (w, h)
    return None


def _ffmpeg_supports_x11grab_option(name: str) -> bool:
    """
    Return True when ffmpeg x11grab demuxer advertises the given option.
    Keeps startup compatible across older/newer ffmpeg builds.
    """
    option = str(name or "").strip()
    if not option:
        return False
    code, out, err = _run_cmd(["ffmpeg", "-hide_banner", "-h", "demuxer=x11grab"], timeout_s=2.4)
    if code != 0:
        return False
    blob = "\n".join([(out or ""), (err or "")])
    return option in blob


def _pactl_list_short(kind: str) -> list[str]:
    code, out, _err = _run_cmd(["pactl", "list", "short", kind], timeout_s=1.8)
    if code != 0:
        return []
    rows = []
    for raw in out.splitlines():
        raw = raw.strip()
        if raw:
            rows.append(raw)
    return rows


def _pulse_entry_exists(kind: str, name: str) -> bool:
    needle = f"\t{name}\t"
    for row in _pactl_list_short(kind):
        if needle in f"\t{row}\t":
            return True
    return False


def _pactl_load_module(args: list[str]) -> tuple[str | None, str | None]:
    code, out, err = _run_cmd(["pactl", "load-module", *args], timeout_s=2.6)
    if code != 0:
        return None, err or "pactl load-module failed"
    return ((out or "").strip() or None), None


def ensure_pulse_sink_and_source(sink_name: str, source_name: str) -> dict[str, Any]:
    sink = str(sink_name or "").strip() or "at_teacher_sink"
    source = str(source_name or "").strip() or "teacher_voice"

    module_sink_id = None
    module_source_id = None
    last_error = None

    sink_exists = _pulse_entry_exists("sinks", sink)
    if not sink_exists:
        module_sink_id, last_error = _pactl_load_module(
            [
                "module-null-sink",
                f"sink_name={sink}",
                f"sink_properties=device.description={sink}",
            ]
        )
        sink_exists = _pulse_entry_exists("sinks", sink)

    source_exists = _pulse_entry_exists("sources", source)
    if not source_exists:
        module_source_id, src_err = _pactl_load_module(
            [
                "module-remap-source",
                f"source_name={source}",
                f"master={sink}.monitor",
                f"source_properties=device.description={source}",
            ]
        )
        if src_err:
            last_error = src_err
        source_exists = _pulse_entry_exists("sources", source)

    return {
        "ready": bool(sink_exists and source_exists),
        "sink_name": sink,
        "source_name": source,
        "sink_exists": sink_exists,
        "source_exists": source_exists,
        "module_sink_id": module_sink_id,
        "module_source_id": module_source_id,
        "last_error": last_error,
    }


class TeacherMediaBridge:
    def __init__(
        self,
        *,
        sink_name: str | None = None,
        source_name: str | None = None,
        cam_device: str | None = None,
        cam_video_nr: int | None = None,
        cam_label: str | None = None,
        fps: int | None = None,
        width: int | None = None,
        height: int | None = None,
        capture_display: str | None = None,
    ):
        try:
            from config import (
                TEACHER_CAM_DEVICE as CFG_CAM_DEVICE,
                TEACHER_CAM_FPS as CFG_CAM_FPS,
                TEACHER_CAM_HEIGHT as CFG_CAM_HEIGHT,
                TEACHER_CAM_LABEL as CFG_CAM_LABEL,
                TEACHER_CAM_VIDEO_NR as CFG_CAM_VIDEO_NR,
                TEACHER_CAM_WIDTH as CFG_CAM_WIDTH,
                TEACHER_CAPTURE_DISPLAY as CFG_CAPTURE_DISPLAY,
                TEACHER_PULSE_SINK as CFG_SINK,
                TEACHER_PULSE_SOURCE as CFG_SOURCE,
            )
        except Exception:
            CFG_SINK, CFG_SOURCE = "at_teacher_sink", "teacher_voice"
            CFG_CAM_VIDEO_NR, CFG_CAM_LABEL = 9, "teacher_cam"
            CFG_CAM_DEVICE, CFG_CAM_FPS = "/dev/video9", 30
            CFG_CAM_WIDTH, CFG_CAM_HEIGHT = 960, 540
            CFG_CAPTURE_DISPLAY = ":0.0"

        self.sink_name = str(sink_name or CFG_SINK)
        self.source_name = str(source_name or CFG_SOURCE)
        self.cam_video_nr = int(cam_video_nr if cam_video_nr is not None else CFG_CAM_VIDEO_NR)
        self.cam_label = str(cam_label or CFG_CAM_LABEL)
        self.cam_device = str(cam_device or CFG_CAM_DEVICE)
        self.fps = max(1, int(fps if fps is not None else CFG_CAM_FPS))
        self.width = max(64, int(width if width is not None else CFG_CAM_WIDTH))
        self.height = max(64, int(height if height is not None else CFG_CAM_HEIGHT))
        self.capture_display = str(capture_display or CFG_CAPTURE_DISPLAY or os.getenv("DISPLAY") or "")

        self._lock = threading.Lock()
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._ffmpeg_stderr_thread: threading.Thread | None = None
        self._ffmpeg_cmd: list[str] | None = None
        self._capture_rect: dict[str, int] | None = None
        self._started_ts_ms: int | None = None
        self._last_error: str | None = None
        self._stderr_tail: list[str] = []
        self._module_sink_id: str | None = None
        self._module_source_id: str | None = None
        self._window_watch_stop: threading.Event | None = None
        self._window_watch_thread: threading.Thread | None = None
        self._window_last_wh: tuple[int, int] | None = None
        self._last_restart_ts_ms: int = 0
        self._ffmpeg_support_use_xdamage: bool | None = None
        self._status_cache: dict[str, Any] | None = None
        self._status_cache_ts: float = 0.0
        self._status_cache_ttl_s: float = 2.5

    def _append_stderr_line(self, line: str):
        text = str(line or "").strip()
        if not text:
            return
        self._stderr_tail.append(text)
        if len(self._stderr_tail) > 40:
            del self._stderr_tail[: len(self._stderr_tail) - 40]

    def _consume_ffmpeg_stderr(self, proc: subprocess.Popen):
        stream = proc.stderr
        if stream is None:
            return
        try:
            for raw in stream:
                with self._lock:
                    self._append_stderr_line(raw)
        except Exception:
            return

    def _display_value(self) -> str:
        value = str(self.capture_display or "").strip()
        if value:
            return value
        return str(os.getenv("DISPLAY") or "").strip()

    def _device_exists(self) -> bool:
        return os.path.exists(self.cam_device)

    def _device_writable(self) -> bool:
        return self._device_exists() and os.access(self.cam_device, os.W_OK)

    def _sanitize_rect(self, capture_rect: dict[str, Any] | None) -> dict[str, int]:
        rect = capture_rect if isinstance(capture_rect, dict) else {}
        try:
            x = int(rect.get("x", 0))
        except Exception:
            x = 0
        try:
            y = int(rect.get("y", 0))
        except Exception:
            y = 0
        try:
            w = int(rect.get("width", self.width))
        except Exception:
            w = self.width
        try:
            h = int(rect.get("height", self.height))
        except Exception:
            h = self.height
        return {
            "x": max(0, x),
            "y": max(0, y),
            "width": max(64, w),
            "height": max(64, h),
        }

    def _clamp_rect_to_display(self, rect: dict[str, int], display: str) -> dict[str, int]:
        if not isinstance(rect, dict):
            return {"x": 0, "y": 0, "width": self.width, "height": self.height}
        size = _x11_display_size(display)
        if not size:
            return dict(rect)
        sw, sh = size
        if sw < 64 or sh < 64:
            return dict(rect)

        x = max(0, int(rect.get("x", 0)))
        y = max(0, int(rect.get("y", 0)))
        w = max(64, int(rect.get("width", self.width)))
        h = max(64, int(rect.get("height", self.height)))

        x = min(x, max(0, sw - 64))
        y = min(y, max(0, sh - 64))
        max_w = max(64, sw - x)
        max_h = max(64, sh - y)
        w = min(w, max_w)
        h = min(h, max_h)
        return {"x": int(x), "y": int(y), "width": int(w), "height": int(h)}

    def _ensure_loopback_device_locked(self) -> tuple[bool, str | None]:
        if self._device_exists():
            return True, None

        sudo_hint = (
            "sudo modprobe v4l2loopback "
            f"devices=1 video_nr={self.cam_video_nr} "
            f"card_label={self.cam_label} exclusive_caps=1"
        )
        cmd = [
            "modprobe",
            "v4l2loopback",
            "devices=1",
            f"video_nr={self.cam_video_nr}",
            f"card_label={self.cam_label}",
            "exclusive_caps=1",
        ]
        code, _out, err = _run_cmd(cmd, timeout_s=4.5)
        # If launcher is not root, try non-interactive sudo as a fallback.
        # This works when user configured NOPASSWD for modprobe.
        if code != 0 and os.geteuid() != 0:
            sudo_cmd = ["sudo", "-n", *cmd]
            sudo_code, _sudo_out, sudo_err = _run_cmd(sudo_cmd, timeout_s=4.8)
            if sudo_code == 0:
                code, err = 0, ""
            elif sudo_err:
                err = sudo_err
        if code != 0:
            msg = (err or "unknown error").strip()
            if os.geteuid() != 0:
                return (
                    False,
                    "modprobe_failed: "
                    f"{msg} | auto-sudo requires NOPASSWD; otherwise run once: {sudo_hint}",
                )
            return False, f"modprobe_failed: {msg}"

        deadline = time.time() + 1.8
        while time.time() < deadline:
            if self._device_exists():
                return True, None
            time.sleep(0.1)
        return False, f"loopback_device_missing_after_modprobe: {self.cam_device}"

    def _ensure_ready_locked(self) -> dict[str, Any]:
        self._status_cache = None
        self._status_cache_ts = 0.0
        pulse = ensure_pulse_sink_and_source(self.sink_name, self.source_name)
        self._module_sink_id = pulse.get("module_sink_id") or self._module_sink_id
        self._module_source_id = pulse.get("module_source_id") or self._module_source_id

        display = self._display_value()
        display_ready = bool(display)

        device_ready, device_err = self._ensure_loopback_device_locked()
        device_exists = self._device_exists()
        device_writable = self._device_writable()

        last_error = pulse.get("last_error")
        if not display_ready:
            last_error = "display_unavailable"
        if not device_ready:
            last_error = device_err or last_error
        if device_exists and not device_writable:
            last_error = f"device_not_writable: {self.cam_device} (add user to 'video' group)"
        self._last_error = last_error or self._last_error

        running = bool(self._ffmpeg_proc and self._ffmpeg_proc.poll() is None)
        return {
            "ready": bool(
                pulse.get("ready")
                and display_ready
                and device_ready
                and device_exists
                and device_writable
            ),
            "running": running,
            "sink_name": self.sink_name,
            "source_name": self.source_name,
            "sink_exists": bool(pulse.get("sink_exists")),
            "source_exists": bool(pulse.get("source_exists")),
            "module_sink_id": self._module_sink_id,
            "module_source_id": self._module_source_id,
            "cam_device": self.cam_device,
            "cam_video_nr": self.cam_video_nr,
            "cam_label": self.cam_label,
            "device_exists": device_exists,
            "device_writable": device_writable,
            "capture_display": display or None,
            "capture_rect": dict(self._capture_rect or {}),
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "pid": self._ffmpeg_proc.pid if running else None,
            "started_ts_ms": self._started_ts_ms,
            "last_error": self._last_error,
            "ffmpeg_cmd": " ".join(self._ffmpeg_cmd) if self._ffmpeg_cmd else None,
        }

    def _stop_locked(self):
        try:
            if self._window_watch_stop is not None:
                self._window_watch_stop.set()
        except Exception:
            pass
        self._window_watch_stop = None
        self._window_watch_thread = None
        self._window_last_wh = None

        proc = self._ffmpeg_proc
        self._ffmpeg_proc = None
        self._ffmpeg_stderr_thread = None
        self._started_ts_ms = None
        self._ffmpeg_cmd = None
        self._status_cache = None
        self._status_cache_ts = 0.0

        if proc is None:
            return

        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass

        deadline = time.time() + 2.5
        while time.time() < deadline:
            try:
                if proc.poll() is not None:
                    return
            except Exception:
                return
            time.sleep(0.08)

        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass

    def _start_window_watch_locked(self, window_id: int):
        try:
            wid = int(window_id)
        except Exception:
            return
        if wid <= 0:
            return

        # Replace any prior watcher.
        try:
            if self._window_watch_stop is not None:
                self._window_watch_stop.set()
        except Exception:
            pass

        stop_ev = threading.Event()
        self._window_watch_stop = stop_ev

        def _watch():
            stable_reads = 0
            last_seen: tuple[int, int] | None = None
            while not stop_ev.is_set():
                time.sleep(1.0)
                geom = _x11_window_geometry(wid)
                if not geom:
                    continue
                wh = (int(geom.get("width", 0) or 0), int(geom.get("height", 0) or 0))
                if wh[0] < 64 or wh[1] < 64:
                    continue

                if last_seen == wh:
                    stable_reads += 1
                else:
                    last_seen = wh
                    stable_reads = 0

                # Require 2 reads in a row (reduces chatter during animations).
                if stable_reads < 1:
                    continue

                with self._lock:
                    running = bool(self._ffmpeg_proc and self._ffmpeg_proc.poll() is None)
                    if not running:
                        return
                    current_wh = self._window_last_wh
                    current_rect = dict(self._capture_rect or {})
                    raw = current_rect.get("window_id") or current_rect.get("x11_window_id")
                    try:
                        current_wid = int(str(raw).strip(), 0) if isinstance(raw, str) else int(raw)
                    except Exception:
                        current_wid = None
                    if current_wid != wid:
                        return
                    if current_wh == wh:
                        continue

                    now_ms = _now_ms()
                    if now_ms - int(self._last_restart_ts_ms or 0) < 1600:
                        continue
                    self._last_restart_ts_ms = now_ms

                    # Restart ffmpeg with updated capture size. Output size stays stable.
                    new_rect = dict(current_rect)
                    new_rect["width"] = wh[0]
                    new_rect["height"] = wh[1]
                    try:
                        self._stop_locked()
                        self._start_ffmpeg_locked(new_rect)
                    except Exception as exc:
                        self._last_error = f"ffmpeg_restart_failed: {exc}"
                        return

        t = threading.Thread(target=_watch, daemon=True, name="teacher_window_watch")
        self._window_watch_thread = t
        t.start()

    def _start_ffmpeg_locked(self, capture_rect: dict[str, Any]) -> None:
        rect = self._sanitize_rect(capture_rect)

        raw_window_id = None
        for k in ("window_id", "x11_window_id", "windowId", "x11WindowId"):
            if isinstance(capture_rect, dict) and capture_rect.get(k) is not None:
                raw_window_id = capture_rect.get(k)
                break

        window_id = None
        if raw_window_id is not None:
            try:
                if isinstance(raw_window_id, str):
                    window_id = int(raw_window_id.strip(), 0)
                else:
                    window_id = int(raw_window_id)
            except Exception:
                window_id = None
        display = self._display_value()
        if not window_id:
            # Prevent ffmpeg x11grab failures when Selenium reports a rect that
            # extends outside the physical X screen.
            rect = self._clamp_rect_to_display(rect, display)

        self._capture_rect = dict(rect)
        if window_id:
            # Keep window id visible in status()/pipeline UI and allow the
            # resize watcher to validate it's still tracking the same source.
            try:
                self._capture_rect["window_id"] = hex(int(window_id))
            except Exception:
                self._capture_rect["window_id"] = str(raw_window_id)

        input_spec = f"{display}+{rect['x']},{rect['y']}"

        # Chrome/WebRTC are much happier with YUYV than planar yuv420p from
        # v4l2loopback. Keep output stable at configured WxH.
        vf = f"scale={self.width}:{self.height},format=yuyv422"

        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "x11grab",
            "-framerate",
            str(self.fps),
        ]
        if self._ffmpeg_support_use_xdamage is None:
            self._ffmpeg_support_use_xdamage = _ffmpeg_supports_x11grab_option("use_xdamage")
        if self._ffmpeg_support_use_xdamage:
            cmd.extend(["-use_xdamage", "0"])

        if window_id:
            geom = _x11_window_geometry(window_id)
            cap_w = cap_h = None
            if geom:
                try:
                    cap_w = int(geom.get("width", 0) or 0)
                    cap_h = int(geom.get("height", 0) or 0)
                except Exception:
                    cap_w = cap_h = None

            if cap_w and cap_h:
                cmd.extend(["-video_size", f"{cap_w}x{cap_h}"])
                self._window_last_wh = (cap_w, cap_h)
                try:
                    self._capture_rect["width"] = int(cap_w)
                    self._capture_rect["height"] = int(cap_h)
                except Exception:
                    pass
            else:
                cmd.extend(["-video_size", f"{rect['width']}x{rect['height']}"])
                self._window_last_wh = (rect["width"], rect["height"])

            capture_display = display.split("+", 1)[0]
            cmd.extend(["-window_id", str(window_id), "-i", capture_display])
        else:
            cmd.extend(["-video_size", f"{rect['width']}x{rect['height']}", "-i", input_spec])

        cmd.extend([
            "-vf",
            vf,
            "-pix_fmt",
            "yuyv422",
            "-vcodec",
            "rawvideo",
            "-f",
            "v4l2",
            self.cam_device,
        ])

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        self._ffmpeg_proc = proc
        self._ffmpeg_cmd = cmd
        self._started_ts_ms = _now_ms()
        self._stderr_tail = []
        self._status_cache = None
        self._status_cache_ts = 0.0

        t = threading.Thread(
            target=self._consume_ffmpeg_stderr,
            args=(proc,),
            daemon=True,
            name="teacher_ffmpeg_stderr",
        )
        t.start()
        self._ffmpeg_stderr_thread = t

        if window_id:
            try:
                self._start_window_watch_locked(window_id)
            except Exception:
                pass

    def ensure_ready(self) -> dict[str, Any]:
        with self._lock:
            return self._ensure_ready_locked()

    def start(self, capture_rect: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            ready = self._ensure_ready_locked()
            if not ready.get("ready"):
                return {"ok": False, "error": ready.get("last_error") or "teacher_media_not_ready", "status": ready}

            self._stop_locked()
            try:
                self._start_ffmpeg_locked(capture_rect)
            except Exception as exc:
                self._last_error = f"ffmpeg_start_failed: {exc}"
                return {"ok": False, "error": self._last_error, "status": self._ensure_ready_locked()}

        time.sleep(0.35)
        with self._lock:
            proc = self._ffmpeg_proc
            failed = False
            if proc is None:
                failed = True
            else:
                rc = proc.poll()
                if rc is not None:
                    tail = " | ".join(self._stderr_tail[-4:])
                    self._last_error = f"ffmpeg_exited_early rc={rc}" + (f" ({tail})" if tail else "")
                    self._stop_locked()
                    failed = True

            status = self._ensure_ready_locked()
            if failed:
                return {"ok": False, "error": self._last_error or "ffmpeg_failed", "status": status}
            return {"ok": True, "status": status}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop_locked()
            status = self._ensure_ready_locked()
        return {"ok": True, "status": status}

    def status(self, force_refresh: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            running = bool(self._ffmpeg_proc and self._ffmpeg_proc.poll() is None)
            if self._ffmpeg_proc is not None and not running:
                rc = self._ffmpeg_proc.poll()
                if rc not in (None, 0):
                    tail = " | ".join(self._stderr_tail[-4:])
                    self._last_error = f"ffmpeg_stopped rc={rc}" + (f" ({tail})" if tail else "")

            cache = self._status_cache
            cache_ts = float(self._status_cache_ts or 0.0)
            if (
                not force_refresh
                and isinstance(cache, dict)
                and (now - cache_ts) < max(0.2, float(self._status_cache_ttl_s))
            ):
                out = dict(cache)
                out["running"] = running
                out["pid"] = self._ffmpeg_proc.pid if running else None
                out["started_ts_ms"] = self._started_ts_ms
                out["last_error"] = self._last_error
                out["capture_rect"] = dict(self._capture_rect or {})
                out["ffmpeg_cmd"] = " ".join(self._ffmpeg_cmd) if self._ffmpeg_cmd else None
                out["ffmpeg_stderr_tail"] = list(self._stderr_tail[-8:])
                out["module_sink_id"] = self._module_sink_id
                out["module_source_id"] = self._module_source_id
                return out

        sink_exists = _pulse_entry_exists("sinks", self.sink_name)
        source_exists = _pulse_entry_exists("sources", self.source_name)
        display = self._display_value()
        device_exists = self._device_exists()
        device_writable = self._device_writable()

        with self._lock:
            running = bool(self._ffmpeg_proc and self._ffmpeg_proc.poll() is None)
            if self._ffmpeg_proc is not None and not running:
                rc = self._ffmpeg_proc.poll()
                if rc not in (None, 0):
                    tail = " | ".join(self._stderr_tail[-4:])
                    self._last_error = f"ffmpeg_stopped rc={rc}" + (f" ({tail})" if tail else "")

            out = {
                "ready": bool(sink_exists and source_exists and bool(display) and device_exists and device_writable),
                "running": running,
                "sink_name": self.sink_name,
                "source_name": self.source_name,
                "sink_exists": sink_exists,
                "source_exists": source_exists,
                "module_sink_id": self._module_sink_id,
                "module_source_id": self._module_source_id,
                "capture_display": display or None,
                "capture_rect": dict(self._capture_rect or {}),
                "fps": self.fps,
                "width": self.width,
                "height": self.height,
                "cam_device": self.cam_device,
                "cam_video_nr": self.cam_video_nr,
                "cam_label": self.cam_label,
                "device_exists": device_exists,
                "device_writable": device_writable,
                "pid": self._ffmpeg_proc.pid if running else None,
                "started_ts_ms": self._started_ts_ms,
                "last_error": self._last_error,
                "ffmpeg_cmd": " ".join(self._ffmpeg_cmd) if self._ffmpeg_cmd else None,
                "ffmpeg_stderr_tail": list(self._stderr_tail[-8:]),
            }
            self._status_cache = dict(out)
            self._status_cache_ts = now
            return out
