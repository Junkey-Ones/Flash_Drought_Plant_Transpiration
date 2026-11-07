r"""M0 entry point for protocol validation and immutable publication.

Inputs: local configuration, seven registered protocol notes, input manifest.
Outputs: immutable metadata snapshot, completion manifest and attempt logs.
Downstream: M1 reads the validated M0 manifest and corresponding configuration.
From the project root:
  .venv\Scripts\python.exe src/fdwt/stages/m00_protocol/freeze.py --mode test --config config/m0_test.json
  .venv\Scripts\python.exe src/fdwt/stages/m00_protocol/freeze.py --mode production --config config/m0_production.json
Exit codes: 0 success, 1 execution failure, 2 invalid preflight, 130 interrupted.
"""

import argparse
import csv
from datetime import datetime, timezone
from importlib.metadata import distributions
import os
from pathlib import Path
import platform
import subprocess
import sys
from time import monotonic
from uuid import uuid4

import yaml

from contract import (
    CONFIG_FILES, VERSION, InvalidContract, canonical, digest, parse, releases,
    require, sources, validate, validate_grid_boundary,
)

ROOT = Path(__file__).resolve().parents[4]
STAGE = "m00_protocol"
DEPENDENCIES = {
    "M0": [], "M1": ["M0"], "M2": ["M1"], "M3": ["M2"], "M4": ["M3"],
    "M5": ["M3", "M4"], "M6": ["M3", "M4", "M5"],
    "M7_Q1": ["M4", "M5"], "M7_Q2": ["M5"],
    "M7_Q3": ["M6", "M7_Q1", "M7_Q2"],
    "M8a": ["M4", "M5"], "M8b": ["M8a"],
    "M9": ["M7_Q1", "M7_Q2", "M7_Q3"],
    "M10": ["M7_Q1", "M7_Q2", "M7_Q3", "M9"],
}
OPTIONAL_DEPENDENCIES = {"M8b": ["M6"], "M9": ["M8b"], "M10": ["M8b"]}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def git(*args):
    return subprocess.check_output(
        ["git", "-c", f"safe.directory={ROOT.as_posix()}", "--no-optional-locks", *args],
        cwd=ROOT, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    ).strip()


def environment():
    return {
        "python": platform.python_version(), "implementation": platform.python_implementation(),
        "platform": platform.platform(), "architecture": platform.machine(),
        "packages": {item.metadata["Name"].lower(): item.version for item in distributions()},
    }


def safe_output(*parts):
    path = ROOT.joinpath(*parts)
    require(path.resolve() == path, f"output redirected outside its declared path: {path}")
    return path


def prepare(args):
    config_path = (ROOT / args.config).resolve()
    directory = config_path.parent
    run = parse(config_path.read_bytes())
    require(run["mode"] == args.mode, f"{args.mode} requires a matching run configuration")
    files = {name: (directory / name).read_bytes() for name in CONFIG_FILES}
    params, schema, matrix = (parse(files[name]) for name in CONFIG_FILES)
    validate(params, schema, matrix)
    protocol = sources(ROOT, (directory / run["protocol_dir"]).resolve())
    input_data = (directory / run["input_manifest"]).read_bytes()
    require(parse(input_data)["mode"] == args.mode, "input manifest mode mismatch")
    gate_status = releases(run, directory)
    validate_grid_boundary(params, run, directory, gate_status, ROOT)
    deviations = run["deviations"]
    require(isinstance(deviations, list), "deviations must be a list")
    headers = list(schema["tables"]["deviation_log"]["fields"])
    require(all(set(row) <= set(headers) and row.get("deviation_id") and row.get("status")
                for row in deviations), "invalid deviation record")
    config_hashes = {name: digest(data) for name, data in files.items()}
    files["run_config.json"] = canonical(run)
    if gate_status["boundary"] == "released":
        boundary = run["external"]["boundary"]
        evidence = (directory / boundary["path"]).read_bytes()
        require(digest(evidence) == boundary["sha256"], "boundary evidence changed during preflight")
        files["boundary_evidence" + Path(boundary["path"]).suffix] = evidence
    env = environment()
    code = {str(path.relative_to(ROOT).as_posix()): digest(path.read_bytes())
            for path in sorted(Path(__file__).parent.glob("*.py"))}
    code_hash = digest(canonical(code))
    identity = {
        "mode": args.mode, "protocol_version": VERSION, "protocol_hashes": protocol,
        "config_hashes": config_hashes, "code_hash": code_hash, "code_files": code,
        "source_manifest_hash": digest(input_data), "environment": env,
        "environment_hash": digest(canonical(env)), "seed_id": params["random"]["seed_id"],
        "run_config_hash": digest(files["run_config.json"]), "git_commit": git("rev-parse", "HEAD"),
    }
    identity_hash = digest(canonical(identity))
    blockers = []
    if gate_status["boundary"] != "released":
        blockers.append("research boundary evidence missing or invalid")
    if any(row["status"] != "closed" or not row.get("approver") or not row.get("rerun_scope")
           for row in deviations):
        blockers.append("unresolved protocol discrepancy")
    if any(field["type"] == "pending_review" for table in schema["tables"].values()
           for field in table["fields"].values()):
        blockers.append("schema field types pending review")
    approval = run.get("approval") or {}
    reviewed = {"config_hashes": config_hashes, "protocol_hashes": protocol,
                "environment_hash": identity["environment_hash"]}
    if not approval.get("reviewer") or not approval.get("date") or approval.get("reviewed") != reviewed:
        blockers.append("missing approval for the configuration, protocol and environment hashes")
    if params["random"].get("status") != "frozen" or any(
            item.get("status") != "frozen" for item in (schema, matrix)):
        blockers.append("seed, schema or matrix still marked as candidate")
    tracked = (*code, "docs/protocol_sources.md")
    if git("status", "--porcelain", "--untracked-files=all", "--", *tracked):
        blockers.append("participating code or protocol registration is not fixed in Git")
    base = f"{VERSION}__{config_hashes['params.yaml'][:12]}__{code_hash[:12]}__{identity['seed_id']}"
    run_id = base if args.mode == "production" else f"test__{base}__{identity_hash[:12]}"
    target = safe_output("metadata", "runs", run_id)
    return target, identity, gate_status, blockers, files, input_data, deviations, headers


