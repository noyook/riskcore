"""Pytest bootstrap: put every area implementation package on sys.path.

Physical layout follows AGENTS.md — code lands under areas/<area>/ — so tests
import the area packages directly (eventlog, riskfn, executor, disclosure,
projection) without an installed distribution.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _area in ("event-log", "risk-function", "executor", "disclosure", "projection", "verification"):
    _p = ROOT / "areas" / _area
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
