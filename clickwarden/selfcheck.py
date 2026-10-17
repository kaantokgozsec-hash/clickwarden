"""Check your configured vision provider on a neutral test window (nothing else on screen is sent).

    python -m clickwarden.selfcheck        (or scripts/provider_check.py)

Opens a small window with a known heading and a "Verify" button, captures ONLY that window, and asks the
provider (1) to read the text and (2) to locate the button. The returned point is compared with the button's
real position, so "it works" is measured, not assumed. Uses the provider you set up with `clickwarden configure`
and counts as one or two paid/quota requests. Exit code 0 means both checks passed; the last stdout line is JSON.
"""
import json
import sys
import tkinter as tk

HEADING = "PROVIDER CHECK 4417"


def run() -> dict:
    from .frames import capture
    from .vision import VisionClient
    from .win32core import ensure_dpi_aware

    client = VisionClient()
    if not client.configured:
        return {"ok": False, "problem": client.settings.problem}
    ensure_dpi_aware()
    root = tk.Tk()
    root.title("Clickwarden provider check")
    root.configure(bg="white")
    root.attributes("-topmost", True)
    root.geometry("560x340+160+160")
    tk.Label(root, text=HEADING, font=("Segoe UI", 30, "bold"), bg="white", fg="black").place(x=24, y=40)
    button = tk.Button(root, text="Verify", font=("Segoe UI", 18), bg="#dde6f5", relief="raised")
    button.place(x=200, y=200, width=160, height=64)
    root.update()
    root.update()
    left, top = root.winfo_rootx(), root.winfo_rooty()
    width, height = root.winfo_width(), root.winfo_height()
    box = (button.winfo_rootx(), button.winfo_rooty(), button.winfo_rootx() + button.winfo_width(),
           button.winfo_rooty() + button.winfo_height())
    print(f"Sending ONLY this {width}x{height} test window to {client.settings.host} ({client.model}).", flush=True)
    frame = capture(roi=(left, top, width, height), mode="precision")
    report = {"destination": client.settings.host, "model": client.model, "image": f"{width}x{height}"}
    try:
        read = client.inspect(frame, "Read the large heading and the button label in this window. "
                                     "Reply with only those texts.")
        answer = read.get("answer", "")
        report["read"] = {"ok": bool(read.get("success")) and "4417" in answer and "verify" in answer.lower(),
                          "answer": answer[:160], "latency_ms": read.get("latency_ms"), "error": read.get("error")}
        found = client.locate(frame, "the Verify button")
        point = found.get("point")
        inside = bool(point) and box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]
        report["locate"] = {"ok": bool(found.get("success")) and inside, "point": point,
                            "button_box": list(box), "inside_button": inside,
                            "confidence": found.get("confidence"), "latency_ms": found.get("latency_ms"),
                            "error": found.get("error")}
    finally:
        root.destroy()
        client.close()
    report["tokens"] = {"prompt": client.prompt_tokens, "completion": client.completion_tokens}
    report["ok"] = report["read"]["ok"] and report["locate"]["ok"]
    return report


def main() -> int:
    report = run()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("ok") else (2 if "problem" in report else 1)


if __name__ == "__main__":
    sys.exit(main())
