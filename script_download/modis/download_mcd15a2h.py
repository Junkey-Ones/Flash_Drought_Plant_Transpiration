"""下载 MODIS MCD15A2H Collection 6.1 LAI/FPAR 产品。

默认研究区为 102–115°E、33.5–41.5°N，对应的 MODIS Sinusoidal 瓦片为
``h25v04、h25v05、h26v04、h26v05、h27v05``。下载器只负责从 NASA
Earthdata 检索并保存原始 HDF 文件，不执行裁剪、QA 或重采样。

认证：NASA Earthdata Login。脚本使用 ``earthaccess`` 完成登录和 CMR
检索，使用带 Bearer Token 的 requests 会话流式下载。

常用用法（请使用项目虚拟环境运行）：

    python download_mcd15a2h.py --dry-run
    python download_mcd15a2h.py --list-tiles
    python download_mcd15a2h.py --check-only
    python download_mcd15a2h.py --workers 4
    python download_mcd15a2h.py --start 2002-01-01 --end 2023-12-31

下载运行时按 ``Q/q`` 可安全终止；``Ctrl+C`` 可作为备用中断。中断后再次
运行会从 ``.filepart`` 继续；已有完整文件默认跳过，覆盖已有文件必须显式
传入 ``--overwrite``。
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

SHORT_NAME = "MCD15A2H"
VERSION = "061"
PRODUCT_LABEL = "MODIS MCD15A2H V061"

OUTPUT_DIR = PROJECT_ROOT / "data_raw" / "modis" / "MCD15A2H" / "V061"
LOG_DIR = PROJECT_ROOT / "logs" / "download" / "modis"
LOG_FILE_NAME = "download_mcd15a2h.log"
FAILED_FILE_NAME = "failed_mcd15a2h.txt"

# 修正后的研究区：102–115°E、33.5–41.5°N。
BBOX = (102.0, 33.5, 115.0, 41.5)

# 对该范围有空间相交的 MCD15A2H 瓦片。
TILES = ("h25v04", "h25v05", "h26v04", "h26v05", "h27v05")

# MCD15A2H 的现有产品记录从 2002 年开始；主体研究时段到 2023 年。
DEFAULT_START_DATE = "2002-01-01"
DEFAULT_END_DATE = "2023-12-31"

DEFAULT_WORKERS = 4
CHUNK_SIZE = 4 * 1024 * 1024
RETRY_MAX = 4
RETRY_DELAY = 5
REFRESH_INTERVAL = 0.5
USER_AGENT = "Flash_Drought_Plant_Transpiration/1.0 MCD15A2H-V061-downloader"

FILENAME_RE = re.compile(
    r"(?P<name>MCD15A2H\.A(?P<doy>\d{7})\.(?P<tile>h\d{2}v\d{2})\.061\.\d+\.hdf)(?:$|\?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RemoteFile:
    """一个待下载的 CMR granule。"""

    name: str
    url: str
    year: int
    tile: str
    expected_size: int | None = None


# ---------------------------------------------------------------------------
# 日志和参数校验
# ---------------------------------------------------------------------------


def setup_logging(log_dir: Path) -> logging.Logger:
    """创建 UTF-8 文件日志和终端日志。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("mcd15a2h")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(log_dir / LOG_FILE_NAME, encoding="utf-8")
    file_handler.setFormatter(formatter)
    # 卡片写 stdout；普通日志写 stderr，避免刷新卡片时覆盖其边框。
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def parse_date(value: str) -> date:
    """解析 ISO 日期，供 argparse 使用。"""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"日期必须为 YYYY-MM-DD：{value}"
        ) from exc


def resolve_directory(value: str | None, default: Path) -> Path:
    """将目录解析为绝对路径并创建目录。"""
    path = Path(value).expanduser() if value else default
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_bbox(values: list[float]) -> tuple[float, float, float, float]:
    if len(values) != 4:
        raise argparse.ArgumentTypeError("--bbox 需要 west south east north 四个值")
    west, south, east, north = values
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise argparse.ArgumentTypeError("--bbox 范围无效")
    return west, south, east, north


