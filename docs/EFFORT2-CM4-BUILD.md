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

## 9. Current status / next action (updated 2026-07-06)
> **▶ LATEST: Console messaging LIVE 2026-07-07 — see §9.4** (outbound send + reliable DM delivery, incl. the ⚠️ `pymc_core` ACK-length monkeypatch). MVP-A itself SHIPPED & DEPLOYED 2026-07-06 — see §9.3. The "NEXT ACTION #1 / Finish MVP-A" below and the §9.2 "RESUME HERE" are **DONE**.
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

**✅ ARCHITECTURE DECISION RESOLVED (2026-07-05 late) → "B-prime", proven live:** one process, one radio does BOTH. `CompanionRadio(radio, identity, node_name)` owns the SX1262 AND is a valid bridge for `CompanionFrameServer(bridge=companion, companion_hash=pubkey[:8], port=5000)`. Result (script `/home/sdg/pymc/companion_server.py`): TCP **:5000 LISTENING**, the **MeshCore Android app connected over TCP** (`192.168.86.52:5000`), contacts/config synced. jTAK ingests **in-process** (it's the node, not a TCP client) so it does NOT consume the frame server's **single client slot** (only one app at a time) — the phone gets that slot. **BLE not supported by pyMC (TCP only)**, though the CM4 has BT hardware.
- **Gotchas found (must handle in the real service):** (a) pyMC's convenience callbacks `on_advert_received` / `on_channel_message_received` **did NOT fire** even though the dispatcher parsed the adverts/msgs — **use the `EventService` (`NODE_DISCOVERED` / `NEW_CHANNEL_MESSAGE`) subscription instead** (proven in `chan_monitor.py`/`hub_advert.py`). (b) The hub companion node must have the **jTAK Private channel loaded** (`ChannelStore.set`) to decrypt channel msgs — the phone also pushes it via `CMD_SET_CHANNEL` (saw `cmd 0x20 len=50`), but load it server-side too. (c) `client_idle_timeout_sec=None` to avoid dropping the phone; the app's TCP session can still drop on backgrounding — expect reconnects. (d) **persistent-identity bug:** `hub_advert.py`/`companion_server.py` don't round-trip the identity (`get_private_key()`→`LocalIdentity(seed=)` yields a different pubkey) — fix so "jTAK-Hub" is a stable contact.
- **Naming/collision (Sean's requirement):** key jTAK rows on **`source_id = MeshCore pubkey` + `source_type='meshcore'`** (NOT display name) so MeshCore nodes never collide with the existing Meshtastic "jTAK Adam" etc. Renames are safe (pubkey stable). MeshCore nodes renamed to **MC Rachel** (`9263a332…`) / **MC Isaac** (`832f90c7…`).

**RESUME HERE (fresh session on mcore1, 2026-07-06):** (1) sanity: `/dev/spidev0.0`, `halow0`, GPS via `gps_check.py`; `cd /home/sdg/pymc && ./venv/bin/python companion_server.py` → :5000 listens, phone can reconnect. (2) Build the real **`jtak-meshcore` service** around `companion_server.py`: `CompanionRadio` + `CompanionFrameServer` (phone) + **`EventService` subscribers** → `write_position(pubkey, name, lat, lon)` / `mesh_messages` / `rf_metrics(rssi,snr)`; load the jTAK Private channel; feed the hub's own GPS as its node position; **key everything on pubkey+source_type**. (3) Fix the persistent-identity round-trip. (4) `jtak-meshcore.service` (mirror `tactical_monitor.service`). (5) strip the dormant Meshtastic path. All decode + the phone/TCP topology are proven — this is integration, not R&D.

## 9.3 MVP-A SHIPPED — full `jtak-meshcore` service LIVE & DEPLOYED (2026-07-06)
The stub is gone. `ingest/meshcore_monitor.py` is the real service (runs under `/home/sdg/pymc/venv`, `jtak-meshcore.service`, enabled). Architecture is the proven **B-prime** (one process/one radio: `CompanionRadio` owns the SX1262 + backs `CompanionFrameServer` :5000 for the phone; jTAK ingests **in-process** via `companion._event_service.subscribe_all`). Verified end-to-end on 2× Heltec T096 with real GPS locks. Everything keys on `source_id=pubkey` + `source_type='meshcore'`. Commits: `5796368` (CSV producer), `4535fe0` (telemetry/contacts/position policy), `f734c41` (GPS sats / self-marker / LED).

**Shipped features:**
- **Ingest → live dashboard:** adverts (`NODE_DISCOVERED`), channel msgs (`NEW_CHANNEL_MESSAGE`), DMs (`NEW_MESSAGE`) → the **RF-log CSV** → `csv_watcher` (inside jtak-api) owns positions/rf_metrics writes **+ the live WS broadcast + LED**. ⚠️ The live NODES / LAST-RF / map panels are **WS-driven, not DB-polled** — a producer MUST write the RF CSV, not just the DB.
- **Hub-driven telemetry poll** (`telemetry:` cfg, 5 min): `send_telemetry_request` per chat node → GPS + battery (volts→%) + temp/humidity (CayenneLPP). CSV cols `temp_c/humidity_pct/battery_pct`. **Position policy:** adverts (~0.1 m) drive the pin; coarse ~11 m telemetry GPS only overrides on stale advert (`position_stale_sec`) or move > `move_threshold_m` — so co-located nodes don't collapse onto one pin.
- **Persistent contact book:** `companion.contacts.to_dicts()` (nodes + `out_path` routing) → `contacts_file` JSON, reloaded on boot (debounced atomic saver). Survives restart — no re-advert; telemetry polls immediately.
- **Hub GPS on the header:** `read_gps` parses sats + HDOP from GGA; a 120 s poll refreshes hub position AND writes `/opt/jtak/data/meshcore-gps.json`; `routes_status` reads it → `/api/status hub_sats/hub_hdop/hub_position` (LED6 + header).
- **Hub = dashboard self-marker** (white/orange `marker-self` from `hub_position`), **NOT** a mesh node (writing it as a node duplicated + overlapped it). Renamed **jTAK-MCore1**: `meshcore.node_name` (mesh advert) + `hub.name/short_name` (dashboard).
- **LED ring LIVE:** `jtak-led.service` (root, `led_daemon_new.py`, WS2812 7-LED on GPIO19). Per-event: new_node 🌈 / channel cyan / DM green / RF white / GPS-lock blue (LED6); telemetry quiet. `rpi_ws281x` installed to system python3.

**Key operational learnings (non-obvious — keep):**
- **gpsd is DEAD on this hub** (`gpsd inactive`, `gpspipe` absent) — the Meshtastic-era GPS-status path never worked. **MeshCore owns the serial GPS (`/dev/serial0`)** directly and publishes to the status file `routes_status` now reads.
- **`csv_watcher` backfills the last 500 RF-CSV rows into the DB on every jtak-api restart** → **DB-only row deletions get UNDONE**. To durably remove bad rows, fix the CSV too (bit us with coarse telemetry positions).
- **RF-log CSV header must match the column set** — a stale header (file created before cols were added mid-day) silently drops extras into a DictReader `None` bucket (battery was being lost). Self-heals at next-day rollover; today's file was repaired.
- **LED on GPIO19 = PWM1**, onboard audio = PWM0 → **no conflict** (`dtparam=audio=on` is fine). Daemon runs as root for `/dev/mem` + DMA.
- SX126x `getPacketStatus` RSSI reads intermittently garbage (~0/−1) while SNR is valid — sanitized (blank if > −5 dBm).

**NEXT (Effort-2 remaining, no longer MVP-A):**
1. **HaLow two-node link + iperf at range** — THE open HaLow item (needs a 2nd node); then MVP-B/C (backhaul + Reticulum bridge LoRa↔HaLow).
2. Strip the dormant **Meshtastic** ingest path (`tactical_monitor`, `routes_meshtastic_debug`, the 86 KB MT config UI).
3. Waypoints strategy (decision (i)) + minimal MeshCore config page (decision (ii)) — post-MVP-A, still deferred.
4. Set real `admin.password` (still `CHANGEME` per §9 notes).

## 9.4 Console messaging: outbound send + reliable DM delivery (2026-07-07)
The dashboard's **Send Message** console was dead on MeshCore: `POST /mesh/send` only INSERTs into `mesh_send_queue`, which the retired Meshtastic `tactical_monitor` used to drain. Added a **`send_queue_drainer`** to the MeshCore service that transmits queued rows over the radio; channel rows (`to_id '^all'`) via `send_channel_message`, DMs (64-hex pubkey) via `send_text_message`, mirroring sent rows into `mesh_messages`. Commits: `5da257e` (drainer), `a3aab91` (real channel names from `jtak.yaml`, secret never leaked), `30cbdaf` (battery fix, see below), `e419ab8` (DM delivery: ACK interop + retries + failed/recipient UI).

**Shipped:**
- **Outbound send LIVE** — channel + DM from the console actually transmit; sent messages appear in the chat panel (`direction='tx'`).
- **`/mesh/channels` shows the real registry** — reads `meshcore.channels` from `jtak.yaml` (index+name only, **secret stays backend-side**) instead of the stale Meshtastic-era `mesh_channels.json` fallback ("Primary").
- **Battery no longer flaps** (`30cbdaf`) — `/api/nodes` + map pulled `battery_pct` via `SELECT battery_pct, MAX(timestamp) GROUP BY source_id`; SQLite's bare-column rule took battery from the latest row, but battery only rides `telemetry` packets (adverts/channel/DM = NULL), so any advert blanked it. Fixed with a `latest_batt` CTE (newest row `WHERE battery_pct IS NOT NULL`); also carried through the live WS + frontend merge.
- **DM retries** — DMs retry up to `meshcore.send.max_attempts` (default 3) with escalating backoff (`retry_backoff_sec * attempt`); channel/group sends stay single-shot flood (no ACK exists). New `meshcore.send` cfg block (`max_attempts`, `retry_backoff_sec`, `ack_timeout_sec`).
- **Failed / recipient UI** — every attempt recorded (sent OR failed) via new `mesh_messages.status` column (idempotent `ALTER` migration in `store/db.py`); failed sends render red + "NOT DELIVERED". Channel msgs store `to_id '^all'` and inbound DMs record the hub as recipient, so the old "→ ?" resolves to the channel name / hub name.

**⚠️ CRITICAL interop learning — the ACK-length monkeypatch (keep, re-verify on any `pymc_core` bump):**
- **Symptom:** DMs to a *powered-on, in-range* node delivered fine (peer showed the message ×3) but every send reported failed → all 3 retries fired.
- **Root cause:** the ACK **does** come back (~500 ms) with the correct CRC, but our Heltec T096s (with the **extended / 2-ACK radio setting**) send a **6-byte ACK** = 4-byte CRC + 2 trailing metadata bytes (e.g. `FEA6637F00A7`). `pymc_core`'s `AckHandler.process_discrete_ack` **hard-rejects any payload `!= 4` bytes** (`node/handlers/ack.py`), so a valid ACK was always dropped.
- **Fix (lives in OUR repo, not the vendored lib):** `_patch_pymc_ack_length()` in `meshcore_monitor.py` monkeypatches `AckHandler.process_discrete_ack` at startup to accept `>= 4` bytes and use the first 4 (LE) as the CRC. **If `pymc_core` is ever upgraded/reinstalled, re-verify this patch still applies** (grep the installed `ack.py` for the `!= 4` check).
- **Two more gotchas fixed alongside:** (a) `send_text_message(wait_for_ack=True)` waits on the *packet* CRC, but MeshCore ACKs carry a *content-derived* `ack_crc` — so we send with its wait OFF and wait on `res.expected_ack` via `dispatcher.wait_for_ack`. (b) Each send stamps a **fresh `ack_crc`** (hash of `timestamp + attempt`), so retries change the CRC we're listening for — `_wait_for_any_ack` waits on the accumulated set of *all* attempts' CRCs so a late ACK still counts.
- **Radio setting:** the node-side "2 ACKs / extended ACK" toggle can stay **ON** — the hub now handles 6-byte ACKs natively and the redundancy helps.

## 9.5 Cloning a hub to new hardware — NVME copy + personalize (2026-07-08)
Workflow mirrors the Meshtastic fleet: **clone the NVME to an identical one, then run one script** to make the copy unique. Script: **`scripts/clone-personalize.sh <hub-number>`** (run as root on the NEW hardware after the clone boots; derives `name=jTAK-MCore<N>`, `hostname=mcore<N>`, `hub_id=mcore<N>`, `guid=mcore000<N>`; flags `--name/--hostname/--hub-id/--guid/--keep-db/--yes`).

**⚠️ The MeshCore-specific catch (different from Meshtastic):** MeshCore's node identity is an **Ed25519 seed FILE on the disk** (`meshcore.seed_file` = `/home/sdg/pymc/hub_identity.seed`), NOT in radio firmware. A raw clone shares it → **both hubs advertise the SAME mesh public key = same node = advert/ACK/routing collisions.** `load_identity()` mints a fresh key only if the seed file is absent, so the script **deletes the seed** to force a new unique identity on first start.

**What the script changes (unique per hub):** deletes the mesh seed; rewrites `jtak.identity.yaml` (guid/hub_id); renames the hub in `jtak.yaml` (`hub.name`+`short_name`+`meshcore.node_name`, all 3 hold the same string, sed-replaced with a channel-secret-unchanged guard); sets hostname + `/etc/hosts`; regenerates SSH host keys + machine-id (network-distinct); wipes learned/runtime state (`hub_contacts.json`, `meshcore-gps.json`, `hq_watermarks.json`, and by default `jtak.db` + RF logs — `--keep-db` to retain). **Left SHARED (do NOT change):** the channel secret + radio config (one private mesh), `hq.url`. After running: re-advert the field radios (contact book wiped), `ssh-keygen -R` the old host key on clients, and reboot to settle hostname/machine-id.

---
*Master planning memory lives on the `stage` Pi at `~/.claude/projects/-home-sdg/memory/` (files `project_jtak_hub_roadmap.md`, `changelog_jtak_hub.md`). That memory does NOT travel between machines — this file is the portable handoff. Keep it updated as the build progresses.*
