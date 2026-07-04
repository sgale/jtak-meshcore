"""
led_client.py — non-blocking LED daemon client for jTAK backend.
Sends JSON commands to /run/jtak-led.sock. Silently no-ops if daemon is absent.
"""

import json
import socket
import os

SOCKET_PATH = "/run/jtak-led.sock"


def _send(msg: dict):
    if not os.path.exists(SOCKET_PATH):
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(SOCKET_PATH)
            s.sendall(json.dumps(msg).encode())
    except Exception:
        pass


def event(name: str):
    """Fire a transient event (lora_message, new_node, …)."""
    _send({"event": name})


def set_state(state: str):
    """Assert a persistent state."""
    _send({"state": state, "action": "add"})


def clear_state(state: str):
    """Remove a persistent state."""
    _send({"state": state, "action": "remove"})


def set_brightness(pct: int):
    """Set LED brightness 0–100. 0 turns off entirely."""
    _send({"brightness": max(0, min(100, int(pct)))})


def get_brightness() -> int | None:
    """Query actual brightness from daemon. Returns 0-100 or None if unavailable."""
    if not os.path.exists(SOCKET_PATH):
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(SOCKET_PATH)
            s.sendall(json.dumps({"query": "brightness"}).encode())
            s.shutdown(socket.SHUT_WR)
            data = s.recv(64)
            return json.loads(data).get("brightness")
    except Exception:
        return None
