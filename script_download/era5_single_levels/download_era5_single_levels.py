"""Download ERA5 hourly single-level variables for the flash-drought study.

Dataset: reanalysis-era5-single-levels
Period: selected at runtime as a start and end month
Area: 102-115 E, 33.5-41.5 N
Output: one or two NetCDF files per month under data_raw/era5_single_levels/

Before running:
    pip install --upgrade "cdsapi>=0.7.7"

The CDS credentials are configured below for this local script. Do not commit
this file to a public repository or share its contents.

The terminal display shows CDS queue/processing status, downloaded bytes,
progress, current speed, average speed and ETA for each monthly request.
"""

import argparse
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import logging
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import unicodedata
import zipfile

try:
    import cdsapi
except ModuleNotFoundError as exc:
    if exc.name == "cdsapi":
        raise SystemExit(
            "缺少依赖 cdsapi。请在当前终端运行：\n"
            '    python -m pip install --upgrade "cdsapi>=0.7.7"\n'
            "然后重新运行本脚本。"
        ) from None
    raise


# Resolve paths from this file so the script can be launched from any cwd.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data_raw" / "era5_single_levels"
LOG_DIR = PROJECT_ROOT / "logs" / "download" / "era5_single_levels"
LOG_FILE = LOG_DIR / "download_era5_single_levels.log"
LOGGER = logging.getLogger("era5_single_levels")

DATASET = "reanalysis-era5-single-levels"

# CDS credentials. Keep this file private; rotate the token if it is exposed.
CDS_API_URL = "https://cds.climate.copernicus.eu/api"
CDS_API_KEY = "f1c9cac3-186a-468e-8c64-7825f4672455"

# These are the six variables selected for the study.
VARIABLES = [
    "2m_temperature",
    "2m_dewpoint_temperature",
    "total_precipitation",
    "surface_solar_radiation_downwards",
    "potential_evaporation",
    "evaporation",
]

# CDS area order is [North, West, South, East].
AREA = [41.5, 102.0, 33.5, 115.0]

TIMES = [f"{hour:02d}:00" for hour in range(24)]


def setup_logging() -> logging.Logger:
    """初始化文件日志，避免并发 worker 的日志破坏终端卡片。"""

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()
    LOGGER.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)
    return LOGGER


