"""M6 MCD15A2H 年 LAI 与事件前暴露，见原生数据 SOP 第 7 节。

读取 M3、M5 和可选的 M1 已审计瓦片。向 M7、M8、M10 提供植被年值、
事件暴露和暴露簇审计。年份和覆盖阈值采用 M0 冻结值。
最小示例：python -B workflow/downstream.py m6 --help
退出码：0 成功，1 执行失败，2 预检失败，130 中断。
"""
from collections import defaultdict
import csv
from datetime import date
from itertools import groupby
import json
import re

import numpy as np
from pyhdf.SD import SD, SDC
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject

from fdwt.stages.m03_support_climatology.climatology import (
    MASK, PHENOLOGY, active_days, plotting_probability, series)
from fdwt.stages.m05_recovery_precip.recovery import MASTER, sen
from preprocess import calendar_rows, fingerprint, m1

EXPOSURE = "event_exposure.parquet"
SINUSOIDAL = "+proj=sinu +R=6371007.181 +units=m +no_defs"
STRIP_ROWS = 96


def preflight(ctx):
    """检查冻结的 MODIS 产品与条带读取内存预算。"""
    lai = ctx.params["lai"]
    if lai["product"] != "MCD15A2H.061" or lai["composite_days"] != 8 or not lai["no_future_fill"]:
        raise ValueError("当前入口只支持冻结的 MCD15A2H.061 八天 LAI 方案")
    if ctx.plan["memory_bytes"] * 0.6 < 256 * 2**20:
        raise ValueError("可用内存不足以读取一条 MODIS 瓦片条带")


def lai_mask(raw, qc, extra, strict=False):
    """按 SOP 第 7.2–7.4 节解析无符号 QA 位，剔除填充值和低质量像元。"""
    return ((raw <= 100) & (raw >= 0) & (qc != 255) & ((qc & 29) == 0)
            & ((qc >> 5) <= (0 if strict else 1)) & (extra != 255)
            & ((extra & 3) <= 1) & ((extra & 124) == 0))


def source_tiles(ctx):
    """选出通过 M1 审计的唯一瓦片和对应源文件指纹。"""
    if 1 not in ctx.bundles:
        return {}
    path = ctx.path(1, "source_file_manifest.csv")
    first, last = ctx.params["time"]["periods"]["lai_complete"]
    tiles = defaultdict(list)
    keys = set()
    with path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["product_code"] != "MCD15A2H" or row["audit_status"] != "passed":
                continue
            start, end = date.fromisoformat(row["time_start"]), date.fromisoformat(row["time_end"])
            if not first <= start.year <= last:
                continue
            file = m1.safe(m1.ROOT / row["file_path"])
            match = re.search(r"\.(h\d{2}v\d{2})\.061\.", file.name)
            if match is None or not file.is_relative_to(m1.ROOT / "data_raw"):
                raise ValueError("已审计的 LAI 文件路径或瓦片编号不符合约定")
            tile = match[1]
            key = start, tile
            if key in keys:
                raise ValueError(f"发现重复的 MCD15A2H 合成期及瓦片：{key}")
            keys.add(key)
            expected = dict(bytes=int(row["file_size_bytes"]), sha256=row["sha256"])
            tiles[start.year, tile].append((start, end, file, expected,
                                            json.loads(row["spatial_extent"])))
    return tiles


def read_stripe(file, offset, height):
    """从三个 HDF4 科学数据集读取固定高度的原生像元条带。"""
    hdf = SD(str(file), SDC.READ)
    arrays = []
    try:
        for name in ("Lai_500m", "FparLai_QC", "FparExtra_QC"):
            layer = hdf.select(name)
            try:
                arrays.append(np.asarray(layer.get(start=(offset, 0), count=(height, 2400)), dtype=np.uint8))
            finally:
                layer.endaccess()
    finally:
        hdf.end()
    return arrays


