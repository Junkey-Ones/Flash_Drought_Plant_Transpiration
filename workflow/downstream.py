r"""适用于现有 ERA5-Land 冻结方案的 M3–M10 工作流入口。

读取 M0 配置和上游完成清单，按责任阶段调用独立脚本并发布通过校验的交接。
试跑沿用上游范围，M3 可另限格点数。试跑结果位于 data/frozen/test__...。
  .venv\Scripts\python.exe -B workflow/downstream.py m3 --mode test --config metadata/runs/M0_ID/run_config.json --input data/frozen/M2_TEST_ID/m02_preprocess/m2_complete.json --max-cells 2 --dry-run
  .venv\Scripts\python.exe -B workflow/downstream.py m3 --mode production --config metadata/runs/M0_ID/run_config.json
退出码：0 成功，1 执行失败，2 预检失败，130 中断。
"""
import argparse
from contextlib import contextmanager
import importlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from time import monotonic
import traceback
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT / "src/fdwt/stages/m02_preprocess"):
    sys.path.insert(0, str(path))
from preprocess import ARTIFACTS, fingerprint, m1, make_table, schema_for
from terminal_panel import ChineseHelp, ChineseParser, LiveStatus, validate_progress

STAGES = dict(enumerate(("m01_source_audit", "m02_preprocess", "m03_support_climatology",
                         "m04_event_catalog", "m05_recovery_precip", "m06_exposure",
                         "m07_route1", "m08_route2", "m09_robustness", "m10_review"), 1))
MODULES = {3: "climatology", 4: "events", 5: "recovery", 6: "exposure",
           7: "route1", 8: "route2", 9: "robustness", 10: "review"}
REQUIRED = {3: (2,), 4: (3,), 5: (3, 4), 6: (3, 5), 7: (3, 5),
            8: (3, 5), 9: (3, 4, 5, 7, 8), 10: (3, 4, 5, 6, 7, 8, 9)}
OPTIONAL = {6: (1,), 7: (6,), 8: (6,), 9: (6,)}
PRODUCT = "era5land__era5land"


TITLES = {3: "格点支撑与气候态计算", 4: "骤旱事件识别与编目", 5: "恢复过程与极端降水接续",
          6: "植被与土壤水前期暴露", 7: "路线一：恢复风险与统计分析", 8: "路线二：随访与出口判定",
          9: "稳健性与规范敏感性复算", 10: "结果复核与样本流审计"}


class StageProgress:
    """集中显示处理状态，并将运行事件写入独立的 JSONL 日志。"""

    def __init__(self, args, plan, logdir, reuse):
        self.plan, self.logdir = plan, logdir
        self.stage_number = args.stage
        self.title = f"M{args.stage} · {TITLES[args.stage]}"
        self.total = 1 if args.check_only else 2 if reuse else 3
        mode = "试跑" if args.mode == "test" else "正式运行"
        if args.check_only:
            mode += " · 只读校验"
        elif args.resume:
            mode += " · 续跑"
        self.context = f"{mode}  |  ERA5-Land  |  {' 至 '.join(plan['dates'])}"
        self.live = LiveStatus(args.progress, self.show)
        self.panel = self.live.panel
        self.started = monotonic()
        self.state = (0, "准备校验输入", None, None, "", self.started)

    def __enter__(self):
        self.live.start()
        return self

    def __exit__(self, *_):
        self.live.close()

    def active(self, step, completed=None, total=None, unit=""):
        """报告当前操作，并可附带实际处理量和计量单位。"""
        done, _, previous, size, previous_unit, started = self.state
        if total != size or unit != previous_unit or (total and completed <= 0 < (previous or 0)):
            started = monotonic()
        self.state = (done, step, completed, total, unit, started)

    def advance(self, step):
        """标记当前步骤已完成，开始显示下一步。"""
        self.state = (self.state[0] + 1, step, None, None, "", monotonic())
        if self.live.mode == "plain" and self.state[0] < self.total:
            self.show()

    def event(self, name, message, level="INFO", **details):
        if self.logdir:
            with (self.logdir / "run.jsonl").open("ab") as stream:
                stream.write(m1.canonical(dict(timestamp=m1.utc_now(), stage=self.plan["stage"],
                                               run_id=self.plan["run_id"], attempt_id=self.plan["attempt_id"],
                                               event=name, level=level, message=message, **details)))

    def show(self):
        done, step, completed, total, unit, started = self.state
        now = monotonic()
        elapsed = now - self.started
        fraction = done / self.total
        partial = min(1, completed / total) if total else None
        detail = f"流程进度 {fraction:.0%}  ·  已完成 {done}/{self.total} 个步骤"
        if total:
            detail += f"  |  当前任务 {partial:.0%}，{completed}/{total} {unit}"
        timing = f"已用 {elapsed:.0f} 秒"
        if partial and now - started >= 1:
            seconds = now - started
            timing += f"  |  {completed/seconds:.1f} {unit}/秒  |  本任务预计剩余 {(total-completed)*seconds/completed:.0f} 秒"
        if self.panel.restore is not None:
            self.panel.draw(self.title, self.context, f"当前操作：{step}", detail,
                            f"成功 {done}  |  跳过 0  |  警告 0  |  失败 0  |  工作单元 {self.plan['workers']}",
                            timing, fraction, partial)
        else:
            self.panel.write(f"{self.title} | {self.context} | {detail} | {step} | {timing}\n")


