"""
RF bakeoff logger — polls rf_metrics every 60s and appends per-node link quality
stats to /opt/jtak/data/rf_bakeoff.csv for comparing antenna/placement/modem configs.

CSV fields:
  timestamp, config_tag, hub_id, source_name,
  modem_preset, sf, bw_khz, cr, tx_power_dbm, freq_mhz,
  packet_count, pkt_rate_per_min,
  rssi_avg, rssi_min, rssi_max,
  snr_avg, snr_min, snr_max, snr_margin_avg,
  channel_util_avg, air_util_tx_avg,
  hop_avg, direct_pct, distance_mi_avg,
  window_secs

LoRa config is read once at startup from meshtasticd and cached to
/opt/jtak/data/lora_config_cache.json — survives restarts without
needing a live meshtasticd connection every time.
"""

import asyncio
import csv
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from utils.identity import get_identity

log = logging.getLogger("rf_logger")

LOG_PATH    = Path("/opt/jtak/data/rf_bakeoff.csv")
TAG_PATH    = Path("/opt/jtak/data/rf_config_tag.txt")
CACHE_PATH  = Path("/opt/jtak/data/lora_config_cache.json")
DB_PATH     = "/opt/jtak/data/jtak.db"
MESH_HOST   = "localhost"

POLL_SECS   = 60
WINDOW_SECS = 120   # aggregate packets seen in last 2 min

# SNR decode floor (dB) by spreading factor — below this the radio can't decode
SNR_FLOOR = {7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}

# Meshtastic modem_preset enum → human label
PRESET_NAMES = {
    0: "LONG_FAST",
    1: "LONG_SLOW",
    2: "VERY_LONG_SLOW",
    3: "MEDIUM_SLOW",
    4: "MEDIUM_FAST",
    5: "SHORT_SLOW",
    6: "SHORT_FAST",
    7: "LONG_MODERATE",
    8: "SHORT_TURBO",
}

FIELDNAMES = [
    # Identity / context
    "timestamp", "config_tag", "hub_id", "source_name",
    # LoRa modem config (hub's own settings — context for the measurement)
    "modem_preset", "sf", "bw_khz", "cr", "tx_power_dbm", "freq_mhz",
    # Packet stats
    "packet_count", "pkt_rate_per_min",
    # Signal quality
    "rssi_avg", "rssi_min", "rssi_max",
    "snr_avg",  "snr_min",  "snr_max",
    "snr_margin_avg",       # SNR above the SF decode floor — headroom indicator
    # Channel health
    "channel_util_avg",     # % channel busy (from node telemetry)
    "air_util_tx_avg",      # % hub TX utilization
    # Topology
    "hop_avg", "direct_pct", "distance_mi_avg",
    "window_secs",
]

# ── LoRa config cache ─────────────────────────────────────────────────────────

_lora_cfg = {}   # populated at startup


def _default_lora() -> dict:
    return {
        "modem_preset": "UNKNOWN",
        "sf":           0,
        "bw_khz":       0,
        "cr":           0,
        "tx_power_dbm": 0,
        "freq_mhz":     None,
    }


def _load_lora_cache() -> dict:
    try:
        d = json.loads(CACHE_PATH.read_text())
        log.info("RF logger: loaded lora config from cache → preset=%s SF%s BW%s TX%sdBm",
                 d.get("modem_preset"), d.get("sf"), d.get("bw_khz"), d.get("tx_power_dbm"))
        return d
    except Exception:
        return {}


def _fetch_lora_config():
    """Blocking — run in a thread. Connects to meshtasticd, reads lora config, caches it."""
    global _lora_cfg
    try:
        import meshtastic.tcp_interface
        import time
        iface = meshtastic.tcp_interface.TCPInterface(hostname=MESH_HOST)
        time.sleep(2)
        lora = iface.localNode.localConfig.lora

        # Frequency: channel_num * 0.25 + 902.0 MHz for US915
        try:
            ch = getattr(lora, "channel_num", 0) or 0
            freq = round(902.0 + ch * 0.25, 3) if ch else None
        except Exception:
            freq = None

        cfg = {
            "modem_preset": PRESET_NAMES.get(lora.modem_preset, f"PRESET_{lora.modem_preset}"),
            "sf":           lora.spread_factor  or 0,
            "bw_khz":       lora.bandwidth      or 0,
            "cr":           lora.coding_rate    or 0,
            "tx_power_dbm": lora.tx_power       or 0,
            "freq_mhz":     freq,
        }
        iface.close()

        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cfg, indent=2))
        _lora_cfg = cfg
        log.info("RF logger: lora config fetched → preset=%s SF%s BW%skHz TX%sdBm",
                 cfg["modem_preset"], cfg["sf"], cfg["bw_khz"], cfg["tx_power_dbm"])
    except Exception as e:
        log.warning("RF logger: could not read lora config from meshtasticd: %s", e)
        if not _lora_cfg:
            _lora_cfg = _load_lora_cache() or _default_lora()


