#!/usr/bin/env python3
"""
jTAK Push Agent — streams edge telemetry to HQ SensorThings ingest API.

Runs as a daemon on each edge hub.  Reads from local SQLite, pushes batched
observations to https://hq.jtak.club/api/v1/ingest, tracks watermarks locally.

Config from /opt/jtak/config/jtak.yaml (hq: section) or environment:
  JTAK_HQ_URL            — HQ base URL  (default: https://hq.jtak.club)
  JTAK_HQ_URL_SECONDARY  — Optional secondary HQ (best-effort, never blocks primary)
  JTAK_HQ_KEY            — API key      (required if not in yaml)
  JTAK_HUB_ID            — hub thing ID (default: hub-tak-2)
"""

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import httpx
import yaml

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR       = Path("/opt/jtak")
DB_PATH        = BASE_DIR / "data" / "jtak.db"
YAML_PATH      = BASE_DIR / "config" / "jtak.yaml"
IDENTITY_PATH  = BASE_DIR / "config" / "jtak.identity.yaml"
WATERMARK_FILE = BASE_DIR / "data" / "hq_watermarks.json"
BACKLOG_CUTOFF_SECS = 300  # observations older than this are flagged is_backlog=True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [push-agent] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

def _load_config() -> dict:
    cfg = {}
    identity = {}
    if YAML_PATH.exists():
        with open(YAML_PATH) as f:
            full = yaml.safe_load(f) or {}
        cfg = full.get("hq", {})
    if IDENTITY_PATH.exists():
        with open(IDENTITY_PATH) as f:
            identity = yaml.safe_load(f) or {}

    # hub_id derived from identity (e.g. "tak-2" → "hub-tak-2") — never from jtak.yaml
    identity_hub = identity.get("hub_id", "")
    hub_id = f"hub-{identity_hub}" if identity_hub else cfg.get("hub_id", os.getenv("JTAK_HUB_ID", "hub-tak-2"))

    # push_token and url_secondary live in identity.yaml; jtak.yaml is fallback only
    return {
        "url":               cfg.get("url", os.getenv("JTAK_HQ_URL", "https://hq.jtak.club")),
        "url_secondary":     identity.get("hq_url_secondary", cfg.get("url_secondary", os.getenv("JTAK_HQ_URL_SECONDARY", ""))),
        "push_token":        identity.get("hq_push_token", cfg.get("push_token", os.getenv("JTAK_HQ_KEY", ""))),
        "hub_id":            hub_id,
        "push_interval_sec": int(cfg.get("push_interval_sec", 60)),
        "batch_size":        int(cfg.get("batch_size", 500)),
        "enabled":           cfg.get("enabled", True),
        "sync_channels":     list(cfg.get("sync_channels", [])),
    }

# ── Watermarks ────────────────────────────────────────────────────────────────
def _load_watermarks() -> dict:
    defaults = {"rf_metrics": 0, "positions": 0, "sensors": 0, "messages": 0}
    if WATERMARK_FILE.exists():
        try:
            return {**defaults, **json.loads(WATERMARK_FILE.read_text())}
        except Exception:
            pass
    return defaults

def _save_watermarks(wm: dict):
    WATERMARK_FILE.write_text(json.dumps(wm, indent=2))

def _migrate_watermarks_to_flags():
    """One-time migration: mark rows already pushed (id <= watermark) as synced_to_hq=1.
    Prevents re-pushing historical data after switching from watermark to flag model."""
    wm = _load_watermarks()
    if wm.get("rf_metrics", 0) > 0:
        _exec("UPDATE rf_metrics SET synced_to_hq = 1 WHERE id <= ?", (wm["rf_metrics"],))
        log.info("Migrated rf_metrics watermark → synced_to_hq flags (id <= %d)", wm["rf_metrics"])
    if wm.get("positions", 0) > 0:
        _exec("UPDATE positions SET synced_to_hq = 1 WHERE id <= ?", (wm["positions"],))
        log.info("Migrated positions watermark → synced_to_hq flags (id <= %d)", wm["positions"])

# ── SQLite helpers ────────────────────────────────────────────────────────────
def _exec(sql: str, params: tuple = ()):
    """Write helper — mirrors _query() error handling, commits on success."""
    try:
        con = sqlite3.connect(str(DB_PATH), timeout=5)
        con.execute(sql, params)
        con.commit()
        con.close()
    except Exception as e:
        log.warning("SQLite exec failed: %s", e)

