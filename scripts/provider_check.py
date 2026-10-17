"""Check your configured vision provider on a neutral test window (nothing else on screen is sent).

    python scripts/provider_check.py

Thin wrapper around `python -m clickwarden.selfcheck`; see that module for what it does and sends.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clickwarden.selfcheck import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
