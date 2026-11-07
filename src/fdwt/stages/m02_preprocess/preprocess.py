r"""M2 · ERA5-Land 日候预处理

读取冻结 M0 配置及 M1 已放行交接，向 M3 提供 UTC 候日历、日表、候表及静态网格。
正式模式覆盖冻结事件期。试跑日期含端点，并读取相交的完整候。
从项目根目录运行，将 M0_ID 和 M1_TEST_ID 替换为对应运行编号：
  .venv\Scripts\python.exe -B src/fdwt/stages/m02_preprocess/preprocess.py --config metadata/runs/M0_ID/run_config.json --mode test --m1 data/frozen/M1_TEST_ID/m01_source_audit/m1_complete.json --start 2004-02-27 --end 2004-02-29 --dry-run
  .venv\Scripts\python.exe -B src/fdwt/stages/m02_preprocess/preprocess.py --config metadata/runs/M0_ID/run_config.json --mode production
预览通过后去掉 --dry-run 即可试跑。试跑交接位于 data/frozen/test__.../m02_preprocess。
退出码：0 成功，1 执行失败，2 预检失败，130 中断。
"""

from contextlib import ExitStack
from datetime import date
import os
from pathlib import Path
import shutil
import subprocess
import sys
from time import monotonic
import traceback
from uuid import uuid4

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from inputs import MonthlyReader, fingerprint, load_sources, m1, member_hashes
from terminal_panel import ChineseHelp, ChineseParser, LiveStatus, validate_progress
from physics import (DAY, FLUX, STATE, VALUES, calendar_rows, cell_area_km2,
                     identity_support, pentad_flux, pentad_state)

STAGE = "m02_preprocess"
PRODUCT = "era5_land"
SUPPORT = "era5land_grid_full_footprint"
BASE_TABLES = {
    "time_pentad_calendar": "time_pentad_calendar",
    "grid_day_era5_land": "grid_day",
    "grid_pentad_base_era5_land": "grid_pentad_base",
}
ARTIFACTS = {name + ".parquet" for name in BASE_TABLES} | {"grid_static_covariates.parquet"}
REPORTS = {"m2_accumulation_tests.json", "m2_sign_and_balance_tests.json",
           "m2_area_conservation.csv", "m2_pentad73_audit.csv", "m2_coverage.csv",
           "sop_unit_test_result.csv"}
STATIC_MISSING = {
    "annual_p_mean_mm": "registered_2000_2023_period_not_released",
    "apr_sep_t2m_mean_c": "registered_2000_2023_period_not_released",
    "sand_0_30": "soilgrids_not_released", "clay_0_30": "soilgrids_not_released",
    "elevation_m": "nasadem_not_released", "slope_deg": "nasadem_not_released",
    "land_cover": "landcover_not_released_for_primary_profile",
}


def options(argv):
    parser = ChineseParser(description=__doc__, formatter_class=ChineseHelp, add_help=False)
    parser._optionals.title = "命令参数"
    parser.add_argument("-h", "--help", action="help", help="查看中文帮助和试跑示例")
    parser.add_argument("--config", required=True, type=Path, help="M0 冻结的 run_config.json 路径")
    parser.add_argument("--mode", choices=("test", "production"), required=True,
                        help="必填。test 使用 M1 试跑清单，production 使用正式交接")
    parser.add_argument("--m1", type=Path, help="试跑必填。正式运行默认读取同一冻结运行下的 m1_complete.json")
    parser.add_argument("--start", type=date.fromisoformat, help="仅试跑可用，首个输出日含当天（YYYY-MM-DD）")
    parser.add_argument("--end", type=date.fromisoformat, help="仅试跑可用，末个输出日含当天（YYYY-MM-DD）")
    parser.add_argument("--product", choices=(PRODUCT,), default=PRODUCT, help="默认且仅支持 ERA5-Land")
    parser.add_argument("--workers", type=int, choices=(1,), default=1, help="当前固定为 1，按月串行读取")
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--dry-run", action="store_true", help="只预览输入范围、完整候和目标路径")
    checks.add_argument("--check-only", action="store_true", help="只读核对上游清单和原始文件校验值")
    parser.add_argument("--progress", choices=("auto", "panel", "plain"), default="auto",
                        help="auto 自动启用面板；panel 要求交互终端；plain 逐行输出")
    parser.add_argument("--resume", action="store_true", help="校验并复用已发布交接，或另起一次尝试")
    args = parser.parse_args(argv)
    validate_progress(parser, args)
    return args


