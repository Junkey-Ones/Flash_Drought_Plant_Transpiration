"""M5 恢复轨迹、降水窗口及依赖块，见技术手册第 7 节。

读取 M3 候表及 M4 事件骨架，向 M6–M10 提供事件主表、降水候、轨迹及分支。
窗口完整性依据冻结定义。最小示例：python -B workflow/downstream.py m5 --help
退出码：0 成功，1 执行失败，2 预检失败，130 中断。
"""
from collections import Counter, defaultdict
from contextlib import ExitStack
from itertools import combinations

import numpy as np

from fdwt.stages.m03_support_climatology.climatology import series
from fdwt.stages.m04_event_catalog.events import Components, EVENTS

MASTER = "event_master.parquet"


def preflight(ctx):
    """预检冻结窗口语法和事件索引内存预算。"""
    window(ctx.params)
    memory = 128 * 2**20 + ctx.bundles[4]["manifest"]["row_counts"][EVENTS] * 6500
    ctx.plan["memory_budget_bytes"] = memory
    if memory > 0.6 * ctx.plan["memory_bytes"]:
        raise ValueError("内存不足，无法建立 M5 事件和空间块索引")


def window(params):
    """返回冻结的 P-term 末端偏移和 F_R 对称窗口半宽。"""
    pterm = {"[t_min,t_e+2]": 2, "[t_min,t_e+1]": 1}
    fr = {"[t_e-1,t_e+1]": 1, "[t_e-2,t_e+2]": 2}
    return pterm[params["pterm"]["window"]], fr[params["fr"]["window"]]


def sen(x, y):
    """对唯一有序时间和有限观测值计算两两斜率的中位数。"""
    slopes = [(y[j] - y[i]) / (x[j] - x[i]) for i, j in combinations(range(len(x)), 2)]
    return float(np.median(slopes)) if slopes else None


def qualifies(row, minimum, threshold="p95_mm"):
    """阈值可用、P 达到阈值且严格超过最低毫米数时为阳性。"""
    if row is None or row["p_mm"] is None or not row["p95_usable"] or row.get(threshold) is None:
        return None
    return bool(row["p_mm"] >= row[threshold] and row["p_mm"] > minimum)


def wet_runs(history, minimum, allow_gap=False, threshold="p95_mm"):
    """按格点及首个湿候生成湿过程 ID。未知候截断湿过程。"""
    out = {}
    last = None
    process = None
    gap = 0
    for row in history:
        q = qualifies(row, minimum, threshold)
        if q is None:
            last = None
            continue
        if q:
            t = row["t_abs"]
            if last is None or t-last > 1+int(allow_gap) or gap > int(allow_gap):
                process = f"wet__{row['product_combo']}__{row['grid_id']}__{t}"
            out[t] = process
            last = t
            gap = 0
        else:
            gap += 1
    return out


def classify(by_t, first, last, minimum, threshold="p95_mm"):
    """返回三值结局、状态及观测到的全部合格候。

    不完整窗口的结局为未知。可见的合格候仅供审计，不能据此计算首候滞后。
    """
    rows = [by_t.get(t) for t in range(first, last+1)]
    hits = [row["t_abs"] for row in rows if qualifies(row, minimum, threshold) is True]
    if any(row is None or row["p_mm"] is None for row in rows):
        return None, "window_incomplete", hits
    if any(qualifies(row, minimum, threshold) is None for row in rows):
        return None, "r_unusable", hits
    return bool(hits), "positive" if hits else "negative", hits