def tile_annual(ctx, composites, active, shape, target_transform, strict):
    """先在原生像元上合成年值，再按有效面积重采样。

    各波段分别累计有效面积、LAI 乘面积、时间覆盖率乘面积和有效候数乘面积。
    重投影过程中面积用平方米，交接表按平方公里记录。
    """
    composites = sorted(composites)
    west, south, east, north = composites[0][4]
    weights = np.array([[max(0, (min(row["end_date"], end) - max(row["start_date"], start)).days+1)
                         for start, end, *_ in composites] for row in active], dtype=np.uint8)
    relevant = weights.any(axis=0)
    weights = weights[:, relevant]
    composites = [item for item, keep in zip(composites, relevant) if keep]
    output = np.zeros((4, *shape))
    if not composites:
        return output
    for _, _, path, expected, bounds in composites:
        if bounds != [west, south, east, north] or fingerprint(path) != expected:
            raise ValueError(f"已审计的 MODIS 原始文件发生变化：{path}")
    days = sum(row["n_days"] for row in active)
    pixel_size = (east-west)/2400
    dy = (north-south)/2400
    scale = ctx.params["lai"]["scale_factor"]
    for offset in range(0, 2400, STRIP_ROWS):
        ctx.progress.active(f"正在合成 {composites[0][0].year} 年 LAI 瓦片 {composites[0][2].name}", offset, 2400, "行像元")
        height = min(STRIP_ROWS, 2400-offset)
        raw = [read_stripe(item[2], offset, height) for item in composites]
        valid = [lai_mask(*bands, strict=strict) for bands in raw]
        sums = np.zeros((height, 2400))
        n = np.zeros_like(sums)
        observed = np.zeros_like(sums)
        for row_weights in weights:
            denominator = np.zeros_like(sums)
            numerator = np.zeros_like(sums)
            for j in np.flatnonzero(row_weights):
                w = row_weights[j] * valid[j]
                denominator += w
                numerator += (raw[j][0] * scale) * w
            mask = denominator > 0
            sums += np.divide(numerator, denominator, out=np.zeros_like(sums), where=mask)
            n += mask
            observed += denominator
        good = n > 0
        area = pixel_size * dy
        source = np.stack([good * area, np.divide(sums, n, out=np.zeros_like(sums), where=good)*area,
                           observed/days*area, n*area])
        trans = from_bounds(west, north-(offset+height)*dy, east, north-offset*dy, 2400, height)
        dest = np.zeros_like(output)
        reproject(source, dest, src_transform=trans, src_crs=SINUSOIDAL,
                  dst_transform=target_transform, dst_crs="EPSG:4326", resampling=Resampling.sum,
                  num_threads=1, warp_mem_limit=32)
        output += dest
    for _, _, path, expected, _ in composites:
        if fingerprint(path) != expected:
            raise ValueError(f"MODIS 原始文件在合成期间发生变化：{path}")
    return output


def annual_lai(ctx, geometry, tiles):
    """生成全范围的格点年值，保留缺少已验收瓦片的格点年。"""
    p = ctx.params
    west, south, east, north = p["grid"]["domain"]["cell_center_bounds"]
    domain = p["grid"]["domain"]
    dx = domain["resolution_deg"] / 2
    shape = domain["latitude_count"], domain["longitude_count"]
    trans = from_bounds(west-dx, south-dx, east+dx, north+dx, shape[1], shape[0])
    lo, hi = p["time"]["periods"]["lai_complete"]
    rows, main = [], {}
    for year in range(lo, hi+1):
        ctx.progress.active(f"正在整理 {year} 年 LAI 合成窗口")
        calendar = calendar_rows(date(year, 1, 1), date(year, 12, 31))
        active = [row for row in calendar if active_days(row)]
        files = [items for (y, _), items in sorted(tiles.items()) if y == year]
        for strict in (False, True):
            total = np.zeros((4, *shape))
            for composites in files:
                total += tile_annual(ctx, composites, active, shape, trans, strict)
            for g, cell in geometry.items():
                i, j = divmod(int(g[1:])-1, shape[1])
                area, laiarea, timearea, narea = total[:, i, j]
                cell_area = cell["area_km2"] * 1e6
                if area > cell_area * 1.01:
                    raise ValueError(f"MODIS 有效面积重复或重投影结果无效：{g} {year}")
                space = min(1.0, area/cell_area)
                time = timearea/area if area else 0.0
                good = bool(space >= p["lai"]["annual_space_cover_min"] and
                            time >= p["lai"]["annual_time_cover_min"])
                row = dict(grid_id=g, year=year, vegetation_product=p["lai"]["product"],
                           phenology_mode=PHENOLOGY, mask_version=MASK, qc_version="strict" if strict else "main",
                           vegetation_annual_mean=laiarea/area if good else None, active_pentad_n=len(active),
                           valid_pentad_n=round(narea/area) if area else 0, temporal_coverage=time,
                           native_valid_area=area/1e6, total_native_area=cell_area/1e6, spatial_coverage=space,
                           annual_valid=good, strict_qc_flag=strict,
                           exclusion_reason=None if good else "missing_source" if not files else "insufficient_coverage")
                rows.append(row)
                if not strict:
                    main[g, year] = row["vegetation_annual_mean"]
    return rows, main


