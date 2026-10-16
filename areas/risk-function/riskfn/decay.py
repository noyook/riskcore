"""Integer decay — 2^(-age/half_life) without floats.

q = milli-quanta of age relative to the half-life:
    q = (age_days * 1000) // half_life_days
factor_milli = TABLE[q % 1000] >> (q // 1000)   ∈ [0, 1000]

Non-increasing in q (older evidence never counts more) — asserted by the
table generator and by the property suite. All arithmetic is integer.
"""
from __future__ import annotations

from ._decay_table import DECAY_TABLE_MILLI


def decay_milli(q: int) -> int:
    """Decay factor in milli for age quanta q (q = age_milli / half_life_milli)."""
    if q <= 0:
        return 1000
    return DECAY_TABLE_MILLI[q % 1000] >> (q // 1000)


def age_quanta_milli(age_days: int, half_life_days: int) -> int:
    """Convert an age in days to milli-quanta of the given half-life."""
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    if age_days <= 0:
        return 0
    return (age_days * 1000) // half_life_days


def decayed_value_milli(value: int, age_days: int, half_life_days: int) -> int:
    """value * decay(age) in milli units (truncated per event, deterministic)."""
    if value <= 0:
        return 0
    return (value * decay_milli(age_quanta_milli(age_days, half_life_days))) // 1000
