"""
led_monitor.py — polls hub conditions every N seconds and drives the LED daemon.

Thresholds and timing are configured in jtak.yaml under the `led:` key.

Conditions monitored:
  overtemp          cpu_temp_c >= led.overtemp_c          → fire preset
  low_disk          disk_free_gb < led.low_disk_gb         → yellow blink slow
  no_internet       internet == False                      → red fade slow
  fire_nearby       WFIGS perimeter within led.fire_radius_nm → red spin fast
  gps_locked        hub_position set, HDOP < 2.0           → LED6 blue blink slow
  gps_fair          hub_position set, HDOP 2.0–5.0         → LED6 amber blink medium
  gps_poor          hub_position set, HDOP > 5.0            → LED6 orange blink fast
  no_gps            hub_position is null                   → LED6 orange blink slow
"""

import asyncio
import json
import math
from pathlib import Path

import aiohttp

import led_client
from utils.config import get

AIRCRAFT_JSON  = Path("/run/dump1090/aircraft.json")
API_BASE       = "http://127.0.0.1:8420/jtak/api/api"

# ── Thresholds from jtak.yaml ─────────────────────────────────────────────────
def _cfg():
    return {
        "enabled":       get("led.enabled",          True),
        "poll_interval": get("led.poll_interval_sec", 15),
        "overtemp_c":    get("led.overtemp_c",        80),    # fire preset
        "low_disk_gb":   get("led.low_disk_gb",        2),    # yellow blink slow
        "fire_radius_nm":get("led.fire_radius_nm",    50),    # red spin fast
    }

# track previous states to only send on change
_prev: dict = {}


def _changed(key: str, val: bool) -> bool:
    if _prev.get(key) == val:
        return False
    _prev[key] = val
    return True


def _haversine_nm(lat1, lon1, lat2, lon2) -> float:
    R = 3440.065  # Earth radius in nautical miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _check_aircraft() -> bool:
    try:
        d = json.loads(AIRCRAFT_JSON.read_text())
        return any(a.get("seen", 999) < 60 for a in d.get("aircraft", []))
    except Exception:
        return False


async def _check_fire(hub_lat, hub_lon, radius_nm, session) -> bool:
    try:
        async with session.get(f"{API_BASE}/fire/perimeters",
                               timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json()
        for feat in data.get("features", []):
            coords = feat.get("geometry", {}).get("coordinates", [])
            if coords and coords[0]:
                ring  = coords[0]
                c_lat = sum(p[1] for p in ring) / len(ring)
                c_lon = sum(p[0] for p in ring) / len(ring)
                if _haversine_nm(hub_lat, hub_lon, c_lat, c_lon) <= radius_nm:
                    return True
    except Exception:
        pass
    return False


async def run():
    await asyncio.sleep(10)   # let jTAK finish startup
    print("[LED monitor] started")

    async with aiohttp.ClientSession() as session:
        while True:
            cfg = _cfg()

            if not cfg["enabled"]:
                await asyncio.sleep(cfg["poll_interval"])
                continue

            try:
                async with session.get(f"{API_BASE}/status",
                                       timeout=aiohttp.ClientTimeout(total=5)) as r:
                    status = await r.json()

                # ── GPS ── LED6 color reflects HDOP quality
                # Always resend every poll (no change guard) so daemon re-syncs after restart
                hub_pos  = status.get("hub_position")
                hub_hdop = status.get("hub_hdop")
                if hub_pos:
                    if hub_hdop is None or hub_hdop < 2.0:
                        gps_state = "gps_locked"   # LED6 blue blink slow
                    elif hub_hdop < 5.0:
                        gps_state = "gps_fair"     # LED6 amber blink medium
                    else:
                        gps_state = "gps_poor"     # LED6 orange blink fast
                else:
                    gps_state = "no_gps"           # LED6 orange blink slow
                led_client.set_state(gps_state)
                # Ring: orange spin while no GPS fix; clear once acquired
                if _changed("gps_ring_spin", gps_state == "no_gps"):
                    if gps_state == "no_gps":
                        led_client.set_state("orange_spin")
                    else:
                        led_client.clear_state("orange_spin")

                # ── Internet ── red fade slow
                internet = status.get("internet", True)
                if _changed("no_internet", not internet):
                    if not internet:
                        led_client.set_state("no_internet")   # red fade slow
                    else:
                        led_client.clear_state("no_internet")

                # ── Overtemp ── fire preset
                temp = status.get("cpu_temp_c") or 0
                hot  = temp >= cfg["overtemp_c"]
                if _changed("overtemp", hot):
                    if hot:
                        led_client.set_state("overtemp")      # fire preset
                    else:
                        led_client.clear_state("overtemp")

                # ── Low disk ── yellow blink slow
                disk = status.get("disk_free_gb") or 999
                low  = disk < cfg["low_disk_gb"]
                if _changed("low_disk", low):
                    if low:
                        led_client.set_state("low_disk")      # yellow blink slow
                    else:
                        led_client.clear_state("low_disk")

                # ── Fire nearby ── red spin fast
                if hub_pos:
                    fire = await _check_fire(
                        hub_pos["latitude"], hub_pos["longitude"],
                        cfg["fire_radius_nm"], session
                    )
                    if _changed("fire_nearby", fire):
                        if fire:
                            led_client.set_state("fire_nearby")     # red spin fast
                        else:
                            led_client.clear_state("fire_nearby")

                # ── Lightning nearby ── white blink fast (within 25 mi)
                try:
                    async with session.get(f"{API_BASE}/lightning",
                                           timeout=aiohttp.ClientTimeout(total=5)) as r:
                        ldata = await r.json()
                    lightning = ldata.get("alert", False)
                except Exception:
                    lightning = False
                if _changed("lightning_nearby", lightning):
                    if lightning:
                        led_client.set_state("lightning_nearby")
                    else:
                        led_client.clear_state("lightning_nearby")

            except Exception as e:
                print(f"[LED monitor] poll error: {e}")

            await asyncio.sleep(cfg["poll_interval"])
