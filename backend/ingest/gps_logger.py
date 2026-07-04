"""
GPS bakeoff logger — polls gpsd every 30s and appends signal quality metrics
to /opt/jtak/data/gps_bakeoff.csv for comparing antenna/module configurations.

CSV fields:
  timestamp, config_tag, fix_mode, usat, nsat,
  snr_avg, snr_max, snr_min, hdop, vdop, pdop,
  epx_m, epy_m, epv_m, lat, lon, alt_m,
  fix_uptime_pct
"""

import asyncio
import csv
import glob
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from store.db import get_db
from utils.identity import get_identity

log = logging.getLogger("gps_logger")

LOG_PATH      = Path("/opt/jtak/data/gps_bakeoff.csv")
TAG_PATH      = Path("/opt/jtak/data/gps_config_tag.txt")
RF_LOG_DIR    = "/opt/jtak/logs/rf"
POLL_SECS     = 30
GPSPIPE_MSGS  = 12    # SKY appears by message 5-6; 12 is safe with margin
UPTIME_WINDOW = 20    # rolling window size (20 × 30s = 10 min)

FIELDNAMES = [
    "timestamp", "config_tag",
    "fix_mode", "usat", "nsat",
    "snr_avg", "snr_max", "snr_min",
    "hdop", "vdop", "pdop",
    "epx_m", "epy_m", "epv_m",
    "lat", "lon", "alt_m",
    "fix_uptime_pct",
    "speed_mph", "heading_deg", "climb_mps",
]

FIX_MODES = {0: "no_data", 1: "no_fix", 2: "2D", 3: "3D"}

# Rolling fix window — reset when tag changes
_fix_window: deque = deque(maxlen=UPTIME_WINDOW)
_last_tag: str = ""


def _read_tag() -> str:
    try:
        return TAG_PATH.read_text().strip() or "untagged"
    except FileNotFoundError:
        return "untagged"


def _ensure_csv():
    if not LOG_PATH.exists():
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()


async def _fetch_gpsd() -> tuple[dict, dict]:
    sky, tpv = {}, {}
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/gpspipe", "-w", "-n", str(GPSPIPE_MSGS),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        for line in stdout.decode().splitlines():
            if '"class":"SKY"' in line and not sky:
                sky = json.loads(line)
            elif '"class":"TPV"' in line and not tpv:
                tpv = json.loads(line)
            if sky and tpv:
                break
    except Exception as e:
        log.debug("gpsd fetch error: %s", e)
    return sky, tpv


def _parse(sky: dict, tpv: dict) -> dict:
    sats = sky.get("satellites", [])
    snrs = [s["ss"] for s in sats if s.get("ss", 0) > 0]
    snr_avg = round(sum(snrs) / len(snrs), 1) if snrs else None
    snr_max = max(snrs) if snrs else None
    snr_min = min(snrs) if snrs else None
    fix_mode_num = tpv.get("mode", 0)

    return {
        "fix_mode": FIX_MODES.get(fix_mode_num, fix_mode_num),
        "usat":     sky.get("uSat"),
        "nsat":     sky.get("nSat") or (len(sats) if sats else None),
        "snr_avg":  snr_avg,
        "snr_max":  snr_max,
        "snr_min":  snr_min,
        "hdop":     sky.get("hdop"),
        "vdop":     sky.get("vdop"),
        "pdop":     sky.get("pdop"),
        "epx_m":    round(tpv["epx"], 2) if tpv.get("epx") else None,
        "epy_m":    round(tpv["epy"], 2) if tpv.get("epy") else None,
        "epv_m":    round(tpv.get("epv", 0), 2) if tpv.get("epv") else None,
        "lat":      tpv.get("lat"),
        "lon":      tpv.get("lon"),
        "alt_m":    round(tpv["alt"], 1) if tpv.get("alt") else None,
        "speed_mph":   round(tpv["speed"] * 2.23694, 2) if tpv.get("speed") is not None else None,
        "heading_deg": tpv.get("track"),
        "climb_mps":   round(tpv["climb"], 3) if tpv.get("climb") is not None else None,
        "_fix_mode_num": fix_mode_num,
    }


