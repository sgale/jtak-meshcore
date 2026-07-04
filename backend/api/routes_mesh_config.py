"""
routes_mesh_config.py — Batch LoRa config and node management for jTAK hubs.

Node inventory: merges all nodes seen on the mesh (from localhost meshtasticd)
with IP config from jtak.yaml peers + mesh_devices.

Connectivity tiers:
  self     — this hub (localhost)
  tcp      — IP configured + meshtasticd TCP reachable → full config support
  mesh     — heard on LoRa but no IP → show only, actions via mesh admin (Phase 2)
  offline  — IP configured but TCP unreachable

Endpoints (all require Bearer token):
  GET  /mesh-config/nodes
  GET  /mesh-config/node/{key}/lora
  POST /mesh-config/batch/lora        (method: "mesh"|"ble", register_admin_key: bool)
  POST /mesh-config/batch/identity
  POST /mesh-config/batch/action
  POST /mesh-config/ble/scan
  GET  /mesh-config/ble/cache
"""

import asyncio
import json
import logging
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from utils.config import get
from api.routes_auth import require_auth
from store.db import get_db

log = logging.getLogger("mesh_config")
router = APIRouter()

MESH_PORT = 4403
BLE_CACHE_PATH = "/opt/jtak/config/ble_cache.json"

_hub_pubkey_cache: Optional[bytes] = None

PRESET_NAMES = {
    0: "LONG_FAST",   1: "LONG_SLOW",    2: "VERY_LONG_SLOW",
    3: "MEDIUM_SLOW", 4: "MEDIUM_FAST",  5: "SHORT_SLOW",
    6: "SHORT_FAST",  7: "LONG_MODERATE",8: "SHORT_TURBO",
}

ROLE_NAMES = {
    0: "CLIENT", 1: "CLIENT_MUTE", 2: "ROUTER", 3: "ROUTER_CLIENT",
    4: "REPEATER", 5: "TRACKER", 6: "SENSOR", 7: "TAK",
    8: "CLIENT_HIDDEN", 9: "LOST_AND_FOUND", 10: "TAK_TRACKER",
}

def _role_str(raw) -> str:
    """Normalise role to string. API returns int or str depending on firmware/field presence."""
    if isinstance(raw, str):
        return raw
    return ROLE_NAMES.get(raw or 0, "CLIENT")

# Simple in-process mesh node cache (refreshed every 45s)
_mesh_cache: dict = {}
_mesh_cache_ts: float = 0.0
_MESH_CACHE_TTL = 45.0

# Prevent overlapping background discovery runs
_discovery_running: bool = False
# node_ids that were scanned and not found → don't retry for 5 min
_discovery_cooldown: dict = {}   # node_id → timestamp of last failed attempt
_DISCOVERY_COOLDOWN_SECS = 300


def isBleOnly_node(n: dict) -> bool:
    return bool(n.get("ble_address")) and not n.get("on_mesh") and not n.get("ip")


async def _bg_discover_ips(node_ids: list):
    global _discovery_running, _discovery_cooldown
    _discovery_running = True
    try:
        loop = asyncio.get_event_loop()
        discovered = await asyncio.wait_for(
            loop.run_in_executor(None, _mesh_discover_ips_blocking, node_ids),
            timeout=60,
        )
        now = time.monotonic()
        if discovered:
            for nid, entry in discovered.items():
                name = (_mesh_cache.get(nid) or {}).get("long_name") or nid
                _yaml_update_ip(nid, name, entry["ip"])
            _invalidate_mesh_cache()
        # Mark not-found nodes with a cooldown so we don't scan every refresh
        for nid in node_ids:
            if nid not in discovered:
                _discovery_cooldown[nid] = now
    except Exception as exc:
        log.warning("bg_discover_ips: error: %s", exc)
    finally:
        _discovery_running = False


# ── IP config helpers ─────────────────────────────────────────────────────────

def _build_ip_config() -> dict:
    """Returns {node_id: {key, name, ip, is_self}} from peers + mesh_devices."""
    result = {}
    hub_id = get("hub.id", "tak-2")

    peers = get("peers", {}) or {}
    for key, p in peers.items():
        nid = p.get("node_id")
        if not nid:
            continue
        is_self = key == hub_id or key.replace("-", "") == hub_id.replace("-", "")
        result[nid] = {
            "key":     hub_id if is_self else key,
            "name":    p.get("name", key),
            "ip":      "localhost" if is_self else (p.get("zt_ip") or ""),
            "is_self": is_self,
        }

    devices = get("mesh_devices", {}) or {}
    for key, d in devices.items():
        nid = d.get("node_id")
        if not nid:
            continue
        result[nid] = {
            "key":     key,
            "name":    d.get("name", key),
            "ip":      d.get("ip", ""),
            "is_self": False,
        }

    return result


def _arp_lookup(node_id: str) -> Optional[str]:
    """Find current IP for a node by matching node_id suffix against ARP table.

    Meshtastic node IDs are the last 4 bytes of the MAC address, so
    !22d6cda3 matches any ARP entry whose MAC ends in 22:d6:cd:a3.
    """
    nid = node_id.lstrip("!")
    if len(nid) != 8:
        return None
    # Build the 4-byte MAC suffix (e.g. "22:d6:cd:a3")
    suffix = ":".join(nid[i:i+2] for i in range(0, 8, 2)).lower()
    try:
        with open("/proc/net/arp") as f:
            next(f)  # skip header
            for line in f:
                parts = line.split()
                if len(parts) >= 4:
                    ip, mac = parts[0], parts[3].lower()
                    if mac.endswith(suffix):
                        return ip
    except Exception:
        pass
    return None


def _yaml_update_ip(node_id: str, name: str, ip: str):
    """Add or update a mesh_devices entry in jtak.yaml with a discovered IP."""
    import re as _re
    yaml_path = "/opt/jtak/config/jtak.yaml"
    try:
        with open(yaml_path) as f:
            content = f.read()

        # Build a safe yaml key from node_id (strip !)
        key = "node_" + node_id.lstrip("!")

        # If entry already exists anywhere (by node_id), update just the ip line
        if node_id in content:
            lines = content.splitlines(keepends=True)
            new_lines = []
            in_block = False
            for line in lines:
                stripped = line.lstrip()
                indent = len(line) - len(stripped)
                if indent == 4 and node_id in line:
                    in_block = True
                elif in_block and indent <= 2 and stripped and not stripped.startswith("#"):
                    in_block = False
                if in_block and _re.match(r'\s+ip\s*:', line):
                    line = _re.sub(r'(ip\s*:\s*).*', rf'\g<1>{ip}', line)
                new_lines.append(line)
            with open(yaml_path, "w") as f:
                f.writelines(new_lines)
            log.info("ARP: updated ip for %s → %s", node_id, ip)
            return

        # New entry — append to mesh_devices block
        lines = content.splitlines(keepends=True)
        new_lines = []
        in_mesh_devices = False
        inserted = False
        for i, line in enumerate(lines):
            new_lines.append(line)
            if _re.match(r'^mesh_devices\s*:', line):
                in_mesh_devices = True
            elif in_mesh_devices and not inserted:
                # Check if we've left the mesh_devices block
                stripped = line.lstrip()
                indent = len(line) - len(stripped)
                if indent == 0 and stripped and not stripped.startswith("#") and not _re.match(r'^\s', line):
                    # Insert new entry before this top-level key
                    new_lines.insert(-1,
                        f'  {key}:\n    name: "{name}"\n    node_id: "{node_id}"\n    ip: {ip}\n')
                    inserted = True
                    in_mesh_devices = False
        if not inserted and in_mesh_devices:
            new_lines.append(f'  {key}:\n    name: "{name}"\n    node_id: "{node_id}"\n    ip: {ip}\n')

        with open(yaml_path, "w") as f:
            f.writelines(new_lines)
        log.info("ARP: added new mesh_devices entry %s → %s (%s)", key, ip, node_id)
    except Exception as e:
        log.warning("yaml ip update failed for %s: %s", node_id, e)


def _mesh_admin_send(iface, dest_num: int, admin_msg, timeout: float = 30.0) -> bool:
    """Send an AdminMessage to a remote node via mesh and wait for ACK.

    Returns True on ACK, raises RuntimeError on NAK or timeout.
    Uses sendData directly so we can set onResponseAckPermitted=True.
    """
    import threading
    from meshtastic.protobuf import portnums_pb2 as _ports

    ack_event  = threading.Event()
    ack_result = [None]   # None=pending, True=ok, False=nak

    def on_ack(packet):
        try:
            routing = (packet.get("decoded") or {}).get("routing") or {}
            err     = routing.get("errorReason", "NONE")
            log.warning("mesh admin ACK from !%08x: errorReason=%s packet=%s", dest_num, err, packet.get("decoded",{}))
            ack_result[0] = (err == "NONE" or err == 0)
        except Exception as exc:
            log.warning("mesh admin ACK parse error: %s", exc)
            ack_result[0] = False
        ack_event.set()

    # Inject session passkey if node has one (firmware 2.5+ session auth)
    node_data = iface._getOrCreateByNum(dest_num) or {}
    if node_data.get("adminSessionPassKey"):
        admin_msg.session_passkey = node_data["adminSessionPassKey"]

    # PKI-encrypted admin — requires hub's own public key in its admin_key list.
    # channelIndex is irrelevant for PKI sends; use 0 to avoid NO_CHANNEL from
    # an invalid admin channel index.
    iface.sendData(
        admin_msg,
        dest_num,
        portNum=_ports.PortNum.ADMIN_APP,
        wantAck=True,
        wantResponse=False,
        onResponse=on_ack,
        onResponseAckPermitted=True,
        channelIndex=0,
        pkiEncrypted=True,
    )

    ack_event.wait(timeout=timeout)
    if ack_result[0] is None:
        raise RuntimeError(f"mesh admin: no ACK from !{dest_num:08x} within {timeout}s")
    if not ack_result[0]:
        raise RuntimeError(f"mesh admin: NAK from !{dest_num:08x}")
    return True


