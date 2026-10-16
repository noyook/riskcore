"""Monotone factor mappings g_i(fact) — integer milli only.

Two shapes (policy.factor_params):
- ratio:  g = min(value, cap) / cap          (count-like facts, milli-scale input)
- expiry: g = (warn_days - days) / warn_days for 0 < days <= warn_days, else 0
          (days <= 0 is a veto, handled before factors)

Both are monotone in the "worsening" direction, which the property suite
asserts end-to-end (single fact worsens → r does not decrease).
"""
from __future__ import annotations

from .policy import FactorExpiry, FactorRatio


def g_ratio_milli(value_milli: int, params: FactorRatio) -> int:
    """min(value, cap)/cap in milli; value_milli is milli-scale (value*1000)."""
    if value_milli <= 0:
        return 0
    cap_milli = params.cap * 1000
    capped = value_milli if value_milli < cap_milli else cap_milli
    return (capped * 1000) // cap_milli


def g_expiry(days_remaining: int, params: FactorExpiry) -> int:
    """(warn - days)/warn in milli for 0 < days <= warn; 0 beyond the warn window."""
    if days_remaining <= 0:
        # caller must have vetoed already; defensive: max risk inside window
        return 1000
    if days_remaining >= params.warn_days:
        return 0
    return ((params.warn_days - days_remaining) * 1000) // params.warn_days
