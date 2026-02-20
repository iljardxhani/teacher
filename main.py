import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

from config import CLASS_WALKIE_MODE, ROUTER_HOST, ROUTER_PORT


def _is_tcp_port_open(host, port, timeout=0.4):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
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
        blob = " ".join([proc.stdout or "", proc.stderr or ""])
        for token in blob.replace("/", " ").replace(":", " ").split():
            if token.isdigit():
                pids.add(int(token))
    return sorted(pids)


def _terminate_port_listener(port, timeout_s=4.0):
    pids = _listening_pids_for_port(port)
    if not pids:
        return True

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    deadline = time.time() + max(0.1, float(timeout_s))
    while time.time() < deadline:
        if not _is_tcp_port_open("127.0.0.1", port):
            return True
        time.sleep(0.15)

    for pid in _listening_pids_for_port(port):
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    time.sleep(0.2)
    return not _is_tcp_port_open("127.0.0.1", port)


def _router_supports_walkie():
    url = f"http://{ROUTER_HOST}:{ROUTER_PORT}/walkie/api/info"
    try:
        with urllib.request.urlopen(url, timeout=1.2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return isinstance(data, dict) and isinstance(data.get("walkie"), dict)
    except Exception:
        return False


def _start_router():
    if _is_tcp_port_open(ROUTER_HOST, ROUTER_PORT):
        if _router_supports_walkie():
            print(f"[main] Router already listening on {ROUTER_HOST}:{ROUTER_PORT} (walkie endpoints detected).")
            return None
        print(f"[main] Port {ROUTER_PORT} is occupied by stale router/service (walkie missing). Restarting...")
        if not _terminate_port_listener(ROUTER_PORT, timeout_s=4.0):
            raise RuntimeError(f"Could not free port {ROUTER_PORT}")

    proc = subprocess.Popen(
        [sys.executable, "-u", "route.py"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.time() + 6.0
    while time.time() < deadline:
        if _is_tcp_port_open(ROUTER_HOST, ROUTER_PORT):
            print("🚀 Router server launched!")
            break
        if proc.poll() is not None:
            raise RuntimeError("route.py exited early during startup")
        time.sleep(0.2)

    if not _is_tcp_port_open(ROUTER_HOST, ROUTER_PORT):
        print("[main] Warning: router port did not open within timeout.")
    if CLASS_WALKIE_MODE and not _router_supports_walkie():
        print("[main] Warning: walkie endpoints are still unavailable; class walkie page may 404.")

    return proc


if __name__ == "__main__":
    _start_router()
    try:
        from prepare import launch_environment
    except Exception as exc:
        raise SystemExit(
            "Failed importing Selenium launcher. Run inside project venv: "
            "`source .venv/bin/activate && python main.py`\n"
            f"Details: {exc}"
        )
    launch_environment()
    print("System ready.")
