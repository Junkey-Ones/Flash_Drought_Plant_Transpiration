"""M2 manifest validation and monthly read-only ERA5-Land access.

Inputs: frozen M0/M1 metadata and audited NetCDF files. Returns UTC daily arrays
in canonical units, with NaN for missing values. Used by preprocess.py, no CLI.
"""

from contextlib import contextmanager
import csv
from datetime import date
from pathlib import Path
import sys

from netCDF4 import Dataset
import numpy as np

from physics import DAY, FLUX, daily_state, decoded, era_storage, water_mm

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "m01_source_audit"))
import audit as m1
from native import ERA, check_month_hours, fingerprint, month_bounds, utc_times


M1_MEMBERS = {
    "source_file_manifest.csv", "source_variable_map.csv", "station_metadata.csv",
    "m1_data_coverage.csv", "m1_missing_files.csv", "m1_release.json",
}
STEP = "processing_step_accumulation"
REQUIRED = ("instant", "accumulated")
FACTORS = dict(tp=1000, e=-1000, pev=-1000, sro=1000)


def csv_rows(path):
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def member_hashes(directory, members, names):
    """Check the exact inventory and each member's bytes inside its owner directory."""
    m1.require(set(members) == names, f"unexpected members in {directory}")
    for name, expected in members.items():
        m1.require(fingerprint(m1.safe(directory / name)) == expected,
                   f"artifact mismatch: {directory / name}")


def stream_end(release, stream, start, end):
    """Require a released primary stream, or return the optional runoff prefix."""
    gate = release.get("era5_land_" + stream, {})
    through = gate.get("usable_through")
    if (gate.get("sample_verified") is True and through and
            gate["requested_start"] <= start.isoformat() <= through <= gate["requested_end"]):
        if stream not in REQUIRED or (gate["status"] == "passed" and through >= end.isoformat()):
            return min(end, date.fromisoformat(through))
    m1.require(stream not in REQUIRED, f"M1 has not released the requested {stream} scope")
    return None


def relative_source(stream, month):
    prefix = "instant" if stream == "instant" else "accumulated"
    return f"data_raw/era5_land/{stream}/ERA5-Land_{prefix}_{month:%Y_%m}.nc"


def file_stamp(path):
    info = path.stat()
    return info.st_size, info.st_mtime_ns, info.st_ino


def load_sources(manifest_path, mode, m0, start, end):
    """Bind the published M1 manifest, mappings and exact files needed by M2."""
    path = m1.safe(manifest_path)
    directory = path.parent
    run = directory.parent.name
    m1.require(path.name == "m1_complete.json" and directory ==
               m1.ROOT / "data" / "frozen" / run / m1.STAGE, "expected a published M1 manifest")
    m1_hash = fingerprint(path)["sha256"]
    complete = m1.read_json(path)
    identity = complete["identity"]
    m1.require(complete["run_id"] == run and complete["stage"] == m1.STAGE and
               complete["manifest_version"] == 1 and complete["validation"] == "passed" and
               complete["schema_version"] == "v1.3.1" and identity["mode"] == mode and
               ("era5_land" in identity["products"]) and
               ((run == m0["m0_run_id"]) if mode == "production" else run.startswith("test__")) and
               all(identity[key] == m0[key] for key in ("m0_run_id", "m0_sha256", "environment_hash")),
               "M1 scope, mode or M0/environment identity mismatch")
    m1.require(identity["start"] <= start.isoformat() <= end.isoformat() <= identity["end"],
               "complete pentad context lies outside M1 scope")
    member_hashes(directory, complete["artifacts"], M1_MEMBERS)
    report = m1.read_json(directory / "m1_release.json")
    m1.require(report["release"] == complete["release"] and not report["required_blocked"],
               "M1 release report disagrees with completion manifest")
    rows = csv_rows(directory / "source_file_manifest.csv")
    files = {row["file_path"]: row for row in rows}
    inventory = [[row["file_path"], int(row["file_size_bytes"]) if row["file_size_bytes"] else "",
                  row["sha256"]] for row in rows]
    m1.require(len(files) == len(rows) and m1.object_hash(inventory) ==
               complete["input_inventory_hash"] == report["input_inventory_hash"],
               "M1 input inventory mismatch")

    scopes = {stream: stream_end(report["release"], stream, start, end) for stream in ERA}
    selected, mappings = {}, {}
    map_rows = csv_rows(directory / "source_variable_map.csv")
    for stream, last in scopes.items():
        if last is None:
            continue
        kind = "instantaneous" if stream == "instant" else STEP
        final_time = last if stream == "instant" else last + DAY
        for month in m1.covered_months(start, final_time):
            rel = relative_source(stream, month)
            row = files.get(rel)
            stop = month_bounds(month.year, month.month)[1]
            first_hour = month.isoformat() + "T00:00:00Z"
            last_hour = (stop - DAY).isoformat() + "T23:00:00Z"
            terminal_context = stream != "instant" and month.isoformat() > identity["end"]
            valid_end = row and (first_hour <= row["time_end"] <= last_hour if terminal_context
                                 else row["time_end"] == last_hour)
            m1.require(row and row["product_code"] == "era5_land" and
                       row["audit_status"] == "passed" and row["accumulation_type"] == kind and
                       row["time_start"] == first_hour and valid_end and
                       row["timestamp_meaning"] == "UTC valid time" and
                       len(row["sha256"]) == 64, f"missing or incompatible M1 source row: {rel}")
            selected[stream, month] = (m1.safe(m1.ROOT / rel), {
                "bytes": int(row["file_size_bytes"]), "sha256": row["sha256"]})
            for name, unit in ERA[stream].items():
                field, out_unit, sign = m1.ERA_MAP[name]
                candidates = [entry for entry in map_rows if entry["product_code"] == "era5_land" and
                              entry["collection_version"] == row["collection_version"] and
                              entry["note"] == stream and entry["native_variable_name"] == name and
                              entry["mapping_status"] == "verified"]
                m1.require(candidates and all(
                    (entry["canonical_field"], entry["native_unit"], entry["canonical_unit"],
                     entry["sign_rule"], entry["accumulation_type"]) == (field, unit, out_unit, sign, kind)
                    for entry in candidates), f"unverified or conflicting M1 mapping: {name}")
                mappings[stream, month, name] = {(float(entry["scale_factor"]), float(entry["add_offset"]))
                                                for entry in candidates}
                if stream != "instant":
                    m1.require(name in report["accumulation_evidence"], f"missing M1 evidence: {name}")
    m1.require(fingerprint(path)["sha256"] == m1_hash, "M1 completion changed during preflight")
    return selected, mappings, scopes, m1_hash, report["accumulation_evidence"]


