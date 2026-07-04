# FrequencyLabs MeshAdv Hat Swap — Hub Migration Cheat Sheet
# Waveshare SX1262 → FrequencyLabs MeshAdv Pi Hat (E22-900M30S)
#
# Reference hub: TAK-5 (completed, verified)
# Completed:     TAK-4 (2026-05-28)
# Pending:       TAK-2, TAK-3
#
# Physical pre-req: Hat seated on GPIO header, ATGM336H ceramic GPS antenna
# connected to IPEX connector, ADS-B SDR and Flamingo filter still in line.

---

## 1. meshtasticd config.d

Remove old Waveshare config, remove BME sensor config (hardware removed),
activate the MeshAdv LoRa config from the native available.d.

```bash
sudo rm /etc/meshtasticd/config.d/lora-waveshare-sxxx.yaml
sudo rm /etc/meshtasticd/config.d/i2c-bme280.yaml          # sensor removed from hub

sudo cp /etc/meshtasticd/available.d/lora-MeshAdv-900M30S.yaml \
        /etc/meshtasticd/config.d/
sudo chmod 644 /etc/meshtasticd/config.d/lora-MeshAdv-900M30S.yaml

ls /etc/meshtasticd/config.d/
# Expected: gps-atgm336h.yaml (if present) + lora-MeshAdv-900M30S.yaml
# GPS section in main /etc/meshtasticd/config.yaml already sets SerialPath: /dev/gps0
# Do NOT copy gps-atgm336h.yaml from another hub — it causes "bad file" exception
# on fresh installs (not in available.d). Main config.yaml handles GPS already.
```

Resulting config.d should contain ONLY:
- `lora-MeshAdv-900M30S.yaml`

---

## 2. /boot/firmware/config.txt

Changes needed:
- REMOVE: `dtoverlay=uart3`           (Waveshare GPS was on UART3 / ttyAMA3)
- ADD:    `dtoverlay=spi0-0cs`        (MeshAdv hat needs SPI0 without kernel CS mgmt)
- KEEP:   `enable_uart=1`             (primary UART for ATGM336H GPS on ttyS0)
- KEEP:   `dtoverlay=pps-gpio,gpiopin=23`
- KEEP:   `dtparam=audio=off`         (required — frees PWM hardware for LED)

```bash
# Edit /boot/firmware/config.txt:
# Under the global section (before any [cm4]/[cm5] blocks):
#   dtparam=spi=on
#   dtoverlay=spi0-0cs      <-- ADD THIS LINE after dtparam=spi=on
#
# Under [cm4] block:
#   REMOVE: dtoverlay=uart3
#   KEEP:   dtoverlay=pps-gpio,gpiopin=23

sudo nano /boot/firmware/config.txt
```

After editing, the [cm4] section should look like:
```
[cm4]
otg_mode=1
dtoverlay=pps-gpio,gpiopin=23
```
(no uart3 line)

---

## 3. gpsd device

The ATGM336H GPS on the MeshAdv hat connects to the primary UART (GPIO14/15).
On Pi/CM4 this is /dev/ttyS0 (not ttyAMA3 which was for Waveshare UART3).

```bash
sudo nano /etc/default/gpsd
# Change:  DEVICES="/dev/ttyAMA3 /dev/pps0"
# To:      DEVICES="/dev/ttyS0 /dev/pps0"
```

---

## 4. LED pin and channel

The MeshAdv hat uses GPIO12 as RXen and GPIO13 as TXen.
TAK-1 and TAK-5 standard: LED strip on GPIO19 (PWM1).

IMPORTANT: GPIO19 is PWM1 — requires LED_CHANNEL = 1 in rpi_ws281x.
           GPIO12 (old/TAK-2 current) is PWM0 — uses LED_CHANNEL = 0.
           Forgetting to change LED_CHANNEL causes segfault / "GPIO not possible" error.

```bash
sudo nano /opt/jtak/led/led_effects.py
# Change:  LED_PIN     = 12   →   LED_PIN     = 19
# Change:  LED_CHANNEL = 0    →   LED_CHANNEL = 1
# KEEP:    DMA_CHANNEL = 10   (same for both)
```

---

## 5. Reboot

Config.txt changes require a full reboot.

```bash
sudo reboot
```

---

## 6. Post-reboot verification

```bash
# All services should be active:
systemctl is-active meshtasticd gpsd gpsnmea-pty tactical_monitor jtak-api jtak-led jtak-push

# meshtasticd TCP port up:
ss -tlnp | grep 4403

# meshtasticd found the radio (look for GPIO inits, no "bad file" or "USB device" errors):
journalctl -u meshtasticd -n 30 --no-pager | grep -E 'GPIO|sx1262|LoRa|Error|bad file'
# Expected lines:
#   Initializing GPIO21 on chip gpiochip0   (CS)
#   Initializing GPIO16 on chip gpiochip0   (IRQ)
#   Initializing GPIO20 on chip gpiochip0   (Busy)
#   Initializing GPIO18 on chip gpiochip0   (Reset)
#   Initializing GPIO13 on chip gpiochip0   (TXen)
#   Initializing GPIO12 on chip gpiochip0   (RXen)

# GPS — will be null until outdoor fix (cold start ~60-90s):
cat /opt/jtak/data/jtak-gps.json

# LED running:
journalctl -u jtak-led -n 5 --no-pager
# Expected: [LED] Daemon started on /run/jtak-led.sock

# Mesh traffic visible (after ~1-2 min):
journalctl -u tactical_monitor -n 20 --no-pager | grep -E 'DIRECT|RELAY'
```

---

## 7. Known gotchas

| Issue | Symptom | Fix |
|-------|---------|-----|
| Config file permissions | meshtasticd: "*** Exception bad file" | `sudo chmod 644 /etc/meshtasticd/config.d/*.yaml` |
| Copied gps-atgm336h.yaml from another hub | Same "bad file" exception | Remove it — GPS is in main config.yaml already |
| LED_CHANNEL not updated | "Selected GPIO not possible" segfault | Change LED_CHANNEL = 0 → 1 alongside LED_PIN change |
| uart3 overlay still in config.txt | GPS on ttyAMA3 (wrong port, no fix) | Remove dtoverlay=uart3 from [cm4] section |
| dtparam=audio=off missing | LED PWM hardware unavailable | Add to config.txt, reboot |
| GPS cold start indoors | jtak-gps.json all null | Normal — move antenna outdoors, wait 60-90s |

---

## TAK-2 specific notes (this machine)

TAK-2 currently runs:
- Waveshare SX1262 board → will be replaced with MeshAdv hat
- GPS on /dev/ttyAMA0 (check before swap: `cat /etc/default/gpsd`)
- LED_PIN = 12, LED_CHANNEL = 0 → change to 19 / 1
- meshtasticd config.d: check contents before removing

```bash
ls /etc/meshtasticd/config.d/
cat /etc/default/gpsd
grep 'LED_PIN\|LED_CHANNEL' /opt/jtak/led/led_effects.py
```

Restart jtak-api after swap (it serves the frontend that reads GPS state):
```bash
sudo systemctl restart jtak-api.service
```

---

## TAK-3 specific notes

TAK-3 GPS was on /dev/ttyS0 (Waveshare SX1262 combo board GNSS).
After swap, /dev/ttyS0 stays the same — just the LoRa radio config changes.
Verify gpsd DEVICES is already /dev/ttyS0 before assuming it needs changing.

```bash
ssh tak3 "cat /etc/default/gpsd && ls /etc/meshtasticd/config.d/"
```
