"""Everything Clickwarden stores on this computer, listed honestly, with a way to delete each part.

Nothing here talks to the network. Only the known locations below are ever touched: a user-chosen
``CLICKWARDEN_JOB_DIR`` is never deleted, and links/junctions are unlinked rather than followed.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import providers

# name -> (label, what it holds, sensitive)
ITEMS = {
    "key": ("API key", "Your provider key, encrypted with Windows for your account only.", True),
    "settings": ("Settings and consent", "Provider, model, options and the record of which host you accepted.", False),
    "sessions": ("Session records", "Text records of sessions: what was done and when; may include window titles "
                                    "and text that was typed.", True),
    "jobs": ("Job records", "Text records of background jobs (goal, steps, results).", True),
    "layouts": ("Desktop layouts", "Saved icon/window layouts.", False),
    "shots": ("Saved screenshots", "Screenshots written to disk; these may show anything that was on screen.", True),
    "shortcut": ("Desktop shortcut", "The Clickwarden.lnk shortcut. Deleting it never touches your data.", False),
}
ACTIVITY_DIRS = ("sessions", "jobs", "layouts", "shots")
WIPE_DEFAULT = ("key", "settings") + ACTIVITY_DIRS  # "delete everything" (the shortcut is only removed if asked)


def data_root(home: Path | None = None) -> Path:
    return Path(home or Path.home()) / ".clickwarden"


def locate(name: str, home: Path | None = None) -> Path:
    if name == "key":
        return providers.config_dir() / "key.dpapi"
    if name == "settings":
        return providers.config_dir() / "config.json"
    if name == "shortcut":
        return Path(home or Path.home()) / "Desktop" / "Clickwarden.lnk"
    if name in ACTIVITY_DIRS:
        return data_root(home) / name
    raise KeyError(name)


def _is_link(path: Path) -> bool:
    """Symlink or Windows junction/reparse point (never followed or recursed into)."""
    if path.is_symlink():
        return True
    try:
        return bool(path.lstat().st_file_attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except (OSError, AttributeError):
        return False


def _measure(path: Path) -> tuple[int, int]:
    """(files, bytes) without following links."""
    if _is_link(path) or not path.exists():
        return 0, 0
    if path.is_file():
        return 1, path.stat().st_size
    files = size = 0
    for root, dirs, names in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not _is_link(Path(root) / d)]
        for name in names:
            try:
                size += (Path(root) / name).stat().st_size
                files += 1
            except OSError:
                pass
    return files, size


def _probe(path: Path) -> tuple[bool, str]:
    """(exists, error). A permission problem is reported, not mistaken for "nothing stored"."""
    try:
        os.lstat(path)
        return True, ""
    except FileNotFoundError:
        return False, ""
    except OSError as error:
        return False, f"{type(error).__name__}: {error.strerror or error}"


def inventory(home: Path | None = None) -> list[dict]:
    rows = []
    for name, (label, note, sensitive) in ITEMS.items():
        path = locate(name, home)
        exists, error = _probe(path)
        files, size = _measure(path) if exists else (0, 0)
        rows.append({"name": name, "label": label, "note": note, "sensitive": sensitive, "path": str(path),
                     "exists": exists, "files": files, "bytes": size, "error": error})
    return rows


TEXT_VIEWABLE = ("settings", "sessions", "jobs", "layouts")  # the key is never shown; screenshots open as a folder
MAX_VIEW_BYTES = 200_000
MAX_LISTED = 300


def list_files(name: str, home: Path | None = None) -> list[dict]:
    """Files inside one viewable item, newest first: [{"name": relative path, "bytes": n, "modified": ts}]."""
    if name not in TEXT_VIEWABLE:
        return []
    base = locate(name, home)
    if _is_link(base) or not base.exists():
        return []
    if base.is_file():
        return [{"name": base.name, "bytes": base.stat().st_size, "modified": base.stat().st_mtime}]
    found = []
    for root, dirs, names in os.walk(base, followlinks=False):
        dirs[:] = [d for d in dirs if not _is_link(Path(root) / d)]
        for file_name in names:
            path = Path(root) / file_name
            if _is_link(path):
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            found.append({"name": str(path.relative_to(base)), "bytes": info.st_size, "modified": info.st_mtime})
    found.sort(key=lambda row: row["modified"], reverse=True)
    return found[:MAX_LISTED]


def read_text(name: str, relative: str, home: Path | None = None) -> str:
    """Read one listed file as text for the viewer. Refuses the key, other items, links and escapes."""
    if name not in TEXT_VIEWABLE:
        raise PermissionError(f"{name} cannot be shown")
    base = locate(name, home)
    target = base if base.is_file() else (base / relative)
    if base.is_file() and relative != base.name:
        raise PermissionError("not part of this item")
    resolved_base = base.resolve()
    resolved = target.resolve()
    if not base.is_file() and resolved_base not in resolved.parents:
        raise PermissionError("outside this item")
    if _is_link(target) or not target.is_file():
        raise PermissionError("not a regular file")
    with open(target, "rb") as handle:
        data = handle.read(MAX_VIEW_BYTES + 1)
    text = data[:MAX_VIEW_BYTES].decode("utf-8", errors="replace")
    return text + ("\n\n[... shown only the first %d bytes ...]" % MAX_VIEW_BYTES if len(data) > MAX_VIEW_BYTES else "")


def human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} GB"


def status_text(row: dict) -> str:
    """Short state without the label, e.g. "3 file(s), 12.0 KB"."""
    if row.get("error"):
        return "cannot be read (" + row["error"][:80] + ")"
    if not row["exists"]:
        return "nothing stored"
    if row["name"] in ("key", "settings", "shortcut"):
        return f"stored ({human_size(row['bytes'])})"
    return f"{row['files']} file(s), {human_size(row['bytes'])}"


def summary_line(row: dict) -> str:
    return f"{row['label']}: {status_text(row)}"


def _remove(path: Path) -> str | None:
    """Delete one known path; returns an error message or None."""
    try:
        if _is_link(path) or path.is_file():
            path.unlink()
        elif path.is_dir():
            failures = []
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=lambda _f, p, e: failures.append(f"{p}: {e}"))
            else:
                shutil.rmtree(path, onerror=lambda _f, p, e: failures.append(f"{p}: {e[1]}"))
            if failures:
                return f"{len(failures)} item(s) could not be removed (in use?), e.g. {failures[0]}"
        return None
    except OSError as error:
        return str(error)


def delete(names, home: Path | None = None) -> dict:
    """Delete the named items. Returns {"deleted": [...], "missing": [...], "errors": {name: message}}."""
    result = {"deleted": [], "missing": [], "errors": {}}
    for name in names:
        if name not in ITEMS:
            result["errors"][str(name)] = "unknown item"
            continue
        path = locate(name, home)
        if not (path.exists() or _is_link(path)):
            result["missing"].append(name)
            continue
        problem = _remove(path)
        if name == "settings":
            path.with_name(path.name + ".tmp").unlink(missing_ok=True)
        if problem:
            result["errors"][name] = problem
        else:
            result["deleted"].append(name)
    for folder in (providers.config_dir(), data_root(home)):  # tidy up empty folders only
        try:
            folder.rmdir()
        except OSError:
            pass
    return result


def wipe_everything(include_shortcut: bool = False, home: Path | None = None) -> dict:
    return delete(WIPE_DEFAULT + (("shortcut",) if include_shortcut else ()), home)
