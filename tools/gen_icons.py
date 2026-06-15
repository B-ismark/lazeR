#!/usr/bin/env python3
"""Generate LazeR brand icons: Android mipmap PNGs + a Windows .ico.

Mark: a glowing laser-pointer dot with a 4-ray crosshair on a periwinkle
rounded square — matches the in-app accent (#8C9BFF).
"""
import os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES  = os.path.join(ROOT, "android", "app", "src", "main", "res")
SRV  = os.path.join(ROOT, "server")

BG_TOP    = (140, 155, 255)   # #8C9BFF
BG_BOT    = (101, 117, 224)   # #6575E0
WHITE     = (255, 255, 255)
GLOW      = (255, 255, 255)


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def vgradient(size, top, bot):
    g = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        g.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    return g.resize((size, size))


def draw_mark(img, size):
    d = ImageDraw.Draw(img, "RGBA")
    c = size / 2
    # soft glow behind the dot
    for r, a in ((0.30, 40), (0.22, 70), (0.15, 120)):
        rr = size * r
        d.ellipse([c - rr, c - rr, c + rr, c + rr], fill=GLOW + (a,))
    # core dot
    dot = size * 0.11
    d.ellipse([c - dot, c - dot, c + dot, c + dot], fill=WHITE)
    # crosshair rays
    w = max(2, int(size * 0.035))
    inner = size * 0.20
    outer = size * 0.36
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        d.line([c + dx * inner, c + dy * inner,
                c + dx * outer, c + dy * outer], fill=WHITE, width=w)


def make_icon(size, round_icon=False):
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = vgradient(size, BG_TOP, BG_BOT).convert("RGBA")
    if round_icon:
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    else:
        mask = rounded_mask(size, int(size * 0.22))
    base.paste(grad, (0, 0), mask)
    draw_mark(base, size)
    base.putalpha(mask if round_icon else mask)
    # re-apply mark alpha so it isn't clipped by putalpha above
    return base


def build_icon(size, round_icon=False):
    grad = vgradient(size, BG_TOP, BG_BOT).convert("RGBA")
    if round_icon:
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    else:
        mask = rounded_mask(size, int(size * 0.22))
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(grad, (0, 0), mask)
    draw_mark(canvas, size)
    # clip everything (incl. glow) to the shape
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(canvas, (0, 0), mask)
    return out


# ── Android mipmaps ───────────────────────────────────────────────────────────
DENSITIES = {
    "mipmap-mdpi": 48, "mipmap-hdpi": 72, "mipmap-xhdpi": 96,
    "mipmap-xxhdpi": 144, "mipmap-xxxhdpi": 192,
}
for folder, px in DENSITIES.items():
    d = os.path.join(RES, folder)
    os.makedirs(d, exist_ok=True)
    build_icon(px, False).save(os.path.join(d, "ic_launcher.png"))
    build_icon(px, True).save(os.path.join(d, "ic_launcher_round.png"))
    print(f"  {folder}/ic_launcher.png ({px}px)")

# ── Windows .ico ────────────────────────────────────────────────────────────
ico = build_icon(256, False)
ico_path = os.path.join(SRV, "LazeR.ico")
ico.save(ico_path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64),
                          (128, 128), (256, 256)])
print(f"  {ico_path}")
print("done.")
