"""Sample-based, read-only structure inspection for the project's raw datasets.

The checker intentionally inspects only two representative time slices per
dynamic dataset.  It never writes below ``data_raw`` and never extracts or
rewrites source files.  Reports are written to ``logs/datacheck`` by default.

Optional readers
----------------
NetCDF: xarray (preferred) or netCDF4
GeoTIFF: rasterio (preferred) or osgeo.gdal
MODIS HDF4: pyhdf or osgeo.gdal

Examples (PowerShell)
---------------------
    python script_processed/01_datacheck/check_structure.py
    python script_processed/01_datacheck/check_structure.py --dataset esa_cci
    python script_processed/01_datacheck/check_structure.py --output-dir logs/my_check

The command prints each action and writes:
    structure_check.csv     one row per sampled file/variable
    structure_check.json    full machine-readable records
    structure_summary.md    dataset-level summary and warnings
    structure_check.log     timestamped terminal-equivalent log
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data_raw"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "logs" / "datacheck"

DATASET_ORDER = [
    "esa_cci",
    "gleam",
    "era5",
    "modis_lai",
    "gosif",
    "ai",
    "mcd12q1",
    "gmia",
]

CSV_FIELDS = [
    "dataset",
    "sample_group",
    "filename",
    "path",
    "format",
    "file_size_bytes",
    "sha256",
    "dimensions",
    "variables",
    "variable",
    "shape",
    "dtype",
    "time_range",
    "time_interval",
    "coordinate_range",
    "spatial_resolution",
    "crs_projection",
    "units",
    "fill_value",
    "scale_factor",
    "add_offset",
    "qa_variables",
    "source_subdataset",
    "status",
    "message",
]


def _json(value: Any) -> Any:
    """Convert numpy/scalar/path values into JSON-safe values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    try:
        return value.item()
    except Exception:
        return str(value)


def fmt(value: Any) -> str:
    """Stable compact representation for CSV/log output."""
    value = _json(value)
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def log_line(logger: logging.Logger, level: str, message: str) -> None:
    prefix = {"INFO": "[INFO]", "OK": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]"}.get(level, "[INFO]")
    getattr(logger, "warning" if level == "WARN" else "error" if level == "FAIL" else "info")(f"{prefix} {message}")


def enable_ansi() -> None:
    """Enable VT100 sequences on Windows terminals when possible."""
    if os.name != "nt":
        return
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        # A redirected/non-standard console can still use the plain-text
        # fallback below.
        pass