def _yaml_clear_ip(node_id: str):
    """Remove the ip field from a mesh_devices entry in jtak.yaml."""
    import re as _re
    yaml_path = "/opt/jtak/config/jtak.yaml"
    try:
        with open(yaml_path) as f:
            content = f.read()
        if node_id not in content:
            return
        lines = content.splitlines(keepends=True)
        new_lines = []
        in_block = False
        for line in lines:
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            if indent == 4 and node_id in line:
                in_block = True
            elif in_block and indent <= 2 and stripped and not stripped.startswith("#"):
                in_block = False
            # Drop the ip line inside the block
            if in_block and _re.match(r'\s+ip\s*:', line):
                continue
            new_lines.append(line)
        with open(yaml_path, "w") as f:
            f.writelines(new_lines)
    except Exception as e:
        log.warning("yaml_clear_ip failed for %s: %s", node_id, e)


def _key_to_ip(key: str) -> Optional[str]:
    """Resolve a node key to its TCP IP, or None."""
    ip_cfg = _build_ip_config()
    for nid, cfg in ip_cfg.items():
        if cfg["key"] == key or nid == key:
            return cfg["ip"] or None
    return None


def _key_to_node_id(key: str) -> Optional[str]:
    """Resolve a node key to its meshtastic node_id (!xxxxxxxx), or None."""
    ip_cfg = _build_ip_config()
    for nid, cfg in ip_cfg.items():
        if cfg["key"] == key:
            return nid
    # For mesh-only nodes the key IS the node_id — strip any stray backslash escaping
    clean = key.lstrip("\\")
    if clean.startswith("!"):
        return clean
    return None


def _get_node_name(key: str) -> str:
    """Best-effort display name for a key."""
    ip_cfg = _build_ip_config()
    for nid, cfg in ip_cfg.items():
        if cfg["key"] == key or nid == key:
            return cfg["name"]
    if key in _mesh_cache:
        return _mesh_cache[key].get("long_name", key)
    # BLE cache fallback (for BLE-only nodes not seen on mesh)
    entry = _ble_cache_load().get(key)
    if isinstance(entry, dict):
        return entry.get("long_name") or entry.get("ble_name") or key
    return key


_MESH_STALE_SECS = 600  # matches frontend MESH_OFFLINE_SECS

def _is_mesh_stale(node_id: str) -> bool:
    """True if node hasn't been heard on mesh within the stale threshold."""
    mn = _mesh_cache.get(node_id, {})
    last = mn.get("last_heard")
    if not last:
        return True
    return (time.time() - last) > _MESH_STALE_SECS


