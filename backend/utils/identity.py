"""
Hub identity — single source of truth at runtime.

Priority (highest → lowest):
  1. Live Meshtastic query  (longname, shortname, guid)
  2. jtak.identity.yaml     (guid, hub_id — permanent)
  3. jtak.yaml hub.*        (fallback values)
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

from utils.config import get

IDENTITY_PATH = Path("/opt/jtak/config/jtak.identity.yaml")
MESH_PYTHON   = "/opt/jtak/venv/bin/python3"

_identity: dict = {}

_MESH_READER = """
import meshtastic.tcp_interface, time, json, sys
try:
    iface = meshtastic.tcp_interface.TCPInterface('localhost', portNumber=4403)
    time.sleep(3)
    me = iface.getMyUser()
    print(json.dumps({
        'id':        me.get('id', '').lstrip('!'),
        'longName':  me.get('longName', ''),
        'shortName': me.get('shortName', ''),
    }))
    iface.close()
except Exception as e:
    print(json.dumps({'error': str(e)}), file=sys.stderr)
    sys.exit(1)
"""


def _query_meshtastic() -> dict | None:
    try:
        r = subprocess.run(
            [MESH_PYTHON, "-c", _MESH_READER],
            capture_output=True, text=True, timeout=12,
        )
        if r.returncode == 0:
            return json.loads(r.stdout.strip())
    except Exception:
        pass
    return None


def init_identity() -> dict:
    """Call once at service startup (blocking ~3-4s for Meshtastic query)."""
    global _identity

    # Load permanent identity file
    saved: dict = {}
    if IDENTITY_PATH.exists():
        saved = yaml.safe_load(IDENTITY_PATH.read_text()) or {}

    # Live Meshtastic query
    mesh = _query_meshtastic()

    guid      = (mesh or {}).get("id")        or saved.get("guid", "")
    longname  = (mesh or {}).get("longName")  or get("hub.name", "")
    shortname = (mesh or {}).get("shortName") or get("hub.short_name", get("hub.short_name", ""))
    hub_id    = saved.get("hub_id")           or get("hub.id", "")

    _identity = {
        "guid":      guid,
        "hub_id":    hub_id,
        "hub_name":  longname,
        "hub_short": shortname,
    }

    # Write identity file on first run (fresh install or new clone)
    if not IDENTITY_PATH.exists() and guid:
        IDENTITY_PATH.write_text(yaml.dump({
            "guid":        guid,
            "hub_id":      hub_id,
            "provisioned": datetime.now(timezone.utc).isoformat(),
        }))
        print(f"[identity] Created {IDENTITY_PATH}")

    src = "Meshtastic" if mesh else "config fallback"
    print(f"[identity] {longname} ({shortname}) guid={guid} hub_id={hub_id} [{src}]")
    return _identity


def get_identity() -> dict:
    return _identity