class LegacyProgressDisplay:
    """显示多个并行月度请求的状态、进度、速度和 ETA。"""

    BOX_WIDTH = 104
    BAR_WIDTH = 22
    CLEAR = "\033[2K"
    CURSOR_UP = "\033[A"

    def __init__(self, workers: int, total_jobs: int) -> None:
        self.workers = workers
        self.total_jobs = total_jobs
        self.completed = 0
        self.failed = 0
        self.start_time = time.monotonic()
        self.last_render = 0.0
        self.rendered_lines = 0
        self.lock = threading.Lock()
        self.jobs: dict[int, dict[str, object]] = {}
        self._enable_ansi()

    @staticmethod
    def _enable_ansi() -> None:
        if os.name != "nt":
            return
        try:
            import ctypes

            handle = ctypes.windll.kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass

    @staticmethod
    def format_size(value: float) -> str:
        if value < 1024:
            return f"{value:.0f} B"
        if value < 1024**2:
            return f"{value / 1024:.1f} KB"
        if value < 1024**3:
            return f"{value / 1024**2:.1f} MB"
        return f"{value / 1024**3:.2f} GB"

    @staticmethod
    def format_time(seconds: float) -> str:
        if seconds < 0:
            return "--"
        if seconds < 60:
            return f"{seconds:.0f}s"
        if seconds < 3600:
            return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"
        return f"{int(seconds // 3600)}h {int(seconds % 3600 // 60):02d}m"

    def _bar(self, percent: float) -> str:
        percent = max(0.0, min(percent, 100.0))
        filled = round(self.BAR_WIDTH * percent / 100)
        return "[" + "#" * filled + "-" * (self.BAR_WIDTH - filled) + "]"

    def _line(self, text: str = "") -> str:
        text = text[: self.BOX_WIDTH - 2]
        return f"| {text:<{self.BOX_WIDTH - 2}}|"

    def begin(self, slot: int, name: str, part_path: Path) -> None:
        with self.lock:
            self.jobs[slot] = {
                "name": name,
                "part": part_path,
                "total": None,
                "status": "queued",
                "detail": "",
            }

    def set_total(self, slot: int, total: int) -> None:
        with self.lock:
            if slot in self.jobs:
                self.jobs[slot]["total"] = total
                self.jobs[slot]["status"] = "downloading"

    def finish(self, slot: int, status: str, detail: str = "") -> None:
        with self.lock:
            if slot in self.jobs:
                old_status = str(self.jobs[slot].get("status", ""))
                self.jobs[slot]["status"] = status
                self.jobs[slot]["detail"] = detail
                if status in {"done", "skip"} and old_status not in {"done", "skip"}:
                    self.completed += 1
                elif status == "fail" and old_status != "fail":
                    self.failed += 1

    def render(self) -> None:
        now = time.monotonic()
        if now - self.last_render < 0.4:
            return
        self.last_render = now

        with self.lock:
            jobs = {slot: dict(job) for slot, job in self.jobs.items()}

        elapsed = max(now - self.start_time, 0.001)
        total_bytes = 0
        known_total = 0
        done = self.completed
        failed = self.failed
        progress_units = float(done)
        rows: list[str] = []

        for slot in range(self.workers):
            job = jobs.get(slot)
            if not job:
                rows.append(f"{slot + 1:02d} [idle]")
                continue

            part_path = job["part"]
            received = part_path.stat().st_size if isinstance(part_path, Path) and part_path.exists() else 0
            total = job.get("total")
            status = str(job.get("status", "queued"))
            if status in {"done", "skip"} and isinstance(total, int):
                received = total
            if isinstance(total, int):
                known_total += total
            total_bytes += received
            if status not in {"done", "skip", "fail"} and isinstance(total, int) and total:
                progress_units += min(received / total, 1.0)

            percent = received / total * 100 if isinstance(total, int) and total else 0
            status_text = {
                "queued": "CDS处理",
                "downloading": "下载中",
                "done": "完成",
                "skip": "跳过",
                "fail": "失败",
            }.get(status, status)
            name = str(job["name"])[-34:].ljust(34)
            rows.append(
                f"{slot + 1:02d} {name} {self._bar(percent)} {percent:5.1f}% "
                f"{self.format_size(received):>10} {status_text}"
            )

        average_speed = total_bytes / elapsed
        overall_percent = progress_units / self.total_jobs * 100 if self.total_jobs else 100
        if known_total:
            eta = (known_total - total_bytes) / average_speed if average_speed > 0 else -1
            overall = (
                f"{self._bar(overall_percent)} {overall_percent:5.1f}%  "
                f"当前批次 {self.format_size(total_bytes)} / {self.format_size(known_total)}"
            )
        else:
            eta = -1
            overall = f"{self._bar(overall_percent)} {overall_percent:5.1f}%  文件 {done}/{self.total_jobs}"

        lines = [
            f"+{'-' * self.BOX_WIDTH}+",
            self._line(f"ERA5 单层小时数据并行下载 | workers={self.workers}"),
            f"+{'-' * self.BOX_WIDTH}+",
        ]
        lines.extend(self._line(row) for row in rows)
        lines.extend(
            [
                f"+{'-' * self.BOX_WIDTH}+",
                self._line(f"总进度：{overall}"),
                self._line(
                    f"总速度：{self.format_size(average_speed)}/s  "
                    f"已用：{self.format_time(elapsed)}  "
                    f"预计剩余：{self.format_time(eta)}  "
                    f"完成：{done}/{self.total_jobs}  失败：{failed}"
                ),
                f"+{'-' * self.BOX_WIDTH}+",
            ]
        )

        if self.rendered_lines:
            sys.stdout.write(self.CURSOR_UP * self.rendered_lines)
        for line in lines:
            sys.stdout.write(f"{self.CLEAR}{line}\n")
        sys.stdout.flush()
        self.rendered_lines = len(lines)

    def clear(self) -> None:
        if not self.rendered_lines:
            return
        sys.stdout.write(self.CURSOR_UP * self.rendered_lines)
        for _ in range(self.rendered_lines):
            sys.stdout.write(f"{self.CLEAR}\n")
        sys.stdout.flush()
        self.rendered_lines = 0


