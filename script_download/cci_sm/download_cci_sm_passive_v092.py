"""
下载 ESA CCI Soil Moisture PASSIVE v09.2 日尺度产品。

数据目录：
https://data.ceda.ac.uk/neodc/esacci/soil_moisture/data/daily_files/PASSIVE/v09.2

默认下载时段：2001-01-01 至 2024-12-31
默认保存目录：data_raw/esa_cci_sm/passive/v09.2/

特点：
1. 通过 CEDA 的 JSON directory listing 获取文件清单，不依赖手工整理文件名。
2. 按文件名日期筛选时间范围。
3. 已完整下载的文件自动跳过。
4. 中断文件保存为 .filepart，重新运行时使用 HTTP Range 断点续传。
5. 下载完成后按目录清单中的文件大小进行校验。
6. 终端实时显示总进度、总体速度、当前文件名和当前文件进度。

用法示例：
    python download_cci_sm_passive_v092.py --dry-run
    python download_cci_sm_passive_v092.py
    python download_cci_sm_passive_v092.py --start 2010-01-01 --end 2012-12-31
    python download_cci_sm_passive_v092.py --check-only
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import (
    Request,
    ProxyHandler,
    build_opener,
)


# ---------------------------------------------------------------------------
# 项目路径和数据源配置
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]

BASE_URL = (
    "https://data.ceda.ac.uk/neodc/esacci/soil_moisture/"
    "data/daily_files/PASSIVE/v09.2"
)
ARCHIVE_HOST = "https://data.ceda.ac.uk"

OUTPUT_DIR = PROJECT_ROOT / "data_raw" / "esa_cci_sm" / "passive" / "v09.2"
LOG_DIR = PROJECT_ROOT / "logs" / "download" / "cci_sm"
LOG_FILE = LOG_DIR / "download_cci_sm_passive_v092.log"

DEFAULT_START = date(2001, 1, 1)
DEFAULT_END = date(2024, 12, 31)
DEFAULT_WORKERS = 6
CHUNK_SIZE = 4 * 1024 * 1024
RETRY_MAX = 4
RETRY_DELAY = 3
REFRESH_INTERVAL = 0.5
USER_AGENT = "Flash_Drought_Green/1.1 ESA-CCI-SM-downloader"

# 强制直连 CEDA，不读取 HTTP_PROXY、HTTPS_PROXY 等代理环境变量。
# 这样即使系统当前开启了代理，下载器仍使用原始网络连接。
DIRECT_OPENER = build_opener(ProxyHandler({}))

FILE_RE = re.compile(
    r"^ESACCI-SOILMOISTURE-L3S-SSMV-PASSIVE-"
    r"(?P<yyyymmdd>\d{8})000000-fv09\.2\.nc$"
)


@dataclass(frozen=True)
class RemoteFile:
    name: str
    url: str
    file_date: date
    expected_size: int | None = None


class ProgressDisplay:
    """按 GLEAM 下载器样式显示 worker、总进度、速度和 ETA。"""

    BOX_WIDTH = 86
    BAR_WIDTH = 24
    COLORS = {
        "clear": "\033[2K", "up": "\033[A", "hide": "\033[?25l",
        "show": "\033[?25h", "bold": "\033[1m", "dim": "\033[2m",
        "green": "\033[32m", "cyan": "\033[36m", "yellow": "\033[33m",
        "red": "\033[31m", "white": "\033[97m", "reset": "\033[0m",
    }

    def __init__(self, files: list[RemoteFile], workers: int, overwrite: bool) -> None:
        self.files = {item.name: item for item in files}
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
        """计算终端显示宽度，中文/全角字符按 2 列处理。"""
        width = 0
        index = 0
        while index < len(text):
            if text[index] == "\033":
                end = text.find("m", index)
                index = len(text) if end < 0 else end + 1
            else:
                char = text[index]
                if unicodedata.combining(char):
                    index += 1
                    continue
                width += 2 if unicodedata.east_asian_width(char) in "WF" else 1
                index += 1
        return width

    def _pad(self, text: str, width: int) -> str:
        return text + " " * max(0, width - self._visible_width(text))

    def _content_line(self, text: str = "") -> str:
        """卡片内容统一使用一个左内边距，并与边框保持完整宽度。"""
        return f"│ {self._pad(text, self.BOX_WIDTH - 1)}│"

    def _bar(self, percent: float, width: int | None = None) -> str:
        width = width or self.BAR_WIDTH
        filled = round(width * max(0.0, min(percent, 100.0)) / 100)
        c = self.COLORS
        return f"{c['green']}{'█' * filled}{'░' * (width - filled)}{c['reset']}"

    def begin(self, slot: int, name: str, received: int, total: int | None) -> None:
        with self.lock:
            state = self.states[name]
            state.update({"received": received, "total": total, "status": "下载中"})
            self.workers[slot] = {
                "name": name, "pct": received / total * 100 if total else 0,
                "speed": 0.0, "downloaded": 0, "status": "下载中",
                "last_bytes": received, "last_time": time.monotonic(),
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
            worker.update({"name": name, "pct": 100.0 if status == "done" else 0.0,
                           "downloaded": max(0, received - state["base"]),
                           "speed": 0.0, "status": status})
            if status == "done":
                self.completed += 1
            elif status == "fail":
                self.failed += 1

    def mark_idle(self, slot: int) -> None:
        with self.lock:
            self.workers[slot] = {"name": "", "pct": 0, "speed": 0,
                                  "downloaded": 0, "status": "idle"}

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
        percent = received / total_size * 100 if known_size and total_size else (
            completed / self.total_files * 100 if self.total_files else 100
        )
        eta = (max(0, total_size - received) / speed) if known_size and speed > 0 else -1
        active = sum(value.get("status") == "下载中" for value in workers.values())

        lines = [f"┌{'─' * self.BOX_WIDTH}┐"]
        title = f"{c['bold']}{c['white']} ESA CCI SM PASSIVE v09.2 并行下载 ×{self.workers_count}{c['reset']}"
        padding = self.BOX_WIDTH - self._visible_width(title)
        lines.append(f"│{' ' * (padding // 2)}{title}{' ' * (padding - padding // 2)}│")
        lines.append(f"├{'─' * self.BOX_WIDTH}┤")
        for slot in range(self.workers_count):
            worker = workers.get(slot)
            if not worker or worker.get("status") == "idle":
                row = f"{c['dim']}[空闲]{c['reset']}"
            else:
                name = worker["name"][-22:].ljust(22)
                pct = worker.get("pct", 0)
                if worker.get("status") == "done":
                    tag = f"{c['green']}✓ 完成{c['reset']}"
                elif worker.get("status") == "fail":
                    tag = f"{c['red']}✗ 失败{c['reset']}"
                else:
                    tag = f"{c['yellow']}↓ {worker.get('speed', 0) / 1024**2:.1f} MB/s{c['reset']}"
                row = (f"{c['cyan']}{name}{c['reset']} {self._bar(pct)} "
                       f"{c['white']}{pct:5.1f}%{c['reset']}  "
                       f"{self.format_size(worker.get('downloaded', 0)):>10}  {tag}")
            lines.append(self._content_line(row))
        lines.append(f"├{'─' * self.BOX_WIDTH}┤")
        if known_size and total_size:
            overall = f"{self._bar(percent)} {min(percent, 100):5.1f}%  {self.format_size(received)}/{self.format_size(total_size)}"
        else:
            overall = f"{self._bar(percent)} {min(percent, 100):5.1f}%  文件 {completed}/{self.total_files}"
        lines.append(self._content_line(f"{c['yellow']}总进度{c['reset']} {overall}"))
        info = (f"{c['green']}速度:{c['reset']} {speed / 1024**2:.1f} MB/s  "
                f"{c['cyan']}已用:{c['reset']} {self.format_time(elapsed)}  "
                f"{c['yellow']}剩余:{c['reset']} {self.format_time(eta)}  "
                f"{c['yellow']}活跃:{c['reset']} {active}/{self.workers_count}  "
                f"{c['red']}失败:{c['reset']} {failed}")
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
    """在 Windows 控制台启用 VT100，失败时仍可正常下载。"""
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


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("cci_sm_passive_v092")
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


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"日期必须使用 YYYY-MM-DD 格式：{value}"
        ) from exc


def request_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with DIRECT_OPENER.open(request, timeout=60) as response:
        payload = response.read()
    result = json.loads(payload.decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError(f"JSON listing 格式异常：{url}")
    return result


def item_url(item: dict[str, Any], year: int, name: str) -> str:
    """兼容 CEDA JSON listing 中的绝对 path、相对 path 和缺失 path。"""
    raw_path = str(item.get("path", "")).strip()
    if raw_path.startswith("http://") or raw_path.startswith("https://"):
        return raw_path
    if raw_path.startswith("/"):
        return f"{ARCHIVE_HOST}{raw_path}"
    return f"{BASE_URL}/{year}/{name}"


def item_size(item: dict[str, Any]) -> int | None:
    """读取不同版本 JSON listing 可能使用的文件大小字段。"""
    for key in ("size", "bytes", "length", "file_size"):
        value = item.get(key)
        if value is None:
            continue
        try:
            size = int(value)
            return size if size >= 0 else None
        except (TypeError, ValueError):
            continue
    return None


def list_year(year: int, start: date, end: date, logger: logging.Logger) -> list[RemoteFile]:
    """读取某一年的 CEDA JSON listing，并按日期筛选 nc 文件。"""
    listing_url = f"{BASE_URL}/{year}?json="
    logger.info("读取目录清单：%s", listing_url)
    listing = request_json(listing_url)
    items = listing.get("items", [])
    if not isinstance(items, list):
        raise RuntimeError(f"目录清单缺少 items：{listing_url}")

    selected: list[RemoteFile] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in ("file", None):
            continue
        name = str(item.get("name", "")).strip()
        match = FILE_RE.match(name)
        if not match:
            continue
        file_date = datetime.strptime(match.group("yyyymmdd"), "%Y%m%d").date()
        if start <= file_date <= end:
            selected.append(
                RemoteFile(
                    name=name,
                    url=item_url(item, year, name),
                    file_date=file_date,
                    expected_size=item_size(item),
                )
            )

    selected.sort(key=lambda item: item.file_date)
    logger.info("%d 年筛选得到 %d 个文件", year, len(selected))
    return selected


def is_complete(path: Path, expected_size: int | None) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    return expected_size is None or path.stat().st_size == expected_size


def download_one(
    slot: int,
    remote: RemoteFile,
    logger: logging.Logger,
    progress: ProgressDisplay,
    overwrite: bool = False,
) -> tuple[str, str, int, str]:
    """下载一个文件，返回 name、状态、大小和说明。"""
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

    if overwrite and output_path.exists():
        output_path.unlink()
    if overwrite and part_path.exists():
        part_path.unlink()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, RETRY_MAX + 1):
        try:
            existing_size = part_path.stat().st_size if part_path.exists() else 0
            progress.begin(slot, remote.name, existing_size, remote.expected_size)
            headers = {"User-Agent": USER_AGENT}
            if existing_size > 0:
                headers["Range"] = f"bytes={existing_size}-"
            headers["Accept-Encoding"] = "identity"
            headers["Connection"] = "keep-alive"

            request = Request(remote.url, headers=headers)
            with DIRECT_OPENER.open(request, timeout=120) as response:
                status = getattr(response, "status", None)
                resumed = existing_size > 0 and status == 206
                if existing_size > 0 and not resumed:
                    logger.warning(
                        "%s 不支持按 Range 返回，重新开始下载", remote.name
                    )
                    existing_size = 0

                content_length = response.headers.get("Content-Length")
                if remote.expected_size is not None:
                    total_size = remote.expected_size
                elif content_length:
                    total_size = existing_size + int(content_length) if resumed else int(content_length)
                else:
                    total_size = None
                progress.set_total(remote.name, total_size)

                mode = "ab" if resumed else "wb"
                with part_path.open(mode) as output:
                    downloaded_size = existing_size
                    while True:
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        output.write(chunk)
                        downloaded_size += len(chunk)
                        progress.update(slot, downloaded_size)

            actual_size = part_path.stat().st_size
            if remote.expected_size is not None and actual_size != remote.expected_size:
                raise IOError(
                    f"文件大小不符：实际 {actual_size}，预期 {remote.expected_size}"
                )

            part_path.replace(output_path)
            progress.finish(slot, remote.name, "done", actual_size)
            return remote.name, "downloaded", actual_size, "完成并通过大小校验"

        except (HTTPError, URLError, TimeoutError, OSError, IOError) as exc:
            # 断点超过远端文件长度时，Range 会返回 416；删除损坏断点后从头重试。
            if isinstance(exc, HTTPError) and exc.code == 416 and part_path.exists():
                logger.warning("%s 断点无效，删除 .filepart 后重新下载", remote.name)
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
                progress.finish(
                    slot,
                    remote.name,
                    "fail",
                    part_path.stat().st_size if part_path.exists() else 0,
                )
                return remote.name, "failed", 0, str(exc)

    progress.finish(
        slot,
        remote.name,
        "fail",
        part_path.stat().st_size if part_path.exists() else 0,
    )
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
            logger.warning("大小不匹配：%s", remote.name)

    logger.info(
        "检查完成：应有 %d 个，缺失 %d 个，大小不匹配 %d 个",
        len(files),
        missing,
        invalid,
    )
    return 0 if missing == 0 and invalid == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="下载 ESA CCI Soil Moisture PASSIVE v09.2 日尺度 NetCDF 文件"
    )
    parser.add_argument("--start", type=parse_date, default=DEFAULT_START)
    parser.add_argument("--end", type=parse_date, default=DEFAULT_END)
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"并行下载数，默认 {DEFAULT_WORKERS}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只读取目录并显示文件数量，不下载",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只检查目标时段文件是否齐全和大小是否正确",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖已有文件；默认不会覆盖大小不匹配的文件",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logger = setup_logging()

    if args.start > args.end:
        logger.error("--start 不能晚于 --end")
        return 2
    if args.workers < 1:
        logger.error("--workers 必须大于等于 1")
        return 2

    years = range(args.start.year, args.end.year + 1)
    logger.info("ESA CCI SM PASSIVE v09.2 下载器启动")
    logger.info("时间范围：%s 至 %s", args.start, args.end)
    logger.info("保存目录：%s", OUTPUT_DIR)
    logger.info("网络模式：直连 CEDA（忽略 HTTP_PROXY/HTTPS_PROXY 等代理环境变量）")

    try:
        remote_files: list[RemoteFile] = []
        for year in years:
            remote_files.extend(list_year(year, args.start, args.end, logger))
    except Exception as exc:
        logger.exception("读取 CEDA 文件清单失败：%s", exc)
        return 1

    logger.info("目标文件总数：%d", len(remote_files))
    if not remote_files:
        logger.error("没有找到目标文件，请检查 CEDA 地址或日期范围")
        return 1

    if args.dry_run:
        for remote in remote_files[:20]:
            logger.info("[DRY RUN] %s", remote.name)
        if len(remote_files) > 20:
            logger.info("[DRY RUN] 其余 %d 个文件省略", len(remote_files) - 20)
        return 0

    if args.check_only:
        return check_only(remote_files, logger)

    progress = ProgressDisplay(remote_files, args.workers, args.overwrite)
    pending = [
        remote for remote in remote_files
        if args.overwrite or not is_complete(OUTPUT_DIR / remote.name, remote.expected_size)
    ]
    skipped = len(remote_files) - len(pending)
    success = 0
    failed = 0
    total_bytes = 0

    if not pending:
        logger.info("所有目标文件已存在且大小正确，无需下载")
        return 0

    print(
        f"待下载文件: {len(pending)} 个 | 总文件: {len(remote_files)} 个 | "
        f"并发数: {args.workers} | 直连网络"
    )
    sys.stdout.write(ProgressDisplay.COLORS["hide"])
    sys.stdout.flush()
    progress.render()

    pending_iter = iter(pending)
    futures: dict[Any, tuple[int, RemoteFile]] = {}

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            for slot in range(args.workers):
                try:
                    remote = next(pending_iter)
                except StopIteration:
                    break
                futures[executor.submit(
                    download_one, slot, remote, logger, progress, args.overwrite
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
                            download_one, slot, next_remote, logger,
                            progress, args.overwrite
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

    logger.info(
        "进度统计：下载 %d，跳过 %d，失败 %d",
        success,
        skipped,
        failed,
    )

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