def _query(sql: str, params: tuple = ()) -> list:
    try:
        con = sqlite3.connect(str(DB_PATH), timeout=5)
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, params).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("SQLite query failed: %s", e)
        return []

# ── FOI mapping (node_id → HQ foi_id, from HQ init-handshake message) ─────────
FOI_MAP = {
    # Mesh node IDs (hardware MAC-derived)
    "!4615e8b0": "foi-hub-tak2",
    "!4cc9f0c3": "foi-hub-tak1",
    "!34a6049d": "foi-hub-tak3",
    "!12b3078a": "foi-hub-tak4",
    "!16bff2e1": "foi-hub-tak5",
    # Hub-self GPS positions use hub_id (name) not mesh node ID
    "tak-1": "foi-hub-tak1",
    "tak-2": "foi-hub-tak2",
    "tak-3": "foi-hub-tak3",
    "tak-4": "foi-hub-tak4",
    "tak-5": "foi-hub-tak5",
    "!7e9998e4": "foi-jacob",
    "!79e5c9f2": "foi-sean",
    "!a5fdd71b": "foi-adam",
    "!22d6cda3": "foi-squints",
    "!6c72e6cc": "foi-tacoma",
}

# ── Data mappers ──────────────────────────────────────────────────────────────
def _clean_ts(ts) -> str | None:
    """Return timestamp string if valid ISO-ish, else None (skip corrupt rows)."""
    if not ts:
        return None
    s = str(ts)
    if '\x00' in s or len(s.strip()) < 10:
        return None
    # Strip trailing garbage after the datetime portion
    s = s.strip()
    # Keep only the datetime prefix if extra junk appended
    for i, c in enumerate(s):
        if i > 25:
            s = s[:i]
            break
    return s

def _rf_to_observations(hub_id: str, rows: list, pos_lookup: dict | None = None) -> list:
    """Map rf_metrics rows → multiple observations per row (one per datastream)."""
    prefix = hub_id.replace("hub-tak-", "tak")  # hub-tak-2 → tak2
    obs = []
    for r in rows:
        if not _clean_ts(r.get("timestamp")):
            log.debug("Skipping row id=%s with corrupt timestamp", r.get("id"))
            continue
        ts    = _clean_ts(r["timestamp"])
        src   = r["source_id"]
        sname = r["source_name"] or src
        params = {"source_node": src, "source_name": sname, "edge_id": r["id"]}
        # Embed nearest node position so HQ uses inline coords, not GPS join
        if pos_lookup and src in pos_lookup:
            import bisect as _bisect
            tl = pos_lookup[src]
            if tl["times"]:
                idx = _bisect.bisect_right(tl["times"], r["timestamp"]) - 1
                if idx < 0:
                    idx = 0
                params["node_lat"] = tl["lats"][idx]
                params["node_lon"] = tl["lons"][idx]
                if tl["alts"][idx] is not None:
                    params["node_alt_m"] = tl["alts"][idx]

        foi = FOI_MAP.get(src)

        def _o(ds_suffix, result):
            if result is None:
                return None
            o = {
                "datastream_id":  f"ds-{prefix}-{ds_suffix}",
                "phenomenon_time": ts,
                "result":         result,
                "parameters":     params,
            }
            if foi:
                o["foi_id"] = foi
            return o

        for o in [
            _o("lora-rssi",    r.get("rssi")),
            _o("lora-snr",     r.get("snr")),
            _o("lora-hops",    r.get("hop_count")),
            _o("lora-pathloss",r.get("path_loss_db")),
            _o("lora-distance",r.get("distance_mi")),
            _o("lora-bearing", r.get("bearing_deg")),
            _o("channel-util", r.get("channel_util_pct")),
            _o("air-util-tx",  r.get("air_util_tx_pct")),
            _o("cpu-temp",     r.get("cpu_temp_c")),
            _o("battery",      r.get("battery_pct")),
        ]:
            if o is not None:
                obs.append(o)
    return obs

