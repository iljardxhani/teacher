from __future__ import annotations

from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import json
import os
import re
import secrets
import socket
import threading
import time
from typing import Any

from werkzeug.serving import make_server

app = Flask(__name__)
CORS(app)

# ================== REGISTRIES ==================
message_queues_by_role = {
    "ai": [],
    "teacher": [],
    "class": [],
    "stt": []
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOOK_RULES_DIR = os.path.join(BASE_DIR, "book_rules")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

try:
    from config import (
        AUDIO_SEGMENT_SECONDS,
        CLASS_PULSE_SINK,
        CLASS_WALKIE_MODE,
        STT_PULSE_SOURCE,
        WALKIE_ENABLE_TLS,
        WALKIE_SESSION_TTL_SECONDS,
        WALKIE_TLS_CERT_PATH,
        WALKIE_TLS_KEY_PATH,
        WALKIE_TLS_PORT,
    )
except Exception:
    CLASS_PULSE_SINK, STT_PULSE_SOURCE, AUDIO_SEGMENT_SECONDS = "at_class_sink", "student_voice", 4.0
    CLASS_WALKIE_MODE = False
    WALKIE_ENABLE_TLS = False
    WALKIE_TLS_PORT = 5443
    WALKIE_TLS_CERT_PATH = ""
    WALKIE_TLS_KEY_PATH = ""
    WALKIE_SESSION_TTL_SECONDS = 1800

_rule_cache = {}  # path -> (mtime, text)

# In-memory structured event log for debugging.
_event_log = []  # list[dict]
_EVENT_LOG_MAX = 5000

_run_files_by_id = {}  # run_id -> file_path
_run_events_by_id = {}  # run_id -> list[event_entry]
_RUN_EVENTS_MAX = 20000
_auto_run_lock = threading.Lock()
_auto_run_next_idx = None
_legacy_run_id_map = {}  # old/custom id -> generated logN

_pipeline_segments_by_id = {}  # segment_id -> dict
_pipeline_segment_order = []
_PIPELINE_SEGMENTS_MAX = 2000
_pipeline_last_ids = {
    "captured": None,
    "transcribed": None,
    "sent": None,
    "dropped": None,
}

_audio_bridge = None
_audio_bridge_error = None
_audio_bridge_lock = threading.Lock()
_audio_bridge_ready_logged = False
_audio_bridge_last_ensure_ms = 0

WALKIE_PAGES_DIR = os.path.join(BASE_DIR, "walkie_pages")
os.makedirs(WALKIE_PAGES_DIR, exist_ok=True)

_walkie_lock = threading.Lock()
_walkie_sessions_by_id: dict[str, dict[str, Any]] = {}
_walkie_session_id_by_pair_code: dict[str, str] = {}
_WALKIE_SIGNAL_TYPES = {"offer", "answer", "ptt_state", "heartbeat"}
_WALKIE_MAX_SIGNAL_QUEUE = 300
_WALKIE_PULL_TIMEOUT_MS_MAX = 25000

_walkie_tls_ready = False


def _safe_filename(raw):
    raw = (raw or "").strip()
    raw = raw.replace(" ", "_")
    raw = raw.lower()
    raw = re.sub(r"[^a-z0-9_-]+", "", raw)
    return raw or "run"


def _scan_next_log_index() -> int:
    max_idx = 0
    try:
        for name in os.listdir(LOGS_DIR):
            base = str(name or "")
            m = re.match(r"^log(\d+)(?:[\.-]|$)", base, re.IGNORECASE)
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
    return max_idx + 1


def _next_auto_run_id() -> str:
    global _auto_run_next_idx
    with _auto_run_lock:
        if _auto_run_next_idx is None:
            _auto_run_next_idx = _scan_next_log_index()
        rid = f"log{int(_auto_run_next_idx)}"
        _auto_run_next_idx += 1
        return rid


def _now_ms():
    return int(time.time() * 1000)


def _walkie_is_tls_ready():
    if not WALKIE_ENABLE_TLS:
        return True
    cert_ok = bool(WALKIE_TLS_CERT_PATH and os.path.isfile(WALKIE_TLS_CERT_PATH))
    key_ok = bool(WALKIE_TLS_KEY_PATH and os.path.isfile(WALKIE_TLS_KEY_PATH))
    return bool(cert_ok and key_ok and _walkie_tls_ready)


def _walkie_pair_code():
    # 6-digit code is enough for temporary LAN pairing tests.
    return "".join(secrets.choice("0123456789") for _ in range(6))


def _walkie_make_session_id():
    return f"walkie-{_now_ms()}-{secrets.token_hex(4)}"


def _walkie_token():
    return secrets.token_urlsafe(24)


def _walkie_lane_ip_guess():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0] or "")
        finally:
            s.close()
    except Exception:
        return ""


def _walkie_lan_transmitter_url():
    host = _walkie_lane_ip_guess() or "127.0.0.1"
    scheme = "https" if WALKIE_ENABLE_TLS else "http"
    port = WALKIE_TLS_PORT if WALKIE_ENABLE_TLS else 5000
    return f"{scheme}://{host}:{port}/walkie/transmitter"


def _walkie_info_payload():
    cert_exists = bool(WALKIE_TLS_CERT_PATH and os.path.isfile(WALKIE_TLS_CERT_PATH))
    key_exists = bool(WALKIE_TLS_KEY_PATH and os.path.isfile(WALKIE_TLS_KEY_PATH))
    scheme = "https" if WALKIE_ENABLE_TLS else "http"
    port = WALKIE_TLS_PORT if WALKIE_ENABLE_TLS else 5000
    return {
        "class_walkie_mode": bool(CLASS_WALKIE_MODE),
        "tls_enabled": bool(WALKIE_ENABLE_TLS),
        "tls_ready": bool(_walkie_is_tls_ready()),
        "tls_port": int(WALKIE_TLS_PORT),
        "tls_cert_path": WALKIE_TLS_CERT_PATH,
        "tls_key_path": WALKIE_TLS_KEY_PATH,
        "tls_cert_exists": cert_exists,
        "tls_key_exists": key_exists,
        "lan_ip": _walkie_lane_ip_guess() or None,
        "transmitter_url_template": f"{scheme}://<LAN_IP>:{port}/walkie/transmitter",
        "transmitter_lan_url": _walkie_lan_transmitter_url(),
        "receiver_local_url": f"{scheme}://127.0.0.1:{port}/walkie/receiver",
    }


