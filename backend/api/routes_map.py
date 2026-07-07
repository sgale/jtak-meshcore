import math
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Query
from store.db import get_db
from ingest.csv_watcher import node_sensors

router = APIRouter()

# ── Motion helpers ────────────────────────────────────────────────────────────
_MIN_SPEED_MPH  = 0.5
_MIN_DELTA_SECS = 10
_ARROW_TTL_SECS = 180  # drop arrow after 3 min of no new motion data

def _haversine_mi(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    R = 3958.8
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = (math.sin(dlat/2)**2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def _bearing(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2-lon1)
    x = math.sin(dlon) * math.cos(lat2r)
    y = (math.cos(lat1r) * math.sin(lat2r)
         - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon))
    return round((math.degrees(math.atan2(x, y)) + 360) % 360, 1)

def _parse_ts(ts_str):
    """Parse ISO timestamp string to UTC datetime."""
    try:
        return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
    except Exception:
        return None

async def _motion_by_node(db) -> dict:
    """Return {source_id: {speed_mph, heading_deg, motion_age_s}} from last 2 positions."""
    async with db.execute(
        """WITH ranked AS (
               SELECT source_id, latitude, longitude, timestamp,
                      ROW_NUMBER() OVER (PARTITION BY source_id ORDER BY timestamp DESC) AS rn
               FROM positions
           )
           SELECT source_id, latitude, longitude, timestamp, rn
           FROM ranked WHERE rn <= 2
           ORDER BY source_id, rn"""
    ) as cur:
        rows = await cur.fetchall()

    # Group into pairs: rn=1 is latest, rn=2 is previous
    pairs = {}
    for r in rows:
        sid = r["source_id"]
        if sid not in pairs:
            pairs[sid] = {}
        pairs[sid][r["rn"]] = r

    now = datetime.now(timezone.utc)
    result = {}
    for sid, p in pairs.items():
        curr = p.get(1)
        prev = p.get(2)
        if not curr:
            continue

        curr_ts = _parse_ts(curr["timestamp"])
        motion_age_s = round((now - curr_ts).total_seconds()) if curr_ts else None

        speed_mph = heading_deg = None
        if prev:
            prev_ts = _parse_ts(prev["timestamp"])
            if curr_ts and prev_ts:
                delta_s = (curr_ts - prev_ts).total_seconds()
                if delta_s >= _MIN_DELTA_SECS:
                    dist = _haversine_mi(prev["latitude"], prev["longitude"],
                                         curr["latitude"],  curr["longitude"])
                    if dist is not None:
                        spd = round((dist / delta_s) * 3600, 2)
                        if spd >= _MIN_SPEED_MPH and (motion_age_s is None or motion_age_s <= _ARROW_TTL_SECS):
                            speed_mph = spd
                            if motion_age_s is not None and motion_age_s <= _ARROW_TTL_SECS:
                                heading_deg = _bearing(
                                    prev["latitude"], prev["longitude"],
                                    curr["latitude"],  curr["longitude"]
                                )

        result[sid] = {
            "speed_mph":   speed_mph,
            "heading_deg": heading_deg,
            "motion_age_s": motion_age_s,
        }
    return result


@router.get("/positions")
async def latest_positions():
    """Most recent position per node."""
    db = await get_db()
    async with db.execute(
        """SELECT source_id, source_name, latitude, longitude, altitude, MAX(timestamp) as ts
           FROM positions
           GROUP BY source_id
           ORDER BY ts DESC"""
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@router.get("/positions/geojson")
async def positions_geojson():
    """Latest node positions as RFC 7946 GeoJSON FeatureCollection."""
    db = await get_db()
    async with db.execute(
        """WITH latest_pos AS (
               SELECT source_id, source_name, latitude, longitude, altitude,
                      MAX(timestamp) AS last_position
               FROM positions GROUP BY source_id
           ),
           latest_rf AS (
               SELECT source_id, rssi, snr, distance_mi, bearing_deg,
                      packet_type, MAX(timestamp) AS last_rf
               FROM rf_metrics GROUP BY source_id
           ),
           latest_batt AS (
               -- Battery only rides telemetry rows; take the newest row that HAS a
               -- reading so an interleaved advert/channel packet (battery NULL) can't
               -- blank it out when it happens to be the latest overall packet.
               SELECT source_id, battery_pct, MAX(timestamp) AS batt_ts
               FROM rf_metrics WHERE battery_pct IS NOT NULL GROUP BY source_id
           )
           SELECT p.source_id, p.source_name, p.latitude, p.longitude,
                  p.altitude, p.last_position,
                  r.rssi, r.snr, r.distance_mi, r.bearing_deg,
                  r.packet_type, b.battery_pct
           FROM latest_pos p
           LEFT JOIN latest_rf r ON r.source_id = p.source_id
           LEFT JOIN latest_batt b ON b.source_id = p.source_id
           ORDER BY p.source_id"""
    ) as cur:
        rows = await cur.fetchall()

    motion = await _motion_by_node(db)
    features = []
    for r in rows:
        sens = node_sensors.get(r["source_id"], {})
        mv = motion.get(r["source_id"], {})
        props = {
            "source_id":    r["source_id"],
            "source_name":  r["source_name"],
            "altitude_m":   r["altitude"],
            "last_position": r["last_position"],
            "rssi_dbm":     r["rssi"],
            "snr_db":       r["snr"],
            "distance_mi":  r["distance_mi"],
            "bearing_deg":  r["bearing_deg"],
            "packet_type":  r["packet_type"],
            "battery_pct":  r["battery_pct"],
            "temp_c":        sens.get("temp_c"),
            "humidity_pct":  sens.get("humidity_pct"),
            "pressure_hpa":  sens.get("pressure_hpa"),
            "speed_mph":     mv.get("speed_mph"),
            "heading_deg":   mv.get("heading_deg"),
            "motion_age_s":  mv.get("motion_age_s"),
        }
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                # GeoJSON spec: [longitude, latitude] per RFC 7946 §3.1.2
                "coordinates": [r["longitude"], r["latitude"]],
            },
            "properties": props,
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "meta": {
            "units": {
                "rssi_dbm":     "dBm",
                "snr_db":       "dB",
                "distance_mi":  "miles",
                "bearing_deg":  "degrees",
                "battery_pct":  "%",
                "altitude_m":   "meters",
                "temp_c":       "°C",
                "humidity_pct": "%",
                "pressure_hpa": "hPa",
            }
        },
    }


