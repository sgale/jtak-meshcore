"""
Federation routes — start/stop the jtak-push agent and report its status.
GET  /api/federation  → { enabled, running, hub_id, hq_url }
POST /api/federation  → { enabled: bool }  start or stop jtak-push.service
"""

import asyncio
import subprocess
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from utils.config import get

router = APIRouter()


def _service_active() -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "jtak-push"],
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False


async def _set_service(enable: bool) -> bool:
    action = "start" if enable else "stop"
    loop = asyncio.get_event_loop()
    try:
        r = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["sudo", "systemctl", action, "jtak-push"],
                    capture_output=True, text=True
                )
            ),
            timeout=10.0,
        )
        return r.returncode == 0
    except Exception:
        return False


@router.get("/federation")
async def get_federation():
    return {
        "enabled": _service_active(),
        "hub_id":  get("hq.hub_id", ""),
        "hq_url":  get("hq.url", ""),
    }


class FedToggle(BaseModel):
    enabled: bool


@router.post("/federation")
async def set_federation(body: FedToggle):
    ok = await _set_service(body.enabled)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to toggle jtak-push service")
    # Brief pause so systemctl has time to change state
    await asyncio.sleep(1.0)
    return {"enabled": _service_active()}
