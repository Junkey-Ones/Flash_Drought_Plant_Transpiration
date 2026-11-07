# 工作流入口

此目录用于实现阶段依赖、运行入口、续跑和产物发布。科学计算代码放入 `src/fdwt/stages/`。入口及发布规则见[文件操作与存放规范](../docs/file_operations.md)。

## M1 原始数据审计

入口位于 `src/fdwt/stages/m01_source_audit/audit.py`，从项目根目录运行。正式模式使用冻结的 M0 配置、表契约和环境，要求参与代码已固定在 Git。

```powershell
$m0 = 'metadata/runs/v1.3.1__f062ea869961__436dbca2574e__seed_13120261102/run_config.json'
$python = '.\.venv\Scripts\python.exe'
$audit = 'src/fdwt/stages/m01_source_audit/audit.py'
& $python -B $audit --mode test --config $m0 --product era5_land --start 2004-02-01 --end 2004-03-02 --dry-run
& $python -B $audit --mode test --config $m0 --product era5_land --start 2004-02-01 --end 2004-03-02
& $python -B $audit --mode production --config $m0 --workers 2
```

日期含端点。样例可读取 ERA5-Land 相邻月份核查跨月累计语义。`--check-only` 执行完整只读检查并向标准输出写报告。`--resume` 校验已有交接和原始输入的哈希，或在尚未发布时另起尝试。

有效交接目录为 `data/frozen/<run_id>/m01_source_audit/`。M2 须校验 `m1_complete.json` 的模式、上游身份、输入及成员哈希，并检查请求分支的放行范围。测试使用独立的 `test__` 标识。必需输入受阻时，审计表和放行判定留在终端所示的尝试目录，退出码为 1。日志位于对应尝试的 `logs/` 目录。

末日累计缺少次日 00 UTC 时，缺档表登记精确时次。报告分别记录瞬时场、累计场和地表径流的可用日期。跨产品覆盖表记录名义共同范围，格点级共同有效支持仍需后续数据质控。