@router.get("/positions/history")
async def position_history(
    source_id: str,
    limit: int = Query(200, le=2000),
):
    db = await get_db()
    async with db.execute(
        """SELECT source_id, source_name, latitude, longitude, altitude, timestamp
           FROM positions
           WHERE source_id = ?
           ORDER BY timestamp DESC LIMIT ?""",
        (source_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@router.get("/nodes")
async def nodes():
    """All known nodes with latest signal metrics and derived motion."""
    db = await get_db()
    motion = await _motion_by_node(db)
    async with db.execute(
        """WITH latest_pos AS (
               SELECT source_id, source_name, latitude, longitude,
                      MAX(timestamp) AS last_position
               FROM positions GROUP BY source_id
           ),
           latest_rf AS (
               SELECT source_id, rssi, snr, distance_mi, bearing_deg, packet_type,
                      MAX(timestamp) AS last_rf
               FROM rf_metrics GROUP BY source_id
           ),
           latest_batt AS (
               -- Battery only rides telemetry rows; take the newest row that HAS a
               -- reading so an interleaved advert/channel packet (battery NULL) can't
               -- blank it out when it happens to be the latest overall packet.
               SELECT source_id, battery_pct, MAX(timestamp) AS batt_ts
               FROM rf_metrics WHERE battery_pct IS NOT NULL GROUP BY source_id
           )
           SELECT p.source_id, p.source_name, p.latitude, p.longitude,
                  p.last_position, r.rssi, r.snr, r.distance_mi, r.bearing_deg,
                  r.packet_type, b.battery_pct, r.last_rf
           FROM latest_pos p
           LEFT JOIN latest_rf r ON r.source_id = p.source_id
           LEFT JOIN latest_batt b ON b.source_id = p.source_id
           ORDER BY p.source_id"""
    ) as cur:
        rows = await cur.fetchall()
    result = []
    for r in rows:
        node = dict(r)
        node.update(motion.get(r["source_id"], {
            "speed_mph": None, "heading_deg": None, "motion_age_s": None
        }))
        result.append(node)
    return result