# ---------------------------------------------------------------------------
# CMR granule 元数据解析
# ---------------------------------------------------------------------------


def _get_umm(granule: Any) -> dict[str, Any]:
    try:
        umm = granule.get("umm", {})
    except (AttributeError, TypeError):
        return {}
    return umm if isinstance(umm, dict) else {}


def extract_filename(granule: Any) -> str | None:
    """从 CMR 元数据或下载链接提取 HDF 文件名。"""
    umm = _get_umm(granule)
    candidates = [str(umm.get("GranuleUR", ""))]
    try:
        links = granule.data_links()
    except (AttributeError, TypeError):
        links = []
    candidates.extend(str(link) for link in links)

    for candidate in candidates:
        match = FILENAME_RE.search(candidate)
        if match:
            return match.group("name")
    return None


def extract_tile(filename: str) -> str | None:
    match = FILENAME_RE.search(filename)
    return match.group("tile").lower() if match else None


def extract_download_url(granule: Any, filename: str) -> str | None:
    """选择原始 HDF 下载链接，跳过 XML/元数据链接。"""
    try:
        links = granule.data_links()
    except (AttributeError, TypeError):
        return None
    for link in links:
        url = str(link)
        clean_url = url.split("?", 1)[0].lower()
        if filename in url and clean_url.endswith(".hdf"):
            return url
    for link in links:
        url = str(link)
        if filename in url:
            return url
    return None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def extract_expected_size(granule: Any) -> int | None:
    """读取 UMM 中精确的 SizeInBytes；近似 Size 不用于硬校验。"""
    umm = _get_umm(granule)
    archive_info = umm.get("ArchiveAndDistributionInformation", [])
    if isinstance(archive_info, dict):
        archive_info = [archive_info]
    if not isinstance(archive_info, list):
        return None

    for item in archive_info:
        if not isinstance(item, dict):
            continue
        for key in ("SizeInBytes", "FileSizeBytes"):
            size = _positive_int(item.get(key))
            if size:
                return size
        file_info_list = item.get("FileDistributionInformation", [])
        if not isinstance(file_info_list, list):
            continue
        for file_info in file_info_list:
            if not isinstance(file_info, dict):
                continue
            for key in ("SizeInBytes", "FileSizeBytes"):
                size = _positive_int(file_info.get(key))
                if size:
                    return size
    return None


# ---------------------------------------------------------------------------
# CMR 搜索
# ---------------------------------------------------------------------------


def search_granules(
    start_date: str,
    end_date: str,
    bbox: tuple[float, float, float, float],
    tiles: tuple[str, ...],
    logger: logging.Logger,
) -> list[RemoteFile]:
    """检索日期范围内的 granule，并严格按目标瓦片去重过滤。"""
    logger.info("搜索 %s：%s 至 %s", PRODUCT_LABEL, start_date, end_date)
    logger.info("研究区 bbox：%s", bbox)
    logger.info("目标 tiles：%s", ", ".join(tiles))

    granules = earthaccess.search_data(
        short_name=SHORT_NAME,
        version=VERSION,
        temporal=(start_date, end_date),
        bounding_box=bbox,
        count=-1,
    )
    logger.info("CMR 返回 %d 个 granules", len(granules))

    target_tiles = set(tiles)
    remote_by_name: dict[str, RemoteFile] = {}
    missing_filename = 0
    missing_url = 0
    for granule in granules:
        filename = extract_filename(granule)
        if not filename:
            missing_filename += 1
            continue
        tile = extract_tile(filename)
        if tile not in target_tiles:
            continue
        match = FILENAME_RE.search(filename)
        if not match:
            continue
        url = extract_download_url(granule, filename)
        if not url:
            missing_url += 1
            logger.warning("无法获取下载链接：%s", filename)
            continue
        remote_by_name[filename] = RemoteFile(
            name=filename,
            url=url,
            year=int(match.group("doy")[:4]),
            tile=tile,
            expected_size=extract_expected_size(granule),
        )

    if missing_filename:
        logger.warning("%d 个 granule 无法提取文件名", missing_filename)
    if missing_url:
        logger.warning("%d 个文件无法提取下载链接", missing_url)

    result = sorted(
        remote_by_name.values(), key=lambda item: (item.year, item.tile, item.name)
    )
    logger.info("匹配目标文件：%d 个", len(result))
    for tile in tiles:
        count = sum(item.tile == tile for item in result)
        logger.info("  %s：%d 个", tile, count)
    return result