def evaluate(event, by_t, params, wet, threshold="p95_mm", fr_width=None):
    """计算单个事件的恢复指标和分支锚点，保留原入组标记。"""
    p_after, width = window(params)
    width = width if fr_width is None else fr_width
    e = event.copy()
    te, tm = e["t_e"], e["t_min"]
    if te is None:
        e.update(P_term_status="not_applicable", F_R_status="not_applicable", F_Z_status="missing_source")
        return e, [], None
    rt = te - tm
    values = [by_t[t]["smp"] for t in range(tm, te+1)]
    if rt <= 0 or any(x is None for x in values):
        raise ValueError(f"事件的恢复候序列有缺口或时长无效：{e['episode_id']}")
    delta = values[-1] - values[0]
    early = params["recovery"]["early_pentads"]
    early_val = by_t.get(tm+early, {}).get("smp") if rt >= early else None
    p, pstat, phit = classify(by_t, tm, te+p_after, params["pterm"]["min_p_mm"], threshold)
    fr, fstat, fhit = classify(by_t, te-width, te+width, params["fr"]["min_p_mm"], threshold)
    ptime, fw = phit[0] if p else None, fhit[0] if fr else None
    subtype = None if fw is None else "pre" if fw < te else "post" if fw > te else "sync"
    if width == 1 and fr and p is False:
        raise ValueError("完整 P-term 窗口判为阴性时，主 F_R 窗口不应判为阳性")
    e.update(RT=rt, delta_SMP=delta, RR=delta/rt, recovery_slope=sen(range(len(values)), values),
             early_recovery_rate=(early_val-values[0])/early if early_val is not None else None,
             P_term=p, P_term_status=pstat, t_P95=ptime, Lag_P=ptime-tm if p else None,
             F_R=fr, F_R_status=fstat, t_w_R=fw, F_R_subtype=subtype,
             Lag_FR=fw-tm if fr else None, Offset_FR=fw-te if fr else None,
             F_Z=None, F_Z_status="missing_source", wet_process_id=wet.get(fw))
    rows = []
    for t in sorted(set(phit) | set(fhit)):
        r = by_t[t]
        rows.append(dict(episode_id=e["episode_id"], t_abs=t, year=r["year"], pentad=r["pentad"],
                         p_mm=r["p_mm"], p95_mm=r["p95_mm"], p99_mm=r.get("p99_mm"),
                         p_mm_per_day=r["p_mm_per_day"], p_5day_equivalent_mm=r["p_5day_equivalent_mm"],
                         p95_qualify=qualifies(r, params["pterm"]["min_p_mm"]),
                         p99_qualify=qualifies(r, params["pterm"]["min_p_mm"], "p99_mm"),
                         in_pterm_window=tm <= t <= te+p_after, in_fr_window=te-width <= t <= te+width,
                         is_first_pterm=t == ptime, is_first_fr=t == fw, pentad73_flag=r["pentad"] == 73,
                         precip_qc_status="complete", p_term_definition=f"{threshold}__plus{p_after}"))
    rain = [by_t.get(t, {}).get("p_mm") for t in range(tm, te+p_after+1)]
    return e, rows, None if any(v is None for v in rain) else (sum(rain), max(rain))


def trajectories(e, by_t, params):
    """输出恢复候和计划内的终止后候，标记资料缺失。"""
    te, tm = e["t_e"], e["t_min"]
    end = e["censor_t_abs"] if te is None else te + max(params["recovery"]["post_te_trajectory"])
    for t in range(tm, end+1):
        row = by_t.get(t, {})
        yield dict(episode_id=e["episode_id"], t_abs=t, relative_to_tmin=t-tm,
                   relative_to_te=t-te if te is not None else None, smp=row.get("smp"),
                   recovery_phase_flag=te is None or t <= te, early_recovery_flag=0 < t-tm <= params["recovery"]["early_pentads"],
                   post_te_flag=te is not None and t > te, active_flag=row.get("active_flag"),
                   qc_status=row.get("qc_status", "outside_support"))


def superblocks(events, geometry):
    """将共享区域事件或湿过程的 100 km 空间块合并。"""
    graph, first = Components(), {}
    for cell in geometry.values():
        graph.root(cell["block_id"])
    for e in events:
        block = geometry[e["grid_id"]]["block_id"]
        for key in (e["cluster_id"], e["wet_process_id"]):
            if key:
                graph.union(block, first.setdefault(key, block))
    return {block: graph.root(block) for block in sorted(graph.parents)}


