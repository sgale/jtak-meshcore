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
RF_CSV_COLUMNS = ["timestamp", "source_type", "node_id", "node_name", "hub_id",
                  "node_lat", "node_lon", "rssi", "snr", "freq_mhz", "packet_type"]


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
                  want_ack: int = 0, ack_received: int = 0) -> None:
    """Insert one MeshCore message (rx/tx) into `mesh_messages`."""
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "INSERT INTO mesh_messages "
            "(timestamp, direction, from_id, from_name, to_id, to_name, "
            " channel_index, channel_name, message, want_ack, ack_received) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), direction, from_id, from_name,
             to_id, to_name, channel_index or 0, channel_name, message, want_ack, ack_received),
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
                  snr: float | None, freq_mhz: float | None, packet_type: str | None) -> None:
    """Append one observation to the MeshCore RF log; csv_watcher tails this to write
    positions + rf_metrics AND broadcast the live {type:'rf'} WS event (+ LED/map).

    RSSI note: on this MeshAdv HAT the SX126x GetPacketStatus RSSI bytes read back
    invalid (~0/-1 dBm) while SNR is valid. A real received-packet RSSI is always well
    below -5 dBm, so anything above that is the hardware garbage value — write blank
    (→ NULL) rather than a misleading 0. SNR (the better LoRa link metric) is kept.
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
    }
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RF_CSV_COLUMNS)
        if new_file:
            w.writeheader()
        w.writerow(row)


# ── GPS (ATGM336H on /dev/serial0) ──
def _nmea_to_deg(val, hemi):
    if not val:
        return None
    d = int(float(val) / 100)
    m = float(val) - d * 100
    dec = d + m / 60.0
    return -dec if hemi in ("S", "W") else dec


def read_gps(port: str, baud: int, dur: int):
    """Return (lat, lon) once a fix is parsed from NMEA, else (None, None)."""
    try:
        import serial
        ser = serial.Serial(port, baud, timeout=1)
    except Exception as e:
        log.warning(f"[gps] open failed: {e}")
        return None, None
    t0 = time.time()
    try:
        while time.time() - t0 < dur:
            raw = ser.readline().decode("ascii", "replace").strip()
            f = raw.split(",")
            if raw[3:6] == "RMC" and len(f) > 6 and f[2] == "A" and f[3]:
                return _nmea_to_deg(f[3], f[4]), _nmea_to_deg(f[5], f[6])
            if raw[3:6] == "GGA" and len(f) > 6 and f[6] not in ("", "0") and f[2]:
                return _nmea_to_deg(f[2], f[3]), _nmea_to_deg(f[4], f[5])
    finally:
        ser.close()
    return None, None


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

                elif et == MeshEvents.NEW_CHANNEL_MESSAGE:
                    ni = data.get("network_info") or {}
                    sender = data.get("sender_name") or "unknown"
                    text = data.get("message_text") or ""
                    ch_name = data.get("channel_name")
                    # Channel msgs are group-encrypted: sender is a self-declared name.
                    write_message("rx", None, sender, None, None, 0, ch_name, text)
                    append_rf_row(sender, sender, hub_name, None, None,
                                  ni.get("rssi"), ni.get("snr"), freq_mhz, "channel_text")
                    log.info(f"[chan] {ch_name} <{sender}>: {text!r}")

                elif et == MeshEvents.NEW_MESSAGE:  # DM (PKI — has sender pubkey)
                    ni = data.get("network_info") or {}
                    pk = data.get("contact_pubkey")
                    pk_hex = pk.hex() if isinstance(pk, (bytes, bytearray)) else (pk or None)
                    sender = data.get("sender_name") or data.get("contact_name") or "unknown"
                    text = data.get("message_text") or ""
                    write_message("rx", pk_hex, sender, None, None, 0, None, text)
                    append_rf_row(pk_hex or sender, sender, hub_name, None, None,
                                  ni.get("rssi"), ni.get("snr"), freq_mhz, "direct_text")
                    log.info(f"[dm] <{sender}>: {text!r}")
            except Exception as e:
                log.error(f"[ingest] {et} handler error: {e}", exc_info=True)

    return JtakIngest()


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

    # 2) Initial GPS fix (own node position)
    g = mc.get("gps") or {}
    lat, lon = read_gps(g.get("port", "/dev/serial0"), g.get("baud", 9600),
                        g.get("fix_timeout_sec", 12))
    if lat is None:
        log.warning("[gps] no fix at startup — advertising WITHOUT location until one is acquired.")

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

    # 7) In-process jTAK ingest (coexists with the companion's phone-push subscriber)
    companion._event_service.subscribe_all(_make_ingest(hub_name, freq))

    await companion.start()
    log.info(f"[node] CompanionRadio started as {node_name!r} ({pubkey[:12]}…)")

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

    # 9) Record the hub's own position row, then periodic advert + GPS refresh
    if lat is not None:
        write_position(pubkey, node_name, lat, lon)

    adv_interval = mc.get("advert_interval_sec", 300)
    gps_refresh = g.get("refresh_sec", 900)
    last_gps = time.time()
    await companion.advertise(flood=True)
    log.info(f"[node] self-advert sent; advertising every {adv_interval}s. Running.")

    try:
        while True:
            await asyncio.sleep(adv_interval)
            # Occasionally refresh our GPS fix so a moving hub keeps its map pin current.
            if time.time() - last_gps >= gps_refresh:
                nlat, nlon = read_gps(g.get("port", "/dev/serial0"), g.get("baud", 9600),
                                      g.get("fix_timeout_sec", 12))
                last_gps = time.time()
                if nlat is not None:
                    companion.set_advert_latlon(nlat, nlon)
                    write_position(pubkey, node_name, nlat, nlon)
            await companion.advertise(flood=True)
            log.info("[node] advert re-sent.")
    finally:
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
