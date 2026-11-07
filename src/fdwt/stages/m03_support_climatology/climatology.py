"""M3 固定活跃季、支撑掩膜和独立气候态，见技术手册第 5 节。

读取 M2 候表、日历和静态网格，生成 M4–M9 使用的掩膜、物候、气候态和候表。
正式模式要求完整基准期。局部试跑样本不足时气候态为空。
最小示例：python -B workflow/downstream.py m3 --help
退出码：0 成功，1 执行失败，2 预检失败，130 中断。
"""
from collections import Counter
from contextlib import ExitStack
from datetime import date, timedelta
from itertools import groupby
import json
from statistics import NormalDist

import numpy as np
import pyarrow.parquet as pq
from pyproj import Transformer

GRID = "grid_pentad_era5land__era5land.parquet"
CLIM = "grid_climatology_era5land.parquet"
COMBO = "era5land__era5land"
PHENOLOGY = "fixed_apr_sep"
MASK = "no_landcover_mask"
SUPPORT = "own_full"
VERSION = "existing_data_r1"
BASE = "grid_pentad_base_era5_land.parquet"
FIELDS = ("sm_0_100_mm", "sm_deep_mm", "p_mm", "et_loss_mm", "runoff_mm",
          "balance_mm", "valid_native_area_frac")


def preflight(ctx):
    """验证冻结方案以及转置缓存的容量。"""
    p = ctx.params
    if (p["phenology"]["primary_mode"] != "fixed_april_september" or
            p["mask"]["primary_mode"] != MASK or not p["clim"]["p95_include_zero"]):
        raise ValueError("当前 M3 入口只支持冻结的固定活跃季及相应掩膜方案")
    base = pq.ParquetFile(ctx.path(2, BASE))
    n = base.metadata.num_rows
    disk = n * 200 + 64 * 2**20
    mem = 128 * 2**20 + 73 * 24 * ctx.args.chunk_cells * 2200
    ctx.plan.update(input_pentads=n, disk_budget_bytes=disk, memory_budget_bytes=mem)
    if disk > ctx.plan["free_bytes"] or mem > 0.6 * ctx.plan["memory_bytes"]:
        raise ValueError("磁盘空间或内存不足，无法转置和写出 M3 候表")


def plotting_probability(sample, values, rules):
    """对有限值计算 Gringorten 概率，并列值取平均秩。"""
    sample = np.sort(np.asarray(sample, dtype=float))
    values = np.asarray(values, dtype=float)
    ranks = (np.searchsorted(sample, values, "left") +
             np.searchsorted(sample, values, "right") + 1) / 2
    p = (ranks - rules["gringorten_a"]) / (len(sample) + rules["gringorten_b"])
    eps = float(rules["normal_p_eps"])
    return np.where(np.isfinite(values), np.clip(p, eps, 1 - eps), np.nan)


def pooled_indices(calendar, pentad, years, offsets):
    """沿连续候轴扩展基准年份的同日历候，保留跨年邻候。"""
    n = len(calendar)
    indices = {row["t_abs"]: i for i, row in enumerate(calendar)}
    anchors = [row["t_abs"] for row in calendar if row["pentad"] == pentad]
    pool = {indices[t + offset] for t in anchors for offset in offsets if t + offset in indices}
    return np.array(sorted(i for i in pool if 0 <= i < n and years[0] <= calendar[i]["year"] <= years[1]),
                    dtype=int)


def active_days(row):
    """计算候与固定 4–9 月活跃窗的重叠天数。"""
    return max(0, (min(row["end_date"], date(row["year"], 9, 30)) -
                   max(row["start_date"], date(row["year"], 4, 1))).days + 1)


def block_id(x, y, km):
    """以 EPSG:6933 投影原点划分方形公里块。"""
    return f"b{km}_{int(x // (1000 * km))}_{int(y // (1000 * km))}"


def series(ctx):
    """按格点读取 M3 候历史，一次仅保留一个读取批次。"""
    reader = pq.ParquetFile(ctx.path(3, GRID))
    rows = (row for batch in reader.iter_batches(batch_size=8192) for row in batch.to_pylist())
    previous = ""
    for grid_id, group in groupby(rows, key=lambda row: row["grid_id"]):
        history = list(group)
        if grid_id <= previous or any(b["t_abs"] != a["t_abs"] + 1 for a, b in zip(history, history[1:])):
            raise ValueError("M3 候表未按格点排列，或候序列不连续")
        previous = grid_id
        yield grid_id, history


