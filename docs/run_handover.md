# FD-WT v1.3.1 项目托孤任务书

交接日期为 2026-11-08。接手人从正在运行的 M1 开始。M1 放行后，按本任务书执行 M2–M10。每阶段先核对输入，执行后验收交接，再启动下一阶段。

当前方案为 `existing_data_era5land_grid_r1`。正式事件期和气候基准期均为 2001–2023 年，使用 ERA5-Land 原生主网格，范围为 100°–115°E、33°–42°N，含两端格点。研究域对外称为黄土高原及邻近区。活跃窗固定为 4–9 月，主分析使用数据质控掩膜。Q1 和 Q2 的 F_R 分支在 M1–M3 质控通过后进入分析。Q3 交付 LAI 和深层水的描述性支撑审计，路线二按门槛交付条件描述。

## 开工与续跑

在 Windows Terminal 或 VS Code 的集成终端中打开 PowerShell。每次新开终端先执行这段。整份任务书的命令共用这组变量。

```powershell
Set-Location 'F:\Flash_Drought_Plant_Transpiration'
$env:PYTHONIOENCODING = 'utf-8'
$python = '.\.venv\Scripts\python.exe'
$run = 'v1.3.1__ee0d9fb46eb2__561932e2cedf__seed_13120261102'
$m0 = "metadata/runs/$run/run_config.json"
$frozen = "data/frozen/$run"
& $python -B --version
git rev-parse --short HEAD
git status --short
```

- 当前冻结包记录 Python 3.12.14。正式运行还要匹配整个环境指纹。保留 `.venv`，按 [准备阶段验收](preparation_readiness.md) 记录实际后端、真实样例、资源预算和最小恢复证据。参与运行的代码须已纳入 Git，且文件内容与本次交接的指纹匹配。
- 下文正式命令逐段执行。M1–M10 如需预览，把命令中的 `--resume` 换成 `--dry-run`。预览只检查入口和计划，M2 及以后仍须已有必需的上游清单。`--resume` 在已有交接时复验并复用，在尚无交接时另开一次尝试。M1 的 `--check-only` 执行完整只读审计，M2–M10 的只读检查主要核对运行输入。
- M1–M10 的交互命令都显式使用 `--progress panel`。直接在终端执行，保留标准错误连接到终端。若当前窗口不支持原地刷新，入口会在预检时明确提示。`--progress plain` 留给需要逐行日志的批处理。
- 完成标准是退出码为 0、终端给出本次正式完成清单，且通过本阶段的验收条目。在命令返回后立即读取 `$LASTEXITCODE`。退出码 1 通常表示执行或验收失败，2 表示预检或参数失败，130 表示中断。以终端和 `run.jsonl` 的实际原因为准。
- 正式交接位于 `data/frozen/<run_id>/<stage>/`。M2–M10 的报告及结果位置以完成清单中的 `reports` 路径为准。下游通过完成清单校验身份与成员哈希。失败现场在 `data/interim/<run_id>/<stage>/<attempt_id>/`，日志在 `logs/<run_id>/<stage>/<attempt_id>/run.jsonl`。
- 同一阶段只保留一个正在执行的正式进程。运行期间保持原始数据、冻结包、代码和环境稳定。发现哈希或身份冲突时，先按 [文件操作与存放规范](file_operations.md) 判定影响范围并联系 M0。测试产物使用 `test__` 标识，正式链只接正式清单。

## M0 协议、配置和环境冻结

**任务。** 核对现有冻结包的协议版本、运行范围、配置、表契约、敏感性矩阵、种子、环境和批准的偏离。本轮 M1 已使用本节指定的冻结包，直接读取即可。

```powershell
Get-Content -Raw -Encoding UTF8 "metadata/runs/$run/protocol_freeze.json"
Get-Content -Raw -Encoding UTF8 "metadata/runs/$run/m0_complete.json"
```

**验收。** `validation=passed`，`production_blockers` 为空，`release.boundary=released`。对照 [协议来源登记](protocol_sources.md) 和 `run_manifest.json`。站点、环流、PET 下限按各分支记录；其缺项由冻结的分析范围约束。当前冻结包已纳入获批的《模块化思路理解》来源修订。

**重新冻结时执行。** 由 M0 负责人备妥并审批本次候选配置。下例读入其提供的本地生产候选 JSON 路径；同目录应有三份配置，JSON 应引用有效的协议来源、输入清单和研究域证据。

```powershell
$candidate = Read-Host '输入 M0 负责人已审核的生产候选 JSON 路径'
& $python -B 'src/fdwt/stages/m00_protocol/freeze.py' --mode production --config $candidate --check-only
if ($LASTEXITCODE -ne 0) { throw 'M0 预检失败，退回负责人处理' }
& $python -B 'src/fdwt/stages/m00_protocol/freeze.py' --mode production --config $candidate --progress plain
```

