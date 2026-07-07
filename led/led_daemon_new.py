#!/usr/bin/env python3
"""
jTAK LED Daemon
Runs as root. Owns GPIO12/WS2812B ring.
Listens on Unix socket /run/jtak-led.sock for JSON event commands.

Command format:
  {"event": "lora_message"}          — transient, 2s
  {"event": "new_node"}              — transient, 8s
  {"state": "gps_locked"}            — persistent state change
  {"state": "gps_fair"}
  {"state": "gps_poor"}
  {"state": "no_gps"}
  {"state": "fire_nearby"}
  {"state": "overtemp"}
  {"state": "low_disk"}
  {"state": "no_internet"}
  {"state": "beacon"}
"""

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/jtak/led")
from led_effects import (
    strip, off,
    do_blink, do_fade, do_spin, do_strobe, do_solid,
    preset_rainbow, preset_fire,
    COLORS,
)

SOCKET_PATH = "/run/jtak-led.sock"

# ── Priority table (lower number = higher priority) ───────────────────────────
STATE_PRIORITY = {
    "overtemp":   1,   # fire preset             — CPU too hot
    "low_disk":   2,   # yellow blink slow       — disk nearly full
    "no_internet":3,   # red fade slow           — no internet
    "fire_nearby":      4,   # red spin fast           — WFIGS fire within radius
    "lightning_nearby": 4,   # white blink fast        — lightning within 25 mi
    "beacon":     5,   # orange/white strobe     — manual beacon mode
    "gps_locked": 7,   # LED6 blue blink slow    — GPS good (HDOP < 2)
    "gps_fair":   7,   # LED6 amber blink medium — GPS fair (HDOP 2–5)
    "gps_poor":   7,   # LED6 orange blink fast  — GPS poor (HDOP > 5)
    "no_gps":     8,   # outer ring orange spin  — waiting for GPS fix
}

STATE_EFFECTS = {
    "overtemp":   ("preset", "fire"),
    "low_disk":   ("action", "yellow", "blink",  "slow"),
    "no_internet":("action", "red",    "fade",   "slow"),
    "fire_nearby":      ("action", "red",   "spin",   "fast"),
    "lightning_nearby": ("action", "white", "blink",  "fast"),
    "beacon":     ("preset", "construction"),
    "gps_locked": ("led6",   "blue",   "blink",  "slow"),
    "gps_fair":   ("led6",   "amber",  "blink",  "medium"),
    "gps_poor":   ("led6",   "orange", "blink",  "fast"),
    "no_gps":     ("action", "orange", "spin",   "medium"),
}

TRANSIENT_EFFECTS = {
    "lora_message":    {"ttl": 2, "effect": ("action", "white", "strobe", "fast")},
    "new_node":        {"ttl": 8, "effect": ("preset", "rainbow")},
    "channel_message": {"ttl": 3, "effect": ("action", "cyan",  "strobe", "fast")},
    "direct_message":  {"ttl": 3, "effect": ("action", "green", "strobe", "fast")},
}

# ── Effect runner ─────────────────────────────────────────────────────────────