class ProgressDisplay:
    """按 CCI 下载器样式显示并发 worker、总进度、速度和 ETA。"""

    BOX_WIDTH = 86
    BAR_WIDTH = 24
    COLORS = {
        "clear": "\033[2K", "up": "\033[A", "hide": "\033[?25l",
        "show": "\033[?25h", "bold": "\033[1m", "dim": "\033[2m",
        "green": "\033[32m", "cyan": "\033[36m", "yellow": "\033[33m",
        "red": "\033[31m", "white": "\033[97m", "reset": "\033[0m",
    }

    def __init__(self, workers: int, total_jobs: int) -> None:
        self.workers_count = workers
        self.total_jobs = total_jobs
        self.lock = threading.Lock()
        self.start = time.monotonic()
        self.rendered_lines = 0
        self.completed = 0
        self.failed = 0
        self.completed_bytes = 0
        self.workers: dict[int, dict[str, object]] = {}
        self._enable_ansi()

    @staticmethod
    def _enable_ansi() -> None:
        if os.name != "nt":
            return
        try:
            import ctypes

            handle = ctypes.windll.kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass

    @staticmethod
    def format_size(value: float) -> str:
        if value < 1024:
            return f"{value:.0f} B"
        if value < 1024**2:
            return f"{value / 1024:.1f} KB"
        if value < 1024**3:
            return f"{value / 1024**2:.1f} MB"
        return f"{value / 1024**3:.2f} GB"

    @staticmethod
    def format_time(seconds: float) -> str:
        if seconds < 0:
            return "--"
        if seconds < 60:
            return f"{seconds:.0f}s"
        if seconds < 3600:
            return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"
        return f"{int(seconds // 3600)}h {int(seconds % 3600 // 60):02d}m"

    def _visible_width(self, text: str) -> int:
        width = 0
        index = 0
        while index < len(text):
            if text[index] == "\033":
                end = text.find("m", index)
                index = len(text) if end < 0 else end + 1
                continue
            if unicodedata.combining(text[index]):
                index += 1
                continue
            width += 2 if unicodedata.east_asian_width(text[index]) in "WF" else 1
            index += 1
        return width

    def _pad(self, text: str, width: int) -> str:
        return text + " " * max(0, width - self._visible_width(text))

    def _content_line(self, text: str = "") -> str:
        return f"│ {self._pad(text, self.BOX_WIDTH - 1)}│"

    def _bar(self, percent: float, width: int | None = None) -> str:
        width = width or self.BAR_WIDTH
        filled = round(width * max(0.0, min(percent, 100.0)) / 100)
        c = self.COLORS
        return f"{c['green']}{'█' * filled}{'░' * (width - filled)}{c['reset']}"

    def begin(self, slot: int, name: str, part_path: Path) -> None:
        with self.lock:
            self.workers[slot] = {
                "name": name,
                "part": part_path,
                "total": None,
                "received": 0,
                "base": 0,
                "speed": 0.0,
                "downloaded": 0,
                "last_bytes": 0,
                "last_time": time.monotonic(),
                "status": "queued",
            }

    def set_total(self, slot: int, total: int) -> None:
        with self.lock:
            if slot in self.workers:
                self.workers[slot]["total"] = total
                self.workers[slot]["status"] = "downloading"

    def finish(self, slot: int, status: str, detail: str = "") -> None:
        with self.lock:
            worker = self.workers.get(slot)
            if not worker:
                return
            old_status = str(worker.get("status", ""))
            total = worker.get("total")
            received = int(worker.get("received", 0))
            if status in {"done", "skip"} and isinstance(total, int):
                received = total
            worker.update({
                "status": status,
                "received": received,
                "downloaded": max(0, received - int(worker.get("base", 0))),
                "speed": 0.0,
                "detail": detail,
            })
            if status in {"done", "skip"} and old_status not in {"done", "skip"}:
                self.completed += 1
                self.completed_bytes += int(worker["downloaded"])
            elif status == "fail" and old_status != "fail":
                self.failed += 1

    def _snapshot(self) -> dict[int, dict[str, object]]:
        now = time.monotonic()
        with self.lock:
            for worker in self.workers.values():
                part = worker.get("part")
                if isinstance(part, Path) and part.exists():
                    received = part.stat().st_size
                else:
                    received = int(worker.get("received", 0))
                old_received = int(worker.get("last_bytes", 0))
                old_time = float(worker.get("last_time", now))
                elapsed = now - old_time
                if elapsed >= 0.2 and str(worker.get("status")) not in {"done", "skip", "fail"}:
                    worker["speed"] = max(0, received - old_received) / elapsed
                    worker["last_bytes"] = received
                    worker["last_time"] = now
                worker["received"] = received
                total = worker.get("total")
                worker["pct"] = min(received / total * 100, 100.0) if isinstance(total, int) and total else 0.0
                worker["downloaded"] = max(0, received - int(worker.get("base", 0)))
            return {slot: dict(worker) for slot, worker in self.workers.items()}

    def render(self) -> None:
        c = self.COLORS
        workers = self._snapshot()
        with self.lock:
            completed, failed, completed_bytes = self.completed, self.failed, self.completed_bytes

        elapsed = max(time.monotonic() - self.start, 0.001)
        active = sum(worker.get("status") in {"queued", "downloading"} for worker in workers.values())
        active_units = sum(
            min(float(worker.get("pct", 0)) / 100, 1.0)
            for worker in workers.values()
            if worker.get("status") not in {"done", "skip", "fail"}
        )
        percent = (completed + active_units) / self.total_jobs * 100 if self.total_jobs else 100
        run_bytes = completed_bytes + sum(
            int(worker.get("downloaded", 0))
            for worker in workers.values()
            if worker.get("status") not in {"done", "skip", "fail"}
        )
        speed = run_bytes / elapsed
        eta = -1

        lines = [f"┌{'─' * self.BOX_WIDTH}┐"]
        title = f"{c['bold']}{c['white']} ERA5 SINGLE LEVELS 并行下载 ×{self.workers_count}{c['reset']}"
        padding = self.BOX_WIDTH - self._visible_width(title)
        lines.append(f"│{' ' * (padding // 2)}{title}{' ' * (padding - padding // 2)}│")
        lines.append(f"├{'─' * self.BOX_WIDTH}┤")

        for slot in range(self.workers_count):
            worker = workers.get(slot)
            if not worker:
                row = f"{c['dim']}[空闲]{c['reset']}"
            else:
                name = str(worker.get("name", ""))[-22:].ljust(22)
                pct = float(worker.get("pct", 0))
                status = worker.get("status")
                if status in {"done", "skip"}:
                    tag = f"{c['green']}✓ 完成{c['reset']}"
                elif status == "fail":
                    tag = f"{c['red']}✗ 失败{c['reset']}"
                elif status == "queued":
                    tag = f"{c['yellow']}… CDS处理中{c['reset']}"
                else:
                    tag = f"{c['yellow']}↓ {float(worker.get('speed', 0)) / 1024**2:.1f} MB/s{c['reset']}"
                row = (
                    f"{c['cyan']}{name}{c['reset']} {self._bar(pct)} "
                    f"{c['white']}{pct:5.1f}%{c['reset']}  "
                    f"{self.format_size(float(worker.get('downloaded', 0))):>10}  {tag}"
                )
            lines.append(self._content_line(row))

        lines.append(f"├{'─' * self.BOX_WIDTH}┤")
        overall = f"{self._bar(percent)} {min(percent, 100):5.1f}%  文件 {completed}/{self.total_jobs}"
        lines.append(self._content_line(f"{c['yellow']}总进度{c['reset']} {overall}"))
        info = (
            f"{c['green']}速度:{c['reset']} {speed / 1024**2:.1f} MB/s  "
            f"{c['cyan']}已用:{c['reset']} {self.format_time(elapsed)}  "
            f"{c['yellow']}剩余:{c['reset']} {self.format_time(eta)}  "
            f"{c['yellow']}活跃:{c['reset']} {active}/{self.workers_count}  "
            f"{c['red']}失败:{c['reset']} {failed}"
        )
        lines.append(self._content_line(info))
        lines.append(f"└{'─' * self.BOX_WIDTH}┘")

        if self.rendered_lines:
            sys.stdout.write(c["up"] * self.rendered_lines)
        for line in lines:
            sys.stdout.write(f"{c['clear']}{line}\n")
        sys.stdout.flush()
        self.rendered_lines = len(lines)

    def clear(self) -> None:
        if not self.rendered_lines:
            return
        c = self.COLORS
        sys.stdout.write(c["up"] * self.rendered_lines)
        for _ in range(self.rendered_lines):
            sys.stdout.write(f"{c['clear']}\n")
        sys.stdout.flush()
        self.rendered_lines = 0


