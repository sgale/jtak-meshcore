"""
routes_meshtastic_debug.py — Meshtastic debug mode toggle

Stops/starts tactical_monitor so the Meshtastic phone app can connect freely.
Only exposed when meshtastic.debug: true in jtak.yaml.

Endpoints:
  GET  /api/meshtastic/debug        — current state
  POST /api/meshtastic/debug        — {"enabled": true} stops tactical_monitor
                                       {"enabled": false} starts it
"""

import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from utils.config import get

router = APIRouter()

SERVICE = "tactical_monitor"


async def _systemctl(action: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "sudo", "systemctl", action, SERVICE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    return proc.returncode, out.decode().strip()


async def _is_active() -> bool:
    rc, _ = await _systemctl("is-active")
    return rc == 0


@router.get("/meshtastic/debug")
async def get_debug_state():
    if not get("meshtastic.debug", False):
        raise HTTPException(404)
    active = await _is_active()
    return {
        "debug_enabled":  not active,   # debug ON = tactical_monitor stopped
        "monitor_active": active,
    }


class DebugRequest(BaseModel):
    enabled: bool   # True = enter debug mode (stop monitor), False = exit (start monitor)


@router.post("/meshtastic/debug")
async def set_debug_state(req: DebugRequest):
    if not get("meshtastic.debug", False):
        raise HTTPException(404)

    action = "stop" if req.enabled else "start"
    rc, out = await _systemctl(action)

    if rc != 0:
        raise HTTPException(500, f"systemctl {action} failed: {out}")

    # Brief settle
    await asyncio.sleep(1)
    active = await _is_active()
    return {
        "debug_enabled":  not active,
        "monitor_active": active,
        "action":         action,
    }