当前 `config/existing_data_r1/m0_production.json` 为旧候选。交接前以它预检得到审批哈希阻塞，现有运行应从上面的正式冻结包接手。新冻结成功后，以终端输出的 `target` 更新 `$run`、`$m0` 和 `$frozen`。

## M1 原始数据审计与放行

**任务。** 审核 ERA5-Land 小时时轴、范围、原生变量和跨月累计语义，记录 MODIS 等产品的可用范围及缺项。交接时正式 M1 已启动，先等原终端结束。以下命令供确认原进程已退出后启动或续跑。

```powershell
& $python -B 'src/fdwt/stages/m01_source_audit/audit.py' --mode production --config $m0 --workers 2 --resume --progress panel
```

**产物与检查。** 成功后查看 `source_file_manifest.csv`、`source_variable_map.csv`、`m1_data_coverage.csv`、`m1_missing_files.csv`、`m1_release.json` 和 `m1_complete.json`。下面的检查通过后进入 M2。

```powershell
$m1Dir = "$frozen/m01_source_audit"
if (-not (Test-Path "$m1Dir/m1_complete.json")) { throw '正式 M1 清单尚未发布' }
$complete = Get-Content "$m1Dir/m1_complete.json" -Raw -Encoding UTF8 | ConvertFrom-Json
$release = Get-Content "$m1Dir/m1_release.json" -Raw -Encoding UTF8 | ConvertFrom-Json
if ($complete.validation -ne 'passed' -or $complete.run_id -ne $run -or
    $complete.identity.mode -ne 'production' -or
    $complete.identity.start -ne '2001-01-01' -or $complete.identity.end -ne '2023-12-31' -or
    $release.required_blocked -ne $false -or
    $release.release.era5_land_instant.status -ne 'passed' -or
    $release.release.era5_land_accumulated.status -ne 'passed') {
    throw 'ERA5-Land 必需分支尚未放行'
}
$release.release | ConvertTo-Json -Depth 6
```

同时核对两个必需分支覆盖 2001-01-01 至 2023-12-31，跨月累计样例通过。若未发布，查原终端显示的尝试目录，按 `m1_missing_files.csv` 的 `required=True`、文件和时次处理。历史问题包括 2018 年 5 月累计场曾出现 NetCDF HDF 读取错误，以及 2024-01-01 00 UTC 边界曾未通过。2023 年末短样例已通过后续复验，以本轮全量放行报告确认全期。发现必需文件仍异常时，保留文件名、错误、哈希和日志，交数据负责人按授权流程验收补档，再重跑 M1。可选分支的缺项登记影响范围。若读取预算超限，确认原进程退出后将 `--workers 2` 改为 `--workers 1`。

## M2 ERA5-Land 日值与候值

**输入与任务。** 读取同一运行的 M1 正式清单，按 UTC 从小时场构建日表、候表和静态格点表。

```powershell
& $python -B 'src/fdwt/stages/m02_preprocess/preprocess.py' --mode production --config $m0 --resume --progress panel
```

在 Windows Terminal 中直接运行上述命令即可显示实时面板。已经启动的 M2 进程沿用启动时的显示模式，可查看其 `run.jsonl` 跟踪进度。

**产物与检查。** 核对 `m2_complete.json` 和四张表 `time_pentad_calendar.parquet`、`grid_day_era5_land.parquet`、`grid_pentad_base_era5_land.parquet`、`grid_static_covariates.parquet`。核对 UTC 日历首尾、每年 73 候、全期共 1,679 候、闰年第 73 候 6 天和其余年第 73 候 5 天，以及原生域 13,741 格点。查 `m2_coverage.csv`、`m2_pentad73_audit.csv`、`m2_accumulation_tests.json`、`m2_sign_and_balance_tests.json` 和 `m2_area_conservation.csv`。抽查月末降水用次月首个 00 UTC 时次，核对降水、蒸散的符号和单位，检查候通量缺日时为空。地下径流缺项时，检查总径流和水量平衡保留缺失。

## M3 分析支撑与气候态

**输入与任务。** 用 M2 完整候表建立格点清单、掩膜、活跃窗和空间块，计算 2001–2023 基准期的 SMP 与降水阈值。

```powershell
& $python -B 'src/fdwt/stages/m03_support_climatology/run.py' --mode production --config $m0 --resume --progress panel
```

