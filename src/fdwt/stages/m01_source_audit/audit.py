r"""M1 original-source audit for the frozen existing-data profile.

Input: M0 run_config.json and verified completion bundle, plus read-only data_raw.
Output: source_file_manifest.csv, source_variable_map.csv, station_metadata.csv,
coverage, missing inputs, sample evidence and M2 release decisions. No daily tables.
Default scope: all inventoried products within the frozen inclusive analysis years.
From the repository root, replace RUN with the M0 run ID:
  .venv\Scripts\python.exe src/fdwt/stages/m01_source_audit/audit.py --mode test --config metadata/runs/RUN/run_config.json --product era5_land --start 2004-02-01 --end 2004-03-02
  .venv\Scripts\python.exe src/fdwt/stages/m01_source_audit/audit.py --mode production --config metadata/runs/RUN/run_config.json
Test outputs: data/frozen/test__.../m01_source_audit and corresponding attempt paths.
Exit codes: 0 passed, 1 audit failure or required scope blocked, 2 invalid preflight,
130 interrupted.
"""

import argparse
from calendar import isleap
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
import csv
import ctypes
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from time import monotonic
import traceback
from uuid import uuid4
from zipfile import BadZipFile

import yaml
from pyhdf.error import HDF4Error

from native import (ERA, MODIS, SOP, TILES, accumulation_sample, fingerprint,
                    modis_metadata, month_bounds, nc_metadata, raster_metadata)

ROOT = Path(__file__).resolve().parents[4]
STAGE = "m01_source_audit"
STAGE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(STAGE_DIR.parent / "m00_protocol"))
from contract import CONFIG_FILES, EXISTING_DATA_PROFILE, canonical, digest, parse, sources
from freeze import environment, git

PRODUCTS = ("era5_land", "modis", "era5_single_levels", "esa_cci_sm",
            "gleam", "gosif", "aridity", "irrigation")
M0_MEMBERS = {*CONFIG_FILES, "boundary_evidence.json", "dependencies.json", "protocol_freeze.json",
              "run_config.json", "run_manifest.json", "source_input_manifest.json"}
SOURCE_URL = {
    "era5_land": "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land",
    "era5_single_levels": "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels",
    "esa_cci_sm": "https://data.ceda.ac.uk/neodc/esacci/soil_moisture/data/daily_files/PASSIVE/v09.2",
    "gosif": "https://data.globalecology.unh.edu/data/GOSIF_v2/8day/",
}
STATION_FIELDS = """source_id network_name station_id_native station_id station_name lon lat crs
elevation_m elevation_datum variable_type variable_name_native depth_top_cm depth_bottom_cm
unit_native fill_value_native qc_scheme_native qc_flag_native timezone utc_offset
daylight_saving_rule day_definition timestamp_start timestamp_end temporal_resolution
access_permission license official_url download_time_utc file_sha256 chirps_fusion_status
independent_holdout_flag acceptance_status exclusion_reason notes""".split()
ERA_MAP = {
    **{f"swvl{layer}": (f"sm_l{layer}_m3m3", "m3 m-3", "identity") for layer in range(1, 5)},
    "t2m": ("t2m_c", "degC", "subtract 273.15"),
    "tp": ("p_mm", "mm", "multiply daily total by 1000"),
    "e": ("et_loss_mm", "mm", "multiply daily total by -1000; preserve condensation"),
    "pev": ("pet_loss_mm", "mm", "multiply daily total by -1000"),
    "sro": ("runoff_surface_mm", "mm", "multiply daily total by 1000"),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def safe(path):
    """Refuse traversal and redirected paths, including NTFS junctions."""
    require(path.is_relative_to(ROOT) and path.resolve() == path, f"unsafe path: {path}")
    return path


def read_json(path):
    return json.loads(path.read_bytes())


def object_hash(value):
    return digest(canonical(value))


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def available_memory():
    """Return available physical memory, in bytes, on the target Windows host."""
    if os.name != "nt":
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    status = (ctypes.c_uint64 * 8)()
    ctypes.cast(status, ctypes.POINTER(ctypes.c_uint32))[0] = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx failed")
    return status[2]


def peak_memory():
    """Return this process's peak resident bytes, as reported by Windows."""
    if os.name != "nt":
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return usage if sys.platform == "darwin" else usage * 1024
    class Counters(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("faults", ctypes.c_uint32),
                    ("peak", ctypes.c_size_t), ("other", ctypes.c_size_t * 7)]
    counters = Counters(size=ctypes.sizeof(Counters))
    if not ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.c_void_p(-1), ctypes.byref(counters), counters.size):
        return None
    return counters.peak