def code_identity(m0, mode):
    """绑定参与源码，正式运行要求源码已纳入 Git 且无改动。"""
    paths = sorted(Path(__file__).resolve().parent.glob("*.py"))
    code = dict(m0["code"])
    code.update({path.relative_to(m1.ROOT).as_posix(): fingerprint(path)["sha256"] for path in paths})
    if mode == "production":
        names = [*code, "docs/protocol_sources.md", "workflow/README.md"]
        m1.require(not m1.git("status", "--porcelain", "--untracked-files=all", "--", *names)
                   and set(m1.git("ls-files", "--", *names).splitlines()) == set(names),
                   "participating M2 code or workflow instructions are not fixed in Git")
    return code


def schema_for(registry, name, metadata):
    """使用冻结的 Arrow 字段类型、单位和可空性。"""
    types = {"integer": pa.int32(), "number": pa.float64(), "date": pa.date32(),
             "boolean": pa.bool_(), "string": pa.string()}
    fields = [pa.field(key, types[spec["type"]], spec["nullable"],
                       metadata={"unit": spec["unit"]} if "unit" in spec else None)
              for key, spec in registry["tables"][name]["fields"].items()]
    return pa.schema(fields, metadata=metadata)


def make_table(schema, columns):
    """核对字段集合、数值有限性和必填列。"""
    m1.require(set(columns) == set(schema.names), "output differs from frozen field set")
    arrays = []
    for field in schema:
        values = columns[field.name]
        if pa.types.is_floating(field.type):
            values = np.asarray(values, dtype=float)
            m1.require(not np.isinf(values).any(), f"nonfinite output: {field.name}")
            array = pa.array(values, mask=np.isnan(values), type=field.type)
        else:
            array = pa.array(values, type=field.type)
        m1.require(field.nullable or not array.null_count, f"missing required field: {field.name}")
        arrays.append(array)
    return pa.Table.from_arrays(arrays, schema=schema)


def grid(params):
    """按冻结研究域生成稳定格点编号及完整格点面积。"""
    domain = params["grid"]["domain"]
    west, south, east, north = domain["cell_center_bounds"]
    lat = np.linspace(north, south, domain["latitude_count"])
    lon = np.linspace(west, east, domain["longitude_count"])
    resolution = domain["resolution_deg"]
    m1.require(domain["coordinate_reference_system"] == "EPSG:4326" and
               np.allclose(np.diff(lat), -resolution) and np.allclose(np.diff(lon), resolution),
               "frozen grid is inconsistent with native-aligned EPSG:4326")
    size = len(lat) * len(lon)
    ids = [f"g{i:06d}" for i in range(1, size + 1)]
    area = np.repeat(cell_area_km2(lat, resolution), len(lon))
    return lat, lon, ids, area


def qc_columns(values, fraction, runoff_available=True):
    """汇总主变量及可选变量支撑，同时保留字段级空值。"""
    core = np.isfinite(values["sm_0_100_mm"]) & np.isfinite(values["p_mm"])
    primary = core & np.logical_and.reduce([np.isfinite(values[field]) for field in
                                         ("sm_deep_mm", "t2m_c", "et_loss_mm", "pet_loss_mm")])
    reasons = np.where(primary, "subsurface_runoff_not_released", "native_qc_or_time_incomplete")
    if not runoff_available:
        reasons = np.where(primary, "surface_and_subsurface_runoff_not_released", reasons)
    return dict(valid_native_area_frac=fraction, qc_status=np.where(primary, "partial", "missing_primary"),
                missing_reason=reasons)


def assess_area(values, area, threshold):
    """核对同一网格有效区域在质控前后的水量守恒。"""
    rows = []
    for name, value in values.items():
        if name == "t2m_c":
            continue
        valid = np.isfinite(value)
        if not valid.any():
            rows.append(dict(field=name, cells=0, before_m3=None, after_m3=None, status="no_support"))
            continue
        target, fractions = identity_support(value, threshold)
        # 1 平方公里内的 1 毫米水深对应 1,000 立方米。
        before = float(np.dot(value[valid], area[valid]) * 1000)
        after = float(np.nansum(target * area * fractions) * 1000)
        m1.require(np.isclose(before, after, rtol=1e-10, atol=1e-4),
                   f"water volume fails same-grid conservation: {name}")
        rows.append(dict(field=name, cells=int(valid.sum()), before_m3=before, after_m3=after,
                         status="passed"))
    return rows


