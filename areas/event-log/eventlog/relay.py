"""Outbox relay — asynchronous, at-least-once delivery with idempotent consumers.

Pattern (event-schema.md §写入幂等):
    business tx  → event_log row + outbox row (same store, same tx)
    relay        → pending outbox rows → consumer channel
    consumer     → dedupes by event_id (unique index), duplicates just acked

The relay never mutates the log. Delivery state lives only on the outbox.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Protocol

from .store import EventLogStore, OutboxRow

MAX_ATTEMPTS = 5


class Consumer(Protocol):
    def __call__(self, row: OutboxRow) -> None:  # may raise on transient failure
        ...


def relay_once(
    store: EventLogStore,
    deliver: Consumer,
    *,
    now: datetime | None = None,
    limit: int = 100,
    max_attempts: int = MAX_ATTEMPTS,
) -> dict[str, int]:
    """Deliver pending outbox rows. Returns {delivered, retried, dead}."""
    now = now or datetime.now(timezone.utc)
    stats = {"delivered": 0, "retried": 0, "dead": 0}
    for row in store.pending_outbox(limit=limit):
        try:
            deliver(row)
        except Exception as exc:  # transient consumer failure → retry, then dead-letter
            if row.attempts + 1 >= max_attempts:
                store.mark_dead(row.id, f"{type(exc).__name__}: {exc}")
                stats["dead"] += 1
            else:
                store.bump_attempts(row.id, f"{type(exc).__name__}: {exc}")
                stats["retried"] += 1
            continue
        store.mark_delivered(row.id, now)
        stats["delivered"] += 1
    return stats


def make_logging_consumer(log: Callable[[str], None] | None = None) -> Consumer:
    """Reference consumer: acks everything, logs the event id (idempotent)."""

    def _deliver(row: OutboxRow) -> None:
        if log:
            log(f"relayed event_id={row.event_id} channel={row.channel}")

    return _deliver