class ProgressDisplay:
    """Card-style terminal progress display aligned with download scripts.

    In an interactive terminal the card is redrawn in place using ANSI
    sequences.  When stdout is redirected, one concise ASCII line is emitted
    per sample so CI logs remain readable and do not contain cursor control
    codes.
    """

    WIDTH = 86
    BAR_WIDTH = 28
    COLORS = {
        "clear": "\033[2K", "up": "\033[A", "hide": "\033[?25l", "show": "\033[?25h",
        "bold": "\033[1m", "dim": "\033[2m", "green": "\033[32m", "cyan": "\033[36m",
        "yellow": "\033[33m", "red": "\033[31m", "white": "\033[97m", "reset": "\033[0m",
    }

    def __init__(self, total: int, data_root: Path, output_dir: Path) -> None:
        self.total = max(1, total)
        self.data_root = data_root
        self.output_dir = output_dir
        self.started = time.monotonic()
        self.rendered_lines = 0
        self.interactive = bool(getattr(sys.stdout, "isatty", lambda: False)())
        enable_ansi()

    @classmethod
    def visible_width(cls, text: str) -> int:
        width = 0
        index = 0
        while index < len(text):
            if text[index] == "\033":
                end = text.find("m", index)
                index = len(text) if end < 0 else end + 1
                continue
            if not unicodedata.combining(text[index]):
                width += 2 if unicodedata.east_asian_width(text[index]) in "WF" else 1
            index += 1
        return width

    @classmethod
    def pad(cls, text: str, width: int) -> str:
        return text + " " * max(0, width - cls.visible_width(text))

    @classmethod
    def content_line(cls, text: str = "") -> str:
        return f"│ {cls.pad(text, cls.WIDTH - 1)}│"

    @classmethod
    def bar(cls, percent: float) -> str:
        c = cls.COLORS
        filled = round(cls.BAR_WIDTH * max(0.0, min(percent, 100.0)) / 100)
        return f"{c['green']}{'█' * filled}{'░' * (cls.BAR_WIDTH - filled)}{c['reset']}"

    @staticmethod
    def short(text: str, width: int) -> str:
        text = str(text)
        # Use ASCII ellipsis so redirected Windows output remains encodable
        # under legacy code pages.
        return text if len(text) <= width else f"...{text[-width + 3:]}"

    @staticmethod
    def elapsed(seconds: float) -> str:
        seconds = max(0, int(seconds))
        if seconds < 60:
            return f"{seconds}s"
        return f"{seconds // 60}m {seconds % 60:02d}s"

    def _write_card(self, lines: list[str]) -> None:
        c = self.COLORS
        if self.interactive:
            if self.rendered_lines:
                sys.stdout.write(c["up"] * self.rendered_lines)
            for line in lines:
                sys.stdout.write(f"{c['clear']}{line}\n")
            sys.stdout.flush()
            self.rendered_lines = len(lines)
        else:
            # Pipes and CI logs should not receive cursor movement or ANSI.
            plain = re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", " | ".join(line.strip("│ ") for line in lines[3:-1]))
            plain = plain.replace("█", "#").replace("░", "-")
            print(f"[PROGRESS] {plain}", flush=True)

    def render(self, index: int, current: str, status: str, counts: dict[str, int], detail: str = "") -> None:
        c = self.COLORS
        percent = min(100.0, max(0.0, index / self.total * 100))
        elapsed = max(0.001, time.monotonic() - self.started)
        status_colors = {"PASS": c["green"], "WARN": c["yellow"], "FAIL": c["red"], "RUN": c["cyan"], "START": c["cyan"]}
        status_text = f"{status_colors.get(status, c['white'])}{status}{c['reset']}"
        detail = self.short(detail, 62)
        lines = [
            f"┌{'─' * self.WIDTH}┐",
            self.content_line(f"{c['bold']}{c['white']} DATASET STRUCTURE CHECK | READ-ONLY {c['reset']}"),
            f"├{'─' * self.WIDTH}┤",
            self.content_line(f"{c['cyan']}Source:{c['reset']} {self.short(self.data_root, 68)}"),
            self.content_line(f"{c['cyan']}Output:{c['reset']} {self.short(self.output_dir, 68)}"),
            self.content_line(f"{c['yellow']}Progress:{c['reset']} {self.bar(percent)} {percent:5.1f}%  files {index}/{self.total}"),
            self.content_line(f"{c['cyan']}Current:{c['reset']} {self.short(current, 68)}"),
            self.content_line(f"{c['yellow']}Status:{c['reset']} {status_text}  pass={counts.get('PASS', 0)} warn={counts.get('WARN', 0)} fail={counts.get('FAIL', 0)}"),
            self.content_line(f"{c['dim']}Elapsed:{c['reset']} {self.elapsed(elapsed)}  {detail}"),
            f"└{'─' * self.WIDTH}┘",
        ]
        self._write_card(lines)

    def finish(self) -> None:
        if self.interactive:
            sys.stdout.write(self.COLORS["show"])
            sys.stdout.flush()