def static_table(params, ids, lat, lon, area, metadata):
    """按补充契约记录静态格点及暂不可用的协变量。"""
    count = len(ids)
    columns = dict(grid_version=[params["grid"]["main_version"]] * count, grid_id=ids,
                   lon=np.tile(lon, len(lat)), lat=np.repeat(lat, len(lon)),
                   area_km2=area, boundary_include=[True] * count)
    fields = [pa.field("grid_version", pa.string(), False), pa.field("grid_id", pa.string(), False),
              pa.field("lon", pa.float64(), False, metadata={"unit": "degree_east"}),
              pa.field("lat", pa.float64(), False, metadata={"unit": "degree_north"}),
              pa.field("area_km2", pa.float64(), False, metadata={"unit": "km2"}),
              pa.field("boundary_include", pa.bool_(), False)]
    for name, reason in STATIC_MISSING.items():
        kind = pa.string() if name == "land_cover" else pa.float64()
        fields.extend((pa.field(name, kind), pa.field(name + "_missing_reason", pa.string(), False)))
        columns[name] = [None] * count
        columns[name + "_missing_reason"] = [reason] * count
    # make_table 将数值缺测映射为 Arrow 空值，格点主键按网格顺序一次生成。
    return make_table(pa.schema(fields, metadata={**metadata, "static_contract": "m2_grid_r1"}), columns)


class Progress:
    """集中显示日候处理或文件校验状态，并单独保存运行日志。"""

    def __init__(self, args, run, attempt, start, end, total, logdir, unit):
        mode = "试跑" if args.mode == "test" else "正式运行"
        mode += " · 只读校验" if args.check_only else " · 续跑" if args.resume else ""
        self.title = "M2 · ERA5-Land 日候预处理"
        self.context = f"{mode}  |  ERA5-Land  |  {start} 至 {end}  |  1 个工作单元"
        self.run, self.attempt, self.logdir, self.total = run, attempt, logdir, total
        self.started, self.done, self.step = monotonic(), 0, "正在准备输入"
        self.unit = unit
        self.live = LiveStatus(args.progress, self.show)

    def show(self):
        """按实际写出候数或已核对的文件数更新面板。"""
        done = self.done
        elapsed = monotonic() - self.started
        ratio = done / max(self.total, 1)
        detail = f"处理进度 {ratio:.0%}  |  已处理 {done}/{self.total} {self.unit}"
        stats = f"成功 {done}  |  跳过 0  |  警告 0  |  失败 0"
        timing = f"已用 {elapsed:.0f} 秒"
        if 0 < done < self.total and elapsed >= 1:
            timing += f"  |  {done/elapsed:.1f} {self.unit}/秒  |  预计剩余 {(self.total-done)*elapsed/done:.0f} 秒"
        panel = self.live.panel
        if panel.restore is not None:
            panel.draw(self.title, self.context, f"当前操作：{self.step}", detail, stats, timing, ratio)
        else:
            panel.write(" | ".join((self.title, self.context, self.step, detail, stats, timing)) + "\n")

    def active(self, step):
        self.step = step

    def event(self, name, message, level="INFO", **details):
        if self.logdir:
            with (self.logdir / "run.jsonl").open("ab") as stream:
                stream.write(m1.canonical(dict(timestamp=m1.utc_now(), stage=STAGE, run_id=self.run,
                                               attempt_id=self.attempt, event=name, level=level,
                                               message=message, **details)))

    def finish(self, final, **details):
        self.live.close()
        self.event("finished", final, elapsed_s=monotonic() - self.started,
                   peak_memory_bytes=m1.peak_memory(), processed_pentads=self.done if self.unit == "候" else 0,
                   verified_files=self.done if self.unit != "候" else 0,
                   failed=int(final == "failed"), **details)


