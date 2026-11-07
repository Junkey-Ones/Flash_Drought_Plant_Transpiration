"""M10 主表复算、样本流、偏离与可复现性审计，见工程规格第 7.12 节。

读取 M3–M9 冻结交接。输出逐事件复核、总数和样本流。正式复核另需
研究组独立签署。最小示例：python -B workflow/downstream.py m10 --help
退出码：0 成功，1 执行失败，2 预检失败，130 中断。
"""
from collections import Counter, defaultdict
import csv
from itertools import islice
import csv
from math import isclose
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from fdwt.stages.m03_support_climatology.climatology import series
from fdwt.stages.m04_event_catalog.events import EVENTS
from fdwt.stages.m05_recovery_precip.recovery import MASTER
from fdwt.stages.m06_exposure.exposure import EXPOSURE
from fdwt.stages.m08_route2.route2 import EXIT
from preprocess import m1


def preflight(ctx):
    """确认事件键及可复核的冻结偏离记录已登记。"""
    ctx.deviations = m1.read_json(ctx.config)["deviations"]
    n = ctx.bundles[5]["manifest"]["row_counts"][MASTER]
    if 128 * 2**20 + n * 9000 > .6 * ctx.plan["memory_bytes"]:
        raise ValueError("事件复核的内存预算超限")


