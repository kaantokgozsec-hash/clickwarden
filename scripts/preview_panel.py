"""Show the control panel for a few seconds (no session, no input, no model calls).

    python scripts/preview_panel.py [seconds]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clickwarden.panel import NotchPanel  # noqa: E402

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 25
panel = NotchPanel("Preview of the control panel", lambda: None, lambda: None, lambda: None,
                   lambda: panel.root.after(0, panel.root.destroy),
                   destination=("Screens → api.example.com", "#ffcf70"))
panel.update("Preview: nothing is running", "Click the arrow to expand. STOP closes this preview.")
end = time.monotonic() + seconds
try:
    while time.monotonic() < end:
        panel.root.update()
        time.sleep(0.03)
except Exception:  # window closed by STOP
    pass