def _init_lora_config():
    """Load from cache immediately, then refresh from live meshtasticd in background."""
    global _lora_cfg
    cached = _load_lora_cache()
    _lora_cfg = cached if cached else _default_lora()
    # Always refresh from device — picks up any config changes since last run
    t = threading.Thread(target=_fetch_lora_config, daemon=True, name="lora_cfg_fetch")
    t.start()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_tag() -> str:
    try:
        return TAG_PATH.read_text().strip() or "untagged"
    except FileNotFoundError:
        return "untagged"


def _snr_margin(snr_avg, sf: int):
    """How many dB above the SF decode floor the average SNR sits."""
    if snr_avg is None or not sf:
        return None
    return round(snr_avg - SNR_FLOOR.get(sf, -17.5), 1)


def _ensure_csv():
    if not LOG_PATH.exists():
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()


def _query(cutoff: str) -> list[dict]:
    """Return per-node aggregate stats for packets received since cutoff."""
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT
                COALESCE(source_name, source_id)             AS source_name,
                COUNT(*)                                      AS packet_count,
                ROUND(AVG(rssi),  1)                          AS rssi_avg,
                ROUND(MIN(rssi),  1)                          AS rssi_min,
                ROUND(MAX(rssi),  1)                          AS rssi_max,
                ROUND(AVG(snr),   1)                          AS snr_avg,
                ROUND(MIN(snr),   1)                          AS snr_min,
                ROUND(MAX(snr),   1)                          AS snr_max,
                ROUND(AVG(hop_count),  2)                     AS hop_avg,
                ROUND(
                    SUM(CASE WHEN hop_count = 0 THEN 1.0 ELSE 0.0 END)
                    / COUNT(*) * 100.0, 1
                )                                             AS direct_pct,
                ROUND(AVG(distance_mi), 2)                    AS distance_mi_avg,
                ROUND(AVG(channel_util_pct), 1)               AS channel_util_avg,
                ROUND(AVG(air_util_tx_pct),  1)               AS air_util_tx_avg
            FROM rf_metrics
            WHERE timestamp >= ?
              AND rssi IS NOT NULL
            GROUP BY COALESCE(source_name, source_id)
            ORDER BY packet_count DESC
        """, (cutoff,)).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug("rf_metrics query error: %s", e)
        return []


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run():
    """Background loop — logs RF link quality stats every POLL_SECS seconds."""
    _init_lora_config()
    _ensure_csv()
    log.info("RF bakeoff logger started → %s", LOG_PATH)

    while True:
        try:
            tag    = _read_tag()
            ident  = get_identity()
            hub_id = ident.get("hub_id", "unknown")
            now    = datetime.now()
            cutoff = (now - timedelta(seconds=WINDOW_SECS)).strftime("%Y-%m-%d %H:%M:%S")
            ts     = now.strftime("%Y-%m-%d %H:%M:%S")
            sf     = _lora_cfg.get("sf", 0)

            nodes = await asyncio.get_event_loop().run_in_executor(None, _query, cutoff)

            if nodes:
                with open(LOG_PATH, "a", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=FIELDNAMES)
                    for n in nodes:
                        pkt_rate = round(n["packet_count"] / (WINDOW_SECS / 60), 1)
                        w.writerow({
                            "timestamp":        ts,
                            "config_tag":       tag,
                            "hub_id":           hub_id,
                            "source_name":      n["source_name"],
                            "modem_preset":     _lora_cfg.get("modem_preset", ""),
                            "sf":               sf,
                            "bw_khz":           _lora_cfg.get("bw_khz", ""),
                            "cr":               _lora_cfg.get("cr", ""),
                            "tx_power_dbm":     _lora_cfg.get("tx_power_dbm", ""),
                            "freq_mhz":         _lora_cfg.get("freq_mhz", ""),
                            "packet_count":     n["packet_count"],
                            "pkt_rate_per_min": pkt_rate,
                            "rssi_avg":         n["rssi_avg"],
                            "rssi_min":         n["rssi_min"],
                            "rssi_max":         n["rssi_max"],
                            "snr_avg":          n["snr_avg"],
                            "snr_min":          n["snr_min"],
                            "snr_max":          n["snr_max"],
                            "snr_margin_avg":   _snr_margin(n["snr_avg"], sf),
                            "channel_util_avg": n["channel_util_avg"],
                            "air_util_tx_avg":  n["air_util_tx_avg"],
                            "hop_avg":          n["hop_avg"],
                            "direct_pct":       n["direct_pct"],
                            "distance_mi_avg":  n["distance_mi_avg"],
                            "window_secs":      WINDOW_SECS,
                        })
                log.debug("RF log: tag=%s preset=%s SF%s nodes=%d",
                          tag, _lora_cfg.get("modem_preset", "?"), sf, len(nodes))
            else:
                log.debug("RF log: no packets in window (tag=%s)", tag)

        except Exception as e:
            log.warning("RF logger error: %s", e)

        await asyncio.sleep(POLL_SECS)