def numeric_checks():
    """发布交接前执行 M2 原生处理规程的数值自检。"""
    from physics import era_storage, interval_total, water_mm

    leap = calendar_rows(date(2020, 1, 1), date(2020, 12, 31))
    common = calendar_rows(date(2021, 1, 1), date(2021, 12, 31))
    calendar_ok = all(
        len(rows) == 73 and sum(row["n_days"] for row in rows) == days and
        all(left["end_date"] + DAY == right["start_date"] and
            left["next_t_abs"] == right["t_abs"]
            for left, right in zip(rows, rows[1:]))
        for rows, days in ((leap, 366), (common, 365)))
    checks = {
        "utc_calendar": calendar_ok and leap[-1]["next_t_abs"] == common[0]["t_abs"],
        "step_midnight": np.isclose(interval_total(np.linspace(0.0002, 0.005, 24), "processing_step_accumulation"), 0.005),
        "increments": np.isclose(interval_total(np.full(24, 0.000125), "increment"), 0.003),
        "interval_total": np.isclose(interval_total(np.array([0.003]), "interval_total"), 0.003),
        "signed_loss": np.allclose(water_mm(np.array([-0.003, 0.001]), -1000), [3, -1])
                       and water_mm(3, 1) == 3,
        "era_layer_storage": np.allclose(era_storage([0.1, 0.2, 0.3, 0.4]), [265, 756]),
        "leap_pentad73": leap[-1]["n_days"] == 6 and common[-1]["n_days"] == 5
                         and pentad_flux(np.ones(6)) * 5 / 6 == 5,
        "flux_missing": np.isnan(pentad_flux(np.array([2, 0, 1, np.nan, 3]))),
        "state_completeness": pentad_state(np.array([1, np.nan, np.nan, 2, 3, 4]), 4, 2)[0] == 2.5
                              and np.isnan(pentad_state(np.array([1, np.nan, np.nan, np.nan, 2, 3]), 4, 2)[0]),
        "aligned_area_qc": np.isnan(identity_support(np.array([np.nan]), 0.7)[0][0]),
    }
    m1.require(all(checks.values()), "M2 numerical self-check failed")
    return [dict(test=name, status="passed") for name in checks]


