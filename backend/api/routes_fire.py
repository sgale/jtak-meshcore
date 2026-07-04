"""
Fire data — WFIGS perimeters + NASA FIRMS hotspots.

GET /api/fire/perimeters   — NIFC/WFIGS current wildland fire perimeters (GeoJSON)
GET /api/fire/hotspots     — NASA FIRMS VIIRS active fire detections (GeoJSON)

Both endpoints are cached server-side and require internet (internet toggle gate
is enforced on the frontend, not here).  WFIGS needs no key.  FIRMS needs a free
API key configured at fire.firms_api_key in jtak.yaml.
"""

import asyncio
import time
import csv
import io
import httpx
from fastapi import APIRouter, HTTPException
from utils.config import get

router = APIRouter()

# ── WFIGS — ESRI USA Wildfires v1 (publicly accessible, updated daily) ────────
# Layer 0 = Current_Incidents (point, has containment/state/acres)
# Layer 1 = Current_Perimeters (polygon geometry, used for map rendering)
WFIGS_PERIM_URL = (
    "https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services"
    "/USA_Wildfires_v1/FeatureServer/1/query"
)
WFIGS_PERIM_PARAMS = {
    "where": "1=1",
    "outFields": "IncidentName,GISAcres,CreateDate,DateCurrent,IncidentTypeCategory,GACC",
    "outSR": "4326",
    "f": "geojson",
}

WFIGS_INC_URL = (
    "https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services"
    "/USA_Wildfires_v1/FeatureServer/0/query"
)
WFIGS_INC_PARAMS = {
    "where": "1=1",
    "outFields": "IncidentName,PercentContained,POOState,FireDiscoveryDateTime,DailyAcres,IrwinID",
    "outSR": "4326",
    "f": "json",
}
WFIGS_TTL = get("fire.wfigs_cache_min", 30) * 60
_wfigs_cache: dict = {}

# ── FIRMS ─────────────────────────────────────────────────────────────────────
FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
FIRMS_TTL  = get("fire.firms_cache_min", 15) * 60
_firms_cache: dict = {}


# ── WFIGS endpoint ────────────────────────────────────────────────────────────
@router.get("/fire/perimeters")
async def fire_perimeters():
    """Current wildland fire perimeters from ESRI USA Wildfires v1. No API key required."""
    cached = _wfigs_cache.get("data")
    if cached and (time.time() - _wfigs_cache.get("ts", 0)) < WFIGS_TTL:
        return {**cached, "cached": True, "cache_age_min": int((time.time() - _wfigs_cache["ts"]) / 60)}

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        # Fetch perimeter polygons and incident metadata in parallel
        perim_r, inc_r = await asyncio.gather(
            client.get(WFIGS_PERIM_URL, params=WFIGS_PERIM_PARAMS),
            client.get(WFIGS_INC_URL,   params=WFIGS_INC_PARAMS),
        )

    if perim_r.status_code != 200:
        raise HTTPException(502, f"WFIGS perimeters: HTTP {perim_r.status_code}")

    geojson = perim_r.json()

    # Build lookup: incident name → incident metadata (containment, state, discovery)
    inc_lookup: dict = {}
    if inc_r.status_code == 200:
        try:
            inc_data = inc_r.json()
            for feat in inc_data.get("features", []):
                p = feat.get("attributes", {})
                name = (p.get("IncidentName") or "").strip().upper()
                if name:
                    inc_lookup[name] = p
        except Exception:
            pass

    # Normalise properties for consistent frontend consumption
    for feat in geojson.get("features", []):
        p = feat.get("properties", {})
        name = (p.get("IncidentName") or "Unknown Fire").strip()
        inc = inc_lookup.get(name.upper(), {})
        feat["properties"] = {
            "name":          name,
            "acres":         round(inc.get("DailyAcres") or p.get("GISAcres") or 0),
            "contained_pct": inc.get("PercentContained"),
            "discovered":    inc.get("FireDiscoveryDateTime"),
            "state":         inc.get("POOState") or p.get("GACC", ""),
            "updated":       p.get("DateCurrent"),
            "type":          p.get("IncidentTypeCategory", "Wildfire"),
        }

    data = {**geojson, "cached": False, "cache_age_min": 0}
    _wfigs_cache["data"] = data
    _wfigs_cache["ts"]   = time.time()
    return data