# ---------------------------------------------------------------------------
# 文件完整性和断点下载
# ---------------------------------------------------------------------------


def is_complete(path: Path, expected_size: int | None) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    return expected_size is None or path.stat().st_size == expected_size


class ProgressDisplay:
    """项目统一的并行下载终端卡片。"""

    BOX_WIDTH = 86
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
        output_dir: Path,
        overwrite: bool,
    ) -> None:
        self.workers_count = workers
        self.total_files = len(files)
        self.output_dir = output_dir
        self.lock = threading.Lock()
        self.start = time.monotonic()
        self.rendered_lines = 0
        self.completed = 0
        self.failed = 0
        self.cancelled = 0
        self.stop_requested = False
        self.initial_bytes = 0
        self.workers: dict[int, dict[str, Any]] = {}
        self.states: dict[str, dict[str, Any]] = {}
        self.interactive = bool(sys.stdout.isatty())
        self.last_plain_render = 0.0

        for item in files:
            path = output_dir / item.name
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

        if self.interactive and _enable_ansi():
            sys.stdout.write(self.COLORS["hide"])
            sys.stdout.flush()
        else:
            self.interactive = False

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
            elif status == "cancel":
                self.cancelled += 1

    def request_stop(self) -> None:
        with self.lock:
            self.stop_requested = True

    def mark_idle(self, slot: int) -> None:
        with self.lock:
            self.workers[slot] = {
                "name": "",
                "pct": 0,
                "speed": 0,
                "downloaded": 0,
                "status": "idle",
            }

    def _snapshot(
        self,
    ) -> tuple[dict[int, dict[str, Any]], dict[str, dict[str, Any]], int, int, int, bool]:
        with self.lock:
            return (
                {slot: dict(value) for slot, value in self.workers.items()},
                {name: dict(value) for name, value in self.states.items()},
                self.completed,
                self.failed,
                self.cancelled,
                self.stop_requested,
            )

    def render(self) -> None:
        workers, states, completed, failed, cancelled, stop_requested = self._snapshot()
        total_size = sum(value["total"] or 0 for value in states.values())
        known_size = bool(states) and all(value["total"] for value in states.values())
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
        eta = max(0, total_size - received) / speed if known_size and speed > 0 else -1
        active = sum(value.get("status") == "下载中" for value in workers.values())

        if not self.interactive:
            now = time.monotonic()
            if now - self.last_plain_render < 1.0 and completed < self.total_files:
                return
            self.last_plain_render = now
            print(
                f"[PROGRESS] {min(percent, 100):5.1f}% 文件 {completed}/{self.total_files} "
                f"速度 {speed / 1024**2:.1f} MB/s 活跃 {active}/{self.workers_count} "
                f"失败 {failed} 中断 {cancelled}",
                flush=True,
            )
            return

        c = self.COLORS
        lines = [f"┌{'─' * self.BOX_WIDTH}┐"]
        title = f"{c['bold']}{c['white']} {PRODUCT_LABEL} 并行下载 ×{self.workers_count}{c['reset']}"
        padding = self.BOX_WIDTH - self._visible_width(title)
        lines.append(f"│{' ' * (padding // 2)}{title}{' ' * (padding - padding // 2)}│")
        lines.append(f"├{'─' * self.BOX_WIDTH}┤")
        lines.append(self._content_line(f"{c['cyan']}输入源:{c['reset']} NASA Earthdata CMR"))
        lines.append(self._content_line(f"{c['cyan']}输出目录:{c['reset']} {self.output_dir}"))
        lines.append(f"├{'─' * self.BOX_WIDTH}┤")

        for slot in range(self.workers_count):
            worker = workers.get(slot)
            if not worker or worker.get("status") == "idle":
                row = f"{c['dim']}[空闲]{c['reset']}"
            else:
                name = worker["name"][-32:].ljust(32)
                pct = worker.get("pct", 0)
                if worker.get("status") == "done":
                    tag = f"{c['green']}PASS 完成{c['reset']}"
                elif worker.get("status") == "fail":
                    tag = f"{c['red']}FAIL 失败{c['reset']}"
                elif worker.get("status") == "cancel":
                    tag = f"{c['yellow']}WARN 中断{c['reset']}"
                else:
                    tag = f"{c['yellow']}RUN {worker.get('speed', 0) / 1024**2:.1f} MB/s{c['reset']}"
                row = (
                    f"{c['cyan']}{name}{c['reset']} {self._bar(pct)} "
                    f"{c['white']}{pct:5.1f}%{c['reset']}  "
                    f"{self.format_size(worker.get('downloaded', 0)):>10}  {tag}"
                )
            lines.append(self._content_line(row))

        lines.append(f"├{'─' * self.BOX_WIDTH}┤")
        if known_size and total_size:
            overall = f"{self._bar(percent)} {min(percent, 100):5.1f}%  {self.format_size(received)}/{self.format_size(total_size)}"
        else:
            overall = f"{self._bar(percent)} {min(percent, 100):5.1f}%  文件 {completed}/{self.total_files}"
        lines.append(self._content_line(f"{c['yellow']}总进度:{c['reset']} {overall}"))
        lines.append(self._content_line(
            f"{c['green']}速度:{c['reset']} {speed / 1024**2:.1f} MB/s  "
            f"{c['cyan']}已用:{c['reset']} {self.format_time(elapsed)}  "
            f"{c['yellow']}剩余:{c['reset']} {self.format_time(eta)}  "
            f"{c['yellow']}活跃:{c['reset']} {active}/{self.workers_count}  "
            f"{c['red']}失败:{c['reset']} {failed}  "
            f"{c['yellow']}中断:{c['reset']} {cancelled}"
        ))
        if stop_requested:
            lines.append(self._content_line(f"{c['yellow']}WARN:{c['reset']} 用户请求停止，正在保留断点文件"))
        else:
            lines.append(self._content_line(f"{c['dim']}操作:{c['reset']} 按 Q 停止；Ctrl+C 备用中断"))
        lines.append(f"└{'─' * self.BOX_WIDTH}┘")

        if self.rendered_lines:
            sys.stdout.write(c["up"] * self.rendered_lines)
        for line in lines:
            sys.stdout.write(f"{c['clear']}{line}\n")
        sys.stdout.flush()
        self.rendered_lines = len(lines)

    def clear(self) -> None:
        c = self.COLORS
        if self.interactive and self.rendered_lines:
            sys.stdout.write(c["up"] * self.rendered_lines)
            for _ in range(self.rendered_lines):
                sys.stdout.write(f"{c['clear']}\n")
            sys.stdout.flush()
            self.rendered_lines = 0
        if self.interactive:
            sys.stdout.write(c["show"])
            sys.stdout.flush()


