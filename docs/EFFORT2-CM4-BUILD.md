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
- **LoRa:** **MeshAdv Pi HAT** (`github.com/chrismyers2000/MeshAdv-Pi-Hat`) — **SX1262** (Ebyte E22-900M30S/M33S, ~900 MHz, **1 W PA up to 33 dBm**) on the Pi's **SPI0** (CLK=GPIO11, MOSI=10, MISO=9; CS=21, RESET=18, BUSY=20, DIO1/IRQ=16, TXEN=13, RXEN=12), plus UART GPS (ATGM336H, PPS→GPIO23) and I2C (GPIO2/3). NOT a USB/BLE companion node — it's a **bare radio the Pi drives over SPI**. ⚠️ **RF coexistence:** its ~900 MHz is the SAME band as the HaLow dongle (§6.3) — now with a 1 W PA in the mix. ⚠️ **CM4 IO board** must host the 40-pin HAT (SPI0 + those GPIOs are present, but it's a CM4 IO board, not a classic Pi — check physical fit).

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
> **BEFORE rebuilding the kernel — protect the boot.** A bad kernel can leave `mcore1` unbootable, and git won't help (code is safe on GitHub; the *boot* is the risk). So: back up `/boot` (`sudo cp -a /boot /boot.bak`), install the Morse-patched kernel *alongside* the working one (don't overwrite), keep the current kernel as a fallback, and have console/keyboard access. Do all kernel work locally on `mcore1`, never over SSH from another host.

- **No prebuilt .deb** for the EKH19 on Pi OS — compile from source.
- Driver: `github.com/MorseMicro/morse_driver`, build with `CONFIG_MORSE_USB=y CONFIG_MORSE_SDIO=n CONFIG_WLAN_VENDOR_MORSE=m` **plus `CONFIG_MORSE_VENDOR_COMMAND=y`** (without it, `bss_stats.c` calls `morse_vendor_*` unconditionally → modpost "undefined symbol" errors). Also `git submodule update --init` for `mmrc-submodule`.
- Firmware: `github.com/MorseMicro/morse-firmware` → `sudo make install` drops blobs (incl. `mm8108b2-rl.bin`) flat into `/lib/firmware/morse/`.
- Userspace S1G tools: `github.com/MorseMicro/hostap` → `wpa_supplicant_s1g`, `hostapd_s1g`, `wpa_cli_s1g`, `hostapd_cli_s1g`. **NOTE:** there is NO standalone `morse_cli` binary here — `morse_cli.c` has no `main()`, it links into `wpa_supplicant_s1g`. The chip-control CLI is Morse's separate `morsectrl` repo, which is **private/unreachable** (git asks for creds). Not needed for link bring-up (S1G channel/op_class live in the hostapd/wpa_supplicant config).
- ~~**Kernel patch practically required** (driver ≥1.15.3)~~ **← FALSE, disproven 2026-07-05.** Driver **1.17.9 built clean against stock kernel 6.12.93 with NO kernel patch and NO source edits** — every `.c` compiled, only link/config issues (see RESULTS). The risky `rpi-linux` kernel rebuild appears **unnecessary**. Keep the boot-protection ritual above on standby only if a future driver rev regresses.
- **USB adapters need NO device-tree overlay** (unlike SPI/SDIO HATs).
- Quirk: the driver **fakes 802.11ac / shows 5 GHz freqs** in the kernel — intentional Linux-compat, not a bug.
- Community refs: morsemicro community threads `/t/…/1025` (EKH19 usage), `/t/…/88` (RPi5), `/t/…/1124` (RPi OS build). Morse recommends their OpenWRT fork — not viable here (we need the Python app stack).