def _pos_to_observations(hub_id: str, rows: list) -> list:
    """Map positions rows → gps observations.  Only push hub own-ship if known."""
    prefix = hub_id.replace("hub-tak-", "tak")
    obs = []
    for r in rows:
        if not _clean_ts(r.get("timestamp")):
            continue
        if r.get("latitude") is None or r.get("longitude") is None:
            continue
        params = {
                "source_node":  r["source_id"],
                "source_name":  r.get("source_name", r["source_id"]),
                "source_type":  r.get("source_type", ""),
                "edge_id":      r["id"],
            }
        for field in ("speed_mph", "heading_deg", "climb_mps", "epx_m", "epy_m"):
            if r.get(field) is not None:
                params[field] = r[field]
        entry = {
            "datastream_id":  f"ds-{prefix}-gps",
            "phenomenon_time": r["timestamp"],
            "result": {
                "lat":   r["latitude"],
                "lon":   r["longitude"],
                "alt_m": r.get("altitude"),
            },
            "parameters": params,
        }
        foi = FOI_MAP.get(r["source_id"])
        if foi:
            entry["foi_id"] = foi
        obs.append(entry)
    return obs

# ── Push ──────────────────────────────────────────────────────────────────────
async def _post_batch(client: httpx.AsyncClient, cfg: dict, observations: list,
                      is_backlog: bool = False) -> bool:
    if not observations:
        return True
    payload = {
        "hub_id":     cfg["hub_id"],
        "batch_id":   f"batch-{uuid.uuid4().hex[:12]}",
        "batch_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "observations": observations,
        "is_backlog": is_backlog,
    }
    try:
        r = await client.post(
            f"{cfg['url']}/api/v1/ingest",
            json=payload,
            headers={"X-jTAK-Key": cfg["push_token"]},
            timeout=15.0,
        )
        if r.status_code in (200, 202):
            body = r.json()
            log.info("HQ accepted %d / %d observations", body.get("accepted", "?"), len(observations))
            return True
        else:
            log.warning("HQ ingest HTTP %d: %s", r.status_code, r.text[:200])
            return False
    except Exception as e:
        log.warning("HQ ingest request failed: %s", e)
        return False

async def _post_secondary(client: httpx.AsyncClient, cfg: dict, observations: list,
                          is_backlog: bool = False):
    """Best-effort mirror post to secondary HQ — never raises, never affects watermarks."""
    url = cfg.get("url_secondary", "")
    if not url or not observations:
        return
    sec_cfg = {**cfg, "url": url}
    try:
        await _post_batch(client, sec_cfg, observations, is_backlog=is_backlog)
    except Exception as e:
        log.warning("Secondary HQ post failed: %s", e)


# ── Messages ──────────────────────────────────────────────────────────────────
def _msg_to_payload(hub_id: str, rows: list) -> list:
    """Map mesh_messages rows → HQ message payload list."""
    msgs = []
    for r in rows:
        if not _clean_ts(r.get("timestamp")):
            continue
        entry = {
            "hub_id":        hub_id,
            "hub_message_id": r["id"],
            "edge_id":       r["id"],
            "timestamp":     _clean_ts(r["timestamp"]),
            "direction":     r.get("direction", "rx"),
            "channel_index": r.get("channel_index", 0),
            "channel_name":  r.get("channel_name", ""),
            "from_id":       r.get("from_id", ""),
            "from_name":     r.get("from_name") or r.get("from_id", ""),
            "to_id":         r.get("to_id", ""),
            "message":       r.get("message", ""),
            "mesh_packet_id": r.get("mesh_packet_id"),
        }
        foi = FOI_MAP.get(r.get("from_id", ""))
        if foi:
            entry["foi_id"] = foi
        msgs.append(entry)
    return msgs


async def _post_messages(client: httpx.AsyncClient, cfg: dict, messages: list) -> bool:
    if not messages:
        return True
    payload = {
        "hub_id":     cfg["hub_id"],
        "batch_id":   f"batch-{uuid.uuid4().hex[:12]}",
        "batch_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "messages":   messages,
    }
    try:
        r = await client.post(
            f"{cfg['url']}/api/v1/messages",
            json=payload,
            headers={"X-jTAK-Key": cfg["push_token"]},
            timeout=15.0,
        )
        if r.status_code in (200, 202):
            body = r.json()
            log.info("HQ accepted %d / %d messages", body.get("accepted", "?"), len(messages))
            return True
        else:
            log.warning("HQ messages HTTP %d: %s", r.status_code, r.text[:200])
            return False
    except Exception as e:
        log.warning("HQ messages request failed: %s", e)
        return False