def _walkie_log_rejected(reason, **extra):
    payload = {"reason": reason}
    payload.update(extra or {})
    _log_event("walkie_signal_rejected", payload, level="warn")


def _walkie_prune_sessions_locked():
    now_ms = _now_ms()
    stale_ids = []
    for sid, sess in _walkie_sessions_by_id.items():
        if not isinstance(sess, dict):
            stale_ids.append(sid)
            continue
        expires_at = int(sess.get("expires_at") or 0)
        if sess.get("closed") or (expires_at > 0 and now_ms > expires_at):
            stale_ids.append(sid)

    for sid in stale_ids:
        sess = _walkie_sessions_by_id.pop(sid, None) or {}
        code = str(sess.get("pair_code") or "")
        if code and _walkie_session_id_by_pair_code.get(code) == sid:
            _walkie_session_id_by_pair_code.pop(code, None)
        if sess and not sess.get("closed"):
            _log_event(
                "walkie_session_expired",
                {
                    "session_id": sid,
                    "pair_code": code or None,
                    "flow_run_id": sess.get("flow_run_id"),
                },
                level="warn",
            )


def _walkie_get_session_by_id_locked(session_id: str | None):
    sid = str(session_id or "").strip()
    if not sid:
        return None
    return _walkie_sessions_by_id.get(sid)


def _walkie_auth_locked(session_id: str | None, token: str | None):
    sess = _walkie_get_session_by_id_locked(session_id)
    if not sess:
        return None, None, "session_not_found"

    now_ms = _now_ms()
    expires_at = int(sess.get("expires_at") or 0)
    if sess.get("closed"):
        return None, None, "session_closed"
    if expires_at > 0 and now_ms > expires_at:
        return None, None, "session_expired"

    t = str(token or "").strip()
    if not t:
        return None, None, "missing_token"
    if t == sess.get("receiver_token"):
        return sess, "receiver", None
    if t == sess.get("transmitter_token"):
        return sess, "transmitter", None
    return None, None, "invalid_token"


def _walkie_queue_signal_locked(session: dict[str, Any], target_role: str, signal: dict[str, Any]):
    queues = session.get("signals")
    if not isinstance(queues, dict):
        queues = {"receiver": [], "transmitter": []}
        session["signals"] = queues
    if target_role not in queues:
        queues[target_role] = []
    q = queues[target_role]
    q.append(signal)
    if len(q) > _WALKIE_MAX_SIGNAL_QUEUE:
        del q[: len(q) - _WALKIE_MAX_SIGNAL_QUEUE]


def _extract_flow_run_id_from_obj(obj):
    if not isinstance(obj, dict):
        return None

    for k in ("flow_run_id", "run_id", "runId", "flowRunId"):
        v = obj.get(k)
        if v:
            return str(v)

    entry = obj.get("entry")
    if isinstance(entry, dict):
        v = entry.get("run_id") or entry.get("runId") or entry.get("flow_run_id") or entry.get("flowRunId")
        if v:
            return str(v)
        data = entry.get("data")
        if isinstance(data, dict):
            v = data.get("flow_run_id") or data.get("run_id") or data.get("runId")
            if v:
                return str(v)
        meta = entry.get("meta")
        if isinstance(meta, dict):
            v = meta.get("flow_run_id") or meta.get("run_id") or meta.get("runId")
            if v:
                return str(v)

    meta = obj.get("meta")
    if isinstance(meta, dict):
        v = meta.get("flow_run_id") or meta.get("run_id") or meta.get("runId")
        if v:
            return str(v)

    return None


def _run_file_for_id(run_id, first_ts_ms):
    if run_id in _run_files_by_id:
        return _run_files_by_id[run_id]
    safe = _safe_filename(run_id)
    if re.fullmatch(r"log\d+", safe):
        path = os.path.join(LOGS_DIR, f"{safe}.json")
    else:
        path = os.path.join(LOGS_DIR, f"{safe}-{first_ts_ms}.json")
    _run_files_by_id[run_id] = path
    return path


def _flush_run_to_disk(run_id):
    events = _run_events_by_id.get(run_id) or []
    if not events:
        return
    first_ts = events[0].get("ts") or _now_ms()
    path = _run_file_for_id(run_id, first_ts)

    counts = {}
    last_problem = None
    saw_failure_signal = False
    for e in events:
        lvl = str(e.get("level") or "info").lower()
        counts[lvl] = counts.get(lvl, 0) + 1
        if lvl in ("warn", "warning", "error"):
            last_problem = e
        ev_name = str(e.get("event") or "").lower()
        if "failed" in ev_name or ev_name.endswith("_error") or ev_name.endswith("error"):
            saw_failure_signal = True

    status = "ok"
    if counts.get("error", 0) > 0 or saw_failure_signal:
        status = "failed"
    elif counts.get("warn", 0) > 0 or counts.get("warning", 0) > 0:
        status = "warning"

    out = {
        "run_id": run_id,
        "created_ts": first_ts,
        "updated_ts": _now_ms(),
        "summary": {
            "status": status,
            "counts": counts,
            "last_problem": last_problem,
        },
        "events": events,
    }

    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=True, indent=2)
    os.replace(tmp, path)


def _append_run_event(run_id, event_entry):
    if not run_id or not isinstance(run_id, str):
        return
    if not isinstance(event_entry, dict):
        return
    bucket = _run_events_by_id.get(run_id)
    if bucket is None:
        bucket = []
        _run_events_by_id[run_id] = bucket
    bucket.append(event_entry)
    if len(bucket) > _RUN_EVENTS_MAX:
        del bucket[: len(bucket) - _RUN_EVENTS_MAX]
    try:
        _flush_run_to_disk(run_id)
    except Exception as e:
        print(f"[route] failed to flush run log ({run_id}): {e}")