def load_m0(config, mode):
    """Read the immutable M0 bundle and bind its registered scientific sources."""
    config = safe(Path(os.path.abspath(ROOT / config)))
    directory = config.parent
    require(directory.parent == ROOT / "metadata" / "runs" and config.name == "run_config.json",
            "--config must point to metadata/runs/<M0 run_id>/run_config.json")
    m0 = safe(directory / "m0_complete.json")
    raw = m0.read_bytes()
    complete = json.loads(raw)
    require(complete["manifest_version"] == 1 and complete["stage"] == "m00_protocol" and
            complete["run_id"] == directory.name and complete["validation"] == "passed" and
            set(complete["artifacts"]) == M0_MEMBERS, "invalid M0 completion manifest")
    identity = complete["identity"]
    require(identity["mode"] == "production" and not directory.name.startswith("test__"),
            "this profile requires a production M0 bundle")
    for name, expected in complete["artifacts"].items():
        require(fingerprint(safe(directory / name)) == expected, f"M0 artifact mismatch: {name}")
    m0_run = read_json(directory / "run_manifest.json")
    require(all(m0_run.get(key) == value for key, value in identity.items()) and
            m0_run["run_id"] == directory.name, "M0 run identity mismatch")
    require(complete["artifacts"]["run_config.json"]["sha256"] == identity["run_config_hash"] and
            complete["artifacts"]["source_input_manifest.json"]["sha256"] ==
            identity["source_manifest_hash"] and
            all(complete["artifacts"][name]["sha256"] == identity["config_hashes"][name]
                for name in CONFIG_FILES), "M0 identity does not match frozen inputs")
    release = read_json(directory / "protocol_freeze.json")
    require(not release["production_blockers"] and release["release"]["boundary"] == "released",
            "M0 production gates are blocked")
    frozen = read_json(config)
    protocol = sources(ROOT, Path(frozen["protocol_dir"]))
    require(protocol == identity["protocol_hashes"], "M0 protocol identity mismatch")
    params = parse((directory / "params.yaml").read_bytes())
    registry = parse((directory / "schema_registry.yaml").read_bytes())
    require(params["protocol"]["profile"] == EXISTING_DATA_PROFILE and
            registry["status"] == "frozen" and registry["profile"] == EXISTING_DATA_PROFILE,
            "unsupported frozen profile or table contract")
    env = object_hash(environment())
    if mode == "production":
        require(env == identity["environment_hash"], "environment differs from approved M0")
    code = {p.relative_to(ROOT).as_posix(): fingerprint(p)["sha256"]
            for p in sorted(STAGE_DIR.glob("*.py"))}
    for name, approved in identity["code_files"].items():
        source = safe(ROOT / name).read_bytes()
        code[name] = digest(source)
        require(approved in (code[name], digest(source.replace(b"\r\n", b"\n"))),
                f"M0 helper differs from approved code: {name}")
    git_commit = git("rev-parse", "HEAD")
    if mode == "production":
        participating = [*code, "docs/protocol_sources.md"]
        require(not git("status", "--porcelain", "--untracked-files=all", "--", *participating),
                "participating code or protocol registration is uncommitted")
        require(set(git("ls-files", "--", *participating).splitlines()) == set(participating),
                "participating code or protocol registration is untracked")
    return params, registry, dict(m0_run_id=directory.name, m0_sha256=digest(raw),
                                  m0_grid_evidence=read_json(directory / "source_input_manifest.json")["files"],
                                  code=code, git_commit=git_commit, environment_hash=env)


def declared_date(path, product):
    """Return filename date for inventory filtering. File readers verify native dates."""
    patterns = {
        "era5_land": (r"_(\d{4}_\d{2})\.nc$", "%Y_%m"),
        "era5_single_levels": (r"_(\d{4}_\d{2})\.nc$", "%Y_%m"),
        "esa_cci_sm": (r"-(\d{8})000000-", "%Y%m%d"),
        "gleam": (r"_(\d{4})_", "%Y"),
        "gosif": (r"_(\d{7})\.", "%Y%j"),
        "modis": (r"\.A(\d{7})\.", "%Y%j"),
    }
    if product in patterns:
        pattern, fmt = patterns[product]
        match = re.search(pattern, path.name)
        if match:
            try:
                return datetime.strptime(match[1], fmt).date()
            except ValueError:
                pass
    return None


