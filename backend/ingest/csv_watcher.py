"""
csv_watcher.py — Tail tactical_monitor RF log CSVs into jTAK SQLite.

tactical_monitor.py already does all the hard meshtastic work and writes
rf_log_JTAK_Hub_2_YYYY-MM-DD.csv. We watch those files and ingest rows
into jTAK's DB + broadcast via the live WebSocket queue.

New rows are detected by tracking file size. On startup we load today's
file from the last N rows to populate the DB quickly, then tail from there.
"""

import asyncio
import csv
import io
import os
import glob
import time
from datetime import datetime, date, timezone, timedelta
from pathlib import Path

from store.db import get_db
from utils.config import get
import led_client

LOG_DIR = get("logs.base_path", "/opt/jtak/logs/rf")
PATTERN = get("logs.rf_log_pattern", "rf_log_*.csv")
POLL_INTERVAL = 2.0       # seconds between tail checks
STARTUP_ROWS  = 500       # rows to backfill from today's file on startup

# Broadcast queue — api/routes_ws.py reads from this
broadcast_queue: asyncio.Queue = asyncio.Queue(maxsize=500)

# Latest hub BME680 environment reading — updated on every ingested row
latest_hub_env: dict = {
    "hub_temp_c":          None,
    "hub_humidity_pct":    None,
    "hub_pressure_hpa":    None,
    "hub_gas_resistance_ohm": None,
    "hub_iaq_pct":         None,
    "hub_smoke_alert":     False,
}

# Latest sensor readings per node (from mesh telemetry packets)
node_sensors: dict = {}   # source_id → {temp_c, temp_f, humidity_pct, pressure_hpa, timestamp}

# Nodes seen since startup — used to detect first-seen nodes for LED event
_seen_nodes: set = set()
_backfilling: bool = False   # suppress LED events during startup backfill

# This hub's own position (from hub_lat/hub_lon columns in every CSV row)
hub_position: dict = {
    "source_id":   None,
    "source_name": None,
    "latitude":    None,
    "longitude":   None,
    "altitude":    None,
    "sats":        None,
    "speed_mph":   None,
    "heading_deg": None,
    "climb_mps":   None,
    "epx_m":       None,
    "epy_m":       None,
}


def _to_utc_iso(local_ts_str: str) -> str:
    """Convert tactical_monitor local time string to UTC ISO 8601."""
    try:
        local_dt = datetime.strptime(local_ts_str, "%Y-%m-%d %H:%M:%S")
        # time.timezone is offset of local (non-DST) zone from UTC in seconds west
        # time.daylight is 1 if DST zone is defined; time.altzone is DST offset
        offset_sec = time.altzone if time.daylight and time.localtime().tm_isdst else time.timezone
        utc_dt = local_dt + timedelta(seconds=offset_sec)
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return local_ts_str  # pass through if unparseable


def _current_log() -> str | None:
    today = date.today().strftime("%Y-%m-%d")
    files = glob.glob(os.path.join(LOG_DIR, PATTERN))
    # Prefer today's file; fallback to most-recent
    for f in sorted(files, reverse=True):
        if today in f:
            return f
    return files[-1] if files else None