def _log_event(event, data=None, level="info"):
    entry = {
        "ts": _now_ms(),
        "level": level,
        "event": event,
        "data": data or {}
    }
    _event_log.append(entry)
    if len(_event_log) > _EVENT_LOG_MAX:
        del _event_log[: len(_event_log) - _EVENT_LOG_MAX]
    print("[route_log] " + json.dumps(entry, ensure_ascii=True))
    try:
        run_id = _extract_flow_run_id_from_obj(entry.get("data") or {})
        if run_id:
            _append_run_event(_safe_run_id(run_id), entry)
    except Exception:
        pass


def _safe_book_key(raw):
    raw = (raw or "").strip().lower()
    return re.sub(r"[^a-z0-9_]+", "", raw)


def _resolve_rule_path(book_type):
    key = _safe_book_key(book_type)
    if not key:
        return None

    candidates = [
        f"{key}.txt",
        f"{key.replace('_', '')}.txt",
    ]
    for fname in candidates:
        path = os.path.join(BOOK_RULES_DIR, fname)
        if os.path.isfile(path):
            return path
    return None


def _resolve_kickoff_path(book_type):
    key = _safe_book_key(book_type)
    if not key:
        return None

    key2 = key.replace("_", "")
    candidates = [
        f"{key}_kickoff.txt",
        f"{key}_start.txt",
        f"{key2}_kickoff.txt",
        f"{key2}_start.txt",
    ]
    for fname in candidates:
        path = os.path.join(BOOK_RULES_DIR, fname)
        if os.path.isfile(path):
            return path
    return None


def _read_rule_text(book_type):
    path = _resolve_rule_path(book_type)
    if not path:
        return None

    try:
        mtime = os.path.getmtime(path)
        cached = _rule_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
    except Exception:
        pass

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        print(f"[route] Failed to read rule file {path}: {e}")
        return None

    text = text.strip()
    try:
        _rule_cache[path] = (os.path.getmtime(path), text)
    except Exception:
        pass
    return text or None


def _read_kickoff_text(book_type):
    path = _resolve_kickoff_path(book_type)
    if not path:
        return None

    try:
        mtime = os.path.getmtime(path)
        cached = _rule_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
    except Exception:
        pass

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        print(f"[route] Failed to read kickoff file {path}: {e}")
        return None

    text = text.strip()
    try:
        _rule_cache[path] = (os.path.getmtime(path), text)
    except Exception:
        pass
    return text or None


def _make_id(prefix="msg"):
    return f"{prefix}-{_now_ms()}-{secrets.token_hex(4)}"


def _extract_message_text(message_obj):
    if isinstance(message_obj, str):
        return message_obj
    if not isinstance(message_obj, dict):
        return None

    for key in ("text", "message", "textbook_text"):
        value = message_obj.get(key)
        if isinstance(value, str):
            return value
    return None


def _debug_print_ai_text(stage, sender, receiver, message_obj):
    text = _extract_message_text(message_obj)
    if not isinstance(text, str):
        return

    msg_id = message_obj.get("id") if isinstance(message_obj, dict) else None
    kind = message_obj.get("kind") if isinstance(message_obj, dict) else None
    print(
        f"[route] {stage} {sender} -> {receiver} "
        f"| kind={kind} id={msg_id} text_len={len(text)}"
    )
    print(f"[route] {stage} text START >>>")
    print(text)
    print(f"[route] {stage} text END <<<")


def _enqueue(receiver, sender, message_obj):
    try:
        if receiver == "ai":
            _debug_print_ai_text("enqueue_to_ai", sender, receiver, message_obj)
    except Exception:
        pass

    message_queues_by_role[receiver].append({
        "from": sender,
        "message": message_obj
    })
    try:
        flow_run_id = None
        if isinstance(message_obj, dict):
            meta = message_obj.get("meta")
            if isinstance(meta, dict):
                flow_run_id = meta.get("flow_run_id") or meta.get("run_id") or meta.get("runId")
        _log_event(
            "enqueue",
            {
                "to": receiver,
                "from": sender,
                "queue_len": len(message_queues_by_role.get(receiver, [])),
                "message_id": (message_obj or {}).get("id") if isinstance(message_obj, dict) else None,
                "kind": (message_obj or {}).get("kind") if isinstance(message_obj, dict) else None,
                "flow_run_id": flow_run_id,
            }
        )
    except Exception:
        pass


def _pipeline_upsert_segment(segment_id, **updates):
    if not segment_id:
        return None
    sid = str(segment_id)
    now_ms = _now_ms()

    row = _pipeline_segments_by_id.get(sid)
    if row is None:
        row = {
            "segment_id": sid,
            "created_ts": now_ms,
            "updated_ts": now_ms,
            "flow_run_id": None,
            "text": None,
            "audio_ref": None,
            "status": "created",
            "source_role": None,
            "source_page": None,
            "injected": False,
            "sent_status": None,
        }
        _pipeline_segments_by_id[sid] = row
        _pipeline_segment_order.append(sid)
        if len(_pipeline_segment_order) > _PIPELINE_SEGMENTS_MAX:
            stale = _pipeline_segment_order.pop(0)
            _pipeline_segments_by_id.pop(stale, None)

    for k, v in updates.items():
        if v is not None:
            row[k] = v
    row["updated_ts"] = now_ms

    status = str(row.get("status") or "")
    if status in _pipeline_last_ids:
        _pipeline_last_ids[status] = sid
    sent_status = row.get("sent_status")
    if sent_status in _pipeline_last_ids:
        _pipeline_last_ids[sent_status] = sid
    return row


def _pipeline_recent_segments(limit=200):
    out = []
    safe_limit = max(1, min(2000, int(limit or 200)))
    for sid in _pipeline_segment_order[-safe_limit:]:
        row = _pipeline_segments_by_id.get(sid)
        if row:
            out.append(dict(row))
    return out


def _looks_like_noise(text: str) -> bool:
    t = str(text or "").strip().lower()
    if not t:
        return True

    if len(t) < 2:
        return True

    alnum = re.sub(r"[^a-z0-9]+", "", t)
    if not alnum:
        return True

    if len(set(alnum)) == 1 and len(alnum) >= 5:
        return True

    if re.fullmatch(r"[.?!,\-_/\\*~\s]+", t):
        return True

    return False


