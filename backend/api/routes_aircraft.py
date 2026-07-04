"""
Aircraft — live ADS-B positions.

Source priority:
  1. Local SDR (dump1090) — reads /run/dump1090/aircraft.json if fresh (< 10s)
  2. OpenSky Network (OAuth2) — fallback when SDR not running

GET /api/aircraft?radius_mi=140
Response includes `source`: "sdr" | "opensky" | "opensky_cached"
"""

import json
import time
import asyncio
import sqlite3
import httpx
from pathlib import Path
from fastapi import APIRouter, HTTPException
from utils.config import get
from ingest.csv_watcher import hub_position as _hub_position


def _hub_center() -> tuple[float, float]:
    """Return live hub GPS coords, falling back to map.default_center."""
    lat = _hub_position.get("latitude")
    lon = _hub_position.get("longitude")
    if lat is not None and lon is not None:
        return lat, lon
    center = get("map.default_center", [40.5729, -111.9941])
    return center[0], center[1]

DB_PATH        = "/opt/jtak/data/jtak.db"
DUMP1090_JSON  = Path("/run/dump1090/aircraft.json")
SDR_MAX_AGE_S  = 10     # ignore SDR file if older than this

OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network"
    "/protocol/openid-connect/token"
)
OPENSKY_API_URL = "https://opensky-network.org/api/states/all"
CACHE_TTL       = 20
MI_PER_DEG      = 69.0


# ── OAuth2 Token Manager ───────────────────────────────────────────────────────
class _TokenManager:
    def __init__(self):
        self._token: str | None = None
        self._expires_at: float = 0
        self._lock = asyncio.Lock()

    def _configured(self) -> bool:
        return bool(get("opensky.client_id", "") and get("opensky.client_secret", ""))

    async def get_token(self) -> str | None:
        if not self._configured():
            return None
        if time.time() < self._expires_at - 300 and self._token:
            return self._token
        async with self._lock:
            if time.time() < self._expires_at - 300 and self._token:
                return self._token
            await self._refresh()
        return self._token

    async def _refresh(self):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    OPENSKY_TOKEN_URL,
                    data={
                        "grant_type":    "client_credentials",
                        "client_id":     get("opensky.client_id"),
                        "client_secret": get("opensky.client_secret"),
                    },
                )
            if r.status_code == 200:
                body = r.json()
                self._token      = body["access_token"]
                self._expires_at = time.time() + body.get("expires_in", 1800)
            else:
                self._expires_at = time.time() + 60
        except Exception:
            self._token      = None
            self._expires_at = time.time() + 60


_tokens = _TokenManager()


# ── Aircraft type DB lookup ────────────────────────────────────────────────────
def _classify(icao_type: str) -> str:
    if not icao_type:
        return "unknown"
    c = icao_type[0].upper()
    e = icao_type[2].upper() if len(icao_type) >= 3 else ""
    if c == "H": return "helicopter"
    if c == "G": return "glider"
    if e == "J": return "jet"
    if e == "T": return "turboprop"
    if e == "P": return "piston"
    return "unknown"


def _lookup_aircraft(icao24_list: list[str]) -> dict:
    if not icao24_list:
        return {}
    try:
        con = sqlite3.connect(DB_PATH, timeout=3)
        placeholders = ",".join("?" * len(icao24_list))
        rows = con.execute(
            f"SELECT icao24, registration, manufacturer, model, icao_type, operator, category "
            f"FROM aircraft_db WHERE icao24 IN ({placeholders})",
            [i.lower() for i in icao24_list],
        ).fetchall()
        con.close()
        return {r[0]: r for r in rows}
    except Exception:
        return {}


def _enrich(features: list) -> list:
    """Batch DB lookup and attach type/registration info to features."""
    icao_list = [f["properties"]["icao24"] for f in features]
    db_rows   = _lookup_aircraft(icao_list)
    for feat in features:
        row = db_rows.get(feat["properties"]["icao24"].lower())
        if row:
            _, reg, mfr, model, icao_type, operator, _ = row
            feat["properties"].update({
                "ac_type":      _classify(icao_type),
                "registration": reg or None,
                "manufacturer": mfr or None,
                "model":        model or None,
                "operator":     operator or None,
            })
    return features


# ── SDR source (dump1090) ──────────────────────────────────────────────────────
def _read_sdr(radius_mi: float) -> list | None:
    """Read dump1090 JSON. Returns feature list or None if unavailable/stale."""
    try:
        stat = DUMP1090_JSON.stat()
        age  = time.time() - stat.st_mtime
        if age > SDR_MAX_AGE_S:
            return None
        raw = json.loads(DUMP1090_JSON.read_text())
    except Exception:
        return None

    lat_c, lon_c = _hub_center()
    bbox_deg = max(0.3, min(radius_mi / MI_PER_DEG, 8.0))

    features = []
    for ac in (raw.get("aircraft") or []):
        lat = ac.get("lat")
        lon = ac.get("lon")
        if lat is None or lon is None:
            continue
        # readsb uses alt_baro; dump1090 uses altitude
        altitude = ac.get("alt_baro") or ac.get("altitude")
        if altitude is not None and altitude == 0:
            continue  # skip on-ground

        # Bbox filter (dump1090/readsb doesn't filter by range)
        if not (lat_c - bbox_deg <= lat <= lat_c + bbox_deg and
                lon_c - bbox_deg <= lon <= lon_c + bbox_deg):
            continue

        alt_m    = round(altitude * 0.3048) if altitude else None
        alt_ft   = altitude
        vel_kts  = ac.get("gs") or ac.get("speed")
        vrate_fpm = ac.get("baro_rate") or ac.get("vert_rate")
        vrate_ms = round(vrate_fpm / 196.85) if vrate_fpm is not None else None

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "icao24":       ac.get("hex", ""),
                "callsign":     (ac.get("flight") or ac.get("hex", "")).strip(),
                "country":      None,
                "heading":      ac.get("track"),
                "alt_m":        alt_m,
                "alt_ft":       alt_ft,
                "vel_kts":      vel_kts,
                "vrate_fpm":    vrate_fpm,
                "squawk":       ac.get("squawk"),
                "last_seen":    ac.get("seen"),
                "rssi":         ac.get("rssi"),
                "ac_type":      "unknown",
                "registration": None,
                "manufacturer": None,
                "model":        None,
                "operator":     None,
            },
        })

    return features


