#!/usr/bin/env python3
"""
meshcore_monitor.py — ingest positions / messages / RF from a MeshCore radio into jTAK.

The MeshCore counterpart to the Meshtastic `tactical_monitor.py`. It writes to the
SAME SQLite tables (`positions`, `mesh_messages`, `rf_metrics`) with
`source_type='meshcore'` and `source_id=<MeshCore pubkey>`, so the existing
dashboard, `/api/positions`, and map render MeshCore nodes with zero downstream
changes — this is the "swap the source, keep the app" seam. Collision-safe: MeshCore
nodes key on their pubkey, so they never clash with the legacy Meshtastic "jTAK Adam".

Architecture (resolved "B-prime", proven on hardware):
  ONE process, ONE radio. `CompanionRadio` owns the SX1262 AND backs a
  `CompanionFrameServer` on TCP :5000 so the operator's Android MeshCore app connects
  over IP (it consumes the frame server's single client slot). jTAK ingests
  IN-PROCESS by subscribing an `EventSubscriber` to the CompanionRadio's internal
  `EventService` (NODE_DISCOVERED / NEW_CHANNEL_MESSAGE / NEW_MESSAGE) — NOT as a
  second TCP client, and NOT via the `on_*_received` convenience callbacks (those did
  not fire for adverts/channel messages). The hub advertises its own GPS so remote
  nodes see it on their map.

Config: `meshcore:` section of /opt/jtak/config/jtak.yaml (gitignored — the channel
secret lives there and MUST NOT be committed). Runs under /home/sdg/pymc/venv (the
venv that has pymc_core + pyserial).
"""
import asyncio
import csv
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timezone

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("meshcore")

CONFIG_PATH = "/opt/jtak/config/jtak.yaml"
DB_PATH = "/opt/jtak/data/jtak.db"
LOG_DIR = "/opt/jtak/logs/rf"
SOURCE_TYPE = "meshcore"

# RF-log columns csv_watcher reads by name; unlisted columns default to None there.
# We append this same-format CSV so csv_watcher (in the API) owns the positions/
# rf_metrics writes AND the live WS broadcast + LED + map — full Meshtastic parity.
# The trailing telemetry columns (temp_c/humidity_pct/battery_pct) are ALSO read by
# name in csv_watcher (node_sensors + WS temp fields), so hub-polled telemetry lights
# up the dashboard's sensor/battery panels with no API or frontend changes.
RF_CSV_COLUMNS = ["timestamp", "source_type", "node_id", "node_name", "hub_id",
                  "node_lat", "node_lon", "rssi", "snr", "freq_mhz", "packet_type",
                  "temp_c", "humidity_pct", "battery_pct"]


def _cfg() -> dict:
    """Load the `meshcore:` config block (self-contained: only needs pyyaml)."""
    with open(CONFIG_PATH) as f:
        full = yaml.safe_load(f)
    mc = full.get("meshcore") or {}
    # DB path + RF-log dir are authoritative from the shared config sections.
    global DB_PATH, LOG_DIR
    DB_PATH = (full.get("database") or {}).get("path", DB_PATH)
    LOG_DIR = (full.get("logs") or {}).get("base_path", LOG_DIR)
    return {"mc": mc, "hub_name": (full.get("hub") or {}).get("name", "MCORE")}


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── DB writers (each opens its own short-lived connection — safe from callbacks) ──
def write_position(source_id: str, source_name: str, lat: float, lon: float,
                   alt: float | None = None, db_path: str | None = None) -> str:
    """Insert one MeshCore position into the shared `positions` table."""
    ts = _utcnow()
    con = sqlite3.connect(db_path or DB_PATH, timeout=10)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "INSERT INTO positions "
            "(timestamp, source_id, source_name, source_type, latitude, longitude, altitude, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, source_id, source_name, SOURCE_TYPE, lat, lon, alt, ts),
        )
        con.commit()
    finally:
        con.close()
    return ts


def write_message(direction: str, from_id: str | None, from_name: str | None,
                  to_id: str | None, to_name: str | None, channel_index: int,
                  channel_name: str | None, message: str,
                  want_ack: int = 0, ack_received: int = 0,
                  status: str | None = None) -> None:
    """Insert one MeshCore message (rx/tx) into `mesh_messages`.

    status is the tx delivery result ('sent' | 'failed'); left NULL for rx.
    """
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "INSERT INTO mesh_messages "
            "(timestamp, direction, from_id, from_name, to_id, to_name, "
            " channel_index, channel_name, message, want_ack, ack_received, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), direction, from_id, from_name,
             to_id, to_name, channel_index or 0, channel_name, message,
             want_ack, ack_received, status),
        )
        con.commit()
    finally:
        con.close()


def _rf_log_path() -> str:
    """Today's MeshCore RF log — matches csv_watcher's rf_log_*.csv glob."""
    return os.path.join(LOG_DIR, f"rf_log_meshcore_{date.today():%Y-%m-%d}.csv")