def inventory(products, start, end):
    """Enumerate raw inputs deterministically without following redirected paths."""
    result = []
    for product in products:
        root = safe(ROOT / "data_raw" / product)
        for directory, folders, names in os.walk(root, followlinks=False):
            for name in folders:
                safe(Path(directory) / name)
            for name in names:
                path = safe(Path(directory) / name)
                if path.suffix.lower() not in {".nc", ".hdf", ".tif", ".gz", ".zip"}:
                    continue
                stamp = declared_date(path, product)
                stream = path.relative_to(root).parts[0] if path.parent != root else product
                if product in ("era5_land", "era5_single_levels") and stamp:
                    stop = month_bounds(stamp.year, stamp.month)[1] - timedelta(days=1)
                    boundary = (product == "era5_land" and stream != "instant" and
                                stamp in (month_bounds(end.year, end.month)[1],
                                          (start.replace(day=1) - timedelta(days=1)).replace(day=1)))
                else:
                    span = 366 if product == "gleam" or stream == "MCD12Q1" else 8 if (
                        product in ("modis", "gosif")) else 1
                    stop = stamp + timedelta(days=span - 1) if stamp else None
                    boundary = False
                if stamp is None or (stamp <= end and stop >= start) or boundary:
                    result.append((product, stream, path))
    return sorted(result)


def version(product, stream):
    return {"esa_cci_sm": "09.2", "gleam": "4.3a", "aridity": "3.1", "irrigation": "5",
            "gosif": "2", "modis": "061"}.get(product, "unknown")


def product_code(product, stream):
    return stream if product == "modis" else product


def inspect_file(task, domain, hash_only=False):
    """Return file row and reader evidence. Invalid files remain in the inventory."""
    product, stream, path = task
    relative = path.relative_to(ROOT).as_posix()
    file_hash = {}
    meta = {}
    row = dict(file_id=digest(relative.encode())[:24], product_code=product_code(product, stream),
               collection_version=version(product, stream), file_path=relative, source_url=SOURCE_URL.get(
                   product, "unknown"), download_date="", request_id="unknown", license="unknown",
               access_status="local_copy", audit_status="passed", audit_note="")
    try:
        stat = path.stat()
        file_hash = fingerprint(path)
        if not hash_only:
            if path.suffix == ".nc":
                meta = nc_metadata(path, product, stream, domain)
            elif product == "modis":
                meta = modis_metadata(path, stream)
            else:
                meta = raster_metadata(path)
        after = path.stat()
        require((stat.st_mtime_ns, stat.st_size, stat.st_ino) ==
                (after.st_mtime_ns, after.st_size, after.st_ino), "input changed during inspection")
    except (ValueError, OSError, RuntimeError, KeyError, IndexError, AttributeError, TypeError,
            HDF4Error, BadZipFile) as error:
        row.update(audit_status="failed", audit_note=str(error))
    fields = meta.get("fields", {})
    row.update(file_size_bytes=file_hash.get("bytes", ""), sha256=file_hash.get("sha256", ""),
               native_variable_names=sorted(fields), **{key: meta.get(key, "") for key in (
                   "time_start", "time_end", "spatial_extent", "native_resolution", "crs",
                   "lat_direction", "lon_direction", "time_zone")})
    row["source_url"] = meta.get("source_url", row["source_url"])
    row["accumulation_type"] = ("instantaneous" if product == "era5_land" and stream == "instant"
                                else "unknown")
    row["timestamp_meaning"] = (
        "UTC valid time" if product in ("era5_land", "era5_single_levels") else
        "inclusive composite dates" if product in ("modis", "gosif") else "unknown")
    row["unit_source"] = "native attributes" if fields else "unknown"
    row["scale_factor"] = 1.0 if fields and all(a.get("scale_factor", 1) == 1
                                               for a in fields.values()) else ""
    row["add_offset"] = 0.0 if fields and all(a.get("add_offset", 0) == 0
                                             for a in fields.values()) else ""
    fills = {key: {k: v for k, v in attrs.items() if k in
                   ("_FillValue", "missing_value")} for key, attrs in fields.items()}
    row["fill_value"] = json.dumps(fills, ensure_ascii=False, sort_keys=True) if fields else ""
    notes = [row["audit_note"], *meta.get("warnings", [])]
    if fields:
        notes.append("native_attributes=" + json.dumps(fields, ensure_ascii=False, sort_keys=True))
    row["audit_note"] = " | ".join(filter(None, notes))
    meta["peak_memory"] = peak_memory()
    return row, meta


def inspect_tasks(tasks, domain, workers, progress, hash_only=False):
    """Bound the process queue and keep progress reporting in the parent."""
    if workers == 1:
        for task in tasks:
            progress.active = [task[2].name]
            result = inspect_file(task, domain, hash_only)
            progress.file(result)
            yield task, result
        return
    executor = ProcessPoolExecutor(max_workers=workers)
    pending = {}
    queue = iter(tasks)
    try:
        while True:
            while len(pending) < workers:
                task = next(queue, None)
                if task is None:
                    break
                pending[executor.submit(inspect_file, task, domain, hash_only)] = task
            if not pending:
                break
            progress.active = [task[2].name for task in pending.values()]
            done, _ = wait(pending, timeout=0.3, return_when=FIRST_COMPLETED)
            progress.show()
            for future in done:
                task = pending.pop(future)
                result = future.result()
                progress.file(result)
                yield task, result
    finally:
        executor.shutdown(cancel_futures=True)


