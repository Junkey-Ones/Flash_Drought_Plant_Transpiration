# MCD15A2H 下载器运行环境与使用说明

本文档对应脚本：

`script_download/modis/download_mcd15a2h.py`

下载器只负责从 NASA Earthdata 检索并保存 MCD15A2H V061 原始 HDF 文件，不执行空间裁剪、QA 筛选、时间聚合、单位转换或重采样。

## 1. 研究区与默认下载范围

当前研究区已经统一为：

- 经度：`102.0–115.0°E`
- 纬度：`33.5–41.5°N`
- MODIS 瓦片：`h25v04、h25v05、h26v04、h26v05、h27v05`
- 默认时间：`2002-01-01–2023-12-31`

MCD15A2H 产品实际从 2002 年开始，因此 2001 年没有对应 LAI 文件。若需要其他时间范围，可以通过 `--start` 和 `--end` 覆盖默认值。

## 2. 文件、日志和元数据位置

| 内容 | 位置 | 说明 |
|---|---|---|
| 下载好的原始 HDF | `data_raw/modis/MCD15A2H/V061/` | 保持 NASA 原始文件名和 HDF-EOS2 格式 |
| 下载日志 | `logs/download/modis/download_mcd15a2h.log` | 搜索数量、瓦片分布、跳过、完成、失败和重试信息 |
| 失败文件列表 | `logs/download/modis/failed_mcd15a2h.txt` | 只有存在失败或未完成文件时生成 |
| 断点文件 | `data_raw/modis/MCD15A2H/V061/*.hdf.filepart` | 下载中断时保留，重新运行可续传 |
| 结构检查元数据 | `logs/datacheck/structure_check.csv`、`structure_check.json` | 抽样文件的结构、变量、维度、单位、FillValue 和 QA 字段检查结果 |

需要注意：当前下载器不会把 CMR 返回的完整 UMM 元数据单独保存为 JSON。CMR 元数据只在检索过程中用于提取文件名、瓦片、下载链接和文件大小；下载完成后，产品自身的科学元数据仍保存在 HDF 文件内部。若以后需要保存完整 CMR 元数据，应另行增加 manifest 输出，不应把它与 `structure_check.json` 混淆。

## 3. 运行环境

项目推荐使用本地虚拟环境：

```powershell
& .\.venv\Scripts\python.exe --version
```

安装下载器依赖：

```powershell
& .\.venv\Scripts\python.exe -m pip install earthaccess requests
```

检查依赖是否可导入：

```powershell
& .\.venv\Scripts\python.exe -c "import earthaccess, requests; print('download dependencies: OK')"
```

NASA Earthdata 账号需要具备数据访问权限。凭据建议由 `earthaccess` 使用交互式登录或用户目录中的 `.netrc` 读取，不要写入项目脚本、日志或 Git 仓库。

## 4. 使用方法

脚本命令均应从项目根目录执行。

### 4.1 查看参数

```powershell
& .\.venv\Scripts\python.exe script_download\modis\download_mcd15a2h.py --help
```

### 4.2 只补充缺少的 h27v05

建议先进行检索预览。`--dry-run` 仍需要登录 Earthdata 和访问 CMR，但不会下载 HDF 文件：

```powershell
& .\.venv\Scripts\python.exe script_download\modis\download_mcd15a2h.py `
  --tiles h27v05 `
  --start 2002-01-01 `
  --end 2023-12-31 `
  --dry-run
```

确认检索结果后正式下载：

```powershell
& .\.venv\Scripts\python.exe script_download\modis\download_mcd15a2h.py `
  --tiles h27v05 `
  --start 2002-01-01 `
  --end 2023-12-31 `
  --workers 4
```

### 4.3 下载全部目标瓦片

不指定 `--tiles` 时使用默认的五个瓦片；已有完整文件会自动跳过：

```powershell
& .\.venv\Scripts\python.exe script_download\modis\download_mcd15a2h.py `
  --start 2002-01-01 `
  --end 2023-12-31 `
  --workers 4
```

### 4.4 检查 CMR 结果中的瓦片分布

```powershell
& .\.venv\Scripts\python.exe script_download\modis\download_mcd15a2h.py `
  --start 2002-01-01 `
  --end 2023-12-31 `
  --list-tiles
```

### 4.5 检查本地文件

```powershell
& .\.venv\Scripts\python.exe script_download\modis\download_mcd15a2h.py `
  --tiles h27v05 `
  --start 2002-01-01 `
  --end 2023-12-31 `
  --check-only
```

### 4.6 覆盖已有文件

只有确认本地文件损坏或需要重新下载时才使用：

```powershell
& .\.venv\Scripts\python.exe script_download\modis\download_mcd15a2h.py `
  --tiles h27v05 `
  --start 2002-01-01 `
  --end 2023-12-31 `
  --overwrite
```

## 5. 中断、失败与恢复

- 交互式 PowerShell 下载时按 `Q` 或 `q` 可安全终止；卡片会停止派发新任务，并保留正在下载的 `.filepart`，脚本返回退出码 `130`。
- `Ctrl+C` 是备用中断方式；非TTY、管道或 CI 环境不监听 `Q`，请使用 `Ctrl+C`。
- 下载中断：保留 `.filepart`，重新运行相同命令即可继续。
- 服务器不支持断点：脚本会记录警告并从头下载。
- 文件大小校验失败：文件会保留为 `.filepart`，日志中记录失败原因。
- 已存在但大小不匹配的最终文件：默认不会覆盖，需要显式使用 `--overwrite`。
- 失败文件名会写入 `logs/download/modis/failed_mcd15a2h.txt`，可根据该列表复核。

## 6. 下载后的下一步

下载完成后，先运行结构检查：

```powershell
& .\.venv\Scripts\python.exe script_processed\01_datacheck\check_structure.py --dataset modis_lai
```

结构检查通过后，再进入空间粗剪。粗剪阶段应保留 HDF 原生投影、原始分辨率、LAI/FPAR 数组和 QA 数组，不在下载器中提前修改原始文件。
