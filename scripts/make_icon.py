"""Draw the Clickwarden icon (dark tile, red stop ring, white cursor) into clickwarden/assets/clickwarden.ico.

    python scripts/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
OUT = Path(__file__).resolve().parents[1] / "clickwarden" / "assets" / "clickwarden.ico"


def draw(size: int = 1024) -> Image.Image:
    scale = size / 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas = ImageDraw.Draw(img)
    canvas.rounded_rectangle((0, 0, size - 1, size - 1), radius=int(56 * scale), fill="#0b0d12")
    canvas.ellipse([int(v * scale) for v in (36, 36, 220, 220)], outline="#e63950", width=int(22 * scale))
    cursor = [(92, 72), (92, 182), (121, 156), (143, 206), (168, 196), (146, 147), (186, 147)]
    canvas.polygon([(int(x * scale), int(y * scale)) for x, y in cursor], fill="white")
    return img


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    draw().save(OUT, sizes=SIZES)
    print("wrote", OUT, OUT.stat().st_size, "bytes")
