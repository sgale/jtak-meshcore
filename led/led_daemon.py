#!/usr/bin/env python3
"""
jTAK LED Daemon
Runs as root. Owns GPIO12/WS2812B ring.
Listens on Unix socket /run/jtak-led.sock for JSON event commands.

LED architecture:
  LED 6 (center) = GPS status ALWAYS — never overwritten by other effects
  LEDs 0–5 (ring) = all other effects (transient, alerts, status)
"""

import asyncio
import json
import math
import os
import signal
import sys
import time

sys.path.insert(0, "/opt/jtak/led")
from led_effects import (
    strip, off,
    do_blink, do_fade, do_spin, do_strobe, do_solid,
    preset_rainbow, preset_fire,
    COLORS,
)

SOCKET_PATH = "/run/jtak-led.sock"

# GPS states — handled on LED 6 only, never enter STATE_PRIORITY
_GPS_STATES = {"gps_locked", "gps_fair", "gps_poor", "no_gps"}
_gps_led6_state = "no_gps"

# GPS LED6 config: (color, action, speed)
_GPS_LED6_EFFECTS = {
    "gps_locked": ("blue",   "blink", "slow"),
    "gps_fair":   ("amber",  "blink", "medium"),
    "gps_poor":   ("orange", "blink", "fast"),
    "no_gps":     ("orange", "fade",  "slow"),
}

# ── Priority table (lower number = higher priority) ───────────────────────────
STATE_PRIORITY = {
    "overtemp":         1,
    "low_disk":         2,
    "no_internet":      3,
    "fire_nearby":      4,
    "lightning_nearby": 4,
    "orange_spin":      4,
    "beacon":           5,
}

STATE_EFFECTS = {
    "overtemp":         ("preset", "fire"),
    "low_disk":         ("action", "yellow", "blink",  "slow"),
    "no_internet":      ("preset", "red_chase_flash"),
    "fire_nearby":      ("action", "red",    "spin",   "fast"),
    "lightning_nearby": ("action", "white",  "blink",  "fast"),
    "orange_spin":      ("action", "orange", "spin",   "fast"),
    "beacon":           ("preset", "construction"),
}

TRANSIENT_EFFECTS = {
    "lora_message": {"ttl": 1, "effect": ("action", "white", "strobe", "fast")},
    "new_node":     {"ttl": 8, "effect": ("preset", "rainbow")},
}

# ── GPS LED6 writer — uses wall-clock time, independent of frame rate ──────────

def _apply_gps_led6(suppress=False):
    """Write GPS status to LED 6. Uses time.time() so animation is independent of frame rate.
    Pass suppress=True (e.g. when beacon is active) to blank LED 6."""
    from rpi_ws281x import Color
    if suppress:
        strip.setPixelColor(6, Color(0, 0, 0))
        strip.show()
        return
    effect = _GPS_LED6_EFFECTS.get(_gps_led6_state, _GPS_LED6_EFFECTS["no_gps"])
    color_name, action, speed = effect
    r, g, b = COLORS[color_name]
    si = ["slow", "medium", "fast"].index(speed)
    now = time.time()

    if action == "blink":
        half = (0.6, 0.3, 0.1)[si]
        on = int(now / half) % 2 == 0
        strip.setPixelColor(6, Color(r, g, b) if on else Color(0, 0, 0))
    elif action == "fade":
        period = (4.0, 2.0, 1.0)[si]
        factor = (math.sin(2 * math.pi * now / period) + 1) / 2
        strip.setPixelColor(6, Color(int(r * factor), int(g * factor), int(b * factor)))
    else:
        strip.setPixelColor(6, Color(r, g, b))

    strip.show()


# ── Effect runner (LEDs 0–5 only) ─────────────────────────────────────────────

