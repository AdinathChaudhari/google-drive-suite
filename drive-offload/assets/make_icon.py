#!/usr/bin/env python3
"""Generate drive-offload's app icon: a 1024px master PNG rendered as a
macOS-style rounded-rectangle ("squircle") with a deep-blue->teal gradient,
a soft cloud, and a bold up-arrow rising into it — the offload motif.

Run: ./.venv/bin/python3 assets/make_icon.py
Then build the .icns with sips + iconutil (see the build docs).
"""
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
    """A soft, friendly cloud silhouette sitting in the upper half."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    c = color + (alpha,)
    cx, cy = size * 0.5, size * 0.37
    # base slab
    d.rounded_rectangle(
        [cx - size * 0.26, cy - size * 0.02, cx + size * 0.26, cy + size * 0.115],
        radius=size * 0.065, fill=c,
    )
    # puffs
    for dx, dy, r in [(-0.16, 0.0, 0.105), (0.02, -0.055, 0.145), (0.18, 0.0, 0.095)]:
        d.ellipse(
            [cx + size * dx - size * r, cy + size * dy - size * r,
             cx + size * dx + size * r, cy + size * dy + size * r],
            fill=c,
        )
    return layer


def _arrow_pts(size):
    """A bold upward arrow: triangular head over a rounded shaft, whose tip
    rises up into the cloud."""
    cx = size * 0.5
    head_tip_y = size * 0.335       # tip reaches into the cloud
    head_base_y = size * 0.565
    head_half_w = size * 0.155
    shaft_half_w = size * 0.062
    shaft_bottom_y = size * 0.755
    head = [
        (cx, head_tip_y),
        (cx + head_half_w, head_base_y),
        (cx - head_half_w, head_base_y),
    ]
    shaft = [cx - shaft_half_w, head_base_y - size * 0.01,
             cx + shaft_half_w, shaft_bottom_y]
    return head, shaft


def up_arrow(size, color):
    """A crisp up-arrow with a soft drop shadow for depth."""
    head, shaft = _arrow_pts(size)

    def draw_arrow(d, dx, dy, fill):
        d.polygon([(x + dx, y + dy) for x, y in head], fill=fill)
        d.rounded_rectangle(
            [shaft[0] + dx, shaft[1] + dy, shaft[2] + dx, shaft[3] + dy],
            radius=size * 0.032, fill=fill,
        )

    # soft shadow: dark arrow, offset down, heavily blurred
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    off = size * 0.012
    draw_arrow(sd, off, off, (8, 45, 70, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(size * 0.018))

    # crisp arrow on top
    arrow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ad = ImageDraw.Draw(arrow)
    draw_arrow(ad, 0, 0, color + (255,))

    return Image.alpha_composite(shadow, arrow)


def main():
    # gradient body
    top = (0x0B, 0x3D, 0x91)      # deep blue
    bottom = (0x17, 0xB8, 0xC4)   # teal-cyan
    body = vertical_gradient(SIZE, top, bottom).convert("RGBA")

    # subtle top highlight — heavily blurred so there is no hard edge
    gloss = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gd = ImageDraw.Draw(gloss)
    gd.ellipse([-SIZE * 0.4, -SIZE * 0.95, SIZE * 1.4, SIZE * 0.28],
               fill=(255, 255, 255, 55))
    gloss = gloss.filter(ImageFilter.GaussianBlur(SIZE * 0.06))
    body = Image.alpha_composite(body, gloss)

    # cloud (soft, subtle) then the up-arrow rising into it
    cl = cloud(SIZE, (255, 255, 255), 52).filter(ImageFilter.GaussianBlur(SIZE * 0.004))
    body = Image.alpha_composite(body, cl)
    body = Image.alpha_composite(body, up_arrow(SIZE, (255, 255, 255)))

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