def _tcp_reachable(ip: str, port: int = MESH_PORT, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── Meshtastic node list from localhost ───────────────────────────────────────

def _fetch_mesh_nodes_blocking() -> dict:
    """Connect to localhost meshtasticd, return {node_id: {...}} for all known nodes."""
    global _mesh_cache, _mesh_cache_ts
    now = time.monotonic()
    if _mesh_cache and (now - _mesh_cache_ts) < _MESH_CACHE_TTL:
        return _mesh_cache

    import meshtastic.tcp_interface
    iface = meshtastic.tcp_interface.TCPInterface(hostname="localhost")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if iface.nodes and len(iface.nodes) > 0:
            break
        time.sleep(0.3)
    try:
        nodes = {}
        for node_id, n in (iface.nodes or {}).items():
            user = n.get("user", {})
            dm   = n.get("deviceMetrics", {})
            nodes[node_id] = {
                "node_id":     node_id,
                "long_name":   user.get("longName",  node_id),
                "short_name":  user.get("shortName", ""),
                "role":        _role_str(user.get("role")),
                "last_heard":  n.get("lastHeard"),
                "snr":         n.get("snr"),
                "battery_level": dm.get("batteryLevel"),
                "voltage":       dm.get("voltage"),
                "channel_util":  dm.get("channelUtilization"),
                "air_util_tx":   dm.get("airUtilTx"),
                "uptime_secs":   dm.get("uptimeSeconds"),
            }
        _mesh_cache    = nodes
        _mesh_cache_ts = now
        return nodes
    finally:
        try:
            iface.close()
        except Exception:
            pass


# ── Meshtastic config operations (blocking) ───────────────────────────────────

def _connect(ip: str):
    import meshtastic.tcp_interface
    iface = meshtastic.tcp_interface.TCPInterface(hostname=ip)
    # Wait for node database to sync from meshtasticd.
    # localhost connections take ~4-5s to populate iface.nodes.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if iface.nodes and len(iface.nodes) > 0:
            break
        time.sleep(0.3)
    else:
        time.sleep(2)   # fallback — proceed even if empty
    return iface


def _read_lora(ip: str) -> dict:
    iface = _connect(ip)
    try:
        lora  = iface.localNode.localConfig.lora
        power = iface.localNode.localConfig.power
        me    = iface.getMyNodeInfo() or {}
        user  = me.get("user", {})
        dm    = me.get("deviceMetrics", {})

        # batteryLevel 101 = externally powered / charging
        batt_raw = dm.get("batteryLevel", None)
        batt_pct = None if batt_raw is None else ("POWERED" if batt_raw == 101 else batt_raw)
        voltage  = dm.get("voltage", None)
        uptime_s = dm.get("uptimeSeconds", None)

        uptime_str = None
        if uptime_s is not None:
            h, rem = divmod(int(uptime_s), 3600)
            m, s   = divmod(rem, 60)
            uptime_str = f"{h}h {m}m {s}s"

        # Channels
        ch_list = []
        try:
            for ch in (iface.localNode.channels or []):
                role = getattr(ch, "role", 0)
                if role == 0:
                    continue  # DISABLED
                settings = getattr(ch, "settings", None) or ch
                name = getattr(settings, "name", "") or ""
                psk  = getattr(settings, "psk", b"") or b""
                # PSK: empty/b'\x01' = default AQ==, otherwise show truncated hex
                if not psk or psk == b"\x01":
                    psk_str = "(default)"
                else:
                    psk_str = psk.hex()[:12] + "…"
                ch_list.append({
                    "index":    getattr(ch, "index", 0),
                    "name":     name if name else "(primary)" if role == 1 else f"ch{getattr(ch,'index',0)}",
                    "role":     "PRIMARY" if role == 1 else "SECONDARY",
                    "psk":      psk_str,
                    "uplink":   bool(getattr(settings, "uplink_enabled",   False)),
                    "downlink": bool(getattr(settings, "downlink_enabled", False)),
                })
        except Exception as e:
            ch_list = [{"error": str(e)}]

        return {
            # Identity
            "long_name":              user.get("longName", ""),
            "short_name":             user.get("shortName", ""),
            "role":                   _role_str(user.get("role")),
            # Channels
            "channels":               ch_list,
            # LoRa
            "use_preset":             bool(getattr(lora, "use_preset", True)),
            "modem_preset":           getattr(lora, "modem_preset", 0),
            "modem_preset_name":      PRESET_NAMES.get(getattr(lora, "modem_preset", 0), "UNKNOWN"),
            "spread_factor":          getattr(lora, "spread_factor", 0),
            "bandwidth":              getattr(lora, "bandwidth", 0),
            "coding_rate":            getattr(lora, "coding_rate", 0),
            "tx_power":               getattr(lora, "tx_power", 0),
            "hop_limit":              getattr(lora, "hop_limit", 3),
            "sx126x_rx_boosted_gain": bool(getattr(lora, "sx126x_rx_boosted_gain", False)),
            "channel_num":            getattr(lora, "channel_num", 0),
            "override_duty_cycle":    bool(getattr(lora, "override_duty_cycle", False)),
            # Power / battery
            "battery_pct":            batt_pct,
            "voltage_v":              round(voltage, 2) if voltage else None,
            "uptime":                 uptime_str,
            "uptime_secs":            uptime_s,
            "channel_util_pct":       round(dm.get("channelUtilization", 0), 1),
            "air_util_tx_pct":        round(dm.get("airUtilTx", 0), 2),
            # Power config
            "pwr_is_power_saving":    bool(getattr(power, "is_power_saving", False)),
            "pwr_screen_on_secs":     getattr(power, "screen_on_secs", 0),
            "pwr_wait_bt_secs":       getattr(power, "wait_bluetooth_secs", 0),
            "pwr_mesh_sds_timeout":   getattr(power, "mesh_sds_timeout_secs", 0),
            "pwr_min_wake_secs":      getattr(power, "min_wake_secs", 0),
        }
    finally:
        try:
            iface.close()
        except Exception:
            pass


def _read_lora_ble(address: str) -> dict:
    """Read live LoRa config + device metrics from a BLE-only node."""
    from meshtastic.ble_interface import BLEInterface as _BLEI
    iface = _BLEI(address)
    try:
        lora  = iface.localNode.localConfig.lora
        power = iface.localNode.localConfig.power
        me    = iface.getMyNodeInfo() or {}
        user  = me.get("user", {})
        dm    = me.get("deviceMetrics", {})

        batt_raw = dm.get("batteryLevel", None)
        batt_pct = None if batt_raw is None else ("POWERED" if batt_raw == 101 else batt_raw)
        voltage  = dm.get("voltage", None)
        uptime_s = dm.get("uptimeSeconds", None)
        uptime_str = None
        if uptime_s is not None:
            h, rem = divmod(int(uptime_s), 3600)
            m, s   = divmod(rem, 60)
            uptime_str = f"{h}h {m}m {s}s"

        ch_list = []
        try:
            for ch in (iface.localNode.channels or []):
                role = getattr(ch, "role", 0)
                if role == 0:
                    continue
                settings = getattr(ch, "settings", None) or ch
                name = getattr(settings, "name", "") or ""
                psk  = getattr(settings, "psk", b"") or b""
                if not psk or psk == b"\x01":
                    psk_str = "(default)"
                else:
                    psk_str = psk.hex()[:12] + "…"
                ch_list.append({
                    "index":    getattr(ch, "index", 0),
                    "name":     name if name else "(primary)" if role == 1 else f"ch{getattr(ch,'index',0)}",
                    "role":     "PRIMARY" if role == 1 else "SECONDARY",
                    "psk":      psk_str,
                    "uplink":   bool(getattr(settings, "uplink_enabled",   False)),
                    "downlink": bool(getattr(settings, "downlink_enabled", False)),
                })
        except Exception as e:
            ch_list = [{"error": str(e)}]

        return {
            "long_name":              user.get("longName", ""),
            "short_name":             user.get("shortName", ""),
            "role":                   _role_str(user.get("role")),
            "channels":               ch_list,
            "use_preset":             bool(getattr(lora, "use_preset", True)),
            "modem_preset":           getattr(lora, "modem_preset", 0),
            "modem_preset_name":      PRESET_NAMES.get(getattr(lora, "modem_preset", 0), "UNKNOWN"),
            "spread_factor":          getattr(lora, "spread_factor", 0),
            "bandwidth":              getattr(lora, "bandwidth", 0),
            "coding_rate":            getattr(lora, "coding_rate", 0),
            "tx_power":               getattr(lora, "tx_power", 0),
            "hop_limit":              getattr(lora, "hop_limit", 3),
            "sx126x_rx_boosted_gain": bool(getattr(lora, "sx126x_rx_boosted_gain", False)),
            "channel_num":            getattr(lora, "channel_num", 0),
            "override_duty_cycle":    bool(getattr(lora, "override_duty_cycle", False)),
            "battery_pct":            batt_pct,
            "voltage_v":              round(voltage, 2) if voltage else None,
            "uptime":                 uptime_str,
            "uptime_secs":            uptime_s,
            "channel_util_pct":       round(dm.get("channelUtilization", 0), 1),
            "air_util_tx_pct":        round(dm.get("airUtilTx", 0), 2),
            "pwr_is_power_saving":    bool(getattr(power, "is_power_saving", False)),
            "pwr_screen_on_secs":     getattr(power, "screen_on_secs", 0),
            "pwr_wait_bt_secs":       getattr(power, "wait_bluetooth_secs", 0),
            "pwr_mesh_sds_timeout":   getattr(power, "mesh_sds_timeout_secs", 0),
            "pwr_min_wake_secs":      getattr(power, "min_wake_secs", 0),
        }
    finally:
        try:
            iface.close()
        except Exception:
            pass


def _apply_lora(ip: str, cfg: dict) -> str:
    iface = _connect(ip)
    try:
        lora = iface.localNode.localConfig.lora
        for field in ("use_preset", "modem_preset", "spread_factor", "bandwidth",
                      "coding_rate", "tx_power", "hop_limit",
                      "sx126x_rx_boosted_gain", "override_duty_cycle"):
            if field in cfg:
                setattr(lora, field, cfg[field])
        iface.localNode.writeConfig("lora")
        _invalidate_mesh_cache()
        return "ok"
    finally:
        try:
            iface.close()
        except Exception:
            pass


def _apply_identity(ip: str, long_name: Optional[str], short_name: Optional[str]) -> str:
    iface = _connect(ip)
    try:
        me   = iface.getMyNodeInfo() or {}
        user = me.get("user", {})
        ln   = long_name  if long_name  is not None else user.get("longName",  "")
        sn   = short_name if short_name is not None else user.get("shortName", "")
        iface.localNode.setOwner(long_name=ln, short_name=sn)
        return "ok"
    finally:
        try:
            iface.close()
        except Exception:
            pass


def _do_action(ip: str, action: str) -> str:
    iface = _connect(ip)
    try:
        if action == "reboot":
            iface.localNode.reboot(secs=3)
            return "rebooting in 3s"
        elif action == "nodeinfo":
            me   = iface.getMyNodeInfo() or {}
            user = me.get("user", {})
            iface.localNode.setOwner(
                long_name=user.get("longName", ""),
                short_name=user.get("shortName", ""),
            )
            return "nodeinfo broadcast sent"
        elif action == "dbreset":
            try:
                iface.localNode.resetNodeDb()
                return "node db reset sent"
            except Exception as e:
                return f"dbreset error: {e}"
        elif action == "factory_reset":
            iface.localNode.factoryReset()
            return "factory reset sent"
        else:
            return f"unknown action: {action}"
    finally:
        try:
            iface.close()
        except Exception:
            pass


# ── Mesh admin (single localhost connection, multiple remote nodes) ────────────

def _mesh_apply_lora_batch(items: list[dict], cfg: dict) -> list[dict]:
    """Connect once to localhost, push LoRa config to multiple mesh-only nodes.

    Uses PKC admin (pkiEncrypted=True). Requires TAK-2's public key to be
    registered in the target node's Security Config > Admin Key slots.
    TAK-2 public key: A/ygULR/DOhvS+AfPM/3o8Se1tkxbclzAwhWDr0X2SU=
    """
    from meshtastic.protobuf import admin_pb2 as _admin_pb2
    iface = _connect("localhost")
    results = []
    try:
        for item in items:
            node_id = item["node_id"]
            name    = item["name"]
            try:
                dest_num = int(node_id.lstrip("!"), 16)
                msg  = _admin_pb2.AdminMessage()
                lora = msg.set_config.lora
                bool_fields = {"use_preset", "sx126x_rx_boosted_gain", "override_duty_cycle"}
                for field, val in cfg.items():
                    if hasattr(lora, field):
                        setattr(lora, field, bool(val) if field in bool_fields else int(val))
                _mesh_admin_send(iface, dest_num, msg)
                _invalidate_mesh_cache()
                results.append({"key": item["key"], "name": name, "ok": True,
                                 "message": "sent via mesh"})
                log.info("mesh lora → %s (%s)", name, node_id)
            except Exception as e:
                results.append({"key": item["key"], "name": name, "ok": False,
                                 "message": str(e)})
                log.warning("mesh lora failed %s: %s", name, e)
    finally:
        try:
            iface.close()
        except Exception:
            pass
    return results


def _mesh_identity_batch(items: list[dict], long_name: Optional[str],
                          short_name: Optional[str]) -> list[dict]:
    """Push owner name via mesh admin to multiple nodes."""
    from meshtastic.protobuf import admin_pb2 as _admin_pb2
    iface = _connect("localhost")
    results = []
    try:
        for item in items:
            node_id = item["node_id"]
            name    = item["name"]
            try:
                ln = long_name  or name
                sn = short_name or ""
                dest_num = int(node_id.lstrip("!"), 16)
                msg = _admin_pb2.AdminMessage()
                msg.set_owner.long_name  = ln
                msg.set_owner.short_name = sn
                _mesh_admin_send(iface, dest_num, msg)
                results.append({"key": item["key"], "name": name, "ok": True,
                                 "message": "sent via mesh"})
            except Exception as e:
                results.append({"key": item["key"], "name": name, "ok": False,
                                 "message": str(e)})
    finally:
        try:
            iface.close()
        except Exception:
            pass
    return results


def _mesh_action_batch(items: list[dict], action: str) -> list[dict]:
    """Send an action to multiple mesh-only nodes via a single localhost connection."""
    from meshtastic.protobuf import admin_pb2 as _admin_pb2, portnums_pb2 as _portnums_pb2
    iface = _connect("localhost")
    results = []
    try:
        for item in items:
            node_id = item["node_id"]
            name    = item["name"]
            try:
                remote = iface.getNode(node_id, requestChannels=False)
                time.sleep(0.5)
                if action == "reboot":
                    remote.reboot(secs=3)
                    msg = "reboot sent via mesh"
                elif action == "nodeinfo":
                    iface.sendData(b"", destinationId=node_id,
                                   portNum=_portnums_pb2.PortNum.NODEINFO_APP,
                                   wantResponse=True)
                    msg = "nodeinfo request sent via mesh"
                elif action == "dbreset":
                    remote.resetNodeDb()
                    msg = "db reset sent via mesh"
                elif action == "factory_reset":
                    remote.factoryReset()
                    msg = "factory reset sent via mesh"
                else:
                    msg = f"unknown action: {action}"
                results.append({"key": item["key"], "name": name, "ok": True, "message": msg})
                log.info("mesh action=%s → %s (%s)", action, name, node_id)
            except Exception as e:
                results.append({"key": item["key"], "name": name, "ok": False,
                                 "message": str(e)})
                log.warning("mesh action=%s failed %s: %s", action, name, e)
    finally:
        try:
            iface.close()
        except Exception:
            pass
    return results


# ── BLE helpers ───────────────────────────────────────────────────────────────

def _ble_cache_load() -> dict:
    try:
        with open(BLE_CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _ble_cache_save(cache: dict):
    try:
        with open(BLE_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        log.warning("BLE cache save failed: %s", e)


def _get_hub_pubkey() -> bytes:
    global _hub_pubkey_cache
    if _hub_pubkey_cache is None:
        iface = _connect("localhost")
        try:
            _hub_pubkey_cache = bytes(iface.localNode.localConfig.security.public_key)
        finally:
            try: iface.close()
            except: pass
    return _hub_pubkey_cache


def _ble_scan_blocking() -> dict:
    """One BLE scan → match devices to node IDs → update cache."""
    import asyncio as _asyncio
    from bleak import BleakScanner as _BS
    loop = _asyncio.new_event_loop()
    try:
        raw_devs = loop.run_until_complete(_BS.discover(timeout=10.0))
    finally:
        loop.close()
    # discover() returns a list of BLEDevice objects

    cache = _ble_cache_load()
    now_ts = int(time.time())
    try:
        mesh_nodes = _fetch_mesh_nodes_blocking()
    except Exception:
        mesh_nodes = {}

    cached_by_addr = {v["address"].upper(): k for k, v in cache.items()
                      if isinstance(v, dict) and v.get("address")}
    found = []
    unmatched = []
    for dev in raw_devs:
        addr = dev.address.upper()
        name = dev.name or ""
        node_id = cached_by_addr.get(addr)
        if not node_id and "_" in name:
            suffix = name.split("_")[-1].lower()
            for nid in mesh_nodes:
                if nid.lower().endswith(suffix):
                    node_id = nid
                    break
        if not node_id:
            # Fallback: match last 4 bytes of MAC against node ID
            # e.g. MAC E6:1C:B0:19:45:07 → "b0194507" matches node !b0194507
            mac_suffix = addr.replace(":", "")[-8:].lower()
            for nid in mesh_nodes:
                if nid.lstrip("!").lower() == mac_suffix:
                    node_id = nid
                    break
        if node_id:
            long_name = (mesh_nodes.get(node_id, {}).get("long_name")
                         or cache.get(node_id, {}).get("long_name")
                         or name or addr)
            cache[node_id] = {"address": addr, "ble_name": name,
                               "long_name": long_name, "last_seen": now_ts}
            found.append({"node_id": node_id, "ble_address": addr,
                           "ble_name": name, "long_name": long_name})
        else:
            unmatched.append({"ble_address": addr, "ble_name": name})
    _ble_cache_save(cache)
    return {"found": found, "unmatched": unmatched, "cache": cache}


_BLE_TORADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"


def _yaml_update_name(key: str, node_id: Optional[str], long_name: str):
    """Update the name field in jtak.yaml for a node if it has a yaml entry."""
    if not long_name:
        return
    try:
        yaml_path = "/opt/jtak/config/jtak.yaml"
        with open(yaml_path) as f:
            lines = f.readlines()

        # Find the entry block for this node and update its name line in-place.
        # Locates the entry by node_id match, then replaces the nearest name: line.
        ip_cfg = _build_ip_config()
        target_entry_key = None
        for nid, cfg in ip_cfg.items():
            if cfg["key"] == key or nid == key or (node_id and nid == node_id):
                # Find which yaml key maps to this entry
                target_entry_key = cfg["key"]
                break
        if not target_entry_key:
            return

        import re as _re
        in_block = False
        new_lines = []
        for line in lines:
            # Detect entry block start (e.g. "  sky_router:" or "  tak-1:")
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            if indent == 2 and stripped.rstrip().rstrip(":") == target_entry_key:
                in_block = True
            elif in_block and indent <= 2 and stripped and not stripped.startswith("#"):
                in_block = False  # left the block

            if in_block and _re.match(r'\s+name\s*:', line):
                q = '"' if '"' in line else "'"
                line = _re.sub(r'(name\s*:\s*)["\']?.*["\']?', f'name: {q}{long_name}{q}', line)

            new_lines.append(line)

        with open(yaml_path, "w") as f:
            f.writelines(new_lines)
        log.info("yaml name updated: %s → %r", target_entry_key, long_name)
    except Exception as e:
        log.warning("yaml name update failed for %s: %s", key, e)


def _invalidate_mesh_cache():
    """Force next mesh node fetch to re-query meshtasticd."""
    global _mesh_cache_ts
    _mesh_cache_ts = 0.0


def _ble_lora_read_existing(address: str) -> dict:
    """Read current lora config via BLEInterface (read-only, no write).

    Returns dict of all lora fields including region.
    Called in a thread before the raw GATT write in _connect_and_write.
    """
    from meshtastic.ble_interface import BLEInterface as _BLEI
    iface = _BLEI(address)
    try:
        lora = iface.localNode.localConfig.lora
        return {f: getattr(lora, f, None) for f in (
            "use_preset", "modem_preset", "spread_factor", "bandwidth",
            "coding_rate", "tx_power", "hop_limit", "region",
            "sx126x_rx_boosted_gain", "override_duty_cycle", "channel_num"
        )}
    finally:
        try:
            iface.close()
        except Exception:
            pass


def _make_to_radio_bytes(admin_msg) -> bytes:
    from meshtastic.protobuf import mesh_pb2 as _mesh, portnums_pb2 as _ports
    pkt = _mesh.MeshPacket()
    pkt.decoded.portnum       = _ports.PortNum.ADMIN_APP
    pkt.decoded.payload       = admin_msg.SerializeToString()
    pkt.decoded.want_response = False
    pkt.to = 0xFFFFFFFF
    pkt.id = int(time.time() * 1000) & 0xFFFFFFFF
    tr = _mesh.ToRadio()
    tr.packet.CopyFrom(pkt)
    return tr.SerializeToString()


def _ble_apply_batch(items: list, cfg: dict, register_admin_key: bool) -> list:
    """One scan → sleep → connect+write each device sequentially.

    Single asyncio event loop for the whole batch: scan once, release the
    BlueZ lock with a 3s sleep, then connect to each device in turn.
    """
    import asyncio as _asyncio
    from bleak import BleakClient as _BC, BleakScanner as _BS
    from meshtastic.protobuf import admin_pb2 as _admin

    # Build address→item map for quick lookup
    addr_map = {}
    no_addr = []
    for item in items:
        addr = (item.get("ble_address") or "").upper()
        if addr:
            addr_map[addr] = item
        else:
            no_addr.append({"key": item["key"], "name": item["name"],
                            "ok": False,
                            "message": "no BLE address cached — run BLE Scan first"})

    _bool_fields = {"use_preset", "sx126x_rx_boosted_gain", "override_duty_cycle"}

    def _build_lora_admin(existing: dict) -> "_admin.AdminMessage":
        """Overlay cfg onto existing lora fields and return AdminMessage."""
        merged = dict(existing)
        for field, val in cfg.items():
            if field in ("node_keys", "method", "register_admin_key"):
                continue
            if field in _bool_fields:
                merged[field] = bool(val)
            else:
                try:
                    merged[field] = int(val)
                except (TypeError, ValueError):
                    pass
        admin = _admin.AdminMessage()
        lora_proto = admin.set_config.lora
        for field, val in merged.items():
            if val is None:
                continue
            try:
                setattr(lora_proto, field,
                        bool(val) if field in _bool_fields else int(val))
            except Exception:
                pass
        return admin

    async def _connect_and_write(device, item, existing_lora: dict) -> dict:
        """Connect to one device and write admin packets. Returns result dict."""
        name = item["name"]
        loop = _asyncio.get_event_loop()

        async def _do_write() -> list:
            msgs = []
            async with _BC(device, timeout=60.0) as client:
                if cfg:
                    lora_admin = _build_lora_admin(existing_lora)
                    await client.write_gatt_char(
                        _BLE_TORADIO_UUID, _make_to_radio_bytes(lora_admin), response=True)
                    applied = ", ".join(k for k in cfg
                                        if k not in ("node_keys","method","register_admin_key"))
                    msgs.append(f"lora ok ({applied})")
                if register_admin_key:
                    key_bytes = _get_hub_pubkey()
                    admin = _admin.AdminMessage()
                    admin.set_config.security.admin_key.append(key_bytes)
                    await client.write_gatt_char(
                        _BLE_TORADIO_UUID, _make_to_radio_bytes(admin), response=True)
                    msgs.append("admin key sent")
            return msgs

        try:
            msgs = await _do_write()
            if cfg:
                _invalidate_mesh_cache()
            msg = " · ".join(msgs) if msgs else "ok (nothing to do)"
            log.info("BLE apply → %s: %s", name, msg)
            return {"key": item["key"], "name": name, "ok": True,
                    "message": (msg + " — device rebooting") if cfg else msg}

        except Exception as e:
            err_str = str(e)
            # ATT 0x0e = radio just released a previous BLE session (e.g. phone disconnected).
            # Wait 8s and retry once.
            if "0x0e" in err_str or "Unlikely" in err_str:
                log.info("BLE ATT 0x0e on %s — waiting 8s and retrying", name)
                await _asyncio.sleep(8)
                try:
                    msgs = await _do_write()
                    if cfg:
                        _invalidate_mesh_cache()
                    msg = " · ".join(msgs) if msgs else "ok"
                    log.info("BLE retry succeeded → %s: %s", name, msg)
                    return {"key": item["key"], "name": name, "ok": True,
                            "message": (msg + " — device rebooting") if cfg else msg}
                except Exception as e2:
                    err_str = str(e2)
            if isinstance(e, TimeoutError) or "TimeoutError" in type(e).__name__:
                err_str = "unreachable — is a phone connected to this radio?"
            log.warning("BLE apply failed %s: %r", name, e)
            return {"key": item["key"], "name": name, "ok": False, "message": err_str}

    async def _run():
        results = list(no_addr)
        if not addr_map:
            return results

        loop = _asyncio.get_event_loop()
        for target_addr, item in addr_map.items():
            # Step 1: if lora cfg — pre-read current config via BLEInterface FIRST,
            # before BleakScanner runs. Two sequential scans conflict in BlueZ.
            existing_lora = {}
            if cfg:
                try:
                    existing_lora = await _asyncio.wait_for(
                        loop.run_in_executor(None, _ble_lora_read_existing, item["ble_address"]),
                        timeout=35)
                    log.info("BLE pre-read lora for %s: region=%s", item["name"],
                             existing_lora.get("region"))
                except Exception as pre_err:
                    log.warning("BLE pre-read failed for %s (%s) — region may be lost",
                                item["name"], pre_err)
                await _asyncio.sleep(3)  # release BlueZ before next scan

            # Step 2: BleakScanner scan to get the device object for BleakClient
            raw = await _BS.discover(timeout=10.0, return_adv=True)
            device = None
            for addr, (dev, _adv) in raw.items():
                if addr.upper() == target_addr:
                    device = dev
                    break

            if device is None:
                results.append({"key": item["key"], "name": item["name"],
                                 "ok": False, "message": "not found — out of BLE range?"})
                continue

            await _asyncio.sleep(3)   # release BlueZ scan lock before connect
            results.append(await _connect_and_write(device, item, existing_lora))
            await _asyncio.sleep(2)   # brief pause before next device

        return results

    return _asyncio.run(_run())


def _ble_addr_for(key: str, ble_cache: dict) -> Optional[str]:
    """Return cached BLE MAC for a node key/node_id, or None."""
    nid   = _key_to_node_id(key) or key
    entry = ble_cache.get(nid) or ble_cache.get(key)
    if isinstance(entry, dict):
        return entry.get("address") or None
    return None


# ── IP Discovery ──────────────────────────────────────────────────────────────

def _get_local_scan_subnet() -> Optional[str]:
    """Return the local LAN subnet (CIDR) on the non-ZT, non-hotspot interface."""
    import ipaddress, psutil, socket as _sock
    for iface, addrs in psutil.net_if_addrs().items():
        if iface.startswith("zt") or iface == "lo" or iface.startswith("docker"):
            continue
        for a in addrs:
            if a.family != _sock.AF_INET:
                continue
            if a.address.startswith("10.42.") or a.address.startswith("127."):
                continue  # skip hotspot / loopback
            if a.netmask:
                net = ipaddress.IPv4Network(f"{a.address}/{a.netmask}", strict=False)
                if net.prefixlen >= 24:  # only scan /24 or tighter
                    return str(net)
    return None


def _scan_subnet_for_meshtastic(subnet: str, timeout: float = 0.4) -> dict:
    """Port-scan subnet for port 4403, return {node_id: ip} for responding hosts."""
    import ipaddress, concurrent.futures
    import meshtastic.tcp_interface

    net = ipaddress.IPv4Network(subnet, strict=False)
    hosts = [str(h) for h in net.hosts()]

    # Phase 1: fast TCP SYN probe for port 4403
    open_ips = []
    def probe(ip):
        try:
            with socket.create_connection((ip, MESH_PORT), timeout=timeout):
                return ip
        except OSError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
        for ip in ex.map(probe, hosts):
            if ip:
                open_ips.append(ip)

    if not open_ips:
        return {}

    # Phase 2: connect and read node_id from each open host
    results = {}
    def identify(ip):
        try:
            iface = meshtastic.tcp_interface.TCPInterface(hostname=ip, noProto=False)
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if iface.myInfo:
                    break
                time.sleep(0.3)
            nid = None
            if iface.myInfo:
                num = iface.myInfo.my_node_num
                nid = f"!{num:08x}"
            try: iface.close()
            except Exception: pass
            return (ip, nid)
        except Exception:
            return (ip, None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        for ip, nid in ex.map(identify, open_ips):
            if nid:
                results[nid] = ip

    return results


def _mesh_discover_ips_blocking(node_ids: list) -> dict:
    """Discover IPs for mesh-only nodes.

    Strategy (in order):
      1. ARP lookup — instant, works if node has sent gateway traffic
      2. Port scan  — scan local /24 for port 4403, identify by reading node_id

    Returns {node_id: {"ip": ip, "method": method}}.
    """
    results: dict = {}

    # ── 1. ARP pass ──────────────────────────────────────────────────────────
    for nid in node_ids:
        ip = _arp_lookup(nid)
        if ip:
            log.info("discover: ARP hit %s → %s", nid, ip)
            results[nid] = {"ip": ip, "method": "arp"}

    remaining = [nid for nid in node_ids if nid not in results]
    if not remaining:
        return results

    # ── 2. Port scan local subnet ────────────────────────────────────────────
    subnet = _get_local_scan_subnet()
    if not subnet:
        log.warning("discover: no local subnet found for port scan")
        return results

    log.info("discover: port scan %s for %d remaining nodes", subnet, len(remaining))
    scan_map = _scan_subnet_for_meshtastic(subnet)
    log.info("discover: port scan found %d meshtastic hosts: %s", len(scan_map), scan_map)

    for nid in remaining:
        if nid in scan_map:
            results[nid] = {"ip": scan_map[nid], "method": "scan"}
            log.info("discover: scan hit %s → %s", nid, scan_map[nid])

    return results


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/mesh-config/discover-ips", dependencies=[Depends(require_auth)])
async def discover_ips():
    """Discover IPs for mesh-only nodes via mesh admin query, ARP, and port scan fallback."""
    loop = asyncio.get_event_loop()

    # Get current node list to find mesh-only nodes without IPs
    try:
        mesh_nodes = await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_mesh_nodes_blocking), timeout=15)
    except Exception as e:
        raise HTTPException(503, f"Could not fetch mesh nodes: {e}")

    ip_cfg = _build_ip_config()
    # Target: on mesh, not self, no confirmed-reachable IP
    targets = []
    for nid in mesh_nodes:
        cfg = ip_cfg.get(nid, {})
        if cfg.get("is_self"):
            continue
        if not cfg.get("ip"):
            targets.append(nid)

    if not targets:
        return {"discovered": {}, "message": "No mesh-only nodes need IP discovery"}

    log.info("discover-ips: targeting %d nodes: %s", len(targets), targets)
    discovered = await asyncio.wait_for(
        loop.run_in_executor(None, _mesh_discover_ips_blocking, targets),
        timeout=60,
    )

    # Persist discovered IPs to jtak.yaml and invalidate mesh cache
    for nid, entry in discovered.items():
        ip   = entry["ip"]
        name = (mesh_nodes.get(nid) or {}).get("long_name") or nid
        _yaml_update_ip(nid, name, ip)
    if discovered:
        _invalidate_mesh_cache()

    return {
        "discovered": discovered,
        "count": len(discovered),
        "targets": len(targets),
    }


@router.get("/mesh-config/nodes", dependencies=[Depends(require_auth)])
async def list_nodes():
    loop = asyncio.get_event_loop()

    # Fetch mesh node list (cached) + IP config
    try:
        mesh_nodes = await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_mesh_nodes_blocking), timeout=15
        )
    except Exception as e:
        log.warning("mesh node fetch failed: %s", e)
        mesh_nodes = {}

    ip_cfg = _build_ip_config()  # {node_id: {key, name, ip, is_self}}

    # Build merged list
    result = []
    seen_keys: set = set()

    for node_id, mn in mesh_nodes.items():
        cfg     = ip_cfg.get(node_id, {})
        key     = cfg.get("key") or node_id
        ip      = cfg.get("ip") or ""
        is_self = cfg.get("is_self", False)
        seen_keys.add(key)
        result.append({
            "key":        key,
            "node_id":    node_id,
            "name":       mn["long_name"] or cfg.get("name"),
            "short_name": mn["short_name"],
            "long_name":  mn["long_name"],
            "ip":         ip,
            "is_self":    is_self,
            "on_mesh":    True,
            "reachable":  False,
            "last_heard": mn.get("last_heard"),
            "role":       mn.get("role", "CLIENT"),
        })

    # Add IP-configured nodes not seen on mesh (e.g. offline hubs)
    for nid, cfg in ip_cfg.items():
        if cfg["key"] not in seen_keys:
            result.append({
                "key":        cfg["key"],
                "node_id":    nid,
                "name":       cfg["name"],
                "short_name": "",
                "long_name":  cfg["name"],
                "ip":         cfg["ip"] or "",
                "is_self":    cfg["is_self"],
                "on_mesh":    False,
                "reachable":  False,
                "last_heard": None,
                "role":       "CLIENT",
            })

    # Overlay BLE addresses on all nodes + add BLE-only nodes not yet in list
    ble_cache = _ble_cache_load()
    seen_node_ids = {n["node_id"] for n in result}
    for n in result:
        entry = ble_cache.get(n["node_id"]) or ble_cache.get(n["key"])
        if isinstance(entry, dict) and entry.get("address"):
            n["ble_address"] = entry["address"]
    for node_id, entry in ble_cache.items():
        if node_id in seen_node_ids:
            continue
        if not isinstance(entry, dict) or not entry.get("address"):
            continue
        name = entry.get("long_name") or entry.get("ble_name") or node_id
        result.append({
            "key":        node_id,
            "node_id":    node_id,
            "name":       name,
            "short_name": "",
            "long_name":  name,
            "ip":         "",
            "is_self":    False,
            "on_mesh":    False,
            "reachable":  False,
            "ble_address": entry["address"],
            "last_heard": entry.get("last_seen"),
            "role":       "CLIENT",
        })

    # ARP-based IP discovery: for nodes with no IP, check ARP table using node_id MAC suffix.
    # Also re-check ARP for nodes with an IP that are currently unreachable (DHCP may have changed).
    # Discovered IPs are written to jtak.yaml so they persist across refreshes.
    for n in result:
        nid = n.get("node_id", "")
        if not nid or n.get("is_self"):
            continue
        if not n.get("ip"):
            # No IP known — try ARP discovery
            discovered = _arp_lookup(nid)
            if discovered:
                log.info("ARP discovered %s → %s", nid, discovered)
                n["ip"] = discovered
                _yaml_update_ip(nid, n.get("name", nid), discovered)

    # Check TCP reachability in parallel for nodes with IPs.
    # If TCP fails on a known-IP node, re-check ARP in case DHCP changed.
    async def check(n):
        if not n["ip"]:
            return n
        ok = await loop.run_in_executor(None, _tcp_reachable, n["ip"])
        if ok:
            return {**n, "reachable": True}
        # TCP failed — see if ARP has a fresher IP
        nid = n.get("node_id", "")
        if nid and not n.get("is_self"):
            fresh_ip = await loop.run_in_executor(None, _arp_lookup, nid)
            if fresh_ip and fresh_ip != n["ip"]:
                log.info("ARP: IP changed for %s: %s → %s", nid, n["ip"], fresh_ip)
                ok2 = await loop.run_in_executor(None, _tcp_reachable, fresh_ip)
                if ok2:
                    _yaml_update_ip(nid, n.get("name", nid), fresh_ip)
                    return {**n, "ip": fresh_ip, "reachable": True}
            # TCP failed + ARP found nothing → if node is on mesh, clear stale IP
            # so it falls back to MESH ONLY and gets re-discovered next load
            if n.get("on_mesh") and not fresh_ip:
                _yaml_clear_ip(nid)
                _discovery_cooldown.pop(nid, None)
                log.info("Cleared stale IP for %s (%s) — back to mesh-only", nid, n["ip"])
                return {**n, "ip": "", "reachable": False}
        return {**n, "reachable": False}

    result = list(await asyncio.gather(*[check(n) for n in result]))

    # Background IP discovery: fire off for mesh-only nodes still without IPs,
    # skipping nodes that were recently scanned and not found (cooldown).
    global _discovery_running, _discovery_cooldown
    now = time.monotonic()
    _discovery_cooldown = {
        nid: ts for nid, ts in _discovery_cooldown.items()
        if now - ts < _DISCOVERY_COOLDOWN_SECS
    }
    pending_discovery = [
        n["node_id"] for n in result
        if n.get("on_mesh") and not n.get("is_self")
        and not n.get("ip") and not isBleOnly_node(n)
        and n["node_id"] not in _discovery_cooldown
    ]
    if pending_discovery and not _discovery_running:
        asyncio.ensure_future(_bg_discover_ips(pending_discovery))

    # Sort: self first, then TCP-reachable, then mesh-only, then name
    def sort_key(n):
        return (not n["is_self"], not n["reachable"], not n["on_mesh"], n["name"].lower())

    return {
        "nodes": sorted(result, key=sort_key),
        "discovery_pending": bool(pending_discovery),
    }


