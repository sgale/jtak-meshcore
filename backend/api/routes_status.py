import asyncio
import json
import shutil
import socket
import subprocess
import psutil
import ipaddress
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from utils.config import get
from utils.identity import get_identity
from ingest.csv_watcher import latest_hub_env, hub_position
from ingest.gps_logger import TAG_PATH, LOG_PATH
from ingest.sdr_logger import TAG_PATH as SDR_TAG_PATH, LOG_PATH as SDR_LOG_PATH

# Shared GPS state file — read by tactical_monitor.py
_GPS_STATE_PATH = Path("/opt/jtak/data/jtak-gps.json")

router = APIRouter()

_cached_sats: int | None = None
_cached_hdop: float | None = None

# MeshCore hub GPS status — written by ingest/meshcore_monitor.py, which owns the
# serial GPS on this hub (gpsd is not used). Header sat count / HDOP come from here.
_MC_GPS_PATH = Path("/opt/jtak/data/meshcore-gps.json")


async def _poll_gps_sats():
    """Background task: refresh sat count + HDOP + position from the MeshCore hub GPS
    status file (meshcore_monitor publishes it ~every 2 min)."""
    global _cached_sats, _cached_hdop
    while True:
        try:
            data = json.loads(_MC_GPS_PATH.read_text())
            _cached_sats = data.get("sats")
            _cached_hdop = data.get("hdop")
            hub_position["sats"] = _cached_sats
            if data.get("fix") and data.get("lat") is not None:
                hub_position["latitude"]  = data["lat"]
                hub_position["longitude"] = data["lon"]
            else:
                # No fix — clear stale position so the dashboard shows current reality.
                hub_position["latitude"]  = None
                hub_position["longitude"] = None
        except FileNotFoundError:
            pass  # meshcore service not up yet
        except Exception:
            pass
        await asyncio.sleep(10)



async def _check_internet() -> bool:
    """Non-blocking check: can we reach 8.8.8.8:53 (Google DNS)?"""
    loop = asyncio.get_event_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: socket.create_connection(("8.8.8.8", 53), timeout=1.5).close()
            ),
            timeout=2.0,
        )
        return True
    except Exception:
        return False

def _get_zt_ip() -> str | None:
    """Return first IPv4 address on a ZeroTier interface (zt*)."""
    for iface, addrs in psutil.net_if_addrs().items():
        if iface.startswith("zt"):
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    return addr.address
    return None


@router.get("/status")
async def status():
    cpu, internet = await asyncio.gather(
        asyncio.get_event_loop().run_in_executor(None, lambda: psutil.cpu_percent(interval=0.1)),
        _check_internet(),
    )
    mem = psutil.virtual_memory()
    disk = shutil.disk_usage("/opt/jtak/data")

    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            cpu_temp_c = round(int(f.read().strip()) / 1000, 1)
    except Exception:
        cpu_temp_c = None

    # External BME sensor readings
    bme_temp_c = latest_hub_env.get("hub_temp_c")
    bme_temp_f = round(bme_temp_c * 9/5 + 32, 1) if bme_temp_c is not None else None
    bme_pres   = latest_hub_env.get("hub_pressure_hpa")

    ident = get_identity()
    return {
        "hub_id":         ident.get("hub_id")    or get("hub.id"),
        "hub_name":       ident.get("hub_name")  or get("hub.name"),
        "hub_short_name": ident.get("hub_short") or get("hub.short_name", ""),
        "hub_guid":       ident.get("guid", ""),
        "role":           get("hub.role"),
        "time":      datetime.utcnow().isoformat() + "Z",
        # System
        "cpu_pct":      cpu,
        "cpu_temp_c":   cpu_temp_c,
        "mem_pct":      mem.percent,
        "disk_free_gb": round(disk.free / 1073741824),
        # External BME680
        "bme_temp_c":        bme_temp_c,
        "bme_temp_f":        bme_temp_f,
        "bme_humidity_pct":  latest_hub_env.get("hub_humidity_pct"),
        "bme_pressure_hpa":  bme_pres,
        "bme_iaq_pct":       latest_hub_env.get("hub_iaq_pct"),
        "bme_smoke_alert":   latest_hub_env.get("hub_smoke_alert", False),
        # This hub's own GPS position
        "hub_position": hub_position if hub_position.get("latitude") else None,
        "hub_sats":     _cached_sats,
        "hub_hdop":     _cached_hdop,
        # HUD chip order/visibility (from jtak.yaml hud.chips)
        "hud_chips": get("hud.chips",
            ["hub","atmo","wind","fire_data","aircraft","fire_spread","ember_spot","measure","waypoint","zones","hq_feed"]),
        # Sidebar panel visibility (from jtak.yaml sidebar.panels)
        "sidebar_panels": get("sidebar.panels",
            ["zones","nodes","rf","health","sensors","atmo","weather","tilecache","waypoints","mesh","log"]),
        # Sound notifications (from jtak.yaml sounds)
        "sounds": {
            "enabled":         get("sounds.enabled",         True),
            "volume":          get("sounds.volume",          0.6),
            "direct_message":  get("sounds.direct_message",  "chime"),
            "channel_message": get("sounds.channel_message", "ping"),
            "waypoint":        get("sounds.waypoint",        "drop"),
        },
        # Connectivity
        "meshtastic_debug": get("meshtastic.debug", False),
        "internet": internet,
        "zt_ip":    _get_zt_ip(),
        # Units metadata
        "meta": {
            "units": {
                "cpu_pct":      "%",
                "cpu_temp_c":   "°C",
                "mem_pct":      "%",
                "disk_free_gb": "GB",
                "bme_temp_c":   "°C",
                "bme_temp_f":   "°F",
                "bme_humidity_pct": "%",
                "bme_iaq_pct":  "%",
            }
        },
    }