def _fmt(v):
    return "" if v is None else v


def append_rf_row(node_id: str, node_name: str | None, hub_id: str,
                  lat: float | None, lon: float | None, rssi: float | None,
                  snr: float | None, freq_mhz: float | None, packet_type: str | None,
                  temp_c: float | None = None, humidity_pct: float | None = None,
                  battery_pct: float | None = None) -> None:
    """Append one observation to the MeshCore RF log; csv_watcher tails this to write
    positions + rf_metrics AND broadcast the live {type:'rf'} WS event (+ LED/map).

    RSSI note: on this MeshAdv HAT the SX126x GetPacketStatus RSSI bytes read back
    invalid (~0/-1 dBm) while SNR is valid. A real received-packet RSSI is always well
    below -5 dBm, so anything above that is the hardware garbage value — write blank
    (→ NULL) rather than a misleading 0. SNR (the better LoRa link metric) is kept.

    temp_c/humidity_pct/battery_pct come from hub-polled telemetry (packet_type
    'telemetry'); they are blank for advert/text rows.
    """
    if rssi is not None and rssi > -5:
        rssi = None
    path = _rf_log_path()
    new_file = not os.path.exists(path)
    os.makedirs(LOG_DIR, exist_ok=True)
    # csv_watcher parses the timestamp as LOCAL time ("%Y-%m-%d %H:%M:%S") -> UTC.
    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_type": SOURCE_TYPE, "node_id": node_id, "node_name": node_name or node_id,
        "hub_id": hub_id, "node_lat": _fmt(lat), "node_lon": _fmt(lon),
        "rssi": _fmt(rssi), "snr": _fmt(snr), "freq_mhz": _fmt(freq_mhz),
        "packet_type": packet_type or "",
        "temp_c": _fmt(temp_c), "humidity_pct": _fmt(humidity_pct),
        "battery_pct": _fmt(battery_pct),
    }
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RF_CSV_COLUMNS)
        if new_file:
            w.writeheader()
        w.writerow(row)


# ── Persistent contact book (survives restarts; keeps known nodes + their paths) ──
# The dashboard NODES list is already persistent (it's in SQLite). What is NOT is the
# MeshCore radio's contact book — the in-memory list of known nodes AND their routing
# paths that DMs/telemetry need. Without persisting it, every restart forgets everyone
# until they re-advert (why the first telemetry sweep found "no chat nodes"). We
# serialize companion.contacts.to_dicts() (includes out_path) to JSON and reload it at
# startup, so the hub comes back up already knowing its nodes — like the Meshtastic hubs.
_contacts_dirty: "asyncio.Event | None" = None


def _mark_contacts_dirty() -> None:
    """Flag the contact book for a (debounced) save. Safe to call from any callback."""
    if _contacts_dirty is not None:
        _contacts_dirty.set()


def load_contacts(companion, path: str) -> int:
    """Restore the contact book from JSON at startup. Returns count loaded."""
    if not os.path.exists(path):
        return 0
    try:
        with open(path) as f:
            records = json.load(f)
    except Exception as e:
        log.warning(f"[contacts] load failed ({path}): {e}")
        return 0
    if not isinstance(records, list) or not records:
        return 0
    companion.contacts.load_from_dicts(records)  # replaces all (store is empty at boot)
    return len(records)


def save_contacts(companion, path: str) -> int:
    """Atomically write the current contact book (nodes + paths) to JSON."""
    records = companion.contacts.to_dicts()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(records, f)
    os.replace(tmp, path)  # atomic — never leaves a half-written file
    return len(records)


async def _contacts_flusher(companion, path: str, coalesce_sec: int = 5) -> None:
    """Debounced saver: on the first change wait a beat to coalesce a burst of adverts,
    then persist. Keeps disk writes bounded even if many nodes advert at once."""
    assert _contacts_dirty is not None
    while True:
        await _contacts_dirty.wait()
        await asyncio.sleep(coalesce_sec)
        _contacts_dirty.clear()
        try:
            n = save_contacts(companion, path)
            log.info(f"[contacts] saved {n} known node(s) -> {path}")
        except Exception as e:
            log.warning(f"[contacts] save failed: {e}")


# ── GPS (ATGM336H on /dev/serial0) ──
def _nmea_to_deg(val, hemi):
    if not val:
        return None
    d = int(float(val) / 100)
    m = float(val) - d * 100
    dec = d + m / 60.0
    return -dec if hemi in ("S", "W") else dec