@router.get("/mesh-config/node/{key}/lora", dependencies=[Depends(require_auth)])
async def get_node_lora(key: str):
    ip = _key_to_ip(key)
    if ip:
        try:
            cfg = await asyncio.get_event_loop().run_in_executor(None, _read_lora, ip)
            return {"key": key, "mesh_only": False, **cfg}
        except Exception as e:
            raise HTTPException(503, f"Could not connect: {e}")

    # Check BLE cache — if node has a BLE address, read live via BLE
    ble_cache = _ble_cache_load()
    node_id = _key_to_node_id(key)
    ble_addr = _ble_addr_for(key, ble_cache)
    if ble_addr:
        try:
            loop = asyncio.get_event_loop()
            cfg = await asyncio.wait_for(
                loop.run_in_executor(None, _read_lora_ble, ble_addr), timeout=30)
            return {"key": key, "mesh_only": False, "ble_only": True, **cfg}
        except Exception as e:
            log.warning("BLE read failed for %s (%s): %s", key, ble_addr, e)
            # Fall through to mesh cache

    # Mesh-only node: return cached data (no LoRa config available)
    loop = asyncio.get_event_loop()
    try:
        mesh_nodes = await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_mesh_nodes_blocking), timeout=15)
    except Exception:
        mesh_nodes = {}
    mn = mesh_nodes.get(node_id) or mesh_nodes.get(key) or {}
    if not mn:
        raise HTTPException(404, f"Node '{key}' not found in mesh or BLE cache")

    batt_raw = mn.get("battery_level")
    batt_pct = None if batt_raw is None else ("POWERED" if batt_raw == 101 else batt_raw)
    voltage  = mn.get("voltage") or None
    uptime_s = mn.get("uptime_secs")
    uptime_str = None
    if uptime_s:
        h, rem = divmod(int(uptime_s), 3600)
        m, s   = divmod(rem, 60)
        uptime_str = f"{h}h {m}m {s}s"

    last_heard = mn.get("last_heard")
    last_str = None
    if last_heard:
        import datetime
        from zoneinfo import ZoneInfo
        tz = get("timezone", "America/Denver") or "America/Denver"
        try:
            tzinfo = ZoneInfo(tz)
        except Exception:
            tzinfo = ZoneInfo("America/Denver")
        last_str = datetime.datetime.fromtimestamp(last_heard, tz=tzinfo).strftime("%H:%M:%S")

    return {
        "key":             key,
        "mesh_only":       True,
        "long_name":       mn.get("long_name", key),
        "short_name":      mn.get("short_name", ""),
        "role":            mn.get("role", "—"),
        "snr":             mn.get("snr"),
        "last_heard":      last_str,
        # Device metrics (from periodic mesh broadcasts — may be stale)
        "battery_pct":     batt_pct,
        "voltage_v":       round(voltage, 2) if voltage else None,
        "uptime":          uptime_str,
        "uptime_secs":     uptime_s,
        "channel_util_pct": round(mn.get("channel_util") or 0, 1),
        "air_util_tx_pct":  round(mn.get("air_util_tx") or 0, 2),
        # Power config not available
        "pwr_is_power_saving": False, "pwr_screen_on_secs": 0,
        "pwr_wait_bt_secs": 0, "pwr_mesh_sds_timeout": 0, "pwr_min_wake_secs": 0,
        # LoRa config not readable remotely
        "channels": [],
        "use_preset": None, "modem_preset": None, "modem_preset_name": None,
        "spread_factor": None, "bandwidth": None, "coding_rate": None,
        "tx_power": None, "hop_limit": None,
        "sx126x_rx_boosted_gain": None, "override_duty_cycle": None, "channel_num": None,
    }


