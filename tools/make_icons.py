#!/usr/bin/env python3
"""Regenerate the Android launcher icons (and the web favicons) from the real logo.

Nothing here draws anything: every icon is the actual logo file, resized. Run it
after replacing ``static/…Logo….png`` and commit what it writes.

    python tools/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "lottery_tracker" / "web" / "static"
RES = ROOT / "android" / "app" / "src" / "main" / "res"

PAPER = (250, 247, 236, 255)      # @color/paper — the adaptive-icon background

# Legacy square/round launcher icons, per density bucket.
LAUNCHER = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
# Adaptive-icon foreground: a 108dp canvas whose outer ~25% can be cropped by the
# launcher's mask, so the logo only gets the inner safe zone.
FOREGROUND = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}
SAFE_ZONE = 0.62                  # fraction of the canvas the logo may occupy


def find_logo() -> Path:
    """The store logo: any static image with 'logo' in the name."""
    for p in sorted(STATIC.iterdir()):
        if "logo" in p.name.lower() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            return p
    raise SystemExit(f"no logo image found in {STATIC}")


def centered(logo: Image.Image, canvas: int, scale: float, bg=None) -> Image.Image:
    """The logo, resized to `scale` of a square canvas and centred on it."""
    out = Image.new("RGBA", (canvas, canvas), bg or (0, 0, 0, 0))
    side = max(1, int(canvas * scale))
    art = logo.copy()
    art.thumbnail((side, side), Image.LANCZOS)
    out.paste(art, ((canvas - art.width) // 2, (canvas - art.height) // 2), art)
    return out


def circle_masked(img: Image.Image) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, img.width - 1, img.height - 1), fill=255)
    out = img.copy()
    out.putalpha(mask)
    return out


def main() -> None:
    src = find_logo()
    logo = Image.open(src).convert("RGBA")
    print(f"source: {src.name} {logo.size[0]}×{logo.size[1]}")

    for bucket, size in LAUNCHER.items():
        d = RES / f"mipmap-{bucket}"
        d.mkdir(parents=True, exist_ok=True)
        square = centered(logo, size, 0.88, bg=PAPER)
        square.save(d / "ic_launcher.png")
        circle_masked(centered(logo, size, 0.80, bg=PAPER)).save(d / "ic_launcher_round.png")

    for bucket, size in FOREGROUND.items():
        d = RES / f"drawable-{bucket}"
        d.mkdir(parents=True, exist_ok=True)
        centered(logo, size, SAFE_ZONE).save(d / "ic_logo_fg.png")

    # Web icons, from the same source.
    centered(logo, 180, 0.92, bg=PAPER).save(STATIC / "apple-touch-icon.png")
    circle_masked(centered(logo, 32, 0.94, bg=PAPER)).save(STATIC / "favicon-32.png")
    ico = circle_masked(centered(logo, 64, 0.94, bg=PAPER))
    ico.save(STATIC / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    print("wrote launcher icons, adaptive foregrounds and web favicons")


if __name__ == "__main__":
    main()
