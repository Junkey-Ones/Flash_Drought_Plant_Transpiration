"""M7 路线一风险集、条件描述和趋势，见预注册第 7 节。

读取 M3 候表、M5 事件及可选 M6 暴露。向 M9、M10 提供风险集和结果表。
随访长度采用 M0 冻结值。最小示例：python -B workflow/downstream.py m7 --help
退出码：0 成功，1 执行失败，2 预检失败，130 中断。
"""
from collections import Counter, defaultdict
from itertools import groupby
from math import erfc, sqrt
from statistics import NormalDist

import numpy as np

from fdwt.stages.m03_support_climatology.climatology import series
from fdwt.stages.m05_recovery_precip.recovery import MASTER, sen, superblocks

RISK = "route1_recovery_person_pentad.parquet"


def preflight(ctx):
    """为事件级和统计计算数组预估单工作单元内存。"""
    n = ctx.bundles[5]["manifest"]["row_counts"][MASTER]
    memory = 128 * 2**20 + n * 5500
    ctx.plan["memory_budget_bytes"] = memory
    if memory > .6 * ctx.plan["memory_bytes"]:
        raise ValueError("内存不足，无法建立 M7 事件分析所需数组")


def risk_rows(e, by_t, horizon):
    """从最低点下一候开始随访，停在首次恢复、最后连续有效候或 H。"""
    if not e["recovery_risk_eligible"]:
        return []
    t0 = e["t_min"]
    end = max(by_t)
    data = []
    for k in range(1, horizon+1):
        t = t0+k
        row = by_t.get(t)
        if row is None or row["smp"] is None:
            reason = "data_end" if t > end else "missing_smp"
            if not data:
                data.append(risk_row(e, 0, by_t[t0], False, False))
            data[-1].update(censor_administrative=reason == "data_end", censor_missing=reason == "missing_smp",
                            censor_data_end=reason == "data_end", censor_reason=reason)
            break
        recovery = t == e["t_e"]
        data.append(risk_row(e, k, row, True, recovery))
        if recovery:
            break
        if k == horizon:
            data[-1].update(censor_administrative=True, censor_reason="horizon")
    return data


def risk_row(e, k, row, at_risk, recovered):
    """生成单条候风险记录。随访零候仅用于登记没有观察区间的事件。"""
    return dict(episode_id=e["episode_id"], t0_tmin=e["t_min"], followup_pentad=k,
                t_abs=row["t_abs"], at_risk=at_risk, recovery_event=recovered,
                censor_administrative=False, censor_missing=False, censor_data_end=False,
                last_valid_t_abs=row["t_abs"], smp=row["smp"], active_flag=row["active_flag"],
                analysis_weight=1.0)


def mk_trend(years, values):
    """计算 Sen 斜率、MK 检验、Ljung–Box 一阶检验及 Hamed–Rao 修正。"""
    x, y = np.asarray(years), np.asarray(values)
    n = len(x)
    slope = sen(x, y)
    diffs = y[np.newaxis, :] - y[:, np.newaxis]
    score = np.sign(diffs[np.triu_indices(n, 1)]).sum()
    _, counts = np.unique(y, return_counts=True)
    var = (n*(n-1)*(2*n+5)-np.sum(counts*(counts-1)*(2*counts+5)))/18
    centered = y-y.mean()
    den = centered @ centered
    r = centered[:-1] @ centered[1:] / den if den else 0
    q = n*(n+2)*r*r/(n-1)
    lb = erfc(sqrt(q/2))
    residual = y-slope*(x-x[0])
    # 去趋势后的并列值取平均秩。
    order = np.sort(residual)
    ranks = (np.searchsorted(order, residual, "left")+np.searchsorted(order, residual, "right")+1)/2
    ranks -= ranks.mean()
    denom = ranks @ ranks
    cutoff = NormalDist().inv_cdf(.975)/sqrt(n)
    correction = 0.0
    for lag in range(1, n-2):
        ac = ranks[lag:] @ ranks[:-lag] / denom if denom else 0
        if abs(ac) > cutoff:
            correction += (n-lag)*(n-lag-1)*(n-lag-2)*ac
    factor = 1 + 2*correction/(n*(n-1)*(n-2))
    def pvalue(variance):
        if score == 0:
            return 1.0
        return erfc((abs(score)-1)/sqrt(2*variance)) if variance > 0 else None
    return slope, pvalue(var), lb, pvalue(var*factor)


