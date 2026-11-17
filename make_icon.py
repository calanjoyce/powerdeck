#!/usr/bin/env python3
"""Draw a launcher icon for PowerDeck: two toggle switches and a bolt.

Usage: make_icon.py OUT.png [SIZE]   (90 for the launcher, 256 for the store)
"""
import sys
from PIL import Image, ImageDraw

out = sys.argv[1] if len(sys.argv) > 1 else "powerdeck.png"
size = int(sys.argv[2]) if len(sys.argv) > 2 else 90
S = 4
W = 90 * S
im = Image.new("RGBA", (W, W), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
light = (214, 226, 250, 255)
green = (52, 211, 120, 255)
grey = (110, 118, 134, 255)
knob = (255, 255, 255, 255)


def toggle(y, on):
    d.rounded_rectangle((12 * S, y * S, 58 * S, (y + 22) * S),
                        radius=11 * S, fill=green if on else grey)
    cx = 47 if on else 23
    d.ellipse(((cx - 8) * S, (y + 3) * S, (cx + 8) * S, (y + 19) * S),
              fill=knob)


toggle(20, True)
toggle(50, False)
bolt = [(74, 14), (60, 44), (70, 44), (62, 76), (82, 38), (72, 38), (80, 14)]
d.polygon([(x * S, y * S) for x, y in bolt], fill=(245, 177, 52, 255),
          outline=(30, 34, 42, 255), width=2 * S)
im.resize((size, size), Image.LANCZOS).save(out)