**产物与检查。** 核对 `m3_complete.json`、`grid_manifest.parquet`、`grid_mask.parquet`、`grid_active_pentad.parquet`、`grid_phenology_year.parquet`、`grid_climatology_era5land.parquet`、`grid_pentad_era5land__era5land.parquet`。从 `m3_climatology_support.json` 核对有效格点候和阈值可用范围。抽查活跃窗重叠至少一天的边界候、跨年的相邻候池、100 km 空间块，以及样本不足和 P95 无法使用的格点候。全期支撑和第 73 候敏感性须单独记录验收状态。

## M4 闪旱候选与全量事件库

**输入与任务。** 从 M3 可分析格点候识别闪旱，保存候选排除原因、格点事件与区域事件连接。

```powershell
& $python -B 'src/fdwt/stages/m04_event_catalog/run.py' --mode production --config $m0 --resume --progress panel
```

**产物与检查。** 核对 `m4_complete.json`、`event_candidate_audit.parquet`、`event_master_base.parquet`、`event_regional.parquet`、`event_regional_member.parquet` 和 `m4_event_sample_flow.csv`。从正常事件、低点并列、缺口、季末和研究期末分层抽查。复算起点至低点的降速和持续时间，检查并列最低取首候、缺口断开、右删失标记及区域四邻域成员。正式事件人工抽查签字后交 M5。

## M5 恢复轨迹与极端降水接续

**输入与任务。** 用 M3 候序列和 M4 事件计算恢复、P-term、F_R、Lag、湿过程和超级块。

```powershell
& $python -B 'src/fdwt/stages/m05_recovery_precip/run.py' --mode production --config $m0 --resume --progress panel
```

**产物与检查。** 核对 `m5_complete.json`、`event_master.parquet`、`event_precip.parquet`、`event_branch.parquet`、`event_recovery_trajectory.parquet`、`m5_window_completeness.csv` 和 `m5_branch_overlap.json`。以 M4 事件总数对账。抽查低点后首次 SMP 大于 20 的恢复候、RT 和 RR、P-term 的 `[t_min,t_e+2]`、F_R 的 `[t_e-1,t_e+1]`、首次超阈时次及 pre／sync／post 分类。检查窗不完整、阈值不可用和右删失的状态分别保留。FZ 和 DFAI 按冻结方案登记不可用。

## M6 前期 LAI 与深层水暴露

**输入与任务。** 用 M3 格点、M5 事件和本运行自动接入的 M1 MODIS 审计，做 MCD15A2H.061 QA、年合成及面积重投影，构建事件前暴露。独立试跑缺 MODIS 清单时，LAI 保留缺失。

```powershell
& $python -B 'src/fdwt/stages/m06_exposure/run.py' --mode production --config $m0 --resume --progress panel
```

**产物与检查。** 核对 `m6_complete.json`、`vegetation_grid_year_lai.parquet`、`event_exposure.parquet`、`exposure_cluster_audit.parquet` 和 `m6_support.json`。查看 `m6_exposure_missingness.csv` 与 `m6_leave_one_year_out.csv`。抽查 2008–2023 年事件的 LAI 窗口为事件年前五个完整年 `y-5…y-1`，主口径要求五年齐全。核对主／严格 QA、时空覆盖率、原生像元到格点面积和事件起始前四候深层水。真实单合成期已有样例证据，真实全年合成和全域资源仍须验收。NDVI 和 Q3 验证性模型按冻结状态登记。

## M7 路线一 Q1–Q3

**输入与任务。** 读取 M3、M5，若已发布则自动接入 M6。Q1 做恢复分布、趋势和风险集，Q2 做条件比例、Lag 和描述对比，Q3 做暴露支撑审计。建议按本任务书顺序先完成 M6，再发布本轮 M7，使完成清单包含完整血缘。若需先跑 Q1／Q2，应事先由运行负责人安排分阶段交付，避免在同一正式目录覆盖已发布的 M7。

```powershell
& $python -B 'src/fdwt/stages/m07_route1/run.py' --mode production --config $m0 --resume --progress panel
```

**产物与检查。** 核对 `m7_complete.json`、`route1_recovery_person_pentad.parquet`、`grid_year.parquet`、`q1_recovery_distribution.csv`、`q1_trend_result.csv`、`q1_recovery_risk.csv`、`q2_fr_conditional_proportion.csv`、`q2_lag_result.csv`、`q2_h1_contrast.csv` 和 `q3_exposure_support.json`。从 M5 复数 Q2 分子、分母、窗口未知和排除数。H1 主对比按 pre／sync 定义，post 单列；检查 Q1 的风险集从低点后的下一候开始、主随访 12 候、行政与缺测删失分列、趋势与 Holm 家族一致。Q2 调整对比和完整模型输出、双向聚类与循环位移列为待办。