def _enable_ansi() -> bool:
    """在 Windows 控制台启用 VT100；失败时返回 False。"""
    if os.name != "nt":
        return True
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return bool(ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception as exc:
        logging.getLogger("mcd15a2h").debug("Windows VT100 启用失败：%s", exc)
    return False


class DownloadCancelled(Exception):
    """用户请求安全终止当前下载。"""


class StopController:
    """在交互式终端监听 Q/q；非TTY环境由 Ctrl+C 负责中断。"""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.thread: threading.Thread | None = None
        self.enabled = bool(sys.stdin.isatty() and sys.stdout.isatty())

    def start(self) -> None:
        if not self.enabled:
            return
        self.thread = threading.Thread(
            target=self._listen,
            name="mcd15a2h-stop-listener",
            daemon=True,
        )
        self.thread.start()

    def _listen(self) -> None:
        if os.name == "nt":
            self._listen_windows()
        else:
            self._listen_posix()

    def _listen_windows(self) -> None:
        import msvcrt

        while not self.event.is_set():
            if msvcrt.kbhit():
                key = msvcrt.getwch()
                if key.lower() == "q":
                    self.event.set()
                    return
            time.sleep(0.1)

    def _listen_posix(self) -> None:
        import select

        while not self.event.is_set():
            ready, _, _ = select.select([sys.stdin], [], [], 0.1)
            if ready and sys.stdin.read(1).lower() == "q":
                self.event.set()
                return

    def requested(self) -> bool:
        return self.event.is_set()

    def stop(self) -> None:
        self.event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=0.5)


