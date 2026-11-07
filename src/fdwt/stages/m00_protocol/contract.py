"""Validate M0 protocol sources, configuration, and release evidence."""

import hashlib
import json
import re
from pathlib import Path

import yaml

VERSION = "v1.3.1"
CONFIG_FILES = ("params.yaml", "schema_registry.yaml", "specification_matrix.yaml")
GATES = ("boundary", "stations", "circulation", "pet_floor")
EXISTING_DATA_PROFILE = "existing_data_era5land_grid_r1"
SECTIONS = (
    "protocol grid time spatial mask phenology clim drought recovery pterm fr fz lag "
    "dfai lai ndvi deep covariates route1 route2 infer trend block equivalence psm "
    "spec_curve products random output"
).split()


class InvalidContract(ValueError):
    """The supplied inputs cannot be used to freeze a run."""


def require(condition, message):
    if not condition:
        raise InvalidContract(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode()


class UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            require(isinstance(key, str), "mapping keys must be strings")
            require(key not in result, f"duplicate key: {key}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def parse(data):
    value = yaml.load(data.decode("utf-8-sig"), Loader=UniqueLoader)
    require(isinstance(value, dict), "expected a mapping")
    return value


def sources(root, protocol_dir):
    registration = (root / "docs/protocol_sources.md").read_text(encoding="utf-8")
    expected = dict(re.findall(r"^\| `([^`]+\.md)` \| [^|]+ \| `([a-f0-9]{64})`", registration, re.M))
    require(len(expected) == 7, "protocol registration must contain seven documents")
    observed = {}
    for suffix, expected_hash in expected.items():
        filename = f"黄土高原FD-WT_{VERSION}_{suffix}"
        path = protocol_dir / filename
        require(path.is_file(), f"missing protocol source: {filename}")
        observed[filename] = digest(path.read_bytes())
        require(observed[filename] == expected_hash, f"protocol hash mismatch: {filename}")
    return observed


def validate(params, registry, matrix):
    require(all(isinstance(params.get(key), dict) for key in SECTIONS), "missing parameter section")
    require(params["protocol"]["version"] == registry["version"] == VERSION, "protocol version mismatch")
    profile = params["protocol"].get("profile")
    require(profile in (None, EXISTING_DATA_PROFILE), "unknown protocol profile")
    start_year = 2001 if profile == EXISTING_DATA_PROFILE else 2000
    require(params["time"]["periods"] == {
        "event": [start_year, 2023], "lai_complete": [2003, 2023],
        "lai_exposure": [2008, 2023], "lai_ndvi_common": [2008, 2023],
    }, "four protocol periods mismatch")
    if profile == EXISTING_DATA_PROFILE:
        require(params["clim"]["base_window"] == [2001, 2023], "incorrect revised climatology period")
        require(params["grid"]["domain"] == {
            "coordinate_reference_system": "EPSG:4326", "cell_center_bounds": [100, 33, 115, 42],
            "resolution_deg": 0.1, "longitude_count": 151, "latitude_count": 91,
        }, "incorrect revised grid domain")
    require([params["time"]["year_start"], params["time"]["year_end"]] ==
            params["time"]["periods"]["event"], "event years disagree")
    require(params["route1"]["recovery_risk_t0"] == "t_min" and
            params["route2"]["t0"] == "t_s", "incorrect time anchor")
    require(params["multiple_testing"]["method"] == "holm" and
            {key: len(value) for key, value in params["multiple_testing"]["families"].items()} ==
            {"Q1": 3, "Q2": 2, "Q3": 4}, "invalid Holm families")
    seed = params["random"]
    require(type(seed["seed"]) is int and seed["seed"] >= 0 and
            re.fullmatch(r"[a-z0-9_]+", seed["seed_id"]), "invalid random seed or seed ID")
    tables = registry["tables"]
    require({"event_master", "deviation_log", "source_file_manifest", "route2_exit"} <= tables.keys(),
            "missing core schema")
    for name, table in tables.items():
        keys = table["primary_key"]
        require(keys and len(set(keys)) == len(keys), f"invalid primary key: {name}")
        require(all(key in table["fields"] and not table["fields"][key]["nullable"] for key in keys),
                f"invalid primary key field: {name}")
    alternatives = params["spec_curve"]["candidate_alternatives"]
    require(matrix["estimands"].keys() == alternatives.keys(), "matrix estimands disagree")
    for name, spec in matrix["estimands"].items():
        require(spec["alternatives"].keys() == alternatives[name].keys(), f"matrix families disagree: {name}")
        counts = []
        for family, entry in spec["alternatives"].items():
            count = len(alternatives[name][family])
            require(entry["parameter_ref"] == f"spec_curve.candidate_alternatives.{name}.{family}"
                    and entry["count"] == count and entry["requires"], f"invalid matrix entry: {name}/{family}")
            counts.append(count)
        require(spec["count"] == 1 + sum(counts), f"matrix count mismatch: {name}")


def check_evidence(record, directory):
    """Verify declared source and version and the hash of an evidence file."""
    if not record or not all(record.get(key) for key in ("source", "version", "path", "sha256")):
        return False
    path = (directory / record["path"]).resolve()
    return path.is_file() and digest(path.read_bytes()) == record["sha256"]


def releases(run, directory):
    external = run["external"]
    result = {gate: check_evidence(external.get(gate), directory) for gate in GATES}
    floor = (external.get("pet_floor") or {}).get("value")
    result["pet_floor"] = result["pet_floor"] and type(floor) in (int, float) and 0 < floor < float("inf")
    return {gate: "released" if ok else "pending" for gate, ok in result.items()}


def validate_grid_boundary(params, run, directory, gates, root):
    """Check the accepted grid definition against the frozen parameter domain."""
    if params["protocol"].get("profile") != EXISTING_DATA_PROFILE:
        return
    if gates["boundary"] != "released":
        return
    evidence = run["external"]["boundary"]
    definition = parse((directory / evidence["path"]).read_bytes())
    require(evidence["version"] == EXISTING_DATA_PROFILE and
            definition["domain"] == params["grid"]["domain"],
            "grid boundary evidence disagrees with the revised profile")
    raw = (root / "data_raw" / "era5_land").resolve()
    samples = definition["evidence"]
    require(len(samples) == 6, "expected six ERA5-Land boundary evidence files")
    for sample in samples:
        path = (root / sample["path"]).resolve()
        require(path.is_relative_to(raw) and path.is_file() and
                path.stat().st_size == sample["bytes"], "boundary source file missing or changed")
        with path.open("rb") as stream:
            observed = hashlib.file_digest(stream, "sha256").hexdigest()
        require(observed == sample["sha256"], f"boundary source hash mismatch: {path.name}")
