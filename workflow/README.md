# 工作流入口

此目录用于实现阶段依赖、运行入口、续跑和产物发布。科学计算代码放入 `src/fdwt/stages/`。入口及发布规则见[文件操作与存放规范](../docs/file_operations.md)。

M1 和 M2 可用 `--help` 查看中文命令示例。支持 ANSI 的交互终端默认原地刷新面板，运行结束后保留结果、报告和日志路径。重定向输出时使用纯文本进度，标准输出保留 JSON 计划。`--progress plain` 可手动切换为纯文本。后续阶段见 [M3–M10 运行说明](downstream.md)。

## M1 原始数据审计

入口位于 `src/fdwt/stages/m01_source_audit/audit.py`，从项目根目录运行。正式模式使用冻结的 M0 配置、表契约和环境，要求参与代码已固定在 Git。

```powershell
$m0 = 'metadata/runs/v1.3.1__ee0d9fb46eb2__561932e2cedf__seed_13120261102/run_config.json'
$python = '.\.venv\Scripts\python.exe'
$audit = 'src/fdwt/stages/m01_source_audit/audit.py'
& $python -B $audit --mode test --config $m0 --product era5_land --start 2023-12-27 --end 2023-12-31 --workers 2 --dry-run
& $python -B $audit --mode test --config $m0 --product era5_land --start 2023-12-27 --end 2023-12-31 --workers 2
& $python -B $audit --mode production --config $m0 --workers 2
```

日期含端点。样例可读取 ERA5-Land 相邻月份核查跨月累计语义。面板按文件计数，并发时逐槽显示当前文件。`--check-only` 执行完整只读检查，重定向标准输出可取得 JSON 报告。`--resume` 校验已有交接和原始输入的哈希，或在尚未发布时另起尝试。

有效交接目录为 `data/frozen/<run_id>/m01_source_audit/`。M2 须校验 `m1_complete.json` 的模式、上游身份、输入及成员哈希，并检查请求分支的放行范围。测试使用独立的 `test__` 标识。必需输入受阻时，审计表和放行判定留在终端所示的尝试目录，退出码为 1。日志位于对应尝试的 `logs/` 目录。

末日累计缺少次日 00 UTC 时，缺档表登记精确时次。报告分别记录瞬时场、累计场和地表径流的可用日期。跨产品覆盖表记录名义共同范围，格点级共同有效支持仍需后续数据质控。

## M2 ERA5-Land 日值与候值

入口为 `src/fdwt/stages/m02_preprocess/preprocess.py`。以下命令从项目根目录执行。`$m0` 使用上节配置，`$m1test` 指向已发布的测试清单。

```powershell
$m2 = 'src/fdwt/stages/m02_preprocess/preprocess.py'
$m1test = 'data/frozen/test__v1.3.1__ee0d9fb46eb2__561932e2cedf__seed_13120261102__m1_0dd6edb1e7d4/m01_source_audit/m1_complete.json'
& $python -B $m2 --mode test --config $m0 --m1 $m1test --start 2023-12-27 --end 2023-12-31 --dry-run
& $python -B $m2 --mode test --config $m0 --m1 $m1test --start 2023-12-27 --end 2023-12-31
# 正式放行后才执行
& $python -B $m2 --mode production --config $m0
```

测试日期含端点。日表写出所选日，候表读取并写出与该范围相交的完整候。如果 M1 清单的有效范围缺少候内某日，预检会拒绝运行。累计场月末读取次月文件的首个 00 UTC 时次。M2 按月缓存、按候聚合并写出 Parquet，目前支持一个读取工作单元。生产要求正式 M1 清单和已固定的代码，默认处理 M0 冻结的完整事件期。

`--dry-run` 验证元数据并显示计划与实际读取日期。 `--check-only` 另逐个核对计划读取的原始文件哈希。`--resume` 复核原始文件、身份和已发布产物，或在尚无发布时另开尝试。成功交接位于 `data/frozen/<run_id>/m02_preprocess/`，报告位于 `outputs/<run_id>/reports/m02_preprocess/<attempt_id>/`。下游通过 `m2_complete.json` 校验身份及逐文件哈希。日志位于对应尝试目录。重试后仅消费完成清单引用的报告。

M2 面板按已写出的候计数，只读校验和复用交接时按已校验的原始文件计数。命令打印的完成清单和报告目录可直接用于核对结果。若工作区版本改变，请以当前 M1 试跑结束时显示的完成清单替换示例中的 `$m1test`。

### 表契约与缺测

- `time_pentad_calendar.parquet`、`grid_day_era5_land.parquet` 和 `grid_pentad_base_era5_land.parquet` 沿用 M0 冻结字段。UTC 候日历以每年 1 月 1 日开始，第 73 候保留 5 或 6 天。相邻候编号沿时间轴连续。
- 瞬时状态日均要求该格点当天 24 个小时有效。根区储量需要三层在同一批有效小时均完整。候状态逐变量检验有效日数和最长缺口，候通量逐变量要求日值齐全。日、候的 `valid_native_area_frac` 表示根区储量与降水的共同网格支持。`state_valid_days` 和最长缺口描述根区储量。`flux_complete_flag` 表示 P、ET 和 PET 三项完整。分变量的日记录覆盖及候值通过格点数记在 `m2_coverage.csv`。
- 当前 ERA5-Land 规则经纬网与主网格对齐，使用冻结的完整格点面积。地表径流按 M1 明确放行的日期输出。缺少地下径流时，地下径流、总径流和水量平衡保留为空。
- 冻结注册表未定义 `grid_static_covariates`。当前方案使用 `m2_grid_r1` 补充契约，主键为 `grid_version + grid_id`，包含经度、纬度、球面面积和域内标记。气候背景协变量需要完整的 2000–2023 年范围。气候背景、土壤、地形和地类各字段同时提供缺失原因。此实现范围为当前方案放行的 ERA5-Land 主链。GLDAS、CHIRPS 和跨产品处理需相应 M0 配置与 M1 放行，MODIS 像元质控由对应下游阶段接手。

### 中文面板样例验收

用 Windows 伪终端运行 ERA5-Land 2023-12-27 至 2023-12-31 的试跑。M1 使用两个工作进程，六份文件通过，耗时约 5.6 秒，单进程峰值约 266 MiB。M2 处理一候和 13,741 个格点，交接四张表共 96,188 行，耗时约 1.9 秒，峰值约 250 MiB，交接文件约 3.03 MiB。

两阶段均验证了原地刷新、中文帮助、参数错误、运行预览、只读检查和纯文本续跑。M1 输入清单哈希及放行判定与改造前一致，M2 四张表的数据逐表一致。全仓 39 项测试通过。验证范围为试跑，正式全量运行尚未执行。