# ── FIRMS endpoint ────────────────────────────────────────────────────────────
@router.get("/fire/hotspots")
async def fire_hotspots(
    lat: float = get("map.default_center", [40.5729, -111.9941])[0],
    lon: float = get("map.default_center", [40.5729, -111.9941])[1],
):
    """
    NASA FIRMS VIIRS_SNPP_NRT active fire hotspots.
    Requires fire.firms_api_key in jtak.yaml.
    Returns GeoJSON FeatureCollection of thermal anomalies.
    """
    api_key = get("fire.firms_api_key", "")
    if not api_key:
        raise HTTPException(503, "NASA FIRMS API key not configured — add fire.firms_api_key to jtak.yaml")

    bbox_deg = get("fire.firms_bbox_deg", 1.5)
    days     = get("fire.firms_days", 1)

    # Bounding box: W,S,E,N
    bbox = f"{lon - bbox_deg},{lat - bbox_deg},{lon + bbox_deg},{lat + bbox_deg}"

    cache_key = (round(lat, 1), round(lon, 1))
    cached = _firms_cache.get(cache_key)
    if cached and (time.time() - cached["ts"]) < FIRMS_TTL:
        return {**cached["data"], "cached": True, "cache_age_min": int((time.time() - cached["ts"]) / 60)}

    use_modis = get("fire.firms_modis", True)
    urls = [f"{FIRMS_BASE}/{api_key}/VIIRS_SNPP_NRT/{bbox}/{days}"]
    if use_modis:
        urls.append(f"{FIRMS_BASE}/{api_key}/MODIS_NRT/{bbox}/{days}")

    async with httpx.AsyncClient(timeout=20.0) as client:
        responses = await asyncio.gather(*[client.get(u) for u in urls], return_exceptions=True)

    # Combine CSV text from all sensors; skip any that errored
    combined_text = ""
    header = None
    for r in responses:
        if isinstance(r, Exception) or r.status_code != 200:
            continue
        t = r.text.strip()
        if not t or t.startswith("Error"):
            continue
        lines = t.splitlines()
        if not header:
            header = lines[0]
            combined_text += t + "\n"
        else:
            # Skip duplicate header line from subsequent sensors
            combined_text += "\n".join(lines[1:]) + "\n"

    if not combined_text or not header:
        raise HTTPException(502, "NASA FIRMS: no data returned from any sensor")

    text = combined_text

    features = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        try:
            flat = float(row["latitude"])
            flon = float(row["longitude"])
        except (KeyError, ValueError):
            continue

        confidence = row.get("confidence", "n").lower()   # l / n / h  or 0-100
        # Normalise to low/nominal/high label
        if confidence in ("l", "low"):
            conf_label = "low"
        elif confidence in ("h", "high"):
            conf_label = "high"
        elif confidence.isdigit():
            v = int(confidence)
            conf_label = "low" if v < 40 else "high" if v >= 80 else "nominal"
        else:
            conf_label = "nominal"

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [flon, flat]},
            "properties": {
                "brightness":  _safe_float(row.get("bright_ti4") or row.get("brightness")),
                "scan":        _safe_float(row.get("scan")),
                "track":       _safe_float(row.get("track")),
                "acq_date":    row.get("acq_date"),
                "acq_time":    row.get("acq_time"),
                "satellite":   row.get("satellite"),
                "instrument":  row.get("instrument", ""),
                "confidence":  conf_label,
                "frp":         _safe_float(row.get("frp")),   # Fire Radiative Power (MW)
            },
        })

    data = {
        "type": "FeatureCollection",
        "features": features,
        "cached": False,
        "cache_age_min": 0,
        "count": len(features),
    }
    _firms_cache[cache_key] = {"ts": time.time(), "data": data}
    return data


def _safe_float(v):
    try:
        return round(float(v), 2) if v not in (None, "", "nan") else None
    except (ValueError, TypeError):
        return None
