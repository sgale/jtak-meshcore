#!/usr/bin/env python3
"""
tactical_monitor.py  —  jTAK RF Performance + Air Quality Logger
=================================================================
One row per received Meshtastic packet.

Reliability:
  - systemd WatchdogSec=120 keepalive (kills+restarts if we hang)
  - Dead-man timer: exits if no packet received for DEADMAN_SECS while connected
    (catches half-open TCP sockets that look alive but carry no data)
  - Reconnects automatically on meshtasticd restart or connection loss
  - Subscribes to pubsub once — never re-subscribes on reconnect (no CPU leak)

Air quality (hub-local BME680):
  - Sampled every 30s in a background thread
  - gas_resistance_ohm: raw MOx reading — drops sharply in smoke/combustion
  - iaq_pct: composite score (gas + humidity weighted) — 100=clean, 0=very poor
  - smoke_delta_pct: % drop in gas resistance vs rolling 10-min baseline
  - smoke_alert: True when resistance drops >50% from baseline (abrupt smoke event)
  - Hub temp/humidity/pressure from onboard sensor (hub_ prefix vs node telemetry)
"""

import meshtastic
import meshtastic.tcp_interface
from pubsub import pub
import csv
import json
import os
import math
import sqlite3
import time
import sys
import threading
from datetime import datetime
from pathlib import Path

# Shared GPS quality state written by jtak-api (routes_status.py)
_GPS_STATE_PATH    = Path("/opt/jtak/data/jtak-gps.json")
_MESH_CHANNELS_PATH = Path("/opt/jtak/data/mesh_channels.json")
_DB_PATH           = "/opt/jtak/data/jtak.db"


# ── Mesh messaging helpers ─────────────────────────────────────────────────────