def options(argv, stage=None):
    """解析共用参数。仅 M3 试跑可限制格点数。"""
    description, epilog = __doc__, None
    if stage is not None:
        entry = f".\\.venv\\Scripts\\python.exe -B src/fdwt/stages/{STAGES[stage]}/run.py"
        upstream = " ".join(f'--input "<M{i} 完成清单>"' for i in REQUIRED[stage])
        command = f'{entry} --config "<M0 的 run_config.json>"'
        epilog = (f"从项目根目录运行。请把尖括号中的占位值换成实际路径。\n"
                  f"试跑预览：\n  {command} --mode test {upstream} --dry-run\n"
                  f"通过预检后，可去掉 --dry-run 开始试跑。\n"
                  f"正式预览：\n  {command} --mode production --dry-run\n"
                  "试跑产物位于 data/frozen/test__…，日志位于 logs/test__…。\n"
                  "退出码：0 成功，1 执行失败，2 预检失败，130 中断。")
        description = f"M{stage} · {TITLES[stage]}\n读取冻结配置和上游完成清单，运行范围含上游起止日期。"
    parser = ChineseParser(description=description, epilog=epilog, formatter_class=ChineseHelp,
                           add_help=False)
    parser._positionals.title = "阶段"
    parser._optionals.title = "命令参数"
    parser.add_argument("-h", "--help", action="help", help="查看中文帮助和运行示例")
    if stage is None:
        parser.add_argument("stage", choices=[f"m{i}" for i in MODULES], help="选择 M3–M10 中的一个阶段")
    parser.add_argument("--mode", choices=("test", "production"), required=True,
                        help="必填。test 为试跑，production 为正式运行")
    parser.add_argument("--config", required=True, help="M0 冻结的 run_config.json")
    parser.add_argument("--input", action="append", default=[], metavar="COMPLETE_JSON",
                        help="每个直接上游提供一次。正式模式默认定位同一冻结运行")
    parser.add_argument("--product", choices=[PRODUCT], default=PRODUCT,
                        help=f"产品组合，默认且仅支持 {PRODUCT}（ERA5-Land）")
    parser.add_argument("--max-cells", type=int, help="仅 M3 试跑可用。保留前 N 个格点 ID")
    parser.add_argument("--chunk-cells", type=int, default=16,
                        help="M3 每个写出块的格点数。范围 1–128，默认 16")
    parser.add_argument("--workers", type=int, choices=[1], default=1, help="当前固定为单工作单元")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="只读元数据并显示计划")
    modes.add_argument("--check-only", action="store_true", help="只读核对上游文件校验值")
    parser.add_argument("--resume", action="store_true", help="核对身份和文件后复用已发布交接")
    parser.add_argument("--progress", choices=("auto", "panel", "plain"), default="auto",
                        help="auto 自动启用面板；panel 要求交互终端；plain 逐行输出")
    args = parser.parse_args(argv)
    args.stage = int(args.stage[1:]) if stage is None else stage
    if not 1 <= args.chunk_cells <= 128:
        parser.error("--chunk-cells 必须介于 1 和 128 之间")
    if args.max_cells is not None and (args.mode != "test" or args.stage != 3 or args.max_cells < 1):
        parser.error("--max-cells 仅用于 M3 的 test 模式，必须是正整数")
    validate_progress(parser, args)
    return args