class Progress:
    """Render aggregate progress and persist structured attempt events."""

    def __init__(self, args, run, attempt, total, logs):
        self.args, self.run, self.attempt, self.total, self.logs = args, run, attempt, total, logs
        self.started, self.last = monotonic(), 0
        self.counts = Counter()
        self.active = []
        self.ansi = args.progress == "auto" and sys.stderr.isatty() and os.getenv("TERM") != "dumb"

    def event(self, event, message, level="INFO", **details):
        if self.logs:
            with (self.logs / "run.jsonl").open("ab") as output:
                output.write(canonical(dict(timestamp=utc_now(), level=level, event=event,
                                            message=message, stage=STAGE, run_id=self.run,
                                            attempt_id=self.attempt, **details)))

    def file(self, result):
        row, meta = result
        self.counts[row["audit_status"]] += 1
        self.counts["bytes"] += row["file_size_bytes"] or 0
        self.counts["peak_single_process_bytes"] = max(
            self.counts["peak_single_process_bytes"], meta.get("peak_memory") or 0)
        if row["audit_status"] == "failed" or meta.get("warnings"):
            failed = row["audit_status"] == "failed"
            self.counts["warning"] += int(not failed)
            self.event("source_check", row["audit_note"] if failed else "; ".join(meta["warnings"]),
                       "ERROR" if failed else "WARNING", product=row["product_code"],
                       file=row["file_path"])
        self.show()

    def show(self, final=None):
        now = monotonic()
        if not final and now - self.last < (0.3 if self.ansi else 15):
            return
        self.last = now
        done = self.counts["passed"] + self.counts["failed"]
        elapsed = now - self.started
        pct = done / max(self.total, 1)
        eta = f"{elapsed / done * (self.total - done):.0f}s" if done else "--"
        filled = int(pct * 16)
        status = (f"M1 {self.args.mode} {self.args.product or 'all'} "
                  f"[{'#' * filled}{'.' * (16 - filled)}] {pct:.0%} {done}/{self.total} files "
                  f"ok={self.counts['passed']} skip=0 warn={self.counts['warning']} "
                  f"fail={self.counts['failed']} elapsed={elapsed:.0f}s eta={eta}")
        if final:
            status += " " + final
        else:
            status += " " + ", ".join(self.active)
        if self.ansi and not final:
            width = shutil.get_terminal_size((100, 25)).columns
            print("\r\x1b[2K" + status[:max(1, width - 1)], end="", file=sys.stderr, flush=True)
        else:
            print(("\r\x1b[2K" if self.ansi else "") + status, file=sys.stderr, flush=True)

def write_csv(path, fields, rows):
    """Serialize nullable scalars as blanks and compound fields as JSON."""
    with path.open("x", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                             if isinstance(value, (list, dict)) else value
                             for key, value in row.items()})


def contract_rows(registry, name, rows):
    """Validate generated field types, non-null fields and uniqueness before writing."""
    table = registry["tables"][name]
    fields = table["fields"]
    checks = {
        "array": lambda value: isinstance(value, list),
        "string": lambda value: isinstance(value, str),
        "integer": lambda value: type(value) is int,
        "number": lambda value: type(value) in (int, float),
        "datetime": lambda value: isinstance(value, str) and bool(datetime.fromisoformat(value)),
        "date": lambda value: isinstance(value, str) and bool(date.fromisoformat(value)),
        "boolean": lambda value: type(value) is bool,
    }
    keys = set()
    for row in rows:
        require(set(row) <= set(fields), f"{name}: unexpected columns")
        for field, spec in fields.items():
            value = row.get(field, "")
            if value is None or value == "":
                require(spec["nullable"], f"{name}: missing non-null field {field}")
                continue
            require(checks[spec["type"]](value), f"{name}.{field}: invalid {spec['type']}")
            require("values" not in spec or value in spec["values"], f"{name}.{field}: invalid value")
        key = tuple(row[field] for field in table["primary_key"])
        require(key not in keys, f"{name}: duplicate key {key}")
        keys.add(key)
    return fields


def missing_record(product, stream, key, reason, required=False):
    return dict(product=product, stream=stream, partition=key, reason=reason, required=required)


def covered_months(start, end):
    current = start.replace(day=1)
    while current <= end:
        yield current
        current = month_bounds(current.year, current.month)[1]


