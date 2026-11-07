# M3–M10 下游阶段

采用当前 M0 冻结的现有数据方案。主链使用 ERA5-Land 原生主网格、2001–2023 基准期及固定 4–9 月活跃窗。候与活跃窗重叠至少一天即纳入。100 km 方块以 EPSG:6933 投影原点为锚点。

## 独立入口和上游清单

每个阶段在自己的目录运行。工作流只负责参数、清单、资源预检、日志和发布。正式模式默认从同一个冻结运行查找上游，试跑模式用可重复的 `--input` 参数逐个指定完成清单。

| 阶段 | 独立入口 | 必需清单 | 可选清单 |
|---|---|---|---|
| M3 | `src/fdwt/stages/m03_support_climatology/run.py` | M2 | |
| M4 | `src/fdwt/stages/m04_event_catalog/run.py` | M3 | |
| M5 | `src/fdwt/stages/m05_recovery_precip/run.py` | M3、M4 | |
| M6 | `src/fdwt/stages/m06_exposure/run.py` | M3、M5 | M1 MODIS 审计 |
| M7 | `src/fdwt/stages/m07_route1/run.py` | M3、M5 | M6 |
| M8 | `src/fdwt/stages/m08_route2/run.py` | M3、M5 | M6 |
| M9 | `src/fdwt/stages/m09_robustness/run.py` | M3、M4、M5、M7、M8 | M6 |
| M10 | `src/fdwt/stages/m10_review/run.py` | M3–M9 | |

M6 的 MODIS 测试清单可与 M2 的 ERA5-Land 测试审计分开生成，二者须对应同一 M0 冻结包。未提供 MODIS 时，LAI 年值及暴露均保留缺失状态。

以下示例从项目根目录执行。真实短样例用于验证文件格式、交接和路径，完整基准期才具备计算主气候态的条件。

~~~powershell
$env:PYTHONIOENCODING = 'utf-8'
$python = '.\.venv\Scripts\python.exe'
$m0 = 'metadata/runs/v1.3.1__ee0d9fb46eb2__561932e2cedf__seed_13120261102/run_config.json'
$m2 = 'data/frozen/test__v1.3.1__ee0d9fb46eb2__561932e2cedf__seed_13120261102__m2_b6878bfaee3a/m02_preprocess/m2_complete.json'
$m3 = 'src/fdwt/stages/m03_support_climatology/run.py'
& $python -B $m3 --help
& $python -B $m3 --mode test --config $m0 --input $m2 --max-cells 3 --dry-run
& $python -B $m3 --mode test --config $m0 --input $m2 --max-cells 3 --progress panel
# 正式阶段须通过准备验收，使用与参与代码、环境一致的新冻结配置。
& $python -B $m3 --mode production --config $m0 --dry-run
~~~

各阶段沿用上游含端点的日期范围。M3 的 `--max-cells` 只限制试跑输出格点，不截断候历史，`--chunk-cells` 控制每块格点数。目前各阶段使用一个工作单元。已发布的交接位于 `data/frozen/<run_id>/<stage>/`，以 `mN_complete.json` 及文件指纹为准。阶段报告位于 `outputs/<run_id>/<category>/<stage>/<attempt_id>/`。用同样的参数加 `--resume` 可复验并复用交接。原始文件和上游产物按只读方式打开。

## 终端面板和运行结果

在 Windows Terminal 或支持 ANSI 的交互终端直接运行命令。默认 `--progress auto` 会选择 Rich Live，`--progress panel` 可要求必须显示面板。中文标题下方显示模式、产品和日期。流程进度按校验输入、执行阶段、验收发布计数。上游校验和格点处理等任务有实际总量时，另显示任务进度和计数。有足够计时样本时显示当前任务的处理速度和预计剩余时间。可用 Ctrl+C 安全中断。

结束后，终端恢复普通显示并保留摘要。完成或复用时列出完成清单、交接表行数、结果目录和实际的 `run.jsonl` 路径。失败时列出原因和已创建的尝试目录。`--check-only` 只读校验，`--dry-run` 只预览范围和预计路径。

