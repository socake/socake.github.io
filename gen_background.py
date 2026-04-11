#!/usr/bin/env python3
"""Generate elegant dark background for socake blog."""
from PIL import Image, ImageDraw, ImageFilter
import math, random

W, H = 1920, 1080

def make_background():
    # Base: deep midnight navy
    img = Image.new("RGB", (W, H), (8, 12, 30))
    draw = ImageDraw.Draw(img, "RGBA")

    # Diagonal gradient overlay: dark navy -> slightly lighter
    for y in range(H):
        for x in range(W):
            t = (x / W * 0.4 + y / H * 0.6)
            r = int(8  + (14 - 8)  * t)
            g = int(12 + (22 - 12) * t)
            b = int(30 + (55 - 30) * t)
            img.putpixel((x, y), (r, g, b))

    draw = ImageDraw.Draw(img, "RGBA")

    # Soft glowing orbs in background (like bokeh)
    orbs = [
        # (cx, cy, radius, color RGBA)
        (W * 0.15, H * 0.25, 380, (99,  102, 241, 18)),   # indigo top-left
        (W * 0.85, H * 0.15, 320, (34,  211, 238, 14)),   # cyan top-right
        (W * 0.70, H * 0.80, 420, (99,  102, 241, 12)),   # indigo bottom-right
        (W * 0.30, H * 0.75, 280, (139, 92,  246, 10)),   # violet bottom-left
        (W * 0.50, H * 0.45, 500, (14,  16,  42,  30)),   # center depth
    ]
    for (cx, cy, r, color) in orbs:
        # Draw radial gradient orb via concentric ellipses
        steps = 40
        for i in range(steps, 0, -1):
            ratio = i / steps
            alpha = int(color[3] * (1 - ratio) * 2.5)
            alpha = min(alpha, 50)
            cr = int(r * ratio)
            c = (color[0], color[1], color[2], alpha)
            draw.ellipse(
                [(cx - cr, cy - cr * 0.7), (cx + cr, cy + cr * 0.7)],
                fill=c
            )

    # Subtle dot grid
    grid_step = 48
    dot_r = 1
    for x in range(0, W, grid_step):
        for y in range(0, H, grid_step):
            # vary opacity slightly for depth
            dist_center = math.hypot(x - W/2, y - H/2) / math.hypot(W/2, H/2)
            alpha = int(max(8, 22 - dist_center * 16))
            draw.ellipse(
                [(x - dot_r, y - dot_r), (x + dot_r, y + dot_r)],
                fill=(148, 163, 210, alpha)
            )

    # Diagonal faint lines (circuit-board / grid feel)
    line_spacing = 120
    for offset in range(-H, W + H, line_spacing):
        draw.line(
            [(offset, 0), (offset + H, H)],
            fill=(99, 102, 241, 5), width=1
        )
    for offset in range(0, W + H * 2, line_spacing):
        draw.line(
            [(offset, 0), (offset - H, H)],
            fill=(34, 211, 238, 4), width=1
        )

    # Top accent bar: thin gradient
    for x in range(W):
        t = x / W
        r = int(99  + (34  - 99)  * t)
        g = int(102 + (211 - 102) * t)
        b = int(241 + (238 - 241) * t)
        alpha = int(80 + 40 * math.sin(t * math.pi))
        draw.line([(x, 0), (x, 2)], fill=(r, g, b, alpha))

    # Bottom accent bar: subtle
    for x in range(W):
        draw.line([(x, H - 1), (x, H)], fill=(99, 102, 241, 30))

    # Gentle blur for depth
    img = img.filter(ImageFilter.GaussianBlur(radius=0.5))

    out_path = "/home/ubuntu/socake-site/assets/background2.png"
    img.save(out_path, "PNG", optimize=True)
    print(f"✓ Background: {out_path}")
    return img

if __name__ == "__main__":
    make_background()
    print("Done!")
