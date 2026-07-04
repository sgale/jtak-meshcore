"""
NOAA Weather — proxy endpoint so the frontend makes one local call.

Flow:
  1. /api/weather?lat=X&lon=Y
  2. GET api.weather.gov/points/{lat},{lon}  → grid metadata + stations URL
  3. GET {stations_url}                       → list of nearby obs stations
  4. GET {station[0]}/observations/latest     → current conditions

Results are cached for CACHE_TTL seconds — NOAA observations update ~hourly.
"""

import asyncio
import time
import math
import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

CACHE_TTL = 300          # 5 min — NOAA updates hourly, no need to hammer it
_cache: dict = {}        # key = (lat_r, lon_r) → {"ts": float, "data": dict}
NOAA_HEADERS = {"User-Agent": "jTAK/1.0 (field mesh dashboard; contact: ops@jtak.local)"}


def _deg_to_cardinal(deg: float) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[round(deg / 22.5) % 16]


async def _noaa_fetch(lat: float, lon: float) -> dict:
    async with httpx.AsyncClient(headers=NOAA_HEADERS, timeout=8.0) as client:
        # Step 1 — grid lookup
        pts = await client.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
        if pts.status_code != 200:
            raise HTTPException(502, f"NOAA points API: {pts.status_code}")
        pts_j = pts.json()
        props = pts_j.get("properties", {})
        stations_url = props.get("observationStations")
        location_name = props.get("relativeLocation", {}).get("properties", {}).get("city", "")

        if not stations_url:
            raise HTTPException(502, "NOAA: no observation stations URL returned")

        # Step 2 — nearest station
        stns = await client.get(stations_url)
        if stns.status_code != 200:
            raise HTTPException(502, f"NOAA stations API: {stns.status_code}")
        features = stns.json().get("features", [])
        if not features:
            raise HTTPException(502, "NOAA: no stations near this location")

        # Step 3 — try stations in order until one has current observations
        obs = None
        station_name = ""
        for feat in features[:5]:
            sid = feat["id"]
            sname = feat["properties"].get("name", "")
            r = await client.get(f"{sid}/observations/latest")
            if r.status_code == 200 and "properties" in r.json():
                obs = r
                station_name = sname
                break
        if obs is None:
            raise HTTPException(502, "NOAA: no stations with current observations")
        o = obs.json().get("properties", {})

    def _val(key):
        v = o.get(key, {})
        return v.get("value") if isinstance(v, dict) else None

    wind_dir_deg  = _val("windDirection")    # degrees FROM
    wind_spd_ms   = _val("windSpeed")        # m/s
    wind_gust_ms  = _val("windGust")         # m/s
    temp_c        = _val("temperature")
    humidity      = _val("relativeHumidity")
    baro_pa       = _val("barometricPressure")
    visibility_m  = _val("visibility")
    desc          = o.get("textDescription", "")
    obs_time      = o.get("timestamp", "")

    # Unit conversions — NOAA returns km/h for wind speed
    wind_spd_mph  = round(wind_spd_ms  * 0.621371, 1) if wind_spd_ms  is not None else None
    wind_gust_mph = round(wind_gust_ms * 0.621371, 1) if wind_gust_ms is not None else None
    wind_spd_kts  = round(wind_spd_ms  * 0.539957, 1) if wind_spd_ms  is not None else None
    temp_f        = round(temp_c * 9/5 + 32, 1)      if temp_c       is not None else None
    baro_hpa      = round(baro_pa / 100, 1)           if baro_pa      is not None else None
    wind_cardinal = _deg_to_cardinal(wind_dir_deg)    if wind_dir_deg is not None else None

    return {
        "source":        "NOAA",
        "station":       station_name,
        "location":      location_name,
        "observed_at":   obs_time,
        # Wind
        "wind_dir_deg":  round(wind_dir_deg, 1) if wind_dir_deg is not None else None,
        "wind_cardinal": wind_cardinal,
        "wind_spd_mph":  wind_spd_mph,
        "wind_spd_kts":  wind_spd_kts,
        "wind_gust_mph": wind_gust_mph,
        # Conditions
        "temp_c":        round(temp_c, 1)  if temp_c   is not None else None,
        "temp_f":        temp_f,
        "humidity_pct":  round(humidity, 0) if humidity is not None else None,
        "baro_hpa":      baro_hpa,
        "visibility_m":  round(visibility_m) if visibility_m is not None else None,
        "description":   desc,
    }


@router.get("/weather")
async def weather(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
):
    # Round to ~1km for cache key
    key = (round(lat, 2), round(lon, 2))
    cached = _cache.get(key)
    if cached and (time.time() - cached["ts"]) < CACHE_TTL:
        return {**cached["data"], "cached": True}

    data = await _noaa_fetch(lat, lon)
    _cache[key] = {"ts": time.time(), "data": data}
    return {**data, "cached": False}
