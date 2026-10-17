"""Disposable fixtures for the Clickwarden failure suite (WF-01).

* ``layout``  deterministic seeded fixture specs and the self-contained page
* ``oracle``  fixture-truth classification of an attempt
* ``app``     local HTTP host and the focus-stealer window for live runs

These modules are only used by tests/benchmarks; no runtime code imports them,
and fixture truth never feeds back into a runtime decision.
"""

from .layout import FIXTURE_VERSION, build_layout, layout_hash, render_page  # noqa: F401

__all__ = ["FIXTURE_VERSION", "build_layout", "layout_hash", "render_page"]