def trailing(history, field, width):
    """计算严格早于当前候的完整滑动均值，缺候保持为空。"""
    data = np.array([r[field] for r in history], dtype=float)
    result = np.full(len(data), np.nan)
    if len(data) > width:
        blocks = np.lib.stride_tricks.sliding_window_view(data, width)
        result[width:] = blocks[:-1].mean(axis=1)
    return result


def exposure(e, history, index, deep, shallow, annual, params):
    """连接事件前完整年份，并用同日历候参考样本计算百分位。"""
    t, y, grid = e["t_s"], e["year"], e["grid_id"]
    i = index[t]
    base = history[i]
    width = params["lai"]["window_years"]
    years = range(y-width, y)
    points = [(year, annual[grid, year]) for year in years if annual.get((grid, year)) is not None]
    lo, hi = params["time"]["periods"]["lai_exposure"]
    full = lo <= y <= hi and len(points) == params["lai"]["main_requires_full"]
    values = [v for _, v in points]
    sl = sen([year for year, _ in points], values) if full else None
    mask = np.array([r["pentad"] == base["pentad"] and
                     params["clim"]["base_window"][0] <= r["year"] <= params["clim"]["base_window"][1]
                     for r in history])
    deep_ref, shallow_ref = [values[mask & np.isfinite(values)] for values in (deep, shallow)]
    d, s = deep[i], shallow[i]
    # 同日历候的深层参考分布每年最多贡献一个值。
    enough = len(deep_ref) > 1 and np.isfinite(d)
    sd = deep_ref.std(ddof=1) if enough else np.nan
    phase = 2*np.pi*(base["pentad"]-1)/params["time"]["pentads_per_year"]
    version = f"lai{width}_full{params['lai']['main_requires_full']}_{PHENOLOGY}"
    result = dict(episode_id=e["episode_id"], exposure_version=version, grid_id=grid, year=y,
                  window_start_year=y-width, window_end_year=y-1, window_years=width,
                  LAI_base=float(np.mean(values)) if full else None, LAI_trend=sl,
                  LAI_n_valid_years=len(points), LAI_full_window=full,
                  LAI_exposure_cluster=f"lai__{grid}__{y}" if full else None,
                  LAI_missing_reason=None if full else "outside_period" if not lo <= y <= hi else "incomplete_window",
                  NDVI_missing_reason="missing_source", deep_SM_pre4=d, shallow_SM_pre4=s,
                  deep_SM_pre4_percentile=float(100*plotting_probability(deep_ref, d, params["clim"])) if enough else None,
                  deep_SM_pre4_z=(d-deep_ref.mean())/sd if sd > 0 else None,
                  deep_window_start_tabs=t-params["deep"]["pre_pentads"], deep_window_end_tabs=t-1,
                  deep_missing_reason="missing_smp" if not np.isfinite(d) else None if enough else "insufficient_climatology",
                  shallow_SM_pre4_percentile=float(100*plotting_probability(shallow_ref, s, params["clim"]))
                  if len(shallow_ref) > 1 and np.isfinite(s) else None,
                  pentad_sin=float(np.sin(phase)), pentad_cos=float(np.cos(phase)), year_centered_2000=y-2000,
                  active_window_length_L=base["active_window_length_L"], exposure_complete_L=False,
                  exposure_complete_D=False, exposure_complete_I=False,
                  core_covariate_missing_reason="missing_registered_soil_terrain_and_land_cover")
    loo = [sen([yy for j, (yy, _) in enumerate(points) if j != k],
               [vv for j, (_, vv) in enumerate(points) if j != k]) for k in range(len(points))] if full else []
    return result, loo


