"""
ATMO — Atmospheric intelligence endpoint
Fetches forecast data from Open-Meteo (no API key, free, NOAA HRRR/GFS) for the hub's
GPS location and computes:
  - Lightning risk score (0-100) from CAPE + Lifted Index + precip probability
  - Temperature inversion detection (850hPa vs 2m surface)
  - Precipitation probability 2-hour window
  - Cloud cover at 3 levels
  - 850hPa cloud-steering wind (direction + speed)

Result is cached for CACHE_TTL seconds; updates lazily when GPS position is available.
"""

import asyncio
import time
import math
import httpx
from fastapi import APIRouter, HTTPException
from ingest.csv_watcher import hub_position   # shared GPS dict

router = APIRouter()

CACHE_TTL    = 20 * 60   # 20 minutes — serve fresh data
STALE_TTL    = 60 * 60   # 60 minutes — serve stale while refreshing in background
ERROR_TTL    =  2 * 60   # 2 minutes — don't hammer a failing upstream
_cache: dict | None = None
_cache_ts: float = 0.0
_cache_pos: tuple | None = None   # (lat, lon) of last fetch
_refreshing: bool = False          # background refresh in flight
_last_error_ts: float = 0.0        # timestamp of last upstream failure

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

HOURLY_VARS = ",".join([
    "precipitation_probability",
    "cloudcover",
    "cloudcover_low",
    "cloudcover_mid",
    "cloudcover_high",
    "cape",
    "lifted_index",
    "temperature_2m",
    "temperature_925hPa",
    "temperature_850hPa",
    "temperature_700hPa",
    "windspeed_850hPa",
    "winddirection_850hPa",
])


def _cardinal(deg: float) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[round(deg / 22.5) % 16]


def _lightning_score(cape: float | None, li: float | None, precip_pct: float | None) -> int:
    """
    Composite lightning risk score 0-100.
    CAPE:   0→500→1000→2000+ J/kg
    LI:     >0=stable, 0→-3=unstable, -3→-6=severe, <-6=extreme
    Precip: 0-100 %
    """
    cape_s  = min(40.0, (cape or 0) / 2000 * 40)
    li_s    = min(40.0, max(0.0, -(li or 2) / 6 * 40))
    prec_s  = (precip_pct or 0) / 100 * 20
    return round(min(100, max(0, cape_s + li_s + prec_s)))


def _risk_label(score: int) -> str:
    if score < 20:  return "LOW"
    if score < 45:  return "MODERATE"
    if score < 70:  return "HIGH"
    return "EXTREME"


def _risk_color(score: int) -> str:
    if score < 20:  return "#2ecc71"   # green
    if score < 45:  return "#e6a817"   # amber
    if score < 70:  return "#e85d04"   # orange
    return "#e74c3c"                   # red


def _inversion(t2m: float | None, t925: float | None) -> dict | None:
    """
    Standard atmosphere lapse rate is ~6.5°C/km.
    925hPa ≈ ~750m above sea level (at MSL). Expected delta ≈ -4.9°C.
    If actual delta > -2°C (less cooling than expected), a low-level inversion likely exists.
    """
    if t2m is None or t925 is None:
        return None
    delta = t925 - t2m          # positive means 925hPa is WARMER than surface
    expected = -4.9             # expected lapse to ~750m
    inversion = delta > (expected + 2.5)   # 2.5°C margin
    return {
        "detected":   inversion,
        "delta_c":    round(delta, 1),
        "expected_c": expected,
    }