def _safe_run_id(run_id: str | None):
    rid = str(run_id or "").strip()
    if rid and not rid.lower().startswith("kickstart"):
        return rid
    if rid:
        mapped = _legacy_run_id_map.get(rid)
        if mapped:
            return mapped
        mapped = _next_auto_run_id()
        _legacy_run_id_map[rid] = mapped
        return mapped
    # Auto-assign when caller did not provide flow_run_id.
    return _next_auto_run_id()


def _get_audio_bridge(ensure=False):
    global _audio_bridge, _audio_bridge_error, _audio_bridge_ready_logged, _audio_bridge_last_ensure_ms
    with _audio_bridge_lock:
        if _audio_bridge is None:
            try:
                from audio_bridge import AudioBridge
                _audio_bridge = AudioBridge(
                    sink_name=CLASS_PULSE_SINK,
                    source_name=STT_PULSE_SOURCE,
                    logs_dir=LOGS_DIR,
                    segment_seconds=AUDIO_SEGMENT_SECONDS,
                )
            except Exception as e:
                _audio_bridge_error = str(e)
                return None

        if ensure:
            now_ms = _now_ms()
            # Retry faster until bridge is ready so Chrome/STT can see the virtual
            # source at startup; back off once healthy.
            min_interval_ms = 4000 if _audio_bridge_ready_logged else 800
            if now_ms - _audio_bridge_last_ensure_ms > min_interval_ms:
                _audio_bridge_last_ensure_ms = now_ms
                try:
                    status = _audio_bridge.ensure_ready()
                    if status.get("ready") and not _audio_bridge_ready_logged:
                        _audio_bridge_ready_logged = True
                        _log_event(
                            "audio_bridge_ready",
                            {
                                "sink_name": status.get("sink_name"),
                                "source_name": status.get("source_name"),
                                "sink_exists": status.get("sink_exists"),
                                "source_exists": status.get("source_exists"),
                            },
                        )
                except Exception as e:
                    _audio_bridge_error = str(e)
        return _audio_bridge


def _capture_audio_for_segment(flow_run_id, segment_id):
    bridge = _get_audio_bridge(ensure=True)
    if bridge is None:
        return None
    try:
        audio_ref = bridge.capture_segment(flow_run_id=flow_run_id, segment_id=segment_id, duration_s=AUDIO_SEGMENT_SECONDS)
        _log_event(
            "audio_segment_captured",
            {
                "flow_run_id": flow_run_id,
                "segment_id": segment_id,
                "audio_ref": audio_ref,
                "source_name": bridge.source_name,
                "sink_name": bridge.sink_name,
            },
        )
        _pipeline_upsert_segment(
            segment_id,
            flow_run_id=flow_run_id,
            audio_ref=audio_ref,
            status="captured",
            sent_status="captured",
        )
        return audio_ref
    except Exception as e:
        _log_event(
            "audio_segment_capture_failed",
            {"flow_run_id": flow_run_id, "segment_id": segment_id, "error": str(e)},
            level="warn",
        )
        return None


def _build_student_response_payload(
    text,
    flow_run_id=None,
    segment_id=None,
    injected=False,
    source_role="stt",
    source_page="speechtexter",
    audio_ref=None,
):
    clean = str(text or "").strip()
    seg_id = str(segment_id or _make_id("seg"))
    run_id = _safe_run_id(flow_run_id)
    now_ms = _now_ms()
    return {
        "id": seg_id,
        "kind": "student_response",
        "text": clean,
        "meta": {
            "flow_run_id": run_id,
            "segment_id": seg_id,
            "source_role": source_role,
            "source_page": source_page,
            "audio_ref": audio_ref,
            "injected": bool(injected),
            "finalized": True,
            "ts_ms": now_ms,
        },
    }


def _handle_student_response(sender, message_obj, injected_by=None):
    if not isinstance(message_obj, dict):
        return {"ok": False, "error": "invalid_student_response_message"}

    text = str(message_obj.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "missing_text"}

    meta = message_obj.get("meta") if isinstance(message_obj.get("meta"), dict) else {}
    segment_id = str(meta.get("segment_id") or message_obj.get("id") or _make_id("seg"))
    flow_run_id = _safe_run_id(meta.get("flow_run_id") or meta.get("run_id") or meta.get("runId"))
    injected = bool(meta.get("injected"))

    if _looks_like_noise(text):
        _pipeline_upsert_segment(
            segment_id,
            flow_run_id=flow_run_id,
            text=text,
            status="dropped",
            source_role=meta.get("source_role") or sender,
            source_page=meta.get("source_page"),
            injected=injected,
            sent_status="dropped",
        )
        _log_event(
            "student_response_dropped_noise",
            {
                "from": sender,
                "flow_run_id": flow_run_id,
                "segment_id": segment_id,
                "text": text,
                "text_len": len(text),
                "injected": injected,
            },
            level="warn",
        )
        return {"ok": True, "dropped": True, "segment_id": segment_id, "flow_run_id": flow_run_id}

    audio_ref = meta.get("audio_ref")
    if not audio_ref:
        audio_ref = _capture_audio_for_segment(flow_run_id, segment_id)

    payload = _build_student_response_payload(
        text=text,
        flow_run_id=flow_run_id,
        segment_id=segment_id,
        injected=injected,
        source_role=meta.get("source_role") or sender,
        source_page=meta.get("source_page") or ("launcher" if injected else "speechtexter"),
        audio_ref=audio_ref,
    )
    if injected_by:
        payload["meta"]["injected_by"] = str(injected_by)

    _pipeline_upsert_segment(
        segment_id,
        flow_run_id=flow_run_id,
        text=text,
        audio_ref=audio_ref,
        status="transcribed",
        source_role=payload["meta"].get("source_role"),
        source_page=payload["meta"].get("source_page"),
        injected=injected,
        sent_status="transcribed",
    )
    _log_event(
        "stt_segment_finalized",
        {
            "from": sender,
            "flow_run_id": flow_run_id,
            "segment_id": segment_id,
            "audio_ref": audio_ref,
            "text": text,
            "text_len": len(text),
            "injected": injected,
        },
    )

    _enqueue("ai", sender, payload)
    _pipeline_upsert_segment(segment_id, status="sent", sent_status="sent")
    _log_event(
        "student_response_sent",
        {
            "from": sender,
            "flow_run_id": flow_run_id,
            "segment_id": segment_id,
            "audio_ref": audio_ref,
            "text": text,
            "text_len": len(text),
            "injected": injected,
        },
    )
    return {
        "ok": True,
        "dropped": False,
        "segment_id": segment_id,
        "flow_run_id": flow_run_id,
        "audio_ref": audio_ref,
        "payload": payload,
    }


