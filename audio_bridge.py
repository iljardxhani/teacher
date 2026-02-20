from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from typing import Any


def _safe_slug(raw: str) -> str:
    text = str(raw or "").strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "_", text)
    return text or "run"


class AudioBridge:
    def __init__(self, sink_name: str, source_name: str, logs_dir: str, segment_seconds: float = 4.0):
        self.sink_name = str(sink_name or "at_class_sink")
        self.source_name = str(source_name or "student_voice")
        self.logs_dir = os.path.abspath(logs_dir)
        self.segment_seconds = max(0.2, float(segment_seconds or 4.0))

        self._lock = threading.Lock()
        self._capture_jobs: dict[str, dict[str, Any]] = {}
        self._play_jobs: dict[str, dict[str, Any]] = {}
        self._last_error: str | None = None
        self._module_sink_id: str | None = None
        self._module_source_id: str | None = None
        self._status_cache: dict[str, Any] | None = None
        self._status_cache_ts: float = 0.0
        self._status_cache_ttl_s: float = 2.5

        os.makedirs(self.logs_dir, exist_ok=True)

    def _run(self, cmd: list[str], timeout_s: float = 2.5) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=max(0.5, float(timeout_s)),
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except Exception as e:
            return 1, "", str(e)

    def _list_short(self, kind: str) -> list[str]:
        code, out, _ = self._run(["pactl", "list", "short", kind], timeout_s=1.8)
        if code != 0:
            return []
        lines = []
        for raw in out.splitlines():
            raw = raw.strip()
            if raw:
                lines.append(raw)
        return lines

    def _sink_exists(self) -> bool:
        needle = f"\t{self.sink_name}\t"
        for line in self._list_short("sinks"):
            if needle in f"\t{line}\t":
                return True
        return False

    def _source_exists(self) -> bool:
        needle = f"\t{self.source_name}\t"
        for line in self._list_short("sources"):
            if needle in f"\t{line}\t":
                return True
        return False

    def _load_module(self, args: list[str]) -> str | None:
        code, out, err = self._run(["pactl", "load-module", *args], timeout_s=2.5)
        if code != 0:
            self._last_error = err or f"failed: {' '.join(args)}"
            return None
        return (out or "").strip() or None

    def ensure_ready(self) -> dict[str, Any]:
        with self._lock:
            self._last_error = None
            self._status_cache = None
            self._status_cache_ts = 0.0

            sink_exists = self._sink_exists()
            if not sink_exists:
                self._module_sink_id = self._load_module(
                    [
                        "module-null-sink",
                        f"sink_name={self.sink_name}",
                        f"sink_properties=device.description={self.sink_name}",
                    ]
                )
                sink_exists = self._sink_exists()

            source_exists = self._source_exists()
            if not source_exists:
                self._module_source_id = self._load_module(
                    [
                        "module-remap-source",
                        f"source_name={self.source_name}",
                        f"master={self.sink_name}.monitor",
                        f"source_properties=device.description={self.source_name}",
                    ]
                )
                source_exists = self._source_exists()

            return {
                "ready": bool(sink_exists and source_exists),
                "sink_name": self.sink_name,
                "source_name": self.source_name,
                "sink_exists": sink_exists,
                "source_exists": source_exists,
                "module_sink_id": self._module_sink_id,
                "module_source_id": self._module_source_id,
                "last_error": self._last_error,
            }

    def status(self, force_refresh: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            capture_jobs = list(self._capture_jobs.values())
            play_jobs = list(self._play_jobs.values())
            last_error = self._last_error
            module_sink_id = self._module_sink_id
            module_source_id = self._module_source_id
            cache = self._status_cache
            cache_ts = float(self._status_cache_ts or 0.0)

        if (
            not force_refresh
            and isinstance(cache, dict)
            and (now - cache_ts) < max(0.2, float(self._status_cache_ttl_s))
        ):
            out = dict(cache)
            out["capture_jobs"] = capture_jobs[-20:]
            out["play_jobs"] = play_jobs[-20:]
            out["module_sink_id"] = module_sink_id
            out["module_source_id"] = module_source_id
            if last_error:
                out["last_error"] = last_error
            return out

        code, info_out, info_err = self._run(["pactl", "info"], timeout_s=1.8)
        default_sink = None
        default_source = None
        if code == 0 and info_out:
            for line in info_out.splitlines():
                line = line.strip()
                if line.startswith("Default Sink:"):
                    default_sink = line.split(":", 1)[1].strip()
                elif line.startswith("Default Source:"):
                    default_source = line.split(":", 1)[1].strip()

        sink_exists = self._sink_exists()
        source_exists = self._source_exists()

        out = {
            "ready": bool(sink_exists and source_exists),
            "sink_name": self.sink_name,
            "source_name": self.source_name,
            "sink_exists": sink_exists,
            "source_exists": source_exists,
            "default_sink": default_sink,
            "default_source": default_source,
            "module_sink_id": module_sink_id,
            "module_source_id": module_source_id,
            "capture_jobs": capture_jobs[-20:],
            "play_jobs": play_jobs[-20:],
            "last_error": last_error or (info_err if code != 0 else None),
        }
        with self._lock:
            self._status_cache = dict(out)
            self._status_cache_ts = now
        return out

    def capture_segment(
        self,
        flow_run_id: str | None,
        segment_id: str,
        duration_s: float | None = None,
    ) -> str:
        run_key = _safe_slug(flow_run_id or "no_run")
        seg_key = _safe_slug(segment_id or f"seg-{int(time.time() * 1000)}")
        out_dir = os.path.join(self.logs_dir, "audio", run_key)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.abspath(os.path.join(out_dir, f"{seg_key}.wav"))
        capture_seconds = max(0.2, float(duration_s or self.segment_seconds))

        job = {
            "segment_id": segment_id,
            "flow_run_id": flow_run_id,
            "audio_ref": out_path,
            "state": "queued",
            "started_ts": int(time.time() * 1000),
            "duration_s": capture_seconds,
        }
        with self._lock:
            self._capture_jobs[segment_id] = job

        def runner():
            with self._lock:
                job["state"] = "running"
            cmd = [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "pulse",
                "-i",
                self.source_name,
                "-t",
                f"{capture_seconds:.2f}",
                "-ac",
                "1",
                "-ar",
                "16000",
                out_path,
            ]
            code, _out, err = self._run(cmd, timeout_s=capture_seconds + 8.0)
            with self._lock:
                job["finished_ts"] = int(time.time() * 1000)
                if code == 0 and os.path.isfile(out_path):
                    job["state"] = "done"
                else:
                    job["state"] = "failed"
                    job["error"] = err or "capture_failed"

        threading.Thread(target=runner, daemon=True).start()
        return out_path

    def play_wav(self, wav_path: str) -> dict[str, Any]:
        path = os.path.abspath(str(wav_path or ""))
        if not os.path.isfile(path):
            return {"ok": False, "error": f"wav_not_found: {path}"}

        status = self.ensure_ready()
        if not status.get("ready"):
            return {"ok": False, "error": "audio_bridge_not_ready", "status": status}

        play_id = f"play-{int(time.time() * 1000)}"
        job = {
            "play_id": play_id,
            "wav_path": path,
            "state": "queued",
            "started_ts": int(time.time() * 1000),
        }
        with self._lock:
            self._play_jobs[play_id] = job

        def runner():
            with self._lock:
                job["state"] = "running"
            cmd = [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-re",
                "-i",
                path,
                "-f",
                "pulse",
                self.sink_name,
            ]
            code, _out, err = self._run(cmd, timeout_s=120.0)
            with self._lock:
                job["finished_ts"] = int(time.time() * 1000)
                if code == 0:
                    job["state"] = "done"
                else:
                    job["state"] = "failed"
                    job["error"] = err or "play_failed"

        threading.Thread(target=runner, daemon=True).start()
        return {"ok": True, "play_id": play_id, "wav_path": path}