def parse_year_month(value: str) -> tuple[int, int]:
    """Parse a YYYY-MM value supplied by the user."""

    try:
        parsed = datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise ValueError(
            f"Invalid month '{value}'. Please use YYYY-MM, for example 2001-01."
        ) from exc
    return parsed.year, parsed.month


def iter_months(start: tuple[int, int], end: tuple[int, int]):
    """Yield (year, month) pairs from start through end, inclusively."""

    year, month = start
    while (year, month) <= end:
        yield year, month
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1


def get_period(args: argparse.Namespace) -> tuple[tuple[int, int], tuple[int, int]]:
    """Get the requested period from CLI arguments or interactive prompts."""

    if (args.start is None) != (args.end is None):
        raise ValueError("--start and --end must be supplied together.")

    if args.start is None:
        start_text = input("Start month (YYYY-MM): ").strip()
        end_text = input("End month   (YYYY-MM): ").strip()
    else:
        start_text = args.start
        end_text = args.end

    start = parse_year_month(start_text)
    end = parse_year_month(end_text)

    if start > end:
        raise ValueError("Start month must not be later than end month.")

    return start, end


def download_month_legacy(
    client: cdsapi.Client,
    output_dir: Path,
    year: int,
    month: int,
) -> None:
    """Download one month unless its target file already exists."""

    month_text = f"{month:02d}"
    target = output_dir / f"era5_single_levels_{year}_{month_text}.nc"
    part = Path(f"{target}.filepart")

    if target.exists() and target.stat().st_size > 0:
        print(f"[SKIP] {target.name} already exists")
        return

    days = [
        f"{day:02d}"
        for day in range(1, monthrange(year, month)[1] + 1)
    ]

    request = {
        "product_type": ["reanalysis"],
        "variable": VARIABLES,
        "year": [str(year)],
        "month": [month_text],
        "day": days,
        "time": TIMES,
        "area": AREA,
        "data_format": "netcdf",
    }

    progress = ProgressDisplay(target.name)
    result_holder: dict[str, object] = {}

    def retrieve_and_download() -> None:
        try:
            # 先提交并等待 CDS 生成结果；随后由 Result.download() 流式写入
            # .filepart，主线程通过文件大小实时刷新进度。
            result = client.retrieve(DATASET, request)
            result_holder["total"] = int(result.content_length)
            result.download(str(part))
        except Exception as exc:  # noqa: BLE001 - 交给主线程统一处理
            result_holder["error"] = exc

    part.unlink(missing_ok=True)
    worker = threading.Thread(target=retrieve_and_download, daemon=True)
    worker.start()

    progress.render(0, None, "CDS排队/生成文件")
    while worker.is_alive():
        downloaded = part.stat().st_size if part.exists() else 0
        total = result_holder.get("total")
        progress.render(
            downloaded,
            int(total) if isinstance(total, int) else None,
            "正在下载" if total else "CDS排队/生成文件",
        )
        time.sleep(0.5)

    worker.join()
    downloaded = part.stat().st_size if part.exists() else 0
    total = result_holder.get("total")
    if "error" in result_holder:
        error = result_holder["error"]
        error_text = f"{type(error).__name__}: {error}"
        progress.render(
            downloaded,
            int(total) if isinstance(total, int) else None,
            "失败",
            error_text,
        )
        progress.clear()
        print(f"[ERROR] {year}-{month_text}: {error_text}")
        part.unlink(missing_ok=True)
        raise RuntimeError(
            f"{year}-{month_text} 下载失败：{error_text}"
        ) from error

    if not part.exists():
        progress.render(0, int(total) if isinstance(total, int) else None, "失败", "未生成文件")
        raise RuntimeError(f"{year}-{month_text} 未生成下载文件")

    if isinstance(total, int) and downloaded != total:
        progress.render(downloaded, total, "失败", "文件大小不完整")
        raise RuntimeError(
            f"{year}-{month_text} 文件大小不完整：{downloaded} / {total} bytes"
        )

    part.replace(target)
    progress.render(downloaded, int(total) if isinstance(total, int) else None, "完成")
    progress.clear()
    print(f"[DONE]  {target}")


