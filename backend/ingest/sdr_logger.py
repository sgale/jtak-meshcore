"""
SDR bakeoff logger — polls readsb JSON every 60s and appends RF/decode metrics
to /opt/jtak/data/sdr_bakeoff.csv for comparing dongles, antennas, and filters.

Tag file: /opt/jtak/data/sdr_config_tag.txt  (edit without restart to label runs)
  e.g.  nooelec-nofilter  |  rtlsdr-flamingo  |  nooelec-flamingo

CSV fields:
  timestamp, config_tag,
  gain_db, ppm_est,
  signal_avg_dbfs, noise_floor_dbfs, snr_db, peak_signal_dbfs, strong_signals,
  samples_processed, samples_dropped, samples_lost,
  modes_detected, bad_frames, decode_rate_pct,
  messages_per_min, positions_per_min,
  aircraft_total, aircraft_with_pos,
  ghost_tracks_pct, max_range_mi,
  rssi_avg_db, rssi_min_db, rssi_max_db,
  aircraft_gt_50mi, aircraft_gt_100mi, aircraft_gt_150mi,
  demod_cpu_ms
"""

import asyncio
import csv
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("sdr_logger")

STATS_JSON    = Path("/run/readsb/stats.json")
AIRCRAFT_JSON = Path("/run/readsb/aircraft.json")
LOG_PATH      = Path("/opt/jtak/data/sdr_bakeoff.csv")
TAG_PATH      = Path("/opt/jtak/data/sdr_config_tag.txt")
POLL_SECS     = 60

FIELDNAMES = [
    "timestamp", "config_tag",
    "gain_db", "ppm_est",
    "signal_avg_dbfs", "noise_floor_dbfs", "snr_db", "peak_signal_dbfs", "strong_signals",
    "samples_processed", "samples_dropped", "samples_lost",
    "modes_detected", "bad_frames", "decode_rate_pct",
    "messages_per_min", "positions_per_min",
    "aircraft_total", "aircraft_with_pos",
    "ghost_tracks_pct", "max_range_mi",
    "rssi_avg_db", "rssi_min_db", "rssi_max_db",
    "aircraft_gt_50mi", "aircraft_gt_100mi", "aircraft_gt_150mi",
    "demod_cpu_ms",
]


def _tag() -> str:
    try:
        return TAG_PATH.read_text().strip() or "untagged"
    except FileNotFoundError:
        return "untagged"


def _nm_to_mi(nm: float) -> float:
    return round(nm * 1.15078, 1)


def _read_stats() -> dict:
    raw = json.loads(STATS_PATH.read_text()) if False else json.loads(STATS_JSON.read_text())
    s   = raw.get("last1min", {})
    loc = s.get("local", {})
    trk = s.get("tracks", {})
    cpu = s.get("cpu", {})

    modes    = loc.get("modes", 0)
    accepted = sum(loc.get("accepted", [0, 0]))
    bad      = loc.get("bad", 0)
    sig      = loc.get("signal", None)
    noise    = loc.get("noise", None)

    decode_rate = round(accepted / modes * 100, 1) if modes > 0 else None
    snr         = round(sig - noise, 1) if sig is not None and noise is not None else None

    all_tracks    = trk.get("all", 0)
    single_tracks = trk.get("single_message", 0)
    ghost_pct     = round(single_tracks / all_tracks * 100, 1) if all_tracks > 0 else None

    max_range_m  = s.get("max_distance", 0)
    max_range_mi = round(max_range_m / 1609.34, 1) if max_range_m else None

    return {
        "gain_db":          raw.get("gain_db"),
        "ppm_est":          raw.get("estimated_ppm"),
        "signal_avg_dbfs":  sig,
        "noise_floor_dbfs": noise,
        "snr_db":           snr,
        "peak_signal_dbfs": loc.get("peak_signal"),
        "strong_signals":   loc.get("strong_signals", 0),
        "samples_processed":loc.get("samples_processed", 0),
        "samples_dropped":  loc.get("samples_dropped", 0),
        "samples_lost":     loc.get("samples_lost", 0),
        "modes_detected":   modes,
        "bad_frames":       bad,
        "decode_rate_pct":  decode_rate,
        "messages_per_min": s.get("messages", 0),
        "positions_per_min":s.get("position_count_total", 0),
        "ghost_tracks_pct": ghost_pct,
        "max_range_mi":     max_range_mi,
        "demod_cpu_ms":     cpu.get("demod", 0),
        "tracks_all":       all_tracks,
    }


def _read_aircraft() -> dict:
    raw = json.loads(AIRCRAFT_JSON.read_text())
    ac  = raw.get("aircraft", [])

    now    = raw.get("now", 0)
    recent = [a for a in ac if a.get("seen", 999) < 60]  # seen in last 60s
    with_pos = [a for a in recent if a.get("lat") is not None]

    rssi_vals = [a["rssi"] for a in recent if "rssi" in a]
    rssi_avg  = round(sum(rssi_vals) / len(rssi_vals), 1) if rssi_vals else None
    rssi_min  = round(min(rssi_vals), 1) if rssi_vals else None
    rssi_max  = round(max(rssi_vals), 1) if rssi_vals else None

    # r_dst is in nautical miles in readsb — convert to statute miles
    dists = [_nm_to_mi(a["r_dst"]) for a in with_pos if "r_dst" in a]
    gt50  = sum(1 for d in dists if d > 50)
    gt100 = sum(1 for d in dists if d > 100)
    gt150 = sum(1 for d in dists if d > 150)

    return {
        "aircraft_total":    len(recent),
        "aircraft_with_pos": len(with_pos),
        "rssi_avg_db":       rssi_avg,
        "rssi_min_db":       rssi_min,
        "rssi_max_db":       rssi_max,
        "aircraft_gt_50mi":  gt50,
        "aircraft_gt_100mi": gt100,
        "aircraft_gt_150mi": gt150,
    }


def _ensure_csv():
    if not LOG_PATH.exists():
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()
        log.info(f"Created {LOG_PATH}")


def _write_row(row: dict):
    with open(LOG_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writerow(row)


async def run():
    _ensure_csv()
    log.info("SDR bakeoff logger started — polling every %ds", POLL_SECS)

    while True:
        await asyncio.sleep(POLL_SECS)
        try:
            if not STATS_JSON.exists():
                log.debug("readsb stats.json not found — is readsb running?")
                continue

            stats = _read_stats()
            acft  = _read_aircraft()

            row = {
                "timestamp":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "config_tag": _tag(),
                **stats,
                **acft,
            }
            _write_row(row)
            log.debug(
                "[%s] SNR=%.1fdB  decode=%.1f%%  aircraft=%d  range=%smi  ghost=%.0f%%",
                row["config_tag"],
                row["snr_db"] or 0,
                row["decode_rate_pct"] or 0,
                row["aircraft_total"],
                row["max_range_mi"] or "?",
                row["ghost_tracks_pct"] or 0,
            )
        except Exception as e:
            log.warning("SDR logger error: %s", e)