def read_bundle(path, number, mode, m0, expected_run=None):
    """读取阶段清单和文件信息，预检不扫描 Parquet 数据内容。"""
    path = m1.safe(Path(os.path.abspath(ROOT / path)))
    stage = STAGES[number]
    m1.require(path.name == f"m{number}_complete.json" and path.parent.name == stage and
               path.parent.parent.parent == ROOT / "data/frozen", f"完成清单路径不符合约定：{path}")
    manifest = m1.read_json(path)
    identity = manifest["identity"]
    run = path.parent.parent.name
    m1.require(manifest["manifest_version"] == 1 and manifest["stage"] == stage and
               manifest["run_id"] == run and manifest["validation"] == "passed" and
               identity["mode"] == mode and identity["m0_run_id"] == m0["m0_run_id"] and
               identity["m0_sha256"] == m0["m0_sha256"] and
               identity["environment_hash"] == m0["environment_hash"],
               f"上游完成清单与当前模式或冻结配置不匹配：{path}")
    m1.require((run == m0["m0_run_id"] if mode == "production" else run.startswith("test__"))
               and (expected_run is None or expected_run == run), f"上游运行编号不匹配：{run}")
    if number == 2:
        m1.require(set(manifest["artifacts"]) == ARTIFACTS and manifest["schema_version"] == "v1.3.1",
                   "M2 交接文件清单或契约版本不符合要求")
    files = {}
    for name, expected in manifest["artifacts"].items():
        m1.require(Path(name).name == name and re.fullmatch(r"[A-Za-z0-9_.-]+", name)
                   and name not in (".", ".."), f"交接清单中的文件名不安全：{name}")
        member = m1.safe(path.parent / name)
        m1.require(member.is_file() and member.stat().st_size == expected["bytes"],
                   f"上游文件缺失或大小不符：{member}")
        files[name] = member
    return dict(path=path, manifest=manifest, files=files, stamp=fingerprint(path))


def code_files(m0, number, bundles, production):
    """计算参与源码的哈希，并拒绝生产模式使用未固定或不匹配的源码。"""
    files = dict(m0["code"])
    paths = [Path(__file__), Path(__file__).with_name("terminal_panel.py"),
             *sorted((ROOT / "src/fdwt/stages" / STAGES[number]).glob("*.py"))]
    paths += list((ROOT / "src/fdwt/stages/m02_preprocess").glob("*.py"))
    for path in paths:
        files[path.relative_to(ROOT).as_posix()] = fingerprint(path)["sha256"]
    for bundle in bundles.values():
        for name, sha in bundle["manifest"]["identity"]["code"].items():
            actual = fingerprint(m1.safe(ROOT / name))["sha256"]
            m1.require(actual == sha, f"上游源码已变化，需用当前版本重新生成交接：{name}")
            files[name] = actual
    if production:
        names = sorted(files)
        m1.require(not m1.git("status", "--porcelain", "--untracked-files=all", "--", *names)
                   and set(m1.git("ls-files", "--", *names).splitlines()) == set(names),
                   "正式运行要求所有参与源码已纳入 Git 且工作区无改动")
    return files