def mapping_rows(records, evidence):
    mappings = {}
    for (product, stream, _), (row, meta) in records:
        if row["audit_status"] != "passed":
            continue
        for native, attrs in sorted(meta.get("fields", {}).items()):
            canonical_field, canonical_unit, sign = native, attrs.get("units", "unknown"), "unknown"
            status, accumulation = "unknown", "unknown"
            qc = []
            if product == "era5_land" and native in ERA[stream]:
                canonical_field, canonical_unit, sign = ERA_MAP[native]
                if stream == "instant":
                    status, accumulation = "verified", "instantaneous"
                elif native in evidence:
                    status, accumulation = "verified", "processing_step_accumulation"
            elif stream == "MCD15A2H":
                status = "verified"
                if native == "Lai_500m":
                    canonical_field, canonical_unit = "lai", "m2 m-2"
                    sign = "apply HDF scale after masking fill and checking QA"
                    qc = ["FparLai_QC", "FparExtra_QC"]
                else:
                    sign = "preserve raw bits"
            elif stream == "MCD12Q1":
                status, sign = "verified", "preserve raw classes and flags"
                if native == "LC_Type1":
                    qc = ["QC", "LW"]
            if status == "verified" and canonical_unit == "unknown":
                canonical_unit = "class-flag"
            key = [row["product_code"], row["collection_version"], stream, native, attrs]
            key_hash = object_hash(key)[:24]
            mappings.setdefault(key_hash, dict(
                mapping_id=key_hash, product_code=row["product_code"],
                collection_version=row["collection_version"], native_variable_name=native,
                canonical_field=canonical_field, native_unit=attrs.get("units", "unknown"),
                canonical_unit=canonical_unit, scale_factor=attrs.get("scale_factor", 1),
                add_offset=attrs.get("add_offset", 0), sign_rule=sign, accumulation_type=accumulation,
                qc_fields=qc, metadata_source=SOP + " + " + row["file_path"],
                verified_by=STAGE if status == "verified" else "unknown",
                verification_date=utc_now()[:10] if status == "verified" else "",
                mapping_status=status, note=stream))
    return [mappings[key] for key in sorted(mappings)]


