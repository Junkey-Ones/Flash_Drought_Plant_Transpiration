"""M0 contract and publication regression tests using isolated temporary runs."""

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/fdwt/stages/m00_protocol"))
import contract
import freeze


@pytest.fixture
def project(tmp_path, monkeypatch):
    directory = tmp_path / "config"
    directory.mkdir()
    params = {section: {} for section in contract.SECTIONS}
    params.update({
        "protocol": {"version": contract.VERSION},
        "time": {"year_start": 2000, "year_end": 2023, "periods": {
            "event": [2000, 2023], "lai_complete": [2003, 2023],
            "lai_exposure": [2008, 2023], "lai_ndvi_common": [2008, 2023],
        }},
        "route1": {"recovery_risk_t0": "t_min"},
        "route2": {"t0": "t_s"},
        "multiple_testing": {"method": "holm", "families": {
            "Q1": ["RT_trend", "delta_SMP_trend", "RR_trend"],
            "Q2": ["RR_pre_sync", "delta_SMP_pre_sync"],
            "Q3": ["LAI_trend_recovery", "LAI_trend_FR",
                   "LAI_trend_x_deep_recovery", "LAI_trend_x_deep_FR"],
        }},
        "random": {"seed": 13120261102, "seed_id": "fixture"},
        "spec_curve": {"candidate_alternatives": {"Q1_trend": {"termination": [40]}}},
    })
    registry = {"version": contract.VERSION, "tables": {
        table: {"primary_key": [key], "fields": {key: {"nullable": False, "type": "string"}}}
        for table, key in [("event_master", "episode_id"), ("deviation_log", "deviation_id"),
                           ("source_file_manifest", "file_id"), ("route2_exit", "episode_id")]
    }}
    registry["tables"]["deviation_log"]["fields"]["status"] = {"nullable": False, "type": "string"}
    matrix = {"estimands": {"Q1_trend": {"count": 2, "alternatives": {"termination": {
        "parameter_ref": "spec_curve.candidate_alternatives.Q1_trend.termination",
        "requires": "estimand_input_release", "count": 1,
    }}}}}
    for name, value in zip(contract.CONFIG_FILES, (params, registry, matrix), strict=True):
        (directory / name).write_bytes(contract.canonical(value))
    protocol_dir = tmp_path / "protocol"
    protocol_dir.mkdir()
    registration = []
    for index in range(7):
        suffix = f"fixture_{index}.md"
        data = b"synthetic test only\n"
        (protocol_dir / f"黄土高原FD-WT_{contract.VERSION}_{suffix}").write_bytes(data)
        registration.append(f"| `{suffix}` | fixture | `{contract.digest(data)}` |")
    run = {"mode": "test", "protocol_dir": str(protocol_dir), "input_manifest": "input.json",
           "external": {}, "deviations": [{"deviation_id": "fixture", "status": "open"}]}
    (directory / run["input_manifest"]).write_bytes(contract.canonical({"mode": "test", "files": []}))
    (directory / "run.json").write_bytes(contract.canonical(run))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/protocol_sources.md").write_text("\n".join(registration), encoding="utf-8")
    monkeypatch.setattr(freeze, "ROOT", tmp_path)
    monkeypatch.setattr(freeze, "git", lambda *args: "fixture" if args[0] == "rev-parse" else "")
    monkeypatch.setattr(freeze, "__file__", str(tmp_path / "src/freeze.py"))
    return directory


def run_m0(*args, mode="test"):
    return freeze.main(["--mode", mode, "--config", "config/run.json", "--progress", "plain", *args])


@pytest.mark.parametrize("flag", ["--dry-run", "--check-only"])
def test_preflight_does_not_write(project, flag):
    assert run_m0(flag) == 0
    assert not (project.parent / "metadata").exists()
    assert not (project.parent / "logs").exists()


def test_protocol_hash_mismatch(project):
    run = contract.parse((project / "run.json").read_bytes())
    original = Path(run["protocol_dir"])
    modified = project / "modified"
    modified.mkdir()
    for path in original.glob("*.md"):
        (modified / path.name).write_bytes(path.read_bytes() + b"\n")
    run["protocol_dir"] = str(modified)
    (project / "run.json").write_bytes(contract.canonical(run))
    assert run_m0("--check-only") == 2


def test_duplicate_configuration_key():
    with pytest.raises(contract.InvalidContract, match="duplicate key"):
        contract.parse(b'{"seed": 1, "seed": 2}')


@pytest.mark.parametrize("path,value", [
    (("time", "year_start"), 2001),
    (("route1", "recovery_risk_t0"), "t_s"),
    (("random", "seed"), None),
])
def test_invalid_parameters(project, path, value):
    file = project / "params.yaml"
    params = contract.parse(file.read_bytes())
    params[path[0]][path[1]] = value
    file.write_bytes(contract.canonical(params))
    assert run_m0("--check-only") == 2


def test_invalid_key_and_matrix(project):
    params, registry, matrix = [
        contract.parse((project / name).read_bytes()) for name in contract.CONFIG_FILES
    ]
    altered = deepcopy(registry)
    altered["tables"]["event_master"]["primary_key"] = ["missing"]
    with pytest.raises(contract.InvalidContract, match="primary key field"):
        contract.validate(params, altered, matrix)
    matrix["estimands"]["Q1_trend"]["count"] += 1
    with pytest.raises(contract.InvalidContract, match="matrix count mismatch"):
        contract.validate(params, registry, matrix)


def test_production_refuses_test_and_reports_conflict(project, capsys):
    assert run_m0(mode="production") == 2
    run = contract.parse((project / "run.json").read_bytes())
    run["mode"] = "production"
    (project / "run.json").write_bytes(contract.canonical(run))
    assert run_m0(mode="production") == 2
    assert "input manifest mode mismatch" in capsys.readouterr().err
    (project / run["input_manifest"]).write_bytes(contract.canonical({"mode": "production", "files": []}))
    assert run_m0(mode="production") == 2
    result = json.loads(capsys.readouterr().out)
    assert "unresolved protocol discrepancy" in result["production_blockers"]
    assert result["release"]["stations"] == "pending"
    assert not (project.parent / "metadata").exists()


def test_reuse_and_tamper(project, capsys):
    assert run_m0() == 0
    target = next((project.parent / "metadata/runs").iterdir())
    completion = target / "m0_complete.json"
    original = completion.read_bytes()
    assert run_m0() == 0
    assert completion.read_bytes() == original
    (target / "params.yaml").write_text("tampered", encoding="utf-8")
    assert run_m0() == 1
    assert "failed integrity check" in capsys.readouterr().err


@pytest.mark.parametrize("failure,exit_code", [(OSError("failed"), 1), (KeyboardInterrupt(), 130)])
def test_publish_failure_can_retry(project, monkeypatch, failure, exit_code):
    with monkeypatch.context() as patch:
        def fail(*args):
            raise failure
        patch.setattr(freeze.os, "rename", fail)
        assert run_m0() == exit_code
    assert not list((project.parent / "metadata/runs").glob("*/m0_complete.json"))
    assert run_m0() == 0