async def _post_messages_secondary(client: httpx.AsyncClient, cfg: dict, messages: list):
    """Best-effort mirror post to secondary HQ — never raises, never affects watermarks."""
    url = cfg.get("url_secondary", "")
    if not url or not messages:
        return
    sec_cfg = {**cfg, "url": url}
    try:
        await _post_messages(client, sec_cfg, messages)
    except Exception as e:
        log.warning("Secondary HQ messages post failed: %s", e)

async def _push_cycle(cfg: dict):
    wm = _load_watermarks()
    batch_size = cfg["batch_size"]
    hub_id = cfg["hub_id"]
    cutoff_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - BACKLOG_CUTOFF_SECS))

    async with httpx.AsyncClient() as client:

        # ── RF metrics — current first, then backlog ─────────────────────────
        # Build position timeline for GPS embedding in RF observations
        pos_raw = _query("SELECT source_id, timestamp, latitude, longitude, altitude "
                         "FROM positions ORDER BY source_id, timestamp")
        pos_lookup: dict = {}
        for p in pos_raw:
            sid = p["source_id"]
            if sid not in pos_lookup:
                pos_lookup[sid] = {"times": [], "lats": [], "lons": [], "alts": []}
            pos_lookup[sid]["times"].append(p["timestamp"])
            pos_lookup[sid]["lats"].append(p["latitude"])
            pos_lookup[sid]["lons"].append(p["longitude"])
            pos_lookup[sid]["alts"].append(p["altitude"])

        for is_backlog, ts_filter, ts_param in [
            (False, "AND timestamp > ?",  cutoff_ts),
            (True,  "AND timestamp <= ?", cutoff_ts),
        ]:
            rf_rows = _query(
                f"SELECT * FROM rf_metrics WHERE synced_to_hq = 0 "
                f"AND (direct_or_relay IS NULL OR direct_or_relay != 'SELF') "
                f"{ts_filter} ORDER BY timestamp DESC LIMIT ?",
                (ts_param, batch_size),
            )
            if rf_rows:
                obs = _rf_to_observations(hub_id, rf_rows, pos_lookup)
                primary_ok = await _post_batch(client, cfg, obs, is_backlog=is_backlog)
                await _post_secondary(client, cfg, obs, is_backlog=is_backlog)
                if primary_ok:
                    ids = ",".join(str(r["id"]) for r in rf_rows)
                    _exec(f"UPDATE rf_metrics SET synced_to_hq = 1 WHERE id IN ({ids})")
                    label = "backlog" if is_backlog else "current"
                    log.info("RF %s synced: %d rows", label, len(rf_rows))

        # ── Positions — current first, then backlog ──────────────────────────
        for is_backlog, ts_filter, ts_param in [
            (False, "AND timestamp > ?",  cutoff_ts),
            (True,  "AND timestamp <= ?", cutoff_ts),
        ]:
            pos_rows = _query(
                f"SELECT * FROM positions WHERE synced_to_hq = 0 "
                f"{ts_filter} ORDER BY timestamp DESC LIMIT ?",
                (ts_param, batch_size),
            )
            if pos_rows:
                obs = _pos_to_observations(hub_id, pos_rows)
                if obs:
                    primary_ok = await _post_batch(client, cfg, obs, is_backlog=is_backlog)
                    await _post_secondary(client, cfg, obs, is_backlog=is_backlog)
                    if primary_ok:
                        ids = ",".join(str(r["id"]) for r in pos_rows)
                        _exec(f"UPDATE positions SET synced_to_hq = 1 WHERE id IN ({ids})")
                        label = "backlog" if is_backlog else "current"
                        log.info("Position %s synced: %d rows", label, len(pos_rows))


        # ── Messages ────────────────────────────────────────────────────────────────────
        if cfg["sync_channels"]:
            placeholders = ",".join("?" * len(cfg["sync_channels"]))
            msg_rows = _query(
                f"SELECT id, timestamp, direction, from_id, from_name, to_id, to_name, "
                f"channel_index, channel_name, message, mesh_packet_id "
                f"FROM mesh_messages WHERE id > ? AND direction = 'rx' "
                f"AND channel_index IN ({placeholders}) ORDER BY id LIMIT ?",
                (wm["messages"], *cfg["sync_channels"], batch_size),
            )
            if msg_rows:
                msgs = _msg_to_payload(hub_id, msg_rows)
                if msgs:
                    primary_ok = await _post_messages(client, cfg, msgs)
                    await _post_messages_secondary(client, cfg, msgs)
                    if primary_ok:
                        wm["messages"] = msg_rows[-1]["id"]
                        _save_watermarks(wm)
                        log.info("Messages watermark → %d (%d rows)", wm["messages"], len(msg_rows))
                        for row in msg_rows:
                            _exec("UPDATE mesh_messages SET synced_to_hq = 1 WHERE id = ?", (row["id"],))

        # ── Waypoints ─────────────────────────────────────────────────────────────
        now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        wp_rows = _query(
            "SELECT id, meshtastic_id, name, description, latitude, longitude, altitude, "
            "icon, source_id, source_name, source_type, hub_id, expires_at, deleted_at, created_at "
            "FROM waypoints WHERE synced_to_hq IS NULL "
            "OR (deleted_at IS NOT NULL AND deleted_at > synced_to_hq) "
            "ORDER BY id LIMIT ?",
            (batch_size,),
        )
        if wp_rows:
            wps = _waypoint_to_payload(wp_rows)
            primary_ok = await _post_waypoints(client, cfg, wps)
            await _post_waypoints_secondary(client, cfg, wps)
            if primary_ok:
                for row in wp_rows:
                    _exec("UPDATE waypoints SET synced_to_hq = ? WHERE id = ?", (now_utc, row["id"]))
                log.info("Waypoints synced: %d rows", len(wp_rows))

        # ── Polygons ──────────────────────────────────────────────────────────
        poly_rows = _query(
            "SELECT id, name, description, type, color, geojson, created_at, updated_at, deleted_at "
            "FROM polygons WHERE synced_to_hq IS NULL "
            "OR updated_at > synced_to_hq "
            "OR (deleted_at IS NOT NULL AND deleted_at > synced_to_hq) "
            "ORDER BY id LIMIT ?",
            (batch_size,),
        )
        if poly_rows:
            polys = _polygon_to_payload(poly_rows)
            primary_ok = await _post_polygons(client, cfg, polys)
            await _post_polygons_secondary(client, cfg, polys)
            if primary_ok:
                for row in poly_rows:
                    _exec("UPDATE polygons SET synced_to_hq = ? WHERE id = ?", (now_utc, row["id"]))
                log.info("Polygons synced: %d rows", len(poly_rows))

                # ── Sensors (BME680 etc.) ─────────────────────────────────────────────
        # Sensors table maps to future datastreams; skipped until HQ has them
        # sensor_rows = _query("SELECT * FROM sensors WHERE id > ? LIMIT ?", ...)