def write_results(work, reports, plan, registry, params, reader, dates, calendar, coords, progress):
    """按完整候写出行组，并校验日表及候表。"""
    lat, lon, ids, area = coords
    size = len(ids)
    metadata = {"stage": STAGE, "protocol_version": params["protocol"]["version"],
                "run_id": plan["run_id"], "day_boundary": "UTC", "grid_support": SUPPORT,
                "m1_sha256": plan["identity"]["m1_sha256"]}
    schemas = {name: schema_for(registry, contract, metadata) for name, contract in BASE_TABLES.items()}
    cal = make_table(schemas["time_pentad_calendar"],
                     {name: [row[name] for row in calendar] for name in schemas["time_pentad_calendar"].names})
    pq.write_table(cal, work / "time_pentad_calendar.parquet")
    static = static_table(params, ids, lat, lon, area, metadata)
    pq.write_table(static, work / "grid_static_covariates.parquet")

    start, end = dates
    threshold = params["spatial"]["valid_area_min"]
    min_days = params["time"]["state_min_valid_days"]
    max_gap = params["time"]["state_max_consecutive_missing_days"]
    fixed = dict(product_code=[PRODUCT] * size, grid_version=[params["grid"]["main_version"]] * size,
                 grid_id=ids, support_id=[SUPPORT] * size)
    area_audit, coverage, audit73 = [], [], []
    ranges = {name: [None, None] for name in VALUES}
    day_name, pentad_name = "grid_day_era5_land", "grid_pentad_base_era5_land"
    expected_days = (end - start).days + 1
    written_days = 0
    numeric = numeric_checks()
    with ExitStack() as stack:
        writers = {name: stack.enter_context(pq.ParquetWriter(work / (name + ".parquet"), schemas[name],
                                                               compression="zstd"))
                   for name in (day_name, pentad_name)}
        for row in calendar:
            m1.require(shutil.disk_usage(work).free > plan["partition_reserve_bytes"],
                       "disk space below safety reserve")
            first, n = row["start_date"], row["n_days"]
            days = [first + i * DAY for i in range(n)]
            raw_days = [reader.day(day) for day in days]
            progress.active(f"正在聚合 {first} 至 {days[-1]} 的日值与候值")
            daily = {name: np.stack([block[name] for block in raw_days]) for name in VALUES}
            for name in VALUES:
                daily[name], _ = identity_support(daily[name], threshold)
            for name in ("sm_0_100_mm", "p_mm"):
                m1.require(np.isfinite(daily[name]).any(axis=1).all(),
                           f"no usable {name} cells for a day in pentad {row['t_abs']}")
            pentad = {name: pentad_flux(daily[name]) for name in FLUX}
            states = {name: pentad_state(daily[name], min_days, max_gap) for name in STATE}
            pentad.update({name: value[0] for name, value in states.items()})
            progress.active(f"正在写入 {first} 至 {days[-1]} 的日表和候表")
            for i, day in enumerate(days):
                if not start <= day <= end:
                    continue
                d = {name: values[i] for name, values in daily.items()}
                fraction = (np.isfinite(d["sm_0_100_mm"]) & np.isfinite(d["p_mm"])).astype(float)
                sro_end = reader.scopes["surface_runoff"]
                cols = dict(fixed, date=[day] * size, **d, **qc_columns(d, fraction, sro_end and day <= sro_end))
                writers[day_name].write_table(make_table(schemas[day_name], cols))
                written_days += 1

            tags = ("t_abs", "year", "pentad", "start_date", "end_date", "n_days")
            fields = {name: [row[name]] * size for name in tags}
            _, count, longest = states["sm_0_100_mm"]
            complete = np.logical_and.reduce([np.isfinite(pentad[name])
                                               for name in ("p_mm", "et_loss_mm", "pet_loss_mm")])
            fraction = (np.isfinite(pentad["sm_0_100_mm"]) & np.isfinite(pentad["p_mm"])).astype(float)
            sro_end = reader.scopes["surface_runoff"]
            cols = dict(fixed, **fields, **pentad, p_mm_per_day=pentad["p_mm"] / n,
                        p_5day_equivalent_mm=pentad["p_mm"] * 5 / n, state_valid_days=count,
                        state_max_consecutive_missing_days=longest, flux_complete_flag=complete,
                        **qc_columns(pentad, fraction, sro_end and days[-1] <= sro_end))
            writers[pentad_name].write_table(make_table(schemas[pentad_name], cols))

            for name in VALUES:
                valid = np.isfinite(daily[name])
                n_valid = int(valid.sum())
                if n_valid:
                    low, high = float(np.nanmin(daily[name])), float(np.nanmax(daily[name]))
                    old_low, old_high = ranges[name]
                    ranges[name] = [low if old_low is None else min(low, old_low),
                                    high if old_high is None else max(high, old_high)]
                accepted = int(np.isfinite(pentad[name]).sum())
                reason = {
                    "runoff_subsurface_mm": "subsurface_runoff_not_released",
                    "runoff_mm": "subsurface_runoff_not_released",
                    "balance_mm": "subsurface_runoff_not_released",
                }.get(name, "native_qc_or_time_incomplete")
                if name == "runoff_surface_mm" and not (sro_end and days[-1] <= sro_end):
                    reason = "surface_runoff_not_released_for_full_pentad"
                coverage.append(dict(t_abs=row["t_abs"], field=name, n_valid=n_valid,
                                     n_expected=n * size, coverage=n_valid / (n * size),
                                     valid_pentad_cells=accepted,
                                     missing_reason=reason if n_valid < n * size or accepted < size else ""))
            area_audit.extend(dict(t_abs=row["t_abs"], **item)
                              for item in assess_area(pentad, area, threshold))
            if row["is_pentad73"]:
                audit73.append(dict(year=row["year"], n_days=n, start_date=first, end_date=days[-1],
                                    valid_p_cells=int(np.isfinite(pentad["p_mm"]).sum()),
                                    status="passed"))
            progress.done += 1
            progress.event("pentad_written", f"{first}..{days[-1]}", t_abs=row["t_abs"],
                           written_days=written_days)

    m1.require(written_days == expected_days, "daily coverage mismatch")
    expected = {"time_pentad_calendar": (len(calendar), cal.schema),
                "grid_static_covariates": (size, static.schema),
                day_name: (size * expected_days, schemas[day_name]),
                pentad_name: (size * len(calendar), schemas[pentad_name])}
    for name, (count, schema) in expected.items():
        artifact = pq.ParquetFile(work / (name + ".parquet"))
        m1.require(artifact.metadata.num_rows == count and artifact.schema_arrow.equals(schema, check_metadata=True),
                   f"invalid published row count or schema: {name}")

    m1.write_csv(reports / "m2_area_conservation.csv",
                 ("t_abs", "field", "cells", "before_m3", "after_m3", "status"), area_audit)
    m1.write_csv(reports / "m2_coverage.csv",
                 ("t_abs", "field", "n_valid", "n_expected", "coverage", "valid_pentad_cells",
                  "missing_reason"), coverage)
    m1.write_csv(reports / "m2_pentad73_audit.csv",
                 ("year", "n_days", "start_date", "end_date", "valid_p_cells", "status"), audit73)
    m1.write_csv(reports / "sop_unit_test_result.csv", ("test", "status"), numeric)
    (reports / "m2_accumulation_tests.json").write_bytes(m1.canonical(dict(
        method="verified forecast step 24 at next UTC midnight", m1_cross_month=plan["evidence"],
        checks=[item for item in numeric if item["test"] in ("step_midnight", "increments", "interval_total")])))
    (reports / "m2_sign_and_balance_tests.json").write_bytes(m1.canonical(dict(
        sign_check="passed", observed_daily_min_max=ranges, total_runoff_and_balance="unavailable_missing_ssro",
        surface_runoff_usable_through=(sro_end.isoformat() if sro_end else None))))
    return {name: count for name, (count, _) in expected.items()}