def transpose(ctx, calendar, cells):
    """单次扫描以时间为主序的 M2 候表，检查选定格点的候键完整性。"""
    n, k = len(calendar), len(cells)
    disk = ctx.work / "base_transpose.bin"
    arrays = np.memmap(disk, dtype="float64", mode="w+", shape=(len(FIELDS), n, k))
    arrays[:] = np.nan
    seen = np.zeros((n, k), dtype=bool)
    start = calendar[0]["t_abs"]
    index = {row["grid_id"]: i for i, row in enumerate(cells)}
    columns = ("grid_id", "t_abs", "product_code", "grid_version", "support_id", *FIELDS)
    support = set()
    for batch in pq.ParquetFile(ctx.path(2, BASE)).iter_batches(columns=columns, batch_size=65536):
        ids = batch.column(0).to_pylist()
        locations = np.array([index.get(key, -1) for key in ids], dtype=int)
        keep = locations >= 0
        g = locations[keep]
        t = batch.column(1).to_numpy()[keep] - start
        if not len(t):
            continue
        if (t.min() < 0 or t.max() >= n or seen[t, g].any() or len(np.unique(t * k + g)) != len(t)):
            raise ValueError("M2 存在重复主键或超出候日历的记录")
        products = set(batch.column(2).to_pylist())
        versions = set(batch.column(3).to_pylist())
        support.update(batch.column(4).to_pylist())
        if products != {"era5_land"} or versions != {ctx.params["grid"]["main_version"]} or len(support) != 1:
            raise ValueError("M2 候表包含不一致的产品、网格或支撑范围")
        seen[t, g] = True
        for j in range(len(FIELDS)):
            values = batch.column(j + 5).to_numpy(zero_copy_only=False)[keep]
            if np.isinf(values).any():
                raise ValueError("M2 物理量包含无穷值")
            arrays[j, t, g] = values
    if not seen.all():
        raise ValueError("M2 缺少要求的格点—候记录")
    return arrays


def grid_history(cell, calendar, pool, values, p, active, lengths):
    """计算单格点 SMP、降水阈值和水量平衡 W，缺测保持为空。"""
    sm, deep, precip, et, runoff, balance, coverage = values
    # M2 已独立质控各变量。共同支撑字段用于审计，缺降水不应擦除有效土壤水。
    rules = p["clim"]
    smp = np.full(len(calendar), np.nan)
    bp = smp.copy()
    normal = NormalDist()
    pentads = np.array([row["pentad"] for row in calendar])
    output = []
    records = []
    for pentad, indices in pool.items():
        sel = pentads == pentad
        samples = [arr[indices][np.isfinite(arr[indices])] for arr in (sm, balance, precip)]
        s, b, rain = samples
        wet = rain[rain > 0]
        n, nb, nr = map(len, samples)
        if n >= rules["min_n"]:
            smp[sel] = plotting_probability(s, sm[sel], rules) * rules["smp_multiplier"]
        if nb >= rules["min_n"]:
            bp[sel] = plotting_probability(b, balance[sel], rules)
        p95, p99 = np.quantile(rain, [0.95, 0.99]) if nr >= rules["min_n"] else (np.nan, np.nan)
        wet95 = np.quantile(wet, 0.95) if len(wet) >= rules["p95_min_nonzero_n"] else np.nan
        usable = bool(p95 > 0 and len(wet) >= rules["p95_min_nonzero_n"])
        records.append(dict(product_code="era5land", grid_version=cell["grid_version"],
                            grid_id=cell["grid_id"], pentad=pentad, clim_version=VERSION,
                            clim_start_year=rules["base_window"][0], clim_end_year=rules["base_window"][1],
                            pool_offset=max(abs(offset) for offset in rules["pentad_pool"]), sm_n=n, balance_n=nb, p_n=nr,
                            p_nonzero_n=len(wet), p_zero_fraction=(nr-len(wet))/nr if nr else None,
                            p95_mm=p95, p99_mm=p99, wet_p95_mm=wet95, p95_usable=usable,
                            sm_distribution_id=f"{VERSION}:{cell['grid_id']}:{pentad}:sm",
                            balance_distribution_id=f"{VERSION}:{cell['grid_id']}:{pentad}:balance",
                            climatology_qc_status="complete" if n >= rules["min_n"] else "incomplete"))
    for i, row in enumerate(calendar):
        ref = records[row["pentad"] - 1]
        valid = bool(np.isfinite(smp[i]))
        output.append(dict(
            **{name: row[name] for name in ("t_abs", "year", "pentad", "start_date", "end_date", "n_days")},
            product_combo=COMBO, soil_product="era5land", precip_product="era5land",
            grid_version=cell["grid_version"], grid_id=cell["grid_id"], support_id=SUPPORT,
            phenology_mode=PHENOLOGY, clim_version=VERSION, active_flag=bool(active[i]),
            active_window_length_L=lengths[row["year"]], phenology_fallback=False,
            sm_0_100_mm=sm[i], smp=smp[i], sm_rank_n=ref["sm_n"], p_mm=precip[i],
            p_mm_per_day=precip[i]/row["n_days"], p_5day_equivalent_mm=5*precip[i]/row["n_days"],
            p95_mm=ref["p95_mm"], p95_n=ref["p_n"], p_nonzero_n=ref["p_nonzero_n"],
            p95_usable=ref["p95_usable"], et_loss_mm=et[i], runoff_mm=runoff[i], balance_mm=balance[i],
            balance_rank_n=ref["balance_n"], balance_p=bp[i],
            w=normal.inv_cdf(bp[i]) if np.isfinite(bp[i]) else None, sm_deep_mm=deep[i],
            valid_native_area_frac=coverage[i], qc_status="complete" if valid else "missing_qc",
            missing_reason=None if valid else ("insufficient_climatology" if np.isfinite(sm[i]) else "missing_smp")))
    return records, output