def _walkie_endpoint_guard_json():
    # TEMP_WALKIE_MODE: walkie endpoints depend on local TLS for phone mic access.
    if WALKIE_ENABLE_TLS and not _walkie_is_tls_ready():
        return (
            jsonify(
                {
                    "error": "walkie_tls_unavailable",
                    "message": "Walkie TLS is enabled but cert/key is missing or HTTPS server not ready.",
                    "info": _walkie_info_payload(),
                }
            ),
            503,
        )
    return None


def _walkie_endpoint_guard_page(title):
    # TEMP_WALKIE_MODE: explicit HTML notice when TLS is unavailable.
    if WALKIE_ENABLE_TLS and not _walkie_is_tls_ready():
        info = _walkie_info_payload()
        html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:Arial,sans-serif;background:#121317;color:#e8e8ea;padding:24px}}pre{{background:#1b1d24;padding:12px;border-radius:8px;white-space:pre-wrap}}</style>
</head><body>
<h2>Walkie TLS unavailable</h2>
<p>Expected HTTPS cert/key are missing or HTTPS server is not running.</p>
<pre>{json.dumps(info, indent=2)}</pre>
</body></html>"""
        return Response(html, status=503, mimetype="text/html")
    return None


@app.route("/walkie/receiver", methods=["GET"])
def walkie_receiver_page():
    blocked = _walkie_endpoint_guard_page("Walkie Receiver")
    if blocked:
        return blocked
    return send_from_directory(WALKIE_PAGES_DIR, "receiver.html")


@app.route("/walkie/transmitter", methods=["GET"])
def walkie_transmitter_page():
    blocked = _walkie_endpoint_guard_page("Walkie Transmitter")
    if blocked:
        return blocked
    return send_from_directory(WALKIE_PAGES_DIR, "transmitter.html")


@app.route("/walkie/api/info", methods=["GET"])
def walkie_info():
    return jsonify({"status": "ok", "walkie": _walkie_info_payload()}), 200


@app.route("/walkie/api/session/create", methods=["POST"])
def walkie_session_create():
    blocked = _walkie_endpoint_guard_json()
    if blocked:
        return blocked

    payload = request.json or {}
    flow_run_id = _safe_run_id(payload.get("flow_run_id"))
    now_ms = _now_ms()
    ttl_ms = max(10000, int(float(WALKIE_SESSION_TTL_SECONDS or 1800) * 1000))

    with _walkie_lock:
        _walkie_prune_sessions_locked()
        pair_code = None
        for _ in range(40):
            code = _walkie_pair_code()
            if code not in _walkie_session_id_by_pair_code:
                pair_code = code
                break
        if not pair_code:
            return jsonify({"error": "pair_code_generation_failed"}), 500

        session_id = _walkie_make_session_id()
        receiver_token = _walkie_token()
        expires_at = now_ms + ttl_ms
        sess = {
            "session_id": session_id,
            "pair_code": pair_code,
            "flow_run_id": flow_run_id,
            "receiver_token": receiver_token,
            "transmitter_token": None,
            "created_at": now_ms,
            "expires_at": expires_at,
            "closed": False,
            "signals": {"receiver": [], "transmitter": []},
            "last_seen": {"receiver": now_ms, "transmitter": None},
        }
        _walkie_sessions_by_id[session_id] = sess
        _walkie_session_id_by_pair_code[pair_code] = session_id

    _log_event(
        "walkie_session_created",
        {
            "session_id": session_id,
            "pair_code": pair_code,
            "flow_run_id": flow_run_id,
            "expires_at": expires_at,
        },
    )
    base_url = request.host_url.rstrip("/")
    return (
        jsonify(
            {
                "status": "ok",
                "session_id": session_id,
                "pair_code": pair_code,
                "receiver_token": receiver_token,
                "expires_at": expires_at,
                "receiver_url": f"{base_url}/walkie/receiver",
                "transmitter_url": _walkie_lan_transmitter_url(),
                "transmitter_url_with_code": f"{_walkie_lan_transmitter_url()}?pair_code={pair_code}",
                "flow_run_id": flow_run_id,
            }
        ),
        200,
    )


@app.route("/walkie/api/session/join", methods=["POST"])
def walkie_session_join():
    blocked = _walkie_endpoint_guard_json()
    if blocked:
        return blocked

    payload = request.json or {}
    pair_code = str(payload.get("pair_code") or "").strip()
    if not pair_code:
        _walkie_log_rejected("missing_pair_code")
        return jsonify({"error": "missing_pair_code"}), 400

    with _walkie_lock:
        _walkie_prune_sessions_locked()
        session_id = _walkie_session_id_by_pair_code.get(pair_code)
        if not session_id:
            _walkie_log_rejected("pair_code_not_found", pair_code=pair_code)
            return jsonify({"error": "pair_code_not_found"}), 404
        sess = _walkie_sessions_by_id.get(session_id)
        if not sess:
            _walkie_log_rejected("session_not_found", pair_code=pair_code)
            return jsonify({"error": "session_not_found"}), 404

        now_ms = _now_ms()
        expires_at = int(sess.get("expires_at") or 0)
        if sess.get("closed") or (expires_at > 0 and now_ms > expires_at):
            _walkie_log_rejected("session_expired", session_id=session_id, pair_code=pair_code)
            return jsonify({"error": "session_expired"}), 410

        transmitter_token = _walkie_token()
        sess["transmitter_token"] = transmitter_token
        sess.setdefault("last_seen", {})["transmitter"] = now_ms
        flow_run_id = sess.get("flow_run_id")

    _log_event(
        "walkie_session_joined",
        {
            "session_id": session_id,
            "pair_code": pair_code,
            "flow_run_id": flow_run_id,
        },
    )
    return (
        jsonify(
            {
                "status": "ok",
                "session_id": session_id,
                "transmitter_token": transmitter_token,
                "expires_at": expires_at,
                "flow_run_id": flow_run_id,
            }
        ),
        200,
    )


@app.route("/walkie/api/signal/push", methods=["POST"])
def walkie_signal_push():
    blocked = _walkie_endpoint_guard_json()
    if blocked:
        return blocked

    payload = request.json or {}
    session_id = payload.get("session_id")
    token = payload.get("token")
    to_role = str(payload.get("to") or "").strip().lower()
    signal_type = str(payload.get("type") or "").strip().lower()

    if to_role not in ("receiver", "transmitter"):
        _walkie_log_rejected("invalid_to_role", to=to_role, session_id=session_id)
        return jsonify({"error": "invalid_to_role"}), 400
    if signal_type not in _WALKIE_SIGNAL_TYPES:
        _walkie_log_rejected("invalid_signal_type", type=signal_type, session_id=session_id)
        return jsonify({"error": "invalid_signal_type"}), 400

    with _walkie_lock:
        _walkie_prune_sessions_locked()
        sess, role, err = _walkie_auth_locked(session_id, token)
        if err:
            _walkie_log_rejected(err, session_id=session_id, type=signal_type)
            code = 404 if err == "session_not_found" else 401
            return jsonify({"error": err}), code
        if role == to_role:
            _walkie_log_rejected("cannot_signal_same_role", session_id=session_id, role=role, to=to_role)
            return jsonify({"error": "cannot_signal_same_role"}), 400

        signal = {
            "type": signal_type,
            "from": role,
            "to": to_role,
            "payload": payload.get("payload"),
            "ts_ms": _now_ms(),
        }
        _walkie_queue_signal_locked(sess, to_role, signal)
        sess.setdefault("last_seen", {})[role] = signal["ts_ms"]
        flow_run_id = sess.get("flow_run_id")

    event_by_type = {
        "offer": "walkie_signal_offer",
        "answer": "walkie_signal_answer",
        "ptt_state": "walkie_ptt_state",
    }
    event_name = event_by_type.get(signal_type)
    if event_name:
        _log_event(
            event_name,
            {
                "session_id": session_id,
                "flow_run_id": flow_run_id,
                "from_role": role,
                "to_role": to_role,
                "payload": payload.get("payload"),
            },
        )
    return jsonify({"status": "ok"}), 200


@app.route("/walkie/api/signal/pull", methods=["GET"])
def walkie_signal_pull():
    blocked = _walkie_endpoint_guard_json()
    if blocked:
        return blocked

    session_id = request.args.get("session_id")
    token = request.args.get("token")
    timeout_ms_raw = request.args.get("timeout_ms", "25000")
    try:
        timeout_ms = int(timeout_ms_raw)
    except Exception:
        timeout_ms = 25000
    timeout_ms = max(100, min(_WALKIE_PULL_TIMEOUT_MS_MAX, timeout_ms))
    deadline = time.time() + (timeout_ms / 1000.0)

    while True:
        with _walkie_lock:
            _walkie_prune_sessions_locked()
            sess, role, err = _walkie_auth_locked(session_id, token)
            if err:
                _walkie_log_rejected(err, session_id=session_id, action="pull")
                code = 404 if err == "session_not_found" else 401
                return jsonify({"error": err}), code

            signals = sess.setdefault("signals", {}).setdefault(role, [])
            if signals:
                out = list(signals)
                signals.clear()
                sess.setdefault("last_seen", {})[role] = _now_ms()
                return jsonify({"status": "ok", "role": role, "messages": out}), 200

        if time.time() >= deadline:
            return jsonify({"status": "ok", "messages": []}), 200
        time.sleep(0.15)


@app.route("/walkie/api/session/close", methods=["POST"])
def walkie_session_close():
    payload = request.json or {}
    session_id = payload.get("session_id")
    token = payload.get("token")

    with _walkie_lock:
        _walkie_prune_sessions_locked()
        sess, role, err = _walkie_auth_locked(session_id, token)
        if err:
            code = 404 if err == "session_not_found" else 401
            return jsonify({"error": err}), code

        sess["closed"] = True
        code = str(sess.get("pair_code") or "")
        if code and _walkie_session_id_by_pair_code.get(code) == session_id:
            _walkie_session_id_by_pair_code.pop(code, None)
        _walkie_sessions_by_id.pop(str(session_id), None)
        flow_run_id = sess.get("flow_run_id")

    _log_event(
        "walkie_session_closed",
        {"session_id": session_id, "closed_by": role, "flow_run_id": flow_run_id},
    )
    return jsonify({"status": "ok"}), 200


def _expand_lesson_package_to_ai(sender, msg):
    book_type = (msg or {}).get("book_type") or (msg or {}).get("bookType")
    book_type = _safe_book_key(book_type)
    textbook_text = (msg or {}).get("textbook_text") or ""
    meta = (msg or {}).get("meta") or {}

    if not book_type or not isinstance(textbook_text, str) or not textbook_text.strip():
        return False, "invalid_lesson_package"

    package_id = (msg or {}).get("id") or _make_id("pkg")

    rule_text = _read_rule_text(book_type)
    if not rule_text:
        rule_text = f"You are an English teacher. Follow the teaching rules for textbook: {book_type}."

    rule_payload = {
        "id": _make_id("rule"),
        "sender": "system",
        "receiver": "ai",
        "kind": "rule_prompt",
        "book_type": book_type,
        "package_id": package_id,
        "text": rule_text,
        "delay_after_ms": 1000,
        "flags": {
            "special": True,
            "no_return_expected": True
        },
        "meta": meta
    }
    _enqueue("ai", "system", rule_payload)

    content_payload = {
        "id": _make_id("textbook"),
        "sender": "system",
        "receiver": "ai",
        "kind": "textbook_content",
        "book_type": book_type,
        "package_id": package_id,
        "text": textbook_text.strip(),
        "flags": {
            "special": True,
            "no_return_expected": True
        },
        "meta": meta
    }
    _enqueue("ai", "system", content_payload)

    kickoff_text = _read_kickoff_text(book_type)
    if not kickoff_text:
        kickoff_text = (
            "Now greet the student and start teaching using the textbook content above. "
            "Keep it natural and concise. Ask one question to the student."
        )

    kickoff_payload = {
        "id": _make_id("kickoff"),
        "sender": "system",
        "receiver": "ai",
        "kind": "kickoff_prompt",
        "book_type": book_type,
        "package_id": package_id,
        "text": kickoff_text,
        "flags": {
            "special": True
        },
        "meta": meta
    }
    _enqueue("ai", "system", kickoff_payload)

    _log_event(
        "lesson_package_expanded",
        {
            "sender": sender,
            "book_type": book_type,
            "package_id": package_id,
            "flow_run_id": meta.get("flow_run_id") if isinstance(meta, dict) else None,
            "rule_id": rule_payload.get("id"),
            "content_id": content_payload.get("id"),
            "kickoff_id": kickoff_payload.get("id"),
            "text_len": len(textbook_text.strip()),
        }
    )

    return True, package_id


# ================== POST MESSAGE ==================
@app.route("/send_message", methods=["POST"])
def enqueue_message():
    data = request.json or {}
    sender = data.get("from")
    receiver = data.get("to")
    message = data.get("message")

    if not sender or not receiver or message is None:
        _log_event("send_message_invalid", {"sender": sender, "receiver": receiver}, level="warn")
        return jsonify({"error": "Missing 'from', 'to' or 'message'"}), 400

    if receiver not in message_queues_by_role:
        _log_event("send_message_invalid_receiver", {"sender": sender, "receiver": receiver}, level="warn")
        return jsonify({"error": f"Receiver '{receiver}' unknown"}), 400

    try:
        meta = message.get("meta") if isinstance(message, dict) else None
        flow_run_id = meta.get("flow_run_id") if isinstance(meta, dict) else None
        message_text = _extract_message_text(message)
        _log_event(
            "send_message",
            {
                "from": sender,
                "to": receiver,
                "message_id": message.get("id") if isinstance(message, dict) else None,
                "kind": message.get("kind") if isinstance(message, dict) else None,
                "flow_run_id": flow_run_id,
                "text_len": len(message_text) if isinstance(message_text, str) else None,
            }
        )
    except Exception:
        pass

    if receiver == "ai" and isinstance(message, dict):
        kind = str(message.get("kind") or "")
        if kind == "lesson_package":
            _debug_print_ai_text("received_lesson_package", sender, receiver, message)
            ok, info = _expand_lesson_package_to_ai(sender, message)
            if not ok:
                _log_event("lesson_package_expand_failed", {"from": sender, "error": info}, level="warn")
                return jsonify({"error": info}), 400
            print(f"[route] expanded lesson_package from {sender} -> ai | package_id={info}")
            return jsonify({"status": "ok", "expanded": True, "package_id": info}), 200

        if kind == "student_response":
            result = _handle_student_response(sender=sender, message_obj=message)
            if not result.get("ok"):
                return jsonify({"error": result.get("error") or "student_response_failed"}), 400
            return jsonify({
                "status": "ok",
                "kind": "student_response",
                "dropped": bool(result.get("dropped")),
                "segment_id": result.get("segment_id"),
                "flow_run_id": result.get("flow_run_id"),
                "audio_ref": result.get("audio_ref"),
            }), 200

    print(f"[route] {sender=} -> {receiver=} | message={message}")
    _enqueue(receiver, sender, message)
    return jsonify({"status": "ok"}), 200


@app.route("/inject/student_text", methods=["POST"])
def inject_student_text():
    payload = request.json or {}
    text = str(payload.get("text") or "").strip()
    flow_run_id = _safe_run_id(payload.get("flow_run_id"))
    injected_by = str(payload.get("injected_by") or "launcher")
    if not text:
        return jsonify({"error": "Missing text"}), 400

    msg = _build_student_response_payload(
        text=text,
        flow_run_id=flow_run_id,
        segment_id=_make_id("seg"),
        injected=True,
        source_role="stt",
        source_page="launcher_inject_text",
    )
    msg["meta"]["injected_by"] = injected_by

    result = _handle_student_response(sender="launcher", message_obj=msg, injected_by=injected_by)
    if not result.get("ok"):
        return jsonify({"error": result.get("error") or "inject_text_failed"}), 400

    _log_event(
        "injection_text_sent",
        {
            "flow_run_id": result.get("flow_run_id"),
            "segment_id": result.get("segment_id"),
            "text": text,
            "text_len": len(text),
            "injected_by": injected_by,
            "dropped": bool(result.get("dropped")),
        },
    )
    return jsonify(
        {
            "status": "ok",
            "segment_id": result.get("segment_id"),
            "flow_run_id": result.get("flow_run_id"),
            "audio_ref": result.get("audio_ref"),
            "dropped": bool(result.get("dropped")),
        }
    ), 200


@app.route("/inject/student_audio", methods=["POST"])
def inject_student_audio():
    payload = request.json or {}
    wav_path = str(payload.get("wav_path") or "").strip()
    flow_run_id = _safe_run_id(payload.get("flow_run_id"))
    injected_by = str(payload.get("injected_by") or "launcher")
    if not wav_path:
        return jsonify({"error": "Missing wav_path"}), 400

    abs_path = os.path.abspath(wav_path)
    if not os.path.isfile(abs_path):
        return jsonify({"error": f"wav_path_not_found: {abs_path}"}), 400

    bridge = _get_audio_bridge(ensure=True)
    if bridge is None:
        err = _audio_bridge_error or "audio_bridge_unavailable"
        return jsonify({"error": err}), 503

    status = bridge.ensure_ready()
    if not status.get("ready"):
        return jsonify({"error": "audio_bridge_not_ready", "status": status}), 503

    play = bridge.play_wav(abs_path)
    level = "info" if play.get("ok") else "warn"
    segment_id = _make_id("inj-audio")
    _pipeline_upsert_segment(
        segment_id,
        flow_run_id=flow_run_id,
        audio_ref=abs_path,
        status="captured",
        source_role="launcher",
        source_page="launcher_inject_audio",
        injected=True,
        sent_status="captured",
    )
    _log_event(
        "injection_audio_played",
        {
            "ok": bool(play.get("ok")),
            "error": play.get("error"),
            "play_id": play.get("play_id"),
            "flow_run_id": flow_run_id,
            "segment_id": segment_id,
            "wav_path": abs_path,
            "injected_by": injected_by,
            "sink_name": status.get("sink_name"),
            "source_name": status.get("source_name"),
        },
        level=level,
    )
    code = 200 if play.get("ok") else 500
    return jsonify({"status": "ok" if play.get("ok") else "error", "result": play, "segment_id": segment_id}), code


@app.route("/pipeline_status", methods=["GET"])
def pipeline_status():
    global _audio_bridge_ready_logged
    bridge = _get_audio_bridge(ensure=True)
    if bridge is not None:
        try:
            bridge_status = bridge.status()
            if bridge_status.get("ready") and not _audio_bridge_ready_logged:
                _audio_bridge_ready_logged = True
                _log_event(
                    "audio_bridge_ready",
                    {
                        "sink_name": bridge_status.get("sink_name"),
                        "source_name": bridge_status.get("source_name"),
                        "sink_exists": bridge_status.get("sink_exists"),
                        "source_exists": bridge_status.get("source_exists"),
                    },
                )
        except Exception as e:
            bridge_status = {"ready": False, "error": str(e)}
    else:
        bridge_status = {"ready": False, "error": _audio_bridge_error or "audio_bridge_unavailable"}

    limit = request.args.get("limit", "200")
    try:
        limit_n = int(limit)
    except Exception:
        limit_n = 200

    out = {
        "status": "ok",
        "audio_bridge": bridge_status,
        "roles": list(message_queues_by_role.keys()),
        "queues": {role: len(queue) for role, queue in message_queues_by_role.items()},
        "last_segment_ids": dict(_pipeline_last_ids),
        "segments": _pipeline_recent_segments(limit=limit_n),
        "ts": _now_ms(),
    }
    return jsonify(out), 200


@app.route("/log_event", methods=["POST"])
def log_event():
    payload = request.json or {}
    source = payload.get("source") or "unknown"

    entry = payload.get("entry")
    if isinstance(entry, dict):
        level = entry.get("level") or "info"
        run_id = entry.get("run_id")
        if not run_id:
            data = entry.get("data")
            if isinstance(data, dict):
                run_id = data.get("flow_run_id") or data.get("run_id") or data.get("runId")
        _log_event("client_log_entry", {"source": source, "flow_run_id": run_id, "entry": entry}, level=level)
        return jsonify({"status": "ok"}), 200

    event = payload.get("event") or "event"
    level = payload.get("level") or "info"
    data = payload.get("data") or {}
    _log_event("client_event", {"source": source, "event": event, "level": level, "data": data})
    return jsonify({"status": "ok"}), 200


# ================== GET MESSAGES ==================
@app.route("/get_messages/<receiver>", methods=["GET"])
def dequeue_messages(receiver):
    if receiver not in message_queues_by_role:
        return jsonify({"messages": [], "status": "unknown"}), 400
    messages = message_queues_by_role[receiver].copy()
    message_queues_by_role[receiver].clear()
    print(f"[route] get_messages for {receiver}: {len(messages)} messages")
    _log_event("get_messages", {"receiver": receiver, "count": len(messages)})
    return jsonify({"messages": messages}), 200


@app.route("/get_logs", methods=["GET"])
def get_logs():
    clear = request.args.get("clear") == "1"
    out = list(_event_log)
    if clear:
        _event_log.clear()
    return jsonify({"events": out}), 200


def _start_https_mirror_server():
    global _walkie_tls_ready
    if not WALKIE_ENABLE_TLS:
        _walkie_tls_ready = False
        _log_event(
            "walkie_info",
            {
                **_walkie_info_payload(),
                "note": "WALKIE_ENABLE_TLS is disabled; HTTPS mirror not started.",
            },
            level="warn",
        )
        return None

    cert_ok = bool(WALKIE_TLS_CERT_PATH and os.path.isfile(WALKIE_TLS_CERT_PATH))
    key_ok = bool(WALKIE_TLS_KEY_PATH and os.path.isfile(WALKIE_TLS_KEY_PATH))
    if not cert_ok or not key_ok:
        _walkie_tls_ready = False
        _log_event(
            "walkie_info",
            {
                **_walkie_info_payload(),
                "note": "TLS cert/key missing; walkie endpoints unavailable until files exist.",
            },
            level="error",
        )
        return None

    def worker():
        global _walkie_tls_ready
        ssl_context = (WALKIE_TLS_CERT_PATH, WALKIE_TLS_KEY_PATH)
        try:
            server = make_server("0.0.0.0", int(WALKIE_TLS_PORT), app, threaded=True, ssl_context=ssl_context)
            _walkie_tls_ready = True
            _log_event(
                "walkie_info",
                {
                    **_walkie_info_payload(),
                    "note": "HTTPS mirror started for walkie pages/signaling.",
                },
            )
            server.serve_forever()
        except Exception as e:
            _walkie_tls_ready = False
            _log_event(
                "walkie_info",
                {
                    **_walkie_info_payload(),
                    "note": "HTTPS mirror failed to start.",
                    "error": str(e),
                },
                level="error",
            )

    t = threading.Thread(target=worker, daemon=True, name="walkie_https_server")
    t.start()
    return t


# ================== RUN ==================
if __name__ == "__main__":
    # Pre-create virtual sink/source before Selenium opens STT page so the
    # browser can discover the configured microphone device immediately.
    _get_audio_bridge(ensure=True)
    _start_https_mirror_server()
    _log_event("walkie_info", _walkie_info_payload())
    app.run(host="0.0.0.0", port=5000, threaded=True)