def run_effect_step(effect, frame: int) -> float:
    """Run one frame of an effect on LEDs 0–5. Never touches LED 6."""
    kind = effect[0]

    if kind == "off":
        from rpi_ws281x import Color
        for i in range(6):
            strip.setPixelColor(i, Color(0, 0, 0))
        strip.show()
        return 0.1   # short sleep so GPS LED6 updates smoothly

    elif kind == "preset":
        name = effect[1]
        if name == "fire":
            import random
            from rpi_ws281x import Color
            for i in range(6):
                flicker = random.randint(100, 255)
                green   = int(flicker * random.uniform(0.15, 0.35))
                strip.setPixelColor(i, Color(flicker, green, 0))
            strip.show()
            return 0.05
        elif name == "rainbow":
            from led_effects import wheel
            for i in range(6):
                strip.setPixelColor(i, wheel((i * 256 // 6 + frame) % 256))
            strip.show()
            return 0.04
        elif name == "construction":
            from rpi_ws281x import Color
            phase = frame % 14
            if phase < 6:
                if phase % 2 == 0:
                    for i in range(6): strip.setPixelColor(i, Color(255, 255, 255))
                    strip.show(); return 0.04
                else:
                    for i in range(6): strip.setPixelColor(i, Color(0, 0, 0))
                    strip.show(); return 0.06
            elif phase == 6:
                for i in range(6): strip.setPixelColor(i, Color(0, 0, 0))
                strip.show(); return 0.18
            elif phase < 13:
                if phase % 2 == 1:
                    for i in range(6): strip.setPixelColor(i, Color(255, 90, 0))
                    strip.show(); return 0.04
                else:
                    for i in range(6): strip.setPixelColor(i, Color(0, 0, 0))
                    strip.show(); return 0.06
            else:
                for i in range(6): strip.setPixelColor(i, Color(0, 0, 0))
                strip.show(); return 0.18

        elif name == "red_chase_flash":
            # Single red dot chases ring LEDs 0-5, then all-ring red flash, then dark pause
            # phases 0-5: chase (0.25s/step = 1.5s revolution)
            # phase 6: all ring red flash (0.20s)
            # phase 7: all ring off, pause (0.35s)
            from rpi_ws281x import Color
            phase = frame % 8
            for i in range(6): strip.setPixelColor(i, Color(0, 0, 0))
            if phase < 6:
                strip.setPixelColor(phase, Color(255, 0, 0))
                strip.show()
                return 0.25
            elif phase == 6:
                for i in range(6): strip.setPixelColor(i, Color(255, 0, 0))
                strip.show()
                return 0.20
            else:
                strip.show()
                return 0.35

    elif kind == "action":
        from rpi_ws281x import Color
        color_name = effect[1]
        action     = effect[2]
        speed      = effect[3]
        r, g, b    = COLORS[color_name]
        SPEEDS = {
            "blink":  (0.60, 0.30, 0.10),
            "fade":   (0.04, 0.02, 0.008),
            "spin":   (0.18, 0.09, 0.04),
            "strobe": (0.12, 0.07, 0.03),
            "solid":  (1.0,  1.0,  1.0),
        }
        si = ["slow", "medium", "fast"].index(speed) if speed else 1

        if action == "blink":
            delay = SPEEDS["blink"][si]
            phase = int(frame * delay * 2) % 2
            for i in range(6):
                strip.setPixelColor(i, Color(r, g, b) if phase == 0 else Color(0, 0, 0))
            strip.show()
            return delay
        elif action == "fade":
            delay = SPEEDS["fade"][si]
            factor = (math.sin(frame * math.pi / 100) + 1) / 2
            cr = int(r * factor); cg = int(g * factor); cb = int(b * factor)
            for i in range(6): strip.setPixelColor(i, Color(cr, cg, cb))
            strip.show()
            return delay
        elif action == "spin":
            delay = SPEEDS["spin"][si]
            idx = frame % 6
            for i in range(6): strip.setPixelColor(i, Color(0, 0, 0))
            strip.setPixelColor(idx, Color(r, g, b))
            strip.show()
            return delay
        elif action == "strobe":
            delay = SPEEDS["strobe"][si]
            phase = frame % 2
            if phase == 0:
                for i in range(6): strip.setPixelColor(i, Color(r, g, b))
                strip.show()
                return delay * 0.3
            else:
                for i in range(6): strip.setPixelColor(i, Color(0, 0, 0))
                strip.show()
                return delay * 0.7
        elif action == "solid":
            for i in range(6): strip.setPixelColor(i, Color(r, g, b))
            strip.show()
            return 1.0

    return 0.05


# ── Daemon core ───────────────────────────────────────────────────────────────

class LEDDaemon:
    def __init__(self):
        self.active_states: set = {"orange_spin"}  # boot default: spin until GPS acquired
        self.transient: dict    = {}
        self._stop              = False
        self.disabled           = False
        self._brightness_pct    = round(strip.getBrightness() / 255 * 100)

    def apply_state(self, state: str):
        global _gps_led6_state
        if state in _GPS_STATES:
            _gps_led6_state = state
            print(f"[LED] GPS LED6: {state}", flush=True)
            return
        if state in STATE_PRIORITY:
            self.active_states.add(state)
            print(f"[LED] state add: {state} → active={self.active_states}", flush=True)

    def remove_state(self, state: str):
        self.active_states.discard(state)
        print(f"[LED] state remove: {state} → active={self.active_states}", flush=True)

    def current_effect(self):
        now = time.time()
        for event, expiry in list(self.transient.items()):
            if now < expiry:
                return TRANSIENT_EFFECTS[event]["effect"]
            else:
                del self.transient[event]
        active = [s for s in self.active_states if s in STATE_PRIORITY]
        if not active:
            return ("off",)
        best = min(active, key=lambda s: STATE_PRIORITY[s])
        return STATE_EFFECTS.get(best, ("off",))

    async def handle_client(self, reader, writer):
        try:
            data = await asyncio.wait_for(reader.read(256), timeout=2.0)
            msg  = json.loads(data.decode())
            if "event" in msg:
                ev = msg["event"]
                if ev in TRANSIENT_EFFECTS:
                    ttl = TRANSIENT_EFFECTS[ev]["ttl"]
                    self.transient[ev] = time.time() + ttl
                    print(f"[LED] transient: {ev} ({ttl}s)", flush=True)
            elif "state" in msg:
                action = msg.get("action", "add")
                state  = msg["state"]
                if action == "remove":
                    self.remove_state(state)
                else:
                    self.apply_state(state)
            elif "brightness" in msg:
                pct = max(0, min(100, int(msg["brightness"])))
                raw = int(pct / 100 * 255)
                strip.setBrightness(raw)
                self._brightness_pct = pct
                if pct == 0:
                    self.disabled = True
                    off()
                else:
                    self.disabled = False
                    strip.show()
                print(f"[LED] brightness: {pct}% → {raw}/255", flush=True)
            elif msg.get("query") == "brightness":
                pct = getattr(self, "_brightness_pct", round(strip.getBrightness() / 255 * 100))
                writer.write(json.dumps({"brightness": pct}).encode())
                await writer.drain()
        except Exception as e:
            print(f"[LED] client error: {e}", flush=True)
        finally:
            writer.close()

    async def effect_loop(self):
        frame = 0
        while not self._stop:
            if self.disabled:
                await asyncio.sleep(0.5)
                continue
            effect  = self.current_effect()
            sleep_t = run_effect_step(effect, frame)
            _apply_gps_led6(suppress="beacon" in self.active_states)  # blank LED6 during beacon
            frame   = (frame + 1) % 10000
            await asyncio.sleep(sleep_t)

    async def run(self):
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        server = await asyncio.start_unix_server(self.handle_client, SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o666)
        print(f"[LED] Daemon started on {SOCKET_PATH}", flush=True)
        async with server:
            await asyncio.gather(
                server.serve_forever(),
                self.effect_loop(),
            )


def main():
    daemon = LEDDaemon()

    def _shutdown(sig, frame):
        print("\n[LED] Shutting down.", flush=True)
        daemon._stop = True
        off()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        off()


if __name__ == "__main__":
    main()