def holm(values):
    """使用完整注册家族大小，对已知 p 值做 Holm 调整。"""
    result = {name: None for name in values}
    running = 0
    available = sorted((p, name) for name, p in values.items() if p is not None)
    for i, (p, name) in enumerate(available):
        running = max(running, min(1, (len(values)-i)*p))
        result[name] = running
    return result


def grouped_bootstrap(values, blocks, statistic, params, seed_offset):
    """按完整依赖块重抽样，计算统计量的百分位区间。"""
    _, labels = np.unique(blocks, return_inverse=True)
    n = len(set(labels))
    if len(values) < params["infer"]["min_n"] or n < params["infer"]["min_blocks"]:
        return None, None
    pieces = [values[labels == j] for j in range(n)]
    rng = np.random.default_rng(params["random"]["seed"]+seed_offset)
    stats = [statistic(np.concatenate([pieces[i] for i in rng.integers(n, size=n)]))
             for _ in range(params["block"]["bootstrap_n"])]
    return tuple(np.quantile(stats, [.025, .975]))


def spline_design(h):
    """以四个不同节点构建时间自由度为 3 的受限三次样条。"""
    t = np.arange(1, h+1, dtype=float)/h
    knots = np.linspace(t[0], 1, 4)
    cols = [np.ones(h), t]
    last = knots[-1]-knots[-2]
    for k in knots[:-2]:
        cols.append(np.maximum(t-k, 0)**3 -
                    (knots[-1]-k)/last*np.maximum(t-knots[-2], 0)**3 +
                    (knots[-2]-k)/last*np.maximum(t-knots[-1], 0)**3)
    return np.stack(cols, axis=1)


def cloglog_hazard(n, y, params, n_block):
    """用 Fisher 评分拟合分组二项 cloglog 时间基线。"""
    n, y = np.asarray(n, float), np.asarray(y, float)
    active = n > 0
    infer = params["infer"]
    if (n_block < infer["min_blocks"] or min(y.sum(), (n-y).sum()) < infer["min_event_per_arm"]):
        return None, "insufficient_support"
    design = spline_design(len(n))
    if active.sum() < 4 or y.sum() < infer["epv_min"]*4:
        design = design[:, :2]
    if active.sum() < design.shape[1] or y.sum() < infer["epv_min"]*design.shape[1]:
        return None, "insufficient_support"
    x = design[active]
    a, b = n[active], y[active]
    beta = np.zeros(design.shape[1])
    def probability(matrix, coefficients):
        return np.clip(-np.expm1(-np.exp(np.clip(matrix @ coefficients, -25, 4))), 1e-10, 1-1e-10)
    def likelihood(p):
        return float(b @ np.log(p)+(a-b) @ np.log1p(-p))
    for _ in range(100):
        eta = np.exp(np.clip(x @ beta, -25, 4))
        p = probability(x, beta)
        derivative = eta*np.exp(-eta)
        variance = p*(1-p)
        weight = a*derivative**2/variance
        information = x.T @ (x*weight[:, None])
        if np.linalg.cond(information) > 1e12:
            return None, "singular"
        step = np.linalg.solve(information, x.T @ ((b-a*p)*derivative/variance))
        scale = 1.0
        while scale > 1e-6 and likelihood(probability(x, beta+scale*step)) < likelihood(p)-1e-10:
            scale *= .5
        if scale <= 1e-6 or np.max(np.abs(beta)) > 100:
            return None, "nonconverged"
        beta += scale*step
        if np.max(np.abs(scale*step)) < 1e-7:
            return np.where(active, probability(design, beta), np.nan), "rcs_df3" if len(beta) == 4 else "linear_time"
    return None, "nonconverged"


def annual_rows(events, min_events, min_years):
    """按年计算条件 A 的中位数，并执行注册趋势检验。"""
    grouped = defaultdict(list)
    for e in events:
        if e["route1_A_eligible"]:
            grouped[e["year"]].append(e)
    result = []
    for field in ("RT", "delta_SMP", "RR"):
        values = {year: np.median([e[field] for e in group]) for year, group in sorted(grouped.items())
                  if len(group) >= min_events}
        row = dict(metric=field, n_years=len(values), slope_per_year=None, p_mk=None,
                   p_hamed_rao=None, p_ljung_box=None, p_primary=None, status="insufficient_years")
        if len(values) >= min_years:
            slope, mk, lb, hr = mk_trend(list(values), list(values.values()))
            row.update(slope_per_year=slope, p_mk=mk, p_hamed_rao=hr, p_ljung_box=lb,
                       p_primary=hr if lb < .05 else mk, status="estimated")
        result.append(row)
    adjusted = holm({r["metric"]: r["p_primary"] for r in result})
    for row in result:
        row["p_holm"] = adjusted[row["metric"]]
    return result


