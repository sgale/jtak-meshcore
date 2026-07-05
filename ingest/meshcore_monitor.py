#!/usr/bin/env python3
"""
meshcore_monitor.py — ingest positions / RF from a MeshCore radio into jTAK.

The MeshCore counterpart to the Meshtastic `tactical_monitor.py`. It writes to
the SAME SQLite tables (`positions`, `rf_metrics`) with `source_type='meshcore'`,
so the existing dashboard, `/api/positions`, and map render MeshCore nodes with
zero downstream changes — this is the "swap the source, keep the app" seam.

STATUS (MVP-A):
  - write-path: COMPLETE and testable (see `python meshcore_monitor.py testpos ...`)
  - radio reader `run()`: STUB — pending a MeshCore radio + protocol confirmation
    (MeshCore companion is serial/BLE; wire it in once hardware is attached).
"""
import sqlite3
import sys
import time
from datetime import datetime, timezone

DB_PATH = "/opt/jtak/data/jtak.db"
SOURCE_TYPE = "meshcore"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_position(source_id: str, source_name: str, lat: float, lon: float,
                   alt: float | None = None, db_path: str = DB_PATH) -> str:
    """Insert one MeshCore position into the shared `positions` table.
    Mirrors what tactical_monitor does for Meshtastic, tagged source_type=meshcore."""
    ts = _utcnow()
    con = sqlite3.connect(db_path, timeout=10)
    try:
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


def run():
    """Main loop: read the MeshCore radio and feed write_position() / RF writes.

    TODO(radio): connect to the MeshCore companion radio (serial or BLE) once the
    hardware is attached and the frame format is confirmed, decode node
    positions + RF metrics, and call write_position(...) per packet. Until then
    the service idles so it can be installed and enabled ahead of the hardware.
    """
    print("[meshcore] monitor started — write-path ready; radio reader not yet wired", flush=True)
    while True:
        time.sleep(60)


if __name__ == "__main__":
    # Test injection (proves the ingest→DB→dashboard path without a radio):
    #   python meshcore_monitor.py testpos <source_id> <name> <lat> <lon> [alt]
    if len(sys.argv) >= 6 and sys.argv[1] == "testpos":
        alt = float(sys.argv[6]) if len(sys.argv) >= 7 else None
        ts = write_position(sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5]), alt)
        print(f"[meshcore] wrote test position for {sys.argv[2]!r} @ {ts}")
    else:
        run()