def read_gps(port: str, baud: int, dur: int):
    """Parse NMEA for up to `dur`s. Returns (lat, lon, sats, hdop).

    GGA carries fix quality + satellites-used (field 7) + HDOP (field 8); RMC carries
    position but no sat count. We sample the full window and return the BEST (lowest-HDOP)
    GGA seen so a momentary spike doesn't flip the header/LED; lat/lon track the latest fix.
    lat/lon are None until a fix; sats/hdop may be present even without one.
    """
    lat = lon = sats = hdop = None
    try:
        import serial
        ser = serial.Serial(port, baud, timeout=1)
    except Exception as e:
        log.warning(f"[gps] open failed: {e}")
        return None, None, None, None
    # Sample the WHOLE window and keep the BEST fix (lowest HDOP + its sat count) instead
    # of bailing on the first GGA: a single instantaneous reading catches momentary HDOP
    # spikes (a satellite dropping out) that then paint the header/LED "fair/poor" for the
    # entire poll interval even when the fix is steady. Best-of-window matches how a
    # continuous tracker (gpsd on the other hubs) presents a stable lock.
    t0 = time.time()
    try:
        while time.time() - t0 < dur:
            raw = ser.readline().decode("ascii", "replace").strip()
            f = raw.split(",")
            typ = raw[3:6]
            if typ == "GGA" and len(f) > 8:
                # $..GGA,time,lat,N,lon,E,fixQ,numSat,HDOP,alt,...
                g_sats = g_hdop = None
                if f[7]:
                    try:
                        g_sats = int(f[7])
                    except ValueError:
                        pass
                if f[8]:
                    try:
                        g_hdop = float(f[8])
                    except ValueError:
                        pass
                if f[6] not in ("", "0") and f[2]:
                    lat, lon = _nmea_to_deg(f[2], f[3]), _nmea_to_deg(f[4], f[5])
                # Keep the lowest-HDOP sample of the window (and the sats it saw).
                if g_hdop is not None and (hdop is None or g_hdop < hdop):
                    hdop = g_hdop
                    if g_sats is not None:
                        sats = g_sats
                elif sats is None and g_sats is not None:
                    sats = g_sats
            elif typ == "RMC" and len(f) > 6 and f[2] == "A" and f[3] and lat is None:
                lat, lon = _nmea_to_deg(f[3], f[4]), _nmea_to_deg(f[5], f[6])
    finally:
        ser.close()
    return lat, lon, sats, hdop


# ── Hub GPS status file (header/LED read this via routes_status; gpsd is unused here) ──
GPS_STATUS_PATH = "/opt/jtak/data/meshcore-gps.json"


def write_gps_status(lat, lon, sats, hdop) -> None:
    """Publish the hub's own GPS state for the API/header. Atomic write."""
    try:
        os.makedirs(os.path.dirname(GPS_STATUS_PATH), exist_ok=True)
        tmp = GPS_STATUS_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"lat": lat, "lon": lon, "sats": sats, "hdop": hdop,
                       "fix": lat is not None, "ts": _utcnow()}, fh)
        os.replace(tmp, GPS_STATUS_PATH)
    except Exception as e:
        log.warning(f"[gps] status write failed: {e}")


async def gps_status_poll(companion, node_name: str, pubkey: str, g: dict, interval: int):
    """Poll the hub's own GPS every `interval`s: refresh the advert location + hub
    position row, and publish sats/HDOP to the status file the header reads. One reader
    for the serial port (no contention with the advert loop)."""
    port, baud, fto = g.get("port", "/dev/serial0"), g.get("baud", 9600), g.get("fix_timeout_sec", 12)
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(interval)
        # read_gps blocks on the serial port — run it off the event loop.
        lat, lon, sats, hdop = await loop.run_in_executor(None, read_gps, port, baud, fto)
        write_gps_status(lat, lon, sats, hdop)
        if lat is not None:
            # Advertise our location to the mesh (field radios track the hub), and publish
            # to the header via the GPS status file above. We do NOT write the hub into the
            # positions table — the dashboard renders it as its own self-marker from
            # /api/status (hub_position), so writing it as a mesh node would duplicate it.
            companion.set_advert_latlon(lat, lon)
        log.info(f"[gps] poll: fix={'yes' if lat is not None else 'no'} sats={sats} hdop={hdop}")


# ── Persistent identity (serialize the SIGNING seed, not the X25519 key) ──
def load_identity(seed_file: str):
    from pymc_core.protocol.identity import LocalIdentity
    if os.path.exists(seed_file):
        ident = LocalIdentity(seed=bytes.fromhex(open(seed_file).read().strip()))
        log.info(f"[id] loaded persistent identity {ident.get_public_key().hex()[:12]}…")
    else:
        ident = LocalIdentity()
        open(seed_file, "w").write(ident.get_signing_key_bytes().hex())
        os.chmod(seed_file, 0o600)
        log.info(f"[id] generated NEW identity {ident.get_public_key().hex()[:12]}…")
    return ident