async def _fetch(lat: float, lon: float) -> dict:
    params = {
        "latitude":    lat,
        "longitude":   lon,
        "hourly":      HOURLY_VARS,
        "forecast_days": 1,
        "timezone":    "UTC",
        "models":      "gfs_seamless",
    }
    async with httpx.AsyncClient(timeout=6.0) as client:
        r = await client.get(OPEN_METEO, params=params)
        if r.status_code != 200:
            raise HTTPException(502, f"Open-Meteo API: {r.status_code}")
        data = r.json()

    hourly = data.get("hourly", {})
    times  = hourly.get("time", [])
    if not times:
        raise HTTPException(502, "Open-Meteo: no hourly data returned")

    # Find current hour index
    now_utc = time.gmtime()
    now_str = f"{now_utc.tm_year}-{now_utc.tm_mon:02d}-{now_utc.tm_mday:02d}T{now_utc.tm_hour:02d}:00"
    try:
        idx = times.index(now_str)
    except ValueError:
        idx = 0

    def _get(key, i):
        vals = hourly.get(key, [])
        return vals[i] if i < len(vals) else None

    # 2-hr window: current hour + next 2
    window = [idx, min(idx + 1, len(times) - 1), min(idx + 2, len(times) - 1)]

    precip_now   = _get("precipitation_probability", idx)
    precip_2hr   = max((_get("precipitation_probability", i) or 0) for i in window)
    cape_now     = _get("cape", idx)
    li_now       = _get("lifted_index", idx)
    t2m          = _get("temperature_2m", idx)
    t925         = _get("temperature_925hPa", idx)
    t850         = _get("temperature_850hPa", idx)
    t700         = _get("temperature_700hPa", idx)
    cc_total     = _get("cloudcover", idx)
    cc_low       = _get("cloudcover_low", idx)
    cc_mid       = _get("cloudcover_mid", idx)
    cc_high      = _get("cloudcover_high", idx)
    ws_850       = _get("windspeed_850hPa", idx)   # km/h from Open-Meteo
    wd_850       = _get("winddirection_850hPa", idx)

    score = _lightning_score(cape_now, li_now, precip_2hr)

    # 850hPa ≈ 5,000 ft (cloud steering layer)
    ws_850_mph = round(ws_850 * 0.621371, 1) if ws_850 is not None else None

    inv = _inversion(t2m, t925)

    # Forecast window labels
    hour_labels = []
    for i in window:
        if i < len(times):
            hour_labels.append(times[i][-5:])   # "HH:MM"

    return {
        "source":        "Open-Meteo (GFS Seamless)",
        "fetched_at":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hours_shown":   hour_labels,
        # Lightning
        "lightning_score": score,
        "lightning_risk":  _risk_label(score),
        "lightning_color": _risk_color(score),
        "cape_jkg":       round(cape_now) if cape_now is not None else None,
        "lifted_index":   round(li_now, 1) if li_now is not None else None,
        # Precipitation
        "precip_pct_now": precip_now,
        "precip_pct_2hr": round(precip_2hr),
        # Clouds
        "cloudcover_pct":      cc_total,
        "cloudcover_low_pct":  cc_low,
        "cloudcover_mid_pct":  cc_mid,
        "cloudcover_high_pct": cc_high,
        # 850hPa cloud steering wind
        "wind_850hPa_mph":     ws_850_mph,
        "wind_850hPa_dir_deg": round(wd_850) if wd_850 is not None else None,
        "wind_850hPa_cardinal": _cardinal(wd_850) if wd_850 is not None else None,
        # Temperature profile
        "temp_2m_c":    round(t2m,  1) if t2m  is not None else None,
        "temp_925hPa_c": round(t925, 1) if t925 is not None else None,
        "temp_850hPa_c": round(t850, 1) if t850 is not None else None,
        "temp_700hPa_c": round(t700, 1) if t700 is not None else None,
        # Inversion
        "inversion": inv,
    }


async def _background_refresh(lat: float, lon: float):
    global _cache, _cache_ts, _cache_pos, _refreshing, _last_error_ts
    try:
        data = await _fetch(lat, lon)
        _cache     = data
        _cache_ts  = time.time()
        _cache_pos = (round(lat, 3), round(lon, 3))
    except Exception:
        _last_error_ts = time.time()
    finally:
        _refreshing = False


@router.get("/atmo")
async def atmo():
    global _cache, _cache_ts, _cache_pos, _refreshing, _last_error_ts

    lat = hub_position.get("latitude")
    lon = hub_position.get("longitude")
    if lat is None or lon is None:
        raise HTTPException(503, "Hub GPS position not yet available")

    pos = (round(lat, 3), round(lon, 3))
    age = time.time() - _cache_ts

    # Fresh cache — return immediately
    if _cache and age < CACHE_TTL and _cache_pos == pos:
        return {**_cache, "cached": True, "cache_age_s": round(age)}

    # Stale cache — return immediately and kick off background refresh
    if _cache and age < STALE_TTL and not _refreshing:
        _refreshing = True
        asyncio.create_task(_background_refresh(lat, lon))
        return {**_cache, "cached": True, "stale": True, "cache_age_s": round(age)}

    # Upstream recently failed — don't retry yet
    if time.time() - _last_error_ts < ERROR_TTL:
        raise HTTPException(503, "Atmospheric data temporarily unavailable")

    # No cache or too old — must wait for fresh data
    if not _refreshing:
        try:
            data = await _fetch(lat, lon)
            _cache     = data
            _cache_ts  = time.time()
            _cache_pos = pos
            return {**data, "cached": False, "cache_age_s": 0}
        except Exception:
            _last_error_ts = time.time()
            raise HTTPException(503, "Atmospheric data temporarily unavailable")

    # Refresh already in flight and no usable cache
    raise HTTPException(503, "Atmospheric data not yet available")