**RESULTS — HaLow driver DONE on mcore1 (2026-07-05, kernel 6.12.93, driver/fw/hostap all rel 1.17.9):**
- Build dir: `~sdg/halow-build/{morse_driver,morse-firmware,hostap}`. Apt deps: `build-essential bc bison flex libssl-dev libnl-3-dev libnl-genl-3-dev libnl-route-3-dev pkg-config iw` (+ matching `linux-headers-6.12.93+rpt-rpi-v8` already present).
- Driver built native: `make KERNEL_SRC=/lib/modules/$(uname -r)/build CONFIG_WLAN_VENDOR_MORSE=m CONFIG_MORSE_USB=y CONFIG_MORSE_SDIO=n CONFIG_MORSE_USER_ACCESS=y CONFIG_MORSE_VENDOR_COMMAND=y`. Produces `morse.ko` + `dot11ah.ko`.
- Runtime dep gotcha: `morse` needs `crc7` (`crc7_be_syndrome_table`). `modules_install` + `depmod` makes `modprobe morse` pull the whole chain (rfkill→cfg80211→mac80211→crc7→dot11ah→morse) automatically.
- **hostap OpenSSL-3 fix:** Bookworm's OpenSSL 3.0 + hostap's `-Werror` = deprecation build failures. Patched `wpa_supplicant/Makefile` and `hostapd/Makefile` to append `-Wno-error=deprecated-declarations -Wno-error=deprecated` right after the `CFLAGS += $(EXTRA_CFLAGS)` line (must follow `-Werror` to win). Then `cp defconfig .config` (already has `CONFIG_IEEE80211AH=y`) and `make wpa_supplicant_s1g wpa_cli_s1g` / `make hostapd_s1g hostapd_cli_s1g`. Binaries installed to `/usr/local/bin`.
- **VERIFY status:** ✅ iface appears — `wlan1` = `phy#1`, MAC `0c:bf:74:xx` (Morse OUI, OTP read OK), brings UP clean, no dmesg errors. ✅ driver persistent — installed in `/lib/modules/.../updates/`, auto-loads via `/etc/modules-load.d/morse.conf`. ❌ **link + iperf between two HaLow nodes NOT done — needs a 2nd HaLow node (only one dongle/one Pi here); inherently a two-box test.**
- **Follow-ups:** (1) ✅ **DKMS DONE** — source at `/usr/src/morse-1.17.9/` + `dkms.conf`; `dkms status` = `morse/1.17.9, 6.12.93+rpt-rpi-v8: installed`, `AUTOINSTALL=yes` → auto-rebuilds on apt kernel bumps. Modules now load from `/lib/modules/$KVER/updates/dkms/`. Rebuild deps must persist: `git submodule` content is baked into `/usr/src` copy; DKMS `MAKE[0]` carries `CONFIG_MORSE_VENDOR_COMMAND=y`. (2) ✅ **`halow0` rename DONE** — udev rule `/etc/udev/rules.d/70-halow-name.rules` (by MAC `0c:bf:74:00:29:71`); iface is `halow0`, persists across reload. (3) SSH here rides `wlan0` (192.168.86.52); eth0 carries internet — keep HaLow work off wlan0.

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
- DONE (2026-07-05) — **HaLow driver + S1G userspace BUILT & LIVE (see §5 RESULTS).** MM8108 dongle → `wlan1` (`phy#1`, Morse OUI MAC), driver 1.17.9 compiled clean on stock 6.12.93 **with no kernel patch**, persistent via `modprobe`/`modules-load.d`. `wpa_supplicant_s1g`/`hostapd_s1g` in `/usr/local/bin`. The feared kernel-rebuild spike was **not needed**.
- **NEXT ACTIONS (pick up here), two independent tracks:**
  1. **Finish MVP-A (MeshCore LoRa)** — still the primary Effort-2 goal. **Architecture confirmed 2026-07-05 (see §9.1):** the HAT is a bare SX1262-on-SPI, so the *Pi itself* runs a MeshCore **companion node** (`pyMC` or `meshcore-linux`, driving the HAT) and jTAK ingests via the **official `meshcore` PyPI lib** (`pip install meshcore`, connects Serial/TCP, async event-driven) — NOT manual frame decoding. Steps: enable SPI (`dtparam=spi=on`, `dtoverlay=spi0-0cs`) + install a companion node stack; fill `meshcore_monitor.py run()` to subscribe to `ADVERTISEMENT`/`CONTACTS`/`CHANNEL_MSG_RECV` events → `write_position()` + messages/RF; add `jtak-meshcore.service`; strip the dormant Meshtastic path.
     - **PREP DONE (2026-07-05 pm):** HAT **installed**; SPI **enabled** in `/boot/firmware/config.txt` (`dtparam=spi=on` + `dtoverlay=spi0-0cs` + `i2c_arm=on` + `enable_uart=1`; backup `config.txt.bak-*`) and **rebooted to apply**; `meshcore` **2.3.7** installed in `/opt/jtak/venv`; position fields **confirmed = `adv_lat`/`adv_lon`** on contacts (meshcore `reader.py`), connect via `create_serial()`/`create_tcp()`, events `ADVERTISEMENT`/`NEW_CONTACT`/`CONTACTS`.
     - **RESUME HERE (fresh session on mcore1):** (a) verify `/dev/spidev0.0` exists + `halow0` came back; (b) install a MeshCore **companion node** on the HAT (`pyMC_Repeater` per Boston-Mesh guide, pins CS21/RST18/BUSY20/DIO16/TXEN13/RXEN12) exposing companion over serial/TCP; (c) wire `meshcore_monitor.py run()` → `meshcore` lib subscribes to `ADVERTISEMENT`/`CONTACTS` → `write_position(adv_lat, adv_lon)`; (d) `jtak-meshcore.service`; (e) strip Meshtastic path.
  2. **HaLow next steps** — (a) ✅ DKMS done (auto-rebuilds on kernel bump); (b) ✅ `halow0` rename done (udev by-MAC); (c) **← THE open HaLow item:** the real test is a **two-node HaLow link + iperf at range** — needs a 2nd HaLow node, so schedule when a second dongle/hub is available; (d) then MVP-B/C (backhaul + Reticulum).
