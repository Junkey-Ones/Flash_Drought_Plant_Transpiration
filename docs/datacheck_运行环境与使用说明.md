# `datacheck` 运行环境与使用说明

> 文档版本：v1.0  
> 适用脚本：`script_processed/01_datacheck/check_structure.py`  
> 更新日期：2026-08-14

## 1. 目的

`01_datacheck`用于在粗裁（cropping）前，对原始数据集抽取代表性文件进行结构摸底。脚本只读取元数据和坐标信息，不读取科学变量的完整像元数组，不执行重采样、裁剪或数据质量订正。

当前抽样规则如下：

| 数据集 | 抽样规则 |
|---|---|
| ESA CCI | 目录排序后的最早、最晚NetCDF |
| GLEAM | 2000和2025年；SMs、SMrz、Et分别抽样 |
| ERA5 | 2001-01和2023-12；instant、accumulated分别抽样 |
| MODIS LAI | 优先固定`h26v05`瓦片的最早、最晚HDF |
| GOSIF | 最早、最晚`.tif.gz` |
| AI | 静态GeoTIFF唯一文件 |
| MCD12Q1 | 目录排序后的最早、最晚HDF |
| GMIA | ZIP内ASCII栅格头信息 |

## 2. 目录与环境

脚本位置：

```text
script_processed/
└── 01_datacheck/
    ├── check_structure.py
    ├── requirements.txt
    └── README.md
```

推荐环境：

- Windows或Linux
- Python 3.10及以上
- 可读项目目录和`data_raw`目录
- 输出目录必须位于`data_raw`之外

依赖安装：

```powershell
python -m pip install -r script_processed/01_datacheck/requirements.txt
```

当前已验证的读取后端：

| 包 | 版本 | 用途 |
|---|---:|---|
| xarray | 2026.7.0 | NetCDF结构、坐标和属性 |
| netCDF4 | 1.7.4 | NetCDF备用读取后端 |
| rasterio | 1.5.1 | GeoTIFF/GZip GeoTIFF结构 |
| pyhdf | 0.11.7 | MODIS HDF4科学数据集（SDS） |

验证环境：

```powershell
python -c "import xarray, netCDF4, rasterio; from pyhdf import SD; print('datacheck environment ready')"
```

## 3. 运行方式

### 3.1 全部抽样检查

在项目根目录运行：

```powershell
python script_processed/01_datacheck/check_structure.py
```

如果系统的`python`不在PATH中，可使用当前工作区的Python运行时：

```powershell
$py = "C:\Users\Junkey\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $py script_processed/01_datacheck/check_structure.py
```

### 3.2 指定数据集

```powershell
python script_processed/01_datacheck/check_structure.py --dataset esa_cci
python script_processed/01_datacheck/check_structure.py --dataset gleam --dataset era5
```

可用数据集名称：

```text
esa_cci, gleam, era5, modis_lai, gosif, ai, mcd12q1, gmia
```

### 3.3 指定输入和输出目录

```powershell
python script_processed/01_datacheck/check_structure.py `
  --data-root F:\Flash_Drought_Plant_Transpiration\data_raw `
  --output-dir F:\Flash_Drought_Plant_Transpiration\logs\datacheck_run_02
```

### 3.4 计算SHA-256

SHA-256默认关闭，以避免重复扫描大型GLEAM年度文件。需要校验和时显式启用：

```powershell
python script_processed/01_datacheck/check_structure.py --checksum
```

### 3.5 查看帮助

```powershell
python script_processed/01_datacheck/check_structure.py --help
```

## 4. 终端显示

脚本采用项目下载脚本的卡片式进度风格，参考：

- `script_download/cci_sm/download_cci_sm_passive_v092.py`
- `script_download/gleam/download_gleam_winscp.py`
- `script_download/era5_single_levels/download_era5_single_levels.py`
- `script_download/global_ai_pet_v3/download_global_ai_pet_v3.py`

交互式终端显示：

- 数据源和输出目录
- 当前样本文件和数据集分组
- 总体进度条、文件数和百分比
- PASS/WARN/FAIL数量
- 当前状态和已用时间

当stdout被重定向到日志或CI环境时，自动切换为无ANSI控制符的逐行进度，避免破坏日志可读性。

## 5. 输出文件

默认输出目录：

```text
logs/datacheck/
```

| 文件 | 内容 |
|---|---|
| `structure_check.csv` | 逐文件、逐变量的结构日志，适合Excel或后续脚本读取 |
| `structure_check.json` | 完整嵌套属性和机器可读结果 |
| `structure_summary.md` | 抽样文件、数据集统计和异常汇总 |
| `structure_check.log` | UTF-8时间戳日志 |

同一输出目录重复运行会覆盖上述四个报告。需要保留历史结果时，使用不同的`--output-dir`。

## 6. 只读与临时文件约束

- `data_raw`只以读取模式打开。
- 输出目录设置在`data_raw`内会被安全检查拒绝。
- GOSIF的`.gz`只解压到临时目录，检查结束后自动清理。
- GMIA直接读取ZIP内ASCII头信息，不把压缩包解压到原始数据目录。
- 脚本不执行裁剪、重投影、写回、删除或覆盖原始文件。

## 7. 退出码与故障处理

| 退出码 | 含义 |
|---:|---|
| 0 | 检查完成；没有FAIL记录 |
| 1 | 检查完成，但存在FAIL记录 |
| 2 | 参数、目录或只读安全检查失败 |

缺少可选读取后端时，脚本会在终端和CSV中给出WARN，不会伪造变量、shape或投影信息。正式检查前应按第2节安装依赖。

## 8. 已验证结果

2026-08-14使用项目现有数据完成一次全量抽样：20个样本文件、120条变量/结构记录，结果为`PASS=120、WARN=0、FAIL=0`。