def review_sample(events, seed, years, minimum=30):
    """固定种子按时期、恢复和窗口状态分层，以轮转方式取得代表样本。"""
    if not events:
        return []
    rng = np.random.default_rng(seed)
    groups = defaultdict(list)
    for e in events:
        period = min(2, 3*(e["year"]-years[0])//(years[1]-years[0]+1))
        groups[e["product"], period, e["right_censored"], e["F_R_status"]].append(e)
    for rows in groups.values():
        rng.shuffle(rows)
    keys = sorted(groups)
    ordered = (groups[key][i] for i in range(max(map(len, groups.values())))
               for key in keys if i < len(groups[key]))
    return list(islice(ordered, max(minimum, len(keys))))


def recalc(e, by_t, params):
    """按原始候顺序独立重算，不调用 M4 或 M5 的识别与分类函数。"""
    ts = e["t_s"]
    high = params["drought"]["smp_above"]
    threshold = params["recovery"]["terminate_smp"]
    segment = []
    j = ts
    while j in by_t:
        x = by_t[j]["smp"]
        if x is None or x >= high:
            break
        segment.append((x, j))
        j += 1
    if not segment:
        raise ValueError(f"复核事件缺少起点：{e['episode_id']}")
    smp_min, tm = min(segment)
    anchor, rate = ts-1, None
    while anchor in by_t and by_t[anchor]["smp"] is not None:
        if by_t[anchor]["smp"] > high:
            rate = (by_t[anchor]["smp"]-smp_min)/(tm-anchor)
            break
        anchor -= 1
    t = tm+1
    while t in by_t and by_t[t]["smp"] is not None and by_t[t]["smp"] <= threshold:
        t += 1
    te = t if t in by_t and by_t[t]["smp"] is not None else None
    checks = dict(t_min=tm == e["t_min"], t_e=te == e["t_e"],
                  minimum=isclose(smp_min, e["smp_min"]), rate=rate is not None and isclose(rate, e["development_rate"]))
    if te is None:
        checks.update(censor=e["right_censored"] and e["censor_t_abs"] == t-1)
    else:
        checks.update(RT=te-tm == e["RT"], delta=isclose(by_t[te]["smp"]-smp_min, e["delta_SMP"]),
                      RR=isclose((by_t[te]["smp"]-smp_min)/(te-tm), e["RR"]))
        for tag, limits, minimum in (("P_term", (tm, te+2), params["pterm"]["min_p_mm"]),
                                     ("F_R", (te-1, te+1), params["fr"]["min_p_mm"])):
            rows = [by_t.get(k) for k in range(limits[0], limits[1]+1)]
            first = None
            result = None
            if all(r and r["p_mm"] is not None and r["p95_mm"] is not None and r["p95_usable"] for r in rows):
                first = next((r["t_abs"] for r in rows if r["p_mm"] > minimum and r["p_mm"] >= r["p95_mm"]), None)
                result = first is not None
            field = "t_P95" if tag == "P_term" else "t_w_R"
            checks[tag] = result == e[tag] and first == e[field]
    return dict(episode_id=e["episode_id"], grid_id=e["grid_id"], t_s=ts, t_min_recalc=tm, t_e_recalc=te,
                rate_recalc=rate, passed=all(checks.values()), failed_checks=[k for k, ok in checks.items() if not ok])


def totals(ctx):
    """比较每个已声明 Parquet 行数和物理元数据，并复验报告校验值。"""
    result = []
    for module, bundle in sorted(ctx.bundles.items()):
        manifest = bundle["manifest"]
        contracts = manifest["table_contracts"]
        if set(contracts) != set(manifest["row_counts"]):
            raise ValueError(f"M{module} 表契约清单与行数清单不符")
        for name, count in sorted(manifest["row_counts"].items()):
            reader = pq.ParquetFile(bundle["files"][name])
            actual = reader.metadata.num_rows
            if actual != count:
                raise ValueError(f"M{module} {name} 主表总数不一致")
            contract = contracts[name]
            if contract == "event_master_v1_3_1":
                fields = ("LAI_base", "LAI_trend", "LAI_exposure_cluster", "NDVI_base",
                          "NDVI_trend", "deep_SM_pre4", "shallow_SM_pre4")
                ctx.derived_contract(contract, "event_master", "event_exposure", fields)
            expected = ctx.schema(contract)
            if not reader.schema_arrow.equals(expected, check_metadata=False):
                raise ValueError(f"M{module} {name} 表契约类型不一致")
            result.append(dict(module=f"M{module}", table=name, manifest_rows=count, verified_rows=actual))
        for path, expected in manifest["reports"].items():
            source = m1.safe(m1.ROOT / path)
            run = manifest["run_id"]
            if (not source.is_relative_to(m1.ROOT / "outputs" / run) or
                    fingerprint_report(source) != expected):
                raise ValueError(f"M{module} 报告校验失败：{path}")
    return result


def fingerprint_report(path):
    """使用与各阶段发布时相同的文件校验算法。"""
    from preprocess import fingerprint
    return fingerprint(path)


def sample_flow(events, exposures):
    """分开计算各估计对象的样本流。未知或不适用均保留计数。"""
    n = len(events)
    a = [e for e in events if e["route1_A_eligible"]]
    fr = [e for e in a if e["F_R"] is not None]
    h1 = [e for e in fr if e["F_R_subtype"] != "post"]
    return [
        ("Q1", "全事件至条件 A", n, len(a)),
        ("Q1_risk", "起始和最低候在活跃窗", n, sum(e["recovery_risk_eligible"] for e in events)),
        ("Q2", "条件 A 至 F_R 状态已知", len(a), len(fr)),
        ("H1", "排除仅在终止后接续的事件", len(fr), len(h1)),
        ("Q3_support", "具备五年有效 LAI 暴露", n, sum(e["LAI_full_window"] for e in exposures)),
    ]


def published_report(ctx, stage, name):
    """从上游清单定位已验证报告，拒绝重名和未登记文件。"""
    paths = [m1.ROOT / path for path in ctx.bundles[stage]["manifest"]["reports"]
             if path.rsplit("/", 1)[-1] == name]
    if len(paths) != 1:
        raise ValueError(f"M{stage} 上游报告缺失或重名：{name}")
    return m1.safe(paths[0])


def verify_results(ctx, events, exits):
    """用事件级冻结数据复核 Q2 分母、M8 出口总数和规范矩阵总数。"""
    with published_report(ctx, 7, "q2_fr_conditional_proportion.csv").open(encoding="utf-8", newline="") as file:
        observed = next(csv.DictReader(file))
    a = [e for e in events if e["route1_A_eligible"]]
    positive = [e for e in a if e["F_R"] is True]
    counts = dict(n_A=len(a), n_known=sum(e["F_R"] is not None for e in a), n_FR=len(positive))
    counts["n_unknown"] = counts["n_A"]-counts["n_known"]
    counts.update({label: sum(e["F_R_subtype"] == label for e in positive) for label in ("pre", "sync", "post")})
    if any(int(observed[key]) != expected for key, expected in counts.items()):
        raise ValueError("M7 Q2 条件比例分母、分子或子型总数不一致")
    gate = m1.read_json(published_report(ctx, 8, "route2_gate.json"))
    out = Counter(e["exit_type"] or ("unknown" if e["exit_type_unknown"] else "censored") for e in exits)
    if gate["n_event"] != len(exits) or gate["counts"] != dict(out) or gate["model_allowed"]:
        raise ValueError("M8 出口报告总数或模型放行状态不一致")
    matrix = m1.parse((ctx.config.parent / "specification_matrix.yaml").read_bytes())
    with published_report(ctx, 9, "spec_result.csv").open(encoding="utf-8", newline="") as file:
        specifications = {(r["estimand"], r["spec_id"]) for r in csv.DictReader(file)}
    expected = {(e, f"{e}__{i:03d}") for e, section in matrix["estimands"].items() for i in range(section["count"])}
    if specifications != expected:
        raise ValueError("M9 规范编号与冻结规范矩阵不一致")
    return counts, dict(out), len(specifications)


def run(ctx):
    """先写复核证据，有不一致即阻止 M10 发布。"""
    events = ctx.rows(5, MASTER)
    exposures = ctx.rows(6, EXPOSURE)
    baseline = ctx.rows(4, EVENTS, ["episode_id", "t_s", "t_min", "t_e"])
    if [(e["episode_id"], e["t_s"], e["t_min"], e["t_e"]) for e in events] != [
            (e["episode_id"], e["t_s"], e["t_min"], e["t_e"]) for e in baseline]:
        raise ValueError("M4/M5 事件 ID 或骨架发生改变")
    if [e["episode_id"] for e in events] != [r["episode_id"] for r in exposures]:
        raise ValueError("M6 暴露连接丢失或重复 M5 事件")
    if any(r["window_end_year"] >= r["year"] for r in exposures):
        raise ValueError("暴露窗含事件年或未来年")
    if any((e["Lag_FR"] is not None) != (e["F_R"] is True) or
           (e["Lag_P"] is not None) != (e["P_term"] is True) for e in events):
        raise ValueError("Lag 超出定义域")
    h = ctx.params["route2"]["h"]
    exits = [r for r in ctx.rows(8, EXIT) if r["H"] == h]
    if [e["episode_id"] for e in events] != [r["episode_id"] for r in exits]:
        raise ValueError("M8 主队列丢失或重复 M5 事件")
    if any(r["exit_type"] == "A" and e["F_R"] is not False or
           r["exit_type"] == "B" and e["F_R"] is not True for e,r in zip(events, exits)):
        raise ValueError("M8 出口和 F_R 状态不一致")
    chosen = defaultdict(list)
    for e in review_sample(events, ctx.params["random"]["seed"], ctx.params["time"]["periods"]["event"]):
        chosen[e["grid_id"]].append(e)
    audited = []
    if chosen:
        for grid, history in series(ctx):
            if grid in chosen:
                ctx.progress.active(f"正在独立复算格点 {grid} 的抽样事件")
                by_t = {r["t_abs"]: r for r in history}
                audited.extend(recalc(e, by_t, ctx.params) for e in chosen.pop(grid))
            if not chosen:
                break
    ctx.csv("reports", "review_event_recalc.csv",
            ("episode_id", "grid_id", "t_s", "t_min_recalc", "t_e_recalc", "rate_recalc", "passed", "failed_checks"), audited)
    ctx.progress.active("正在复核表契约、分母与规范数量")
    verified = totals(ctx)
    q2, route2, n_specs = verify_results(ctx, events, exits)
    ctx.csv("reports", "review_table_totals.csv", ("module", "table", "manifest_rows", "verified_rows"), verified)
    fields = ("analysis_id", "step_name", "n_before", "n_after", "n_excluded")
    ctx.csv("tables", "sample_flow.csv", fields,
            [dict(zip(fields, (a, name, before, after, before-after)))
             for a,name,before,after in sample_flow(events, exposures)])
    missing = Counter((e["censor_type"], e["F_R_status"], e["P_term_status"]) for e in events)
    ctx.csv("tables", "missing_exclusion_unknown_summary.csv", ("censor_type", "fr_status", "pterm_status", "n"),
            [dict(censor_type=c, fr_status=f, pterm_status=p, n=n) for (c,f,p), n in sorted(missing.items())])
    ctx.report("reports", "review_deviation_audit.json", ctx.deviations)
    ctx.report("reports", "review_schema_validation.json", dict(
        status="passed", tables=len(verified), q2_totals=q2, route2_totals=route2,
        specification_count=n_specs))
    ctx.report("reports", "result_manifest.json", dict(
        run_id=ctx.plan["run_id"], upstream=ctx.plan["identity"]["lineage"],
        input_report_paths={f"M{number}": sorted(bundle["manifest"]["reports"])
                            for number, bundle in sorted(ctx.bundles.items())},
        independent_signature="pending"))
    ctx.report("reports", "final_reproducibility_report.json", dict(
        n_events=len(events), n_events_recalculated=len(audited), n_tables=len(verified),
        independent_reviewer="pending", production_scope=ctx.args.mode,
        ready_for_publication=False))
    if chosen or any(not row["passed"] for row in audited):
        raise ValueError("独立复算发现主表不一致，应退回责任阶段")