# ── In-process ingest: subscribe to CompanionRadio's own EventService ──
def _make_ingest(hub_name: str, frequency: float):
    from pymc_core.node.events.event_service import EventSubscriber
    from pymc_core.node.events.events import MeshEvents

    freq_mhz = frequency / 1e6

    class JtakIngest(EventSubscriber):
        """Route MeshCore mesh events into jTAK: RF/positions via the RF log
        (csv_watcher owns those writes + the live WS broadcast); chat text goes
        straight to mesh_messages, which csv_watcher does not handle."""

        async def handle_event(self, et, data):
            try:
                if et == MeshEvents.NODE_DISCOVERED:
                    pubkey = data.get("public_key")
                    if not pubkey:
                        return
                    name = data.get("name") or pubkey[:8]
                    lat, lon = data.get("lat"), data.get("lon")
                    has_loc = bool(lat or lon)
                    append_rf_row(pubkey, name, hub_name,
                                  float(lat) if has_loc else None,
                                  float(lon) if has_loc else None,
                                  data.get("rssi"), data.get("snr"), freq_mhz, "advert")
                    loc = f"@ {lat:.5f},{lon:.5f}" if has_loc else "(no location)"
                    log.info(f"[advert] {name} {loc} ({pubkey[:8]}…)")
                    # Remember the fine advert fix + when we heard it, so the telemetry
                    # poll knows whether the advert pin is fresh (keep it) or stale (let
                    # coarse telemetry GPS take over).
                    if has_loc:
                        _last_advert_pos[pubkey] = (time.time(), float(lat), float(lon))
                    # The companion auto-added/updated this contact (+ maybe its path);
                    # persist so it survives a restart.
                    _mark_contacts_dirty()

                elif et == MeshEvents.NEW_CHANNEL_MESSAGE:
                    ni = data.get("network_info") or {}
                    sender = data.get("sender_name") or "unknown"
                    text = data.get("message_text") or ""
                    ch_name = data.get("channel_name")
                    # Channel msgs are group-encrypted: sender is a self-declared name.
                    # to_id '^all' -> UI renders the channel (not a node) as recipient.
                    write_message("rx", None, sender, "^all", None, 0, ch_name, text)
                    append_rf_row(sender, sender, hub_name, None, None,
                                  ni.get("rssi"), ni.get("snr"), freq_mhz, "channel_text")
                    log.info(f"[chan] {ch_name} <{sender}>: {text!r}")

                elif et == MeshEvents.NEW_MESSAGE:  # DM (PKI — has sender pubkey)
                    ni = data.get("network_info") or {}
                    pk = data.get("contact_pubkey")
                    pk_hex = pk.hex() if isinstance(pk, (bytes, bytearray)) else (pk or None)
                    sender = data.get("sender_name") or data.get("contact_name") or "unknown"
                    text = data.get("message_text") or ""
                    # DM addressed to us -> the hub is the recipient (show its name, not '?').
                    write_message("rx", pk_hex, sender, None, hub_name, 0, None, text)
                    append_rf_row(pk_hex or sender, sender, hub_name, None, None,
                                  ni.get("rssi"), ni.get("snr"), freq_mhz, "direct_text")
                    log.info(f"[dm] <{sender}>: {text!r}")
            except Exception as e:
                log.error(f"[ingest] {et} handler error: {e}", exc_info=True)

    return JtakIngest()


def _lipo_pct(volts) -> float | None:
    """Rough 1S-LiPo state-of-charge from pack voltage (3.30V≈0%, 4.20V≈100%).
    Telemetry reports battery as VOLTAGE (LPP 0x74); the dashboard's battery field
    wants a percent, so this is an approximation — good enough for a low/ok/full read,
    not a fuel gauge. Clamped to 0–100."""
    if volts is None:
        return None
    try:
        v = float(volts)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    pct = (v - 3.30) / (4.20 - 3.30) * 100.0
    return round(max(0.0, min(100.0, pct)), 0)


# LPP type ids we care about (see pymc_core protocol_response decoder)
_LPP_GPS, _LPP_TEMP, _LPP_HUMIDITY, _LPP_VOLTAGE = 0x88, 0x67, 0x68, 0x74

# Last ADVERT position we heard per node (source_id -> (local_ts, lat, lon)). Adverts
# carry ~0.1m GPS; telemetry carries ~11m CayenneLPP GPS that collapses nearby nodes
# onto one pin. So the map pin comes from the advert, and telemetry only overrides it
# when the advert is stale or the node has clearly moved (see telemetry_poll policy).
_last_advert_pos: dict = {}


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    from math import radians, sin, cos, asin, sqrt
    r = 6371000.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi, dlmb = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlmb / 2) ** 2
    return 2 * r * asin(sqrt(a))