def prepare(args):
    """只读解析冻结输入、运行范围和安全的尝试路径。"""
    m1.require(args.mode == "test" or not (args.start or args.end), "仅试跑模式可限定日期范围")
    m1.require(args.mode != "test" or args.m1, "试跑必须用 --m1 指定已发布的 M1 试跑完成清单")
    params, registry, m0 = m1.load_m0(args.config, args.mode)
    first, last = params["time"]["periods"]["event"]
    earliest, latest = date(first, 1, 1), date(last, 12, 31)
    m1_path = m1.safe(Path(os.path.abspath(m1.ROOT / args.m1))) if args.m1 else m1.safe(
        m1.ROOT / "data" / "frozen" / m0["m0_run_id"] / m1.STAGE / "m1_complete.json")
    if args.mode == "test":
        scope = m1.read_json(m1_path)["identity"]
        start = args.start or date.fromisoformat(scope["start"])
        end = args.end or date.fromisoformat(scope["end"])
    else:
        start, end = earliest, latest
    m1.require(earliest <= start <= end <= latest, "invalid dates or outside frozen event years")
    calendar = calendar_rows(start, end)
    read_start, read_end = calendar[0]["start_date"], calendar[-1]["end_date"]
    selected, mappings, scopes, m1_hash, evidence = load_sources(m1_path, args.mode, m0, read_start, read_end)
    code = code_identity(m0, args.mode)
    coords = grid(params)
    count = len(coords[2])
    time = params["time"]
    m1.require(time["pentads_per_year"] == 73 and time["flux_requires_complete_days"] and
               1 <= time["state_min_valid_days"] <= 5 and 0 <= time["state_max_consecutive_missing_days"] <= 5
               and 0 < params["spatial"]["valid_area_min"] <= 1,
               "unsupported frozen completeness rules")
    identity = dict(mode=args.mode, m0_run_id=m0["m0_run_id"], m0_sha256=m0["m0_sha256"],
                    m1_run_id=m1_path.parent.parent.name, m1_sha256=m1_hash,
                    source_inventory_hash=m1.object_hash({path.relative_to(m1.ROOT).as_posix(): expected
                                                         for path, expected in selected.values()}),
                    environment_hash=m0["environment_hash"], code=code, start=start.isoformat(),
                    end=end.isoformat(), read_start=read_start.isoformat(), read_end=read_end.isoformat())
    run = m0["m0_run_id"] if args.mode == "production" else (
        "test__" + m0["m0_run_id"] + "__m2_" + m1.object_hash(identity)[:12])
    attempt = uuid4().hex
    target = m1.safe(m1.ROOT / "data" / "frozen" / run / STAGE)
    work = m1.safe(m1.ROOT / "data" / "interim" / run / STAGE / attempt)
    reports = m1.safe(m1.ROOT / "outputs" / run / "reports" / STAGE / attempt)
    logs = m1.safe(m1.ROOT / "logs" / run / STAGE / attempt)
    free = shutil.disk_usage(m1.ROOT).free
    memory = m1.available_memory()
    # 预算覆盖读取缓冲、NetCDF 块缓存、解码数组和 Arrow 行组。
    memory_budget = 384 * 2**20 + 31 * 24 * count * 32
    disk_budget = (end - start).days + 1 + len(calendar)
    disk_budget = disk_budget * count * 192 + 256 * 2**20
    if not args.check_only and not (args.resume and target.exists()):
        m1.require(free >= disk_budget and memory_budget <= 0.6 * memory,
                   "磁盘空间或可用内存不足，无法启动一个月文件读取单元")
    if not args.dry_run and not args.check_only:
        m1.require(args.resume or not target.exists(), "已有发布结果，可用 --resume 校验后复用")
    plan = dict(run_id=run, attempt_id=attempt, identity=identity, start=start.isoformat(),
                end=end.isoformat(), read_start=read_start.isoformat(), read_end=read_end.isoformat(),
                calendar_pentads=len(calendar), raw_files=len(selected), grid_cells=count,
                target=str(target), work=str(work), reports=str(reports), logs=str(logs), workers=1,
                memory_budget_bytes=memory_budget, available_memory_bytes=memory, disk_budget_bytes=disk_budget,
                disk_free_bytes=free, partition_reserve_bytes=128 * 2**20)
    return (plan, params, registry, calendar, coords, selected, mappings, scopes, evidence,
            m1_path, start, end, target, work, reports, logs)