def run(ctx):
    """生成支撑表并按冻结期检查候日历。"""
    calendar = ctx.rows(2, "time_pentad_calendar.parquet")
    cells = ctx.rows(2, "grid_static_covariates.parquet",
                     ["grid_version", "grid_id", "lon", "lat", "area_km2", "boundary_include"])
    cells = sorted((cell for cell in cells if cell["boundary_include"]), key=lambda cell: cell["grid_id"])
    if not calendar or not cells or any(b["t_abs"] != a["t_abs"] + 1 for a, b in zip(calendar, calendar[1:])):
        raise ValueError("M2 网格或候日历为空，或候日历不连续")
    if ctx.args.mode == "production":
        first, last = ctx.params["clim"]["base_window"]
        if calendar[0]["start_date"] != date(first, 1, 1) or calendar[-1]["end_date"] != date(last, 12, 31):
            raise ValueError("正式运行需要 M2 提供完整的冻结基准期")
    cells = cells[:ctx.args.max_cells]
    rules = ctx.params["clim"]
    pool = {i: pooled_indices(calendar, i, rules["base_window"], rules["pentad_pool"]) for i in range(1, 74)}
    active = [active_days(row) for row in calendar]
    lengths = Counter()
    for row in calendar:
        y = row["year"]
        # 局部试跑仍使用完整的冻结活跃窗长度。
        if y not in lengths:
            lengths[y] = sum(active_days(dict(year=y, start_date=date(y, 1, 1) + timedelta(days=5*i),
                                             end_date=date(y, 1, 1) + timedelta(days=5*i+4))) > 0
                             for i in range(73))
    proj = Transformer.from_crs(4326, 6933, always_xy=True)
    for cell in cells:
        x, y = proj.transform(cell["lon"], cell["lat"])
        block = block_id(x, y, ctx.params["block"]["km"])
        cell.update(block_id=block, superblock_id=block, valid_area_fraction=1.0, grid_qc_status="complete",
                    grid_geometry_id=f"epsg4326:{cell['grid_id']}")
    ctx.write("grid_manifest.parquet", "grid_manifest", cells)
    ctx.write("grid_mask.parquet", "grid_mask", [
        dict(grid_version=cell["grid_version"], grid_id=cell["grid_id"], mask_version=MASK,
             mask_include=True, no_landcover_mask_flag=True, baseline_mask_flag=False)
        for cell in cells])
    stats = Counter()
    ctx.progress.active("正在读取 M2 候表并按格点整理")
    arrays = transpose(ctx, calendar, cells)
    outputs = ((CLIM, "grid_climatology"), (GRID, "grid_pentad"),
               ("grid_active_pentad.parquet", "grid_active_pentad"),
               ("grid_phenology_year.parquet", "grid_phenology_year"))
    with ExitStack() as stack:
        writes = [stack.enter_context(ctx.writer(name, contract)) for name, contract in outputs]
        chunk = ctx.args.chunk_cells
        for start in range(0, len(cells), chunk):
            ctx.progress.active(f"正在计算第 {start+1} 至 {min(start+chunk, len(cells))} 个格点的气候态", start, len(cells), "个格点")
            blocks = [[], [], [], []]
            for i in range(start, min(start+chunk, len(cells))):
                cell = cells[i]
                clim, history = grid_history(cell, calendar, pool, arrays[:, :, i], ctx.params, active, lengths)
                blocks[0].extend(clim)
                blocks[1].extend(history)
                stats["smp_valid"] += sum(r["qc_status"] == "complete" for r in history)
                stats["p95_usable"] += sum(r["p95_usable"] for r in history)
                keys = dict(grid_version=cell["grid_version"], grid_id=cell["grid_id"], phenology_mode=PHENOLOGY)
                blocks[2].extend(dict(**keys, **{k: r[k] for k in ("t_abs", "year", "pentad", "active_flag",
                                                                 "active_window_length_L", "phenology_fallback")},
                                      active_window_id=f"{PHENOLOGY}:{r['year']}",
                                      selection_sensitivity_group="primary") for r in history)
                blocks[3].extend(dict(**keys, year=y, active_window_start=date(y, 4, 1).timetuple().tm_yday,
                                      active_window_end=date(y, 9, 30).timetuple().tm_yday, phenology_fallback=False,
                                      phenology_qc_status="complete") for y in sorted(lengths))
            for write, rows in zip(writes, blocks):
                write(rows)
    del arrays
    ctx.report("reports", "m3_climatology_support.json",
               dict(**stats, support=SUPPORT, cells=len(cells), pentads=len(calendar),
                    active_definition="one or more overlapping days", phenology_mode=PHENOLOGY,
                    block_projection="EPSG:6933", block_origin_m=[0, 0]))