def download_one(
    remote: RemoteFile,
    output_dir: Path,
    session: requests.Session,
    logger: logging.Logger,
    progress: ProgressDisplay,
    slot: int,
    stop_event: threading.Event,
    overwrite: bool = False,
) -> tuple[str, str, int, str]:
    """流式下载一个 HDF，支持 Range 断点续传和大小校验。"""
    output_path = output_dir / remote.name
    part_path = Path(f"{output_path}.filepart")

    if stop_event.is_set():
        progress.finish(slot, remote.name, "cancel", 0)
        return remote.name, "cancelled", 0, "用户按 Q 终止"

    if is_complete(output_path, remote.expected_size) and not overwrite:
        progress.finish(slot, remote.name, "done", output_path.stat().st_size)
        return remote.name, "skipped", output_path.stat().st_size, "已有文件完整"
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

    output_dir.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, RETRY_MAX + 1):
        response: requests.Response | None = None
        try:
            if stop_event.is_set():
                raise DownloadCancelled("用户按 Q 终止")
            existing_size = part_path.stat().st_size if part_path.exists() else 0
            progress.begin(slot, remote.name, existing_size, remote.expected_size)
            headers = {
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "identity",
                "Connection": "keep-alive",
            }
            if existing_size:
                headers["Range"] = f"bytes={existing_size}-"

            response = session.get(
                remote.url,
                headers=headers,
                stream=True,
                timeout=(30, 120),
            )
            if response.status_code == 416:
                part_path.unlink(missing_ok=True)
                raise OSError("服务器拒绝断点范围，已删除 .filepart 并重试")
            response.raise_for_status()

            resumed = existing_size > 0 and response.status_code == 206
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
            received = existing_size
            with part_path.open(mode) as output:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if stop_event.is_set():
                        raise DownloadCancelled("用户按 Q 终止")
                    if chunk:
                        output.write(chunk)
                        received += len(chunk)
                        progress.update(slot, received)

            actual_size = part_path.stat().st_size
            if actual_size <= 0:
                raise OSError("下载后文件为空")
            if total_size is not None and actual_size != total_size:
                raise OSError(f"文件大小不符：实际 {actual_size}，预期 {total_size}")

            part_path.replace(output_path)
            progress.finish(slot, remote.name, "done", actual_size)
            logger.info("完成：%s（%.1f MB）", remote.name, actual_size / 1024**2)
            return remote.name, "downloaded", actual_size, "完成并通过大小校验"

        except DownloadCancelled as exc:
            received = part_path.stat().st_size if part_path.exists() else 0
            progress.finish(slot, remote.name, "cancel", received)
            logger.warning("用户按 Q 终止：%s；已保留 .filepart（%d bytes）", remote.name, received)
            return remote.name, "cancelled", received, str(exc)
        except (requests.RequestException, OSError, ValueError) as exc:
            if attempt < RETRY_MAX:
                logger.warning(
                    "%s 第 %d/%d 次失败：%s；%d 秒后重试",
                    remote.name,
                    attempt,
                    RETRY_MAX,
                    exc,
                    RETRY_DELAY,
                )
                if stop_event.wait(RETRY_DELAY):
                    progress.finish(slot, remote.name, "cancel", part_path.stat().st_size if part_path.exists() else 0)
                    return remote.name, "cancelled", part_path.stat().st_size if part_path.exists() else 0, "用户按 Q 终止"
            else:
                received = part_path.stat().st_size if part_path.exists() else 0
                progress.finish(slot, remote.name, "fail", received)
                logger.error("失败：%s；已保留 .filepart（%d bytes）", remote.name, received)
                return remote.name, "failed", received, str(exc)
        finally:
            if response is not None:
                response.close()

    progress.finish(slot, remote.name, "fail", 0)
    return remote.name, "failed", 0, "未知下载错误"


