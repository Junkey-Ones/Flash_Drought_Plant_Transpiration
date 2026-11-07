"""M9 已放行的规范敏感性复算和未放行分支审计，见技术手册第 11 节。

读取 M0 规范矩阵、M3 阈值和 M4/M5/M7/M8 主链。按定义族单独替换，
输出逐规范指标及适用状态供 M10。最小示例：python -B workflow/downstream.py m9 --help
退出码：0 成功，1 执行失败，2 预检失败，130 中断。
"""
from collections import defaultdict
import json

import numpy as np
import pyarrow.parquet as pq

from fdwt.stages.m03_support_climatology.climatology import CLIM, series
from fdwt.stages.m04_event_catalog.events import identify
from fdwt.stages.m05_recovery_precip.recovery import MASTER, evaluate, wet_runs
from fdwt.stages.m07_route1.route1 import annual_rows, risk_rows
from fdwt.stages.m08_route2.route2 import EXIT
from preprocess import m1


def preflight(ctx):
    """核对冻结矩阵及每个备选参数的数量。"""
    path = ctx.config.parent / "specification_matrix.yaml"
    matrix = m1.parse(path.read_bytes())
    if matrix["status"] != "frozen" or matrix["strategy"] != "one_family_at_a_time":
        raise ValueError("规范矩阵没有冻结或替换策略不匹配")
    ctx.matrix = matrix
    ctx.specifications = list(specifications(matrix, ctx.params))
    n = ctx.bundles[5]["manifest"]["row_counts"][MASTER]
    if 200 * 2**20 + n * 17000 > .6 * ctx.plan["memory_bytes"]:
        raise ValueError("规范复算的事件索引超出内存预算")


def specifications(matrix, params):
    """保留所有冻结规范及其适用状态，禁止执行时临时添减。"""
    for estimand, section in matrix["estimands"].items():
        records = [("baseline", None, section.get("baseline_available", True))]
        for family, spec in section["alternatives"].items():
            values = params
            for key in spec["parameter_ref"].split("."):
                values = values[key]
            if len(values) != spec["count"]:
                raise ValueError(f"规范数量不符：{estimand}/{family}")
            unavailable = section.get("unavailable_alternatives", {}).get(family, [])
            records.extend((family, value, value not in unavailable and section.get("baseline_available", True))
                           for value in values)
        if len(records) != section["count"]:
            raise ValueError(f"规范总数不符：{estimand}")
        for i, (family, value, applicable) in enumerate(records):
            yield dict(spec_id=f"{estimand}__{i:03d}", estimand=estimand, family=family,
                       parameter=json.dumps(value, ensure_ascii=False), applicable=applicable)


def event_metrics(events, estimand, params, min_years=None):
    """比较同一定义的描述指标，支持不足时保留空值。"""
    a = [e for e in events if e["route1_A_eligible"]]
    if estimand == "Q1_trend":
        minimum = min_years or params["trend"]["min_valid_years"]
        return [(row["metric"]+"_sen_slope", row["slope_per_year"], row["n_years"])
                for row in annual_rows(events, params["trend"]["year_min_events"], minimum)]
    if estimand == "Q1_recovery":
        return [(field+"_median", float(np.median([e[field] for e in a])) if a else None, len(a))
                for field in ("RT", "delta_SMP", "RR")]
    known = [e for e in a if e["F_R"] is not None]
    out = [("FR_conditional", sum(e["F_R"] for e in known)/len(known) if known else None, len(known))]
    for field in ("delta_SMP", "RR"):
        x = [e[field] for e in known if e["F_R_subtype"] in ("pre", "sync")]
        y = [e[field] for e in known if e["F_R"] is False]
        out.append((field+"_unadjusted_median_difference",
                    float(np.median(x)-np.median(y)) if x and y else None, len(x)+len(y)))
    return out


