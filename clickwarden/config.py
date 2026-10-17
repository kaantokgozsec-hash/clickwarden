"""Environment settings shared by every module.

Settings use the ``CLICKWARDEN_`` prefix.
"""

import os


def env(name: str, default: str = "") -> str:
    """Return CLICKWARDEN_<name>, or *default* when unset or blank."""
    value = os.environ.get("CLICKWARDEN_" + name)
    if value is not None and value.strip() != "":
        return value.strip()
    return default


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


def env_flag(name: str, default: bool) -> bool:
    value = env(name, "1" if default else "0")
    return value.lower() not in ("0", "false", "no", "off")