async def telemetry_poll(companion, hub_name: str, freq_mhz: float, tcfg: dict):
    """Periodically pull telemetry (GPS + environment + battery) from every discovered
    CHAT node and feed it through the same RF-log producer as adverts, so positions,
    sensor readings, and battery land on the live dashboard.

    Cost note: each request is a DIRECT REQ→RESPONSE over LoRa (SF11 — slow), so we
    space nodes out and let the operator pick the interval. Requires the remote node to
    grant telemetry (and, for a fix, location) permission in its MeshCore app.
    """
    from pymc_core.companion import ADV_TYPE_CHAT

    interval = tcfg.get("interval_sec", 300)
    want_base = tcfg.get("want_base", True)
    want_location = tcfg.get("want_location", True)
    want_environment = tcfg.get("want_environment", True)
    timeout = tcfg.get("request_timeout_sec", 12)
    per_node_gap = tcfg.get("per_node_gap_sec", 3)
    # Position policy: the fine advert pin wins unless it's older than this, or the
    # coarse telemetry fix has moved more than this many metres from it.
    pos_stale_sec = tcfg.get("position_stale_sec", 900)
    move_threshold_m = tcfg.get("move_threshold_m", 25)
    # Let contacts populate from adverts before the first sweep.
    await asyncio.sleep(tcfg.get("startup_delay_sec", 60))
    log.info(f"[telem] poll active — every {interval}s, all chat nodes "
             f"(base={want_base} loc={want_location} env={want_environment}).")

    while True:
        try:
            contacts = [c for c in companion.get_contacts() if c.adv_type == ADV_TYPE_CHAT]
            if not contacts:
                log.info("[telem] no chat nodes discovered yet — nothing to poll.")
            for c in contacts:
                pk = bytes(c.public_key)
                name = c.name or pk.hex()[:8]
                try:
                    res = await companion.send_telemetry_request(
                        pk, want_base=want_base, want_location=want_location,
                        want_environment=want_environment, timeout=timeout)
                except Exception as e:
                    log.warning(f"[telem] {name}: request error: {e}")
                    await asyncio.sleep(per_node_gap)
                    continue
                if not res.get("success"):
                    log.info(f"[telem] {name}: no response ({res.get('reason')})")
                    await asyncio.sleep(per_node_gap)
                    continue

                lat = lon = temp_c = humidity = batt = None
                for s in (res.get("telemetry_data") or {}).get("sensors") or []:
                    t, v = s.get("type_id"), s.get("value")
                    if t == _LPP_GPS and isinstance(v, dict):
                        lat, lon = v.get("latitude"), v.get("longitude")
                    elif t == _LPP_TEMP:
                        temp_c = v
                    elif t == _LPP_HUMIDITY:
                        humidity = v
                    elif t == _LPP_VOLTAGE:
                        batt = _lipo_pct(v)
                # Drop a null-island fix (0,0) — means "no location grant/fix".
                if lat in (0, 0.0) and lon in (0, 0.0):
                    lat = lon = None

                # Decide whether this coarse telemetry fix should update the map pin.
                # Default: no (keep the finer advert pin). Override only if the advert is
                # stale or the node has clearly moved — so co-located stationary nodes
                # don't collapse onto one point at 11m resolution.
                pos_lat = pos_lon = None
                pos_note = "no gps"
                if lat is not None:
                    adv = _last_advert_pos.get(pk.hex())
                    if adv is None:
                        pos_lat, pos_lon, pos_note = lat, lon, "pos→telem (no advert)"
                    else:
                        adv_ts, adv_lat, adv_lon = adv
                        age = time.time() - adv_ts
                        moved = _dist_m(lat, lon, adv_lat, adv_lon)
                        if age > pos_stale_sec:
                            pos_lat, pos_lon, pos_note = lat, lon, f"pos→telem (advert {age/60:.0f}m old)"
                        elif moved > move_threshold_m:
                            pos_lat, pos_lon, pos_note = lat, lon, f"pos→telem (moved {moved:.0f}m)"
                        else:
                            pos_note = "pos held (advert)"

                append_rf_row(pk.hex(), name, hub_name, pos_lat, pos_lon, None, None,
                              freq_mhz, "telemetry", temp_c=temp_c,
                              humidity_pct=humidity, battery_pct=batt)
                bits = []
                if lat is not None:
                    bits.append(f"{lat:.5f},{lon:.5f} [{pos_note}]")
                if temp_c is not None:
                    bits.append(f"{temp_c:.1f}C")
                if humidity is not None:
                    bits.append(f"{humidity:.0f}%RH")
                if batt is not None:
                    bits.append(f"batt~{batt:.0f}%")
                log.info(f"[telem] {name}: {' '.join(bits) or 'ok (no fields granted)'}")
                await asyncio.sleep(per_node_gap)
            # Requests may have refreshed routing paths — persist the latest.
            if contacts:
                _mark_contacts_dirty()
        except Exception as e:
            log.error(f"[telem] poll loop error: {e}", exc_info=True)
        await asyncio.sleep(interval)


# ── Outbound: drain the dashboard's send queue over the radio ──────────────────
# POST /mesh/send (routes_mesh.py) only INSERTs into mesh_send_queue; on the old
# Meshtastic hub tactical_monitor drained it. MeshCore replaced that worker, so we
# drain it here: channel rows (to_id like '^all') go out via send_channel_message;
# direct rows (to_id == 64-hex contact pubkey) via send_text_message. Sent rows are
# mirrored into mesh_messages (direction 'tx') so they show in the chat history.
def _is_pubkey(to_id: str) -> bool:
    return (isinstance(to_id, str) and len(to_id) == 64
            and all(c in "0123456789abcdefABCDEF" for c in to_id))


