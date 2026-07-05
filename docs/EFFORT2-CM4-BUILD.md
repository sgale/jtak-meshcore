# jTAK Meshcore Hub — CM4 Build Plan & Handoff (Effort 2)

> **For a fresh Claude Code session on the new CM4 hub:** read this file top to
> bottom — it is a self-contained handoff of a long planning/build session that
> happened on the `stage` Pi. It carries the decisions, recipes, and design
> rules you need to continue. When done reading, confirm the current hardware
> state and pick up at **§8 Current status / next action**.

---

## 1. Context — where this fits
Two-effort hub-side program (operator: Sean Gale, callsign work / ARES / mesh):
- **Effort 1 (DONE):** git-ify the current Meshtastic hubs. The hub code is now a
  private repo **`git@github.com:sgale/jtak-hub.git`**, baseline tagged
  **`v1.0-meshtastic`**. That tag is the fork point for this effort.
- **Effort 2 (THIS doc):** a NEW hub with a different architecture — **replace
  Meshtastic → Meshcore**, add a **USB Wi-Fi HaLow** backhaul, and use
  **Reticulum** to route LoRa ↔ WiFi. Built on new CM4 hardware; the existing
  Meshtastic hubs (tak1–5) stay in production, untouched.

This is a **new repo forked from `v1.0-meshtastic`**, NOT a branch (long-lived divergence).

## 2. Hardware
- **Compute:** Raspberry Pi **Compute Module 4 (8 GB)** + Waveshare CM4 IO board + NVMe. 8 GB is required (see §6 — jTAK + OTS co-tenancy).
- **HaLow:** **Morse Micro MM8108-EKH19** USB dongle (802.11ah, sub-GHz, 1–3 mi hub↔hub). USB id `325b:8100`.
- **LoRa:** Meshcore-compatible radio (~900 MHz, replacing the Meshtastic radio).

## 3. OS decision — Raspberry Pi OS 64-bit (Bookworm) Lite
Chosen over Ubuntu / OpenWRT because it:
1. **Mirrors the current hubs** — the jTAK stack (FastAPI, systemd, NetworkManager AP+STA, the wifi-uplink feature) ports 1:1. One OS across the fleet.
2. **Is the best base for the HaLow driver** — Morse maintains a Raspberry Pi kernel fork (`MorseMicro/rpi-linux`). Ubuntu's kernel is NOT a Morse target; OpenWRT can't run the Python app stack.
3. **Runs OpenTAKServer** — OTS is already installed on Pi OS on jtak2.

Pin near Morse's tested kernel (**~6.12.21**) to ease the HaLow build. (The Meshtastic hubs run 6.12.47, which is newer than Morse tested — expect patch friction on very new kernels.)

## 4. Build order (each step has a verify)
1. **Flash Pi OS Lite 64-bit**, base config (hostname, ssh, NVMe boot). → *verify:* boots, ssh in.
2. **Restore the jTAK stack** from `sgale/jtak-hub`: clone to `/opt/jtak`, create the venv, copy `config/jtak.yaml.example` → `config/jtak.yaml` and fill secrets (real `jtak.yaml`, `jtak.identity.yaml` are gitignored — provision fresh), install systemd units from `config/systemd/`, nginx from `config/`. → *verify:* `jtak-api` serves, dashboard loads.
3. **HaLow driver (MM8108)** — see §5 recipe. → *verify:* a `wlan`/`halow` iface appears, `morse_cli` works, link + `iperf` between two HaLow nodes at range.
4. **Meshcore (replace Meshtastic)** — MVP-A: Meshcore feeds node positions/RF into the existing SQLite/dashboard. → *verify:* dashboard shows a Meshcore-sourced position, zero Meshtastic in the path.
5. **HaLow backhaul + Reticulum** — MVP-B (HaLow link between 2 hubs, iperf at range) then MVP-C (Reticulum bridges LoRa↔HaLow: a LoRa-only node reaches a WiFi-only node and vice-versa).
6. **OpenTAKServer co-tenancy** — see §6.

## 5. HaLow driver recipe (Pi OS) — the hard part
- **No prebuilt .deb** for the EKH19 on Pi OS — compile from source.
- Driver: `github.com/MorseMicro/morse_driver`, build with `CONFIG_MORSE_USB=y CONFIG_MORSE_SDIO=n CONFIG_WLAN_VENDOR_MORSE=m`.
- Firmware: `github.com/MorseMicro/morse-firmware` → `/lib/firmware/morse/`.
- Userspace S1G tools: `github.com/MorseMicro/hostap` → `wpa_supplicant_s1g`, `hostapd_s1g`, `morse_cli`.
- **Kernel patch practically required** (driver ≥1.15.3) via `MorseMicro/rpi-linux`; tested kernels top out ~6.12.21.
- **USB adapters need NO device-tree overlay** (unlike SPI/SDIO HATs).
- Quirk: the driver **fakes 802.11ac / shows 5 GHz freqs** in the kernel — intentional Linux-compat, not a bug.
- Community refs: morsemicro community threads `/t/…/1025` (EKH19 usage), `/t/…/88` (RPi5), `/t/…/1124` (RPi OS build). Morse recommends their OpenWRT fork — not viable here (we need the Python app stack).