# ── Waypoints ─────────────────────────────────────────────────────────────────
def _waypoint_to_payload(rows: list) -> list:
    wps = []
    for r in rows:
        wps.append({
            "meshtastic_id": r.get("meshtastic_id"),
            "name":          r.get("name", "Waypoint"),
            "description":   r.get("description"),
            "latitude":      r.get("latitude"),
            "longitude":     r.get("longitude"),
            "altitude":      r.get("altitude"),
            "icon":          r.get("icon"),
            "source_id":     r.get("source_id"),
            "source_name":   r.get("source_name"),
            "source_type":   r.get("source_type", "mesh"),
            "hub_node_id":   r.get("hub_id"),
            "expires_at":    r.get("expires_at"),
            "deleted_at":    r.get("deleted_at"),
            "created_at":    r.get("created_at"),
        })
    return wps


async def _post_waypoints(client: httpx.AsyncClient, cfg: dict, waypoints: list) -> bool:
    if not waypoints:
        return True
    payload = {
        "hub_id":    cfg["hub_id"],
        "waypoints": waypoints,
    }
    try:
        r = await client.post(
            f"{cfg['url']}/api/v1/waypoints",
            json=payload,
            headers={"X-jTAK-Key": cfg["push_token"]},
            timeout=15.0,
        )
        if r.status_code in (200, 202):
            body = r.json()
            log.info("HQ accepted %d / %d waypoints", body.get("accepted", "?"), len(waypoints))
            return True
        else:
            log.warning("HQ waypoints HTTP %d: %s", r.status_code, r.text[:200])
            return False
    except Exception as e:
        log.warning("HQ waypoints request failed: %s", e)
        return False