class LoraConfig(BaseModel):
    node_keys: list[str]
    method: str                             = "mesh"   # "mesh" | "ble"
    register_admin_key: bool                = False
    use_preset: Optional[bool]              = None
    modem_preset: Optional[int]             = None
    spread_factor: Optional[int]            = None
    bandwidth: Optional[int]                = None
    coding_rate: Optional[int]              = None
    tx_power: Optional[int]                 = None
    hop_limit: Optional[int]                = None
    sx126x_rx_boosted_gain: Optional[bool]  = None
    override_duty_cycle: Optional[bool]     = None


@router.post("/mesh-config/batch/lora", dependencies=[Depends(require_auth)])
async def batch_lora(req: LoraConfig):
    cfg = req.model_dump(
        exclude={"node_keys", "method", "register_admin_key"},
        exclude_none=True)

    # BLE method: apply via direct BLE connection (register_admin_key also handled here)
    if req.method == "ble":
        if not cfg and not req.register_admin_key:
            raise HTTPException(400, "No config fields or register_admin_key provided")
        loop = asyncio.get_event_loop()
        ble_cache = _ble_cache_load()
        ble_items = []
        results = []
        for key in req.node_keys:
            name = _get_node_name(key)
            nid  = _key_to_node_id(key) or key
            entry = ble_cache.get(nid) or ble_cache.get(key)
            if isinstance(entry, dict) and entry.get("address"):
                ble_items.append({"key": key, "node_id": nid, "name": name,
                                   "ble_address": entry["address"]})
            else:
                results.append({"key": key, "name": name, "ok": False,
                                 "message": "no BLE address — run BLE Scan first"})
        if ble_items:
            ble_results = await asyncio.wait_for(
                loop.run_in_executor(
                    None, _ble_apply_batch, ble_items, cfg, req.register_admin_key),
                timeout=300)
            results.extend(ble_results)
        return {"results": results}

    # Mesh / TCP method with automatic BLE fallback
    if not cfg:
        raise HTTPException(400, "No config fields provided")

    loop       = asyncio.get_event_loop()
    results    = []
    tcp_items  = []
    mesh_items = []
    ble_items  = []
    ble_cache  = _ble_cache_load()

    for key in req.node_keys:
        ip   = _key_to_ip(key)
        nid  = _key_to_node_id(key) or (key if key.startswith("!") else None)
        name = _get_node_name(key)
        if ip:
            tcp_items.append({"key": key, "ip": ip, "node_id": nid, "name": name})
        elif nid:
            addr = _ble_addr_for(key, ble_cache)
            if addr and _is_mesh_stale(nid):
                # Node is in meshtasticd cache but stale — mesh send would silently fail.
                # Go straight to BLE.
                ble_items.append({"key": key, "node_id": nid, "name": name,
                                   "ble_address": addr})
            else:
                mesh_items.append({"key": key, "node_id": nid, "name": name})
        else:
            addr = _ble_addr_for(key, ble_cache)
            if addr:
                ble_items.append({"key": key, "node_id": key, "name": name,
                                   "ble_address": addr})
            else:
                results.append({"key": key, "name": name, "ok": False,
                                 "message": "Cannot resolve node — no IP, mesh, or BLE"})

    # TCP nodes — fall back to BLE on failure
    for item in tcp_items:
        try:
            msg = await loop.run_in_executor(None, _apply_lora, item["ip"], cfg)
            results.append({"key": item["key"], "name": item["name"],
                             "ok": True, "message": msg})
        except Exception as e:
            addr = _ble_addr_for(item["key"], ble_cache)
            if addr:
                log.info("TCP failed for %s, queuing BLE fallback", item["name"])
                ble_items.append({"key": item["key"], "node_id": item["node_id"],
                                   "name": item["name"], "ble_address": addr})
            else:
                results.append({"key": item["key"], "name": item["name"],
                                 "ok": False, "message": str(e)})

    # Mesh nodes — fall back to BLE on failure
    if mesh_items:
        mesh_results = await loop.run_in_executor(
            None, _mesh_apply_lora_batch, mesh_items, cfg)
        for r in mesh_results:
            if not r["ok"]:
                nid  = next((it["node_id"] for it in mesh_items if it["key"] == r["key"]), r["key"])
                addr = _ble_addr_for(r["key"], ble_cache)
                if addr:
                    log.info("Mesh failed for %s, queuing BLE fallback", r["name"])
                    ble_items.append({"key": r["key"], "node_id": nid,
                                       "name": r["name"], "ble_address": addr})
                else:
                    results.append(r)
            else:
                results.append(r)

    # BLE fallback / BLE-only nodes
    if ble_items:
        ble_results = await asyncio.wait_for(
            loop.run_in_executor(None, _ble_apply_batch, ble_items, cfg, False),
            timeout=300)
        for r in ble_results:
            r["message"] = "BLE: " + r["message"]
        results.extend(ble_results)

    return {"results": results}


