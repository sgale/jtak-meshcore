"""
Lightning — real-time strike relay from Blitzortung.org
Architecture: persistent WebSocket to Blitzortung, bounding box ≈ ±5° around hub (≈350-mi box).
Strikes filtered to ≤100 mi haversine. Rolling 30-min cache with per-strike timestamp.
Reconnects automatically with exponential backoff.

Exposes:
  GET /api/lightning  → {strikes: [...], nearest_mi, alert (bool), connected}
"""

import asyncio
import json
import math
import random
import time

import websockets
from fastapi import APIRouter
from ingest.csv_watcher import hub_position

router = APIRouter()

STRIKE_TTL_S    = 30 * 60      # keep strikes 30 min
MAX_RADIUS_MI   = 100          # filter beyond this
ALERT_RADIUS_MI = 25           # LED alert threshold
BOX_DEG         = 5.0          # ± degrees for WS subscription (≈350-mi box)
WS_HOSTS        = [f"ws{i}.blitzortung.org" for i in range(1, 9)]

# ── Shared state ──────────────────────────────────────────────────────────────
_strikes: list[dict] = []      # {lat, lon, ts, dist_mi}
_ws_connected: bool  = False
_ws_host: str | None = None
alert_active: bool   = False   # True when nearest strike ≤ ALERT_RADIUS_MI


# ── Haversine ─────────────────────────────────────────────────────────────────
def _dist_mi(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# ── Background WS task ────────────────────────────────────────────────────────
async def _run_ws():
    global _ws_connected, _ws_host, alert_active
    backoff = 5.0

    while True:
        # Wait until we have GPS
        lat = hub_position.get("latitude")
        lon = hub_position.get("longitude")
        if lat is None or lon is None:
            await asyncio.sleep(15)
            continue

        host = random.choice(WS_HOSTS)
        uri  = f"wss://{host}/websocket"
        bbox = {
            "west":  round(lon - BOX_DEG, 3),
            "east":  round(lon + BOX_DEG, 3),
            "north": round(lat + BOX_DEG, 3),
            "south": round(lat - BOX_DEG, 3),
        }

        try:
            async with websockets.connect(
                uri,
                open_timeout=10,
                ping_interval=30,
                ping_timeout=15,
                additional_headers={"Origin": "https://map.blitzortung.org"},
            ) as ws:
                await ws.send(json.dumps(bbox))
                _ws_connected = True
                _ws_host = host
                backoff = 5.0
                print(f"[lightning] connected to {host}")

                async for raw in ws:
                    now = time.time()

                    # Prune stale strikes
                    _strikes[:] = [s for s in _strikes if now - s["ts"] < STRIKE_TTL_S]

                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    # Blitzortung sends strike objects with "lat" and "lon"
                    # Time is nanoseconds epoch
                    s_lat = msg.get("lat")
                    s_lon = msg.get("lon")
                    if s_lat is None or s_lon is None:
                        continue

                    # Recheck hub position each strike (hub may have moved)
                    h_lat = hub_position.get("latitude")
                    h_lon = hub_position.get("longitude")
                    if h_lat is None:
                        continue

                    dist = _dist_mi(h_lat, h_lon, s_lat, s_lon)
                    if dist > MAX_RADIUS_MI:
                        continue

                    _strikes.append({
                        "lat":     round(s_lat, 4),
                        "lon":     round(s_lon, 4),
                        "ts":      now,
                        "dist_mi": round(dist, 1),
                    })

                    # Recompute alert
                    alert_active = any(s["dist_mi"] <= ALERT_RADIUS_MI for s in _strikes)

        except Exception as e:
            _ws_connected = False
            _ws_host = None
            print(f"[lightning] {host} error: {e!r} — retry in {backoff:.0f}s")

        await asyncio.sleep(backoff)
        backoff = min(backoff * 1.5, 120)


async def start_lightning():
    asyncio.create_task(_run_ws())


# ── REST endpoint ─────────────────────────────────────────────────────────────
@router.get("/lightning")
async def lightning():
    now = time.time()
    fresh = [s for s in _strikes if now - s["ts"] < STRIKE_TTL_S]

    nearest = None
    nearest_mi = None
    if fresh:
        s = min(fresh, key=lambda x: x["dist_mi"])
        nearest_mi = s["dist_mi"]
        nearest = {
            **s,
            "age_s": round(now - s["ts"]),
        }

    return {
        "connected":   _ws_connected,
        "ws_host":     _ws_host,
        "strike_count": len(fresh),
        "strikes": [
            {**s, "age_s": round(now - s["ts"])}
            for s in sorted(fresh, key=lambda x: x["ts"], reverse=True)
        ],
        "nearest_mi":  nearest_mi,
        "nearest":     nearest,
        "alert":       alert_active,
        "alert_radius_mi": ALERT_RADIUS_MI,
        "max_radius_mi":   MAX_RADIUS_MI,
    }
