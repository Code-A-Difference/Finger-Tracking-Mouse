"""Draw the Finger Mouse icon and write every size the installers need.

    python tools/make_icons.py

Writes assets/icon.png (window and tray), assets/FingerMouse.ico (Windows)
and assets/FingerMouse.icns (macOS). The design is the app's own pointer
halo — a ring with a dot — on a dark rounded square. Needs Pillow.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"
S = 2048   # drawn large, then scaled down for clean edges


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # Rounded square with a vertical gradient: navy to deep teal.
    gradient = Image.new("RGBA", (S, S))
    top, bottom = (11, 19, 36), (8, 64, 80)
    for y in range(S):
        t = y / (S - 1)
        gradient.paste(tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,), (0, y, S, y + 1))
    mask = Image.new("L", (S, S), 0)
    margin = int(S * 0.06)
    ImageDraw.Draw(mask).rounded_rectangle((margin, margin, S - margin, S - margin), radius=int(S * 0.2), fill=255)
    img.paste(gradient, (0, 0), mask)

    cx, cy, r = S * 0.5, S * 0.5, S * 0.26
    width = int(S * 0.055)

    # Soft glow under the ring.
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((cx - r, cy - r, cx + r, cy + r), outline=(34, 211, 238, 150), width=width * 3)
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.03))
    img = Image.alpha_composite(img, Image.composite(glow, Image.new("RGBA", (S, S), (0, 0, 0, 0)), mask))

    # ImageDraw replaces pixels rather than blending, so the translucent fill
    # goes on its own layer and is composited over the background.
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(34, 211, 238, 50))                  # halo fill
    img = Image.alpha_composite(img, layer)
    d = ImageDraw.Draw(img)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(34, 211, 238, 255), width=width)  # halo ring
    dot = S * 0.075
    d.ellipse((cx - dot, cy - dot, cx + dot, cy + dot), fill=(236, 254, 255, 255))        # the pointer
    return img


def check_mark() -> Image.Image:
    """White tick for the settings checkboxes (drawn at 4x, scaled to 36 px)."""
    img = Image.new("RGBA", (144, 144), (0, 0, 0, 0))
    ImageDraw.Draw(img).line([(30, 76), (60, 106), (116, 40)], fill=(255, 255, 255, 255), width=18, joint="curve")
    return img.resize((36, 36), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    check_mark().save(OUT / "check.png")
    big = draw()
    icon = big.resize((1024, 1024), Image.LANCZOS)
    icon.resize((512, 512), Image.LANCZOS).save(OUT / "icon.png")
    icon.save(OUT / "FingerMouse.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    icon.save(OUT / "FingerMouse.icns")
    print("Wrote", ", ".join(p.name for p in sorted(OUT.glob("*"))))


if __name__ == "__main__":
    main()