class _TagBody(BaseModel):
    tag: str


@router.get("/gps-bakeoff/tag")
def get_gps_tag():
    tag = TAG_PATH.read_text().strip() if TAG_PATH.exists() else "untagged"
    return {"tag": tag}


@router.post("/gps-bakeoff/tag")
def set_gps_tag(body: _TagBody):
    tag = body.tag.strip()[:64]
    if not tag:
        raise HTTPException(status_code=400, detail="tag cannot be empty")
    TAG_PATH.parent.mkdir(parents=True, exist_ok=True)
    TAG_PATH.write_text(tag)
    return {"tag": tag, "log": str(LOG_PATH)}


@router.get("/sdr-bakeoff/tag")
def get_sdr_tag():
    tag = SDR_TAG_PATH.read_text().strip() if SDR_TAG_PATH.exists() else "untagged"
    return {"tag": tag}


@router.post("/sdr-bakeoff/tag")
def set_sdr_tag(body: _TagBody):
    tag = body.tag.strip()[:64]
    if not tag:
        raise HTTPException(status_code=400, detail="tag cannot be empty")
    SDR_TAG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SDR_TAG_PATH.write_text(tag)
    return {"tag": tag, "log": str(SDR_LOG_PATH)}


# ── Timezone ──────────────────────────────────────────────────────────────────

_YAML_PATH = Path("/opt/jtak/config/jtak.yaml")
_DEFAULT_TZ = "America/Denver"

class _TzBody(BaseModel):
    timezone: str

@router.get("/settings/timezone")
def get_timezone():
    return {
        "timezone":      get("timezone",       _DEFAULT_TZ) or _DEFAULT_TZ,
        "posix_timezone": get("posix_timezone", "MST7MDT,M3.2.0,M11.1.0") or "MST7MDT,M3.2.0,M11.1.0",
    }

class _PosixTzBody(BaseModel):
    posix_timezone: str

@router.post("/settings/posix-timezone")
def set_posix_timezone(body: _PosixTzBody):
    import re
    tz = body.posix_timezone.strip()
    if not tz:
        raise HTTPException(400, "posix_timezone cannot be empty")
    lines = _YAML_PATH.read_text().splitlines(keepends=True)
    new_lines = []
    replaced = False
    for line in lines:
        if re.match(r'^posix_timezone\s*:', line):
            new_lines.append(f'posix_timezone: {tz}\n')
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f'posix_timezone: {tz}\n')
    _YAML_PATH.write_text(''.join(new_lines))
    return {"posix_timezone": tz}

@router.post("/settings/timezone")
def set_timezone(body: _TzBody):
    import re
    tz = body.timezone.strip()
    if not re.match(r'^[A-Za-z_]+/[A-Za-z_/]+$|^UTC$', tz):
        raise HTTPException(400, "Invalid timezone")
    # Validate it's a real timezone
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:
        raise HTTPException(400, f"Unknown timezone: {tz}")
    # Write to jtak.yaml — update existing key or append
    lines = _YAML_PATH.read_text().splitlines(keepends=True)
    new_lines = []
    replaced = False
    for line in lines:
        if re.match(r'^timezone\s*:', line):
            new_lines.append(f'timezone: {tz}\n')
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f'timezone: {tz}\n')
    _YAML_PATH.write_text(''.join(new_lines))
    return {"timezone": tz}
