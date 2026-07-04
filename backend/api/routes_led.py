"""
LED control API
POST /api/led/brightness  { "brightness": 0-100 }
GET  /api/led/brightness  → { "brightness": N }
POST /api/led/beacon      { "active": true|false }
GET  /api/led/beacon      → { "active": bool }
"""

import sys
sys.path.insert(0, "/opt/jtak/backend")

from fastapi import APIRouter
from pydantic import BaseModel
import led_client

router = APIRouter()

_current_brightness = 100   # in-memory cache so GET reflects last set value
_beacon_active      = False


class BrightnessBody(BaseModel):
    brightness: int


class BeaconBody(BaseModel):
    active: bool


@router.get("/led/brightness")
async def get_led_brightness():
    global _current_brightness
    actual = led_client.get_brightness()
    if actual is not None:
        _current_brightness = actual
    return {"brightness": _current_brightness}


@router.post("/led/brightness")
async def set_led_brightness(body: BrightnessBody):
    global _current_brightness
    pct = max(0, min(100, body.brightness))
    _current_brightness = pct
    led_client.set_brightness(pct)
    return {"brightness": pct}


@router.get("/led/beacon")
async def get_beacon():
    return {"active": _beacon_active}


@router.post("/led/beacon")
async def set_beacon(body: BeaconBody):
    global _beacon_active
    _beacon_active = body.active
    if body.active:
        led_client.set_state("beacon")
    else:
        led_client.clear_state("beacon")
    return {"active": _beacon_active}