def download_batch(
    files: list[RemoteFile],
    output_dir: Path,
    failed_path: Path,
    workers: int,
    session: requests.Session,
    logger: logging.Logger,
    overwrite: bool,
) -> int:
    """并发下载并显示项目统一的终端卡片。"""
    total = len(files)
    completed = 0
    downloaded_bytes = 0
    failed: list[str] = []
    cancelled_files: list[str] = []
    started = time.monotonic()
    progress = ProgressDisplay(files, workers, output_dir, overwrite)
    stop_controller = StopController()
    stop_controller.start()
    pending = iter(files)
    futures: dict[Any, tuple[int, RemoteFile]] = {}

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for slot in range(workers):
                if stop_controller.requested():
                    progress.request_stop()
                    progress.mark_idle(slot)
                    break
                try:
                    remote = next(pending)
                except StopIteration:
                    progress.mark_idle(slot)
                    break
                futures[executor.submit(
                    download_one,
                    remote,
                    output_dir,
                    session,
                    logger,
                    progress,
                    slot,
                    stop_controller.event,
                    overwrite,
                )] = (slot, remote)

            while futures:
                if stop_controller.requested():
                    progress.request_stop()
                progress.render()
                time.sleep(REFRESH_INTERVAL)
                done = [future for future in futures if future.done()]
                for future in done:
                    slot, remote = futures.pop(future)
                    try:
                        _, status, size, message = future.result()
                    except Exception as exc:  # 线程边界：记录上下文后继续汇总
                        logger.exception("工作线程异常：%s", remote.name)
                        progress.finish(slot, remote.name, "fail", 0)
                        status, size, message = "failed", 0, str(exc)
                    completed += 1
                    downloaded_bytes += size if status == "downloaded" else 0
                    if status == "failed":
                        failed.append(remote.name)
                        logger.error("失败：%s；%s", remote.name, message)
                    elif status == "cancelled":
                        cancelled_files.append(remote.name)

                    if stop_controller.requested():
                        progress.request_stop()
                        progress.mark_idle(slot)
                    else:
                        try:
                            next_remote = next(pending)
                        except StopIteration:
                            progress.mark_idle(slot)
                        else:
                            futures[executor.submit(
                                download_one,
                                next_remote,
                                output_dir,
                                session,
                                logger,
                                progress,
                                slot,
                                stop_controller.event,
                                overwrite,
                            )] = (slot, next_remote)
    except KeyboardInterrupt:
        logger.warning("用户中断下载；保留 .filepart 以便下次断点续传")
        stop_controller.event.set()
        for future in futures:
            future.cancel()
        failed.extend(remote.name for _, remote in futures.values())
    finally:
        stopped = stop_controller.requested()
        stop_controller.stop()
        progress.clear()

    if failed:
        failed_path.parent.mkdir(parents=True, exist_ok=True)
        failed_path.write_text("\n".join(sorted(failed)), encoding="utf-8")
        logger.warning("失败列表已保存：%s", failed_path)
    elif failed_path.exists():
        failed_path.unlink()

    if cancelled_files:
        logger.warning(
            "用户终止后有 %d 个文件未完成；重新运行相同命令即可续传",
            len(cancelled_files),
        )

    elapsed = max(time.monotonic() - started, 1.0)
    logger.info(
        "任务结束：总计 %d，失败 %d，新增 %.2f GB，耗时 %.1f 分钟",
        total,
        len(failed),
        downloaded_bytes / 1024**3,
        elapsed / 60,
    )
    return 130 if stopped else (1 if failed else 0)


