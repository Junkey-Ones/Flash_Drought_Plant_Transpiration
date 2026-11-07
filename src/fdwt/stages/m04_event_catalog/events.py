"""M4 格点骤旱事件识别及区域连接，见技术手册第 6 节。

读取 M3 连续格点历史及网格，向 M5、M10 提供事件骨架、候选审计和区域成员。
默认沿用上游范围。最小示例：python -B workflow/downstream.py m4 --help
退出码：0 成功，1 执行失败，2 预检失败，130 中断。
"""
from collections import Counter, defaultdict
import json

import numpy as np

from fdwt.stages.m03_support_climatology.climatology import series

EVENTS = "event_master_base.parquet"


def preflight(ctx):
    """为事件及区域图索引预估单工作单元内存。"""
    rows = ctx.bundles[3]["manifest"]["row_counts"]["grid_pentad_era5land__era5land.parquet"]
    memory = 128 * 2**20 + rows * 150
    ctx.plan["memory_budget_bytes"] = memory
    if memory > 0.6 * ctx.plan["memory_bytes"]:
        raise ValueError("内存不足，无法建立区域事件连接图")


def identify(history, params, termination=None, development=False):
    """扫描一条连续历史，返回合格事件和候选审计。

    SMP 以百分位点计。恰好 40 可截断片段，但不能当作大于 40 的锚点。
    缺测重置锚点，并删失已达到最低观察持续期的事件。
    """
    dry = params["drought"]
    high, low = dry["smp_above"], dry["smp_below"]
    threshold = params["recovery"]["terminate_smp"] if termination is None else termination
    duration_id = "development_duration" if development else "total_duration"
    term_id = f"smp_gt{threshold:g}"
    rows, audit = [], []
    if not history:
        return rows, audit
    smp = [row["smp"] for row in history]
    n = len(history)
    anchor = None
    i = 0
    while i < n:
        value = smp[i]
        if value is None or value >= high:
            if value is None or value > high:
                anchor = i if value is not None else None
            i += 1
            continue
        s = i
        while i < n and smp[i] is not None and smp[i] < high:
            i += 1
        minimum = min(range(s, i), key=smp.__getitem__)
        e = minimum + 1
        while e < n and smp[e] is not None and smp[e] <= threshold:
            e += 1
        recovered = e < n and smp[e] is not None
        end = e if recovered else e - 1
        base, trough, last = history[s], history[minimum], history[end]
        duration = end - s
        rate = (smp[anchor] - smp[minimum]) / (minimum - anchor) if anchor is not None else None
        key = f"{base['product_combo']}__{duration_id}__{term_id}__{base['grid_id']}__{base['t_abs']}"
        required = minimum - s + 1 if development else duration
        reason = next((message for reject, message in (
            (anchor is None, "no_contiguous_above_anchor"), (smp[minimum] >= low, "minimum_not_below_threshold"),
            (rate is not None and rate < dry["drop_rate_pct_per_pentad"], "development_too_slow"),
            (required < dry["min_duration_pentads"], "insufficient_duration")) if reject), None)
        audit.append(dict(candidate_id=key, product=base["product_combo"], grid_id=base["grid_id"],
                          t_s_candidate=base["t_abs"], t_min_candidate=trough["t_abs"],
                          t_e_candidate=last["t_abs"] if recovered else None,
                          smp_above_anchor=smp[anchor] if anchor is not None else None,
                          smp_min=trough["smp"], development_rate=rate, duration_observed=duration,
                          continuity_ok=anchor is not None,
                          active_window_ok=base["active_flag"] and trough["active_flag"],
                          eligible_main=reason is None, exclusion_reason=reason,
                          code_version="v1.3.1"))
        if reason is not None:
            continue
        deficit = [max(0, high-x) for x in smp[s:end+1]]
        record = dict(
            episode_id=key, grid_id=base["grid_id"], product=base["product_combo"],
            **{k: base[k] for k in ("soil_product", "precip_product", "grid_version",
                                    "support_id", "phenology_mode", "year", "phenology_fallback")},
            duration_definition=duration_id, termination_rule=term_id,
            t_s=base["t_abs"], t_min=trough["t_abs"], t_e=last["t_abs"] if recovered else None,
            right_censored=not recovered, censor_type="none" if recovered else ("data_end" if e == n else "missing_smp"),
            censor_t_abs=None if recovered else last["t_abs"], duration=duration if recovered else None,
            observed_duration=duration, development_rate=rate, smp_min=trough["smp"],
            mean_deficit=float(np.mean(deficit)), max_deficit=max(deficit),
            route1_A_eligible=bool(recovered and base["active_flag"] and last["active_flag"]),
            recovery_risk_eligible=bool(base["active_flag"] and trough["active_flag"]),
            active_window=f"{base['phenology_mode']}:{base['year']}", data_completeness=1.0,
            missingness_flags=[] if recovered else ["right_censored"])
        for name, row in (("t_s", base), ("t_min", trough), ("t_e", last if recovered else None)):
            record.update({f"{name}_{part}": row[part] if row else None for part in ("year", "pentad")})
        rows.append(record)
    return rows, audit