def audit_coverage(records, products, start, end, domain, progress):
    """Evaluate filename partitions and return scoped findings without gap filling."""
    groups = defaultdict(list)
    partitions = defaultdict(list)
    missing = []
    evidence = {}
    for task, result in records:
        product, stream, path = task
        row, meta = result
        groups[product, stream].append(result)
        stamp = declared_date(path, product)
        tile = re.search(r"\.(h\d\dv\d\d)\.", path.name) if product == "modis" else None
        key = (stamp, tile[1] if tile else None) if product == "modis" else stamp
        if stamp is not None:
            partitions[product, stream, key].append((path, row))
        if row["audit_status"] == "failed":
            missing.append(missing_record(product, stream, row["file_path"], row["audit_note"],
                                          product == "era5_land" and stream in ("instant", "accumulated")))
    for (product, stream, key), entries in sorted(partitions.items(), key=lambda item: str(item[0])):
        if len(entries) > 1:
            missing.append(missing_record(product, stream, str(key), "duplicate_partition",
                                          product == "era5_land" and stream in ("instant", "accumulated")))

    def expect(product, stream, key, required=False):
        files = partitions.get((product, stream, key), [])
        if not files:
            missing.append(missing_record(product, stream, str(key), "missing_partition", required))
        return len(files) == 1 and files[0][1]["audit_status"] == "passed"

    release = {}
    if "era5_land" in products:
        progress.active = ["ERA5-Land cross-month evidence"]
        progress.event("sample", "checking ERA5-Land cross-month evidence")
        months = list(covered_months(start, end))
        terminal = end + timedelta(days=1)
        for stream in ERA:
            required = stream in ("instant", "accumulated")
            present = [expect("era5_land", stream, month, required) for month in months]
            sample_ok = stream == "instant"
            if stream != "instant":
                prior = (months[0] - timedelta(days=1)).replace(day=1)
                choices = [month for month in [*months, prior]
                           if partitions.get(("era5_land", stream, month)) and
                           partitions.get(("era5_land", stream, month_bounds(month.year, month.month)[1]))]
                # At most three deterministic boundaries. A dry sample may lack a reset signal.
                sample_errors = []
                for month in choices[:3]:
                    next_month = month_bounds(month.year, month.month)[1]
                    pair = [partitions["era5_land", stream, key] for key in (month, next_month)]
                    if any(len(items) != 1 or items[0][1]["audit_status"] != "passed" for items in pair):
                        continue
                    try:
                        evidence.update(accumulation_sample([items[0][0] for items in pair],
                                                            month.year, month.month, stream))
                        sample_ok = True
                        break
                    except (OSError, ValueError, RuntimeError) as error:
                        sample_errors.append(str(error))
                if not sample_ok:
                    missing.append(missing_record("era5_land", stream, "cross_month",
                                                  "unverified_accumulation: " + (
                                                      "; ".join(sample_errors) or "no valid adjacent pair"),
                                                  required))
            end_present = stream == "instant"
            if stream != "instant":
                key = terminal.replace(day=1)
                files = partitions.get(("era5_land", stream, key), [])
                end_present = (len(files) == 1 and files[0][1]["audit_status"] == "passed" and
                               files[0][1]["time_end"] >= terminal.isoformat() + "T00:00:00Z")
                if not end_present:
                    missing.append(missing_record("era5_land", stream, terminal.isoformat() + "T00:00:00Z",
                                                  "missing_next_midnight", required))
            full = all(present) and sample_ok and end_present
            usable_through = end if full else None
            if all(present) and sample_ok and not end_present and end > start:
                usable_through = end - timedelta(days=1)
            release["era5_land_" + stream] = dict(
                status="passed" if full else "blocked", required=required,
                requested_start=start.isoformat(), requested_end=end.isoformat(),
                usable_through=usable_through.isoformat() if usable_through else None,
                sample_verified=sample_ok)
        for (product, stream, _), (row, _) in records:
            if product == "era5_land" and stream != "instant" and all(
                    name in evidence for name in ERA[stream]) and row["audit_status"] == "passed":
                row["accumulation_type"] = "processing_step_accumulation"

    if "modis" in products:
        for year in range(start.year, end.year + 1):
            for doy in range(1, 367 if isleap(year) else 366, 8):
                stamp = date(year, 1, 1) + timedelta(days=doy - 1)
                if stamp <= end and stamp + timedelta(days=7) >= start:
                    for tile in TILES:
                        expect("modis", "MCD15A2H", (stamp, tile))
            for tile in TILES:
                expect("modis", "MCD12Q1", (date(year, 1, 1), tile))
        release["lai_descriptive_support"] = dict(
            status="conditional" if any(row["audit_status"] == "passed"
                                        for row, _ in groups["modis", "MCD15A2H"]) else "blocked",
            reason="apply pixel QA, spatial overlap and five-year completeness downstream")
        release["landcover_mask"] = dict(status="unavailable", reason="frozen profile and sparse tile-years")

    for product in ("era5_single_levels", "esa_cci_sm", "gleam", "gosif"):
        if product not in products:
            continue
        if product == "era5_single_levels":
            for stream in ("instant", "accumulated", "sm", "wind"):
                for month in covered_months(start, end):
                    expect(product, stream, month)
        elif product == "esa_cci_sm":
            for offset in range((end - start).days + 1):
                expect(product, "passive", start + timedelta(days=offset))
        elif product == "gleam":
            for stream in ("Et", "SMrz", "SMs"):
                for year in range(start.year, end.year + 1):
                    expect(product, stream, date(year, 1, 1))
        else:
            for year in range(start.year, end.year + 1):
                for doy in range(1, 367 if isleap(year) else 366, 8):
                    stamp = date(year, 1, 1) + timedelta(days=doy - 1)
                    if stamp <= end and stamp + timedelta(days=7) >= start:
                        expect(product, "v2", stamp)

    coverage = []
    grid = domain["cell_center_bounds"]
    for (product, stream), items in sorted(groups.items()):
        passed = [row for row, _ in items if row["audit_status"] == "passed"]
        extents = {tuple(row["spatial_extent"]) for row in passed if row["crs"] == "EPSG:4326"
                   and row["spatial_extent"]}
        if len(extents) > 1 and product not in ("modis", "aridity", "irrigation"):
            missing.append(missing_record(product, stream, "all", "spatial_extent_changed",
                                          product == "era5_land" and stream in ("instant", "accumulated")))
        common = []
        if extents:
            common = [max(grid[0], *(extent[0] for extent in extents)),
                      max(grid[1], *(extent[1] for extent in extents)),
                      min(grid[2], *(extent[2] for extent in extents)),
                      min(grid[3], *(extent[3] for extent in extents))]
            if common[0] > common[2] or common[1] > common[3]:
                common = []
        starts = [row["time_start"] for row in passed if row["time_start"]]
        ends = [row["time_end"] for row in passed if row["time_end"]]
        coverage.append(dict(product=product, stream=stream, files=len(items), passed=len(passed),
                             first_observed=min(starts, default=""), last_observed=max(ends, default=""),
                             nominal_common_extent=common,
                             common_start=max(start.isoformat(), min(starts)) if starts else "",
                             common_end=min(end.isoformat(), max(ends)[:10]) if ends else "",
                             support="extent only; pairwise valid cells and times require M2 QA"
                             if product != "era5_land" else "frozen ERA5-Land grid"))
    release.update(
        station_independent_validation=dict(status="unavailable", reason="no accepted station inventory"),
        q3_confirmatory=dict(status="unavailable", reason="frozen M0 restriction"),
        fz_dfai=dict(status="blocked", reason="ssro missing"),
        m9_registered_suite=dict(status="unavailable", reason="frozen M0 restriction"),
        m9_existing_products=dict(status="conditional",
                                  reason="use pairwise common valid support after downstream QA"),
    )
    return coverage, missing, release, evidence