def rerun(ctx):
    """读取每个格点的完整候历史，对可用事件定义和降水口径逐一复算。"""
    p = ctx.params
    baseline = defaultdict(list)
    for e in ctx.rows(5, MASTER):
        baseline[e["grid_id"]].append(e)
    clim = defaultdict(dict)
    columns = ["grid_id", "pentad", "p99_mm", "wet_p95_mm"]
    for batch in pq.ParquetFile(ctx.path(3, CLIM)).iter_batches(columns=columns):
        for r in batch.to_pylist():
            clim[r["grid_id"]][r["pentad"]] = (r["p99_mm"], r["wet_p95_mm"])
    out = defaultdict(list)
    horizons = p["spec_curve"]["candidate_alternatives"]["Q1_recovery"]["followup"]
    counts = {h: np.zeros((2, h), dtype=np.int64) for h in horizons}
    cells = ctx.bundles[3]["manifest"]["row_counts"]["grid_manifest.parquet"]
    for index, (grid, history) in enumerate(series(ctx)):
        ctx.progress.active(f"正在逐项复算格点 {grid} 的定义敏感性", index, cells, "个格点")
        for r in history:
            r["p99_mm"], r["wet_p95_mm"] = clim[grid][r["pentad"]]
        by_t = {r["t_abs"]: r for r in history}
        for event in baseline[grid]:
            for h, count in counts.items():
                for row in risk_rows(event, by_t, h):
                    if row["at_risk"]:
                        k = row["followup_pentad"]-1
                        count[0, k] += 1
                        count[1, k] += row["recovery_event"]
        for family, v, episodes, threshold, width in (
            ("termination", p["recovery"]["terminate_smp_sensitivity"],
             identify(history, p, termination=p["recovery"]["terminate_smp_sensitivity"])[0], "p95_mm", None),
            ("duration", "development", identify(history, p, development=True)[0], "p95_mm", None),
            ("fr_window", 2, baseline[grid], "p95_mm", 2),
            ("precip_threshold", "P99", baseline[grid], "p99_mm", None),
            ("precip_threshold", "wet_P95", baseline[grid], "wet_p95_mm", None),
        ):
            out[family, v]
            wet = wet_runs(history, p["fr"]["min_p_mm"], threshold=threshold)
            for e in episodes:
                r, _, _ = evaluate(e, by_t, p, wet, threshold, width)
                out[family, v].append(r)
        out["baseline", None].extend(baseline.pop(grid, []))
    if baseline:
        raise ValueError("M5 事件格点不在 M3 候表中")
    return out, counts


def run(ctx):
    """输出已计算指标、适用但未完成的规范和产品缺项。"""
    p = ctx.params
    results, recovery_counts = rerun(ctx)
    ctx.progress.active("正在汇总规范结果与适用状态")
    exits = ctx.rows(8, EXIT)
    records = []
    for spec in ctx.specifications:
        family, value = spec["family"], json.loads(spec["parameter"])
        e = spec["estimand"]
        stats = []
        status = "not_applicable" if not spec["applicable"] else "pending_implementation"
        if spec["applicable"] and e in ("Q1_trend", "Q1_recovery", "Q2_FR"):
            key = ("baseline", None) if family == "trend_years" else (family, value)
            if key in results:
                stats = event_metrics(results[key], e, p, value if family == "trend_years" else None)
            if e == "Q1_recovery" and family == "followup":
                risk, recovered = recovery_counts[value]
                hazard = np.divide(recovered, risk, out=np.zeros(value), where=risk > 0)
                identified = bool(risk[-1] > 0 or np.any((risk > 0) & (risk == recovered)))
                stats.append(("observed_recovery_by_H", float(1-np.prod(1-hazard)) if identified else None,
                              int(risk[0])))
        if spec["applicable"] and e == "route2" and (family in ("baseline", "followup") or value == "strict_post"):
            h = value if family == "followup" else p["route2"]["h"]
            group = [r for r in exits if r["H"] == h and r["followup_complete"]]
            for label in ("A", "B", "C"):
                n = sum(("A" if value == "strict_post" and r["strict_reclass_from_B"] else r["exit_type"]) == label
                        for r in group)
                stats.append(("conditional_exit_"+label, n/len(group) if group else None, len(group)))
        if stats:
            records.extend(dict(**spec, metric=metric, estimate=estimate, n=n,
                                status="estimated" if estimate is not None else "insufficient_support")
                           for metric, estimate, n in stats)
        else:
            records.append(dict(**spec, metric=None, estimate=None, n=None, status=status))
    ctx.csv("tables", "spec_result.csv",
            ("spec_id", "estimand", "family", "parameter", "applicable", "metric", "estimate", "n", "status"), records)
    ctx.report("reports", "m9_robustness_report.json", dict(
        registered_products=p["protocol"]["analysis_release"]["M9_registered_product_suite"],
        existing_product_common_support="pending_implementation",
        station_validation="missing_source", circulation="missing_source",
        direction_classification="pending_complete_effects",
        n_specs=len(ctx.specifications), n_estimated_rows=sum(r["status"] == "estimated" for r in records),
        n_pending_rows=sum(r["status"] == "pending_implementation" for r in records)))
