"""Golden replay gate — wired into pytest so CI enforces it (test-plan.md §1)."""
from __future__ import annotations

from areas.verification.replay import verify


def test_golden_replay_bit_identical() -> None:
    assert verify(verbose=False) == 0
