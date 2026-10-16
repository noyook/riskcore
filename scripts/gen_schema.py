#!/usr/bin/env python3
"""Regenerate the envelope JSON Schema from the executable pydantic contract.

Usage: python3 scripts/gen_schema.py
The pydantic models in areas/event-log/eventlog/envelope.py are the source of
truth; this script materializes them as JSON Schema for external producers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "areas" / "event-log"))

from eventlog.envelope import EventEnvelope  # noqa: E402

OUT = ROOT / "areas" / "event-log" / "schemas" / "event-envelope.schema.json"


def main() -> None:
    schema = EventEnvelope.model_json_schema()
    schema["$id"] = "https://riskcore.local/schemas/event-envelope-1.json"
    schema["title"] = "RiskCore Event Envelope v1"
    OUT.write_text(
        json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
