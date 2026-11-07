"""M2 synthetic fixtures for leap days, monthly boundaries, QA and publication.

From the repository root: python -B -m pytest tests/test_m2_preprocess.py.
No external M0/M1 bundle or original observations are required.
"""

from datetime import date
from pathlib import Path
import sys

from netCDF4 import Dataset
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/fdwt/stages/m02_preprocess"))
from inputs import ERA, MonthlyReader, fingerprint, m1, month_bounds, stream_end
from physics import calendar_rows
import preprocess as m2


def write_month(tmp_path, stream, month, hours=None):
    path = tmp_path / f"{stream}_{month:%Y_%m}.nc"
    stop = month_bounds(month.year, month.month)[1]
    with Dataset(path, "w") as ds:
        hours = (stop - month).days * 24 if hours is None else hours
        for dim, length in (("valid_time", hours), ("latitude", 2), ("longitude", 2)):
            ds.createDimension(dim, length)
        clock = ds.createVariable("valid_time", "i4", ("valid_time",))
        clock.units = f"hours since {month.isoformat()} 00:00:00"
        clock[:] = np.arange(hours)
        ds.createVariable("latitude", "f8", ("latitude",))[:] = [42, 41.9]
        ds.createVariable("longitude", "f8", ("longitude",))[:] = [100, 100.1]
        for name, unit in ERA[stream].items():
            var = ds.createVariable(name, "f8", ("valid_time", "latitude", "longitude"),
                                    fill_value=-9999)
            var.units = unit
            if name.startswith("swvl"):
                var[:] = int(name[-1]) / 10
            elif name == "t2m":
                var[:] = 293.15
            elif name == "tp":
                var[:] = 0.002
                var[0] = 0.008 if month.month == 2 else 0.005
                if month.month == 3:
                    var[0, 0, 1] = -9999
                    var[0, 1, 0] = -0.001
            else:
                var[:] = -0.003
                if name == "e" and month.month == 3:
                    var[0] = 0.001
    return path


@pytest.mark.parametrize("boundary_hours", [None, 1, 24])
def test_read_actual_netcdf_month_boundary(tmp_path, boundary_hours):
    feb, mar = date(2020, 2, 1), date(2020, 3, 1)
    leap = date(2020, 2, 29)
    keys = [("instant", feb), ("accumulated", feb), ("accumulated", mar)]
    selected, mappings = {}, {}
    for stream, month in keys:
        path = write_month(tmp_path, stream, month, boundary_hours if month == mar else None)
        selected[stream, month] = (path, fingerprint(path))
        for name in ERA[stream]:
            mappings[stream, month, name] = {(1.0, 0.0)}
    reader = MonthlyReader(selected, mappings, {"surface_runoff": None, "accumulated": leap},
                           date(2020, 2, 28), leap, np.array([42, 41.9]), np.array([100, 100.1]),
                           lambda _: None)
    previous = reader.day(date(2020, 2, 28))
    last = reader.day(leap)
    np.testing.assert_allclose(previous["p_mm"], 2)
    np.testing.assert_allclose(last["p_mm"], [5, np.nan, np.nan, 5], equal_nan=True)
    np.testing.assert_allclose(last["et_loss_mm"], -1)
    np.testing.assert_allclose(last["sm_0_100_mm"], 265)
    np.testing.assert_allclose(last["sm_deep_mm"], 756)
    np.testing.assert_allclose(last["t2m_c"], 20)
    assert np.isnan(last["balance_mm"]).all()
    assert np.isnan(last["runoff_surface_mm"]).all()
    reader.verify_unchanged()
    with selected["accumulated", mar][0].open("ab") as output:
        output.write(b"changed")
    with pytest.raises(ValueError, match="source changed"):
        reader.verify_unchanged()


def test_required_release_and_optional_prefix():
    start, end = date(2023, 12, 26), date(2023, 12, 31)
    gate = dict(status="blocked", requested_start="2023-01-01", requested_end="2023-12-31",
                usable_through="2023-12-30", sample_verified=True)
    with pytest.raises(ValueError, match="not released"):
        stream_end({"era5_land_accumulated": gate}, "accumulated", start, end)
    assert stream_end({"era5_land_surface_runoff": gate}, "surface_runoff", start, end) == date(2023, 12, 30)


def test_calendar_complete_context_and_checks():
    rows = calendar_rows(date(2020, 12, 31), date(2021, 1, 1))
    assert [row["n_days"] for row in rows] == [6, 5]
    assert rows[0]["end_date"] == date(2020, 12, 31)
    assert rows[0]["next_t_abs"] == rows[1]["t_abs"]
    assert all(row["status"] == "passed" for row in m2.numeric_checks())


def test_publication_resume_conflict_and_tampering(tmp_path, monkeypatch):
    monkeypatch.setattr(m1, "ROOT", tmp_path)
    work, target, reports = tmp_path / "work", tmp_path / "frozen", tmp_path / "reports"
    work.mkdir()
    staged = work / "reports"
    staged.mkdir()
    for name in m2.ARTIFACTS:
        (work / name).write_bytes(b"validated data")
    for name in m2.REPORTS:
        (staged / name).write_bytes(b"validated report")
    plan = dict(run_id="test__fixture", attempt_id="0" * 32, identity={"mode": "test"},
                reports=str(reports / ("0" * 32)))
    target.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        m2.publish(work, target, staged, Path(plan["reports"]), plan, {})
    assert not (work / "m2_complete.json").exists()
    assert staged.exists()
    target.rmdir()
    m2.publish(work, target, staged, Path(plan["reports"]), plan, {})
    m2.reuse(target, plan)
    (target / "grid_static_covariates.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="artifact mismatch"):
        m2.reuse(target, plan)


def test_production_rejects_test_m1(tmp_path, monkeypatch):
    monkeypatch.setattr(m1, "ROOT", tmp_path)
    directory = tmp_path / "data/frozen/test__fixture" / m1.STAGE
    directory.mkdir(parents=True)
    manifest = directory / "m1_complete.json"
    manifest.write_bytes(m1.canonical(dict(
        run_id="test__fixture", stage=m1.STAGE, manifest_version=1, schema_version="v1.3.1",
        validation="passed", identity={"mode": "test", "products": ["era5_land"]})))
    with pytest.raises(ValueError, match="scope, mode or M0/environment"):
        m2.load_sources(manifest, "production", {}, date(2004, 2, 25), date(2004, 2, 29))


def test_interrupted_publication_has_no_completion_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(m1, "ROOT", tmp_path)
    work, target = tmp_path / "work", tmp_path / "frozen"
    work.mkdir()
    staged = work / "reports"
    staged.mkdir()
    for name in m2.ARTIFACTS:
        (work / name).write_bytes(b"validated data")
    for name in m2.REPORTS:
        (staged / name).write_bytes(b"validated report")
    attempt = "0" * 32
    reports = tmp_path / "reports" / attempt
    plan = dict(run_id="test__fixture", attempt_id=attempt, identity={"mode": "test"})
    rename = m2.os.rename

    def interrupt(source, dest):
        if dest == target:
            raise KeyboardInterrupt
        rename(source, dest)

    monkeypatch.setattr(m2.os, "rename", interrupt)
    with pytest.raises(KeyboardInterrupt):
        m2.publish(work, target, staged, reports, plan, {})
    assert reports.exists()
    assert not target.exists()
    assert not (work / "m2_complete.json").exists()
    assert not target.with_suffix(".lock").exists()