def reuse(target, identity, members):
    """Reject a different run identity, incomplete publication or changed bytes."""
    manifest = parse((target / "m0_complete.json").read_bytes())
    if manifest.get("identity") != identity or set(manifest.get("artifacts", {})) != members:
        raise RuntimeError("run identity or completion manifest mismatch")
    for name, expected in manifest["artifacts"].items():
        data = (target / name).read_bytes()
        if {"sha256": digest(data), "bytes": len(data)} != expected:
            raise RuntimeError(f"published artifact failed integrity check: {name}")


def publish(prepared, mode, progress):
    target, identity, gates, blockers, files, input_data, deviations, headers = prepared
    attempt = uuid4().hex
    started = monotonic()
    logs = safe_output("logs", target.name, STAGE, attempt)
    work = safe_output("data", "interim", target.name, STAGE, attempt)
    logs.mkdir(parents=True, exist_ok=False)

    def report(status, step, level="INFO"):
        with (logs / "run.jsonl").open("ab") as log:
            log.write(canonical({
                "timestamp": utc_now(), "level": level, "event": status, "message": step,
                "stage": STAGE, "run_id": target.name, "attempt_id": attempt,
                "elapsed_s": round(monotonic() - started, 3), "mode": mode,
            }))
        transient = status == "started" and progress == "auto" and sys.stdout.isatty()
        count = "0/2" if status == "started" else "2/2"
        print(f"{STAGE} {mode} {count} {status} {step}", end="\r" if transient else "\n", flush=True)

    status = "failed"
    try:
        report("started", str(work))
        manifest = dict(identity, run_id=target.name, attempt_id=attempt, created_at=utc_now())
        artifacts = dict(files, **{
            "source_input_manifest.json": input_data,
            "run_manifest.json": canonical(manifest),
            "protocol_freeze.json": canonical({
                "state": "test_validation" if mode == "test" else "internal_preregistration_freeze",
                "release": gates, "production_blockers": blockers,
                "approval_required_for_production": True,
            }),
            "dependencies.json": canonical({"required": DEPENDENCIES, "optional": OPTIONAL_DEPENDENCIES}),
        })
        with (logs / "deviation_log.csv").open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=headers)
            writer.writeheader()
            for row in deviations:
                writer.writerow({key: canonical(value).decode().strip() if isinstance(value, list) else value
                                 for key, value in row.items()})
        if target.exists():
            reuse(target, identity, set(artifacts))
            status = "reused"
        else:
            work.mkdir(parents=True, exist_ok=False)
            for name, data in artifacts.items():
                (work / name).write_bytes(data)
            (work / "m0_complete.json").write_bytes(canonical({
                "manifest_version": 1, "stage": STAGE, "run_id": target.name, "attempt_id": attempt,
                "validation": "passed", "identity": identity,
                "artifacts": {name: {"sha256": digest(data), "bytes": len(data)}
                              for name, data in artifacts.items()},
            }))
            reuse(work, identity, set(artifacts))
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(work, target)
                status = "published"
            except OSError:
                if not target.exists():
                    raise
                reuse(target, identity, set(artifacts))
                status = "reused"
        report(status, f"output={target} logs={logs}")
    except KeyboardInterrupt:
        status = "interrupted"
        report(status, "interrupted", "WARNING")
        raise
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        report(status, str(error), "ERROR")
        raise
    finally:
        with (logs / "module_status.csv").open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(("stage", "mode", "status", "run_id", "attempt_id"))
            writer.writerow((STAGE, mode, status, target.name, attempt))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", required=True, choices=("test", "production"))
    parser.add_argument("--config", required=True, help="run JSON path, relative to project root or absolute")
    parser.add_argument("--progress", choices=("auto", "plain"), default="auto")
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--dry-run", action="store_true", help="show inputs, gates and target without writing")
    checks.add_argument("--check-only", action="store_true", help="validate inputs and release gates without writing")
    args = parser.parse_args(argv)
    try:
        prepared = prepare(args)
        target, identity, gates, blockers, *_ = prepared
        print(canonical({"target": str(target), "mode": args.mode, "release": gates,
                         "production_blockers": blockers, "config_hashes": identity["config_hashes"],
                         "protocol_hashes": identity["protocol_hashes"],
                         "environment_hash": identity["environment_hash"],
                         "protocol_count": len(identity["protocol_hashes"])}).decode().strip())
        if args.mode == "production" and blockers:
            return 2
        if args.dry_run or args.check_only:
            return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError, subprocess.SubprocessError) as error:
        print(f"M0 preflight: {error}", file=sys.stderr)
        return 2
    try:
        publish(prepared, args.mode, args.progress)
    except KeyboardInterrupt:
        return 130
    except (OSError, KeyError, ValueError, RuntimeError, yaml.YAMLError) as error:
        print(f"M0 publish: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