def verify_raw(selected, progress):
    """逐个计算计划输入的文件哈希，无需解码或写入数据。"""
    for path, expected in selected.values():
        progress.active(f"正在校验原始文件 {path.name}")
        m1.require(fingerprint(path) == expected, f"原始文件与 M1 登记的校验值不符：{path}")
        progress.done += 1


def publish(work, target, report_work, reports, plan, rows):
    """在阶段互斥锁保护下同卷移动已验收结果。"""
    data_hashes = {name: fingerprint(work / name) for name in sorted(ARTIFACTS)}
    report_hashes = {name: fingerprint(report_work / name) for name in sorted(REPORTS)}
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = m1.safe(target.with_suffix(".lock"))
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(plan["attempt_id"] + "\n")
    marker = work / "m2_complete.json"
    try:
        m1.require(not target.exists() and not reports.exists(), "M2 publication already exists")
        reports.parent.mkdir(parents=True, exist_ok=True)
        os.rename(report_work, reports)
        with marker.open("xb") as output:
            output.write(m1.canonical(dict(
                manifest_version=1, stage=STAGE, run_id=plan["run_id"], attempt_id=plan["attempt_id"],
                identity=plan["identity"], validation="passed", schema_version="v1.3.1",
                artifacts=data_hashes, reports=report_hashes, row_counts=rows)))
            output.flush()
            os.fsync(output.fileno())
        os.rename(work, target)
    finally:
        # 失败时移除尝试目录中的完成清单。
        marker.unlink(missing_ok=True)
        lock.unlink()


def reuse(target, plan):
    """复用前校验输入和源码身份，以及交接和报告完整性。"""
    manifest = m1.read_json(m1.safe(target / "m2_complete.json"))
    m1.require(manifest["manifest_version"] == 1 and manifest["stage"] == STAGE and
               manifest["validation"] == "passed" and manifest["schema_version"] == "v1.3.1"
               and manifest["run_id"] == plan["run_id"] and manifest["identity"] == plan["identity"],
               "M2 completion identity changed")
    member_hashes(target, manifest["artifacts"], ARTIFACTS)
    attempt = manifest["attempt_id"]
    m1.require(uuid4_format(attempt), "invalid M2 completion attempt ID")
    reports = m1.safe(Path(plan["reports"]).with_name(attempt))
    member_hashes(reports, manifest["reports"], REPORTS)
    return reports, manifest["row_counts"]


def uuid4_format(value):
    return isinstance(value, str) and len(value) == 32 and all(char in "0123456789abcdef" for char in value)