def publish(work, target, manifest):
    """Publish a complete directory by exclusive same-volume rename."""
    members = {path.name: fingerprint(path) for path in sorted(work.iterdir())}
    marker = work / "m1_complete.json"
    with marker.open("xb") as output:
        output.write(canonical(dict(manifest, artifacts=members, validation="passed")))
        output.flush()
        os.fsync(output.fileno())
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = safe(target.with_name(target.name + ".lock"))
    try:
        with lock.open("x", encoding="utf-8") as handle:
            handle.write(manifest["attempt_id"] + "\n")
    except FileExistsError as error:
        marker.unlink()
        raise ValueError(f"another attempt holds publication lock: {lock}") from error
    try:
        require(not target.exists(), f"published output already exists: {target}")
        os.rename(work, target)
    except BaseException:
        marker.unlink(missing_ok=True)
        raise
    finally:
        lock.unlink()


def reuse(target, identity, input_files):
    """Revalidate a completed M1 publication before reusing it."""
    manifest = read_json(safe(target / "m1_complete.json"))
    require(manifest["manifest_version"] == 1 and manifest["validation"] == "passed" and
            manifest["stage"] == STAGE and manifest["run_id"] == target.parent.name and
            manifest["identity"] == identity and manifest["input_inventory_hash"] == object_hash(input_files),
            "M1 completion identity or raw inventory changed")
    expected_names = {"source_file_manifest.csv", "source_variable_map.csv", "station_metadata.csv",
                      "m1_data_coverage.csv", "m1_missing_files.csv", "m1_release.json"}
    require(set(manifest["artifacts"]) == expected_names, "invalid M1 artifact inventory")
    for name, expected in manifest["artifacts"].items():
        require(fingerprint(safe(target / name)) == expected, f"M1 artifact mismatch: {name}")


def check_m0_evidence(identity, results, production):
    hashes = {row["file_path"]: row for row, _ in results}
    for file in identity["m0_grid_evidence"]:
        path = file["path"]
        row = hashes.get(path)
        if row is not None or production:
            require(row is not None and row["sha256"] == file["sha256"] and
                    row["file_size_bytes"] == file["bytes"], f"M0 grid evidence changed: {path}")


def options(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", required=True, choices=("test", "production"))
    parser.add_argument("--config", required=True, type=Path, help="frozen M0 run_config.json")
    parser.add_argument("--start", type=date.fromisoformat, help="inclusive YYYY-MM-DD; test only")
    parser.add_argument("--end", type=date.fromisoformat, help="inclusive YYYY-MM-DD; test only")
    parser.add_argument("--product", choices=PRODUCTS, help="limit to one product; test only")
    parser.add_argument("--workers", type=int, default=1, help="worker processes, 1 to 4; default 1")
    parser.add_argument("--progress", choices=("auto", "plain"), default="auto")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="list planned scope and paths without auditing")
    modes.add_argument("--check-only", action="store_true", help="audit read-only, emit results to stdout")
    modes.add_argument("--resume", action="store_true", help="validate and reuse an existing publication")
    return parser.parse_args(argv)


