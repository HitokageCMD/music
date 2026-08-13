"""Rasterise the app icon. Run: uv run --with pillow python tools/make_icons.py

PNG, not SVG, and not optional: iOS silently ignores an SVG apple-touch-icon and
puts a screenshot of the page on the home screen instead.
"""

from pathlib import Path

from PIL import Image, ImageDraw

WEB = Path(__file__).resolve().parent.parent / "web"
BG = "#0d0d10"
FG = "#f4b942"

# Same geometry as icon.svg, in a 512 box.
STEM = [(196, 330), (196, 150), (346, 118), (346, 298)]
NOTES = [(160, 336), (310, 304)]
R = 38
W = 26
SS = 4  # supersample; Pillow has no antialiasing, so draw big and shrink


def draw(size: int, *, radius: float, scale: float) -> Image.Image:
    n = size * SS
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    k = n / 512

    if radius:
        d.rounded_rectangle([0, 0, n - 1, n - 1], radius=radius * k, fill=BG)
    else:
        d.rectangle([0, 0, n - 1, n - 1], fill=BG)

    def place(x, y):
        # Scale about the centre so maskable art stays inside the safe zone.
        return ((x - 256) * scale + 256) * k, ((y - 256) * scale + 256) * k

    w = max(1, round(W * k * scale))
    d.line([place(*p) for p in STEM], fill=FG, width=w, joint="curve")
    # Pillow has no round line caps; dot the ends by hand.
    for p in (STEM[0], STEM[-1]):
        x, y = place(*p)
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=FG)
    for cx, cy in NOTES:
        x, y = place(cx, cy)
        r = R * k * scale
        d.ellipse([x - r, y - r, x + r, y + r], outline=FG, width=w)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    out = [
        ("icon-192.png", draw(192, radius=112, scale=1.0)),
        ("icon-512.png", draw(512, radius=112, scale=1.0)),
        # Maskable: Android crops to its own shape, so square off the corners and
        # keep everything inside the middle 80%.
        ("icon-maskable-512.png", draw(512, radius=0, scale=0.72)),
    ]
    for name, img in out:
        img.save(WEB / name)
        print(f"  {name:26} {img.size[0]}x{img.size[1]}")

    # iOS masks the corners itself and cannot handle transparency.
    apple = draw(180, radius=0, scale=1.0).convert("RGB")
    apple.save(WEB / "apple-touch-icon.png")
    print(f"  {'apple-touch-icon.png':26} 180x180 (RGB, iOS rounds it itself)")


if __name__ == "__main__":
    main()
