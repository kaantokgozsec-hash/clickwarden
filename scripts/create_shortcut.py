"""Create a desktop shortcut (with the Clickwarden icon) that opens the settings window.

    python scripts/create_shortcut.py [folder]      # default folder: your Desktop

Uses pythonw.exe (no console window) and this checkout as the working directory, so it works without
`pip install`. Only creates one .lnk file; nothing else is touched.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ICON = ROOT / "clickwarden" / "assets" / "clickwarden.ico"


def make_shortcut(folder: Path) -> Path:
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name("pythonw.exe")
    target = pythonw if pythonw.exists() else interpreter
    link = folder / "Clickwarden.lnk"
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:CW_LINK);"
              "$s.TargetPath=$env:CW_TARGET;$s.Arguments='-m clickwarden.cli configure';"
              "$s.WorkingDirectory=$env:CW_DIR;$s.IconLocation=$env:CW_ICON;"
              "$s.Description='Clickwarden: choose your vision provider and review data sharing';$s.Save()")
    env = {**__import__("os").environ, "CW_LINK": str(link), "CW_TARGET": str(target), "CW_DIR": str(ROOT),
           "CW_ICON": str(ICON)}
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, env=env)
    return link


if __name__ == "__main__":
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Desktop"
    print("created", make_shortcut(destination))