def _db_write_waypoint(meshtastic_id, name, description, lat, lon, icon,
                       source_id, source_name, expires_unix, hub_id):
    """Write a Meshtastic waypoint to DB. Upserts on meshtastic_id."""
    from datetime import datetime, timezone
    try:
        exp_str = None
        if expires_unix and expires_unix > 0:
            exp_str = datetime.fromtimestamp(expires_unix, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        icon_str = chr(icon) if isinstance(icon, int) and icon > 0 else (icon or None)
        now_str  = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        con = sqlite3.connect(_DB_PATH, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        if meshtastic_id:
            row = con.execute(
                "SELECT id FROM waypoints WHERE meshtastic_id=?", (meshtastic_id,)
            ).fetchone()
            if row:
                con.execute("""
                    UPDATE waypoints SET name=?, description=?, latitude=?, longitude=?,
                    icon=?, expires_at=?, synced_to_hq=NULL WHERE meshtastic_id=?
                """, (name, description, lat, lon, icon_str, exp_str, meshtastic_id))
                con.commit()
                con.close()
                print(f"[waypoint] Updated id={meshtastic_id}: {name!r} at {lat:.5f},{lon:.5f}")
                return
        con.execute("""
            INSERT INTO waypoints
              (meshtastic_id, name, description, latitude, longitude, icon,
               source_id, source_name, source_type, created_at, expires_at, hub_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'mesh', ?, ?, ?)
        """, (meshtastic_id, name or 'Waypoint', description, lat, lon, icon_str,
              source_id, source_name, now_str, exp_str, hub_id))
        con.commit()
        con.close()
        print(f"[waypoint] RX from {source_name}: {name!r} at {lat:.5f},{lon:.5f}")
    except Exception as e:
        print(f"[waypoint] DB write error: {e}")


def _db_delete_waypoint(meshtastic_id):
    """Soft-delete a waypoint by meshtastic_id (phone-side delete broadcast)."""
    from datetime import datetime, timezone
    try:
        now_str = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        con = sqlite3.connect(_DB_PATH, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        cur = con.execute(
            "UPDATE waypoints SET deleted_at=?, synced_to_hq=NULL WHERE meshtastic_id=? AND deleted_at IS NULL",
            (now_str, meshtastic_id)
        )
        con.commit()
        con.close()
        if cur.rowcount:
            print(f"[waypoint] Deleted meshtastic_id={meshtastic_id} (phone delete)")
        else:
            print(f"[waypoint] Delete for unknown/already-deleted meshtastic_id={meshtastic_id}")
    except Exception as e:
        print(f"[waypoint] DB delete error: {e}")


def _db_write_message(direction, from_id, from_name, to_id, to_name,
                      channel_index, channel_name, message,
                      want_ack=0, ack_received=0, mesh_packet_id=None):
    """Write a mesh message to DB. Opens its own connection — safe for threads."""
    try:
        con = sqlite3.connect(_DB_PATH, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""
            INSERT INTO mesh_messages
              (timestamp, direction, from_id, from_name, to_id, to_name,
               channel_index, channel_name, message, want_ack, ack_received,
               mesh_packet_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
              direction, from_id, from_name, to_id, to_name,
              channel_index or 0, channel_name, message, want_ack, ack_received,
              mesh_packet_id))
        con.commit()
        con.close()
    except Exception as e:
        print(f"[mesh] DB write error: {e}")


def _poll_send_queue(iface):
    """Check mesh_send_queue for pending messages and send them via iface."""
    try:
        con = sqlite3.connect(_DB_PATH, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        rows = con.execute(
            "SELECT id, to_id, to_name, channel_index, channel_name, message, want_ack"
            " FROM mesh_send_queue WHERE status='pending' ORDER BY id LIMIT 5"
        ).fetchall()
        con.close()   # release lock before sendText (which can block)
    except Exception as e:
        print(f"[mesh] queue poll error: {e}")
        return

    for row_id, to_id, to_name, ch_idx, ch_name, message, want_ack in rows:
        try:
            iface.sendText(
                message,
                destinationId=to_id,
                wantAck=bool(want_ack),
                channelIndex=int(ch_idx or 0),
            )
            status = 'sent'
            print(f"[mesh] TX → {to_name or to_id}: {message!r}")
        except Exception as e:
            status = 'failed'
            print(f"[mesh] send failed (id={row_id}): {e}")

        # Single connection for all post-send writes
        try:
            con = sqlite3.connect(_DB_PATH, timeout=10)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("UPDATE mesh_send_queue SET status=? WHERE id=?", (status, row_id))
            if status == 'sent':
                con.execute("""
                    INSERT INTO mesh_messages
                      (timestamp, direction, from_id, from_name, to_id, to_name,
                       channel_index, channel_name, message, want_ack)
                    VALUES (?, 'tx', ?, ?, ?, ?, ?, ?, ?, ?)
                """, (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                      hub_state.get('id', 'hub'), hub_state.get('name', 'Hub'),
                      to_id, to_name, int(ch_idx or 0), ch_name, message, int(bool(want_ack))))
            con.commit()
            con.close()
        except Exception as e:
            print(f"[mesh] queue write error: {e}")

def _poll_waypoint_send_queue(iface):
    """Send or delete Meshtastic waypoints queued by the API."""
    try:
        con = sqlite3.connect(_DB_PATH, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        rows = con.execute(
            "SELECT id, wp_id, action, name, description, latitude, longitude,"
            "       icon, expires_at, meshtastic_id, channel_index"
            " FROM waypoint_send_queue WHERE status='pending' ORDER BY id LIMIT 5"
        ).fetchall()
        con.close()
    except Exception as e:
        print(f"[waypoint-tx] queue poll error: {e}")
        return

    for (row_id, wp_id, action, name, description, lat, lon,
         icon, expires_at, meshtastic_id, ch_idx) in rows:
        status = 'failed'
        try:
            if action == 'delete' and meshtastic_id:
                iface.deleteWaypoint(meshtastic_id, channelIndex=int(ch_idx or 0))
                status = 'sent'
                print(f"[waypoint-tx] DELETE meshtastic_id={meshtastic_id}")
            elif action == 'send' and lat is not None and lon is not None:
                # Convert ISO expires_at to Unix timestamp (0 = never)
                expire_unix = 0
                if expires_at:
                    try:
                        from datetime import datetime, timezone
                        expire_unix = int(datetime.strptime(
                            expires_at, '%Y-%m-%dT%H:%M:%SZ'
                        ).replace(tzinfo=timezone.utc).timestamp())
                    except Exception:
                        pass
                # icon is stored as emoji char; Meshtastic wants the codepoint int
                icon_cp = ord(icon) if icon and len(icon) == 1 else 0
                iface.sendWaypoint(
                    name=name or 'Waypoint',
                    description=description or '',
                    icon=icon_cp,
                    expire=expire_unix,
                    waypoint_id=wp_id,  # use DB id so deletes can reference it
                    latitude=lat,
                    longitude=lon,
                    channelIndex=int(ch_idx or 0),
                    wantAck=False,
                )
                status = 'sent'
                print(f"[waypoint-tx] SEND {name!r} @ {lat:.5f},{lon:.5f} wp_id={wp_id}")
        except Exception as e:
            print(f"[waypoint-tx] send error (row={row_id}): {e}")

        try:
            con = sqlite3.connect(_DB_PATH, timeout=10)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("UPDATE waypoint_send_queue SET status=? WHERE id=?", (status, row_id))
            con.commit()
            con.close()
        except Exception as e:
            print(f"[waypoint-tx] status write error: {e}")


def _read_gps_state() -> dict:
    try:
        return json.loads(_GPS_STATE_PATH.read_text())
    except Exception:
        return {}

# Watchdog: notify systemd we're alive. Graceful if sdnotify absent.
try:
    from sdnotify import SystemdNotifier
    _sd = SystemdNotifier()
    def watchdog_ping():
        _sd.notify('WATCHDOG=1')
except ImportError:
    def watchdog_ping():
        pass

# BME680: graceful if sensor absent or wiring fault
try:
    import bme680 as _bme680_mod
    _bme_sensor = None
    try:
        _bme_sensor = _bme680_mod.BME680(_bme680_mod.I2C_ADDR_SECONDARY)   # 0x77
    except Exception:
        try:
            _bme_sensor = _bme680_mod.BME680(_bme680_mod.I2C_ADDR_PRIMARY) # 0x76
        except Exception:
            _bme_sensor = None
    if _bme_sensor:
        _bme_sensor.set_humidity_oversample(_bme680_mod.OS_2X)
        _bme_sensor.set_pressure_oversample(_bme680_mod.OS_4X)
        _bme_sensor.set_temperature_oversample(_bme680_mod.OS_8X)
        _bme_sensor.set_filter(_bme680_mod.FILTER_SIZE_3)
        _bme_sensor.set_gas_status(_bme680_mod.ENABLE_GAS_MEAS)
        _bme_sensor.set_gas_heater_temperature(320)
        _bme_sensor.set_gas_heater_duration(150)
        _bme_sensor.select_gas_heater_profile(0)
    BME680_PRESENT = _bme_sensor is not None
except ImportError:
    BME680_PRESENT = False
    _bme_sensor = None

# ── Configuration ─────────────────────────────────────────────────────────────
HUB_HOST          = 'localhost'
LOG_DIR           = '/opt/jtak/logs/rf'
T114_TX_POWER_DBM = 30
RECONNECT_DELAY   = 15
DEADMAN_SECS      = 60       # exit if no packet for this long while connected
WATCHDOG_INTERVAL = 60       # seconds between systemd watchdog pings
BME_INTERVAL      = 30       # seconds between BME680 reads
BME_BASELINE_SECS = 600      # rolling clean-air baseline window (10 minutes)
SMOKE_DROP_PCT    = 50.0     # gas resistance drop % that triggers smoke_alert
IAQ_HUM_WEIGHT    = 0.25
IAQ_GAS_WEIGHT    = 0.75
IAQ_HUM_REFERENCE = 40.0
IAQ_GAS_BASELINE  = 150000   # Ohms — adjust after 20min warm-up in clean air

# ── CSV columns ───────────────────────────────────────────────────────────────
CSV_COLUMNS = [
    'timestamp', 'packet_time',
    'hub_id', 'hub_name',
    'node_id', 'node_name',
    'packet_id', 'packet_type',
    'hop_count', 'direct_or_relay',
    'rssi', 'snr',
    'payload_bytes', 'freq_mhz', 'path_loss_db',
    'node_lat', 'node_lon', 'node_alt_m',
    'node_speed_mph', 'node_heading_deg', 'node_pos_age_s',
    'hub_lat', 'hub_lon', 'hub_alt_m', 'hub_sats', 'hub_hdop',
    'hub_epx_m', 'hub_epy_m',
    'hub_speed_mph', 'hub_heading_deg',
    'distance_mi', 'bearing_deg', 'elev_delta_m', 'elev_angle_deg',
    'temp_c', 'humidity_pct', 'pressure_hpa',
    'battery_pct', 'channel_util_pct', 'air_util_tx_pct',
    'cpu_temp_c',
    'hub_temp_c', 'hub_humidity_pct', 'hub_pressure_hpa',
    'hub_gas_resistance_ohm', 'hub_iaq_pct',
    'hub_smoke_delta_pct', 'hub_smoke_alert',
    # ── ICS / NIEM / CoT interoperability stubs ──────────────────────────────
    # Null until populated by CAD feed, ICS tool, or operator input.
    # Aligned with: NIEM 6.0 EM domain, IRWIN wildfire schema, NEMSIS 3.5 EMS,
    #               ICS-209 Incident Status Summary, CoT resource typing.
    'incident_id',      # IRWIN_ID (wildfire) / CAD incident number / ICS Incident ID
    'incident_name',    # e.g. "GROVE CREEK FIRE", "MVC HWY 189"
    'unit_type',        # ICS resource type: engine|medic|handcrew|dozer|tender|air|logistics|command
    'unit_status',      # CoT/CAD status: available|dispatched|en_route|on_scene|transport|staged|oos
    'division',         # ICS Division/Group assignment: e.g. "Division A", "Medical Group"
]

PORTNUMS = {
    0: 'UNKNOWN',  1: 'TEXT',     3: 'POSITION',
    4: 'NODEINFO', 32: 'REPLY',   67: 'TELEMETRY',
    68: 'ADMIN',   70: 'ROUTING', 72: 'ATAK',
    250: 'WAYPOINT',
}

hub_state = {
    'id':               None,
    'name':             'HUB',
    'channel_util_pct': None,
    'air_util_tx_pct':  None,
    'freq_mhz':         None,
}

hub_air = {
    'temp_c':             None,
    'humidity_pct':       None,
    'pressure_hpa':       None,
    'gas_resistance_ohm': None,
    'iaq_pct':            None,
    'smoke_delta_pct':    None,
    'smoke_alert':        False,
}
_air_lock         = threading.Lock()
_gas_history      = []
_gas_history_lock = threading.Lock()
last_packet_time  = time.time()

# Per-node position cache for deriving speed + heading between updates
# key: node_id  →  {'lat': float, 'lon': float, 'ts': int}
_prev_positions: dict = {}

# ── Helpers ───────────────────────────────────────────────────────────────────
def now_str():
    return datetime.now().strftime('%H:%M:%S')

def cpu_temp():
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            return round(int(f.read().strip()) / 1000, 1)
    except Exception:
        return None

def haversine(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return round(R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 4)

def calc_bearing(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(lat2r)
    y = (math.cos(lat1r) * math.sin(lat2r)
         - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon))
    return round((math.degrees(math.atan2(x, y)) + 360) % 360, 1)

def calc_elev_angle(dist_mi, elev_delta_m):
    if dist_mi is None or elev_delta_m is None or dist_mi == 0:
        return None
    return round(math.degrees(math.atan2(elev_delta_m, dist_mi * 1609.34)), 2)

MIN_SPEED_MPH  = 0.3   # below this treat as stationary (GPS jitter threshold)
MIN_DELTA_SECS = 5     # ignore position pairs closer than this in time

def derive_motion(node_id, lat, lon, pos_ts):
    """Return (speed_mph, heading_deg) from cached previous position.
    Updates cache. Returns (None, None) if insufficient data or stationary."""
    if None in (lat, lon, pos_ts):
        return None, None
    prev = _prev_positions.get(node_id)
    _prev_positions[node_id] = {'lat': lat, 'lon': lon, 'ts': pos_ts}
    if not prev:
        return None, None
    delta_t = pos_ts - prev['ts']
    if delta_t < MIN_DELTA_SECS:
        return None, None
    dist_mi = haversine(prev['lat'], prev['lon'], lat, lon)
    if dist_mi is None:
        return None, None
    speed = round((dist_mi / delta_t) * 3600, 2)  # mph
    if speed < MIN_SPEED_MPH:
        return speed, None  # too slow for meaningful heading
    heading = calc_bearing(prev['lat'], prev['lon'], lat, lon)
    return speed, heading


def portnum_name(portnum_raw):
    if isinstance(portnum_raw, int):
        return PORTNUMS.get(portnum_raw, f'PORT_{portnum_raw}')
    return str(portnum_raw).replace('_APP', '').replace('_', '')

def csv_path():
    name = hub_state['name'].replace(' ', '_')
    date = datetime.now().strftime('%Y-%m-%d')
    return os.path.join(LOG_DIR, f'rf_log_{name}_{date}.csv')

def ensure_csv(path):
    os.makedirs(LOG_DIR, exist_ok=True)
    if not os.path.exists(path):
        with open(path, 'w', newline='') as f:
            csv.DictWriter(f, fieldnames=CSV_COLUMNS).writeheader()
        print(f"[{now_str()}] Log: {path}")

def calculate_iaq(resistance, humidity):
    if resistance is None or humidity is None:
        return None
    if humidity >= IAQ_HUM_REFERENCE:
        hum_score = (100 - humidity) / (100 - IAQ_HUM_REFERENCE) * (IAQ_HUM_WEIGHT * 100)
    else:
        hum_score = (humidity / IAQ_HUM_REFERENCE) * (IAQ_HUM_WEIGHT * 100)
    gas_score = min((resistance / IAQ_GAS_BASELINE) * (IAQ_GAS_WEIGHT * 100),
                    IAQ_GAS_WEIGHT * 100)
    return round(hum_score + gas_score, 1)

def smoke_delta(current_ohms):
    now = time.time()
    with _gas_history_lock:
        cutoff = now - BME_BASELINE_SECS
        while _gas_history and _gas_history[0][0] < cutoff:
            _gas_history.pop(0)
        if len(_gas_history) < 3:
            _gas_history.append((now, current_ohms))
            return None, False
        baseline = sum(o for _, o in _gas_history) / len(_gas_history)
        _gas_history.append((now, current_ohms))
    if baseline == 0:
        return None, False
    delta_pct = round((current_ohms - baseline) / baseline * 100, 1)
    alert = delta_pct < -SMOKE_DROP_PCT
    return delta_pct, alert

# ── BME680 background thread ──────────────────────────────────────────────────
def bme680_thread():
    if not BME680_PRESENT:
        print(f"[{now_str()}] BME680 not found — hub air quality columns will be empty")
        return
    print(f"[{now_str()}] BME680 thread started — warming up heater (~30s)...")
    while True:
        try:
            if _bme_sensor.get_sensor_data():
                stable     = _bme_sensor.data.heat_stable
                resistance = _bme_sensor.data.gas_resistance if stable else None
                humidity   = _bme_sensor.data.humidity
                iaq        = calculate_iaq(resistance, humidity)
                delta, alert = smoke_delta(resistance) if resistance else (None, False)
                with _air_lock:
                    hub_air['temp_c']             = round(_bme_sensor.data.temperature, 2)
                    hub_air['humidity_pct']        = round(humidity, 2)
                    hub_air['pressure_hpa']        = round(_bme_sensor.data.pressure, 2)
                    hub_air['gas_resistance_ohm']  = int(resistance) if resistance else None
                    hub_air['iaq_pct']             = iaq
                    hub_air['smoke_delta_pct']     = delta
                    hub_air['smoke_alert']         = alert
                if alert:
                    print(f"[{now_str()}] *** SMOKE ALERT *** resistance dropped "
                          f"{abs(delta):.1f}% from baseline "
                          f"({int(resistance):,} Ohms) IAQ:{iaq}%")
        except Exception as e:
            print(f"[{now_str()}] BME680 read error: {e}")
        time.sleep(BME_INTERVAL)

# ── Packet handler ────────────────────────────────────────────────────────────
def on_receive(packet, interface):
    global last_packet_time
    try:
        from_id = packet.get('fromId')
        if not from_id:
            return

        last_packet_time = time.time()

        my_id   = hub_state['id']
        my_node = interface.nodes.get(my_id, {}) if my_id else {}
        my_pos  = my_node.get('position', {})
        hub_lat  = my_pos.get('latitude')
        hub_lon  = my_pos.get('longitude')
        hub_alt  = my_pos.get('altitude')
        hub_sats = my_pos.get('satsInView')
        hub_pos_ts = my_pos.get('time')
        _gps = _read_gps_state()
        hub_hdop  = _gps.get('hdop')
        hub_epx_m = _gps.get('epx_m')
        hub_epy_m = _gps.get('epy_m')
        # Prefer gpsd native speed/heading (ground truth); derive_motion as fallback
        if _gps.get('speed_mph') is not None:
            hub_speed   = _gps['speed_mph']
            hub_heading = _gps.get('heading_deg')   # None when stationary — correct
        else:
            hub_speed, hub_heading = derive_motion(
                f'__hub_{my_id}', hub_lat, hub_lon, hub_pos_ts
            )

        decoded     = packet.get('decoded', {})
        portnum_raw = decoded.get('portnum', 0)
        portnum_str = portnum_name(portnum_raw)
        tele        = decoded.get('telemetry', {})
        env         = tele.get('environmentMetrics', {})
        dev         = tele.get('deviceMetrics', {})
        chan_util   = dev.get('channelUtilization')
        air_util    = dev.get('airUtilTx')

        if from_id == my_id:
            if chan_util is not None:
                hub_state['channel_util_pct'] = round(chan_util, 2)
            if air_util is not None:
                hub_state['air_util_tx_pct'] = round(air_util, 2)
            return

        node      = interface.nodes.get(from_id, {})
        node_name = node.get('user', {}).get('longName', from_id)
        pos       = node.get('position', {})
        node_lat  = pos.get('latitude')
        node_lon  = pos.get('longitude')
        node_alt  = pos.get('altitude')
        node_pos_ts = pos.get('time')
        node_pos_age = (round(time.time() - node_pos_ts) if node_pos_ts else None)
        # Always update position cache (side effect), then prefer protobuf native values
        derived_speed, derived_heading = derive_motion(from_id, node_lat, node_lon, node_pos_ts)
        mesh_gs = pos.get('groundSpeed')   # m/s (uint32) from Meshtastic Position proto
        mesh_gt = pos.get('groundTrack')   # centidegrees (uint32); ÷100 = degrees true north
        if mesh_gs:
            node_speed   = round(mesh_gs * 2.23694, 2)   # m/s → mph
            node_heading = round(mesh_gt / 100.0, 1) if mesh_gt is not None else None
        else:
            node_speed, node_heading = derived_speed, derived_heading

        rssi        = packet.get('rxRssi')
        snr         = packet.get('rxSnr')
        pkt_id      = hex(packet.get('id', 0))
        rx_time     = packet.get('rxTime', '')
        payload_len = packet.get('payloadLen')
        hop_start   = packet.get('hopStart', 0)
        hop_limit   = packet.get('hopLimit', hop_start)
        hop_count   = (hop_start - hop_limit) if hop_start else 0
        relay_flag  = 'DIRECT' if hop_count == 0 else f'RELAY_{hop_count}'
        path_loss   = round(T114_TX_POWER_DBM - rssi, 1) if rssi is not None else None

        temp     = env.get('temperature')
        humidity = env.get('relativeHumidity')
        pressure = env.get('barometricPressure')
        battery  = (dev.get('batteryLevel')
                    or node.get('deviceMetrics', {}).get('batteryLevel'))

        dist    = haversine(hub_lat, hub_lon, node_lat, node_lon)
        brng    = calc_bearing(hub_lat, hub_lon, node_lat, node_lon)
        e_delta = (round(node_alt - hub_alt, 1)
                   if node_alt is not None and hub_alt is not None else None)
        e_angle = calc_elev_angle(dist, e_delta)

        with _air_lock:
            air = dict(hub_air)

        row = {
            'timestamp':              datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'packet_time':            rx_time,
            'hub_id':                 my_id,
            'hub_name':               hub_state['name'],
            'node_id':                from_id,
            'node_name':              node_name,
            'packet_id':              pkt_id,
            'packet_type':            portnum_str,
            'hop_count':              hop_count,
            'direct_or_relay':        relay_flag,
            'rssi':                   rssi,
            'snr':                    snr,
            'payload_bytes':          payload_len,
            'freq_mhz':               hub_state.get('freq_mhz'),
            'path_loss_db':           path_loss,
            'node_lat':               node_lat,
            'node_lon':               node_lon,
            'node_alt_m':             node_alt,
            'node_speed_mph':         node_speed,
            'node_heading_deg':       node_heading,
            'node_pos_age_s':         node_pos_age,
            'hub_lat':                hub_lat,
            'hub_lon':                hub_lon,
            'hub_alt_m':              hub_alt,
            'hub_sats':               hub_sats,
            'hub_hdop':               hub_hdop,
            'hub_epx_m':              hub_epx_m,
            'hub_epy_m':              hub_epy_m,
            'hub_speed_mph':          hub_speed,
            'hub_heading_deg':        hub_heading,
            'distance_mi':            dist,
            'bearing_deg':            brng,
            'elev_delta_m':           e_delta,
            'elev_angle_deg':         e_angle,
            'temp_c':                 temp,
            'humidity_pct':           humidity,
            'pressure_hpa':           pressure,
            'battery_pct':            battery,
            'channel_util_pct':       hub_state.get('channel_util_pct'),
            'air_util_tx_pct':        hub_state.get('air_util_tx_pct'),
            'cpu_temp_c':             cpu_temp(),
            'hub_temp_c':             air['temp_c'],
            'hub_humidity_pct':       air['humidity_pct'],
            'hub_pressure_hpa':       air['pressure_hpa'],
            'hub_gas_resistance_ohm': air['gas_resistance_ohm'],
            'hub_iaq_pct':            air['iaq_pct'],
            'hub_smoke_delta_pct':    air['smoke_delta_pct'],
            'hub_smoke_alert':        air['smoke_alert'],
            # ICS / NIEM / CoT stubs — null until populated by operator/CAD
            'incident_id':            None,
            'incident_name':          None,
            'unit_type':              None,
            'unit_status':            None,
            'division':               None,
        }

        path = csv_path()
        ensure_csv(path)
        with open(path, 'a', newline='') as f:
            csv.DictWriter(f, fieldnames=CSV_COLUMNS).writerow(row)

        # ── Store text messages in mesh_messages ─────────────────────────────
        if portnum_str in ('TEXT', 'TEXTMESSAGE', 'RANGETEST'):
            text_payload = decoded.get('text', '').strip()
            if text_payload:
                to_raw  = packet.get('to')
                to_id   = '^all' if to_raw in (None, 0xFFFFFFFF, 4294967295) else f'!{to_raw:08x}'
                ch_idx  = packet.get('channel', 0)
                try:
                    _ch_map = {c['index']: c['name'] for c in json.loads(_MESH_CHANNELS_PATH.read_text())}
                    ch_name = _ch_map.get(ch_idx, f'Channel {ch_idx}')
                except Exception:
                    ch_name = None
                _db_write_message('rx', from_id, node_name, to_id, None,
                                  ch_idx, ch_name, text_payload,
                                  mesh_packet_id=packet.get('id'))
                print(f"[mesh] RX TEXT from {node_name}: {text_payload!r}")

        # ── Store waypoints ───────────────────────────────────────────────────
        elif portnum_str == 'WAYPOINT' or portnum_raw == 250:
            print(f"[waypoint] RAW packet keys: {list(packet.keys())}")
            print(f"[waypoint] RAW decoded: {decoded}")
            wp = decoded.get('waypoint', {})
            print(f"[waypoint] wp dict: {wp}")
            wp_lat = wp.get('latitudeI', 0) / 1e7 if wp.get('latitudeI') else None
            wp_lon = wp.get('longitudeI', 0) / 1e7 if wp.get('longitudeI') else None
            wp_expire = wp.get('expire')
            print(f"[waypoint] lat={wp_lat} lon={wp_lon} expire={wp_expire}")
            # expire=1 (Unix epoch+1s) is the Meshtastic delete sentinel
            if wp_expire == 1 and wp.get('id'):
                _db_delete_waypoint(wp.get('id'))
            elif wp_lat and wp_lon:
                _db_write_waypoint(
                    meshtastic_id=wp.get('id'),
                    name=wp.get('name', 'Waypoint'),
                    description=wp.get('description', '') or None,
                    lat=wp_lat,
                    lon=wp_lon,
                    icon=wp.get('icon'),
                    source_id=from_id,
                    source_name=node_name,
                    expires_unix=wp_expire,
                    hub_id=my_id,
                )
            elif wp.get('id'):
                # lat/lon == 0 also signals delete
                _db_delete_waypoint(wp.get('id'))

        alert_tag = ' *** SMOKE ***' if air['smoke_alert'] else ''
        r_str   = f"{rssi}dBm"        if rssi      is not None else 'N/A'
        s_str   = f"{snr}dB"          if snr       is not None else 'N/A'
        d_str   = f"{dist}mi"         if dist      is not None else '?mi'
        b_str   = f"{brng}deg"        if brng      is not None else '?deg'
        pl_str  = f"PL:{path_loss}dB" if path_loss is not None else ''
        iaq_str = f"IAQ:{air['iaq_pct']}%" if air['iaq_pct'] is not None else ''
        print(f"[{now_str()}] {relay_flag:<10} {node_name:<16} "
              f"RSSI:{r_str:<9} SNR:{s_str:<7} {d_str:<9} {b_str:<7} "
              f"{pl_str} {iaq_str}{alert_tag}")

    except Exception as e:
        print(f"[{now_str()}] ERROR in on_receive: {e}")

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    global last_packet_time
    os.makedirs(LOG_DIR, exist_ok=True)

    t = threading.Thread(target=bme680_thread, daemon=True, name='bme680')
    t.start()

    subscribed    = False
    last_watchdog = time.time()
    connect_time  = None
    print(f"[{now_str()}] jTAK RF Logger starting — host: {HUB_HOST}")

    while True:
        iface = None
        try:
            print(f"[{now_str()}] Connecting to meshtasticd...")
            iface = meshtastic.tcp_interface.TCPInterface(hostname=HUB_HOST)

            my_user = iface.getMyUser()
            hub_state['id']   = my_user.get('id')
            hub_state['name'] = my_user.get('longName', 'HUB')

            try:
                lora = iface.localNode.localConfig.lora
                ch   = getattr(lora, 'channel_num', 84)
                hub_state['freq_mhz'] = round(902.0 + ch * 0.25, 3)
            except Exception:
                hub_state['freq_mhz'] = None

            if not subscribed:
                pub.subscribe(on_receive, "meshtastic.receive")
                subscribed = True

            # Export channel list for API
            try:
                channels = [
                    {'index': ch.index,
                     'name': ch.settings.name or f'Channel {ch.index}',
                     'role': ch.role}
                    for ch in iface.localNode.channels if ch.role != 0
                ]
                _MESH_CHANNELS_PATH.write_text(json.dumps(channels))
            except Exception:
                pass

            ensure_csv(csv_path())
            connect_time     = time.time()
            last_packet_time = time.time()
            print(f"[{now_str()}] Active — {hub_state['name']} "
                  f"({hub_state['id']}) @ {hub_state.get('freq_mhz')}MHz")
            if BME680_PRESENT:
                print(f"[{now_str()}] BME680 online — smoke alert at "
                      f"{SMOKE_DROP_PCT}% resistance drop over {BME_BASELINE_SECS}s baseline")

            while iface.isConnected:
                now = time.time()

                # Systemd watchdog
                if now - last_watchdog >= WATCHDOG_INTERVAL:
                    watchdog_ping()
                    last_watchdog = now

                # Dead-man timer — force restart if connection is silently stale
                connected_for = now - connect_time
                silent_for    = now - last_packet_time
                if connected_for > 30 and silent_for > DEADMAN_SECS:
                    print(f"[{now_str()}] DEAD-MAN: no packet for {int(silent_for)}s — "
                          f"restarting to clear stale connection")
                    sys.exit(1)

                # Outbound queues
                _poll_send_queue(iface)
                _poll_waypoint_send_queue(iface)

                time.sleep(1)

        except KeyboardInterrupt:
            print(f"\n[{now_str()}] Stopped.")
            sys.exit(0)

        except Exception as e:
            print(f"[{now_str()}] Connection error: {e}")

        finally:
            if iface:
                try:
                    iface.close()
                except Exception:
                    pass

        print(f"[{now_str()}] Reconnecting in {RECONNECT_DELAY}s...")
        time.sleep(RECONNECT_DELAY)

if __name__ == '__main__':
    main()