## M8 路线二三出口

**输入与任务。** 读取 M3、M5，已发布的 M6 可纳入血缘。以事件起始候为时间原点，计算主随访 12 候及 6／18 候备选口径。

```powershell
& $python -B 'src/fdwt/stages/m08_route2/run.py' --mode production --config $m0 --resume --progress panel
```

**产物与检查。** 核对 `m8_complete.json`、`route2_person_pentad.parquet`、`route2_exit.parquet`、`route2_sample_audit.csv` 和 `route2_gate.json`。每个随访窗的 A、B、C、未知和删失计数之和应等于事件数。核查 B 少于 30 或有效块少于 10 时报告 `exit_model`，通过最低支持门槛时为 `descriptive_only` 并生成 `route2_exit_description.csv`。两种状态下 `model_allowed` 都应为 `false`。严格 post 重分类仅用于敏感性。

## M9 敏感性复算与适用范围

**输入与任务。** 读取 M3、M4、M5、M7、M8，可选 M6。按冻结规范矩阵执行现有实现支持的事件定义、阈值、趋势年限和随访敏感性，逐条登记其余规范。

```powershell
& $python -B 'src/fdwt/stages/m09_robustness/run.py' --mode production --config $m0 --resume --progress panel
```

**产物与检查。** 核对 `m9_complete.json`、`spec_result.csv` 和 `m9_robustness_report.json`。按 `spec_id` 去重，与冻结矩阵核对覆盖，因同一规范可输出多个指标。分开统计 `estimated`、`insufficient_support`、`pending_implementation` 和 `not_applicable`。检查已计算行的分母、口径和对应主结果。登记 50／200 km 等尚未实现的规范。现有产品比较还需可比较的上游候表和格点时间共同有效支持，原预注册四产品方案当前不可用。

## M10 独立复核、交付与归档

**输入与任务。** 读取 M3–M9 的正式清单。分层抽取事件独立复算，核对表契约、行数、Q2 分母、路线二出口、规范总数和协议偏离。

```powershell
& $python -B 'src/fdwt/stages/m10_review/run.py' --mode production --config $m0 --resume --progress panel
```

**产物与检查。** 核对 `m10_complete.json`、`review_event_recalc.csv`、`review_table_totals.csv`、`review_schema_validation.json`、`sample_flow.csv`、`missing_exclusion_unknown_summary.csv`、`review_deviation_audit.json`、`result_manifest.json` 和 `final_reproducibility_report.json`。抽样事件应复算通过，行数和分母应与上游清单及报告一致。失败退回对应责任阶段，记录影响范围后按冻结规则重跑。脚本生成的 `independent_signature` 和 `independent_reviewer` 为 `pending`，`ready_for_publication=false`。独立复核人另行签署，补齐论文图包、措辞审计及未完成分析的处置后，按批准范围发布。

**归档。** 交接记录包括 `run_id`、实际 Git 提交、环境指纹、各阶段 `attempt_id`、完成清单、复核证据、分支限制和未结事项。归档保留冻结配置与协议来源、原始数据清单、正式交接和结果、审计日志及必要的难以重取输入。另选独立存储做最小恢复，核对清单及代表性产物的哈希和可读性。

## 样例验证与交接台账

如需在正式运行之外验证脚本，测试目录使用单独尝试路径。M3–M10 的合成链和真实短样例的具体入口见 [M1–M2 运行说明](../workflow/README.md) 和 [M3–M10 运行说明](../workflow/downstream.md)。

```powershell
$qa = 'data/interim/qa_' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
& $python -B -m pytest -p no:cacheprovider -q --basetemp $qa
```

交接前的真实短样例使用 2023-12-27 至 2023-12-31。M1 六份文件通过，M2 发布四张表共 96,188 行，M3 三格点发布六张表。该记录对应本轮 M1 所用提交之前的代码，用于证明短链路曾跑通。正式验收以本轮各阶段实际清单为准。全域资源、真实全年 LAI、部分敏感性和论文发布仍有待办。

每结束一阶段，在本地交接记录追加一行，便于下次接手从最近的有效清单继续。

| 阶段 | 状态 | 运行标识与尝试标识 | 完成清单或失败日志 | 科学验收与待办 | 检查人／日期 |
|---|---|---|---|---|---|
| M0 | 已冻结 | 本任务书指定运行 | `metadata/runs/<run_id>/m0_complete.json` | 按现有数据方案执行 | |
| M1 | 正式运行已启动 | 同 M0，尝试标识见原终端 | 待运行结束后填写 | 累计场全期放行待核 | |
| M2–M10 | 待逐项记录 | | | | |
