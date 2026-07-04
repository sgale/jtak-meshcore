"""
Fire Spread — elevation + slope endpoint.

GET /api/firespread?lat=X&lon=Y

Samples a 3×3 grid of elevation points (~200 m spacing) via opentopodata
(SRTM30m, free, no key).  Returns slope angle, aspect, and center elevation.
Results cached 5 min — terrain doesn't change.
"""

import math
import time
import httpx
from fastapi import APIRouter, Query, HTTPException

router = APIRouter()

TOPO_URL  = "https://api.opentopodata.org/v1/srtm30m"
CACHE_TTL = 300
_cache: dict = {}

CELL_M          = 200.0
M_PER_DEG_LAT   = 111_320.0


def _grid_offsets(lat: float):
    dlat = CELL_M / M_PER_DEG_LAT
    dlon = CELL_M / (M_PER_DEG_LAT * math.cos(math.radians(lat)))
    return dlat, dlon


def _build_locations(lat: float, lon: float):
    """
    3×3 grid, row-major NW→SE:
        [0]NW [1]N  [2]NE
        [3]W  [4]C  [5]E
        [6]SW [7]S  [8]SE
    """
    dlat, dlon = _grid_offsets(lat)
    locs = []
    for row in range(3):
        rlat = lat + (1 - row) * dlat   # row 0 = north (+dlat)
        for col in range(3):
            rlon = lon + (col - 1) * dlon
            locs.append((rlat, rlon))
    return locs


async def _fetch_elevations(locations):
    loc_str = "|".join(f"{la:.6f},{lo:.6f}" for la, lo in locations)
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(TOPO_URL, params={"locations": loc_str})
    if r.status_code != 200:
        raise HTTPException(502, f"opentopodata HTTP {r.status_code}")
    body = r.json()
    if body.get("status") != "OK":
        raise HTTPException(502, f"opentopodata: {body.get('status')}")
    elevs = []
    for res in body["results"]:
        e = res.get("elevation")
        if e is None:
            raise HTTPException(502, "opentopodata returned null elevation (ocean/void cell)")
        elevs.append(float(e))
    return elevs


def _horn(elevs, cell_m: float):
    """
    Horn's method on a 3×3 grid ordered NW→SE (row 0 = north).

    dz_dx     = east-west gradient   (+ = rises going east)
    dz_north  = north-south gradient (+ = rises going north)
    downslope = compass bearing water flows downhill (0=N, 90=E)
    upslope   = (downslope + 180) % 360
    """
    e0,e1,e2,e3,e4,e5,e6,e7,e8 = elevs

    dz_dx     = ((e2 + 2*e5 + e8) - (e0 + 2*e3 + e6)) / (8 * cell_m)
    dz_dy_raw = ((e6 + 2*e7 + e8) - (e0 + 2*e1 + e2)) / (8 * cell_m)
    dz_north  = -dz_dy_raw   # flip: raster rows increase going south

    rise_run  = math.sqrt(dz_dx**2 + dz_north**2)
    slope_deg = math.degrees(math.atan(rise_run))

    if rise_run < 1e-9:
        downslope_deg = 0.0
    else:
        downslope_deg = (math.degrees(math.atan2(dz_dx, dz_north)) + 180) % 360

    upslope_deg = (downslope_deg + 180) % 360
    return round(slope_deg, 2), round(downslope_deg, 1), round(upslope_deg, 1)


@router.get("/firespread")
async def firespread(
    lat: float = Query(...),
    lon: float = Query(...),
):
    key = (round(lat, 3), round(lon, 3))
    cached = _cache.get(key)
    if cached and (time.time() - cached["ts"]) < CACHE_TTL:
        return {**cached["data"], "cached": True}

    locations = _build_locations(lat, lon)
    elevs     = await _fetch_elevations(locations)

    slope_deg, downslope_deg, upslope_deg = _horn(elevs, CELL_M)

    data = {
        "lat":              lat,
        "lon":              lon,
        "elevation_m":      round(elevs[4], 1),
        "slope_deg":        slope_deg,
        "slope_aspect_deg": downslope_deg,
        "upslope_deg":      upslope_deg,
    }
    _cache[key] = {"ts": time.time(), "data": data}
    return {**data, "cached": False}