- Notes: set a real `admin.password` in `config/jtak.yaml` (still `CHANGEME`); a test `meshcore` row is still in the DB (delete anytime); **this session ran over SSH (rides `wlan0`)** — fine for the out-of-tree module build (no boot risk); master running memory lives on the `stage` Pi (does NOT travel — this doc is the bridge).

## 9.1 MeshCore vs the existing Meshtastic code — architecture, gaps & open scope (research 2026-07-05)
**Integration model (confirmed):** run a MeshCore **companion node** on the HAT (`pyMC` or `meshcore-linux`) + jTAK consumes the **official `meshcore` PyPI lib** (event-driven, Serial/TCP). jTAK writes zero crypto/mesh code. Forward-compatible with BOTH "channels+messages owned in jTAK's DB" (like the current MT hubs) AND a native **Room Server** later. `pyMC_core` (embed) was the earlier pick but is less mature and would reimplement the stack — rejected in favor of the companion-client lib.

**Position events — CONFIRMED:** `meshcore_py` `EventType` includes `ADVERTISEMENT`, `ADVERT_PATH`, `CONTACTS`, `NEW_CONTACT`, `PATH_UPDATE`, `NEIGHBOURS_RESPONSE`, `TELEMETRY_RESPONSE`, `CHANNEL_MSG_RECV`, `CONTACT_MSG_RECV`. Remote-node positions arrive via adverts→contacts (`adv_lat`/`adv_lon` — verify exact field at wire-time). ⚠️ **CAVEAT:** advert-based + opt-in → **sparser/less frequent than Meshtastic `POSITION_APP`**, and only location-sharing nodes appear on the map.

**Gaps vs the code already built here:**
| Existing jTAK (Meshtastic) | MeshCore | Verdict |
|---|---|---|
| positions (map) | adverts/contacts events | ✅ works, sparser cadence |
| mesh_messages / channels | `CHANNEL_MSG_RECV` + `send_msg` | ✅ ports |
| direct messages | per-contact PKI | ✅ upgrade |
| mesh_send_queue (outbound) | `send_msg`/`MSG_SENT`/`ACK` | ✅ ports |
| rf_metrics, telemetry | `STATS_RADIO`, `TELEMETRY_RESPONSE` | ✅ partial |
| **waypoints (OTA pin-drops)** — `routes_waypoints.py` broadcasts over MT mesh | **none in companion protocol** | ❌ **gap → decision (i)** |
| **`routes_mesh_config.py` (86 KB, MT admin config)** | different model (`CMD_SET_CHANNEL`/`GET_CHANNEL_INFO`/device query) | ❌ **~throwaway → decision (ii)** |
| `routes_meshtastic_debug.py` | — | ❌ drop |
| aircraft/weather/fire/atmo/polygons/sensors/IAP/federation/LED | non-mesh data sources | ✅ unaffected |

