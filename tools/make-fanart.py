"""Generate resources/fanart.jpg — 1920x1080, AudioBookShelf palette.

Run: python3 tools/make-fanart.py   (needs Pillow; dev-only, not shipped)

Palette read out of advplyr/audiobookshelf images/banner.svg: gold #CD9D49,
dark gold #875D27, greys #474747 / #C9C9C9. Deliberately carries none of the
upstream wordmark: that repo is GPL-3.0 while this add-on is MIT, and putting
someone else's mark on a third-party client invites exactly the name confusion
this add-on is being renamed to avoid.

Deterministic — the seed is fixed, so re-running reproduces the same image.
"""

import pathlib
from PIL import Image, ImageDraw, ImageFilter
import math

W, H = 1920, 1080
GOLD = (205, 157, 73)
GOLD_D = (135, 93, 39)
BG_TOP = (35, 35, 35)
BG_BOT = (18, 18, 18)

img = Image.new("RGB", (W, H), BG_BOT)
d = ImageDraw.Draw(img)

# vertical gradient
for y in range(H):
    t = y / H
    c = tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3))
    d.line([(0, y), (W, y)], fill=c)

# warm radial glow, lower-left (keeps the right side clear for skin text)
glow = Image.new("L", (W, H), 0)
gd = ImageDraw.Draw(glow)
cx, cy = int(W * 0.28), int(H * 0.62)
for r in range(700, 0, -14):
    gd.ellipse(
        [cx - r, cy - int(r * 0.72), cx + r, cy + int(r * 0.72)],
        fill=int(70 * (1 - r / 700) ** 1.6),
    )
glow = glow.filter(ImageFilter.GaussianBlur(90))
img = Image.composite(Image.new("RGB", (W, H), GOLD_D), img, glow)

d = ImageDraw.Draw(img, "RGBA")

# "shelf" of book spines along the bottom third, varying heights/widths
x = -40
row_y = int(H * 0.98)
import random

random.seed(7)
while x < W + 40:
    w = random.randint(26, 70)
    h = random.randint(150, 420)
    lean = random.random() < 0.08
    a = random.randint(18, 46)
    col = GOLD if random.random() < 0.35 else (200, 200, 200)
    box = [x, row_y - h, x + w, row_y]
    d.rectangle(box, fill=col + (a,))
    # spine highlight
    d.rectangle([x, row_y - h, x + 3, row_y], fill=col + (min(255, a + 34),))
    x += w + random.randint(6, 16)

# horizon rule
d.line([(0, row_y), (W, row_y)], fill=GOLD + (70,), width=3)

# audio waveform sweeping across the upper half
mid = int(H * 0.34)
pts = []
for i in range(0, W + 1, 4):
    t = i / W
    amp = (math.sin(t * math.pi) ** 1.4) * 120
    v = (
        math.sin(t * 26) * 0.55
        + math.sin(t * 11 + 1.3) * 0.35
        + math.sin(t * 47) * 0.10
    )
    pts.append((i, mid + v * amp))
for width, alpha in ((7, 26), (4, 55), (2, 120)):
    d.line(pts, fill=GOLD + (alpha,), width=width, joint="curve")

out = pathlib.Path(__file__).resolve().parent.parent / "resources" / "fanart.jpg"
img.filter(ImageFilter.SMOOTH).save(out, quality=90)
print("wrote", out, img.size)
