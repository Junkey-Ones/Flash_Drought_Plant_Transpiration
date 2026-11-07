"""M3–M10 阶段算法和交接验证。

默认执行可移植单元测试。具备本地 r2 冻结配置时，也构造合成 M2
完整基准期并逐阶段测试。所有生成文件均由 pytest 临时目录管理。
最小示例：python -B -m pytest -q tests/test_m3_m10_downstream.py
退出码采用 pytest 约定。
"""
from datetime import date
from io import StringIO
import json
from pathlib import Path
import sys

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))
import downstream as flow
import terminal_panel
from preprocess import calendar_rows, fingerprint
from fdwt.stages.m03_support_climatology.climatology import CLIM, GRID, pooled_indices, plotting_probability
from fdwt.stages.m04_event_catalog import events as m4
from fdwt.stages.m04_event_catalog.events import identify
from fdwt.stages.m05_recovery_precip.recovery import MASTER, classify, evaluate, wet_runs
from fdwt.stages.m06_exposure.exposure import lai_mask, trailing
from fdwt.stages.m07_route1.route1 import holm, risk_rows
from fdwt.stages.m08_route2.route2 import EXIT, follow


def test_terminal_panel_repaints_and_restores_on_interrupt(monkeypatch):
    """面板用同一组光标坐标刷新，且中断时退出独立缓冲区并恢复光标。"""
    class Capture(StringIO):
        def fileno(self):
            return 2

    stream = Capture()
    restored = []
    monkeypatch.setattr(terminal_panel, "enable_vt", lambda _: lambda: restored.append(True))
    monkeypatch.setattr(terminal_panel.os, "get_terminal_size", lambda _: (64, 24))
    with pytest.raises(KeyboardInterrupt), terminal_panel.TerminalPanel(stream, True) as panel:
        panel.draw("M3 · 气候态", "试跑", "正在读取候表", "已完成 0 步", "成功 0", "已用 1 秒", 0)
        panel.draw("M3 · 气候态", "试跑", "正在计算格点", "已完成 1 步", "成功 1", "已用 2 秒", 1/3, .5)
        raise KeyboardInterrupt
    screen = stream.getvalue()
    assert screen.startswith(terminal_panel.ENTER)
    assert screen.count("\x1b[1;1H") == 2 and "\n" not in screen
    assert "正在读取候表" in screen and "正在计算格点" in screen and "\x1b[42m" in screen
    assert screen.endswith(terminal_panel.LEAVE) and restored == [True]


@pytest.fixture
def rules():
    """构造各算法所需的最小冻结参数。"""
    return dict(drought=dict(smp_above=40, smp_below=20, min_duration_pentads=4,
                             drop_rate_pct_per_pentad=5),
                recovery=dict(terminate_smp=20, early_pentads=2, post_te_trajectory=[1, 2, 3, 4, 5]),
                pterm=dict(window="[t_min,t_e+2]", min_p_mm=1),
                fr=dict(window="[t_e-1,t_e+1]", min_p_mm=1))


def history(values):
    """生成跨年第 73 候的连续格点历史。"""
    out = []
    for i, value in enumerate(values, 69):
        y, p = divmod(i-1, 73)
        out.append(dict(t_abs=i, smp=value, active_flag=True, grid_id="g000001", year=2000+y,
                        pentad=p+1, product_combo=flow.PRODUCT, soil_product="era5land",
                        precip_product="era5land", grid_version="test", support_id="own_full",
                        phenology_mode="fixed_apr_sep", phenology_fallback=False, p_mm=0.5,
                        p95_mm=2.0, p95_usable=True, p_mm_per_day=.1, p_5day_equivalent_mm=.5))
    return out


def test_reference_cross_year_and_tied_rank():
    """跨年借样，且同值取平均秩。"""
    cal = calendar_rows(date(2000, 1, 1), date(2002, 12, 31))
    pool = pooled_indices(cal, 73, [2000, 2002], [-1, 0, 1])
    assert cal[pool[2]]["t_abs"] == 74
    p = plotting_probability([1, 2, 2, 3], [2, np.nan],
                             dict(gringorten_a=.44, gringorten_b=.12, normal_p_eps=1e-6))
    assert p[0] == pytest.approx(.5)
    assert np.isnan(p[1])