def main(argv=None):
    args = options(argv)
    try:
        require(1 <= args.workers <= min(4, os.cpu_count() or 1), "workers outside available range")
        require(args.mode == "test" or not (args.start or args.end or args.product),
                "production uses the complete frozen range and product inventory")
        params, registry, identity = load_m0(args.config, args.mode)
        first_year, last_year = params["time"]["periods"]["event"]
        start = args.start or date(first_year, 1, 1)
        end = args.end or date(last_year, 12, 31)
        require(date(first_year, 1, 1) <= start <= end <= date(last_year, 12, 31),
                "invalid dates or outside frozen event range")
        domain = params["grid"]["domain"]
        products = (args.product,) if args.product else PRODUCTS
        identity.update(mode=args.mode, start=start.isoformat(), end=end.isoformat(), products=list(products))
        scope = object_hash(identity)[:12]
        run = identity["m0_run_id"] if args.mode == "production" else "test__" + identity["m0_run_id"] + "__m1_" + scope
        target = safe(ROOT / "data" / "frozen" / run / STAGE)
        attempt = uuid4().hex
        work = safe(ROOT / "data" / "interim" / run / STAGE / attempt)
        logs = safe(ROOT / "logs" / run / STAGE / attempt)
        tasks = inventory(products, start, end)
        require(tasks, "no matching source files")
        free = shutil.disk_usage(ROOT).free
        memory = available_memory()
        # Conservative per-reader allowance for native libraries, chunks and Python.
        budget = (args.workers + int(args.workers > 1)) * 256 * 2**20
        if not (args.dry_run or args.check_only):
            require(free >= max(128 * 2**20, len(tasks) * 16384), "insufficient disk for audit reports")
            require(args.resume or not target.exists(), "published output exists; use --resume")
        require(budget <= 0.6 * memory, "reduce workers: reader budget exceeds 60% of available memory")
        plan = dict(run_id=run, mode=args.mode, start=start.isoformat(), end=end.isoformat(),
                    products=list(products), files=len(tasks), workers=args.workers,
                    target=str(target), work=str(work), logs=str(logs), disk_free_bytes=free,
                    available_memory_bytes=memory, reader_budget_bytes=budget)
        print(canonical(plan).decode().strip(), flush=True)
        if args.dry_run:
            return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, yaml.YAMLError) as error:
        print(f"M1 preflight: {error}", file=sys.stderr)
        return 2

    progress = Progress(args, run, attempt, len(tasks), None if args.check_only else logs)
    final = "failed"
    try:
        if not args.check_only:
            logs.mkdir(parents=True, exist_ok=False)
        progress.event("started", "M1 raw source audit", scope=plan, identity=identity)
        # Disable GDAL PAM sidecars in raw input directories and nested numeric threads.
        os.environ["GDAL_PAM_ENABLED"] = "NO"
        for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
            os.environ[variable] = "1"
        reuse_existing = args.resume and target.exists()
        records = sorted(inspect_tasks(tasks, domain, args.workers, progress, reuse_existing),
                         key=lambda item: item[0])
        require(tasks == inventory(products, start, end), "raw inventory changed during audit")
        results = [item for _, item in records]
        check_m0_evidence(identity, results, args.mode == "production")
        file_rows = [row for row, _ in results]
        input_files = [[row[key] for key in ("file_path", "file_size_bytes", "sha256")] for row in file_rows]
        if reuse_existing:
            reuse(target, identity, input_files)
            final = f"reused {target}"
            return 0
        coverage, missing, release, evidence = audit_coverage(records, products, start, end, domain, progress)
        bad_required = any(row["required"] for row in missing)
        any_failed = any(row["audit_status"] == "failed" for row in file_rows)
        blocked = bad_required or (args.mode == "test" and any_failed)
        result = dict(release=release, accumulation_evidence=evidence, required_blocked=bad_required,
                      input_inventory_hash=object_hash(input_files),
                      missing_counts=dict(Counter(row["reason"] for row in missing)))
        if args.check_only:
            print(canonical(dict(result, coverage=coverage, issues=missing)).decode().strip())
            final = "blocked" if blocked or any_failed else "checked"
            return int(blocked or any_failed)
        work.mkdir(parents=True, exist_ok=False)
        mapping = mapping_rows(records, evidence)
        for name, rows in (("source_file_manifest", file_rows), ("source_variable_map", mapping)):
            write_csv(work / (name + ".csv"), contract_rows(registry, name, rows), rows)
        write_csv(work / "station_metadata.csv", STATION_FIELDS, [])
        write_csv(work / "m1_data_coverage.csv",
                  ("product", "stream", "files", "passed", "first_observed", "last_observed",
                   "nominal_common_extent", "common_start", "common_end", "support"), coverage)
        write_csv(work / "m1_missing_files.csv",
                  ("product", "stream", "partition", "reason", "required"), missing)
        (work / "m1_release.json").write_bytes(canonical(result))
        if blocked:
            final = f"blocked reports={work}"
            progress.event("release_blocked", "required audit scope did not pass", "ERROR", result=result)
            return 1
        publish(work, target, dict(manifest_version=1, stage=STAGE, run_id=run, attempt_id=attempt,
                                   identity=identity, release=release, schema_version=params["protocol"]["version"],
                                   input_inventory_hash=result["input_inventory_hash"]))
        final = f"published {target}"
        return 0
    except KeyboardInterrupt:
        final = "interrupted"
        return 130
    except Exception as error:
        progress.event("exception", str(error), "ERROR", traceback=traceback.format_exc())
        print(f"M1 audit: {error}", file=sys.stderr)
        return 1
    finally:
        elapsed = monotonic() - progress.started
        progress.event("finished", final, counts=dict(progress.counts), elapsed_s=elapsed,
                       hash_mib_s=progress.counts["bytes"] / max(elapsed, 1e-9) / 2**20,
                       work=str(work), target=str(target), logs=str(logs))
        progress.show(f"{final} logs={logs}" if not args.check_only else final)


if __name__ == "__main__":
    sys.exit(main())