def is_netcdf_file(path: Path) -> bool:
    """Return whether a file starts with a classic NetCDF or NetCDF4 header."""

    if not path.is_file() or path.stat().st_size < 4:
        return False
    with path.open("rb") as handle:
        header = handle.read(8)
    return header[:3] == b"CDF" or header == b"\x89HDF\r\n\x1a\n"


def month_prefix(output_dir: Path, year: int, month: int) -> Path:
    return output_dir / f"era5_single_levels_{year}_{month:02d}"


def month_is_complete(output_dir: Path, year: int, month: int) -> bool:
    """Check for valid output files, avoiding false skips for ZIP files named .nc."""

    prefix = month_prefix(output_dir, year, month)
    direct = Path(f"{prefix}.nc")
    if is_netcdf_file(direct):
        return True
    return is_netcdf_file(Path(f"{prefix}_instant.nc")) and is_netcdf_file(
        Path(f"{prefix}_accum.nc")
    )


def extract_netcdf_archive(archive: Path, output_dir: Path, prefix: Path) -> list[Path]:
    """Extract NetCDF members returned by CDS when it wraps a ZIP archive."""

    with zipfile.ZipFile(archive) as bundle:
        members = [
            info
            for info in bundle.infolist()
            if not info.is_dir() and info.filename.lower().endswith(".nc")
        ]
        if not members:
            raise RuntimeError("CDS ZIP 中没有找到 NetCDF 文件")

        outputs: list[Path] = []
        for index, info in enumerate(members):
            member_name = Path(info.filename).name.lower()
            if "steptype-instant" in member_name:
                suffix = "instant"
            elif "steptype-accum" in member_name:
                suffix = "accum"
            else:
                suffix = f"part{index + 1:02d}"

            target = output_dir / f"{prefix.name}_{suffix}.nc"
            part = Path(f"{target}.filepart")
            part.unlink(missing_ok=True)
            with bundle.open(info) as source, part.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=4 * 1024 * 1024)
            if not is_netcdf_file(part):
                part.unlink(missing_ok=True)
                raise RuntimeError(f"ZIP 成员不是有效 NetCDF：{info.filename}")
            part.replace(target)
            outputs.append(target)

    archive.unlink(missing_ok=True)
    return outputs