def test_recovery_and_different_window_completeness(rules):
    """P-term 尚未知时，F_R 仍可已知。严格规则只改变路线二备选出口。"""
    rows = history([60, 36, 22, 9, 9, 29, 60])
    rows[-1]["p_mm"] = 4
    e = identify(rows, rules)[0][0]
    assert (e["t_s"], e["t_min"], e["t_e"]) == (70, 72, 74)
    by_t = {r["t_abs"]: r for r in rows}
    e, hits, _ = evaluate(e, by_t, rules, wet_runs(rows, 1))
    assert e["F_R"] is True and e["F_R_subtype"] == "post"
    assert e["P_term"] is None and e["Lag_P"] is None
    assert e["Lag_FR"] == 3 and hits[0]["t_abs"] == 75
    assert risk_rows(e, by_t, 12)[-1]["recovery_event"]
    route, out = follow(e, by_t, 12, 1)
    assert out["exit_type"] == "B" and out["strict_reclass_from_B"]
    assert route[-1]["t_abs"] == e["t_e"]


def test_gap_and_unknown_exit(rules):
    """缺口后的资料不会重启同一风险集，恢复后急转未知也不归 A。"""
    rows = history([60, 30, 15, 12, 7, 8, None, 2, 45])
    e = identify(rows, rules)[0][0]
    assert e["right_censored"] and e["censor_type"] == "missing_smp"
    by_t = {r["t_abs"]: r for r in rows}
    assert risk_rows(e, by_t, 12)[-1]["censor_missing"]
    rows = history([60, 30, 15, 12, 7, 21])
    e = identify(rows, rules)[0][0]
    by_t = {r["t_abs"]: r for r in rows}
    e, _, _ = evaluate(e, by_t, rules, {})
    _, out = follow(e, by_t, 5, 1)
    assert out["exit_type"] is None and out["exit_type_unknown"] and not out["censored"]


def test_qa_strict_antecedent_and_holm():
    """主质量掩膜、暴露窗和注册家族大小分别校验。"""
    q = np.array([0, 2, 32, 64, 8, 255], dtype=np.uint8)
    np.testing.assert_array_equal(lai_mask(np.ones(6), q, np.zeros(6, dtype=np.uint8)),
                                  [True, True, True, False, False, False])
    assert not lai_mask(np.array([5]), np.array([32]), np.array([0]), strict=True)[0]
    np.testing.assert_allclose(trailing([{"water": v} for v in [1, 2, None, 4, 5, 6]], "water", 2),
                               [np.nan, np.nan, 1.5, np.nan, np.nan, 4.5], equal_nan=True)
    assert holm({"a": .02, "b": .03, "c": None}) == {"a": .06, "b": .06, "c": None}