@router.post("/mesh-config/ble/scan", dependencies=[Depends(require_auth)])
async def ble_scan():
    loop = asyncio.get_event_loop()
    result = await asyncio.wait_for(
        loop.run_in_executor(None, _ble_scan_blocking), timeout=30)
    return result


@router.get("/mesh-config/ble/cache", dependencies=[Depends(require_auth)])
async def ble_cache_get():
    return {"cache": _ble_cache_load()}


def _apply_device_cfg(ip: str, tzdef: Optional[str], bt_mode: Optional[int], bt_pin: Optional[int]) -> str:
    """Apply device + bluetooth config to a node via TCP."""
    iface = _connect(ip)
    try:
        msgs = []
        if tzdef is not None:
            iface.localNode.localConfig.device.tzdef = tzdef
            iface.localNode.writeConfig("device")
            msgs.append(f"tzdef={tzdef}")
        if bt_mode is not None:
            iface.localNode.localConfig.bluetooth.mode = bt_mode
            if bt_mode == 1 and bt_pin is not None:
                iface.localNode.localConfig.bluetooth.fixed_pin = bt_pin
            iface.localNode.writeConfig("bluetooth")
            msgs.append(f"bt_mode={bt_mode}" + (f" pin={bt_pin}" if bt_mode == 1 and bt_pin else ""))
        return ", ".join(msgs) if msgs else "ok"
    finally:
        try: iface.close()
        except Exception: pass


