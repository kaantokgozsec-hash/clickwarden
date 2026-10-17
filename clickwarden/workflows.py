"""Small deterministic workflows; no model calls or implicit shell execution."""
import fnmatch
import os
from pathlib import Path
import stat
import time
from urllib.parse import urlencode, urlsplit

BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}


def wait_for_content_change(before, sample, check, timeout=8):
    """Local image-change hint, never a claim that a URL or page loaded correctly."""
    from .frames import diff_ratio
    started = time.monotonic()
    previous, changed, stable = before, False, 0
    while time.monotonic() - started < timeout:
        check()
        current = sample()
        changed = changed or diff_ratio(before, current) > .015
        stable = stable + 1 if diff_ratio(previous, current) <= .004 else 0
        elapsed = time.monotonic() - started
        if changed and stable >= 3 and elapsed >= .5:
            return {"hint": "content_changed_and_settled", "wait_ms": round(elapsed*1000),
                    "page_load_verified": False}
        previous = current
        time.sleep(.12)
    return {"hint": "wait_limit", "content_changed": changed,
            "wait_ms": round((time.monotonic()-started)*1000), "page_load_verified": False}


def web_url(value):
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ValueError("URL must be a nonempty string of at most 8192 characters")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("URL must not contain whitespace or control characters")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use an absolute http/https URL without credentials")
    return value


def search_url(query, provider="bing"):
    endpoints = {"bing": "https://www.bing.com/search", "google": "https://www.google.com/search",
                 "duckduckgo": "https://duckduckgo.com/"}
    if provider not in endpoints:
        raise ValueError("provider must be bing, google or duckduckgo")
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ValueError("Search query must contain 1–2000 characters")
    return endpoints[provider] + "?" + urlencode({"q": query})


def find_files(root, pattern="*", limit=40, max_entries=20000, max_seconds=5, check=lambda: None):
    """Bounded name-only search. Do not follow directory links or Windows junctions."""
    base = Path(root).expanduser()
    if not base.is_absolute() or not base.is_dir():
        raise ValueError("root must be an existing absolute directory")
    if not isinstance(pattern, str) or not pattern or any(c in pattern for c in ("/", "\\")):
        raise ValueError("pattern must be a filename glob such as *.dwg")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be 1–100")
    if type(max_entries) is not int or not 1 <= max_entries <= 100000:
        raise ValueError("max_entries must be 1–100000")
    if type(max_seconds) not in (int, float) or not 0 < max_seconds <= 30:
        raise ValueError("max_seconds must be greater than zero and at most 30")
    check()
    base = base.resolve()
    stack, matches = [base], []
    scanned = skipped = 0
    deadline = time.monotonic() + max_seconds
    reason = None
    while stack and reason is None:
        check()
        directory = stack.pop()
        try:
            info = directory.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                    or not directory.resolve().is_relative_to(base)):
                skipped += 1
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    check()
                    if time.monotonic() >= deadline:
                        reason = "time_limit"
                        break
                    if scanned >= max_entries:
                        reason = "entry_limit"
                        break
                    scanned += 1
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                            skipped += 1
                            continue
                        path = Path(entry.path)
                        # Also check resolved containment against junction replacement.
                        if not path.resolve().is_relative_to(base):
                            skipped += 1
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            stack.append(path)
                        elif stat.S_ISREG(info.st_mode) and fnmatch.fnmatchcase(entry.name.casefold(), pattern.casefold()):
                            matches.append({"path": str(path), "bytes": info.st_size,
                                            "modified_ns": info.st_mtime_ns})
                            if len(matches) >= limit:
                                reason = "result_limit"
                                break
                    except OSError:
                        skipped += 1
        except OSError:
            skipped += 1
    return {"status": "observed", "root": str(base), "matches": matches,
            "scanned": scanned, "skipped": skipped, "truncated": reason is not None,
            "stop_reason": reason, "complete": reason is None and skipped == 0,
            "verification": "filesystem_metadata", "content_read": False}
