"""
下载 MODIS MCD12Q1 Collection 6.1 (V061) 土地覆盖产品。

默认下载：
    - 年份：2003、2022
    - 范围：105–116.5°E、30.5–40°N
    - MODIS 瓦片：h26v05、h27v05、h28v05
    - 图层：原始 HDF-EOS2 文件中包含 LC_Type1 和 QC

认证：NASA Earthdata Login。脚本使用 earthaccess 完成登录、CMR 搜索和
带 Bearer Token 的请求会话，然后自行流式下载，以便显示实时速度和进度。

用法：
    python download_mcd12q1.py --dry-run
    python download_mcd12q1.py
    python download_mcd12q1.py --years 2003 2022 --workers 2
    python download_mcd12q1.py --check-only
    python download_mcd12q1.py --overwrite

中断后再次运行会从 .filepart 继续下载。只有通过文件大小校验的文件才会
被视为完成；原始文件保存到 data_raw/modis/MCD12Q1/V061/。
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

try:
    import earthaccess
    import requests
except ImportError as exc:
    print(
        "请先安装依赖：python -m pip install earthaccess requests",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


# ---------------------------------------------------------------------------
# 项目路径和数据配置
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SHORT_NAME = "MCD12Q1"
VERSION = "061"
PRODUCT_LABEL = "MODIS MCD12Q1 V061"

OUTPUT_DIR = PROJECT_ROOT / "data_raw" / "modis" / "MCD12Q1" / "V061"
LOG_DIR = PROJECT_ROOT / "logs" / "download" / "modis"
LOG_FILE = LOG_DIR / "download_mcd12q1.log"
FAILED_FILE = PROJECT_ROOT / "download_script" / "modis" / "failed_mcd12q1.txt"

# 研究区：105–116.5°E、30.5–40°N
BBOX = (105.0, 30.5, 116.5, 40.0)

# 该范围覆盖的 500 m MODIS Sinusoidal tiles。
TILES = ("h26v05", "h27v05", "h28v05")
DEFAULT_YEARS = (2003, 2022)

DEFAULT_WORKERS = 2
CHUNK_SIZE = 4 * 1024 * 1024
RETRY_MAX = 4
RETRY_DELAY = 5
REFRESH_INTERVAL = 0.5
USER_AGENT = "Flash_Drought_Green/1.0 MCD12Q1-V061-downloader"

FILENAME_RE = re.compile(
    r"(?P<name>MCD12Q1\.A(?P<doy>\d{7})\.(?P<tile>h\d{2}v\d{2})\.061\.\d+\.hdf)"
)


@dataclass(frozen=True)
class RemoteFile:
    name: str
    url: str
    year: int
    tile: str
    expected_size: int | None = None


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("mcd12q1")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


# ---------------------------------------------------------------------------
# 终端实时进度卡片
# ---------------------------------------------------------------------------


class ProgressDisplay:
    """显示 worker、单文件进度、总进度、瞬时速度和 ETA。"""

    BOX_WIDTH = 88
    BAR_WIDTH = 24
    COLORS = {
        "clear": "\033[2K",
        "up": "\033[A",
        "hide": "\033[?25l",
        "show": "\033[?25h",
        "bold": "\033[1m",
        "dim": "\033[2m",
        "green": "\033[32m",
        "cyan": "\033[36m",
        "yellow": "\033[33m",
        "red": "\033[31m",
        "white": "\033[97m",
        "reset": "\033[0m",
    }

    def __init__(
        self,
        files: list[RemoteFile],
        workers: int,
        overwrite: bool,
    ) -> None:
        self.workers_count = workers
        self.total_files = len(files)
        self.lock = threading.Lock()
        self.start = time.monotonic()
        self.rendered_lines = 0
        self.completed = 0
        self.failed = 0
        self.initial_bytes = 0
        self.workers: dict[int, dict[str, Any]] = {}
        self.states: dict[str, dict[str, Any]] = {}

        for item in files:
            path = OUTPUT_DIR / item.name
            part = Path(f"{path}.filepart")
            complete_size = path.stat().st_size if path.is_file() else 0
            part_size = part.stat().st_size if part.is_file() else 0

            if overwrite:
                received = 0
                status = "等待中"
            elif is_complete(path, item.expected_size):
                received = complete_size
                status = "done"
            else:
                received = part_size
                status = "等待中"

            if status == "done":
                self.completed += 1
            self.initial_bytes += received
            self.states[item.name] = {
                "received": received,
                "total": item.expected_size,
                "status": status,
                "base": received,
            }

        _enable_ansi()

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
            char = text[index]
            if not unicodedata.combining(char):
                width += 2 if unicodedata.east_asian_width(char) in "WF" else 1
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

    def begin(self, slot: int, name: str, received: int, total: int | None) -> None:
        with self.lock:
            self.states[name].update(
                {"received": received, "total": total, "status": "下载中"}
            )
            self.workers[slot] = {
                "name": name,
                "pct": received / total * 100 if total else 0,
                "speed": 0.0,
                "downloaded": 0,
                "status": "下载中",
                "last_bytes": received,
                "last_time": time.monotonic(),
            }

    def update(self, slot: int, received: int) -> None:
        now = time.monotonic()
        with self.lock:
            worker = self.workers[slot]
            state = self.states[worker["name"]]
            elapsed = now - worker["last_time"]
            if elapsed >= 0.2:
                worker["speed"] = max(0, received - worker["last_bytes"]) / elapsed
                worker["last_bytes"] = received
                worker["last_time"] = now
            state["received"] = received
            total = state["total"]
            worker["pct"] = min(received / total * 100, 100.0) if total else 0
            worker["downloaded"] = max(0, received - state["base"])

    def set_total(self, name: str, total: int | None) -> None:
        if total is not None:
            with self.lock:
                self.states[name]["total"] = total

    def finish(self, slot: int, name: str, status: str, received: int) -> None:
        with self.lock:
            state = self.states[name]
            state.update({"received": received, "status": status})
            worker = self.workers.setdefault(slot, {"name": name, "speed": 0.0})
            worker.update(
                {
                    "name": name,
                    "pct": 100.0 if status == "done" else 0.0,
                    "downloaded": max(0, received - state["base"]),
                    "speed": 0.0,
                    "status": status,
                }
            )
            if status == "done":
                self.completed += 1
            elif status == "fail":
                self.failed += 1

    def mark_idle(self, slot: int) -> None:
        with self.lock:
            self.workers[slot] = {
                "name": "",
                "pct": 0,
                "speed": 0,
                "downloaded": 0,
                "status": "idle",
            }

    def render(self) -> None:
        c = self.COLORS
        with self.lock:
            workers = {slot: dict(value) for slot, value in self.workers.items()}
            states = {name: dict(value) for name, value in self.states.items()}
            completed, failed = self.completed, self.failed

        total_size = sum(value["total"] or 0 for value in states.values())
        known_size = all(value["total"] for value in states.values())
        received = sum(value["received"] for value in states.values())
        run_bytes = max(0, received - self.initial_bytes)
        elapsed = max(time.monotonic() - self.start, 0.001)
        speed = run_bytes / elapsed
        percent = (
            received / total_size * 100
            if known_size and total_size
            else completed / self.total_files * 100
            if self.total_files
            else 100
        )
        eta = (
            max(0, total_size - received) / speed
            if known_size and speed > 0
            else -1
        )
        active = sum(
            value.get("status") == "下载中" for value in workers.values()
        )

        lines = [f"┌{'─' * self.BOX_WIDTH}┐"]
        title = (
            f"{c['bold']}{c['white']} {PRODUCT_LABEL} 并行下载 "
            f"×{self.workers_count}{c['reset']}"
        )
        padding = self.BOX_WIDTH - self._visible_width(title)
        lines.append(
            f"│{' ' * (padding // 2)}{title}"
            f"{' ' * (padding - padding // 2)}│"
        )
        lines.append(f"├{'─' * self.BOX_WIDTH}┤")

        for slot in range(self.workers_count):
            worker = workers.get(slot)
            if not worker or worker.get("status") == "idle":
                row = f"{c['dim']}[空闲]{c['reset']}"
            else:
                name = worker["name"][-30:].ljust(30)
                pct = worker.get("pct", 0)
                if worker.get("status") == "done":
                    tag = f"{c['green']}✓ 完成{c['reset']}"
                elif worker.get("status") == "fail":
                    tag = f"{c['red']}✗ 失败{c['reset']}"
                else:
                    tag = (
                        f"{c['yellow']}↓ "
                        f"{worker.get('speed', 0) / 1024**2:.1f} MB/s{c['reset']}"
                    )
                row = (
                    f"{c['cyan']}{name}{c['reset']} {self._bar(pct)} "
                    f"{c['white']}{pct:5.1f}%{c['reset']}  "
                    f"{self.format_size(worker.get('downloaded', 0)):>10}  {tag}"
                )
            lines.append(self._content_line(row))

        lines.append(f"├{'─' * self.BOX_WIDTH}┤")
        if known_size and total_size:
            overall = (
                f"{self._bar(percent)} {min(percent, 100):5.1f}%  "
                f"{self.format_size(received)}/{self.format_size(total_size)}"
            )
        else:
            overall = (
                f"{self._bar(percent)} {min(percent, 100):5.1f}%  "
                f"文件 {completed}/{self.total_files}"
            )
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


def _enable_ansi() -> None:
    """在 Windows 控制台启用 VT100；失败时不影响下载。"""
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


# ---------------------------------------------------------------------------
# Earthdata 搜索和元数据解析
# ---------------------------------------------------------------------------


def extract_filename(granule: Any) -> str | None:
    """从 CMR granule 元数据或下载链接中提取 HDF 文件名。"""
    try:
        granule_ur = granule.get("umm", {}).get("GranuleUR", "")
        match = FILENAME_RE.search(str(granule_ur))
        if match:
            return match.group("name")
    except Exception:
        pass

    try:
        for link in granule.data_links():
            match = FILENAME_RE.search(str(link))
            if match:
                return match.group("name")
    except Exception:
        pass
    return None


def extract_download_url(granule: Any, filename: str) -> str | None:
    """从 granule 的 links 中选择原始 HDF 下载链接。"""
    try:
        links = granule.data_links()
    except Exception:
        return None

    for link in links:
        url = str(link)
        if filename in url and url.lower().split("?", 1)[0].endswith(".hdf"):
            return url
    for link in links:
        url = str(link)
        if filename in url:
            return url
    return None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def extract_expected_size(granule: Any, filename: str) -> int | None:
    """兼容不同 UMM 元数据结构，读取文件大小；下载时会再次用 HTTP 校验。"""
    try:
        umm = granule.get("umm", {})
    except Exception:
        return None

    # 优先读取与当前文件绑定的 SizeInBytes。UMM 中的 Size/SizeUnit
    # 可能是四舍五入后的 MB/GB，不能直接拿来做精确文件校验。
    exact_candidates: list[Any] = []
    fallback_candidates: list[Any] = []
    archive_info = umm.get("ArchiveAndDistributionInformation", [])
    if isinstance(archive_info, dict):
        archive_info = [archive_info]
    if isinstance(archive_info, list):
        for item in archive_info:
            if not isinstance(item, dict):
                continue
            exact_candidates.extend(
                [
                    item.get("SizeInBytes"),
                    item.get("FileSizeBytes"),
                ]
            )
            fallback_candidates.extend([item.get("Size")])
            for file_info in item.get("FileDistributionInformation", []) or []:
                if isinstance(file_info, dict):
                    name = str(file_info.get("Name", ""))
                    if not name or filename in name:
                        exact_candidates.extend(
                            [
                                file_info.get("SizeInBytes"),
                                file_info.get("FileSizeBytes"),
                            ]
                        )

    for value in exact_candidates:
        size = _positive_int(value)
        if size:
            return size
    # 某些旧元数据只有 Size，没有 SizeUnit；仅在这种情况下把它当作
    # 字节处理。带 MB/GB 单位的近似值留给 HTTP Content-Length 校验。
    for value in fallback_candidates:
        size = _positive_int(value)
        if size:
            return size
    return None


def extract_tile(filename: str) -> str | None:
    match = FILENAME_RE.search(filename)
    return match.group("tile") if match else None


def search_granules(years: tuple[int, ...], logger: logging.Logger) -> list[RemoteFile]:
    """按年份、研究区搜索，并严格过滤目标瓦片。"""
    remote_by_name: dict[str, RemoteFile] = {}
    target_tiles = set(TILES)

    for year in years:
        start = f"{year}-01-01"
        end = f"{year}-12-31"
        logger.info("搜索 %s：%s 至 %s", PRODUCT_LABEL, start, end)
        granules = earthaccess.search_data(
            short_name=SHORT_NAME,
            version=VERSION,
            temporal=(start, end),
            bounding_box=BBOX,
            count=-1,
        )
        logger.info("%d 年 CMR 返回 %d 个 granules", year, len(granules))

        for granule in granules:
            filename = extract_filename(granule)
            if not filename:
                logger.warning("无法从 granule 提取文件名，已跳过")
                continue
            tile = extract_tile(filename)
            if tile not in target_tiles:
                continue
            url = extract_download_url(granule, filename)
            if not url:
                logger.warning("无法获取下载链接：%s", filename)
                continue
            remote_by_name[filename] = RemoteFile(
                name=filename,
                url=url,
                year=year,
                tile=tile,
                expected_size=extract_expected_size(granule, filename),
            )

    result = sorted(
        remote_by_name.values(), key=lambda item: (item.year, item.tile, item.name)
    )
    logger.info("匹配目标文件：%d 个", len(result))
    for year in years:
        year_files = [item for item in result if item.year == year]
        logger.info(
            "%d 年：%s",
            year,
            ", ".join(item.tile for item in year_files) or "无",
        )
    return result


# ---------------------------------------------------------------------------
# 文件校验和下载
# ---------------------------------------------------------------------------


def is_complete(path: Path, expected_size: int | None) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    return expected_size is None or path.stat().st_size == expected_size


def download_one(
    slot: int,
    remote: RemoteFile,
    session: requests.Session,
    logger: logging.Logger,
    progress: ProgressDisplay,
    overwrite: bool = False,
) -> tuple[str, str, int, str]:
    """流式下载一个 HDF，支持 Range 断点续传和文件大小校验。"""
    output_path = OUTPUT_DIR / remote.name
    part_path = Path(f"{output_path}.filepart")

    if is_complete(output_path, remote.expected_size) and not overwrite:
        progress.finish(slot, remote.name, "done", output_path.stat().st_size)
        return remote.name, "skipped", output_path.stat().st_size, "已存在且大小正确"

    if output_path.exists() and not overwrite and not is_complete(
        output_path, remote.expected_size
    ):
        progress.finish(slot, remote.name, "fail", output_path.stat().st_size)
        return (
            remote.name,
            "failed",
            output_path.stat().st_size,
            "已有文件大小不匹配；如需覆盖请使用 --overwrite",
        )

    if overwrite:
        output_path.unlink(missing_ok=True)
        part_path.unlink(missing_ok=True)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, RETRY_MAX + 1):
        response: requests.Response | None = None
        try:
            existing_size = part_path.stat().st_size if part_path.exists() else 0
            progress.begin(slot, remote.name, existing_size, remote.expected_size)

            headers = {
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "identity",
                "Connection": "keep-alive",
            }
            if existing_size > 0:
                headers["Range"] = f"bytes={existing_size}-"

            response = session.get(
                remote.url,
                headers=headers,
                stream=True,
                timeout=(30, 120),
            )
            response.raise_for_status()

            status = response.status_code
            resumed = existing_size > 0 and status == 206
            if existing_size > 0 and not resumed:
                logger.warning("%s 不支持 Range，重新开始下载", remote.name)
                existing_size = 0

            content_length = response.headers.get("Content-Length")
            if remote.expected_size is not None:
                total_size = remote.expected_size
            elif content_length:
                total_size = (
                    existing_size + int(content_length)
                    if resumed
                    else int(content_length)
                )
            else:
                total_size = None
            progress.set_total(remote.name, total_size)

            mode = "ab" if resumed else "wb"
            downloaded_size = existing_size
            with part_path.open(mode) as output:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    output.write(chunk)
                    downloaded_size += len(chunk)
                    progress.update(slot, downloaded_size)

            actual_size = part_path.stat().st_size
            if actual_size <= 0:
                raise OSError("下载后文件为空")
            if total_size is not None and actual_size != total_size:
                raise OSError(
                    f"文件大小不符：实际 {actual_size}，预期 {total_size}"
                )

            part_path.replace(output_path)
            progress.finish(slot, remote.name, "done", actual_size)
            return remote.name, "downloaded", actual_size, "完成并通过大小校验"

        except (requests.RequestException, OSError, IOError, ValueError) as exc:
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                if exc.response.status_code == 416 and part_path.exists():
                    logger.warning(
                        "%s 断点无效，删除 .filepart 后重新下载", remote.name
                    )
                    part_path.unlink(missing_ok=True)
            if attempt < RETRY_MAX:
                logger.warning(
                    "%s 第 %d/%d 次失败：%s；%d 秒后重试",
                    remote.name,
                    attempt,
                    RETRY_MAX,
                    exc,
                    RETRY_DELAY,
                )
                time.sleep(RETRY_DELAY)
            else:
                received = part_path.stat().st_size if part_path.exists() else 0
                progress.finish(slot, remote.name, "fail", received)
                return remote.name, "failed", 0, str(exc)
        finally:
            if response is not None:
                response.close()

    progress.finish(slot, remote.name, "fail", 0)
    return remote.name, "failed", 0, "未知错误"


def check_only(files: list[RemoteFile], logger: logging.Logger) -> int:
    missing = 0
    invalid = 0
    for remote in files:
        path = OUTPUT_DIR / remote.name
        if not path.exists():
            missing += 1
            logger.warning("缺失：%s", remote.name)
        elif not is_complete(path, remote.expected_size):
            invalid += 1
            logger.warning(
                "大小不匹配：%s（本地 %d bytes，预期 %s）",
                remote.name,
                path.stat().st_size,
                remote.expected_size if remote.expected_size else "未知",
            )

    logger.info(
        "检查完成：应有 %d 个，缺失 %d 个，大小不匹配 %d 个",
        len(files),
        missing,
        invalid,
    )
    return 0 if missing == 0 and invalid == 0 else 1


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="下载 MODIS MCD12Q1 V061 年度土地覆盖 HDF-EOS2 文件"
    )
    parser.add_argument(
        "--years",
        nargs="+",
        type=int,
        default=list(DEFAULT_YEARS),
        help="下载年份，默认：2003 2022；不会自动下载中间年份",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"并行下载数，默认 {DEFAULT_WORKERS}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只搜索并列出文件，不下载",
    )
    parser.add_argument(
        "--list-tiles",
        action="store_true",
        help="列出搜索结果中的瓦片分布",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只检查目标文件是否存在且大小正确",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖已有文件，并删除对应断点文件",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logger = setup_logging()

    years = tuple(sorted(set(args.years)))
    current_year = date.today().year
    if not years or any(year < 2001 or year > current_year for year in years):
        logger.error("年份必须在 2001 至 %d 之间", current_year)
        return 2
    if args.workers < 1:
        logger.error("--workers 必须大于等于 1")
        return 2

    logger.info("%s 下载器启动", PRODUCT_LABEL)
    logger.info("目标年份：%s（仅下载这些年份）", ", ".join(map(str, years)))
    logger.info("目标范围：%s", BBOX)
    logger.info("目标 tiles：%s", ", ".join(TILES))
    logger.info("保存目录：%s", OUTPUT_DIR)

    try:
        logger.info("登录 NASA Earthdata...")
        auth = earthaccess.login()
        if not auth or not getattr(auth, "authenticated", True):
            logger.error("Earthdata 登录失败，请检查账号或 .netrc")
            logger.error("注册地址：https://urs.earthdata.nasa.gov/")
            return 1
        logger.info("Earthdata 登录成功")

        remote_files = search_granules(years, logger)
    except Exception as exc:
        logger.exception("Earthdata 搜索失败：%s", exc)
        return 1

    expected_names = {
        f"{year}-{tile}" for year in years for tile in TILES
    }
    actual_names = {f"{item.year}-{item.tile}" for item in remote_files}
    missing_targets = sorted(expected_names - actual_names)
    if missing_targets:
        logger.warning("搜索结果缺少目标年份/瓦片：%s", ", ".join(missing_targets))

    if args.list_tiles:
        for year in years:
            names = [item.name for item in remote_files if item.year == year]
            logger.info("%d 年：%s", year, ", ".join(names) or "无")
        return 0

    if not remote_files:
        logger.error("未找到匹配数据，请检查 Earthdata 登录、时间范围和瓦片")
        return 1

    if args.dry_run:
        for remote in remote_files:
            size = (
                ProgressDisplay.format_size(remote.expected_size)
                if remote.expected_size
                else "未知"
            )
            logger.info("[DRY RUN] %s | %s | %s", remote.name, remote.tile, size)
        return 0

    if args.check_only:
        return check_only(remote_files, logger)

    pending = [
        remote
        for remote in remote_files
        if args.overwrite
        or not is_complete(OUTPUT_DIR / remote.name, remote.expected_size)
    ]
    skipped = len(remote_files) - len(pending)
    if not pending:
        logger.info("所有目标文件已存在且大小正确，无需下载")
        return 0

    try:
        session = earthaccess.get_requests_https_session()
    except Exception as exc:
        logger.exception("创建 Earthdata 下载会话失败：%s", exc)
        return 1

    progress = ProgressDisplay(remote_files, args.workers, args.overwrite)
    success = 0
    failed = 0
    total_bytes = 0
    pending_iter = iter(pending)
    futures: dict[Any, tuple[int, RemoteFile]] = {}

    print(
        f"待下载文件：{len(pending)} 个 | 总文件：{len(remote_files)} 个 | "
        f"并发数：{args.workers}"
    )
    sys.stdout.write(ProgressDisplay.COLORS["hide"])
    sys.stdout.flush()
    progress.render()

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            for slot in range(args.workers):
                try:
                    remote = next(pending_iter)
                except StopIteration:
                    break
                futures[executor.submit(
                    download_one,
                    slot,
                    remote,
                    session,
                    logger,
                    progress,
                    args.overwrite,
                )] = (slot, remote)

            while futures:
                progress.render()
                time.sleep(REFRESH_INTERVAL)
                done = [future for future in futures if future.done()]

                for future in done:
                    slot, remote = futures.pop(future)
                    try:
                        name, status, size, message = future.result()
                    except Exception as exc:
                        name, status, size, message = remote.name, "failed", 0, str(exc)
                        progress.finish(slot, remote.name, "fail", 0)
                        logger.exception("工作线程异常：%s", remote.name)

                    total_bytes += size
                    if status == "downloaded":
                        success += 1
                    elif status == "skipped":
                        skipped += 1
                    else:
                        failed += 1
                        logger.error("失败：%s；%s", name, message)

                    try:
                        next_remote = next(pending_iter)
                    except StopIteration:
                        progress.mark_idle(slot)
                    else:
                        futures[executor.submit(
                            download_one,
                            slot,
                            next_remote,
                            session,
                            logger,
                            progress,
                            args.overwrite,
                        )] = (slot, next_remote)
    except KeyboardInterrupt:
        logger.warning("用户中断下载；保留 .filepart 以便下次断点续传")
        for future in futures:
            future.cancel()
        failed += len(futures)
    finally:
        progress.clear()
        sys.stdout.write(ProgressDisplay.COLORS["show"])
        sys.stdout.flush()

    if failed:
        FAILED_FILE.write_text(
            "\n".join(
                remote.name
                for remote in remote_files
                if not is_complete(OUTPUT_DIR / remote.name, remote.expected_size)
            ),
            encoding="utf-8",
        )
        logger.warning("失败或未完成文件列表已保存：%s", FAILED_FILE)

    logger.info(
        "任务结束：下载 %d，跳过 %d，失败 %d，总计 %.2f GB",
        success,
        skipped,
        failed,
        total_bytes / 1024**3,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
