# Flash Drought Plant Transpiration

## 项目简介

本项目研究**骤发干旱（Flash Drought）对植物蒸腾的影响**，聚焦于太行山-黄土高原区域（约 102°E-115°E, 33.5°N-41.5°N）。

项目涵盖从多源遥感与再分析数据的自动化下载、预处理到统计分析的完整工作流，时间跨度为 2000-2025 年。

---

## 目录结构

```
Flash_Drought_Plant_Transpiration/
│
├── data_raw/                        # 原始数据（从各数据源下载）
│   │
│   ├── aridity/                     # 干旱指数数据
│   │   └── global_ai_pet_v3/        # Global Aridity Index v3.1（Figshare）
│   │       └── Global-AI_ET0__annual_v3_1/
│   │           └── ai_v31_yr.tif    # 年均干旱度栅格（1970-2000 平均）
│   │
│   ├── era5_single_levels/          # ERA5 再分析数据（ECMWF CDS）
│   │   ├── accumulated/             # 累积量：降水、辐射、蒸发等
│   │   └── instant/                 # 瞬时量：温度、露点等
│   │   # 变量：2m温度、露点温度、降水、向下短波辐射、潜在蒸发、实际蒸发
│   │
│   ├── esa_cci_sm/                  # ESA CCI 土壤湿度（CEDA）
│   │   └── v09.2/                   # 被动微波日尺度产品
│   │
│   ├── gleam/                       # GLEAM 蒸散发模型输出（SFTP）
│   │   ├── Et/                      # 实际蒸散发 (mm/day)
│   │   ├── SMs/                     # 表层土壤湿度 (m³/m³)
│   │   └── SMrz/                    # 根区土壤湿度 (m³/m³)
│   │   # 版本：v4.3a，年份：2000-2025
│   │
│   ├── gosif/                       # GOSIF 日光诱导叶绿素荧光
│   │   └── v2/                      # 8 天合成，0.05° 空间分辨率
│   │   # 文件：GOSIF_YYYYDDD.tif.gz，年份：2000-2024
│   │
│   ├── mask/                        # 掩膜数据
│   │   └── gmia_v5/                 # 全球灌溉面积掩膜 v5
│   │
│   └── modis/                       # MODIS 陆地产品（NASA LP DAAC）
│       ├── MCD12Q1/                 # 土地覆盖类型（IGBP 分类）
│       │   └── V061/
│       └── MCD15A2H/                # LAI/FPAR（叶面积指数/光合有效辐射吸收比例）
│           └── V061/
│           # 空间范围：h25v04, h25v05, h26v04, h26v05（覆盖研究区）
│
├── data_processed/                  # 处理后的数据（脚本输出）
│
├── data_analysis/                   # 统计分析结果与图表
│
├── script_download/                 # 数据下载脚本
│   │
│   ├── cci_sm/                      # ESA CCI 土壤湿度下载器
│   │   └── download_cci_sm_passive_v092.py
│   │   # 数据源：CEDA（data.ceda.ac.uk）
│   │   # 特点：JSON 目录自动发现、断点续传、并发下载
│   │
│   ├── era5_single_levels/          # ERA5 单层数据下载器
│   │   └── download_era5_single_levels.py
│   │   # 数据源：Copernicus CDS（cdsapi）
│   │   # 特点：按月下载、支持并发、实时进度面板
│   │
│   ├── gleam/                       # GLEAM 数据下载器
│   │   ├── download_gleam_winscp.py  # WinSCP SFTP 并行下载
│   │   ├── browse_gleam_interactive.py # 交互式浏览 GLEAM 文件
│   │   └── speed_test.py             # 下载速度测试
│   │   # 数据源：aether.ugent.be（SFTP）
│   │
│   ├── global_ai_pet_v3/            # 干燥度指数下载器
│   │   └── download_global_ai_pet_v3.py
│   │   # 数据源：Figshare API
│   │   # 特点：自动识别年度 AI 文件、MD5 校验
│   │
│   ├── gosif/                       # GOSIF 荧光数据下载器
│   │   ├── download_gosif.py         # 完整版（含断点续传）
│   │   ├── download_gosif_simple.py  # 简化版
│   │   └── verify_gosif.py           # 下载完整性校验
│   │   # 数据源：data.globalecology.unh.edu
│   │
│   ├── modis/                       # MODIS 产品下载器
│   │   ├── download_mcd12q1.py       # 土地覆盖类型
│   │   └── download_mcd15a2h.py      # LAI/FPAR
│   │   # 数据源：NASA LP DAAC（earthaccess）
│   │   # 认证：NASA Earthdata Login（~/.netrc）
│   │
│   └── config/                      # 下载配置文件
│
├── script_processed/                # 数据预处理脚本（待补充）
│
├── scropt_analysis/                 # 统计分析脚本（待补充）
│   # 注：目录名 scropt_analysis 为拼写错误，应为 script_analysis
│
├── logs/                            # 运行日志
│   └── download/                    # 下载日志（按数据源分类）
│
└── docs/                            # 项目文档
    └── MCD15A2H_下载环境与使用说明.md
```