def run(ctx):
    """输出 LAI 样本支撑和共享格点年暴露簇，按冻结范围停在描述性审计。"""
    geometry = {r["grid_id"]: r for r in ctx.rows(3, "grid_manifest.parquet")}
    annual, main = annual_lai(ctx, geometry, source_tiles(ctx))
    ctx.write("vegetation_grid_year_lai.parquet", "vegetation_grid_year", annual)
    events = defaultdict(list)
    for row in ctx.rows(5, MASTER, ["episode_id", "grid_id", "year", "t_s"]):
        events[row["grid_id"]].append(row)
    clusters, loo_rows = [], []
    diagnostics = defaultdict(int)
    rng = np.random.default_rng(ctx.params["random"]["seed"])
    width = ctx.params["deep"]["pre_pentads"]
    total = sum(map(len, events.values()))
    view = "event_master_v1_3_1"
    fields = ("LAI_base", "LAI_trend", "LAI_exposure_cluster", "NDVI_base",
              "NDVI_trend", "deep_SM_pre4", "shallow_SM_pre4")
    ctx.derived_contract(view, "event_master", "event_exposure", fields)
    with ctx.writer(EXPOSURE, "event_exposure") as write, ctx.writer(view+".parquet", view) as compat:
        for grid, history in series(ctx):
            if grid not in events:
                continue
            index = {row["t_abs"]: i for i, row in enumerate(history)}
            deep, shallow = [trailing(history, field, width) for field in ("sm_deep_mm", "sm_0_100_mm")]
            block = []
            merged = []
            for e in events.pop(grid):
                row, loo = exposure(e, history, index, deep, shallow, main, ctx.params)
                block.append(row)
                merged.append(dict(e, **{field: row.get(field) for field in fields}))
                diagnostics[(row["LAI_missing_reason"] or "complete", row["deep_missing_reason"] or "complete")] += 1
                if loo:
                    loo_rows.append(dict(episode_id=e["episode_id"], LAI_trend=row["LAI_trend"],
                                         leave_one_out_min=min(loo), leave_one_out_max=max(loo)))
            write(block)
            compat(merged)
            usable = sorted((r for r in block if r["LAI_full_window"]), key=lambda r: r["LAI_exposure_cluster"])
            for key, group in groupby(usable, key=lambda r: r["LAI_exposure_cluster"]):
                group = list(group)
                clusters.append(dict(analysis_id="lai_support", LAI_exposure_cluster=key, grid_id=grid,
                                     year=group[0]["year"], n_event=len(group), cluster_event_fraction=len(group)/total,
                                     selected_event_seed_id=ctx.params["random"]["seed_id"],
                                     selected_episode_id=group[int(rng.integers(len(group)))]["episode_id"],
                                     inverse_cluster_weight=1/len(group), block_id=geometry[grid]["block_id"]))
    if events:
        raise ValueError("连接事件暴露时有格点缺失")
    ctx.write("exposure_cluster_audit.parquet", "exposure_cluster_audit", clusters)
    ctx.csv("reports", "m6_leave_one_year_out.csv", ("episode_id", "LAI_trend", "leave_one_out_min", "leave_one_out_max"), loo_rows)
    ctx.csv("reports", "m6_exposure_missingness.csv", ("lai", "deep", "n"),
            [dict(lai=lai, deep=deep, n=n) for (lai,deep), n in sorted(diagnostics.items())])
    ctx.report("reports", "m6_support.json", dict(
        total_events=total, lai_exposure_clusters=len(clusters), ndvi="missing_source",
        q3="descriptive_only", temporal_method="native complete-overlap coverage",
        spatial_method="GDAL sum resampling from MODIS sinusoidal to EPSG:4326"))
