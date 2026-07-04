#!/usr/bin/env python3
"""
meshtastic_scanner.py — Passive Meshtastic area scanner for jTAK hubs.

Connects to a T114 (or any Meshtastic device) via USB serial and passively
listens for LongFast traffic. Writes live state to scanner_state.json and
appends all packets to a timestamped CSV log.

Runs headless as a systemd service (jtak-scanner.service).
Web UI available at /jtak/scanner.html via the jTAK frontend.

Usage:
    /opt/jtak/venv/bin/python /opt/jtak/tools/meshtastic_scanner.py
    /opt/jtak/venv/bin/python /opt/jtak/tools/meshtastic_scanner.py --port /dev/ttyACM0
"""

import argparse
import csv
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import meshtastic.serial_interface
from pubsub import pub

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_PORT      = "/dev/ttyACM0"
STATE_FILE        = "/opt/jtak/data/scanner_state.json"
CSV_DIR           = "/opt/jtak/data"
MAX_RECENT        = 50
PRESET_LABEL      = "LongFast SF11/BW250 906.875 MHz"

# ── State ─────────────────────────────────────────────────────────────────────
_nodes:   dict  = {}   # node_id → node dict
_packets: list  = []   # recent packets, newest first, capped at MAX_RECENT
_stats          = {"total": 0, "start": time.time()}
_csv_writer     = None
_csv_file       = None


# ── Helpers ───────────────────────────────────────────────────────────────────
def _portnum_label(portnum) -> str:
    mapping = {
        "TEXT_MESSAGE_APP": "TEXT",
        "POSITION_APP":     "POSITION",
        "NODEINFO_APP":     "NODEINFO",
        "TELEMETRY_APP":    "TELEMETRY",
        "ROUTING_APP":      "ROUTING",
        "ADMIN_APP":        "ADMIN",
        "RANGE_TEST_APP":   "RANGETEST",
        "WAYPOINT_APP":     "WAYPOINT",
    }
    s = str(portnum).replace("PortNum.", "")
    return mapping.get(s, s[:12])


def _write_state():
    state = {
        "preset":      PRESET_LABEL,
        "start_time":  _stats["start"],
        "total_packets": _stats["total"],
        "updated":     time.time(),
        "nodes":       list(_nodes.values()),
        "recent":      _packets[:MAX_RECENT],
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    Path(tmp).replace(STATE_FILE)


# ── Packet handler ────────────────────────────────────────────────────────────
def _on_receive(packet, interface):
    now     = time.time()
    decoded = packet.get("decoded", {})
    portnum = decoded.get("portnum", "UNKNOWN")
    label   = _portnum_label(portnum)

    from_id   = packet.get("fromId") or f"!{packet.get('from', 0):08x}"
    rssi      = packet.get("rxRssi")
    snr       = packet.get("rxSnr")
    hop_start = packet.get("hopStart", 0)
    hop_limit = packet.get("hopLimit", 0)
    hop_count = (hop_start - hop_limit) if hop_start else 0
    channel   = packet.get("channel", 0)

    long_name = short_name = hardware = None
    if label == "NODEINFO":
        info       = decoded.get("user", {})
        long_name  = info.get("longName", "")
        short_name = info.get("shortName", "")
        hardware   = str(info.get("hwModel", "")).replace("_", " ")

    lat = lon = alt_m = None
    if label == "POSITION":
        pos   = decoded.get("position", {})
        lat   = pos.get("latitudeI", 0) / 1e7 or None
        lon   = pos.get("longitudeI", 0) / 1e7 or None
        alt_m = pos.get("altitude")
        if lat == 0.0: lat = None
        if lon == 0.0: lon = None

    payload_note = ""
    if label == "TEXT":
        text = decoded.get("text", "")
        payload_note = text[:60] if text else "[encrypted]"

    # Update node summary
    node = _nodes.setdefault(from_id, {
        "node_id":    from_id,
        "long_name":  None, "short_name": None, "hardware": None,
        "rssi": rssi, "snr": snr, "hops": hop_count,
        "pkt_count": 0, "first_seen": now, "last_seen": now,
        "lat": None, "lon": None,
    })
    node["pkt_count"] += 1
    node["last_seen"]  = now
    if rssi      is not None: node["rssi"]       = rssi
    if snr       is not None: node["snr"]        = snr
    node["hops"] = hop_count
    if long_name:             node["long_name"]  = long_name
    if short_name:            node["short_name"] = short_name
    if hardware:              node["hardware"]   = hardware
    if lat       is not None: node["lat"]        = lat
    if lon       is not None: node["lon"]        = lon

    _stats["total"] += 1

    # Recent packets
    _packets.insert(0, {
        "ts":        now,
        "ts_str":    datetime.now().strftime("%H:%M:%S"),
        "node_id":   from_id,
        "name":      node.get("long_name") or node.get("short_name") or from_id,
        "type":      label,
        "rssi":      rssi,
        "snr":       snr,
        "hops":      hop_count,
        "lat":       lat,
        "lon":       lon,
        "note":      payload_note,
        "channel":   channel,
    })
    if len(_packets) > MAX_RECENT:
        _packets.pop()

    # CSV
    if _csv_writer:
        _csv_writer.writerow({
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "node_id":      from_id,
            "long_name":    long_name  or node.get("long_name", ""),
            "short_name":   short_name or node.get("short_name", ""),
            "hardware":     hardware   or node.get("hardware", ""),
            "packet_type":  label,
            "rssi":         rssi,
            "snr":          snr,
            "hop_count":    hop_count,
            "lat":          lat,
            "lon":          lon,
            "alt_m":        alt_m,
            "channel":      channel,
            "payload_note": payload_note,
        })
        _csv_file.flush()

    _write_state()


# ── Main ──────────────────────────────────────────────────────────────────────
CSV_FIELDS = [
    "timestamp", "node_id", "long_name", "short_name", "hardware",
    "packet_type", "rssi", "snr", "hop_count", "lat", "lon", "alt_m",
    "channel", "payload_note",
]


def main():
    global _csv_writer, _csv_file

    parser = argparse.ArgumentParser(description="Passive Meshtastic area scanner")
    parser.add_argument("--port", default=DEFAULT_PORT,
                        help=f"Serial port (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    # CSV
    csv_path = Path(CSV_DIR) / f"scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    _csv_file   = open(csv_path, "w", newline="")
    _csv_writer = csv.DictWriter(_csv_file, fieldnames=CSV_FIELDS)
    _csv_writer.writeheader()
    _csv_file.flush()

    # Initial state file so the API doesn't 404 before any packets arrive
    _write_state()

    pub.subscribe(_on_receive, "meshtastic.receive")

    print(f"[scanner] Connecting to {args.port}...")
    try:
        iface = meshtastic.serial_interface.SerialInterface(args.port)
    except Exception as e:
        print(f"[scanner] Failed to connect: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[scanner] Connected. Listening on {PRESET_LABEL}")
    print(f"[scanner] CSV: {csv_path}")
    print(f"[scanner] State: {STATE_FILE}")

    def _handle_exit(sig, frame):
        print(f"\n[scanner] Shutting down. Packets: {_stats['total']}  Nodes: {len(_nodes)}")
        try: iface.close()
        except: pass
        if _csv_file:
            _csv_file.close()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _handle_exit)
    signal.signal(signal.SIGTERM, _handle_exit)

    # Keep alive — packet handling happens via pubsub callbacks
    while True:
        _write_state()
        time.sleep(5)


if __name__ == "__main__":
    main()
