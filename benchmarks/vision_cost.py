"""Vision cost and accuracy benchmark on synthetic images. Never touches the desktop.

    python benchmarks/vision_cost.py [--runs 8] [--quick]

Renders the same neutral "PROVIDER CHECK 4417 / Verify" window the safe test shows, entirely in memory,
and sends only those synthetic images to the provider you configured with `clickwarden configure`.
No window opens, nothing is captured from your screen and no input is injected, so it can run while you
work. Each request uses your provider quota (free tiers are rate limited).

Conditions (each measured independently, with the true button box known exactly):
  crop       the 560x340 window only, as Clickwarden sends it (region of interest)
  fullscreen the same window inside a 1920x1080 desktop, as a "send the whole screen" agent would
  pixels     crop, but asking for image-pixel coordinates instead of the saved coordinate format
  reasoning  crop, with the provider's reasoning switched on (slow models; few runs)
  read       crop, reading the heading and button text

Writes benchmarks/results/vision_cost_<time>.json and prints a summary.
"""
from __future__ import annotations

import argparse
import dataclasses
import io
import json
import statistics
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clickwarden import providers  # noqa: E402
from clickwarden.frames import Frame  # noqa: E402
from clickwarden.vision import VisionClient  # noqa: E402

HEADING = "PROVIDER CHECK 4417"
WINDOW = (560, 340)
BUTTON = (200, 200, 360, 264)  # left, top, right (exclusive), bottom (exclusive) inside the window
FONTS = Path(r"C:\Windows\Fonts")


def _font(name, size):
    try:
        return ImageFont.truetype(str(FONTS / name), size)
    except OSError:
        return ImageFont.load_default()


def draw_window() -> Image.Image:
    image = Image.new("RGB", WINDOW, "white")
    draw = ImageDraw.Draw(image)
    draw.text((24, 40), HEADING, fill="black", font=_font("segoeuib.ttf", 40))
    l, t, r, b = BUTTON
    draw.rectangle((l, t, r - 1, b - 1), fill="#dde6f5")
    draw.line((l, t, r - 1, t), fill="white", width=2)
    draw.line((l, t, l, b - 1), fill="white", width=2)
    draw.line((l, b - 1, r - 1, b - 1), fill="#7a7a7a", width=2)
    draw.line((r - 1, t, r - 1, b - 1), fill="#7a7a7a", width=2)
    font = _font("segoeui.ttf", 24)
    w = draw.textlength("Verify", font=font)
    draw.text((l + (r - l - w) / 2, t + 16), "Verify", fill="black", font=font)
    return image


def draw_fullscreen(window: Image.Image) -> tuple[Image.Image, tuple[int, int]]:
    """The window placed on a plain 1920x1080 desktop with a taskbar and some unrelated text."""
    desk = Image.new("RGB", (1920, 1080), "#1f3b57")
    draw = ImageDraw.Draw(desk)
    draw.rectangle((0, 1032, 1919, 1079), fill="#202020")
    small = _font("segoeui.ttf", 15)
    for i, name in enumerate(("Documents", "Notes.txt", "Project", "Recycle Bin", "Photos")):
        draw.rectangle((24, 24 + i * 110, 72, 72 + i * 110), fill="#d9b44a" if i % 2 == 0 else "#c9d6e3")
        draw.text((16, 78 + i * 110), name, fill="white", font=small)
    draw.text((1780, 1046), "14:05", fill="white", font=small)
    at = (680, 370)
    draw.rectangle((at[0] - 1, at[1] - 32, at[0] + WINDOW[0], at[1] + WINDOW[1]), fill="#f3f3f3", outline="#888")
    draw.text((at[0] + 10, at[1] - 26), "Clickwarden provider check", fill="black", font=small)
    desk.paste(window, at)
    return desk, at


def frame_of(image: Image.Image) -> Frame:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return Frame(buffer.getvalue(), 0, 0, image.width, image.height, image.width, image.height, "precision", 85)