async def _post_waypoints_secondary(client: httpx.AsyncClient, cfg: dict, waypoints: list):
    url = cfg.get("url_secondary", "")
    if not url or not waypoints:
        return
    try:
        await _post_waypoints(client, {**cfg, "url": url}, waypoints)
    except Exception as e:
        log.warning("Secondary HQ waypoints post failed: %s", e)


# ── Polygons ──────────────────────────────────────────────────────────────────
def _polygon_to_payload(rows: list) -> list:
    polys = []
    for r in rows:
        geojson = r.get("geojson")
        try:
            geojson = json.loads(geojson) if isinstance(geojson, str) else geojson
        except Exception:
            pass
        polys.append({
            "hub_polygon_id": r["id"],
            "name":           r.get("name", "Zone"),
            "description":    r.get("description"),
            "type":           r.get("type", "polygon"),
            "color":          r.get("color", "#f97316"),
            "geojson":        geojson,
            "deleted_at":     r.get("deleted_at"),
            "created_at":     r.get("created_at"),
            "updated_at":     r.get("updated_at"),
        })
    return polys


async def _post_polygons(client: httpx.AsyncClient, cfg: dict, polygons: list) -> bool:
    if not polygons:
        return True
    payload = {
        "hub_id":   cfg["hub_id"],
        "polygons": polygons,
    }
    try:
        r = await client.post(
            f"{cfg['url']}/api/v1/polygons",
            json=payload,
            headers={"X-jTAK-Key": cfg["push_token"]},
            timeout=15.0,
        )
        if r.status_code in (200, 202):
            body = r.json()
            log.info("HQ accepted %d / %d polygons", body.get("accepted", "?"), len(polygons))
            return True
        else:
            log.warning("HQ polygons HTTP %d: %s", r.status_code, r.text[:200])
            return False
    except Exception as e:
        log.warning("HQ polygons request failed: %s", e)
        return False


async def _post_polygons_secondary(client: httpx.AsyncClient, cfg: dict, polygons: list):
    url = cfg.get("url_secondary", "")
    if not url or not polygons:
        return
    try:
        await _post_polygons(client, {**cfg, "url": url}, polygons)
    except Exception as e:
        log.warning("Secondary HQ polygons post failed: %s", e)

# ── Heartbeat ─────────────────────────────────────────────────────────────────
async def _heartbeat(client: httpx.AsyncClient, cfg: dict):
    """Ping HQ /health and log round-trip — lightweight keep-alive."""
    try:
        r = await client.get(f"{cfg['url']}/health", timeout=5.0)
        if r.status_code == 200:
            log.debug("HQ heartbeat OK")
        else:
            log.warning("HQ heartbeat HTTP %d", r.status_code)
    except Exception as e:
        log.warning("HQ heartbeat failed: %s", e)

# ── Main loop ─────────────────────────────────────────────────────────────────
async def main():
    cfg = _load_config()
    if not cfg["enabled"]:
        log.info("Push agent disabled in config — exiting")
        return
    if not cfg["push_token"]:
        log.error("No push_token configured — set hq.push_token in jtak.yaml or JTAK_HQ_KEY env")
        sys.exit(1)

    secondary_note = f"  secondary={cfg['url_secondary']}" if cfg.get("url_secondary") else ""
    log.info("Push agent starting — hub=%s  hq=%s  interval=%ds%s",
             cfg["hub_id"], cfg["url"], cfg["push_interval_sec"], secondary_note)
    _migrate_watermarks_to_flags()

    interval = cfg["push_interval_sec"]
    tick = 0

    async with httpx.AsyncClient() as hb_client:
        while True:
            try:
                if tick % 2 == 0:   # heartbeat every other cycle
                    await _heartbeat(hb_client, cfg)
                await _push_cycle(cfg)
            except Exception as e:
                log.error("Push cycle error: %s", e, exc_info=True)

            tick += 1
            await asyncio.sleep(interval)

if __name__ == "__main__":
    asyncio.run(main())