def prepare(args):
    """只读解析上游、运行身份、路径和资源预算。"""
    params, registry, m0 = m1.load_m0(args.config, args.mode)
    config = m1.safe(Path(os.path.abspath(ROOT / args.config)))
    module = importlib.import_module(f"fdwt.stages.{STAGES[args.stage]}.{MODULES[args.stage]}")
    mandatory = REQUIRED[args.stage]
    allowed = mandatory + OPTIONAL.get(args.stage, ())
    bundles = {}
    for path in args.input:
        match = re.fullmatch(r"m(\d+)_complete\.json", Path(path).name)
        m1.require(match is not None, f"完成清单须命名为 mN_complete.json：{path}")
        number = int(match[1])
        m1.require(number in allowed and number not in bundles, f"M{number} 清单重复或不属于本阶段依赖")
        bundles[number] = read_bundle(path, number, args.mode, m0)
    production = args.mode == "production"
    for number in allowed:
        if number not in bundles and production:
            candidate = ROOT / "data/frozen" / m0["m0_run_id"] / STAGES[number] / f"m{number}_complete.json"
            if number in mandatory or candidate.exists():
                bundles[number] = read_bundle(candidate, number, args.mode, m0)
    m1.require(all(number in bundles for number in mandatory), f"缺少必需的上游完成清单。请为这些阶段分别提供 --input：{', '.join(f'M{i}' for i in mandatory if i not in bundles)}")
    ancestors = {}
    for number, bundle in bundles.items():
        relative = bundle["path"].relative_to(ROOT).as_posix()
        info = bundle["manifest"]["identity"]
        lineage = dict(info.get("lineage", {}))
        if number == 2:
            m1_path = ROOT / "data/frozen" / info["m1_run_id"] / STAGES[1] / "m1_complete.json"
            lineage["1"] = dict(path=m1_path.relative_to(ROOT).as_posix(), sha256=info["m1_sha256"])
        key = "m1_modis" if number == 1 and args.stage == 6 and not production else str(number)
        lineage[key] = dict(path=relative, sha256=bundle["stamp"]["sha256"])
        for key, member in lineage.items():
            m1.require(key not in ancestors or ancestors[key] == member, f"mixed M{key} lineages")
            ancestors[key] = member
    base = bundles[2 if args.stage == 3 else 3]["manifest"]["identity"]
    dates = [base["start"], base["end"]] if args.stage == 3 else base["dates"]
    identity = dict(mode=args.mode, m0_run_id=m0["m0_run_id"], m0_sha256=m0["m0_sha256"],
                    environment_hash=m0["environment_hash"], code=code_files(m0, args.stage, bundles, production),
                    dates=dates, product=PRODUCT, lineage=ancestors, max_cells=args.max_cells,
                    seed_id=params["random"]["seed_id"])
    run = m0["m0_run_id"] if production else (
        f"test__{m0['m0_run_id']}__m{args.stage}_{m1.object_hash(identity)[:12]}")
    stage, attempt = STAGES[args.stage], uuid4().hex
    target = m1.safe(ROOT / "data/frozen" / run / stage)
    work = m1.safe(ROOT / "data/interim" / run / stage / attempt)
    logs = m1.safe(ROOT / "logs" / run / stage / attempt)
    outputs = m1.safe(ROOT / "outputs" / run)
    if not (args.dry_run or args.check_only):
        m1.require(args.resume or not target.exists(), "目标已有发布结果，可用 --resume 校验后复用")
    plan = dict(stage=stage, run_id=run, attempt_id=attempt, dates=dates, identity=identity,
                target=str(target), work=str(work), logs=str(logs), outputs=str(outputs),
                workers=1, chunk_cells=args.chunk_cells, input_manifests=len(bundles),
                input_files=sum(len(bundle["files"]) for bundle in bundles.values()),
                free_bytes=shutil.disk_usage(ROOT).free, memory_bytes=m1.available_memory())
    ctx = Context(args, params, registry, config, bundles, plan)
    module.preflight(ctx)
    return ctx, module


def stage_schema(registry, contract, metadata):
    """复用 M2 的基础类型，并补充列表和时间戳等字段类型。"""
    specs = registry["tables"][contract]["fields"]
    extras = {"array": pa.list_(pa.string()), "object": pa.string(),
              "datetime": pa.timestamp("us", tz="UTC")}
    simple = {k: v for k, v in specs.items() if v["type"] not in extras}
    base = schema_for({"tables": {contract: {"fields": simple}}}, contract, metadata)
    fields = [base.field(k) if k in simple else pa.field(k, extras[v["type"]], v["nullable"])
              for k, v in specs.items()]
    return pa.schema(fields, metadata=metadata)