def check_only(files: list[RemoteFile], output_dir: Path, logger: logging.Logger) -> int:
    missing = 0
    invalid = 0
    for remote in files:
        path = output_dir / remote.name
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
        description="下载 MODIS MCD15A2H V061 LAI/FPAR 原始 HDF 文件"
    )
    parser.add_argument("--start", default=DEFAULT_START_DATE, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=DEFAULT_END_DATE, help="结束日期 YYYY-MM-DD")
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        default=list(BBOX),
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="研究区边界，默认 102 33.5 115 41.5",
    )
    parser.add_argument(
        "--tiles",
        nargs="+",
        default=list(TILES),
        help="目标 MODIS 瓦片，默认包含 h27v05",
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并发数，默认 4")
    parser.add_argument("--output-dir", help="输出目录，默认 data_raw/modis/MCD15A2H/V061")
    parser.add_argument("--log-dir", help="日志目录，默认 logs/download/modis")
    parser.add_argument("--dry-run", action="store_true", help="只搜索并列出文件，不下载")
    parser.add_argument("--list-tiles", action="store_true", help="列出搜索结果的瓦片分布")
    parser.add_argument("--check-only", action="store_true", help="只检查目标文件")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有文件并删除对应断点文件")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        start = parse_date(args.start)
        end = parse_date(args.end)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if start > end:
        parser.error("--start 不能晚于 --end")
    if args.workers < 1:
        parser.error("--workers 必须大于等于 1")
    try:
        bbox = validate_bbox(args.bbox)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    tiles = tuple(sorted({tile.lower() for tile in args.tiles}))
    if not tiles or any(not re.fullmatch(r"h\d{2}v\d{2}", tile) for tile in tiles):
        parser.error("--tiles 必须是 h##v## 格式")

    output_dir = resolve_directory(args.output_dir, OUTPUT_DIR)
    log_dir = resolve_directory(args.log_dir, LOG_DIR)
    logger = setup_logging(log_dir)
    logger.info("%s 下载器启动", PRODUCT_LABEL)
    logger.info("日期范围：%s 至 %s", start.isoformat(), end.isoformat())
    logger.info("输出目录：%s", output_dir)

    try:
        logger.info("登录 NASA Earthdata...")
        auth = earthaccess.login()
        if not auth or not getattr(auth, "authenticated", False):
            logger.error("Earthdata 登录失败，请检查账号或 .netrc")
            logger.error("注册地址：https://urs.earthdata.nasa.gov/")
            return 1
        logger.info("Earthdata 登录成功")
        files = search_granules(
            start.isoformat(), end.isoformat(), bbox, tiles, logger
        )
    except Exception as exc:
        logger.exception("Earthdata 检索失败：%s", exc)
        return 1

    if args.list_tiles:
        logger.info("搜索结果瓦片分布：")
        for tile in tiles:
            logger.info("  %s：%d 个", tile, sum(item.tile == tile for item in files))
        return 0
    if not files:
        logger.error("未找到匹配数据，请检查登录、日期范围和瓦片设置")
        return 1
    if args.dry_run:
        for remote in files:
            size = (
                f"{remote.expected_size / 1024**2:.1f} MB"
                if remote.expected_size
                else "未知大小"
            )
            logger.info("[DRY RUN] %s | %s | %s", remote.name, remote.tile, size)
        return 0
    if args.check_only:
        return check_only(files, output_dir, logger)

    pending = [
        remote
        for remote in files
        if args.overwrite
        or not is_complete(output_dir / remote.name, remote.expected_size)
    ]
    skipped = len(files) - len(pending)
    logger.info("待下载 %d 个，已存在并跳过 %d 个", len(pending), skipped)
    if not pending:
        logger.info("所有目标文件已存在且完整，无需下载")
        return 0

    try:
        session = earthaccess.get_requests_https_session()
    except Exception as exc:
        logger.exception("创建 Earthdata 下载会话失败：%s", exc)
        return 1

    return download_batch(
        pending,
        output_dir,
        log_dir / FAILED_FILE_NAME,
        args.workers,
        session,
        logger,
        args.overwrite,
    )


if __name__ == "__main__":
    raise SystemExit(main())