def _patch_pymc_ack_length() -> None:
    """pymc_core's ACK handler rejects any ACK payload that isn't EXACTLY 4 bytes.
    MeshCore nodes with extended/redundant ACKs send the 4-byte CRC plus trailing
    metadata (e.g. a 6-byte ACK 'FEA6637F00A7' = CRC 7F63A6FE + 00A7), so a valid
    delivery ACK gets dropped and every DM looks unacknowledged. Accept >=4 bytes and
    use the first 4 (little-endian) as the CRC. Patched here (not in the vendored lib)
    so it stays in our repo and survives a pymc_core reinstall."""
    from pymc_core.node.handlers import ack as _ackmod

    async def process_discrete_ack(self, packet):
        payload = packet.payload
        if len(payload) < 4:
            self.log(f"Invalid ACK length: {len(payload)} bytes (need >=4)")
            return None
        crc = int.from_bytes(payload[:4], "little")
        if len(payload) > 4:
            self.log(f"Extended ACK {len(payload)}B -> CRC={crc:08X} "
                     f"(trailing {payload[4:].hex().upper()})")
        return crc

    _ackmod.AckHandler.process_discrete_ack = process_discrete_ack
    log.info("[patch] pymc_core AckHandler now accepts >=4-byte (extended) ACKs.")