class DeviceConfig(BaseModel):
    node_keys: list[str]
    tzdef:    Optional[str] = None
    bt_mode:  Optional[int] = None   # 0=RANDOM_PIN 1=FIXED_PIN 2=NO_PIN
    bt_pin:   Optional[int] = None


@router.post("/mesh-config/batch/device", dependencies=[Depends(require_auth)])
async def batch_device(req: DeviceConfig):
    if not req.node_keys:
        return {"results": []}
    if req.tzdef is None and req.bt_mode is None:
        return {"results": []}

    loop      = asyncio.get_event_loop()
    ip_cfg    = _build_ip_config()
    ble_cache = _ble_cache_load()
    results   = []

    for key in req.node_keys:
        node_id  = _key_to_node_id(key) or key
        cfg_entry = next((c for n, c in ip_cfg.items() if c["key"] == key or n == key), {})
        # Name: yaml config → mesh cache → key as last resort
        name = (cfg_entry.get("name")
                or (_mesh_cache.get(key) or {}).get("long_name")
                or (_mesh_cache.get(node_id) or {}).get("long_name")
                or key)
        ip        = _key_to_ip(key)

        # TCP path
        if ip:
            try:
                msg = await loop.run_in_executor(
                    None, _apply_device_cfg, ip, req.tzdef, req.bt_mode, req.bt_pin)
                results.append({"key": key, "name": name, "ok": True, "message": msg})
                continue
            except Exception as e:
                log.warning("device cfg TCP failed %s: %s", key, e)

        # Mesh path — PKI-signed via local meshtasticd → LoRa
        nid      = _key_to_node_id(key) or (key if key.startswith("!") else None)
        ble_addr = _ble_addr_for(key, ble_cache)
        if nid:
            def _send_device_cfg_mesh(node_id, tzdef, bt_mode, bt_pin):
                import meshtastic.tcp_interface
                from meshtastic.protobuf import admin_pb2 as _adm
                dest_num = int(node_id.lstrip("!"), 16)
                iface = meshtastic.tcp_interface.TCPInterface(hostname="localhost")
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    if iface.nodes: break
                    time.sleep(0.3)
                try:
                    if tzdef is not None:
                        msg = _adm.AdminMessage()
                        msg.set_config.device.tzdef = tzdef
                        _mesh_admin_send(iface, dest_num, msg)
                    if bt_mode is not None:
                        msg = _adm.AdminMessage()
                        msg.set_config.bluetooth.mode = bt_mode
                        if bt_mode == 1 and bt_pin is not None:
                            msg.set_config.bluetooth.fixed_pin = bt_pin
                        _mesh_admin_send(iface, dest_num, msg)
                finally:
                    try: iface.close()
                    except Exception: pass

            try:
                await loop.run_in_executor(
                    None, _send_device_cfg_mesh, nid, req.tzdef, req.bt_mode, req.bt_pin)
                parts = []
                if req.tzdef: parts.append(f"tzdef={req.tzdef}")
                if req.bt_mode is not None: parts.append(f"bt_mode={req.bt_mode}")
                results.append({"key": key, "name": name, "ok": True,
                                 "message": "sent via mesh: " + ", ".join(parts)})
                continue
            except Exception as e:
                log.warning("device cfg mesh failed %s: %s", key, e)

        # BLE path — fallback when TCP and mesh both unavailable/failed
        if ble_addr:
            try:
                import asyncio as _asyncio
                from bleak import BleakClient as _BC, BleakScanner as _BS
                from meshtastic.protobuf import admin_pb2 as _adm

                async def _ble_dev_write():
                    raw = await _BS.discover(timeout=10.0, return_adv=True)
                    device = next((dev for addr,(dev,_) in raw.items()
                                   if addr.upper()==ble_addr.upper()), None)
                    if device is None:
                        raise RuntimeError("not found in BLE scan")
                    async with _BC(device, timeout=60.0) as client:
                        if req.tzdef is not None:
                            msg = _adm.AdminMessage()
                            msg.set_config.device.tzdef = req.tzdef
                            await client.write_gatt_char(
                                _BLE_TORADIO_UUID, _make_to_radio_bytes(msg), response=True)
                        if req.bt_mode is not None:
                            msg = _adm.AdminMessage()
                            msg.set_config.bluetooth.mode = req.bt_mode
                            if req.bt_mode == 1 and req.bt_pin is not None:
                                msg.set_config.bluetooth.fixed_pin = req.bt_pin
                            await client.write_gatt_char(
                                _BLE_TORADIO_UUID, _make_to_radio_bytes(msg), response=True)

                await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: _asyncio.run(_ble_dev_write())),
                    timeout=60)
                parts = []
                if req.tzdef: parts.append(f"tzdef={req.tzdef}")
                if req.bt_mode is not None: parts.append(f"bt_mode={req.bt_mode}")
                results.append({"key": key, "name": name, "ok": True,
                                 "message": "BLE: " + ", ".join(parts)})
                continue
            except Exception as e:
                results.append({"key": key, "name": name, "ok": False, "message": f"BLE: {e}"})
                continue

        results.append({"key": key, "name": name, "ok": False,
                         "message": "no reachable path (no IP, mesh, or BLE)"})

    return {"results": results}


class IdentityConfig(BaseModel):
    node_keys: list[str]
    long_name: Optional[str]  = None
    short_name: Optional[str] = None


