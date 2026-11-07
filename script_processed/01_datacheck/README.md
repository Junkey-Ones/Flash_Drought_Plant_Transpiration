# 01_datacheck 抽样结构检查

脚本入口：`check_structure.py`。脚本只读取`data_raw`，用于粗裁前确认变量、维度、坐标、投影、单位、FillValue、scale_factor和QA字段；不会修改原始数据。

## 运行

```powershell
& .\.venv\Scripts\python.exe script_processed\01_datacheck\check_structure.py
```

指定数据集或输出目录：

```powershell
& .\.venv\Scripts\python.exe script_processed\01_datacheck\check_structure.py --dataset esa_cci
& .\.venv\Scripts\python.exe script_processed\01_datacheck\check_structure.py --output-dir logs\datacheck_run_02
```

依赖安装：

```powershell
& .\.venv\Scripts\python.exe -m pip install -r script_processed\01_datacheck\requirements.txt
```

默认报告位于`logs/datacheck/`：`structure_check.csv`、`structure_check.json`、`structure_summary.md`和`structure_check.log`。

完整运行环境与项目规范见：

- `docs/01_datacheck_运行环境与使用说明.md`
- `docs/项目脚本开发规范.md`

首次配置也可以执行：

```powershell
& .\script_processed\01_datacheck\setup_environment.ps1
```