**Scoping decisions (Sean, 2026-07-05):**
- **(i) Waypoints — DEFERRED.** Not in MVP-A scope; revisit after the pipeline is live / HAT installed. When picked up: choose hub-local only (jTAK DB → own web clients) OR custom **hub↔hub** sync over a MeshCore data datagram (`CMD_SEND_CHANNEL_DATA_DATAGRAM` 0x3E) — neither renders as a native pin in MeshCore phone apps. Precedent: **MeshCore-TEAM** syncs waypoints over the mesh via its own convention; **MeshCore-Solo** firmware does local nav waypoints — so OTA is feasible as a jTAK-owned layer, just not in the base protocol.
- **(ii) Config surface — DECIDED: do NOT port the 86 KB MT config UI (*yet*).** Out of scope for MVP-A; MeshCore config is a different model anyway. A minimal MeshCore channel/config page comes later — "yet" ≠ rejected; revisit post-MVP-A.

**MeshCore roadmap re our needs (checked 2026-07):**
- **Waypoints:** no upstream *companion-protocol* waypoint standard on the roadmap; waypoints live in app/firmware forks (MeshCore-TEAM OTA sync, MeshCore-Solo nav). Treat as jTAK-owned.
- **TAK/ATAK:** **explicitly NOT on MeshCore's roadmap** (FAQ — MeshCore clients don't repeat, and ATAK is too chatty for the flood/path model; "could change if a repeating client firmware emerges"). BUT a community bridge exists — **`emuehlstein/OpenTAKServer-meshcore`** (OTS fork w/ MeshCore support; ~891 commits but early/experimental, 1★, thin docs). Since we already run **OpenTAKServer** (§7), the realistic TAK path is **MeshCore → OTS → ATAK CoT**, not jTAK speaking CoT itself.