async def _wait_for_any_ack(disp, crcs: list, timeout: float) -> bool:
    """Wait up to `timeout` for ANY of the given ack_crcs (each DM attempt has a fresh
    crc; a late ACK for a prior attempt still counts as delivered)."""
    if disp is None or not crcs:
        return False
    events = [disp.expect_ack(c) for c in crcs]     # fires instantly if already cached
    tasks = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        done, _ = await asyncio.wait(tasks, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        return bool(done)
    finally:
        for t in tasks:
            t.cancel()


async def send_queue_drainer(companion, hub_id: str, hub_name: str,
                             scfg: dict | None = None, poll_sec: int = 3):
    # DM retry (stock MeshCore auto-retries an unacked direct message; pymc_core does
    # a single send, so we loop here). Channel/group sends are fire-and-forget flood
    # (no ACK exists) and are never retried. attempt is incremented each pass so the
    # receiver can dedup; back off a bit more before each retry.
    scfg = scfg or {}
    max_attempts = max(1, int(scfg.get("max_attempts", 3)))
    retry_backoff = float(scfg.get("retry_backoff_sec", 3))
    ack_timeout = float(scfg.get("ack_timeout_sec", 8))
    # The dispatcher matches incoming ACKs on the content-derived ack_crc. pymc_core's
    # own wait_for_ack (via send_text_message(wait_for_ack=True)) instead waits on the
    # packet CRC, which never matches — so we send with its wait OFF and wait on
    # res.expected_ack (the real ack_crc) through the dispatcher ourselves.
    disp = getattr(getattr(companion, "node", None), "dispatcher", None)
    log.info(f"[send] outbound queue drainer up — polling every {poll_sec}s "
             f"(DM: up to {max_attempts} attempts, backoff {retry_backoff}s, "
             f"ack wait {ack_timeout}s).")
    while True:
        try:
            con = sqlite3.connect(DB_PATH, timeout=10)
            con.execute("PRAGMA journal_mode=WAL")
            rows = con.execute(
                "SELECT id,to_id,to_name,channel_index,channel_name,message,want_ack "
                "FROM mesh_send_queue WHERE status='pending' ORDER BY id LIMIT 5"
            ).fetchall()
            con.close()   # release the lock before the (blocking) radio send
        except Exception as e:
            log.error(f"[send] queue poll error: {e}")
            await asyncio.sleep(poll_sec)
            continue

        for rid, to_id, to_name, ch_idx, ch_name, message, want_ack in rows:
            is_dm = _is_pubkey(to_id or "")
            status = "failed"
            attempts_used = 0
            try:
                if is_dm:
                    pk = bytes.fromhex(to_id)
                    sent_crcs = []   # every attempt's ack_crc (each send has a fresh crc)
                    for attempt in range(1, max_attempts + 1):
                        attempts_used = attempt
                        # Hand off the packet (library ACK-wait disabled — it waits on the
                        # wrong CRC), then wait on res.expected_ack via the dispatcher.
                        res = await companion.send_text_message(
                            pk, message, attempt=attempt, wait_for_ack=False)
                        handoff = bool(getattr(res, "success", False))
                        ack_crc = getattr(res, "expected_ack", None)
                        if ack_crc is not None:
                            sent_crcs.append(ack_crc)
                        if not want_ack:
                            acked = handoff            # fire-and-forget DM: no delivery wait
                        else:
                            acked = await _wait_for_any_ack(disp, sent_crcs, ack_timeout)
                        if acked:
                            status = "sent"
                            break
                        if attempt < max_attempts:
                            log.info(f"[send] DM → {to_name or to_id[:12]}: no ACK "
                                     f"(attempt {attempt}/{max_attempts}) — retrying")
                            await asyncio.sleep(retry_backoff * attempt)
                    log.info(f"[send] DM → {to_name or to_id[:12]}: {message!r} -> "
                             f"{status} after {attempts_used} attempt(s)")
                else:
                    ok = await companion.send_channel_message(int(ch_idx or 0), message)
                    status = "sent" if ok else "failed"
                    log.info(f"[send] CH{ch_idx or 0} {ch_name or ''} → {message!r} -> {status}")
            except Exception as e:
                log.error(f"[send] TX error (id={rid}): {e}", exc_info=True)

            try:
                con = sqlite3.connect(DB_PATH, timeout=10)
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("UPDATE mesh_send_queue SET status=? WHERE id=?", (status, rid))
                con.commit()
                con.close()
            except Exception as e:
                log.error(f"[send] status write error (id={rid}): {e}")

            # Record every attempt (sent OR failed) so the dashboard shows outcome.
            # Channel rows use to_id '^all' so the UI renders the channel as recipient;
            # DMs keep the contact pubkey. ack_received mirrors a successful DM ACK.
            ack = 1 if (is_dm and status == "sent") else 0
            write_message("tx", hub_id, hub_name,
                          to_id if is_dm else "^all", to_name,
                          int(ch_idx or 0), None if is_dm else ch_name,
                          message, want_ack=int(bool(want_ack)),
                          ack_received=ack, status=status)
        await asyncio.sleep(poll_sec)


async def run():
    """Main loop: bring up the MeshCore companion node, serve the phone, ingest to jTAK."""
    cfg = _cfg()
    mc, hub_name = cfg["mc"], cfg["hub_name"]
    if not mc or not mc.get("enabled", False):
        log.info("[meshcore] disabled in config — idling.")
        while True:
            await asyncio.sleep(60)

    from pymc_core.hardware.sx1262_wrapper import SX1262Radio
    from pymc_core.companion import CompanionRadio, CompanionFrameServer, ADV_TYPE_CHAT

    # Tolerate extended (>4-byte) MeshCore ACKs before the radio comes up.
    _patch_pymc_ack_length()

    r = mc["radio"]
    radio_kwargs = dict(
        bus_id=r["bus_id"], cs_id=r["cs_id"], cs_pin=r["cs_pin"], reset_pin=r["reset_pin"],
        busy_pin=r["busy_pin"], irq_pin=r["irq_pin"], txen_pin=r["txen_pin"], rxen_pin=r["rxen_pin"],
        frequency=r["frequency"], bandwidth=r["bandwidth"], spreading_factor=r["spreading_factor"],
        coding_rate=r["coding_rate"], tx_power=r["tx_power"], preamble_length=r["preamble_length"],
        is_waveshare=r.get("is_waveshare", True),
        use_dio3_tcxo=r.get("use_dio3_tcxo", True),
        dio3_tcxo_voltage=r.get("dio3_tcxo_voltage", 1.8),
    )
    node_name = mc.get("node_name", "jTAK-Hub")
    freq = r["frequency"]

    # 1) Identity
    identity = load_identity(mc["seed_file"])
    pubkey = identity.get_public_key().hex()

    # 2) Initial GPS fix (own node position + sat count for the header)
    g = mc.get("gps") or {}
    lat, lon, sats, hdop = read_gps(g.get("port", "/dev/serial0"), g.get("baud", 9600),
                                    g.get("fix_timeout_sec", 12))
    write_gps_status(lat, lon, sats, hdop)
    if lat is None:
        log.warning("[gps] no fix at startup — advertising WITHOUT location until one is acquired.")
    else:
        log.info(f"[gps] startup fix {lat:.6f},{lon:.6f} sats={sats} hdop={hdop}")

    # 3) Radio up
    radio = SX1262Radio(**radio_kwargs)
    if not radio.begin():
        log.error("radio begin() FAILED — check the HAT / SPI wiring.")
        return
    log.info(f"[radio] up ({freq/1e6:.3f}MHz/{r['bandwidth']/1e3:.0f}k/SF{r['spreading_factor']}/CR{r['coding_rate']} +TCXO)")

    # 4) Companion node (owns the radio; backs the phone's frame server)
    companion = CompanionRadio(radio=radio, identity=identity, node_name=node_name,
                               adv_type=ADV_TYPE_CHAT)

    # 5) Load channels server-side so channel messages decrypt in-process
    for ch in (mc.get("channels") or []):
        ok = companion.set_channel(ch["index"], ch["name"], bytes.fromhex(ch["secret"]))
        log.info(f"[chan] loaded {ch['name']!r} @ idx {ch['index']} -> {ok}")

    # 6) Own GPS position for adverts
    if lat is not None:
        companion.set_advert_latlon(lat, lon)
        log.info(f"[gps] hub fix {lat:.6f},{lon:.6f} — adverts carry location.")

    # 6.5) Restore the persistent contact book BEFORE start() so the hub comes back up
    #      already knowing its nodes + paths (telemetry can poll them without re-adverts).
    global _contacts_dirty
    _contacts_dirty = asyncio.Event()
    contacts_path = mc.get("contacts_file", "/home/sdg/pymc/hub_contacts.json")
    restored = load_contacts(companion, contacts_path)
    if restored:
        log.info(f"[contacts] restored {restored} known node(s) from {contacts_path} "
                 f"— telemetry can poll immediately, no re-advert needed.")
        # Seed the advert-position cache from the restored fine GPS so the telemetry
        # position policy holds the finer pin from the very first post-boot sweep
        # (instead of falling back to coarse telemetry GPS).
        for c in companion.get_contacts():
            if c.gps_lat or c.gps_lon:
                _last_advert_pos[c.public_key.hex()] = (time.time(), c.gps_lat, c.gps_lon)
    else:
        log.info(f"[contacts] none persisted yet — will learn + save nodes as they advert "
                 f"({contacts_path}).")

    # 7) In-process jTAK ingest (coexists with the companion's phone-push subscriber)
    companion._event_service.subscribe_all(_make_ingest(hub_name, freq))

    await companion.start()
    log.info(f"[node] CompanionRadio started as {node_name!r} ({pubkey[:12]}…)")

    # 7.5) Debounced saver: persists the contact book whenever it changes.
    asyncio.create_task(_contacts_flusher(companion, contacts_path))

    # 8) Expose to the Android app over TCP
    cs = mc.get("companion_server") or {}
    server = None
    if cs.get("enabled", True):
        server = CompanionFrameServer(
            bridge=companion, companion_hash=pubkey[:8],
            port=cs.get("port", 5000), bind_address=cs.get("bind", "0.0.0.0"),
            device_model="jTAK-Hub", device_version="0.1", client_idle_timeout_sec=None,
        )
        await server.start()
        log.info(f"[server] CompanionFrameServer LISTENING on {cs.get('bind','0.0.0.0')}:{cs.get('port',5000)}")

    # 9) Optional: hub-driven telemetry poll (GPS + environment + battery per node)
    tcfg = mc.get("telemetry") or {}
    if tcfg.get("enabled", False):
        asyncio.create_task(telemetry_poll(companion, hub_name, freq / 1e6, tcfg))
        log.info(f"[telem] enabled — polling every {tcfg.get('interval_sec', 300)}s.")

    # 9.5) Outbound: drain the dashboard's mesh_send_queue over the radio (channel + DM).
    asyncio.create_task(send_queue_drainer(companion, pubkey, node_name, mc.get("send")))

    # 10) The hub is the dashboard's own self-marker (rendered from /api/status
    #     hub_position), NOT a mesh node — so we do not write it into the positions table.
    #     Its map pin/location/header all flow from the GPS status file below.

    # 10.5) Single GPS reader: refreshes position + sats/HDOP (for the header) on its own
    #       cadence. Owns the serial port so it never collides with the advert loop.
    gps_interval = g.get("refresh_sec", 120)
    asyncio.create_task(gps_status_poll(companion, node_name, pubkey, g, gps_interval))
    log.info(f"[gps] status poll every {gps_interval}s -> {GPS_STATUS_PATH}")

    adv_interval = mc.get("advert_interval_sec", 300)
    await companion.advertise(flood=True)
    log.info(f"[node] self-advert sent; advertising every {adv_interval}s. Running.")

    try:
        while True:
            await asyncio.sleep(adv_interval)
            await companion.advertise(flood=True)
            log.info("[node] advert re-sent.")
    finally:
        try:
            n = save_contacts(companion, contacts_path)
            log.info(f"[contacts] final save: {n} known node(s) -> {contacts_path}")
        except Exception as e:
            log.warning(f"[contacts] final save failed: {e}")
        if server:
            try:
                await server.stop()
            except Exception:
                pass
        await companion.stop()
        log.info("[meshcore] stopped; radio released.")


if __name__ == "__main__":
    # Test injection (proves the ingest→DB→dashboard path without a radio):
    #   python meshcore_monitor.py testpos <source_id> <name> <lat> <lon> [alt]
    if len(sys.argv) >= 6 and sys.argv[1] == "testpos":
        _cfg()  # resolve DB_PATH from config
        alt = float(sys.argv[6]) if len(sys.argv) >= 7 else None
        ts = write_position(sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5]), alt)
        print(f"[meshcore] wrote test position for {sys.argv[2]!r} @ {ts}")
    else:
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            print("\n[meshcore] interrupted — exiting.")