def measure(client: VisionClient, kind: str, frame: Frame, box) -> dict:
    p0, c0 = client.prompt_tokens, client.completion_tokens
    t0 = time.perf_counter()
    if kind == "read":
        out = client.inspect(frame, "Read the large heading and the button label in this window. Reply with only those texts.")
        answer = out.get("answer", "") or ""
        ok = bool(out.get("success")) and "4417" in answer and "verify" in answer.lower()
        row = {"ok": ok, "answer": answer[:80], "error": out.get("error")}
    else:
        out = client.locate(frame, "the Verify button")
        point = out.get("point")
        inside = bool(point) and box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]
        centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        row = {"ok": bool(out.get("success")) and inside, "point": point,
               "error_px": round(((point[0] - centre[0]) ** 2 + (point[1] - centre[1]) ** 2) ** .5, 1) if point else None,
               "error": out.get("error")}
    row.update(ms=round((time.perf_counter() - t0) * 1000), prompt=client.prompt_tokens - p0,
               completion=client.completion_tokens - c0, rate_limited="429" in str(row.get("error") or ""))
    return row


def is_fatal(error) -> bool:
    """Errors that no retry can fix: the provider rejected the key or the request itself."""
    text = str(error or "")
    return any(code in text for code in ("HTTP 401", "HTTP 403", "HTTP 404"))


def summarise(rows: list[dict]) -> dict:
    served = [r for r in rows if not r["rate_limited"]]
    def med(key):
        values = [r[key] for r in served if r[key] is not None]
        return statistics.median(values) if values else None
    errors = [r["error_px"] for r in served if r.get("error_px") is not None]
    ms = sorted(r["ms"] for r in served)
    return {"runs": len(rows), "rate_limited": len(rows) - len(served), "passed": sum(r["ok"] for r in served),
            "median_prompt_tokens": med("prompt"), "median_completion_tokens": med("completion"),
            "median_ms": med("ms"), "max_ms": ms[-1] if ms else None,
            "median_error_px": statistics.median(errors) if errors else None}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=8)
    parser.add_argument("--quick", action="store_true", help="fewer runs per condition")
    parser.add_argument("--pause", type=float, default=3.5, help="seconds between requests (free-tier rate limits)")
    args = parser.parse_args()
    settings = providers.resolve()
    if settings.problem:
        print(json.dumps({"ok": False, "problem": settings.problem}))
        return 2
    n = 3 if args.quick else args.runs
    plan = [("crop", "locate", n), ("read", "read", max(2, n // 2)), ("fullscreen", "locate", max(3, (3 * n) // 4)),
            ("pixels", "locate", max(2, n // 2)), ("reasoning", "locate", 2 if args.quick else 3)]
    window = draw_window()
    desk, at = draw_fullscreen(window)
    crop_frame, desk_frame = frame_of(window), frame_of(desk)
    desk_box = (BUTTON[0] + at[0], BUTTON[1] + at[1], BUTTON[2] + at[0], BUTTON[3] + at[1])
    print(f"Sending synthetic images only (nothing from your screen) to {settings.host} ({settings.model}).", flush=True)
    results = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "host": settings.host, "model": settings.model,
               "saved_coord_space": settings.coord_space, "saved_extra_body": settings.extra_body,
               "images": {"crop": [*WINDOW, len(crop_frame.jpeg)], "fullscreen": [1920, 1080, len(desk_frame.jpeg)]},
               "conditions": {}}
    for name, kind, runs in plan:
        variant = settings
        if name == "pixels":
            variant = dataclasses.replace(settings, coord_space="pixels")
        if name == "reasoning":
            variant = dataclasses.replace(settings, extra_body={**settings.extra_body, "reasoning": {"enabled": True}})
        client = VisionClient(settings=variant)
        frame, box = (desk_frame, desk_box) if name == "fullscreen" else (crop_frame, BUTTON)
        rows = []
        for i in range(runs):
            rows.append(measure(client, kind, frame, box))
            print(f"  {name:10s} {i + 1}/{runs}: ok={rows[-1]['ok']} {rows[-1]['ms']} ms "
                  f"{rows[-1]['prompt']}+{rows[-1]['completion']} tok {rows[-1].get('error') or ''}", flush=True)
            if is_fatal(rows[-1].get("error")):
                client.close()
                print("Stopped: the provider refused the request (expired or wrong key, or unknown model). "
                      "Nothing was saved. Open `clickwarden configure` and enter a new key yourself, then rerun.")
                return 3
            time.sleep(args.pause)
        client.close()
        results["conditions"][name] = {"kind": kind, "summary": summarise(rows), "rows": rows}
    out = Path(__file__).resolve().parent / "results" / f"vision_cost_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps({k: v["summary"] for k, v in results["conditions"].items()}, indent=2))
    print("saved", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