def main(argv=None):
    args = options(argv)
    try:
        (plan, params, registry, calendar, coords, selected, mappings, scopes, evidence,
         m1_path, start, end, target, work, reports, logs) = prepare(args)
        if not sys.stdout.isatty():
            print(m1.canonical(plan).decode().strip(), flush=True)
        elif args.dry_run:
            print(f"M2 · ERA5-Land 日候预处理 · 运行预览\n日表范围：{start} 至 {end}（含端点）")
            print(f"候表读取范围：{plan['read_start']} 至 {plan['read_end']}（完整候）")
            print(f"输入：{len(selected)} 份原始文件  |  {len(calendar)} 候  |  {plan['grid_cells']:,} 个格点")
            print(f"预计交接目录：{target}\n预计报告目录：{reports}\n预计日志：{logs / 'run.jsonl'}")
        if args.dry_run:
            return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, yaml.YAMLError) as error:
        print(f"M2 · 运行前检查失败：{error}\n可用 --help 查看中文说明。", file=sys.stderr)
        return 2

    reuse_existing = args.resume and target.exists()
    verify_only = args.check_only or reuse_existing
    progress = Progress(args, plan["run_id"], plan["attempt_id"], start, end,
                        len(selected) if verify_only else len(calendar), None if args.check_only else logs,
                        "份文件" if verify_only else "候")
    final = "failed"
    bytes_out = 0
    error_note, rows = None, None
    try:
        if not args.check_only:
            logs.mkdir(parents=True, exist_ok=False)
        progress.event("started", "开始 ERA5-Land 日候预处理", plan=plan,
                       surface_runoff_through=(scopes["surface_runoff"].isoformat() if scopes["surface_runoff"] else None))
        progress.live.start()
        if verify_only:
            verify_raw(selected, progress)
            if reuse_existing:
                progress.active("正在核对已发布交接及质量报告")
                reports, rows = reuse(target, plan)
            final = "reused" if reuse_existing else "checked"
            return 0

        work.mkdir(parents=True, exist_ok=False)
        report_work = m1.safe(work / "reports")
        report_work.mkdir()
        reader = MonthlyReader(selected, mappings, scopes,
                               calendar[0]["start_date"], calendar[-1]["end_date"],
                               *coords[:2], progress.active)
        plan["evidence"] = evidence
        rows = write_results(work, report_work, plan, registry, params, reader, (start, end),
                             calendar, coords, progress)
        reader.verify_unchanged()
        m1.require(fingerprint(m1_path)["sha256"] == plan["identity"]["m1_sha256"],
                   "M1 完成清单在计算期间发生变化")
        progress.active("正在验收并发布日候结果")
        bytes_out = sum((work / name).stat().st_size for name in ARTIFACTS)
        publish(work, target, report_work, reports, plan, rows)
        final = "published"
        return 0
    except KeyboardInterrupt:
        final = "interrupted"
        return 130
    except Exception as error:
        progress.event("exception", str(error), "ERROR", traceback=traceback.format_exc())
        error_note = str(error)
        return 1
    finally:
        progress.finish(final, work=str(work), target=str(target), reports=str(reports), logs=str(logs),
                        output_bytes=bytes_out,
                        output_mib_per_s=bytes_out / 2**20 / max(monotonic() - progress.started, 1e-9))
        labels = {"published": "完成，已发布日候结果", "reused": "完成，已复用日候结果",
                  "checked": "完成，原始文件校验通过", "failed": "执行失败", "interrupted": "已中断"}
        print(f"{progress.title} · {labels[final]}\n已处理 {progress.done}/{progress.total} {progress.unit}"
              f"  |  用时 {monotonic()-progress.started:.1f} 秒", file=sys.stderr)
        if error_note:
            print(f"原因：{error_note}", file=sys.stderr)
        if final in ("published", "reused"):
            print(f"完成清单：{target / 'm2_complete.json'}\n"
                  f"交接结果：{len(rows)} 张表，共 {sum(rows.values()):,} 行\n质量报告：{reports}", file=sys.stderr)
        elif work.is_dir():
            print(f"本次尝试保留在：{work}", file=sys.stderr)
        logfile = logs / "run.jsonl"
        print(f"运行日志：{logfile}" if logfile.is_file() else "运行日志：本次未写入日志文件", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())