def summarize(ctx, events, blocks, recovered_counts, risk_counts, censor, risk_blocks):
    """为每个估计对象分别保存分母和支撑状态。"""
    p = ctx.params
    a = [e for e in events if e["route1_A_eligible"]]
    known = [e for e in a if e["F_R"] is not None]
    fr = [e for e in known if e["F_R"]]
    metrics = []
    for j, (field, group) in enumerate([(f, a) for f in ("RT", "delta_SMP", "RR")] +
                                       [("Lag_FR", fr), ("Lag_P", [e for e in a if e["P_term"]])]):
        values = np.array([e[field] for e in group])
        units = [blocks[e["grid_id"]] for e in group]
        qs = np.quantile(values, [.25, .5, .75]) if len(values) else [None]*3
        for k, (name, stat) in enumerate((("median", np.median), ("mean", np.mean))):
            lo, hi = grouped_bootstrap(values, units, stat, p, 2*j+k)
            metrics.append(dict(metric=field, statistic=name, n=len(values), n_block=len(set(units)),
                                estimate=float(stat(values)) if len(values) else None,
                                q25=qs[0], q75=qs[2], ci_low=lo, ci_high=hi,
                                ci_method="superblock_percentile" if lo is not None else None))
    for name, selected in (("q1_recovery_distribution.csv", metrics[:6]), ("q2_lag_result.csv", metrics[6:])):
        ctx.csv("tables", name, tuple(metrics[0]), selected)
    rates = np.array([e["F_R"] for e in known], dtype=float)
    lo, hi = grouped_bootstrap(rates, [blocks[e["grid_id"]] for e in known], np.mean, p, 20)
    ctx.csv("tables", "q2_fr_conditional_proportion.csv",
            ("n_A", "n_known", "n_unknown", "n_FR", "pre", "sync", "post", "proportion", "ci_low", "ci_high"),
            [dict(n_A=len(a), n_known=len(known), n_unknown=len(a)-len(known), n_FR=len(fr),
                  **{s: sum(e["F_R_subtype"] == s for e in fr) for s in ("pre","sync","post")},
                  proportion=float(rates.mean()) if len(rates) else None, ci_low=lo, ci_high=hi)])
    comparison = [e for e in known if e["F_R_subtype"] != "post"]
    contrasts = []
    for field in ("RR", "delta_SMP"):
        groups = [[e[field] for e in comparison if e["F_R"] == arm] for arm in (False, True)]
        contrasts.append(dict(metric=field, n_control=len(groups[0]), n_pre_sync=len(groups[1]),
                              unadjusted_median_difference=float(np.median(groups[1])-np.median(groups[0]))
                              if all(groups) else None, confirmatory_status="missing_frozen_adjustment_support"))
    ctx.csv("tables", "q2_h1_contrast.csv", tuple(contrasts[0]), contrasts)
    trends = annual_rows(events, p["trend"]["year_min_events"], p["trend"]["min_valid_years"])
    ctx.csv("tables", "q1_trend_result.csv", tuple(trends[0]), trends)
    hazard, status = cloglog_hazard(risk_counts, recovered_counts, p, len(risk_blocks))
    risk = [dict(followup=k, n_risk=int(n), n_recovered=int(y),
                 observed_hazard=y/n if n else None, fitted_cloglog_hazard=hazard[k-1] if hazard is not None else None,
                 fit_status=status)
            for k, (n, y) in enumerate(zip(risk_counts, recovered_counts), 1)]
    ctx.csv("tables", "q1_recovery_risk.csv", tuple(risk[0]), risk)
    ctx.csv("tables", "censoring_audit.csv", ("reason", "n"),
            [dict(reason=k, n=v) for k,v in sorted(censor.items())])
    ctx.report("reports", "m7_analysis_support.json", dict(
        n_event=len(events), n_A=len(a), n_recovery_blocks=len(risk_blocks),
        n_FR_known=len(known), n_FR=len(fr), H1="descriptive_only",
        Q3="descriptive_exposure_support_only" if 6 in ctx.bundles else "exposure_not_requested",
        Q1_risk=status, bootstrap_unit="100 km superblock"))