def normalise_legacy_archive(output_dir: Path, year: int, month: int) -> None:
    """Repair an earlier test result that was a ZIP archive named with .nc."""

    prefix = month_prefix(output_dir, year, month)
    mislabeled = Path(f"{prefix}.nc")
    if not mislabeled.is_file() or is_netcdf_file(mislabeled) or not zipfile.is_zipfile(mislabeled):
        return

    archive = Path(f"{prefix}.zip")
    if archive.exists():
        mislabeled.unlink(missing_ok=True)
    else:
        mislabeled.replace(archive)
    extract_netcdf_archive(archive, output_dir, prefix)


def build_request(year: int, month: int) -> dict[str, object]:
    month_text = f"{month:02d}"
    return {
        "product_type": ["reanalysis"],
        "variable": VARIABLES,
        "year": [str(year)],
        "month": [month_text],
        "day": [
            f"{day:02d}"
            for day in range(1, monthrange(year, month)[1] + 1)
        ],
        "time": TIMES,
        "area": AREA,
        "data_format": "netcdf",
    }


def download_month(
    slot: int,
    output_dir: Path,
    year: int,
    month: int,
    progress: ProgressDisplay,
) -> tuple[int, int, str, str]:
    """Download and normalise one month; this function runs in a worker thread."""

    label = f"{year}-{month:02d}"
    prefix = month_prefix(output_dir, year, month)
    archive_part = Path(f"{prefix}.zip.filepart")
    progress.begin(slot, label, archive_part)
    LOGGER.info("[%s] 开始处理", label)

    if month_is_complete(output_dir, year, month):
        progress.finish(slot, "skip", "已存在有效 NetCDF")
        LOGGER.info("[%s] 跳过：已有有效 NetCDF 文件", label)
        return year, month, "skip", "已存在有效 NetCDF"

    archive_part.unlink(missing_ok=True)
    client = cdsapi.Client(
        url=CDS_API_URL,
        key=CDS_API_KEY,
        quiet=True,
        progress=False,
    )

    try:
        result = client.retrieve(DATASET, build_request(year, month))
        total = int(result.content_length)
        LOGGER.info("[%s] CDS 文件已生成，大小 %.2f MB", label, total / 1024**2)
        progress.set_total(slot, total)
        result.download(str(archive_part))
        downloaded = archive_part.stat().st_size
        if downloaded != total:
            raise RuntimeError(f"文件大小不完整：{downloaded} / {total} bytes")

        if zipfile.is_zipfile(archive_part):
            outputs = extract_netcdf_archive(archive_part, output_dir, prefix)
            detail = ", ".join(path.name for path in outputs)
        elif is_netcdf_file(archive_part):
            target = Path(f"{prefix}.nc")
            archive_part.replace(target)
            detail = target.name
        else:
            raise RuntimeError("CDS 返回文件既不是 ZIP，也不是有效 NetCDF")

        progress.finish(slot, "done", detail)
        LOGGER.info("[%s] 下载完成：%s", label, detail)
        return year, month, "downloaded", detail
    except Exception as exc:  # noqa: BLE001 - 汇总到主线程
        error_text = f"{type(exc).__name__}: {exc}"
        progress.finish(slot, "fail", error_text)
        archive_part.unlink(missing_ok=True)
        LOGGER.exception("[%s] 下载失败：%s", label, error_text)
        return year, month, "failed", error_text


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download ERA5 hourly single-level data by month."
    )
    parser.add_argument(
        "--start",
        help="Start month in YYYY-MM format. If omitted, prompt at runtime.",
    )
    parser.add_argument(
        "--end",
        help="End month in YYYY-MM format. If omitted, prompt at runtime.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory (default: data_raw/era5_single_levels).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="并发下载的月份数，默认 4；允许 1-10。",
    )
    args = parser.parse_args()

    if not 1 <= args.workers <= 10:
        parser.error("--workers must be between 1 and 10.")

    try:
        start, end = get_period(args)
    except ValueError as exc:
        parser.error(str(exc))

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    setup_logging()
    LOGGER.info("========== ERA5 下载任务开始 ==========")
    LOGGER.info(
        "时间范围：%04d-%02d 至 %04d-%02d；workers=%d；输出目录：%s",
        start[0], start[1], end[0], end[1], args.workers, output_dir,
    )
    LOGGER.info("变量：%s", ", ".join(VARIABLES))
    LOGGER.info("区域：[N %.1f, W %.1f, S %.1f, E %.1f]", *AREA)

    periods = list(iter_months(start, end))
    for year, month in periods:
        normalise_legacy_archive(output_dir, year, month)

    pending = [
        (year, month)
        for year, month in periods
        if not month_is_complete(output_dir, year, month)
    ]
    LOGGER.info("总月份：%d；待下载：%d；已完成/跳过：%d", len(periods), len(pending), len(periods) - len(pending))

    print(
        f"Downloading {start[0]:04d}-{start[1]:02d} "
        f"through {end[0]:04d}-{end[1]:02d}"
    )
    print(f"Output directory: {output_dir}")
    print(f"Workers: {args.workers} | Pending months: {len(pending)}")

    if not pending:
        print("所有月份均已有有效 NetCDF 文件，无需下载。")
        LOGGER.info("没有待下载月份，任务结束。")
        return

    progress = ProgressDisplay(args.workers, len(pending))
    results: list[tuple[int, int, str, str]] = []
    future_map = {}
    next_index = 0

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            for slot in range(min(args.workers, len(pending))):
                year, month = pending[next_index]
                next_index += 1
                future = executor.submit(
                    download_month,
                    slot,
                    output_dir,
                    year,
                    month,
                    progress,
                )
                future_map[future] = (slot, year, month)

            while future_map:
                progress.render()
                completed_futures = [
                    future for future in future_map if future.done()
                ]
                if not completed_futures:
                    time.sleep(0.5)
                    continue

                for future in completed_futures:
                    slot, year, month = future_map.pop(future)
                    try:
                        results.append(future.result())
                    except Exception as exc:  # noqa: BLE001 - 防止单个月份中断全局
                        error_text = f"{type(exc).__name__}: {exc}"
                        progress.finish(slot, "fail", error_text)
                        results.append((year, month, "failed", error_text))

                    if next_index < len(pending):
                        next_year, next_month = pending[next_index]
                        next_index += 1
                        next_future = executor.submit(
                            download_month,
                            slot,
                            output_dir,
                            next_year,
                            next_month,
                            progress,
                        )
                        future_map[next_future] = (slot, next_year, next_month)
    except KeyboardInterrupt:
        for future in future_map:
            future.cancel()
        print("\n用户中断下载；已完成的文件会保留，下次运行将自动跳过。")
        LOGGER.warning("用户中断下载；已完成的文件会保留。")
        raise
    finally:
        progress.render()
        progress.clear()

    failed = [item for item in results if item[2] == "failed"]
    downloaded = [item for item in results if item[2] == "downloaded"]
    print(
        f"下载结束：成功 {len(downloaded)} 个，失败 {len(failed)} 个，"
        f"共 {len(pending)} 个月。"
    )
    LOGGER.info("下载结束：成功 %d 个，失败 %d 个，共 %d 个月。", len(downloaded), len(failed), len(pending))
    for year, month, _, detail in failed:
        print(f"[ERROR] {year}-{month:02d}: {detail}")
        LOGGER.error("[%04d-%02d] %s", year, month, detail)
    LOGGER.info("========== ERA5 下载任务结束 ==========")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