class Components:
    """用并查集确定性合并事件或空间块。"""
    def __init__(self):
        self.parents = {}

    def root(self, key):
        self.parents.setdefault(key, key)
        while self.parents[key] != key:
            self.parents[key] = self.parents[self.parents[key]]
            key = self.parents[key]
        return key

    def union(self, a, b):
        a, b = sorted((self.root(a), self.root(b)))
        self.parents[b] = a


def regional(events, geometry, width, height):
    """连接同候和相邻候四邻域格点，保留初始足迹至少含三格的区域事件。"""
    starts, stops = defaultdict(list), defaultdict(list)
    graph = Components()
    for event in events:
        starts[event["t_s"]].append(event)
        stops[event["t_s"] + event["observed_duration"] + 1].append(event)
        graph.root(event["episode_id"])
    if not events:
        return [], {}
    active, previous = {}, {}
    for t in range(min(starts), max(stops)):
        for event in stops[t]:
            active.pop(event["grid_id"])
        for event in starts[t]:
            if event["grid_id"] in active:
                raise ValueError("同一格点出现重叠的骤旱事件")
            active[event["grid_id"]] = event["episode_id"]
        for grid, key in active.items():
            cell = int(grid[1:]) - 1
            row, col = divmod(cell, width)
            indices = [cell]
            indices += [cell-1] if col else []
            indices += [cell+1] if col+1 < width else []
            indices += [cell-width] if row else []
            indices += [cell+width] if row+1 < height else []
            for index in indices:
                neighbor = f"g{index+1:06d}"
                for observed in (active, previous):
                    if neighbor in observed:
                        graph.union(key, observed[neighbor])
        previous = active.copy()
    groups = defaultdict(list)
    for event in events:
        groups[graph.root(event["episode_id"])].append(event)
    ordered = sorted(groups.values(), key=lambda group: (min(e["t_s"] for e in group),
                                                        min(e["grid_id"] for e in group)))
    regions, accepted = [], {}
    for group in ordered:
        first = min(e["t_s"] for e in group)
        initial = {e["grid_id"] for e in group if e["t_s"] == first}
        if len(initial) < 3:
            continue
        grids = {e["grid_id"] for e in group}
        cluster = f"regional_{len(regions)+1:08d}"
        for e in group:
            e["cluster_id"] = cluster
        regions.append(dict(cluster_id=cluster, product=group[0]["product"],
                            definition_id=f"{group[0]['duration_definition']}__{group[0]['termination_rule']}",
                            start_t_abs=first, end_t_abs=max(e["t_s"]+e["observed_duration"] for e in group),
                            n_grid_episodes=len(group), footprint_area_km2=sum(geometry[g]["area_km2"] for g in sorted(grids)),
                            initial_footprint_cells=len(initial), regional_qc_status="complete"))
        accepted[cluster] = group
    return regions, accepted


def run(ctx):
    """构建主事件库，保留随访未终止和植被资料缺失的事件。"""
    geometry = {r["grid_id"]: r for r in ctx.rows(3, "grid_manifest.parquet")}
    catalog = []
    reasons = Counter()
    with ctx.writer("event_candidate_audit.parquet", "event_candidate_audit") as write:
        for index, (grid, history) in enumerate(series(ctx)):
            ctx.progress.active(f"正在识别格点 {grid} 的骤旱事件", index, len(geometry), "个格点")
            episodes, audit = identify(history, ctx.params)
            catalog.extend(episodes)
            write(audit)
            reasons.update(row["exclusion_reason"] or "accepted" for row in audit)
    domain = ctx.params["grid"]["domain"]
    ctx.progress.active("正在连接同候和相邻候的区域事件")
    regions, groups = regional(catalog, geometry, domain["longitude_count"], domain["latitude_count"])
    ctx.write(EVENTS, "event_master", catalog)
    ctx.write("event_regional.parquet", "event_regional", regions)
    with ctx.writer("event_regional_member.parquet", "event_regional_member") as write:
        for region in regions:
            key = region["cluster_id"]
            for e in sorted(groups[key], key=lambda row: row["episode_id"]):
                write(dict(cluster_id=key, episode_id=e["episode_id"], t_abs=t, grid_id=e["grid_id"],
                           cell_area_km2=geometry[e["grid_id"]]["area_km2"], connected_flag=True,
                           footprint_flag=t == region["start_t_abs"])
                      for t in range(e["t_s"], e["t_s"] + e["observed_duration"] + 1))
    ctx.csv("reports", "m4_event_sample_flow.csv", ("reason", "n"),
            [dict(reason=reason, n=n) for reason, n in sorted(reasons.items())])