async def _ingest_row(row: dict):
    """Write one CSV row into positions + rf_metrics tables."""
    db = await get_db()
    ts = _to_utc_iso(row.get("timestamp", ""))
    source_id   = row.get("node_id", "")
    source_name = row.get("node_name", source_id)
    hub_id      = row.get("hub_id", "")

    def _f(k):
        v = row.get(k, "")
        return float(v) if v not in ("", "None", None) else None

    def _i(k):
        v = row.get(k, "")
        return int(v) if v not in ("", "None", None) else None

    lat = _f("node_lat")
    lon = _f("node_lon")

    # Position row (only if we have coords). source_type defaults to meshtastic_node
    # for legacy logs; MeshCore's rf_log carries source_type='meshcore' so its nodes
    # stay collision-safe vs the Meshtastic node keyspace.
    source_type = row.get("source_type") or "meshtastic_node"
    if lat is not None and lon is not None:
        await db.execute(
            """INSERT INTO positions
               (timestamp, source_id, source_name, source_type, latitude, longitude, altitude)
               VALUES (?,?,?,?,?,?,?)""",
            (ts, source_id, source_name, source_type, lat, lon, _f("node_alt_m")),
        )

    # Track this hub's own position from hub_lat/hub_lon columns
    h_lat = _f("hub_lat")
    h_lon = _f("hub_lon")
    if h_lat is not None and h_lon is not None:
        hub_position["source_id"]   = row.get("hub_id")
        hub_position["source_name"] = row.get("hub_name")
        hub_position["latitude"]    = h_lat
        hub_position["longitude"]   = h_lon
        hub_position["altitude"]    = _f("hub_alt_m")
        hub_position["sats"]        = _i("hub_sats")

    # Track per-node sensor telemetry if present in this row
    temp_c = _f("temp_c")
    if temp_c is not None:
        node_sensors[source_id] = {
            "source_id":    source_id,
            "source_name":  source_name,
            "temp_c":       temp_c,
            "temp_f":       round(temp_c * 9/5 + 32, 1),
            "humidity_pct": _f("humidity_pct"),
            "pressure_hpa": _f("pressure_hpa"),
            "timestamp":    ts,
        }

    # Update latest hub BME env from every row (it carries hub readings)
    for key in latest_hub_env:
        v = row.get(key, "")
        if v not in ("", "None", None):
            try:
                latest_hub_env[key] = float(v) if key != "hub_smoke_alert" else (v.lower() == "true")
            except ValueError:
                pass

    # RF row
    await db.execute(
        """INSERT INTO rf_metrics
           (timestamp, source_id, source_name, hub_id,
            rssi, snr, frequency, hop_count, direct_or_relay,
            path_loss_db, distance_mi, bearing_deg, packet_type,
            battery_pct, channel_util_pct, air_util_tx_pct, cpu_temp_c)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ts, source_id, source_name, hub_id,
            _f("rssi"), _f("snr"), _f("freq_mhz"), _i("hop_count"),
            row.get("direct_or_relay"),
            _f("path_loss_db"), _f("distance_mi"), _f("bearing_deg"),
            row.get("packet_type"),
            _f("battery_pct"), _f("channel_util_pct"), _f("air_util_tx_pct"),
            _f("cpu_temp_c"),
        ),
    )
    await db.commit()

    # LED events (suppressed during startup backfill). One effect per packet, by type:
    #   first-seen node -> new_node (rainbow); channel msg -> cyan; DM -> green;
    #   telemetry poll -> quiet (don't strobe every cycle); else (advert/RF) -> white.
    if not _backfilling:
        pt = (row.get("packet_type") or "").lower()
        if source_id and source_id not in _seen_nodes:
            _seen_nodes.add(source_id)
            led_client.event("new_node")
        elif pt in ("channel_text", "channel_message", "group_text"):
            led_client.event("channel_message")
        elif pt in ("direct_text", "direct_message", "dm"):
            led_client.event("direct_message")
        elif pt == "telemetry":
            pass  # automated hub poll — no light
        else:
            led_client.event("lora_message")
    elif source_id:
        _seen_nodes.add(source_id)   # populate quietly during backfill

    # Push to WebSocket broadcast queue (non-blocking)
    sens = node_sensors.get(source_id, {})
    msg = {
        "type":         "rf",
        "timestamp":    ts,
        "source_id":    source_id,
        "source_name":  source_name,
        "lat":          lat,
        "lon":          lon,
        "rssi":         _f("rssi"),
        "snr":          _f("snr"),
        "distance_mi":  _f("distance_mi"),
        "packet_type":  row.get("packet_type"),
        "temp_c":       sens.get("temp_c"),
        "temp_f":       sens.get("temp_f"),
        "humidity_pct": sens.get("humidity_pct"),
        "pressure_hpa": sens.get("pressure_hpa"),
    }
    try:
        broadcast_queue.put_nowait(msg)
    except asyncio.QueueFull:
        pass  # drop oldest — WS clients get next update


async def run():
    """Main loop: watch the current log file and ingest new rows."""
    print(f"[csv_watcher] Watching {LOG_DIR}/{PATTERN}")
    current_path = None
    file_pos     = 0
    header       = None

    while True:
        path = _current_log()

        # File rotated (new day) or first run
        if path != current_path:
            current_path = path
            file_pos = 0
            header   = None
            if path:
                # Backfill last N rows on startup (LED events suppressed)
                global _backfilling
                _backfilling = True
                try:
                    with open(path, newline="") as f:
                        reader = csv.DictReader(f)
                        rows = list(reader)
                        for row in rows[-STARTUP_ROWS:]:
                            await _ingest_row(row)
                    file_pos = os.path.getsize(path)
                    print(f"[csv_watcher] Backfilled {min(len(rows), STARTUP_ROWS)} rows from {path}")
                except Exception as e:
                    print(f"[csv_watcher] Backfill error: {e}")
                finally:
                    _backfilling = False

        # Tail new bytes
        if path and os.path.exists(path):
            size = os.path.getsize(path)
            if size > file_pos:
                try:
                    with open(path, newline="") as f:
                        f.seek(file_pos)
                        chunk = f.read()
                    file_pos = size

                    reader = csv.DictReader(io.StringIO(chunk))
                    # If we seeked past the header we need to inject it
                    if file_pos > 0 and not chunk.startswith("timestamp"):
                        # re-read header from start
                        with open(path, newline="") as f:
                            hdr_line = f.readline()
                        reader = csv.DictReader(
                            io.StringIO(hdr_line + chunk)
                        )

                    count = 0
                    for row in reader:
                        if row.get("timestamp"):
                            await _ingest_row(row)
                            count += 1
                    if count:
                        print(f"[csv_watcher] +{count} rows")
                except Exception as e:
                    print(f"[csv_watcher] tail error: {e}")

        await asyncio.sleep(POLL_INTERVAL)
