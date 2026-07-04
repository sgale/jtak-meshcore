#!/usr/bin/env python3
"""
WS2812B 7-LED Ring Effects
Usage: sudo python3 ring_effects.py <color> <action> [speed]
       sudo python3 ring_effects.py <preset>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COLOR + ACTION mode:
  sudo python3 ring_effects.py blue blink fast
  sudo python3 ring_effects.py red fade slow
  sudo python3 ring_effects.py orange spin medium
  sudo python3 ring_effects.py white strobe fast
  sudo python3 ring_effects.py purple solid

  Colors:   red orange yellow green cyan blue purple
            pink white warm_white magenta

  Actions:  blink   — whole ring flashes on/off
            fade    — ring breathes in and out
            spin    — single dot chases the ring
            wipe    — fills LEDs one by one, then clears
            strobe  — rapid sharp flash
            solid   — ring stays on (no speed needed)

  Speed:    slow | medium | fast   (default: medium)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRESET mode (no color needed):
  sudo python3 ring_effects.py rainbow
  sudo python3 ring_effects.py fire
  sudo python3 ring_effects.py disco
  sudo python3 ring_effects.py police        — red/blue
  sudo python3 ring_effects.py construction  — white/orange strobe
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import time, random, math, sys, colorsys
from rpi_ws281x import PixelStrip, Color

# ── hardware ──────────────────────────────────────────────────────────────────
NUM_LEDS    = 7
LED_PIN     = 19
LED_FREQ    = 800_000
DMA_CHANNEL = 10
LED_INVERT  = False
BRIGHTNESS  = 15 # 1-100
LED_CHANNEL = 1

strip = PixelStrip(NUM_LEDS, LED_PIN, LED_FREQ, DMA_CHANNEL,
                   LED_INVERT, BRIGHTNESS, LED_CHANNEL)
strip.begin()

# ── color table ───────────────────────────────────────────────────────────────
COLORS = {
    "red":        (255,   0,   0),
    "orange":     (255,  80,   0),
    "yellow":     (255, 200,   0),
    "green":      (  0, 220,   0),
    "cyan":       (  0, 220, 220),
    "blue":       (  0,  60, 255),
    "purple":     (140,   0, 255),
    "pink":       (255,   0, 140),
    "magenta":    (255,   0, 200),
    "white":      (255, 255, 255),
    "warm_white": (255, 160,  60),
    "amber":      (230, 168,  23),
}

# ── speed table ───────────────────────────────────────────────────────────────
#  Each action reads the speeds it needs from here
SPEEDS = {
    #              slow    medium   fast
    "blink":   (  0.60,   0.30,   0.10 ),
    "fade":    (  0.04,   0.02,   0.008),
    "spin":    (  0.18,   0.09,   0.04 ),
    "wipe":    (  0.22,   0.10,   0.04 ),
    "strobe":  (  0.12,   0.07,   0.03 ),
}

# ── low-level helpers ─────────────────────────────────────────────────────────

def show():
    strip.show()

def off():
    for i in range(NUM_LEDS):
        strip.setPixelColor(i, Color(0, 0, 0))
    show()

def fill(r, g, b):
    for i in range(NUM_LEDS):
        strip.setPixelColor(i, Color(r, g, b))
    show()

def dim_color(r, g, b, factor):
    return Color(int(r * factor), int(g * factor), int(b * factor))

def wheel(pos):
    pos = pos % 256
    if pos < 85:
        return Color(255 - pos * 3, pos * 3, 0)
    if pos < 170:
        pos -= 85
        return Color(0, 255 - pos * 3, pos * 3)
    pos -= 170
    return Color(pos * 3, 0, 255 - pos * 3)

# ── color + action effects ────────────────────────────────────────────────────

def do_blink(r, g, b, speed="medium"):
    delay = SPEEDS["blink"][["slow","medium","fast"].index(speed)]
    print(f"  Blink  rgb=({r},{g},{b})  speed={speed}  |  Ctrl-C to stop")
    while True:
        fill(r, g, b)
        time.sleep(delay)
        off()
        time.sleep(delay)


def do_fade(r, g, b, speed="medium"):
    delay = SPEEDS["fade"][["slow","medium","fast"].index(speed)]
    print(f"  Fade   rgb=({r},{g},{b})  speed={speed}  |  Ctrl-C to stop")
    step = 0
    while True:
        factor = (math.sin(step * math.pi / 100) + 1) / 2
        c = dim_color(r, g, b, factor)
        for i in range(NUM_LEDS):
            strip.setPixelColor(i, c)
        show()
        time.sleep(delay)
        step = (step + 1) % 200


def do_spin(r, g, b, speed="medium"):
    delay = SPEEDS["spin"][["slow","medium","fast"].index(speed)]
    print(f"  Spin   rgb=({r},{g},{b})  speed={speed}  |  Ctrl-C to stop")
    color = Color(r, g, b)
    i = 0
    while True:
        for j in range(NUM_LEDS):
            strip.setPixelColor(j, Color(0, 0, 0))
        strip.setPixelColor(i % NUM_LEDS, color)
        show()
        time.sleep(delay)
        i += 1


def do_wipe(r, g, b, speed="medium"):
    delay = SPEEDS["wipe"][["slow","medium","fast"].index(speed)]
    print(f"  Wipe   rgb=({r},{g},{b})  speed={speed}  |  Ctrl-C to stop")
    while True:
        for i in range(NUM_LEDS):
            strip.setPixelColor(i, Color(r, g, b))
            show()
            time.sleep(delay)
        time.sleep(delay * 2)
        for i in range(NUM_LEDS):
            strip.setPixelColor(i, Color(0, 0, 0))
            show()
            time.sleep(delay)
        time.sleep(delay)


def do_strobe(r, g, b, speed="medium"):
    delay = SPEEDS["strobe"][["slow","medium","fast"].index(speed)]
    print(f"  Strobe rgb=({r},{g},{b})  speed={speed}  |  Ctrl-C to stop")
    while True:
        fill(r, g, b)
        time.sleep(delay * 0.3)   # short bright flash
        off()
        time.sleep(delay * 0.7)   # longer dark gap


def do_solid(r, g, b, speed=None):
    print(f"  Solid  rgb=({r},{g},{b})  |  Ctrl-C to stop")
    fill(r, g, b)
    while True:
        time.sleep(1)

# ── presets ───────────────────────────────────────────────────────────────────

def preset_rainbow(delay=0.04):
    print(f"  Rainbow  |  Ctrl-C to stop")
    offset = 0
    while True:
        for i in range(NUM_LEDS):
            strip.setPixelColor(i, wheel((i * 256 // NUM_LEDS + offset) % 256))
        show()
        time.sleep(delay)
        offset = (offset + 1) % 256


def preset_fire():
    print(f"  Fire  |  Ctrl-C to stop")
    while True:
        for i in range(NUM_LEDS):
            flicker = random.randint(100, 255)
            green   = int(flicker * random.uniform(0.15, 0.35))
            strip.setPixelColor(i, Color(flicker, green, 0))
        show()
        time.sleep(0.05)


def preset_disco():
    print(f"  Disco  |  Ctrl-C to stop")
    while True:
        for i in range(NUM_LEDS):
            h = random.random()
            rv, gv, bv = colorsys.hsv_to_rgb(h, 1.0, 1.0)
            strip.setPixelColor(i, Color(int(rv*255), int(gv*255), int(bv*255)))
        show()
        time.sleep(0.12)


def preset_police(blink_count=3, pause=0.06):
    """Red / blue emergency flash."""
    print(f"  Police (red/blue)  |  Ctrl-C to stop")
    red  = Color(200, 0,   0)
    blue = Color(0,   0, 200)
    while True:
        for _ in range(blink_count):
            fill(200, 0, 0); time.sleep(pause)
            off();           time.sleep(pause)
        time.sleep(0.05)
        for _ in range(blink_count):
            fill(0, 0, 200); time.sleep(pause)
            off();           time.sleep(pause)
        time.sleep(0.05)


def preset_construction():
    """White / orange alternating strobe — roadwork / construction style."""
    print(f"  Construction strobe (white/orange)  |  Ctrl-C to stop")
    # Pattern: 3 rapid white flashes, pause, 3 rapid orange flashes, pause
    flash_on  = 0.04
    flash_off = 0.06
    burst_gap = 0.18
    while True:
        # White burst
        for _ in range(3):
            fill(255, 255, 255); time.sleep(flash_on)
            off();               time.sleep(flash_off)
        time.sleep(burst_gap)
        # Orange burst
        for _ in range(3):
            fill(255, 90, 0);   time.sleep(flash_on)
            off();              time.sleep(flash_off)
        time.sleep(burst_gap)


# ── CLI routing ───────────────────────────────────────────────────────────────

ACTION_FN = {
    "blink":  do_blink,
    "fade":   do_fade,
    "spin":   do_spin,
    "wipe":   do_wipe,
    "strobe": do_strobe,
    "solid":  do_solid,
}

PRESET_FN = {
    "rainbow":      preset_rainbow,
    "fire":         preset_fire,
    "disco":        preset_disco,
    "police":       preset_police,
    "construction": preset_construction,
}

def print_usage():
    print(__doc__)

def main():
    args = [a.lower() for a in sys.argv[1:]]

    if not args:
        print_usage()
        sys.exit(1)

    # ── preset mode: single keyword ───────────────────────────────────────────
    if args[0] in PRESET_FN:
        print(f"\n✦ Preset: {args[0]}  (GPIO12 / PWM0)")
        PRESET_FN[args[0]]()
        return

    # ── color + action mode ───────────────────────────────────────────────────
    if len(args) < 2:
        print(f"Error: need at least a color and an action.\n")
        print_usage()
        sys.exit(1)

    color_name  = args[0]
    action_name = args[1]
    speed       = args[2] if len(args) > 2 else "medium"

    if color_name not in COLORS:
        print(f"Unknown color '{color_name}'. Choose from: {', '.join(COLORS)}")
        sys.exit(1)

    if action_name not in ACTION_FN:
        print(f"Unknown action '{action_name}'. Choose from: {', '.join(ACTION_FN)}")
        sys.exit(1)

    if speed not in ("slow", "medium", "fast"):
        print(f"Unknown speed '{speed}'. Use: slow  medium  fast")
        sys.exit(1)

    r, g, b = COLORS[color_name]
    print(f"\n✦  {color_name} {action_name} {speed}  (GPIO12 / PWM0)")

    ACTION_FN[action_name](r, g, b, speed)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        off()
        print("\nStopped. LEDs off.")
