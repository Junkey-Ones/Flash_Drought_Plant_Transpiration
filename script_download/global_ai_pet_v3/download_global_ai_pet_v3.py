"""下载 Global Aridity Index and PET Database v3.1 的年度 AI 栅格。

本项目只把 AI 作为 1970--2000 年多年平均的静态干旱度梯度，因此默认：

* 只选择年度 Global-AI 压缩包；
* 不下载月尺度 AI、PET/ET0 或未来 CMIP6 数据；
* 解压后的 AI GeoTIFF 写入 data_raw/aridity/global_ai_pet_v3/。

用法：
    python download_global_ai_pet_v3.py --list-files
    python download_global_ai_pet_v3.py --dry-run
    python download_global_ai_pet_v3.py
    python download_global_ai_pet_v3.py --file-name <Figshare文件名>
    python download_global_ai_pet_v3.py --check-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# ---------------------------------------------------------------------------
# 项目路径和数据源配置
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIGSHARE_ARTICLE_ID = 7504448
FIGSHARE_API = f"https://api.figshare.com/v2/articles/{FIGSHARE_ARTICLE_ID}"

OUTPUT_DIR = PROJECT_ROOT / "data_raw" / "aridity" / "global_ai_pet_v3"
TMP_DIR = PROJECT_ROOT / "download_tmp" / "aridity" / "global_ai_pet_v3"
LOG_DIR = PROJECT_ROOT / "logs" / "download" / "global_ai_pet_v3"
LOG_FILE = LOG_DIR / "download_global_ai_pet_v3.log"

DEFAULT_WORKERS = 1  # 当前最小方案只下载一个年度 AI 压缩包
CHUNK_SIZE = 4 * 1024 * 1024
RETRY_MAX = 4
RETRY_DELAY = 5
REFRESH_INTERVAL = 0.5
USER_AGENT = "Flash_Drought_Green/1.1 Global-AI-PET-v3-downloader"


@dataclass(frozen=True)
class RemoteFile:
    name: str
    url: str
    size: int | None = None
    md5: str | None = None


def enable_ansi() -> None:
    """在 Windows 控制台启用 ANSI 转义；失败时仍不影响下载。"""
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
    logger = logging.getLogger("global_ai_pet_v3")
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


def format_size(value: float) -> str:
    if value < 1024:
        return f"{value:.0f} B"
    if value < 1024**2:
        return f"{value / 1024:.1f} KB"
    if value < 1024**3:
        return f"{value / 1024**2:.1f} MB"
    return f"{value / 1024**3:.2f} GB"


def format_time(seconds: float) -> str:
    if seconds < 0:
        return "--"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h {int(seconds % 3600 // 60):02d}m"


class ProgressDisplay:
    """按 CCI 下载器样式，实时显示文件进度、速度、ETA 和状态。"""

    WIDTH = 86
    BAR_WIDTH = 28
    CLEAR = "\033[2K"
    UP = "\033[A"
    HIDE = "\033[?25l"
    SHOW = "\033[?25h"
    GREEN = "\033[32m"
    CYAN = "\033[36m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    WHITE = "\033[97m"
    RESET = "\033[0m"

    def __init__(self, remote: RemoteFile) -> None:
        self.remote = remote
        self.lock = threading.Lock()
        self.start = time.monotonic()
        self.received = 0
        self.total = remote.size
        self.status = "等待中"
        self.speed = 0.0
        self.rendered_lines = 0
        self.last_received = 0
        self.last_time = self.start
        self.last_render = 0.0
        enable_ansi()

    @staticmethod
    def visible_width(text: str) -> int:
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

    def content_line(self, text: str = "") -> str:
        padding = max(0, self.WIDTH - 1 - self.visible_width(text))
        return f"│ {text}{' ' * padding}│"

    def bar(self, percent: float) -> str:
        filled = round(self.BAR_WIDTH * max(0, min(percent, 100)) / 100)
        return (
            f"{self.GREEN}{'█' * filled}{'░' * (self.BAR_WIDTH - filled)}"
            f"{self.RESET}"
        )

    def begin(self, received: int) -> None:
        with self.lock:
            self.received = received
            self.last_received = received
            self.last_time = time.monotonic()
            self.status = "下载中"

    def update(self, received: int) -> None:
        now = time.monotonic()
        with self.lock:
            elapsed = now - self.last_time
            if elapsed >= 0.2:
                self.speed = max(0, received - self.last_received) / elapsed
                self.last_received = received
                self.last_time = now
            self.received = received

    def finish(self, status: str) -> None:
        with self.lock:
            self.status = status
            if status == "完成":
                self.received = self.total or self.received
            self.speed = 0

    def render(self) -> None:
        now = time.monotonic()
        if self.last_render and now - self.last_render < REFRESH_INTERVAL:
            return
        with self.lock:
            received = self.received
            total = self.total
            status = self.status
            speed = self.speed
        elapsed = max(time.monotonic() - self.start, 0.001)
        percent = received / total * 100 if total else 0
        eta = (max(0, total - received) / speed) if total and speed > 0 else -1
        title = f"{self.CYAN} Global-AI_PET v3.1 年度 AI 下载 {self.RESET}"
        current = self.remote.name[-60:]
        if status == "完成":
            status_text = f"{self.GREEN}✓ 完成{self.RESET}"
        elif status == "失败":
            status_text = f"{self.RED}✗ 失败{self.RESET}"
        else:
            status_text = f"{self.YELLOW}↓ 下载中{self.RESET}"

        lines = [
            f"┌{'─' * self.WIDTH}┐",
            self.content_line(title),
            f"├{'─' * self.WIDTH}┤",
            self.content_line(f"文件: {current}"),
            self.content_line(
                f"进度: {self.bar(percent)} {min(percent, 100):5.1f}%  "
                f"{format_size(received)}/{format_size(total or 0)}"
            ),
            self.content_line(
                f"速度: {format_size(speed)}/s  已用: {format_time(elapsed)}  "
                f"剩余: {format_time(eta)}  状态: {status_text}"
            ),
            f"└{'─' * self.WIDTH}┘",
        ]
        if self.rendered_lines:
            sys.stdout.write(self.UP * self.rendered_lines)
        for line in lines:
            sys.stdout.write(f"{self.CLEAR}{line}\n")
        sys.stdout.flush()
        self.rendered_lines = len(lines)
        self.last_render = now

    def clear(self) -> None:
        if not self.rendered_lines:
            return
        sys.stdout.write(self.UP * self.rendered_lines)
        for _ in range(self.rendered_lines):
            sys.stdout.write(f"{self.CLEAR}\n")
        sys.stdout.flush()
        self.rendered_lines = 0


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def is_ai_name(name: str) -> bool:
    normalized = normalize_name(name)
    # Figshare 当前年度文件名为 Global-AI_ET0__annual_v3_1.zip。
    # 这里的 ET0 表示该年度压缩包同时包含 AI 和 ET0，不能直接排除。
    # 只有纯 Global-ET0 / PET 文件才排除。
    if ("et0" in normalized or "pet" in normalized) and "global_ai" not in normalized:
        return False
    return (
        "global_ai" in normalized
        or "globalai" in normalized
        or bool(re.search(r"(?:^|_)ai(?:_|v|yr|annual|year)", normalized))
    )


def is_annual_name(name: str) -> bool:
    normalized = normalize_name(name)
    return any(token in normalized for token in ("annual", "year", "_yr", "yr_"))


def is_monthly_name(name: str) -> bool:
    normalized = normalize_name(name)
    return "month" in normalized or "monthly" in normalized


def candidate_score(name: str) -> int:
    normalized = normalize_name(name)
    score = 0
    if is_ai_name(name):
        score += 10
    if is_annual_name(name):
        score += 8
    if "v3" in normalized:
        score += 4
    if "1970" in normalized:
        score += 2
    if "global_ai_pet" in normalized or "globalaipet" in normalized:
        score -= 2
    if is_monthly_name(name):
        score -= 20
    return score


def fetch_article() -> dict[str, Any]:
    request = Request(FIGSHARE_API, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=120) as response:
        article = json.loads(response.read().decode("utf-8"))
    if not article.get("files"):
        raise RuntimeError("Figshare API 没有返回文件列表")
    return article


def list_files(article: dict[str, Any]) -> None:
    print("Figshare 文件列表：")
    for item in article["files"]:
        size_gb = int(item.get("size", 0)) / 1024**3
        print(f"  {item['name']}\t{size_gb:.3f} GB")


def select_file(article: dict[str, Any], requested_name: str | None) -> RemoteFile:
    if requested_name:
        selected = next(
            (item for item in article["files"] if item["name"] == requested_name),
            None,
        )
        if selected is None:
            raise RuntimeError(f"找不到 Figshare 文件：{requested_name}")
    else:
        candidates = [
            item
            for item in article["files"]
            if is_ai_name(item["name"])
            and is_annual_name(item["name"])
            and not is_monthly_name(item["name"])
            and Path(item["name"]).suffix.lower() not in {".pdf", ".txt"}
        ]
        if not candidates:
            raise RuntimeError(
                "没有自动识别到年度 AI 压缩包；请先运行 --list-files，"
                "再用 --file-name 指定准确文件名。"
            )
        candidates.sort(key=lambda item: candidate_score(item["name"]), reverse=True)
        selected = candidates[0]
        if len(candidates) > 1:
            logger.info("年度 AI 候选文件：%s", [item["name"] for item in candidates])
            logger.info("自动选择：%s", selected["name"])

    return RemoteFile(
        name=selected["name"],
        url=selected["download_url"],
        size=int(selected["size"]) if selected.get("size") else None,
        md5=selected.get("computed_md5"),
    )


def md5sum(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_one(remote: RemoteFile, progress: ProgressDisplay, logger: logging.Logger) -> Path:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = TMP_DIR / remote.name
    part_path = Path(f"{archive_path}.filepart")

    for attempt in range(1, RETRY_MAX + 1):
        try:
            existing = part_path.stat().st_size if part_path.exists() else 0
            headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
            if existing:
                headers["Range"] = f"bytes={existing}-"
            logger.info("下载 %s：第 %d/%d 次", remote.name, attempt, RETRY_MAX)

            request = Request(remote.url, headers=headers)
            with urlopen(request, timeout=300) as response:
                status_code = getattr(response, "status", response.getcode())
                resumed = existing > 0 and status_code == 206
                if existing and not resumed:
                    logger.warning("服务器未返回 206，删除断点并重新下载")
                    existing = 0
                mode = "ab" if resumed else "wb"
                progress.begin(existing)
                progress.render()
                received = existing
                with part_path.open(mode) as output:
                    while True:
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        output.write(chunk)
                        received += len(chunk)
                        progress.update(received)
                        progress.render()

            actual_size = part_path.stat().st_size
            if remote.size and actual_size != remote.size:
                raise IOError(f"大小校验失败：实际 {actual_size}，预期 {remote.size}")
            if remote.md5 and md5sum(part_path).lower() != remote.md5.lower():
                raise IOError("MD5 校验失败")
            part_path.replace(archive_path)
            progress.finish("完成")
            logger.info("下载并校验完成：%s", archive_path)
            return archive_path
        except Exception as exc:
            progress.finish("失败")
            logger.warning("下载失败：%s", exc)
            if attempt < RETRY_MAX:
                time.sleep(RETRY_DELAY)
            else:
                raise RuntimeError(f"下载失败：{remote.name}") from exc
    raise RuntimeError(f"下载失败：{remote.name}")


def is_ai_raster(member_name: str) -> bool:
    # 只看压缩包内部文件名，不看上级目录名；年度压缩包目录本身含有 ET0。
    basename = Path(member_name.replace("\\", "/")).name.lower()
    suffix = Path(basename).suffix.lower()
    return (
        suffix in {".tif", ".tiff"}
        and ("ai" in basename or "aridity" in basename)
        and "pet" not in basename
        and "et0" not in basename
    )


def extract_ai(archive_path: Path, force: bool, logger: logging.Logger) -> list[Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    with zipfile.ZipFile(archive_path) as archive:
        members = [
            info
            for info in archive.infolist()
            if is_ai_raster(info.filename)
            or (Path(info.filename).suffix.lower() == ".pdf" and "readme" in info.filename.lower())
        ]
        if not members:
            raise RuntimeError("压缩包中没有找到 AI GeoTIFF，请检查 --list-files 和 Readme")

        output_root = OUTPUT_DIR.resolve()
        for info in members:
            target = (OUTPUT_DIR / info.filename.replace("\\", "/")).resolve()
            if output_root not in target.parents:
                raise RuntimeError(f"压缩包包含不安全路径：{info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not force:
                logger.info("已存在，跳过解压：%s", target)
                extracted.append(target)
                continue
            with archive.open(info) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=CHUNK_SIZE)
            logger.info("已解压：%s", target)
            extracted.append(target)
    return extracted


def find_ai_rasters() -> list[Path]:
    if not OUTPUT_DIR.exists():
        return []
    return sorted(
        path
        for path in OUTPUT_DIR.rglob("*")
        if path.is_file() and is_ai_raster(str(path))
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="下载 Global-AI_PET v3.1 年度 Global-AI GeoTIFF"
    )
    parser.add_argument("--list-files", action="store_true", help="列出 Figshare 全部文件")
    parser.add_argument("--dry-run", action="store_true", help="只识别目标文件，不下载")
    parser.add_argument("--check-only", action="store_true", help="检查 AI GeoTIFF 是否已解压")
    parser.add_argument("--file-name", help="手动指定 Figshare 文件名")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有 AI GeoTIFF")
    parser.add_argument("--keep-archive", action="store_true", help="将 zip 复制到 data_raw")
    return parser


def main() -> int:
    global logger
    logger = setup_logging()
    args = build_parser().parse_args()
    logger.info("Global-AI_PET v3.1 下载器启动")
    logger.info("数据目录：%s", OUTPUT_DIR)
    logger.info("日志文件：%s", LOG_FILE)

    try:
        article = fetch_article()
        if args.list_files:
            list_files(article)
            return 0

        if args.check_only:
            rasters = find_ai_rasters()
            if rasters:
                logger.info("检查通过，发现 %d 个 AI GeoTIFF：", len(rasters))
                for path in rasters:
                    logger.info("  %s", path)
                return 0
            logger.error("未发现 AI GeoTIFF：%s", OUTPUT_DIR)
            return 1

        remote = select_file(article, args.file_name)
        logger.info("目标文件：%s", remote.name)
        logger.info("远程大小：%s", format_size(remote.size or 0))

        if args.dry_run:
            print(f"将下载：{remote.name}")
            print(f"大小：{format_size(remote.size or 0)}")
            print(f"解压到：{OUTPUT_DIR}")
            return 0

        progress = ProgressDisplay(remote)
        sys.stdout.write(progress.HIDE)
        sys.stdout.flush()
        try:
            archive_path = download_one(remote, progress, logger)
            progress.render()
            extracted = extract_ai(archive_path, args.overwrite, logger)
        finally:
            progress.clear()
            sys.stdout.write(progress.SHOW)
            sys.stdout.flush()

        if args.keep_archive:
            kept = OUTPUT_DIR / remote.name
            if not kept.exists() or args.overwrite:
                shutil.copy2(archive_path, kept)
            logger.info("压缩包已保留到：%s", kept)
        logger.info("完成：解压 %d 个 AI/Readme 文件", len(extracted))
        logger.info("临时压缩包：%s", archive_path)
        return 0
    except KeyboardInterrupt:
        logger.warning("用户中断；下次运行会从 .filepart 继续")
        return 130
    except Exception as exc:
        logger.exception("任务失败：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
