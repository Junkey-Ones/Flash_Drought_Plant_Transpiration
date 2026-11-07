"""M8 固定随访三出口和放行门槛，见技术手册第 8 节。

读取 M3 候表和 M5 事件表。向 M9、M10 提供事件—候和出口表。
主随访从 t_s 后一候开始，长度取 M0 冻结值。
最小示例：python -B workflow/downstream.py m8 --help
退出码：0 成功，1 执行失败，2 预检失败，130 中断。
"""
from collections import Counter, defaultdict

from fdwt.stages.m03_support_climatology.climatology import series
from fdwt.stages.m05_recovery_precip.recovery import MASTER, qualifies

EXIT = "route2_exit.parquet"
PERSON = "route2_person_pentad.parquet"


def preflight(ctx):
    """按事件量检查内存预算和主随访长度。"""
    h = ctx.params["route2"]["h"]
    memory = 128 * 2**20 + ctx.bundles[5]["manifest"]["row_counts"][MASTER] * 6000
    ctx.plan["memory_budget_bytes"] = memory
    if not 1 <= h <= 73 or memory > 0.6 * ctx.plan["memory_bytes"]:
        raise ValueError("路线二随访长度或内存预算超出范围")


def follow(e, by_t, h, p_min):
    """逐候记录首次出口。急转类型未知、删失和持续未恢复分别编码。"""
    t0 = e["t_s"]
    end = max(by_t)
    rows = []
    censored, unknown, extreme = False, False, False
    exit_type, t_exit, reason, censor_t = None, None, None, None
    for k in range(1, h+1):
        t = t0+k
        r = by_t.get(t, {})
        smp = r.get("smp")
        recovered = t == e["t_e"]
        p95 = qualifies(r, p_min) if r else None
        if smp is None:
            censored, reason, censor_t = True, "data_end" if t > end else "missing_smp", t-1
            break
        if recovered:
            fr = e["F_R"]
            unknown = fr is None
            exit_type = None if unknown else "B" if fr else "A"
            t_exit = t
        else:
            extreme |= p95 is True
            if k == h:
                exit_type, t_exit = "C", t
        rows.append(dict(
            episode_id=e["episode_id"], followup_pentad=k, t0_ts=t0, t_abs=t,
            at_risk=True, exit_A=exit_type == "A", exit_B=exit_type == "B",
            exit_C_at_H=exit_type == "C", censored=False, censor_reason=None,
            exit_type_unknown=unknown, extreme_without_recovery=not recovered and p95 is True,
            smp_tv=smp, p95_flag_tv=p95, recovery_this_pentad=int(recovered),
            fr_type_known=not unknown if recovered else None,
            F_R_subtype=e["F_R_subtype"] if recovered else None, first_exit_t_abs=t_exit))
        if recovered or exit_type == "C":
            break
    if censored:
        if not rows:
            rows.append(dict(episode_id=e["episode_id"], followup_pentad=0, t0_ts=t0, t_abs=t0,
                             at_risk=False, exit_A=False, exit_B=False, exit_C_at_H=False,
                             exit_type_unknown=False, smp_tv=by_t[t0]["smp"]))
        rows[-1].update(censored=True, censor_reason=reason)
    strict = exit_type == "B" and e["F_R_subtype"] == "post"
    result = dict(episode_id=e["episode_id"], route2_version=f"main_h{h}", H=h,
                  exit_type=exit_type, exit_time=t_exit, censored=censored, censor_time=censor_t,
                  censor_reason=reason, exit_type_unknown=unknown,
                  exit_A_strict=exit_type == "A" or strict, strict_reclass_from_B=strict,
                  extreme_without_recovery_ever=extreme, followup_complete=exit_type is not None)
    return rows, result


def gate(exits, geometry, grid_for, params):
    """按已知出口计算描述门槛，并遵守 M0 的建模限制。"""
    r = params["route2"]
    counts = Counter(e["exit_type"] or ("unknown" if e["exit_type_unknown"] else "censored") for e in exits)
    valid = [e for e in exits if e["followup_complete"]]
    blocks = Counter(geometry[grid_for[e["episode_id"]]]["block_id"] for e in valid)
    bblocks = Counter(geometry[grid_for[e["episode_id"]]]["block_id"] for e in valid if e["exit_type"] == "B")
    reasons = []
    if counts["B"] < r["exit_b_min"]:
        reasons.append("B_below_minimum")
    if len(blocks) < r["exit_blocks_min"]:
        reasons.append("blocks_below_minimum")
    return dict(status="exit_model" if reasons else "descriptive_only", model_allowed=False,
                frozen_release=params["protocol"]["analysis_release"]["M8"],
                counts=dict(counts), n_event=len(exits), n_complete=len(valid), n_block=len(blocks),
                max_B_block_fraction=max(bblocks.values(), default=0)/counts["B"] if counts["B"] else None,
                triggers=reasons)


def run(ctx):
    """写主随访的候表和主、备选随访长度的事件级出口表。"""
    egroup = defaultdict(list)
    events = ctx.rows(5, MASTER, ["episode_id", "grid_id", "t_s", "t_e", "F_R", "F_R_status", "F_R_subtype"])
    for e in events:
        egroup[e["grid_id"]].append(e)
    h = ctx.params["route2"]["h"]
    alternatives = ctx.params["spec_curve"]["candidate_alternatives"]["route2"]["followup"]
    horizons = sorted(set([h, *alternatives]))
    outcomes = {k: [] for k in horizons}
    geometry = {r["grid_id"]: r for r in ctx.rows(3, "grid_manifest.parquet")}
    grid_for = {e["episode_id"]: e["grid_id"] for e in events}
    with ctx.writer(PERSON, "route2_person_pentad") as w, ctx.writer(EXIT, "route2_exit") as wex:
        for index, (grid, history) in enumerate(series(ctx)):
            if grid not in egroup:
                continue
            ctx.progress.active(f"正在判定格点 {grid} 的三类随访出口", index, len(geometry), "个格点")
            by_t = {r["t_abs"]: r for r in history}
            for e in egroup.pop(grid):
                group = []
                for k in horizons:
                    rows, out = follow(e, by_t, k, ctx.params["fr"]["min_p_mm"])
                    if k == h:
                        w(rows)
                    group.append(out)
                    outcomes[k].append(out)
                wex(group)
    if egroup:
        raise ValueError("路线二事件未能连接 M3 格点")
    ctx.progress.active("正在汇总随访出口并核对样本门槛")
    main = gate(outcomes[h], geometry, grid_for, ctx.params)
    ctx.report("reports", "route2_gate.json", main)
    audit = []
    for k in horizons:
        counts = gate(outcomes[k], geometry, grid_for, ctx.params)["counts"]
        for name in ("A", "B", "C", "unknown", "censored"):
            n = counts.get(name, 0)
            audit.append(dict(horizon=k, exit_type=name, n=n, n_total=len(outcomes[k]),
                              fraction=n/len(outcomes[k]) if outcomes[k] else None))
    ctx.csv("tables", "route2_sample_audit.csv", tuple(audit[0]), audit)
    if main["status"] == "descriptive_only":
        ctx.csv("tables", "route2_exit_description.csv", tuple(audit[0]), audit)
    ctx.report("reports", "route2_exit_report.json", dict(
        status=main["status"], triggers=main["triggers"], n_event=main["n_event"],
        strict_B_to_A=sum(e["strict_reclass_from_B"] for e in outcomes[h]),
        notes="主出口保持原分类，恢复后急转类型未知另列"))