def run(ctx):
    """发布完整事件结局，并区分未知与分支不可用。"""
    geometry = {r["grid_id"]: r for r in ctx.rows(3, "grid_manifest.parquet")}
    events = defaultdict(list)
    for e in ctx.rows(4, EVENTS):
        events[e["grid_id"]].append(e)
    summaries = Counter()
    evaluated, rainfall, wet_members = [], [], []
    clim = {}
    # 降水候表保留 P99，主口径使用 P95。
    import pyarrow.parquet as pq
    from fdwt.stages.m03_support_climatology.climatology import CLIM
    batches = pq.ParquetFile(ctx.path(3, CLIM)).iter_batches(columns=["grid_id", "pentad", "p99_mm"])
    for batch in batches:
        for row in batch.to_pylist():
            clim[row["grid_id"], row["pentad"]] = row["p99_mm"]
    definitions = ((MASTER, "event_master"), ("event_precip.parquet", "event_precip"),
                   ("event_branch.parquet", "event_branch"),
                   ("event_recovery_trajectory.parquet", "event_recovery_trajectory"))
    with ExitStack() as stack:
        wm, wp, wb, wt = [stack.enter_context(ctx.writer(name, key)) for name, key in definitions]
        for index, (grid, history) in enumerate(series(ctx)):
            if grid not in events:
                continue
            ctx.progress.active(f"正在匹配格点 {grid} 的恢复与降水窗口", index, len(geometry), "个格点")
            for row in history:
                row["p99_mm"] = clim[grid, row["pentad"]]
            by_t = {r["t_abs"]: r for r in history}
            wet = wet_runs(history, ctx.params["fr"]["min_p_mm"], ctx.params["wet_process"]["allow_one_gap"])
            used = set()
            for original in events.pop(grid):
                e, hits, rain = evaluate(original, by_t, ctx.params, wet)
                evaluated.append(e)
                wm([e])
                wp(hits)
                wb(dict(episode_id=e["episode_id"], branch=branch, branch_definition=definition,
                        t_w=e.get("t_w_R") if branch == "F_R" else None,
                        wet_process_id=e.get("wet_process_id") if branch == "F_R" else None,
                        window_status=e[branch+"_status"],
                        branch_available=branch == "F_R" and e.get("F_R") is not None,
                        missing_reason=None if branch == "F_R" and e.get("F_R") is not None else e[branch+"_status"])
                   for branch, definition in (("F_R", ctx.params["fr"]["window"]), ("F_Z", ctx.params["fz"]["window"])))
                wt(trajectories(e, by_t, ctx.params))
                if e.get("wet_process_id"):
                    used.add(e["wet_process_id"])
                summaries[e["year"], e["P_term_status"], e["F_R_status"]] += 1
                if e.get("P_term") is False:
                    rainfall.append(dict(episode_id=e["episode_id"], total_mm=rain[0], max_mm=rain[1]))
            wet_members.extend(dict(wet_process_id=process, grid_id=grid, t_abs=t)
                               for t, process in wet.items() if process in used)
    if events:
        raise ValueError("M4 的部分事件格点未出现在 M3 候表中")
    ctx.progress.active("正在合并区域事件与湿过程空间块")
    memberships = superblocks(evaluated, geometry)
    ctx.csv("reports", "m5_window_completeness.csv", ("year", "p_status", "fr_status", "n"),
            [dict(year=y, p_status=p, fr_status=f, n=n) for (y,p,f), n in sorted(summaries.items())])
    ctx.csv("tables", "non_pterm_window_precip.csv", ("episode_id", "total_mm", "max_mm"), rainfall)
    ctx.csv("tables", "wet_process_membership.csv", ("wet_process_id", "grid_id", "t_abs"), wet_members)
    ctx.csv("tables", "superblock_membership.csv", ("block_id", "superblock_id"),
            [dict(block_id=key, superblock_id=value) for key, value in memberships.items()])
    ctx.report("reports", "m5_branch_overlap.json", dict(
        fr_positive=sum(e.get("F_R") is True for e in evaluated), fz="missing_source"))
