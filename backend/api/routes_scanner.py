"""
routes_scanner.py — Serves passive Meshtastic scanner state.

Reads /opt/jtak/data/scanner_state.json written by jtak-scanner.service
and returns it as JSON. Returns empty state if scanner is not running.
"""

import json
import time
from pathlib import Path
from fastapi import APIRouter

STATE_FILE = Path("/opt/jtak/data/scanner_state.json")

router = APIRouter()


@router.get("/scanner")
async def get_scanner():
    if not STATE_FILE.exists():
        return {
            "running":       False,
            "preset":        None,
            "start_time":    None,
            "total_packets": 0,
            "updated":       None,
            "nodes":         [],
            "recent":        [],
        }
    try:
        data = json.loads(STATE_FILE.read_text())
        age  = time.time() - data.get("updated", 0)
        data["running"] = age < 30   # stale if no update in 30s
        return data
    except Exception:
        return {"running": False, "nodes": [], "recent": [], "total_packets": 0}
