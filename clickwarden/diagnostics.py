"""Environment diagnostics; input is tested only when explicitly requested."""
import time

from .win32core import cursor_pos, foreground_info, virtual_screen


def diagnose(probe_input: bool = False) -> dict:
    from .vision import client, DISABLE_THINKING
    foreground = foreground_info()
    report = {
        "status": "ready", "foreground": foreground,
        "virtual_screen": list(virtual_screen()),
        "vision_configured": client.configured,
        "thinking_disabled": DISABLE_THINKING,
        "input_probe": "not_requested",
    }
    if foreground["class"] == "GameInputServiceWindow":
        report.update(status="blocked", reason="GameInputServiceWindow owns the foreground; input probe skipped")
        return report
    if not probe_input:
        return report

    from .engine import Engine
    eng = Engine(motion="instant")
    original = cursor_pos()
    vx, vy, vw, vh = virtual_screen()
    target = (vx + vw // 2, vy + vh // 2)
    if original == target:
        target = (target[0] + 20, target[1] + 20)
    try:
        eng.move(*target)
        time.sleep(0.1)
        observed = cursor_pos()
        matched = observed == target
        report.update(status="ready" if matched else "blocked", input_probe={
            "expected": list(target), "observed": list(observed), "matched": matched,
        })
        if not matched:
            report["reason"] = "SendInput returned but cursor did not reach the target; cause is not established"
    finally:
        eng.move(*original)
    return report