def grid_year_rows(history, events, cell, lai_years):
    """保留零事件格点年，分别记录入组、终止、急转和删失的分母。"""
    starts = defaultdict(list)
    for e in events:
        starts[e["year"]].append(e)
    for year, group in groupby(history, key=lambda r: r["year"]):
        group = list(group)
        annual = starts[year]
        a = [e for e in annual if e["route1_A_eligible"]]
        base = group[0]
        yield dict(product=base["product_combo"], definition_id="total_duration__smp_gt20",
                   grid_id=base["grid_id"], year=year, support_id=base["support_id"],
                   phenology_mode=base["phenology_mode"], L=base["active_window_length_L"],
                   N_onset=len(annual), N_A=len(a), N_FR=sum(e["F_R"] is True for e in a),
                   N_FZ=None, N_censored=sum(e["right_censored"] for e in annual),
                   affected_area=cell["area_km2"] if annual else 0.0,
                   phenology_fallback_rate=float(base["phenology_fallback"]),
                   LAI_availability=lai_years.get((base["grid_id"], year)), block_id=cell["block_id"],
                   data_completeness=sum(r["smp"] is not None for r in group)/len(group))


def exposure_support(ctx):
    """仅描述事件级 LAI 和深层水共同支撑，不拟合 Q3。"""
    if 6 not in ctx.bundles:
        return {}
    rows = ctx.rows(6, "event_exposure.parquet",
                    ["grid_id", "year", "LAI_full_window", "LAI_exposure_cluster", "deep_SM_pre4_percentile"])
    cluster = Counter(r["LAI_exposure_cluster"] for r in rows if r["LAI_full_window"])
    sizes = list(cluster.values())
    eligible = sum(sizes)
    ctx.report("reports", "q3_exposure_support.json", dict(
        n_event=len(rows), n_lai=eligible, n_exposure=len(cluster),
        n_deep=sum(r["deep_SM_pre4_percentile"] is not None for r in rows),
        n_lai_deep=sum(r["LAI_full_window"] and r["deep_SM_pre4_percentile"] is not None for r in rows),
        cluster_p95=float(np.quantile(sizes, .95)) if sizes else None,
        max_cluster_fraction=max(sizes)/eligible if eligible else None,
        status="descriptive_only"))
    return {(r["grid_id"], r["year"]): r["LAI_full_window"] for r in rows}


def run(ctx):
    """生成恢复风险表，保留 M5 定义的事件分母。"""
    events = ctx.rows(5, MASTER)
    geometry = {r["grid_id"]: r for r in ctx.rows(3, "grid_manifest.parquet")}
    memberships = superblocks(events, geometry)
    blocks = {g: memberships[r["block_id"]] for g,r in geometry.items()}
    by_grid = defaultdict(list)
    for e in events:
        by_grid[e["grid_id"]].append(e)
    h = ctx.params["route1"]["recovery_risk_h"]
    n, y = np.zeros(h, dtype=int), np.zeros(h, dtype=int)
    censor = Counter()
    risk_blocks = set()
    lai_years = exposure_support(ctx)
    with ctx.writer(RISK, "route1_recovery_person_pentad") as write, ctx.writer("grid_year.parquet", "grid_year") as write_year:
        for index, (grid, history) in enumerate(series(ctx)):
            grid_events = by_grid.pop(grid, [])
            write_year(grid_year_rows(history, grid_events, geometry[grid], lai_years))
            if not grid_events:
                continue
            ctx.progress.active(f"正在构建格点 {grid} 的恢复风险集", index, len(geometry), "个格点")
            by_t = {r["t_abs"]: r for r in history}
            for e in grid_events:
                rows = risk_rows(e, by_t, h)
                for row in rows:
                    k = row["followup_pentad"]-1
                    if row["at_risk"]:
                        n[k] += 1
                        y[k] += row["recovery_event"]
                        risk_blocks.add(blocks[grid])
                if rows:
                    censor[rows[-1].get("censor_reason") or "recovered"] += 1
                write(rows)
    if by_grid:
        raise ValueError("M7 的部分事件格点未出现在 M3 候表中")
    ctx.progress.active("正在计算恢复分布、区间和年度趋势")
    summarize(ctx, events, blocks, y, n, censor, risk_blocks)
