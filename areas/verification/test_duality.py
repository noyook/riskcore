"""W-009 framework tests — 黄金重放门禁 + 双跑比对报告格式。"""
from __future__ import annotations

from datetime import date

import pytest
from areas.verification.duality import SyntheticLegacySource, compare, level_for_r


def test_level_mapping_matches_fourcolor() -> None:
    th = {"alert": 400, "restrict": 600, "lock": 800}
    assert level_for_r(1000, True, th) == "red"
    assert level_for_r(850, False, th) == "red"
    assert level_for_r(650, False, th) == "orange"
    assert level_for_r(450, False, th) == "yellow"
    assert level_for_r(100, False, th) == "blue"


def test_dual_run_report_agreement_and_diffs() -> None:
    day = date(2026, 10, 15)
    new_levels = {
        "veh-1": ("red", 1000),
        "veh-2": ("orange", 680),
        "veh-3": ("blue", 100),
        "veh-4": ("yellow", 405),  # 贴近 400 边界 → boundary
    }
    legacy = SyntheticLegacySource({"veh-1": "red", "veh-2": "yellow", "veh-3": "blue", "veh-4": "blue"})
    report = compare(day, new_levels, legacy)

    assert report.total == 4 and report.agreed == 2
    assert report.agreement_rate_milli == 500
    by_id = {d.object_id: d for d in report.diffs}
    assert by_id["veh-2"].classification == "explained_new_higher"
    assert by_id["veh-4"].classification == "boundary"

    md = report.render_markdown("v1")
    assert "一致率 50.0%" in md and "裁决台账" in md
    assert "不可解释差异：0 条" in md


def test_dual_run_missing_legacy_object_is_unexplained() -> None:
    day = date(2026, 10, 15)
    report = compare(day, {"veh-x": ("red", 900)}, SyntheticLegacySource({}))
    assert report.diffs[0].classification == "unexplained"
    assert "不可解释差异：1 条" in report.render_markdown("v1")


# ------------------------------------------------------- file landing adapter


def test_file_landing_source_csv(tmp_path) -> None:
    from legacy_source import FileLandingLegacySource, LegacySnapshotError

    day = date(2026, 10, 15)
    (tmp_path / f"{day.isoformat()}.csv").write_text(
        "object_id,level\nveh-1,red\nveh-2,yellow\n", encoding="utf-8"
    )
    src = FileLandingLegacySource(tmp_path)
    assert src.fetch_levels(day) == {"veh-1": "red", "veh-2": "yellow"}


def test_file_landing_source_missing_and_invalid(tmp_path) -> None:
    from legacy_source import FileLandingLegacySource, LegacySnapshotError

    src = FileLandingLegacySource(tmp_path)
    with pytest.raises(LegacySnapshotError):
        src.fetch_levels(date(2026, 10, 15))
    (tmp_path / "2026-10-16.json").write_text('{"veh-1": "purple"}', encoding="utf-8")
    with pytest.raises(LegacySnapshotError, match="unknown legacy levels"):
        src.fetch_levels(date(2026, 10, 16))


# ------------------------------------------------------- file landing adapter


def test_file_landing_source_csv(tmp_path) -> None:
    from legacy_source import FileLandingLegacySource, LegacySnapshotError

    day = date(2026, 10, 15)
    (tmp_path / f"{day.isoformat()}.csv").write_text(
        "object_id,level\nveh-1,red\nveh-2,yellow\n", encoding="utf-8"
    )
    src = FileLandingLegacySource(tmp_path)
    assert src.fetch_levels(day) == {"veh-1": "red", "veh-2": "yellow"}


def test_file_landing_source_missing_and_invalid(tmp_path) -> None:
    from legacy_source import FileLandingLegacySource, LegacySnapshotError

    src = FileLandingLegacySource(tmp_path)
    with pytest.raises(LegacySnapshotError):
        src.fetch_levels(date(2026, 10, 15))
    (tmp_path / "2026-10-16.json").write_text('{"veh-1": "purple"}', encoding="utf-8")
    with pytest.raises(LegacySnapshotError, match="unknown legacy levels"):
        src.fetch_levels(date(2026, 10, 16))