class MonthlyReader:
    """Cache one month of daily results plus any needed next-month endpoints."""

    def __init__(self, selected, mappings, scopes, start, end, lat, lon, progress):
        self.selected, self.mappings, self.scopes = selected, mappings, scopes
        self.start, self.end, self.lat, self.lon, self.progress = start, end, lat, lon, progress
        self.verified = {}
        self.month = self.values = None

    @contextmanager
    def open_source(self, stream, month):
        path, expected = self.selected[stream, month]
        self.progress(f"正在读取月文件 {path.name}")
        stamp = file_stamp(path)
        if path not in self.verified:
            m1.require(fingerprint(path) == expected, f"M1 raw fingerprint mismatch: {path}")
            self.verified[path] = stamp
        m1.require(stamp == self.verified[path], f"source changed since verification: {path}")
        with Dataset(path, "r") as ds:
            stop = month_bounds(month.year, month.month)[1]
            terminal_boundary = month if stream != "instant" and month == self.end + DAY else None
            check_month_hours(utc_times(ds["valid_time"]), month, stop, terminal_boundary)
            for name, axis in (("latitude", self.lat), ("longitude", self.lon)):
                m1.require(np.allclose(decoded(ds[name][:]), axis, atol=1e-5, rtol=0),
                           f"coordinate mismatch: {path} {name}")
            for name, unit in ERA[stream].items():
                var = ds[name]
                packing = (float(getattr(var, "scale_factor", 1)), float(getattr(var, "add_offset", 0)))
                m1.require(var.units == unit and var.dimensions == ("valid_time", "latitude", "longitude")
                           and packing in self.mappings[stream, month, name],
                           f"unverified native field: {path} {name}")
            yield ds
        m1.require(stamp == file_stamp(path), f"source changed during reading: {path}")

    def fluxes(self, stream, month, first, stop):
        """Read each day's next midnight, appending the next file's first hour at month end."""
        through = self.scopes[stream]
        size = (stop - first).days
        shape = (size, len(self.lat), len(self.lon))
        result = {m1.ERA_MAP[name][0]: np.full(shape, np.nan) for name in ERA[stream]}
        if through is None or first > through:
            return result
        boundary = month_bounds(month.year, month.month)[1]
        last = min(stop - DAY, through)
        lo = ((first - month).days + 1) * 24
        hi = min(((last - month).days + 2) * 24, (boundary - month).days * 24)
        if hi > lo:
            with self.open_source(stream, month) as ds:
                for name in ERA[stream]:
                    values = decoded(ds[name][lo:hi:24], low=0 if name in ("tp", "sro") else -np.inf)
                    result[m1.ERA_MAP[name][0]][:len(values)] = water_mm(values, FACTORS[name])
        if last + DAY == boundary:
            with self.open_source(stream, boundary) as ds:
                for name in ERA[stream]:
                    value = decoded(ds[name][0], low=0 if name in ("tp", "sro") else -np.inf)
                    result[m1.ERA_MAP[name][0]][(last - first).days] = water_mm(value, FACTORS[name])
        return result

    def day(self, day):
        """Return one date as flattened fields in frozen latitude/longitude order."""
        month = day.replace(day=1)
        if self.month != month:
            first = max(month, self.start)
            stop = min(month_bounds(day.year, day.month)[1], self.end + DAY)
            begin = (first - month).days * 24
            finish = (stop - month).days * 24
            with self.open_source("instant", month) as ds:
                layers = [daily_state(decoded(ds[name][begin:finish], 0, 1))
                          for name in ("swvl1", "swvl2", "swvl3", "swvl4")]
                soil, deep = era_storage(layers)
                temp = daily_state(decoded(ds["t2m"][begin:finish], low=0)) - 273.15
            values = dict(sm_0_100_mm=soil, sm_deep_mm=deep, t2m_c=temp)
            for stream in ERA:
                if stream != "instant":
                    values.update(self.fluxes(stream, month, first, stop))
            for field in FLUX:
                if field not in values:
                    values[field] = np.full_like(soil, np.nan)
            self.month, self.first, self.values = month, first, values
        return {name: value[(day - self.first).days].ravel() for name, value in self.values.items()}

    def verify_unchanged(self):
        """Fail closed if any consumed or planned file changed during the attempt."""
        for path, expected in self.selected.values():
            if path in self.verified:
                m1.require(file_stamp(path) == self.verified[path], f"source changed: {path}")
            else:
                # A first-of-month file may be required by M1 coverage but contribute
                # no hours when the only requested day is the last day of that month.
                m1.require(fingerprint(path) == expected, f"M1 raw fingerprint mismatch: {path}")