def setup_logger(output_dir: Path, verbose: bool) -> logging.Logger:
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("structure_check")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(output_dir / "structure_check.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    # Do not write ordinary log lines over the live progress card.  The card
    # owns the terminal; all timestamped details remain in the UTF-8 log file.
    return logger


def ensure_read_only_scope(data_root: Path, output_dir: Path) -> tuple[Path, Path]:
    data_root = data_root.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not data_root.is_dir():
        raise FileNotFoundError(f"原始数据目录不存在: {data_root}")
    try:
        output_dir.relative_to(data_root)
    except ValueError:
        return data_root, output_dir
    raise ValueError(f"output directory must be outside data_raw (read-only safety guard): {output_dir}")


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def samples_for(data_root: Path, selected: set[str]) -> list[dict[str, Any]]:
    """Return deterministic first/last samples without touching source files."""
    result: list[dict[str, Any]] = []

    def add(dataset: str, group: str, paths: Iterable[Path]) -> None:
        if selected and dataset not in selected:
            return
        files = sorted({p.resolve() for p in paths if p.is_file()}, key=lambda p: p.name)
        if not files:
            result.append({"dataset": dataset, "group": group, "path": None, "missing": True})
            return
        chosen = [files[0]] if len(files) == 1 else [files[0], files[-1]]
        for index, path in enumerate(chosen, 1):
            result.append({"dataset": dataset, "group": group, "path": path, "sample": "first" if index == 1 else "last"})

    add("esa_cci", "daily_netcdf", (data_root / "esa_cci_sm" / "v09.2").glob("*.nc"))
    for variable in ("SMs", "SMrz", "Et"):
        add("gleam", variable, (data_root / "gleam" / variable / "v4.3a").glob("*.nc"))
    for mode in ("instant", "accumulated"):
        add("era5", mode, (data_root / "era5_single_levels" / mode).glob("*.nc"))

    lai = data_root / "modis" / "MCD15A2H" / "V061"
    lai_files = list(lai.glob("*h26v05*.hdf")) or list(lai.glob("*.hdf"))
    add("modis_lai", "MCD15A2H_h26v05", lai_files)
    add("mcd12q1", "MCD12Q1", (data_root / "modis" / "MCD12Q1" / "V061").glob("*.hdf"))
    add("gosif", "8day_geotiff_gz", (data_root / "gosif" / "v2").glob("*.tif.gz"))
    add("ai", "static_geotiff", (data_root / "aridity").glob("**/*.tif"))
    add("gmia", "static_archive", (data_root / "mask" / "gmia_v5").glob("*.zip"))
    return result


def parse_time_values(values: Any, attrs: dict[str, Any]) -> tuple[str, str]:
    """Return readable range and interval without loading a science array."""
    try:
        import numpy as np  # type: ignore

        arr = np.asarray(values).reshape(-1)
        if arr.size == 0:
            return "", ""
        first, last = arr[0], arr[-1]
        units = str(attrs.get("units", ""))
        match = re.match(r"\s*(seconds?|minutes?|hours?|days?)\s+since\s+(.+)", units, re.I)
        if match:
            base_text = match.group(2).strip().replace("Z", "")
            # Replace only an ISO date/time separator; do not alter the
            # ``T`` in a timezone suffix such as ``UTC``.
            base_text = re.sub(r"^(\d{4}-\d{2}-\d{2})T", r"\1 ", base_text)
            base_text = re.sub(r"\s+(UTC|GMT)$", "", base_text, flags=re.I).strip()
            base = dt.datetime.fromisoformat(base_text)
            factors = {"second": "seconds", "seconds": "seconds", "minute": "minutes", "minutes": "minutes", "hour": "hours", "hours": "hours", "day": "days", "days": "days"}
            unit = factors[match.group(1).lower()]
            kwargs = {unit: float(first)}
            start = base + dt.timedelta(**kwargs)
            kwargs = {unit: float(last)}
            end = base + dt.timedelta(**kwargs)
            range_text = f"{start.isoformat()} ~ {end.isoformat()}"
            if arr.size > 1:
                diffs = np.diff(arr.astype(float))
                interval = f"{unit}, step={float(np.median(diffs)):g}"
            else:
                interval = unit
            return range_text, interval
        numeric = [float(first), float(last)]
        return f"{numeric[0]:g} ~ {numeric[1]:g}", f"raw ({units})" if units else "raw"
    except Exception as exc:
        return "", f"unparsed: {exc}"


def range_text(values: Any) -> str:
    try:
        import numpy as np  # type: ignore

        arr = np.asarray(values).reshape(-1)
        if arr.size == 0:
            return ""
        return f"{arr.min():g} ~ {arr.max():g} (n={arr.size})"
    except Exception:
        return ""


def spacing_text(values: Any) -> str:
    """Return median coordinate spacing without loading a science variable."""
    try:
        import numpy as np  # type: ignore

        arr = np.asarray(values).reshape(-1).astype(float)
        if arr.size < 2:
            return ""
        diffs = np.abs(np.diff(arr))
        diffs = diffs[diffs > 0]
        return f"{float(np.median(diffs)):g}" if diffs.size else ""
    except Exception:
        return ""


def netcdf_records(dataset: str, group: str, path: Path, logger: logging.Logger) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        import xarray as xr  # type: ignore

        ds = xr.open_dataset(path, decode_cf=False, mask_and_scale=False, decode_times=False)
        dims = {str(k): int(v) for k, v in ds.sizes.items()}
        variables = list(ds.variables)
        coordinate_names = {"time", "lat", "latitude", "lon", "longitude", "x", "y"}
        # Prefer the actual 1-D time coordinate. ESA CCI also contains a
        # 3-D science variable named ``t0`` (observation timestamp), which
        # must not be used as the dataset time axis.
        time_name = next((n for n in variables if str(n).lower() in {"time", "valid_time"} and getattr(ds[n], "ndim", 0) <= 1), None)
        if time_name is None:
            time_name = next((n for n in variables if str(n).lower() == "t0" and getattr(ds[n], "ndim", 0) <= 1), None)
        time_attrs = dict(ds[time_name].attrs) if time_name else {}
        time_range, time_interval = (parse_time_values(ds[time_name].values, time_attrs) if time_name else ("", ""))
        coord_parts = []
        spacing_parts = []
        for name in variables:
            if str(name).lower() in coordinate_names:
                coord_parts.append(f"{name}={range_text(ds[name].values)}")
                spacing = spacing_text(ds[name].values)
                if spacing and str(name).lower() not in {"time", "t0", "valid_time"}:
                    spacing_parts.append(f"{name}={spacing}")
        qa_names = [name for name in variables if re.search(r"qa|qc|flag", str(name), re.I)]
        grid_mapping = []
        for name in variables:
            mapping = ds[name].attrs.get("grid_mapping")
            if mapping:
                grid_mapping.append(str(mapping))
        projection = ds.attrs.get("crs", ds.attrs.get("spatial_ref", ""))
        if grid_mapping:
            projection = f"{projection}; grid_mapping={','.join(sorted(set(grid_mapping)))}" if projection else f"grid_mapping={','.join(sorted(set(grid_mapping)))}"
        for name in variables:
            data = ds[name]
            attrs = dict(data.attrs)
            records.append({
                "dataset": dataset,
                "sample_group": group,
                "filename": path.name,
                "path": str(path),
                "format": "NetCDF",
                "file_size_bytes": path.stat().st_size,
                "sha256": "",
                "dimensions": dims,
                "variables": variables,
                "variable": str(name),
                "shape": list(data.shape),
                "dtype": str(data.dtype),
                "time_range": time_range,
                "time_interval": time_interval,
                "coordinate_range": "; ".join(coord_parts),
                "spatial_resolution": "; ".join(spacing_parts),
                "crs_projection": fmt(projection),
                "units": attrs.get("units", ""),
                "fill_value": attrs.get("_FillValue", attrs.get("missing_value", "")),
                "scale_factor": attrs.get("scale_factor", ""),
                "add_offset": attrs.get("add_offset", ""),
                "qa_variables": qa_names,
                "source_subdataset": "",
                "status": "PASS",
                "message": "xarray read OK (decode_cf=False; raw attributes preserved)",
            })
        ds.close()
        return records
    except ImportError:
        pass
    except Exception as exc:
        log_line(logger, "WARN", f"{path.name}: xarray read failed; trying netCDF4: {exc}")

    try:
        import netCDF4  # type: ignore

        ds = netCDF4.Dataset(path, "r")
        dims = {str(k): len(v) if len(v) is not None else None for k, v in ds.dimensions.items()}
        variables = list(ds.variables)
        time_name = next((n for n in variables if str(n).lower() in {"time", "valid_time"} and len(ds.variables[n].dimensions) <= 1), None)
        if time_name is None:
            time_name = next((n for n in variables if str(n).lower() == "t0" and len(ds.variables[n].dimensions) <= 1), None)
        time_range, time_interval = (parse_time_values(ds.variables[time_name][:], {a: getattr(ds.variables[time_name], a) for a in ds.variables[time_name].ncattrs()}) if time_name else ("", ""))
        qa_names = [name for name in variables if re.search(r"qa|qc|flag", str(name), re.I)]
        for name, data in ds.variables.items():
            attrs = {a: getattr(data, a) for a in data.ncattrs()}
            records.append({"dataset": dataset, "sample_group": group, "filename": path.name, "path": str(path), "format": "NetCDF", "file_size_bytes": path.stat().st_size, "sha256": "", "dimensions": dims, "variables": variables, "variable": name, "shape": list(data.shape), "dtype": str(data.dtype), "time_range": time_range, "time_interval": time_interval, "coordinate_range": "", "spatial_resolution": "", "crs_projection": "", "units": attrs.get("units", ""), "fill_value": attrs.get("_FillValue", attrs.get("missing_value", "")), "scale_factor": attrs.get("scale_factor", ""), "add_offset": attrs.get("add_offset", ""), "qa_variables": qa_names, "source_subdataset": "", "status": "PASS", "message": "netCDF4读取成功（只读）"})
        ds.close()
        return records
    except ImportError:
        message = "xarray or netCDF4 is not installed; NetCDF internals unavailable"
    except Exception as exc:
        message = f"netCDF4 read failed: {exc}"
    return [base_record(dataset, group, path, "NetCDF", "WARN", message)]


def base_record(dataset: str, group: str, path: Path, file_format: str, status: str, message: str, **extra: Any) -> dict[str, Any]:
    record = {field: "" for field in CSV_FIELDS}
    record.update({"dataset": dataset, "sample_group": group, "filename": path.name, "path": str(path), "format": file_format, "file_size_bytes": path.stat().st_size if path.exists() else "", "status": status, "message": message})
    record.update(extra)
    return record


def import_raster_backend() -> tuple[str, Any]:
    try:
        import rasterio  # type: ignore
        return "rasterio", rasterio
    except ImportError:
        try:
            from osgeo import gdal  # type: ignore
            return "gdal", gdal
        except ImportError:
            return "", None


def hdf_records(dataset: str, group: str, path: Path, logger: logging.Logger) -> list[dict[str, Any]]:
    """Read MODIS HDF4 SDS metadata with pyhdf, without loading arrays."""
    try:
        from pyhdf import SD  # type: ignore

        handle = SD.SD(str(path), SD.SDC.READ)
        datasets = handle.datasets()
        names = list(datasets)
        global_attrs = handle.attributes()
        projection_hint = "HDF4"
        if "StructMetadata.0" in global_attrs:
            projection_hint += "; StructMetadata.0 present"
        rows: list[dict[str, Any]] = []
        qa_names = [name for name in names if re.search(r"qa|qc|flag", str(name), re.I)]
        for name, info in datasets.items():
            # pyhdf returns (dimension names, shape, data type, attribute count).
            dimension_names, dimensions, data_type, _ = info
            selector = handle.select(name)
            attrs = selector.attributes()
            rows.append(base_record(
                dataset,
                group,
                path,
                "HDF4",
                "PASS",
                "pyhdf read OK (SDS values not loaded)",
                dimensions={"sds": len(names), "dimension_names": list(dimension_names)},
                variables=names,
                variable=str(name),
                shape=list(dimensions) if isinstance(dimensions, (tuple, list)) else dimensions,
                dtype=str(data_type),
                crs_projection=projection_hint,
                units=attrs.get("units", ""),
                fill_value=attrs.get("_FillValue", attrs.get("fill_value", "")),
                scale_factor=attrs.get("scale_factor", ""),
                add_offset=attrs.get("add_offset", ""),
                qa_variables=qa_names,
                source_subdataset=str(name),
            ))
            selector.endaccess()
        handle.end()
        return rows
    except ImportError:
        # Let the regular rasterio/GDAL path provide a useful fallback message.
        return raster_records(dataset, group, path, logger, file_format="HDF4")
    except Exception as exc:
        return [base_record(dataset, group, path, "HDF4", "WARN", f"pyhdf read failed: {exc}")]


def raster_records(dataset: str, group: str, path: Path, logger: logging.Logger, source_path: Optional[str] = None, file_format: str = "GeoTIFF") -> list[dict[str, Any]]:
    backend, module = import_raster_backend()
    if not backend:
        return [base_record(dataset, group, path, file_format, "WARN", "rasterio or GDAL is not installed; raster internals unavailable")]
    try:
        if backend == "rasterio":
            with module.open(path) as src:
                tags = src.tags()
                crs = str(src.crs) if src.crs else ""
                transform = src.transform
                resolution = [abs(float(transform.a)), abs(float(transform.e))]
                coord = f"bounds=({src.bounds.left:g},{src.bounds.bottom:g},{src.bounds.right:g},{src.bounds.top:g})"
                names = [f"band_{i}" for i in range(1, src.count + 1)]
                rows = []
                for index, name in enumerate(names, 1):
                    nodata = src.nodatavals[index - 1]
                    rows.append(base_record(dataset, group, path, file_format, "PASS", "rasterio read OK (pixel array not loaded)", dimensions={"height": src.height, "width": src.width, "band": src.count}, variables=names, variable=name, shape=[src.height, src.width], dtype=str(src.dtypes[index - 1]), coordinate_range=coord, spatial_resolution=resolution, crs_projection=crs, units=tags.get("units", ""), fill_value=nodata if nodata is not None else tags.get("_FillValue", ""), scale_factor=tags.get("scale_factor", ""), add_offset=tags.get("add_offset", ""), qa_variables=[n for n in names if re.search(r"qa|qc|flag", n, re.I)], source_subdataset=source_path or ""))
                return rows
        ds = module.Open(str(path), module.GA_ReadOnly)
        if ds is None:
            raise RuntimeError("GDAL无法打开文件")
        rows = []
        projection = ds.GetProjection() or ""
        transform = ds.GetGeoTransform()
        resolution = [abs(float(transform[1])), abs(float(transform[5]))] if transform else ""
        coord = f"transform={transform}"
        for index in range(1, ds.RasterCount + 1):
            band = ds.GetRasterBand(index)
            rows.append(base_record(dataset, group, path, file_format, "PASS", "GDAL read OK (read-only)", dimensions={"height": ds.RasterYSize, "width": ds.RasterXSize, "band": ds.RasterCount}, variables=[f"band_{i}" for i in range(1, ds.RasterCount + 1)], variable=f"band_{index}", shape=[ds.RasterYSize, ds.RasterXSize], dtype=band.DataType, coordinate_range=coord, spatial_resolution=resolution, crs_projection=projection, fill_value=band.GetNoDataValue(), scale_factor="", add_offset="", source_subdataset=source_path or ""))
        ds = None
        return rows
    except Exception as exc:
        return [base_record(dataset, group, path, "GeoTIFF", "WARN", f"{backend}读取失败: {exc}", source_subdataset=source_path or "")]


def ascii_header_records(dataset: str, group: str, path: Path, logger: logging.Logger) -> list[dict[str, Any]]:
    def parse_header(handle: Any) -> dict[str, str]:
        values: dict[str, str] = {}
        for _ in range(12):
            line = handle.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            parts = text.split(None, 1)
            if len(parts) == 2 and parts[0].lower() in {"ncols", "nrows", "xllcorner", "yllcorner", "cellsize", "nodata_value", "xllcenter", "yllcenter"}:
                values[parts[0].lower()] = parts[1]
            elif values and not parts:
                break
        return values

    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            asc = next((n for n in names if n.lower().endswith(".asc")), None)
            if not asc:
                return [base_record(dataset, group, path, "ZIP", "WARN", f"no ASCII grid in ZIP; members={names}")]
            with archive.open(asc, "r") as handle:
                header = parse_header(handle)
            ncols, nrows, cellsize = header.get("ncols"), header.get("nrows"), header.get("cellsize")
            coord = "; ".join(f"{key}={value}" for key, value in header.items() if key in {"xllcorner", "yllcorner", "xllcenter", "yllcenter"})
            return [base_record(dataset, group, path, "ZIP/ESRI ASCII Grid", "PASS", "ZIP and ASCII header read OK (no extraction to disk)", dimensions={"ncols": ncols, "nrows": nrows}, variables=[asc], variable=asc, shape=[nrows, ncols], dtype="ASCII numeric", coordinate_range=coord, spatial_resolution=cellsize, fill_value=header.get("nodata_value"), source_subdataset=asc)]
    except Exception as exc:
        return [base_record(dataset, group, path, "ZIP", "WARN", f"ZIP/ASCII read failed: {exc}")]


def inspect_sample(sample: dict[str, Any], logger: logging.Logger, calculate_checksum: bool = False) -> list[dict[str, Any]]:
    dataset, group, path = sample["dataset"], sample["group"], sample.get("path")
    if not path:
        return [base_record(dataset, group, Path(f"<missing:{dataset}>"), "", "FAIL", "no sample file found")]
    log_line(logger, "INFO", f"READ {dataset}/{group}/{sample.get('sample', 'only')}: {path.name}")
    suffix = path.name.lower()
    if suffix.endswith(".nc"):
        records = netcdf_records(dataset, group, path, logger)
    elif suffix.endswith(".hdf"):
        records = hdf_records(dataset, group, path, logger)
        if records and records[0]["status"] == "WARN":
            records[0]["message"] += "; MODIS HDF4 requires pyhdf or GDAL"
    elif suffix.endswith(".tif") or suffix.endswith(".tiff"):
        records = raster_records(dataset, group, path, logger)
    elif suffix.endswith(".tif.gz"):
        with tempfile.TemporaryDirectory(prefix="datacheck_gosif_") as temp_dir:
            target = Path(temp_dir) / path.name[:-3]
            try:
                with gzip.open(path, "rb") as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                records = raster_records(dataset, group, target, logger, source_path=f"{path.name} (temporary extraction only)", file_format="GZip/GeoTIFF")
                for row in records:
                    row["filename"] = path.name
                    row["path"] = str(path)
                    row["file_size_bytes"] = path.stat().st_size
            except Exception as exc:
                records = [base_record(dataset, group, path, "GZip/GeoTIFF", "WARN", f"GZip read-only extraction or raster read failed: {exc}")]
    elif suffix.endswith(".zip") and dataset == "gmia":
        records = ascii_header_records(dataset, group, path, logger)
    else:
        records = [base_record(dataset, group, path, path.suffix, "WARN", "unsupported file format")]
    # Full-file hashing is deliberately opt-in: GLEAM annual files are large,
    # while this command is intended to quickly discover structure for cropping.
    digest = file_sha256(path) if calculate_checksum else ""
    for row in records:
        row["sha256"] = digest
        if not calculate_checksum and row.get("status") == "PASS":
            row["message"] = f"{row.get('message', '')}; SHA-256 not computed (use --checksum)"
    for message in sorted({str(row.get("message", "")) for row in records if row.get("status") == "WARN" and row.get("message")}):
        log_line(logger, "WARN", f"{path.name}: {message}")
    level = "OK" if all(row.get("status") == "PASS" for row in records) else "WARN"
    log_line(logger, level, f"DONE {path.name}: {len(records)} structure record(s)")
    return records


def write_csv(records: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({field: fmt(record.get(field, "")) for field in CSV_FIELDS})


def write_summary(records: list[dict[str, Any]], samples: list[dict[str, Any]], output: Path, elapsed: float) -> None:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record.get("dataset", "unknown"))].append(record)
    lines = ["# 数据集抽样结构检查报告", "", f"- 生成时间：{dt.datetime.now().isoformat(timespec='seconds')}", f"- 用时：{elapsed:.1f} 秒", "- 检查范围：每个动态数据集最早/最晚样本；未读取科学数组", "- 原始数据：只读；GOSIF仅解压到临时目录，GMIA在ZIP内读取ASCII头信息", ""]
    lines += ["## 抽样文件", "", "| 数据集 | 分组 | 样本 | 文件 |", "|---|---|---|---|"]
    for sample in samples:
        lines.append(f"| {sample['dataset']} | {sample['group']} | {sample.get('sample', 'only')} | {Path(sample['path']).name if sample.get('path') else '未找到'} |")
    lines += ["", "## 检查结果", "", "| 数据集 | 文件/变量记录数 | PASS | WARN | FAIL |", "|---|---:|---:|---:|---:|"]
    for dataset in DATASET_ORDER:
        rows = grouped.get(dataset, [])
        counts = {status: sum(row.get("status") == status for row in rows) for status in ("PASS", "WARN", "FAIL")}
        lines.append(f"| {dataset} | {len(rows)} | {counts['PASS']} | {counts['WARN']} | {counts['FAIL']} |")
    warnings = [row for row in records if row.get("status") != "PASS"]
    lines += ["", "## 注意事项", ""]
    if warnings:
        for row in warnings:
            lines.append(f"- `{row.get('dataset')}/{row.get('filename')}`：{row.get('message')}")
    else:
        lines.append("- 未发现结构读取异常。")
    lines += ["", "详细字段见 `structure_check.csv`；完整嵌套属性见 `structure_check.json`。", ""]
    output.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only sample structure check for cropping preparation")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT, help="raw data root (default: data_raw)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="report directory (default: logs/datacheck)")
    parser.add_argument("--dataset", action="append", choices=DATASET_ORDER, help="check only selected dataset; repeat for multiple datasets")
    parser.add_argument("--checksum", action="store_true", help="compute full SHA-256 for samples; slower for large files")
    parser.add_argument("--verbose", action="store_true", help="show detailed terminal logging")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        data_root, output_dir = ensure_read_only_scope(args.data_root, args.output_dir)
    except Exception as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    logger = setup_logger(output_dir, args.verbose)
    started = time.perf_counter()
    selected = set(args.dataset or [])
    log_line(logger, "INFO", "START read-only sample structure check")
    log_line(logger, "INFO", f"SOURCE data root: {data_root}")
    log_line(logger, "INFO", f"OUTPUT report dir: {output_dir}")
    backends = ", ".join(f"{name}={'YES' if importlib.util.find_spec(name) else 'NO'}" for name in ("xarray", "netCDF4", "rasterio", "osgeo", "pyhdf"))
    log_line(logger, "INFO", f"BACKENDS {backends}")
    samples = samples_for(data_root, selected)
    log_line(logger, "INFO", f"SAMPLES files={sum(bool(s.get('path')) for s in samples)}; dynamic datasets use first/last slices")
    progress = ProgressDisplay(len(samples), data_root, output_dir)
    progress.render(0, "initializing sample list", "START", {"PASS": 0, "WARN": 0, "FAIL": 0}, "metadata only; source files are read-only")
    records: list[dict[str, Any]] = []
    for index, sample in enumerate(samples, 1):
        log_line(logger, "INFO", f"PROGRESS {index}/{len(samples)}")
        sample_path = sample.get("path")
        current = f"{sample['dataset']}/{sample['group']}/{Path(sample_path).name if sample_path else '<missing>'}"
        before_counts = {status: sum(row.get("status") == status for row in records) for status in ("PASS", "WARN", "FAIL")}
        progress.render(index - 1, current, "RUN", before_counts, "reading metadata")
        sample_records = inspect_sample(sample, logger, calculate_checksum=args.checksum)
        records.extend(sample_records)
        counts = {status: sum(row.get("status") == status for row in records) for status in ("PASS", "WARN", "FAIL")}
        status = "FAIL" if counts["FAIL"] and any(row.get("status") == "FAIL" for row in sample_records) else "WARN" if any(row.get("status") == "WARN" for row in sample_records) else "PASS"
        detail = next((str(row.get("message", "")) for row in sample_records if row.get("status") != "PASS"), f"{len(sample_records)} variable record(s)")
        progress.render(index, current, status, counts, detail)
    elapsed = time.perf_counter() - started
    with (output_dir / "structure_check.json").open("w", encoding="utf-8") as handle:
        json.dump(records, handle, ensure_ascii=False, indent=2, default=_json)
    write_csv(records, output_dir / "structure_check.csv")
    write_summary(records, samples, output_dir / "structure_summary.md", elapsed)
    pass_count = sum(row.get("status") == "PASS" for row in records)
    warn_count = sum(row.get("status") == "WARN" for row in records)
    fail_count = sum(row.get("status") == "FAIL" for row in records)
    progress.render(len(samples), "complete", "FAIL" if fail_count else "PASS", {"PASS": pass_count, "WARN": warn_count, "FAIL": fail_count}, f"{len(records)} structure record(s); elapsed={elapsed:.1f}s")
    progress.finish()
    log_line(logger, "INFO", f"SUMMARY records={len(records)} PASS={pass_count} WARN={warn_count} FAIL={fail_count} elapsed={elapsed:.1f}s")
    log_line(logger, "INFO", f"REPORT {output_dir / 'structure_check.csv'}")
    print(f"[REPORT] {output_dir / 'structure_check.csv'}")
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
