#!/usr/bin/env python3
"""Generate drivecast's app icon: a 1024px master PNG rendered as a macOS-style
rounded-rectangle ("squircle") with a violet->magenta gradient, a soft cloud,
and a bold play triangle — the cloud-streaming motif.

Run: ./venv/bin/python assets/make_icon.py
Then build the .icns with iconutil (see the shell steps in the build docs).
"""
import math
import os

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PNG = os.path.join(HERE, "icon_1024.png")


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def vertical_gradient(size, top, bottom):
    grad = Image.new("RGB", (1, size), 0)
    px = grad.load()
    for y in range(size):
        px[0, y] = lerp(top, bottom, y / (size - 1))
    return grad.resize((size, size))


def rounded_mask(size, radius, margin):
    """Alpha mask: a rounded rect inset by `margin`, corner radius `radius`."""
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=radius, fill=255,
    )
    return m


def cloud(size, color, alpha):
    """A soft, friendly cloud silhouette centered horizontally."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    c = color + (alpha,)
    cx, cy = size * 0.5, size * 0.56
    # base slab
    d.rounded_rectangle(
        [cx - size * 0.26, cy - size * 0.02, cx + size * 0.26, cy + size * 0.12],
        radius=size * 0.07, fill=c,
    )
    # puffs
    for dx, dy, r in [(-0.16, 0.0, 0.11), (0.02, -0.06, 0.15), (0.18, 0.0, 0.10)]:
        d.ellipse(
            [cx + size * dx - size * r, cy + size * dy - size * r,
             cx + size * dx + size * r, cy + size * dy + size * r],
            fill=c,
        )
    return layer


def _play_pts(size):
    cx, cy = size * 0.5, size * 0.5
    r = size * 0.16
    return [
        (cx - r * 0.80, cy - r),
        (cx - r * 0.80, cy + r),
        (cx + r * 1.02, cy),
    ]


def play_triangle(size, color):
    """A crisp play triangle with a soft drop shadow for depth."""
    pts = _play_pts(size)

    # soft shadow: dark triangle, offset down, heavily blurred
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    off = size * 0.012
    sd.polygon([(x + off, y + off) for x, y in pts], fill=(60, 20, 90, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(size * 0.018))

    # crisp triangle on top
    tri = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    td = ImageDraw.Draw(tri)
    td.polygon(pts, fill=color + (255,))

    return Image.alpha_composite(shadow, tri)


def main():
    # gradient body
    top = (0x6A, 0x5C, 0xF6)      # indigo-violet
    bottom = (0xB6, 0x3C, 0xE8)   # magenta-purple
    body = vertical_gradient(SIZE, top, bottom).convert("RGBA")

    # subtle top highlight — heavily blurred so there is no hard edge
    gloss = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gd = ImageDraw.Draw(gloss)
    gd.ellipse([-SIZE * 0.4, -SIZE * 0.95, SIZE * 1.4, SIZE * 0.28],
               fill=(255, 255, 255, 55))
    gloss = gloss.filter(ImageFilter.GaussianBlur(SIZE * 0.06))
    body = Image.alpha_composite(body, gloss)

    # cloud (soft, subtle) then the play mark
    cl = cloud(SIZE, (255, 255, 255), 48).filter(ImageFilter.GaussianBlur(SIZE * 0.004))
    body = Image.alpha_composite(body, cl)
    body = Image.alpha_composite(body, play_triangle(SIZE, (255, 255, 255)))

    # squircle clip (macOS proportions: ~9% margin, ~22% corner radius of body)
    margin = round(SIZE * 0.085)
    radius = round((SIZE - 2 * margin) * 0.235)
    mask = rounded_mask(SIZE, radius, margin)
    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    out.paste(body, (0, 0), mask)

    out.save(OUT_PNG)
    print("wrote", OUT_PNG)


if __name__ == "__main__":
    main()