## 6. Design rules (carry these into the fork)
1. **Backhaul-only routing:** HaLow (`halow0`) is hub↔hub transport — **NEVER a default route.** Route only a dedicated backhaul subnet (e.g. `10.10.0.0/24`). Same principle as the wifi-uplink (default routes only on egress ifaces).
2. **Pin interface names** via udev/systemd.link (by USB path/MAC): `halow0` (MM8108), `wlan-up` (uplink), etc. — with 2–3 USB radios + SDR, `wlanN` ordering is non-deterministic.
3. **RF coexistence (physical, not IP):** HaLow US band **902–928 MHz = SAME ISM band as LoRa**. Co-located TX desensitize each other → antenna separation / band-planning / duty-cycle coord / filtering. Most likely surprise on the new Pi. Persists through the fork (Meshcore still LoRa).
4. **Two hub↔hub paths now:** ZeroTier (over internet) + HaLow (direct RF). Arbitration = MVP-C routing layer (Reticulum).

## 7. OpenTAKServer co-tenancy (jTAK + OTS on one box)
From jtak2 recon: OTS installed but not yet configured (own venv `~/.opentakserver_venv`, workdir `~/ots`, service disabled).
- **Barely collides with jTAK.** Keep separate venvs (already the pattern).
- **RabbitMQ** — both use `:5672`. Share the broker with **separate vhosts** (jTAK exchange `jtak.telemetry`).
- **nginx `:443`** — both have web UIs → path-route `/jtak/` vs OTS's blocks.
- Ports: jTAK owns `8420`; OTS = `8088/8089` (CoT TCP/SSL), `8443` (HTTPS API/UI), `1883/8883` (MQTT), mediamtx video `1935/8554/8888/8889`. Confirm what owns Postgres `:5432`.
- **RAM:** a 4 GB Pi 4 is tight *idle* → **8 GB CM4** for both under load.

## 8. Portable feature: WiFi uplink (from the Meshtastic hubs)
Built & proven on jtak2 (branch `feature/wifi-uplink`): single-radio concurrent **AP + STA** with eth0-primary / wifi-backup failover, plus a dashboard page to type an SSID/password (Option B, no scan). Reusable on the CM4. Files: `bin/jtak-wifi`, `backend/api/routes_wifi.py`, `frontend/wifi.html`, `config/systemd/jtak-wifi-sta.service`. Key facts: BCM43455 does AP+STA but **same-channel only** (`#channels<=1`); NM autoconnect is unreliable for the dynamically-created `sta0` vif → boot path explicitly `nmcli connection up`s the saved uplink.

## 9. Current status / next action (updated 2026-07-05)
Built this far on host **`mcore1`** (CM4 4GB, Pi OS Bookworm, kernel 6.12.93), fork repo `sgale/jtak-meshcore` at `/opt/jtak`:
- DONE — **skeleton LIVE:** venv + deps, config-fallback identity (NO meshtasticd), `jtak-api` on :8420, nginx dashboard at `https://192.168.86.52/jtak/` (self-signed cert).
- DONE — **MVP-A pipeline PROVEN:** `ingest/meshcore_monitor.py` `write_position()` writes to `positions` (`source_type='meshcore'`); a test node flows to `/api/positions` + the map, zero Meshtastic in path. `run()` radio reader is a STUB.
- **YOUR NEXT ACTION (this session, on mcore1) — finish MVP-A:** attach the MeshCore radio, confirm its companion protocol (serial/BLE frame format), wire `run()` to decode real node positions + RF into `positions`/`rf_metrics`, add `jtak-meshcore.service` (mirror `tactical_monitor.service`), then strip the dormant Meshtastic path. After MVP-A: HaLow driver spike (see §5) — kernel work, done here on-device.
- Notes: set a real `admin.password` in `config/jtak.yaml` (still `CHANGEME`); a test `meshcore` row is still in the DB (delete anytime); interface pinned; master running memory lives on the `stage` Pi (does NOT travel — this doc is the bridge).

---
*Master planning memory lives on the `stage` Pi at `~/.claude/projects/-home-sdg/memory/` (files `project_jtak_hub_roadmap.md`, `changelog_jtak_hub.md`). That memory does NOT travel between machines — this file is the portable handoff. Keep it updated as the build progresses.*