## 9.2 MVP-A hardware bring-up — LIVE on real radios (2026-07-05 pm)
**Companion node stack:** `pymc_core` (+`[hardware,gpiod]`) and the `meshcore` client lib, in dedicated venv `/home/sdg/pymc/venv`. Scripts in `/home/sdg/pymc/`: `bringup.py` (chip init), `sendrecv.py` (TX+RX), `rx_listen.py` (raw sniffer), `chan_monitor.py` (**channel-aware monitor = the MVP-A prototype**), `gps_check.py`.
**Radio config (MeshAdv HAT / Ebyte E22-900M30S):** pyMC `waveshare` pins (cs21/rst18/busy20/irq16/txen13/rxen12) **PLUS `use_dio3_tcxo=True, dio3_tcxo_voltage=1.8`** — ⚠️CRITICAL: the E22 runs a 1.8 V TCXO; without it TX/RX silently fail ("no TxDone", IRQ 0x0000) even though `begin()` succeeds. US preset **910.525 MHz / 250 kHz / SF11 / CR5**; LoRa sync word **0x3444** (matches MeshCore). GPIO via gpiod on `gpiochip0`; `sdg` already in spi/gpio/i2c groups (no sudo for radio).
**PROVEN end-to-end vs 2× Heltec T096:** TX ✅ (hub "jTAK-Hub" shows in Android MeshCore Contacts) · RX ✅ (heard Heltec text msgs) · channel decrypt ✅ (loaded "jTAK Private" channel — *key lives in config/secrets, NOT committed* — decoded `jTAK Eve: "Sent from Eve"`, RSSI −18/SNR 6.75) · advert decode ✅ (`NODE_DISCOVERED` → name+lat/lon; Heltec still 0,0 = node-side GPS cold-start, not our issue).
**Ingest API (wire `meshcore_monitor.run()` to this):** `Dispatcher(radio).register_default_handlers(local_identity, channel_db=ChannelStore, event_service=EventService, node_name, radio_config)`; subscribe `EventService` → `MeshEvents.NODE_DISCOVERED` (adverts → name/`lat`/`lon` → `write_position`) + `NEW_CHANNEL_MESSAGE` (decrypted text → `mesh_messages`); `data['network_info']['rssi'/'snr']` → `rf_metrics`. Channel: `ChannelStore().set(0, Channel(name, secret=bytes.fromhex(<key>)))`.
**Hub GPS:** MeshAdv ATGM336H on UART `/dev/serial0` (`ttyS0`, 9600) — **HAS A LOCK** (9 sats, valid RMC). Freed the UART for GPS: removed `console=serial0,115200` from `/boot/firmware/cmdline.txt` (backup `cmdline.txt.bak-*`), disabled `serial-getty@ttyS0`, **rebooted to apply**. After reboot `ttyS0` should be group `dialout` (no sudo). Gives the hub its own live position for its node advert + map home.
**SESSION CLOSE (2026-07-05 night) — position path PROVEN with real data:**
- `hub_advert.py` (persistent identity in `hub_identity.seed`) reads the hub's own GPS and floods a `jTAK-Hub` advert **with location** (`create_flood_advert` auto-sets `HAS_LOCATION` when lat/lon≠0) → **confirmed visible on the Heltec maps**. It re-adverts every 45 s and discovers other nodes.
- **Received a real remote position:** `jTAK Eve` advert → `lat=40.572886 lon=-111.994051` decoded on the hub. So node positions ride in **adverts** once the node has a **sky-view GPS fix** (the earlier 0,0 was cold-start indoors, not our bug). Telemetry-request path exists as a fallback (`PacketBuilder.create_telem_request`, `REQ_TYPE_GET_TELEMETRY_DATA=0x03`) but isn't needed for position.
- Scripts in `/home/sdg/pymc/`: `hub_advert.py` (advert+GPS+discovery), `chan_monitor.py` (channel-aware monitor), `gps_check.py`, `sendrecv.py`, `bringup.py`, `rx_listen.py`. Hub GPS reads no-sudo on `/dev/serial0`.

**⚠️ PENDING DECISION before wiring (Sean to pick):** how jTAK gets the data — the radio is single-owner.
- **A) Embed `pymc_core` directly in `meshcore_monitor.py`** (the `chan_monitor.py`/`hub_advert.py` pattern — proven tonight). Fastest to a live map; jTAK owns the radio. Refactor needed later to share the radio with a room server / phone apps.
- **B) Standalone `CompanionFrameServer` service** (owns radio, TCP) + jTAK connects via the `meshcore` client lib. More setup, frame-server path unproven — but future-proof for the room-server + phone-app goals. **Recommended.**

**RESUME HERE (fresh session on mcore1, 2026-07-06):** (1) sanity: `/dev/spidev0.0`, `halow0`, GPS via `gps_check.py`, and `cd /home/sdg/pymc && ./venv/bin/python hub_advert.py` still discovers `jTAK Eve` w/ location. (2) Get Sean's A/B pick. (3) Implement chosen model → wire `ingest/meshcore_monitor.py run()`: adverts→`write_position(name, lat, lon)`, channel msgs→`mesh_messages`, `network_info.rssi/snr`→`rf_metrics`; feed the hub's own GPS as its node position. (4) `jtak-meshcore.service` (mirror `tactical_monitor.service`). (5) strip the dormant Meshtastic path. Positions/messages/RF decode is all proven — this is integration, not R&D.

---
*Master planning memory lives on the `stage` Pi at `~/.claude/projects/-home-sdg/memory/` (files `project_jtak_hub_roadmap.md`, `changelog_jtak_hub.md`). That memory does NOT travel between machines — this file is the portable handoff. Keep it updated as the build progresses.*