def _append_self_to_rf_log(row: dict, ident: dict) -> None:
    """Append a SELF position row to today's RF log CSV so TRACKS history shows hub track."""
    today = datetime.now().strftime("%Y-%m-%d")
    # Find today's log written by tactical_monitor (glob — name varies by hub_name)
    matches = glob.glob(os.path.join(RF_LOG_DIR, f"rf_log_*_{today}.csv"))
    if not matches:
        return  # No RF log yet today — skip rather than create an orphan file
    path = matches[0]

    # Read header from existing file so we match tactical_monitor's column order exactly
    try:
        with open(path, newline="") as f:
            fieldnames = next(csv.reader(f))
    except Exception as e:
        log.debug("RF self-log header read error: %s", e)
        return

    hub_id   = ident.get("hub_id", "unknown")
    hub_name = ident.get("hub_name", "Hub")
    node_id  = "!" + ident.get("guid", "").lstrip("!")

    rf_row = {k: "" for k in fieldnames}
    rf_row.update({
        "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # local — matches tactical_monitor
        "packet_time":     str(int(time.time())),
        "hub_id":          hub_id,
        "hub_name":        hub_name,
        "node_id":         node_id,
        "node_name":       hub_name,
        "packet_type":     "POSITION",
        "hop_count":       "0",
        "direct_or_relay": "SELF",
        "node_lat":        str(row["lat"]),
        "node_lon":        str(row["lon"]),
        "node_alt_m":      str(row["alt_m"]) if row.get("alt_m") is not None else "",
        "hub_lat":         str(row["lat"]),
        "hub_lon":         str(row["lon"]),
        "hub_alt_m":       str(row["alt_m"]) if row.get("alt_m") is not None else "",
        "hub_sats":        str(row["usat"]) if row.get("usat") is not None else "",
        "hub_hdop":        str(row["hdop"]) if row.get("hdop") is not None else "",
        "hub_epx_m":       str(row["epx_m"]) if row.get("epx_m") is not None else "",
        "hub_epy_m":       str(row["epy_m"]) if row.get("epy_m") is not None else "",
        "hub_speed_mph":   str(round(row["speed_mph"], 2)) if row.get("speed_mph") is not None else "",
        "hub_heading_deg": str(round(row["heading_deg"], 1)) if row.get("heading_deg") is not None else "",
        "distance_mi":     "0",
    })

    try:
        with open(path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore").writerow(rf_row)
        log.debug("RF self-log: wrote SELF position row to %s", os.path.basename(path))
    except Exception as e:
        log.debug("RF self-log write error: %s", e)


async def run():
    """Background loop — logs GPS quality metrics every POLL_SECS seconds."""
    global _last_tag
    _ensure_csv()
    log.info("GPS bakeoff logger started → %s", LOG_PATH)

    while True:
        try:
            tag = _read_tag()

            # Reset window when test config changes
            if tag != _last_tag:
                _fix_window.clear()
                log.info("GPS tag changed: %s → %s (uptime window reset)", _last_tag, tag)
                _last_tag = tag

            sky, tpv = await _fetch_gpsd()
            row = _parse(sky, tpv)

            # Update rolling fix window (True = had a 3D fix this poll)
            had_fix = row.pop("_fix_mode_num") == 3
            _fix_window.append(had_fix)

            fix_uptime = (
                round(sum(_fix_window) / len(_fix_window) * 100, 1)
                if _fix_window else None
            )

            row["timestamp"]      = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            row["config_tag"]     = tag
            row["fix_uptime_pct"] = fix_uptime

            with open(LOG_PATH, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=FIELDNAMES).writerow(row)

            # Insert hub's own position into DB + RF log so it appears on live map and TRACKS history
            if had_fix and row.get("lat") and row.get("lon"):
                try:
                    ident = get_identity()
                    db = await get_db()
                    await db.execute(
                        """INSERT INTO positions
                           (timestamp, source_id, source_name, source_type, latitude, longitude, altitude, speed_mph, heading_deg, climb_mps, epx_m, epy_m)
                           VALUES (?, ?, ?, 'hub', ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            row["timestamp"],
                            ident.get("hub_id", "unknown"),
                            ident.get("hub_name", "Hub"),
                            row["lat"],
                            row["lon"],
                            row.get("alt_m"),
                            row.get("speed_mph"),
                            row.get("heading_deg"),
                            row.get("climb_mps"),
                            row.get("epx_m"),
                            row.get("epy_m"),
                        ),
                    )
                    await db.commit()
                except Exception as e:
                    log.debug("DB position insert error: %s", e)

                # Append SELF row to RF log CSV — picked up by csv_watcher within 2s
                try:
                    ident = get_identity()
                    _append_self_to_rf_log(row, ident)
                except Exception as e:
                    log.debug("RF self-log error: %s", e)

            log.debug(
                "GPS log: tag=%s mode=%s usat=%s snr_avg=%s hdop=%s uptime=%s%%",
                tag, row["fix_mode"], row["usat"], row["snr_avg"],
                row["hdop"], fix_uptime,
            )
        except Exception as e:
            log.warning("GPS logger error: %s", e)

        await asyncio.sleep(POLL_SECS)