面板使用标准错误，只重定向标准输出时仍可原地刷新，标准输出保留 JSON 运行计划。若标准错误也被捕获或不支持 ANSI，自动模式只留下结束摘要，显式面板模式会报告原因。`--progress plain` 用于需要逐行进度的批处理。项目当前环境使用锁定的 pip 自带 Rich，无需运行期间联网安装。

## 已实现范围和待验收内容

| 阶段 | 当前实现 | 放行限制和待补内容 |
|---|---|---|
| M3 | 固定活跃窗、完整格点历史、独立 SMP、P95/P99/湿候 P95，缺测分别记录 | 第 73 候完整阈值敏感性和全域资源验收待补 |
| M4 | 主事件、候选排除审计、区域四邻域连接、右删失 | 正式事件人工抽查待签 |
| M5 | 恢复、P-term、F_R、Lag、轨迹和湿过程 | FZ/DFAI 按 M0 冻结状态记为不可用 |
| M6 | MCD15A2H 主及严格 QA、原生合成、五年 LAI、深浅层水及暴露簇 | 真实年合成和面积重投影仍需验收，NDVI 未放行 |
| M7 | 条件 A 分布、超级块区间、风险集和 cloglog 时间基线、Q1 趋势和 Holm、Q2 比例及描述对比、格点年汇总 | Q2 调整对比和完整模型输出、双向聚类及循环位移待补。Q3 按 M0 只做支撑审计 |
| M8 | 主随访和备选随访出口、未知类型与删失、退出门槛 | 按 M0 只允许条件描述 |
| M9 | 冻结规范清点、事件及阈值定义复算、路线一随访和路线二出口描述敏感性 | 50/200 km 等未完成的规范显式标记。现有产品的共同支撑诊断仍需可比较的上游候表 |
| M10 | 分层事件复算、表契约与总数、Q2 分母、路线二出口、规范数量、偏离记录和样本流 | 正式独立签署、论文图包及完整措辞审计待补 |

在完整验收之前，阶段完成清单只表明该次交接通过实现中的结构和一致性校验。论文推断及最终发布仍按 M0 放行记录和独立复核执行。

## 样例验收记录

- 八个阶段的独立入口均检查了 `--help` 和无效参数。重定向输出验证了逐行进度、最终状态和日志。
- 全仓测试 39 项通过。其中三格点、2001–2023 合成样本贯通 M3–M10，并检查干跑、中断后重试、清单续跑和上游篡改拒绝。
- ERA5-Land 真实短样例处理 2004-02-27 至 2004-02-29，M2 读取 2004-02-25 起的完整候。M3 使用 3 个格点，完成清单对应测试运行 `test__v1.3.1__ee0d9fb46eb2__561932e2cedf__seed_13120261102__m3_a73361915f54`。实际耗时 0.81 秒，峰值常驻内存约 137 MiB，交接文件 42,554 字节，`--resume` 复验通过。基准样本不足，三个格点候的 SMP 和可用 P95 均为空。
- 真实 MODIS 瓦片 `MCD15A2H.A2004217.h26v05.061.2020195173807.hdf` 通过 HDF4 条带读取和主／严格 QA 检查。单合成期面积重投影得到 6,860 个有覆盖格点，最大格点面积比例约 1.0000。全年合成和全域资源尚未验收。
- 中文面板用 Windows ConPTY 运行真实 ERA5-Land 2023 年末候，抽取 128 个格点。验证了多帧原地刷新、流程与任务进度、退出面板和中文结果摘要。运行 `test__v1.3.1__ee0d9fb46eb2__561932e2cedf__seed_13120261102__m3_a9709f45eb3b` 发布 6 张表，共 9,984 行。处理耗时约 1.05 秒，峰值常驻内存约 139 MiB，交接文件 208,831 字节。中文预览及纯文本续跑复验通过。注入中断信号的测试确认退出码为 130，终端恢复并显示尝试目录和日志路径。

在 Windows 沙箱中用项目盘短临时路径运行合成测试，避免默认临时目录触及路径长度限制。每次运行生成新的临时目录。

~~~powershell
$qa = 'data/interim/qa_' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
& $python -B -m pytest -q --basetemp $qa
~~~