@router.post("/mesh-config/batch/identity", dependencies=[Depends(require_auth)])
async def batch_identity(req: IdentityConfig):
    loop       = asyncio.get_event_loop()
    results    = []
    tcp_items  = []
    mesh_items = []
    ble_items  = []
    ble_cache  = _ble_cache_load()

    for key in req.node_keys:
        ip   = _key_to_ip(key)
        nid  = _key_to_node_id(key) or (key if key.startswith("!") else None)
        name = _get_node_name(key)
        if ip:
            tcp_items.append({"key": key, "ip": ip, "node_id": nid, "name": name})
        elif nid:
            addr = _ble_addr_for(key, ble_cache)
            if addr and _is_mesh_stale(nid):
                # Node is in meshtasticd cache but stale — mesh send would silently fail.
                # Go straight to BLE.
                ble_items.append({"key": key, "node_id": nid, "name": name,
                                   "ble_address": addr})
            else:
                mesh_items.append({"key": key, "node_id": nid, "name": name})
        else:
            addr = _ble_addr_for(key, ble_cache)
            if addr:
                ble_items.append({"key": key, "node_id": key, "name": name,
                                   "ble_address": addr})
            else:
                results.append({"key": key, "name": name, "ok": False,
                                 "message": "Cannot resolve node — no IP, mesh, or BLE"})

    for item in tcp_items:
        try:
            msg = await loop.run_in_executor(
                None, _apply_identity, item["ip"], req.long_name, req.short_name)
            if req.long_name:
                _yaml_update_name(item["key"], item["node_id"], req.long_name)
                # Patch mesh cache immediately so node list reflects new name on next refresh
                nid = item.get("node_id")
                if nid and nid in _mesh_cache:
                    _mesh_cache[nid]["long_name"] = req.long_name
            results.append({"key": item["key"], "name": item["name"],
                             "ok": True, "message": msg})
        except Exception as e:
            addr = _ble_addr_for(item["key"], ble_cache)
            if addr:
                ble_items.append({"key": item["key"], "node_id": item["node_id"],
                                   "name": item["name"], "ble_address": addr})
            else:
                results.append({"key": item["key"], "name": item["name"],
                                 "ok": False, "message": str(e)})

    if mesh_items:
        mesh_results = await loop.run_in_executor(
            None, _mesh_identity_batch, mesh_items, req.long_name, req.short_name)
        for r in mesh_results:
            if not r["ok"]:
                addr = _ble_addr_for(r["key"], ble_cache)
                if addr:
                    nid = next((it["node_id"] for it in mesh_items if it["key"] == r["key"]), r["key"])
                    ble_items.append({"key": r["key"], "node_id": nid,
                                       "name": r["name"], "ble_address": addr})
                else:
                    results.append(r)
            else:
                if req.long_name:
                    nid = next((it["node_id"] for it in mesh_items if it["key"] == r["key"]), None)
                    _yaml_update_name(r["key"], nid, req.long_name)
                    if nid and nid in _mesh_cache:
                        _mesh_cache[nid]["long_name"] = req.long_name
                results.append(r)

    if ble_items:
        # identity via BLE: send set_owner AdminMessage
        def _ble_identity_batch(items):
            from meshtastic.protobuf import admin_pb2 as _admin
            cfg_identity = {"long_name": req.long_name, "short_name": req.short_name}
            # Reuse _ble_apply_batch with a custom write — encode identity in cfg placeholder
            # Actually write set_owner directly
            import asyncio as _asyncio
            from bleak import BleakClient as _BC, BleakScanner as _BS

            async def _run():
                res = []
                for item in items:
                    raw = await _BS.discover(timeout=10.0, return_adv=True)
                    device = next((d for a, (d, _) in raw.items()
                                   if a.upper() == item["ble_address"].upper()), None)
                    if device is None:
                        res.append({"key": item["key"], "name": item["name"],
                                    "ok": False, "message": "BLE: not in range"})
                        continue
                    await _asyncio.sleep(3)
                    try:
                        admin = _admin.AdminMessage()
                        admin.set_owner.long_name  = req.long_name  or item["name"]
                        admin.set_owner.short_name = req.short_name or ""
                        async with _BC(device, timeout=60.0) as client:
                            await client.write_gatt_char(
                                _BLE_TORADIO_UUID, _make_to_radio_bytes(admin), response=True)
                        if req.long_name:
                            _yaml_update_name(item["key"], item.get("node_id"), req.long_name)
                        res.append({"key": item["key"], "name": item["name"],
                                    "ok": True, "message": "BLE: identity sent"})
                    except Exception as e:
                        res.append({"key": item["key"], "name": item["name"],
                                    "ok": False, "message": f"BLE: {e}"})
                    await _asyncio.sleep(2)
                return res
            return _asyncio.run(_run())

        ble_results = await asyncio.wait_for(
            loop.run_in_executor(None, _ble_identity_batch, ble_items), timeout=300)
        results.extend(ble_results)

    return {"results": results}


class ActionRequest(BaseModel):
    node_keys: list[str]
    action: str


@router.post("/mesh-config/batch/action", dependencies=[Depends(require_auth)])
async def batch_action(req: ActionRequest):
    if req.action not in ("reboot", "nodeinfo", "dbreset", "factory_reset"):
        raise HTTPException(400, f"Unknown action: {req.action}")

    loop       = asyncio.get_event_loop()
    results    = []
    tcp_items  = []
    mesh_items = []
    ble_items  = []
    ble_cache  = _ble_cache_load()

    for key in req.node_keys:
        ip   = _key_to_ip(key)
        nid  = _key_to_node_id(key) or (key if key.startswith("!") else None)
        name = _get_node_name(key)
        if ip:
            tcp_items.append({"key": key, "ip": ip, "node_id": nid, "name": name})
        elif nid:
            addr = _ble_addr_for(key, ble_cache)
            if addr and _is_mesh_stale(nid):
                # Node is in meshtasticd cache but stale — mesh send would silently fail.
                # Go straight to BLE.
                ble_items.append({"key": key, "node_id": nid, "name": name,
                                   "ble_address": addr})
            else:
                mesh_items.append({"key": key, "node_id": nid, "name": name})
        else:
            addr = _ble_addr_for(key, ble_cache)
            if addr:
                ble_items.append({"key": key, "node_id": key, "name": name,
                                   "ble_address": addr})
            else:
                results.append({"key": key, "name": name, "ok": False,
                                 "message": "Cannot resolve node — no IP, mesh, or BLE"})

    for item in tcp_items:
        try:
            msg = await loop.run_in_executor(None, _do_action, item["ip"], req.action)
            results.append({"key": item["key"], "name": item["name"],
                             "ok": True, "message": msg})
        except Exception as e:
            addr = _ble_addr_for(item["key"], ble_cache)
            if addr:
                ble_items.append({"key": item["key"], "node_id": item["node_id"],
                                   "name": item["name"], "ble_address": addr})
            else:
                results.append({"key": item["key"], "name": item["name"],
                                 "ok": False, "message": str(e)})

    if mesh_items:
        mesh_results = await loop.run_in_executor(
            None, _mesh_action_batch, mesh_items, req.action)
        for r in mesh_results:
            if not r["ok"]:
                addr = _ble_addr_for(r["key"], ble_cache)
                if addr:
                    nid = next((it["node_id"] for it in mesh_items if it["key"] == r["key"]), r["key"])
                    ble_items.append({"key": r["key"], "node_id": nid,
                                       "name": r["name"], "ble_address": addr})
                else:
                    results.append(r)
            else:
                results.append(r)

    # BLE actions: reboot and dbreset are supported (send AdminMessage); others not applicable
    if ble_items:
        if req.action not in ("reboot", "dbreset"):
            for item in ble_items:
                results.append({"key": item["key"], "name": item["name"], "ok": False,
                                 "message": f"BLE: '{req.action}' not supported over BLE"})
        elif req.action == "dbreset":
            def _ble_dbreset_batch(items):
                import asyncio as _asyncio
                from bleak import BleakClient as _BC, BleakScanner as _BS
                from meshtastic.protobuf import admin_pb2 as _admin

                async def _run():
                    res = []
                    for item in items:
                        raw = await _BS.discover(timeout=10.0, return_adv=True)
                        device = next((d for a, (d, _) in raw.items()
                                       if a.upper() == item["ble_address"].upper()), None)
                        if device is None:
                            res.append({"key": item["key"], "name": item["name"],
                                        "ok": False, "message": "BLE: not in range"})
                            continue
                        await _asyncio.sleep(3)
                        try:
                            admin = _admin.AdminMessage()
                            admin.nodedb_reset = True
                            async with _BC(device, timeout=60.0) as client:
                                await client.write_gatt_char(
                                    _BLE_TORADIO_UUID, _make_to_radio_bytes(admin), response=True)
                            res.append({"key": item["key"], "name": item["name"],
                                        "ok": True, "message": "BLE: db reset sent"})
                        except Exception as e:
                            res.append({"key": item["key"], "name": item["name"],
                                        "ok": False, "message": f"BLE: {e}"})
                        await _asyncio.sleep(2)
                    return res
                return _asyncio.run(_run())

            ble_results = await asyncio.wait_for(
                loop.run_in_executor(None, _ble_dbreset_batch, ble_items), timeout=300)
            results.extend(ble_results)
        else:
            def _ble_reboot_batch(items):
                import asyncio as _asyncio
                from bleak import BleakClient as _BC, BleakScanner as _BS
                from meshtastic.protobuf import admin_pb2 as _admin

                async def _run():
                    res = []
                    for item in items:
                        raw = await _BS.discover(timeout=10.0, return_adv=True)
                        device = next((d for a, (d, _) in raw.items()
                                       if a.upper() == item["ble_address"].upper()), None)
                        if device is None:
                            res.append({"key": item["key"], "name": item["name"],
                                        "ok": False, "message": "BLE: not in range"})
                            continue
                        await _asyncio.sleep(3)
                        try:
                            admin = _admin.AdminMessage()
                            admin.reboot_seconds = 3
                            async with _BC(device, timeout=60.0) as client:
                                await client.write_gatt_char(
                                    _BLE_TORADIO_UUID, _make_to_radio_bytes(admin), response=True)
                            res.append({"key": item["key"], "name": item["name"],
                                        "ok": True, "message": "BLE: reboot sent"})
                        except Exception as e:
                            res.append({"key": item["key"], "name": item["name"],
                                        "ok": False, "message": f"BLE: {e}"})
                        await _asyncio.sleep(2)
                    return res
                return _asyncio.run(_run())

            ble_results = await asyncio.wait_for(
                loop.run_in_executor(None, _ble_reboot_batch, ble_items), timeout=300)
            results.extend(ble_results)

    # On dbreset: also purge stale nodes from local SQLite (nodes inactive >7 days)
    if req.action == "dbreset":
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            db = await get_db()
            for table in ("positions", "rf_metrics"):
                await db.execute(
                    f"DELETE FROM {table} WHERE source_id IN "
                    f"(SELECT source_id FROM {table} GROUP BY source_id HAVING MAX(timestamp) < ?)",
                    (cutoff,)
                )
            await db.commit()
            print("[mesh-config] dbreset: purged stale nodes (>7d) from SQLite positions + rf_metrics")
        except Exception as e:
            print(f"[mesh-config] dbreset SQLite cleanup error: {e}")

    return {"results": results}
