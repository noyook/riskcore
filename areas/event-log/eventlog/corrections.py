"""Correction-chain resolution — shared by the projector and the disclosure layer.

Rule (event-schema.md §correction 链规则): the original event is never
modified; a correction supersedes its payload for all READERS. Both the
projector (fact derivation) and the disclosure layer (evidence rendering)
must therefore see the same EFFECTIVE payload — otherwise the evidence chain
would contradict the score.

Semantics:
- correction events are dropped from the stream (meta-events);
- when several corrections target the same event, the latest by seq wins;
- the corrected payload is validated against the target's type model;
- provenance stays the original event's (audit lineage: the correction itself
  carries who/why).
"""
from __future__ import annotations

from .envelope import EventEnvelope, StoredEvent, parse_payload
from .types import EventType


def apply_corrections(events: list[StoredEvent]) -> list[StoredEvent]:
    """Return the event stream with correction chains applied (corrections dropped)."""
    corrections: dict[str, tuple[int, dict]] = {}
    for ev in events:
        if ev.type is EventType.CORRECTION:
            target = str(ev.payload["corrects_event_id"])
            current = corrections.get(target)
            if current is None or ev.seq > current[0]:
                corrections[target] = (ev.seq, ev.payload["corrected_payload"])
    if not corrections:
        return [e for e in events if e.type is not EventType.CORRECTION]

    out: list[StoredEvent] = []
    for ev in events:
        if ev.type is EventType.CORRECTION:
            continue
        hit = corrections.get(str(ev.event_id))
        if hit is not None:
            parse_payload(ev.type, hit[1])  # fail loud on malformed correction
            ev = ev.model_copy(update={"payload": hit[1]})
        out.append(ev)
    return out
