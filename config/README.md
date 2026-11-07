# 研究配置

M0 在此创建并审核 `params.yaml`、`schema_registry.yaml` 和 `specification_matrix.yaml`。正式运行将实际采用的配置复制到对应的运行元数据目录。参数来源和冻结流程见[文件操作与存放规范](../docs/file_operations.md)。

## M0 本地试跑

从项目根目录运行。候选配置可采用 JSON 形式的 YAML，文件名保留 `.yaml`。运行配置与输入清单留在本地。

```powershell
.\.venv\Scripts\python.exe -B src/fdwt/stages/m00_protocol/freeze.py --mode test --config config/m0_test.json --check-only
.\.venv\Scripts\python.exe -B src/fdwt/stages/m00_protocol/freeze.py --mode test --config config/m0_test.json --progress plain
```

运行配置包括 `mode`、`protocol_dir`、`input_manifest`、`approval`、`external` 和 `deviations`。输入清单的 `mode` 必须与入口匹配。相对输入路径以运行配置所在目录解析。三份配置文件与运行配置同目录。

正式模式要求研究区矢量证据包含 `source`、`version`、`path` 和 `sha256`。站点、环流和 PET 下限证据按各自分支登记，PET 下限还须有正值 `value`。负责人审批记录包含 `reviewer`、`date` 和 `reviewed`，其中 `reviewed` 记录 `config_hashes`、`protocol_hashes` 和 `environment_hash`。种子、表契约和规范矩阵经审核后标为 `frozen`。待处理的协议差异记录在 `deviations`，审核完成后附审批人及重跑范围。

完整的候选配置及测试输入位于本机 `config/`。当前部分字段类型仍标为 `pending_review`，规范矩阵按单个定义族逐项替换构建，正式规范总数待审核。测试产物在 `metadata/runs/test__*/`，验收依据为该运行下的 `m0_complete.json`。

## 现有数据修订版

现有数据方案使用 `existing_data_era5land_grid_r1`，本地配置位于 `config/existing_data_r1/`。研究域由 ERA5-Land 原生格点中心定义，包含 100°–115°E、33°–42°N 两端格点。对外称为黄土高原及邻近区。冻结包保存网格定义及其来源文件校验值。

事件与气候基准期均为 2001–2023，采用 4–9 月固定活跃季和数据质控掩膜。MCD15A2H.061 仅用于现有五年 LAI 暴露支撑审计。原 Q3 验证性模型、完整水量平衡分支和原四产品复算仍缺必需输入。路线二按观察窗及样本支持度做条件描述。具体偏离随运行归档。

```powershell
.\.venv\Scripts\python.exe -B src/fdwt/stages/m00_protocol/freeze.py --mode production --config config/existing_data_r1/m0_production.json --check-only
```