class Context:
    """管理本次尝试的阶段读写，冻结表采用登记的 Arrow 契约。"""
    def __init__(self, args, params, registry, config, bundles, plan):
        self.args, self.params, self.config = args, params, config
        self.registry = dict(registry, tables=dict(registry["tables"]))
        self.bundles, self.plan = bundles, plan
        self.work = Path(plan["work"])
        self.tables = self.work / "handoff"
        self.counts = {}
        self.contracts = {}
        self.metadata = {key: value.encode() for key, value in {
            "protocol_version": "v1.3.1", "run_id": plan["run_id"], "stage": plan["stage"],
            "mode": args.mode, "seed_id": params["random"]["seed_id"],
            "params_hash": fingerprint(config.parent / "params.yaml")["sha256"],
            "code_hash": m1.object_hash(plan["identity"]["code"]),
            "input_hash": m1.object_hash(plan["identity"]["lineage"]),
        }.items()}

    def path(self, number, name):
        return self.bundles[number]["files"][name]

    def rows(self, number, name, columns=None):
        return pq.read_table(self.path(number, name), columns=columns).to_pylist()

    def schema(self, contract):
        """按冻结契约取得字段类型、单位和本次运行元数据。"""
        return stage_schema(self.registry, contract, self.metadata)

    def report(self, category, name, value):
        """发布前在尝试目录内写入 UTF-8 结构化报告。"""
        path = m1.safe(self.work / category / name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(m1.canonical(value))

    def derived_contract(self, name, base, extension, fields):
        """为显式登记的兼容视图合并字段，保持基础表主键。"""
        tables = self.registry["tables"]
        m1.require(name not in tables, f"duplicate derived contract: {name}")
        source = tables[base]
        tables[name] = dict(source, fields={**source["fields"], **{
            field: tables[extension]["fields"][field] for field in fields}})

    def csv(self, category, name, fields, rows):
        path = m1.safe(self.work / category / name)
        path.parent.mkdir(parents=True, exist_ok=True)
        m1.require(not path.exists(), f"duplicate report: {name}")
        m1.write_csv(path, fields, rows)

    @contextmanager
    def writer(self, name, contract):
        schema = self.schema(contract)
        keys = self.registry["tables"][contract]["primary_key"]
        path = m1.safe(self.tables / name)
        m1.require(not path.exists(), f"duplicate handoff: {name}")
        self.counts[name] = 0
        self.contracts[name] = contract
        last = None
        with pq.ParquetWriter(path, schema, compression="zstd") as writer:
            def append(rows):
                nonlocal last
                rows = list(rows)
                if not rows:
                    return
                ordered = sorted(rows, key=lambda row: tuple(row[key] for key in keys))
                identifiers = [tuple(row[key] for key in keys) for row in ordered]
                m1.require((last is None or last < identifiers[0]) and
                           all(a < b for a, b in zip(identifiers, identifiers[1:])),
                           f"主键重复或顺序错误：{name}")
                last = identifiers[-1]
                for row in ordered:
                    m1.require(set(row) <= set(schema.names), f"表中出现未登记字段：{name}")
                table = make_table(schema, {field: [row.get(field) for row in ordered] for field in schema.names})
                writer.write_table(table)
                self.counts[name] += len(rows)
            yield append

    def write(self, name, contract, rows):
        with self.writer(name, contract) as append:
            append(rows)


def verify(ctx):
    """核对上游完成清单和每个成员的文件指纹。"""
    total = ctx.plan["input_manifests"] + ctx.plan["input_files"]
    done = 0
    for bundle in ctx.bundles.values():
        ctx.progress.active(f"正在校验上游清单 {bundle['path'].name}", done, total, "份文件")
        m1.require(fingerprint(bundle["path"]) == bundle["stamp"], "上游完成清单在运行期间发生变化")
        done += 1
        for name, path in bundle["files"].items():
            ctx.progress.active(f"正在校验上游文件 {name}", done, total, "份文件")
            m1.require(fingerprint(path) == bundle["manifest"]["artifacts"][name],
                       f"上游文件校验值不一致：{path}")
            done += 1


def publish(ctx):
    """移动本次尝试的报告，并原子发布带完成清单的交接目录。"""
    plan = ctx.plan
    target = Path(plan["target"])
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = m1.safe(target.with_suffix(".lock"))
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(plan["attempt_id"])
    marker = ctx.tables / f"m{ctx.args.stage}_complete.json"
    try:
        m1.require(not target.exists(), "目标已有发布结果，可用 --resume 校验后复用")
        ctx.progress.active("正在计算交接文件校验值")
        artifacts = {p.name: fingerprint(p) for p in ctx.tables.iterdir()}
        for name, count in ctx.counts.items():
            m1.require(pq.ParquetFile(ctx.tables / name).metadata.num_rows == count,
                       f"输出行数与交接清单不符：{name}")
        reports = {}
        for category in ("tables", "reports", "models", "figures"):
            source = ctx.work / category
            if source.is_dir():
                dest = m1.safe(Path(plan["outputs"]) / category / plan["stage"] / plan["attempt_id"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                for path in source.iterdir():
                    reports[(dest / path.name).relative_to(ROOT).as_posix()] = fingerprint(path)
                os.rename(source, dest)
        payload = dict(manifest_version=1, schema_version="v1.3.1", validation="passed",
                       stage=plan["stage"], run_id=plan["run_id"], attempt_id=plan["attempt_id"],
                       identity=plan["identity"], artifacts=artifacts, reports=reports,
                       row_counts=ctx.counts, table_contracts=ctx.contracts)
        with marker.open("xb") as stream:
            stream.write(m1.canonical(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(ctx.tables, target)
        return payload
    finally:
        marker.unlink(missing_ok=True)
        lock.unlink()


def show_plan(args, plan):
    """交互预览显示中文计划，重定向时保留原有的 JSON 接口。"""
    if not sys.stdout.isatty():
        print(m1.canonical(plan).decode().strip(), flush=True)
    elif args.dry_run:
        mode = "试跑" if args.mode == "test" else "正式运行"
        print(f"M{args.stage} · {TITLES[args.stage]}\n运行计划（只预览）")
        print(f"模式：{mode}  |  产品：ERA5-Land  |  {' 至 '.join(plan['dates'])}")
        print(f"输入：{plan['input_manifests']} 份上游清单，{plan['input_files']} 份文件")
        print(f"工作单元：{plan['workers']}")
        if args.stage == 3:
            limit = args.max_cells if args.max_cells is not None else "沿用上游"
            print(f"格点上限：{limit}  |  每块：{plan['chunk_cells']} 个格点")
        print(f"预计流程：校验输入 → {TITLES[args.stage]} → 验收并发布")
        print(f"预计交接目录：{plan['target']}\n预计日志：{Path(plan['logs']) / 'run.jsonl'}")


def reuse_publication(ctx):
    """逐个验证已发布的表和报告，供续跑复用。"""
    plan = ctx.plan
    path = Path(plan["target"]) / f"m{ctx.args.stage}_complete.json"
    identity = plan["identity"]
    bundle = read_bundle(path, ctx.args.stage, ctx.args.mode, identity, plan["run_id"])
    manifest = bundle["manifest"]
    m1.require(manifest["identity"] == identity, "已有结果的运行身份不同，请核对输入与代码版本")
    for name, stamp in manifest["artifacts"].items():
        ctx.progress.active(f"正在核对已有交接文件 {name}")
        m1.require(fingerprint(bundle["files"][name]) == stamp, f"已有交接文件校验值不一致：{name}")
    for name, stamp in manifest["reports"].items():
        ctx.progress.active(f"正在核对已有报告 {Path(name).name}")
        m1.require(fingerprint(m1.safe(ROOT / name)) == stamp, f"已有报告校验值不一致：{name}")
    return manifest


def show_summary(progress, final, result, error):
    """恢复普通终端后输出结果、日志和本次尝试的实际位置。"""
    plan = progress.plan
    elapsed = monotonic() - progress.started
    done = progress.state[0]
    skipped = int(final == "reused")
    status = {"published": "完成，已发布结果", "reused": "完成，已复用已有结果",
              "checked": "完成，上游文件校验通过", "failed": "失败", "interrupted": "已中断"}
    lines = [f"{progress.title} · {status[final]}",
             f"流程：{done}/{progress.total} 步  |  成功 {done-skipped}  |  跳过 {skipped}"
             f"  |  警告 0  |  失败 {int(final == 'failed')}  |  用时 {elapsed:.1f} 秒"]
    if error is not None:
        lines.append(f"原因：{error}")
    if result is not None:
        lines.append(f"完成清单：{Path(plan['target']) / ('m' + str(progress.stage_number) + '_complete.json')}")
        counts = result["row_counts"]
        lines.append(f"交接结果：{len(counts)} 张表，共 {sum(counts.values()):,} 行")
        for folder in sorted({(ROOT / path).parent for path in result["reports"]}):
            lines.append(f"结果表或报告：{folder}")
    if final in ("failed", "interrupted") and Path(plan["work"]).exists():
        lines.append(f"本次尝试保留在：{plan['work']}")
    logfile = Path(plan["logs"]) / "run.jsonl"
    lines.append(f"运行日志：{logfile}" if logfile.is_file() else "运行日志：本次未写入日志文件")
    progress.panel.write("\n".join(lines) + "\n")


def main(argv=None, stage=None):
    args = options(argv, stage)
    try:
        ctx, module = prepare(args)
        show_plan(args, ctx.plan)
        if args.dry_run:
            return 0
    except KeyboardInterrupt:
        print(f"M{args.stage} · 已中断运行前检查", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError, ImportError, subprocess.SubprocessError, yaml.YAMLError) as error:
        print(f"M{args.stage} · 运行前检查失败：{error}\n可用 --help 查看输入要求和试跑示例。", file=sys.stderr)
        return 2
    plan = ctx.plan
    logs = None if args.check_only else Path(plan["logs"])
    reuse = args.resume and not args.check_only and Path(plan["target"]).exists()
    ctx.progress = progress = StageProgress(args, plan, logs, reuse)
    final, result, error, bytes_out, status = "failed", None, None, 0, 1
    try:
        if logs:
            logs.mkdir(parents=True, exist_ok=False)
        progress.event("started", "开始运行阶段", plan=plan)
        with progress:
            verify(ctx)
            if args.check_only:
                progress.advance("上游文件校验通过")
                final = "checked"
            elif reuse:
                progress.advance("正在校验可复用的结果")
                result = reuse_publication(ctx)
                progress.advance("已有结果校验通过")
                final = "reused"
            else:
                progress.advance(f"正在{TITLES[args.stage]}")
                m1.require(not Path(plan["target"]).exists(), "目标已有发布结果，可用 --resume 校验后复用")
                ctx.tables.mkdir(parents=True, exist_ok=False)
                module.run(ctx)
                bytes_out = sum(p.stat().st_size for p in ctx.tables.iterdir())
                progress.advance("正在验收并发布结果")
                for bundle in ctx.bundles.values():
                    m1.require(fingerprint(bundle["path"]) == bundle["stamp"], "上游完成清单在运行期间发生变化")
                ctx.report("reports", f"module_io_m{args.stage}.json", dict(
                    module_id=f"M{args.stage}", run_id=plan["run_id"], attempt_id=plan["attempt_id"],
                    upstream=plan["identity"]["lineage"], rows=ctx.counts, status="validated",
                    seed_id=ctx.params["random"]["seed_id"]))
                result = publish(ctx)
                progress.advance("结果已发布")
                final = "published"
        status = 0
    except KeyboardInterrupt:
        final, status = "interrupted", 130
    except Exception as exc:
        error = exc
        if logs and logs.is_dir():
            try:
                progress.event("exception", str(exc), "ERROR", traceback=traceback.format_exc())
            except OSError:
                pass
    elapsed = monotonic() - progress.started
    if logs and logs.is_dir():
        try:
            progress.event("finished", final, elapsed_s=elapsed, peak_memory_bytes=m1.peak_memory(),
                           completed_steps=progress.state[0], failed=int(status == 1),
                           work=str(ctx.work), outputs=plan["outputs"], logs=plan["logs"],
                           validation="passed" if status == 0 else "incomplete",
                           output_bytes=bytes_out, output_mib_per_s=bytes_out/2**20/max(elapsed, 1e-9))
        except OSError as exc:
            print(f"写入运行日志失败：{exc}", file=sys.stderr)
    show_summary(progress, final, result, error)
    return status


if __name__ == "__main__":
    sys.exit(main())