# ── OpenSky source ─────────────────────────────────────────────────────────────
_cache: dict = {}
_rate_limited_until: float = 0


async def _fetch_opensky(radius_mi: float) -> dict:
    global _rate_limited_until

    if time.time() < _rate_limited_until:
        if _cache.get("data"):
            return {**_cache["data"], "cached": True, "rate_limited": True,
                    "source": "opensky_cached",
                    "cache_age_s": int(time.time() - _cache["ts"])}
        raise HTTPException(429, f"OpenSky rate limited — {int(_rate_limited_until - time.time())}s remaining")

    cached = _cache.get("data")
    if cached and (time.time() - _cache.get("ts", 0)) < CACHE_TTL \
            and _cache.get("radius_mi") == radius_mi:
        return {**cached, "cached": True, "source": "opensky_cached",
                "cache_age_s": int(time.time() - _cache["ts"])}

    lat, lon = _hub_center()
    bbox_deg = max(0.3, min(radius_mi / MI_PER_DEG, 8.0))
    bbox = {"lamin": lat-bbox_deg, "lomin": lon-bbox_deg,
            "lamax": lat+bbox_deg, "lomax": lon+bbox_deg}

    token   = await _tokens.get_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(OPENSKY_API_URL, params=bbox, headers=headers)
    except Exception as e:
        raise HTTPException(502, f"OpenSky unreachable: {e}")

    if r.status_code == 429:
        _rate_limited_until = time.time() + 300
        if _cache.get("data"):
            return {**_cache["data"], "cached": True, "rate_limited": True,
                    "source": "opensky_cached",
                    "cache_age_s": int(time.time() - _cache["ts"])}
        raise HTTPException(429, "OpenSky rate limited — retry in 5 minutes")
    if r.status_code == 401:
        _tokens._token = None; _tokens._expires_at = 0
        raise HTTPException(401, "OpenSky auth failed — check client_id/secret in jtak.yaml")
    if r.status_code != 200:
        raise HTTPException(502, f"OpenSky HTTP {r.status_code}")

    features = []
    for s in (r.json().get("states") or []):
        lon_s, lat_s = s[5], s[6]
        if lon_s is None or lat_s is None: continue
        if s[8]: continue  # on_ground
        baro_m = s[7]; geo_m = s[13]
        alt_m  = baro_m or geo_m
        alt_ft = round(alt_m * 3.281) if alt_m is not None else None
        vel_ms = s[9]
        vrate_ms = s[11]
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon_s, lat_s]},
            "properties": {
                "icao24":       s[0],
                "callsign":     (s[1] or "").strip() or s[0],
                "country":      s[2],
                "heading":      s[10],
                "alt_m":        round(alt_m) if alt_m is not None else None,
                "alt_ft":       alt_ft,
                "vel_kts":      round(vel_ms * 1.944) if vel_ms is not None else None,
                "vrate_fpm":    round(vrate_ms * 196.85) if vrate_ms is not None else None,
                "squawk":       s[14],
                "last_seen":    s[4],
                "rssi":         None,
                "ac_type":      "unknown",
                "registration": None,
                "manufacturer": None,
                "model":        None,
                "operator":     None,
            },
        })

    data = {
        "type": "FeatureCollection", "features": features,
        "count": len(features), "authed": token is not None,
        "source": "opensky", "cached": False, "cache_age_s": 0,
    }
    _cache["data"] = data; _cache["ts"] = time.time(); _cache["radius_mi"] = radius_mi
    return data


# ── Route ──────────────────────────────────────────────────────────────────────
router = APIRouter()


@router.get("/aircraft")
async def get_aircraft(radius_mi: float = 140.0, source: str = "auto"):
    """Live ADS-B.

    source=sdr      — local dump1090 only (no OpenSky fallback)
    source=opensky  — OpenSky only (no SDR)
    source=auto     — SDR preferred, OpenSky fallback (legacy behaviour)
    """

    if source == "opensky":
        data = await _fetch_opensky(radius_mi)
        data["features"] = _enrich(data["features"])
        return data

    sdr_features = await asyncio.get_event_loop().run_in_executor(
        None, _read_sdr, radius_mi
    )

    if source == "sdr":
        if sdr_features is None:
            return {
                "type": "FeatureCollection", "features": [], "count": 0,
                "source": "sdr", "available": False,
                "authed": False, "cached": False, "cache_age_s": 0,
            }
        features = _enrich(sdr_features)
        return {
            "type": "FeatureCollection", "features": features,
            "count": len(features), "source": "sdr", "available": True,
            "authed": False, "cached": False, "cache_age_s": 0,
        }

    # auto: SDR preferred, OpenSky fallback
    if sdr_features is not None:
        features = _enrich(sdr_features)
        return {
            "type":        "FeatureCollection",
            "features":    features,
            "count":       len(features),
            "source":      "sdr",
            "available":   True,
            "authed":      False,
            "cached":      False,
            "cache_age_s": 0,
        }

    data = await _fetch_opensky(radius_mi)
    data["features"] = _enrich(data["features"])
    return data
