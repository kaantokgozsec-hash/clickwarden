"""Clickwarden evaluation tooling (offline-importable, no desktop side effects).

``benchmarks.baseline`` defines the outcome vocabulary and the recorder used by
the WF-01 fixture failure suite in ``tests/live_baseline.py``.
"""

from .baseline import (  # noqa: F401
    OUTCOMES,
    SCHEMA_VERSION,
    BaselineRecorder,
    OutcomeCounters,
    completion_claim,
    environment,
    timing_summary,
)

__all__ = ["OUTCOMES", "SCHEMA_VERSION", "BaselineRecorder", "OutcomeCounters",
           "completion_claim", "environment", "timing_summary"]