def test_downstream_chain(tmp_path, monkeypatch, capsys):
    """以合成物理候值连接全部阶段，并检查干跑、续跑和篡改。"""
    local = ROOT / "config/existing_data_r2"
    if not (local / "schema_registry.yaml").exists():
        pytest.skip("本机 r2 冻结表契约未提供")
    params, registry = [yaml.safe_load((local / name).read_bytes())
                        for name in ("params.yaml", "schema_registry.yaml")]
    run = "s"
    m0 = dict(m0_run_id=run, m0_sha256="a"*64, environment_hash="test", code={})
    config = tmp_path / "metadata/runs" / run / "run_config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"deviations":[]}', encoding="utf-8")
    for name in ("params.yaml", "specification_matrix.yaml"):
        (config.parent / name).write_bytes((local / name).read_bytes())
    monkeypatch.setattr(flow, "ROOT", tmp_path)
    monkeypatch.setattr(flow.m1, "ROOT", tmp_path)
    monkeypatch.setattr(flow.m1, "load_m0", lambda *_: (params, registry, m0))
    monkeypatch.setattr(flow, "code_files", lambda *_: {})
    root = tmp_path / "data/frozen/test__synthetic" / flow.STAGES[2]
    root.mkdir(parents=True)
    cal = calendar_rows(date(2001, 1, 1), date(2023, 12, 31))
    cells = [dict(grid_version="era5land_010", grid_id=f"g{i:06d}", lon=100+(i-1)*.1,
                  lat=42., area_km2=92., boundary_include=True) for i in range(1,4)]
    rain = {31: 12., 37: 15.}
    states = {30: 89., 31: 72., 32: 71., 33: 72., 34: 73., 35: 120.}
    records = []
    for row in cal:
        y, p = row["year"], row["pentad"]
        for c in cells:
            records.append(dict(grid_id=c["grid_id"], grid_version=c["grid_version"],
                                t_abs=row["t_abs"], product_code="era5_land", support_id="synthetic",
                                sm_0_100_mm=states.get(p, 101.) if y == 2008 else 100.+(y-2001)*.05,
                                sm_deep_mm=300.+y-2000, p_mm=rain.get(p, 3.) if y == 2008 else 3.,
                                et_loss_mm=1., runoff_mm=None, balance_mm=None, valid_native_area_frac=1.))
    data = {"time_pentad_calendar.parquet": cal, "grid_static_covariates.parquet": cells,
            "grid_pentad_base_era5_land.parquet": records, "grid_day_era5_land.parquet": [{"unused": 0}]}
    for name, rows in data.items():
        table = pa.Table.from_pylist(rows)
        if name == "grid_pentad_base_era5_land.parquet":
            for col in ("runoff_mm", "balance_mm"):
                idx = table.column_names.index(col)
                table = table.set_column(idx, col, pa.nulls(len(table), type=pa.float64()))
        pq.write_table(table, root / name)
    marker = root / "m2_complete.json"
    identity = dict(**{k: v for k,v in m0.items() if k != "code"}, mode="test", code={},
                    start="2001-01-01", end="2023-12-31", m1_run_id="test__synthetic_m1", m1_sha256="b"*64)
    manifest = dict(manifest_version=1, stage=flow.STAGES[2], run_id=root.parent.name, validation="passed",
                    schema_version="v1.3.1", identity=identity,
                    artifacts={n:fingerprint(root/n) for n in data}, row_counts={n:len(r) for n,r in data.items()})
    marker.write_text(json.dumps(manifest), encoding="utf-8")
    previous = {2: marker}

    def call(number, *flags):
        args = [f"m{number}", "--config", str(config), "--mode", "test", "--progress", "plain", *flags]
        for i in flow.REQUIRED[number]:
            args.extend(["--input", str(previous[i])])
        result = flow.main(args)
        captured = capsys.readouterr()
        assert result == 0, captured.err
        return json.loads(captured.out.splitlines()[0])

    plan = call(3, "--max-cells", "3", "--dry-run")
    assert not Path(plan["work"]).exists()
    for i in range(3, 11):
        if i == 4:
            args = ["m4", "--config", str(config), "--mode", "test", "--progress", "plain",
                    "--input", str(previous[3])]
            def interrupt(_):
                raise KeyboardInterrupt
            with monkeypatch.context() as patch:
                patch.setattr(m4, "run", interrupt)
                assert flow.main(args) == 130
            captured = capsys.readouterr()
            interrupted = json.loads(captured.out.splitlines()[0])
            assert "已中断" in captured.err and "运行日志：" in captured.err
            assert "\x1b[" not in captured.err
            assert not Path(interrupted["target"]).exists()
        plan = call(i, *(["--max-cells", "3"] if i == 3 else []))
        path = Path(plan["target"]) / f"m{i}_complete.json"
        assert path.is_file()
        previous[i] = path
    final = pq.read_table(previous[5].parent / MASTER).to_pylist()
    assert len(final) >= 3 and any(e["F_R"] is not None for e in final)
    call(10, "--resume")
    source = previous[3].parent / CLIM
    args = ["m4", "--mode", "test", "--config", str(config), "--input", str(previous[3]), "--check-only"]
    with source.open("r+b") as stream:
        stream.write(b"BROK")
    assert flow.main(args) == 1
    with source.open("ab") as stream:
        stream.write(b"changed")
    assert flow.main(args) == 2
