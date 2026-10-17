"""Reproducible baseline recording for the Clickwarden fixture failure suite.

WF-01 records what actually happened on a disposable fixture instead of trusting
a caller- or model-supplied success claim.  This package is offline-importable:
it never touches the desktop by itself.  A live runner (``tests/live_baseline.py``)
records one entry per attempted fixture interaction and writes a JSON report
with the seed, layout hash, environment, timings, failures and separate outcome
counters.

Outcome vocabulary (exactly one primary outcome per attempt):

* ``success``         the intended target received the intended effect
* ``missing_target``  a complete observation showed the target absent and no input was dispatched
* ``declined``        a guard or policy refused before any input was dispatched
* ``wrong_dispatch``  input was dispatched but the intended effect did not follow (counted wrong even if the screen changed)
* ``unknown``         fixture-side evidence cannot establish either way

``false_success`` is tracked as a separate flag: the caller (or a planner saying
"done") claimed success while the fixture oracle did not confirm it.  A screen
change, a model PASS or a caller's ``completed`` result never overrides the
fixture-side record.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import tempfile
import time
from collections import Counter

SCHEMA_VERSION = 1

OUTCOMES = ("success", "missing_target", "declined", "wrong_dispatch", "unknown")


class OutcomeCounters:
    """Separate counters for fixture truth, independent of any success claim."""

    def __init__(self):
        self.outcomes = Counter({name: 0 for name in OUTCOMES})
        self.flags = Counter()

    def add(self, outcome: str, false_success: bool = False) -> None:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome '{outcome}'")
        self.outcomes[outcome] += 1
        if false_success:
            self.flags["false_success"] += 1

    def as_dict(self) -> dict:
        return {"outcomes": dict(self.outcomes), "flags": dict(self.flags),
                "total": sum(self.outcomes.values())}


def timing_summary(samples) -> dict:
    """p50/p95/max over a list of millisecond samples; never invents samples."""
    values = sorted(float(v) for v in samples if v is not None)
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None, "total_ms": 0.0}

    def pick(fraction: float) -> float:
        return values[min(len(values) - 1, int(len(values) * fraction))]

    return {"count": len(values), "p50_ms": round(pick(.5), 2), "p95_ms": round(pick(.95), 2),
            "max_ms": round(values[-1], 2), "total_ms": round(sum(values), 2)}


def completion_claim(result) -> bool:
    """True only when a result explicitly claims an outcome, not merely dispatch.

    Clickwarden's dispatch acknowledgements are honest about their limits:
    ``action_status='dispatched'``, ``verification='unverified'`` and
    ``task_verified=False``.  Those are not completion claims.  A planner
    "done" (task ``status='completed'`` with a summary), a verified result, or
    an explicit ``task_verified=True`` are claims, and a claim is exactly what
    the fixture oracle must check rather than trust.
    """
    if not isinstance(result, dict):
        return False
    if result.get("task_verified") is True:
        return True
    if result.get("verification") == "unverified":
        return False
    return str(result.get("status", "")).lower() in ("completed", "verified")


def environment(screen: dict | None = None, apps: dict | None = None) -> dict:
    """Static environment facts plus caller-supplied screen/app inventory.

    The live runner passes the actual screen and application facts it observed;
    nothing here is guessed.  No credentials, paths or typed values are recorded.
    """
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "screen": dict(screen or {}),
        "apps": dict(apps or {}),
    }


class BaselineRecorder:
    """Accumulates one reproducible baseline run and writes it atomically."""

    def __init__(self, seed: int, layout_hash: str, *, fixture_version: int = 1,
                 suite: str = "wf01", model: str | None = None,
                 strategy: str = "native_uia", screen: dict | None = None,
                 apps: dict | None = None, runtime: dict | None = None):
        if type(seed) is not int:
            raise ValueError("seed must be an integer")
        self.seed = seed
        self.layout_hash = str(layout_hash)
        self.fixture_version = int(fixture_version)
        self.suite = suite
        self.model = model
        self.strategy = strategy
        self.environment = environment(screen=screen, apps=apps)
        self.runtime = dict(runtime or {})
        self.started = time.perf_counter()
        self.steps: list[dict] = []
        self.failures: list[dict] = []
        self.counters = OutcomeCounters()

    def step(self, name: str, *, status: str, duration_ms: float | None = None,
             strategy: str | None = None, target: str = "", expected: str = "",
             actual: str = "", deliberate: bool = False, detail: str = "") -> dict:
        entry = {"step": name, "status": status, "target": target, "expected": expected,
                 "actual": actual, "deliberate": bool(deliberate),
                 "strategy": strategy or self.strategy,
                 "duration_ms": None if duration_ms is None else round(float(duration_ms), 2),
                 "detail": detail[:300]}
        self.steps.append(entry)
        return entry

    def record(self, name: str, verdict: dict, *, strategy: str | None = None,
               duration_ms: float | None = None, target: str = "", expected: str = "",
               deliberate: bool = False, detail: str = "") -> dict:
        """Count one fixture verdict and append its report entry."""
        outcome = verdict.get("outcome")
        false_success = bool(verdict.get("false_success"))
        self.counters.add(outcome, false_success)
        entry = self.step(name, status=outcome, duration_ms=duration_ms, strategy=strategy,
                          target=target or verdict.get("intended", ""), expected=expected,
                          actual=outcome, deliberate=deliberate,
                          detail=detail or verdict.get("reason", ""))
        entry["false_success"] = false_success
        return entry

    def failure(self, name: str, error: str) -> dict:
        entry = {"step": name, "error": str(error)[:300]}
        self.failures.append(entry)
        return entry

    def recorded(self, name: str) -> bool:
        """Whether this scenario already produced its one structured result."""
        return any(step["step"] == name for step in self.steps)

    def report(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "suite": self.suite,
            "seed": self.seed,
            "layout_hash": self.layout_hash,
            "fixture_version": self.fixture_version,
            "model": self.model,
            "strategy": self.strategy,
            "runtime": dict(self.runtime),
            "environment": self.environment,
            "counters": self.counters.as_dict(),
            "timings": timing_summary([s["duration_ms"] for s in self.steps]),
            "steps": self.steps,
            "failures": self.failures,
            "wall_seconds": round(time.perf_counter() - self.started, 2),
        }

    def write(self, path, extra: dict | None = None) -> Path:
        """Write the JSON report atomically; ``last_report`` keeps the exact copy."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        report = self.report()
        if extra:
            report.update(extra)
        self.last_report = report
        handle, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                        dir=str(path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=False)
                stream.write("\n")
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.remove(temp)
        return path
