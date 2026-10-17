r"""
clickwarden — ultra-low-latency Windows computer use.

    capture -> the vision model you configure (streaming) -> SendInput
                     \-> HUD: pulsing border + target reticle (never blocks input)

Quick start:
    from clickwarden import engine
    engine.smart_click("the YouTube skip-forward button")
"""

from pathlib import Path
from dotenv import load_dotenv

# Optional: non-secret CLICKWARDEN_* settings from a local, git-ignored .env. The API key belongs in
# the encrypted store written by `clickwarden configure`, never in this file.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from .engine import Engine, engine  # noqa: E402, F401

from ._version import __version__  # noqa: E402
__all__ = ["Engine", "engine", "__version__"]