---

## 数据源说明

| 数据集 | 变量 | 时间分辨率 | 空间分辨率 | 时间范围 | 数据源 |
|--------|------|-----------|-----------|---------|--------|
| ERA5 | 温度、降水、辐射、蒸发 | 小时 | 0.25° | 2001-2024 | Copernicus CDS |
| GLEAM v4.3a | 蒸散发、土壤湿度 | 日 | 0.25° | 2000-2025 | UGent SFTP |
| GOSIF v2 | 日光诱导荧光 | 8 天 | 0.05° | 2000-2024 | UNH |
| ESA CCI SM | 土壤湿度 | 日 | 0.25° | 2001-2024 | CEDA |
| MODIS MCD15A2H | LAI / FPAR | 8 天 | 500 m | 2002-2023（默认下载范围） | NASA LP DAAC |
| MODIS MCD12Q1 | 土地覆盖类型 | 年 | 500 m | 2001-2022 | NASA LP DAAC |
| Global AI v3.1 | 干旱度指数 | 多年平均 | ~1 km | 1970-2000 | Figshare |

---

## 快速开始

### 环境要求

- Python 3.10+
- 依赖包见各下载脚本顶部注释

### 典型依赖安装

```bash
pip install cdsapi earthaccess requests tqdm paramiko
```

### 运行下载脚本

```bash
# ERA5 数据（需要 CDS API key）
cd script_download/era5_single_levels
python download_era5_single_levels.py --start 2001-01 --end 2024-12 --workers 4

# GLEAM 数据（需要 SFTP 密码）
cd script_download/gleam
python download_gleam_winscp.py

# MODIS LAI/FPAR（需要 NASA Earthdata 账号；详见 docs/MCD15A2H_下载环境与使用说明.md）
cd script_download/modis
python download_mcd15a2h.py --start 2002-01-01 --end 2023-12-31

# ESA CCI 土壤湿度
cd script_download/cci_sm
python download_cci_sm_passive_v092.py

# GOSIF 荧光数据
cd script_download/gosif
python download_gosif.py

# 干燥度指数（一次性）
cd script_download/global_ai_pet_v3
python download_global_ai_pet_v3.py
```

---

## 研究区范围

- **经度**：102°E - 115°E
- **纬度**：33.5°N - 41.5°N
- **主要地貌**：太行山脉、黄土高原
- **MODIS Tiles**：h25v04, h25v05, h26v04, h26v05

---

## 下载脚本特性

所有下载脚本均具备以下功能：

- ✅ **断点续传**：中断后重新运行自动从断点继续
- ✅ **并发下载**：可配置并发数（默认 4-6 线程）
- ✅ **实时进度**：终端进度面板（速度、ETA、文件大小）
- ✅ **自动跳过**：已完整下载的文件自动跳过
- ✅ **日志记录**：详细日志保存至 `logs/download/` 目录
- ✅ **大小校验**：下载完成后校验文件完整性

---

## 注意事项

1. **数据下载脚本包含认证信息**（CDS API key、SFTP 密码等），请勿公开分享此仓库
2. **NASA Earthdata** 需要注册并在 `~/.netrc` 中配置凭据
3. **存储空间需求**：全部原始数据约需 500GB+ 磁盘空间
4. **scropt_analysis** 目录名存在拼写错误（应为 `script_analysis`）

---

## 许可证

[待补充]

## 联系方式

[待补充]