def run_effect_step(effect, frame: int) -> float:
    kind = effect[0]

    if kind == "off":
        from rpi_ws281x import Color
        for i in range(7):
            strip.setPixelColor(i, Color(0, 0, 0))
        strip.show()
        return 1.0

    elif kind == "preset":
        name = effect[1]
        if name == "fire":
            import random
            from rpi_ws281x import Color
            for i in range(7):
                flicker = random.randint(100, 255)
                green   = int(flicker * random.uniform(0.15, 0.35))
                strip.setPixelColor(i, Color(flicker, green, 0))
            strip.show()
            return 0.05
        elif name == "rainbow":
            from led_effects import wheel
            for i in range(7):
                strip.setPixelColor(i, wheel((i * 256 // 7 + frame) % 256))
            strip.show()
            return 0.04
        elif name == "construction":
            from rpi_ws281x import Color
            phase = frame % 14
            if phase < 6:
                if phase % 2 == 0:
                    for i in range(7): strip.setPixelColor(i, Color(255, 255, 255))
                    strip.show(); return 0.04
                else:
                    for i in range(7): strip.setPixelColor(i, Color(0, 0, 0))
                    strip.show(); return 0.06
            elif phase == 6:
                for i in range(7): strip.setPixelColor(i, Color(0, 0, 0))
                strip.show(); return 0.18
            elif phase < 13:
                if phase % 2 == 1:
                    for i in range(7): strip.setPixelColor(i, Color(255, 90, 0))
                    strip.show(); return 0.04
                else:
                    for i in range(7): strip.setPixelColor(i, Color(0, 0, 0))
                    strip.show(); return 0.06
            else:
                for i in range(7): strip.setPixelColor(i, Color(0, 0, 0))
                strip.show(); return 0.18

    elif kind == "action":
        import math
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
            for i in range(7):
                strip.setPixelColor(i, Color(r, g, b) if phase == 0 else Color(0, 0, 0))
            strip.show()
            return delay
        elif action == "fade":
            delay = SPEEDS["fade"][si]
            factor = (math.sin(frame * math.pi / 100) + 1) / 2
            cr = int(r * factor); cg = int(g * factor); cb = int(b * factor)
            for i in range(7): strip.setPixelColor(i, Color(cr, cg, cb))
            strip.show()
            return delay
        elif action == "spin":
            delay = SPEEDS["spin"][si]
            idx = frame % 7
            for i in range(7): strip.setPixelColor(i, Color(0, 0, 0))
            strip.setPixelColor(idx, Color(r, g, b))
            strip.show()
            return delay
        elif action == "strobe":
            delay = SPEEDS["strobe"][si]
            phase = frame % 2
            if phase == 0:
                for i in range(7): strip.setPixelColor(i, Color(r, g, b))
                strip.show()
                return delay * 0.3
            else:
                for i in range(7): strip.setPixelColor(i, Color(0, 0, 0))
                strip.show()
                return delay * 0.7
        elif action == "solid":
            for i in range(7): strip.setPixelColor(i, Color(r, g, b))
            strip.show()
            return 1.0

    elif kind == "led6":
        import math
        from rpi_ws281x import Color
        color_name = effect[1]
        action     = effect[2]
        speed      = effect[3]
        r, g, b    = COLORS[color_name]
        SPEEDS = {
            "blink": (0.60, 0.30, 0.10),
            "fade":  (0.04, 0.02, 0.008),
        }
        si = ["slow", "medium", "fast"].index(speed) if speed else 1
        for i in range(6):
            strip.setPixelColor(i, Color(0, 0, 0))
        if action == "blink":
            delay = SPEEDS["blink"][si]
            phase = frame % 2
            strip.setPixelColor(6, Color(r, g, b) if phase == 0 else Color(0, 0, 0))
            strip.show()
            return delay
        elif action == "fade":
            delay = SPEEDS["fade"][si]
            factor = (math.sin(frame * math.pi / 100) + 1) / 2
            strip.setPixelColor(6, Color(int(r*factor), int(g*factor), int(b*factor)))
            strip.show()
            return delay

    return 0.05


# ── Daemon core ───────────────────────────────────────────────────────────────

class LEDDaemon:
    def __init__(self):
        self.active_states: set = {"no_gps"}
        self.transient: dict    = {}
        self._stop              = False
        self.disabled           = False

    def apply_state(self, state: str):
        if state in STATE_PRIORITY:
            self.active_states.add(state)
        # GPS states are mutually exclusive
        _gps_states = {"gps_locked", "gps_fair", "gps_poor", "no_gps"}
        if state in _gps_states:
            self.active_states -= (_gps_states - {state})

    def remove_state(self, state: str):
        self.active_states.discard(state)

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
                    print(f"[LED] transient: {ev} ({ttl}s)")
            elif "state" in msg:
                action = msg.get("action", "add")
                state  = msg["state"]
                if action == "remove":
                    self.remove_state(state)
                else:
                    self.apply_state(state)
                print(f"[LED] state {action}: {state} → active={self.active_states}")
            elif "brightness" in msg:
                pct = max(0, min(100, int(msg["brightness"])))
                raw = int(pct / 100 * 255)
                strip.setBrightness(raw)
                if pct == 0:
                    self.disabled = True
                    off()
                else:
                    self.disabled = False
                    strip.show()
                print(f"[LED] brightness: {pct}% → {raw}/255  disabled={self.disabled}")
        except Exception as e:
            print(f"[LED] client error: {e}")
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
            frame   = (frame + 1) % 10000
            await asyncio.sleep(sleep_t)

    async def run(self):
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        server = await asyncio.start_unix_server(self.handle_client, SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o666)
        print(f"[LED] Daemon started on {SOCKET_PATH}")
        async with server:
            await asyncio.gather(
                server.serve_forever(),
                self.effect_loop(),
            )


def main():
    daemon = LEDDaemon()

    def _shutdown(sig, frame):
        print("\n[LED] Shutting down.")
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
