"""
IAP (Incident Action Plans) — Phase 1

GET /api/iap/incidents       — active wildfire incidents near hub GPS (IRWIN ArcGIS)
GET /api/iap/frequencies     — Great Basin 2025 frequency guide (static JSON)
GET /api/iap/trauma-centers  — Utah region trauma centers (static JSON)
"""

import json
import time
import math
import httpx
from fastapi import APIRouter, Query
from pathlib import Path
from ingest.csv_watcher import hub_position

router = APIRouter()

# ── NIFC WFIGS — current wildfire incidents (public, no auth required) ────────
# Old IRWIN URL (services3.arcgis.com/T4QMspbfLg3qTGWY) now requires token auth
IRWIN_URL = (
    "https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services"
    "/USA_Wildfires_v1/FeatureServer/0/query"
)
IRWIN_TTL  = 15 * 60   # 15 min cache
STATE_TTL  = 24 * 3600  # reverse geocode once per day
_irwin_cache: dict = {}
_state_cache: dict = {}

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
STATIC_DIR = Path(__file__).parent.parent / "data" / "iap"


async def _hub_state(lat: float, lon: float, client) -> str | None:
    """Reverse geocode hub GPS → US state abbreviation (e.g. 'US-UT'). Cached 24h."""
    key = f"{round(lat, 1)},{round(lon, 1)}"
    cached = _state_cache.get(key)
    if cached and (time.time() - cached["ts"]) < STATE_TTL:
        return cached["state"]
    try:
        r = await client.get(CENSUS_URL, params={
            "x": lon, "y": lat,
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "format": "json",
        }, timeout=8)
        states = r.json().get("result", {}).get("geographies", {}).get("States", [])
        abbr = states[0].get("STUSAB") if states else None
        state = f"US-{abbr}" if abbr else None
        _state_cache[key] = {"state": state, "ts": time.time()}
        return state
    except Exception:
        return None


def _haversine_mi(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@router.get("/iap/incidents")
async def iap_incidents(demo: bool = Query(False)):
    """Active wildfire incidents from IRWIN/ESRI, filtered near hub GPS position.
    Pass ?demo=true to load 2025 demo incidents for UI testing."""
    hub_lat = hub_position.get("latitude")
    hub_lon = hub_position.get("longitude")

    if demo:
        p = STATIC_DIR / "demo_incidents_2025.json"
        incidents = json.loads(p.read_text()) if p.exists() else []
        for inc in incidents:
            if hub_lat and hub_lon and inc.get("lat") and inc.get("lon"):
                inc["dist_mi"] = round(_haversine_mi(hub_lat, hub_lon, inc["lat"], inc["lon"]), 1)
        incidents.sort(key=lambda x: -(x.get("acres_daily") or x.get("acres_discovery") or 0))
        return {"hub_lat": hub_lat, "hub_lon": hub_lon, "incidents": incidents, "cached_age_s": 0, "demo": True}

    state_filter = None
    cached = _irwin_cache.get("data")
    if cached and (time.time() - _irwin_cache.get("ts", 0)) < IRWIN_TTL:
        features = cached
    else:
        state_filter = None
        if hub_lat and hub_lon:
            async with httpx.AsyncClient(timeout=10) as sc:
                state_filter = await _hub_state(hub_lat, hub_lon, sc)
        where = f"POOState='{state_filter}'" if state_filter else "1=1"

        params = {
            "where": where,
            "outFields": (
                "IrwinID,IncidentName,IncidentTypeCategory,DiscoveryAcres,DailyAcres,"
                "PercentContained,POOState,POOCounty,FireDiscoveryDateTime,"
                "ModifiedOnDateTime,GACC,FireMgmtComplexity"
            ),
            "orderByFields": "DailyAcres DESC",
            "resultRecordCount": 200,
            "outSR": "4326",
            "f": "json",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(IRWIN_URL, params=params)
                r.raise_for_status()
                data = r.json()
            features = data.get("features", [])
            _irwin_cache["data"] = features
            _irwin_cache["ts"]   = time.time()
        except Exception as e:
            features = cached or []

    # Annotate with distance from hub
    result = []
    for f in features:
        att = f.get("attributes", {})
        geo = f.get("geometry", {})
        inc_lat = geo.get("y") if geo else None
        inc_lon = geo.get("x") if geo else None

        dist_mi = None
        if hub_lat and hub_lon and inc_lat and inc_lon:
            dist_mi = round(_haversine_mi(hub_lat, hub_lon, inc_lat, inc_lon), 1)

        result.append({
            "irwin_id":        att.get("IrwinID"),
            "name":            att.get("IncidentName", "Unknown"),
            "type":            att.get("IncidentTypeCategory", "WF"),
            "acres_discovery": att.get("DiscoveryAcres"),
            "acres_daily":     att.get("DailyAcres"),
            "contained_pct":   att.get("PercentContained"),
            "state":           att.get("POOState"),
            "county":          att.get("POOCounty"),
            "dispatch_center": att.get("GACC"),
            "complexity":      att.get("FireMgmtComplexity"),
            "discovery_ts":    att.get("FireDiscoveryDateTime"),
            "modified_ts":     att.get("ModifiedOnDateTime"),
            "lat":             inc_lat,
            "lon":             inc_lon,
            "dist_mi":         dist_mi,
        })

    # Sort by acres descending; distance is informational only
    result.sort(key=lambda x: -(x.get("acres_daily") or x.get("acres_discovery") or 0))

    return {
        "hub_lat":     hub_lat,
        "hub_lon":     hub_lon,
        "hub_state":   state_filter,
        "incidents":   result,
        "cached_age_s": int(time.time() - _irwin_cache.get("ts", time.time())),
    }


@router.get("/iap/frequencies")
async def iap_frequencies():
    """Great Basin 2025 fire frequency guide (static)."""
    p = STATIC_DIR / "gb_frequencies_2025.json"
    if p.exists():
        import json
        return json.loads(p.read_text())
    return {"error": "frequency data not found"}


@router.get("/iap/trauma-centers")
async def iap_trauma_centers():
    """Utah region Level 1/2 trauma centers (static)."""
    p = STATIC_DIR / "ut_trauma_centers.json"
    if p.exists():
        import json
        return json.loads(p.read_text())
    return {"error": "trauma center data not found"}
