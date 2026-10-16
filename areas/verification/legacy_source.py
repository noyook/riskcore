"""Legacy snapshot source — file landing-zone adapter (W-009).

The legacy system (现网) exports its daily risk_code_assignments snapshot into a
landing directory; this adapter reads it. No direct production DB access is
required from the kernel side — the export is an ops-side cron job.

Expected file: <dir>/<YYYY-MM-DD>.csv  (or .json)
CSV columns: object_id, level        level ∈ blue|yellow|orange|red
JSON:        {"object_id": "level", ...}
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

LEVELS = ("blue", "yellow", "orange", "red")


class LegacySnapshotError(RuntimeError):
    pass


class FileLandingLegacySource:
    """Reads the daily legacy snapshot from a landing directory."""

    def __init__(self, landing_dir: str | Path) -> None:
        self.landing_dir = Path(landing_dir)

    def path_for(self, day: date) -> Path:
        return self.landing_dir / f"{day.isoformat()}.csv"

    def fetch_levels(self, day: date) -> dict[str, str]:
        path = self.path_for(day)
        if not path.exists():
            json_path = path.with_suffix(".json")
            if json_path.exists():
                data = json.loads(json_path.read_text(encoding="utf-8"))
                return self._validate(data)
            raise LegacySnapshotError(f"legacy snapshot for {day} not found in {self.landing_dir}")
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames or "object_id" not in reader.fieldnames or "level" not in reader.fieldnames:
                raise LegacySnapshotError(f"{path}: need columns object_id,level")
            data = {row["object_id"].strip(): row["level"].strip() for row in reader if row.get("object_id")}
        return self._validate(data)

    @staticmethod
    def _validate(data: dict[str, str]) -> dict[str, str]:
        bad = {k: v for k, v in data.items() if v not in LEVELS}
        if bad:
            raise LegacySnapshotError(f"unknown legacy levels: {bad}")
        return data
