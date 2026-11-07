"""M1 synthetic regression tests. Run from root with python -B -m pytest."""
from argparse import Namespace
from datetime import date
import hashlib
from pathlib import Path
import sys

from netCDF4 import Dataset
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/fdwt/stages/m01_source_audit"))
import audit
from native import check_month_hours, expected_hours, interval_amount, month_bounds, utc_times


def test_terminal_context_requires_midnight_and_contiguous_hours():
    start, stop = month_bounds(2024, 1)
    hours = expected_hours(start, stop)
    check_month_hours(hours[:1], start, stop, start)
    check_month_hours(hours[:24], start, stop, start)
    check_month_hours(hours, start, stop, start)
    for invalid in (hours[1:24], hours[[0, 2]], hours[[0, 0]]):
        with pytest.raises(ValueError):
            check_month_hours(invalid, start, stop, start)
    with pytest.raises(ValueError):
        check_month_hours(hours[:24], start, stop)


def test_leap_month_hours():
    start, stop = month_bounds(2004, 2)
    hours = expected_hours(start, stop)
    assert len(hours) == 29 * 24
    assert hours[-1] + 3600 == expected_hours(stop, date(2004, 3, 2))[0]


@pytest.mark.parametrize("kind,values", [
    ("processing_step_accumulation", -np.arange(1, 25) * 0.000125),
    ("increment", np.full(24, -0.000125)),
    ("interval_total", np.array([-0.003])),
])
def test_accumulation_types_and_sign(kind, values):
    assert interval_amount(values, kind) * -1000 == pytest.approx(3)


def test_missing_midnight_cannot_be_summed():
    with pytest.raises(ValueError, match="next midnight"):
        interval_amount(np.arange(23), "processing_step_accumulation")


def test_duplicate_time():
    with Dataset("synthetic.nc", "w", diskless=True) as ds:
        ds.createDimension("time", 3)
        time = ds.createVariable("time", "i4", ("time",))
        time.units = "hours since 2000-01-01"
        time[:] = [0, 1, 1]
        with pytest.raises(ValueError, match="duplicated"):
            utc_times(time)


def test_corrupt_file_retains_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    source = tmp_path / "invalid.nc"
    source.write_bytes(b"not netcdf")
    row, _ = audit.inspect_file(("era5_land", "instant", source), {})
    assert row["audit_status"] == "failed"
    assert row["sha256"] == hashlib.sha256(b"not netcdf").hexdigest()


def test_redirect_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="unsafe path"):
        audit.safe(tmp_path / ".." / "outside")


def test_test_bundle_rejected_by_production(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    run = tmp_path / "metadata/runs/test__synthetic"
    run.mkdir(parents=True)
    (run / "m0_complete.json").write_bytes(audit.canonical(dict(
        manifest_version=1, stage="m00_protocol", run_id=run.name, validation="passed",
        artifacts=dict.fromkeys(audit.M0_MEMBERS), identity={"mode": "test"})))
    with pytest.raises(ValueError, match="production M0 bundle"):
        audit.load_m0(run / "run_config.json", "production")


def test_terminal_gap_and_optional_failure_do_not_invalidate_state(monkeypatch):
    records = []
    for stream in audit.ERA:
        for month in ("11", "12"):
            path = Path(f"ERA5-Land_{'instant' if stream == 'instant' else 'accumulated'}_2023_{month}.nc")
            end = f"2023-{month}-{'30' if month == '11' else '31'}T23:00:00Z"
            row = dict(product_code="era5_land", file_path=str(path), audit_status="passed",
                       time_end=end, time_start=end, spatial_extent=[], crs="EPSG:4326")
            records.append((("era5_land", stream, path), (row, {})))
    bad = dict(audit_status="failed", file_path="x.nc", audit_note="corrupt")
    records.append((("gleam", "Et", Path("Et_2023_GLEAM_v4.3a.nc")), (bad, {})))
    monkeypatch.setattr(audit, "accumulation_sample",
                        lambda _, y, m, stream: dict.fromkeys(audit.ERA[stream], {"days": 3}))
    progress = audit.Progress(Namespace(progress="plain"), "test", "attempt", 7, None)
    _, missing, release, _ = audit.audit_coverage(
        records, ("era5_land", "gleam"), date(2023, 12, 1), date(2023, 12, 31),
        {"cell_center_bounds": [100, 33, 115, 42]}, progress)
    assert release["era5_land_instant"]["status"] == "passed"
    assert release["era5_land_accumulated"]["usable_through"] == "2023-12-30"
    assert release["era5_land_accumulated"]["status"] == "blocked"
    assert any(row["reason"] == "missing_next_midnight" and row["required"] for row in missing)
    assert all(not row["required"] for row in missing if row["product"] == "gleam")


def test_publish_conflict_preserves_target_and_can_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    work, target = tmp_path / "work", tmp_path / "target"
    work.mkdir()
    target.mkdir()
    (work / "report.csv").write_text("abc")
    manifest = {"attempt_id": "attempt"}
    with pytest.raises(ValueError, match="already exists"):
        audit.publish(work, target, manifest)
    assert not (work / "m1_complete.json").exists()
    assert list(target.iterdir()) == []
    target.rmdir()
    audit.publish(work, target, manifest)
    assert (target / "m1_complete.json").is_file()


def test_interrupted_publish_drops_work_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    work, target = tmp_path / "work", tmp_path / "target"
    work.mkdir()
    (work / "report.csv").write_text("abc")

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(audit.os, "rename", interrupt)
    with pytest.raises(KeyboardInterrupt):
        audit.publish(work, target, {"attempt_id": "attempt"})
    assert not target.exists()
    assert not (work / "m1_complete.json").exists()
    assert not target.with_suffix(".lock").exists()
