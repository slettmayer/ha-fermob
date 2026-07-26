"""Generate the HACS/Home Assistant brand icon for this integration.

The icon is an **original work** authored here and shipped under this
repository's MIT licence. It deliberately does **not** reproduce Fermob's logo,
wordmark or product silhouette -- those are trademarks of Fermob and could not
be relicensed under MIT. See docs/tech/BRANDING.md for the reasoning.

The design is functional rather than brand-derived: a light source carrying a
horizontal warm-to-cold gradient, which is exactly how these lamps express
colour temperature (a warm channel and a cold channel whose sum is the total
output).

Run:
    pip install Pillow
    python scripts/generate_brand_icon.py

Writes custom_components/fermob/brand/icon.png (256px) and icon@2x.png (512px).
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SUPERSAMPLE = 4
OUT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "fermob" / "brand"

# Background squircle: a dark slate that reads on both light and dark HA themes.
BG_TOP = (40, 56, 72)
BG_BOTTOM = (18, 26, 35)
# The two LED channels. Warm is the 3000 K end, cold the 6000 K end.
WARM = (255, 158, 66)
COLD = (188, 224, 255)
BLOOM = (255, 226, 196)

CORNER_RADIUS = 0.22
DISC_RADIUS = 0.235
RAY_INNER = 0.30
RAY_OUTER = 0.405


def _vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    """Blend two colours top-to-bottom across a square image."""
    mask = Image.linear_gradient("L").resize((size, size), Image.LANCZOS)
    return Image.composite(
        Image.new("RGB", (size, size), bottom),
        Image.new("RGB", (size, size), top),
        mask,
    )


def _horizontal_gradient(size: int, left: tuple, right: tuple) -> Image.Image:
    """Blend two colours left-to-right across a square image."""
    # rotate(90) puts 0 on the left edge and 255 on the right, and composite()
    # takes its first image where the mask is 255 -- so `right` goes first.
    mask = Image.linear_gradient("L").rotate(90, expand=False)
    mask = mask.resize((size, size), Image.LANCZOS)
    return Image.composite(
        Image.new("RGB", (size, size), right),
        Image.new("RGB", (size, size), left),
        mask,
    )


def _radial_falloff(size: int) -> Image.Image:
    """Return an 'L' mask that is bright at the centre and fades to the edge."""
    gradient = Image.radial_gradient("L").resize((size, size), Image.LANCZOS)
    return Image.eval(gradient, lambda v: 255 - v)


def _circle_mask(size: int, radius: int) -> Image.Image:
    """Return an 'L' mask holding one filled circle centred in the image."""
    mask = Image.new("L", (size, size), 0)
    half = size // 2
    ImageDraw.Draw(mask).ellipse(
        (half - radius, half - radius, half + radius, half + radius), fill=255
    )
    return mask


def _ray_mask(size: int, count: int = 8) -> Image.Image:
    """Return an 'L' mask of `count` tapered rays radiating from the centre."""
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    centre = size / 2
    wide, narrow = math.radians(3.6), math.radians(1.1)
    for index in range(count):
        angle = math.radians(index * (360 / count) - 90)
        points = []
        for radius, spread in (
            (RAY_INNER, wide),
            (RAY_OUTER, narrow),
        ):
            points.append(
                (
                    centre + radius * size * math.cos(angle - spread),
                    centre + radius * size * math.sin(angle - spread),
                )
            )
        for radius, spread in (
            (RAY_OUTER, narrow),
            (RAY_INNER, wide),
        ):
            points.append(
                (
                    centre + radius * size * math.cos(angle + spread),
                    centre + radius * size * math.sin(angle + spread),
                )
            )
        draw.polygon(points, fill=235)
    return mask


def _tint(size: int, colour: tuple, alpha: Image.Image) -> Image.Image:
    """Build an RGBA layer of one flat colour carrying `alpha`."""
    layer = Image.new("RGBA", (size, size), (*colour, 0))
    layer.putalpha(alpha)
    return layer


def build_icon(size: int) -> Image.Image:
    """Render the icon at `size` px, supersampled internally."""
    n = size * SUPERSAMPLE
    half = n // 2

    # 1. The rounded-square plate.
    plate = Image.new("L", (n, n), 0)
    ImageDraw.Draw(plate).rounded_rectangle(
        (0, 0, n - 1, n - 1), radius=int(n * CORNER_RADIUS), fill=255
    )
    canvas = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    canvas.paste(_vertical_gradient(n, BG_TOP, BG_BOTTOM), (0, 0), plate)

    # 2. A broad bloom behind the disc, so it reads as something emitting light.
    bloom_box = int(n * 0.92)
    bloom = Image.new("L", (n, n), 0)
    bloom.paste(
        Image.eval(_radial_falloff(bloom_box), lambda v: int(v * 0.30)),
        ((n - bloom_box) // 2, (n - bloom_box) // 2),
    )
    canvas = Image.alpha_composite(canvas, _tint(n, BLOOM, bloom))

    # 3. Light rays, softened so they glow rather than cut.
    rays = _ray_mask(n).filter(ImageFilter.GaussianBlur(radius=n * 0.006))
    rays = Image.eval(rays, lambda v: int(v * 0.85))
    canvas = Image.alpha_composite(canvas, _tint(n, BLOOM, rays))

    # 4. The disc: warm channel on the left, cold on the right, mixed across the
    #    middle the way the lamp mixes them.
    disc_r = int(n * DISC_RADIUS)
    disc = _circle_mask(n, disc_r)
    body = _horizontal_gradient(n, WARM, COLD).convert("RGBA")
    body.putalpha(disc)
    canvas = Image.alpha_composite(canvas, body)

    # 5. A faint specular arc across the top of the disc, for depth.
    sheen = Image.new("L", (n, n), 0)
    inset = int(disc_r * 0.28)
    ImageDraw.Draw(sheen).ellipse(
        (
            half - disc_r + inset,
            half - disc_r + int(disc_r * 0.12),
            half + disc_r - inset,
            half - int(disc_r * 0.30),
        ),
        fill=70,
    )
    sheen = sheen.filter(ImageFilter.GaussianBlur(radius=disc_r * 0.16))
    sheen = Image.composite(sheen, Image.new("L", (n, n), 0), disc)
    canvas = Image.alpha_composite(canvas, _tint(n, (255, 255, 255), sheen))

    # 6. Clip everything back to the plate so nothing bleeds past the corners.
    clipped = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    clipped.paste(canvas, (0, 0), plate)
    return clipped.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    root = OUT_DIR.parents[2]
    for size, name in ((256, "icon.png"), (512, "icon@2x.png")):
        path = OUT_DIR / name
        build_icon(size).save(path, "PNG", optimize=True)
        print(f"wrote {path.relative_to(root)} ({size}x{size})")


if __name__ == "__main__":
    main()
