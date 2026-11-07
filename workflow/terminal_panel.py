"""M1–M10 的终端显示组件。

输入为标题、运行范围和阶段处理状态，向标准错误输出面板或文本进度。
由各阶段入口调用，不读取数据文件，无独立命令入口。
"""
import argparse
import os
import sys
from threading import Event, Thread
import unicodedata


ENTER = "\x1b[?1049h\x1b[?25l\x1b[2J"
LEAVE = "\x1b[0m\x1b[?1049l\x1b[?25h"


def enable_vt(stream):
    """启用 Windows 虚拟终端序列，返回复原操作。其他终端直接使用 ANSI。"""
    if not stream.isatty():
        return None
    if os.name != "nt":
        return None if os.environ.get("TERM") == "dumb" else lambda: None

    import ctypes
    import msvcrt
    from ctypes import wintypes

    console = ctypes.WinDLL("kernel32", use_last_error=True)
    console.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    console.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    handle = msvcrt.get_osfhandle(stream.fileno())
    previous = wintypes.DWORD()
    if not console.GetConsoleMode(handle, ctypes.byref(previous)):
        return None
    # 开启处理后的输出和虚拟终端控制序列。
    if not console.SetConsoleMode(handle, previous.value | 0x0001 | 0x0004):
        return None
    return lambda: console.SetConsoleMode(handle, previous.value)


def fit(text, width):
    """按终端列宽截断并补齐文本，汉字占两列，去除输入中的控制字符。"""
    chars, columns = [], 0
    for char in str(text):
        if unicodedata.category(char).startswith("C"):
            char = " "
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
        if columns + size > width:
            break
        chars.append(char)
        columns += size
    return "".join(chars) + " " * (width - columns)


class TerminalPanel:
    """在独立屏幕缓冲区绘制固定位置的面板，退出后恢复原终端。"""

    def __init__(self, stream, interactive):
        self.stream = stream
        self.interactive = interactive
        self.restore = None
        self.height = 0

    def write(self, text):
        self.stream.write(text)
        self.stream.flush()

    def __enter__(self):
        if self.interactive:
            try:
                self.restore = enable_vt(self.stream)
            except (OSError, ValueError):
                self.restore = None
        if self.restore is not None:
            try:
                self.write(ENTER)
            except BaseException:
                self.__exit__(None, None, None)
                raise
        return self

    def __exit__(self, *_):
        if self.restore is not None:
            try:
                self.write(LEAVE)
            finally:
                self.restore()
                self.restore = None

    def draw(self, title, context, step, detail, statistics, elapsed, fraction, task=None, *, slots=()):
        """在原位置更新中文状态和背景填充的进度条，避免写入最后一列。"""
        try:
            columns, rows = os.get_terminal_size(self.stream.fileno())
        except OSError:
            columns, rows = 80, 24
        width = max(0, min(columns - 1, 100))
        lines = [title, context, step, detail, statistics, elapsed, "按 Ctrl+C 安全中断"]
        bars_height = 10 + int(task is not None)
        if width >= 30 and rows >= bars_height:
            lines[3:3] = slots[:rows - bars_height]
            inner = width - 4
            lines = [f"│ {fit(line, inner)} │" for line in lines]
            bars = [fraction] if task is None else [fraction, task]
            for index, ratio in enumerate(bars):
                label = "流程 " if index == 0 else "任务 "
                span = inner - 5
                filled = round(span * ratio)
                bar = label + "\x1b[42m" + " " * filled + "\x1b[100m" + " " * (span - filled) + "\x1b[0m"
                lines.insert(3 + min(len(slots), rows - bars_height) + index, f"│ {bar} │")
            lines = ["┌" + "─" * (width - 2) + "┐", *lines,
                     "└" + "─" * (width - 2) + "┘"]
        else:
            lines = [fit(line, width) for line in lines[:rows]]
        # 用绝对光标位置覆盖原面板，并清除终端尺寸变化后多余的行。
        screen = "".join(f"\x1b[{i};1H\x1b[2K{line}" for i, line in enumerate(lines, 1))
        if self.height > len(lines):
            screen += f"\x1b[{len(lines) + 1};1H\x1b[J"
        self.height = len(lines)
        self.write(screen)


class LiveStatus:
    """为阻塞式读取定时刷新，由入口更新状态并处理退出。"""

    def __init__(self, mode, render):
        interactive = mode == "auto" and sys.stdout.isatty() and sys.stderr.isatty()
        self.panel = TerminalPanel(sys.stderr, interactive)
        self.render = render
        self.stop = Event()
        self.thread = Thread(target=self.refresh, daemon=True)

    def start(self):
        self.panel.__enter__()
        try:
            self.render()
            self.thread.start()
        except BaseException:
            self.close()
            raise

    def refresh(self):
        interval = 0.25 if self.panel.restore is not None else 15
        while not self.stop.wait(interval):
            self.render()

    def close(self):
        self.stop.set()
        try:
            if self.thread.ident is not None:
                self.thread.join()
        finally:
            self.panel.__exit__(None, None, None)


class ChineseHelp(argparse.RawDescriptionHelpFormatter):
    """以中文标注命令用法。"""

    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, "用法：" if prefix is None else prefix)


class ChineseParser(argparse.ArgumentParser):
    """将常见命令行输入错误转换成可操作的中文提示。"""

    def error(self, message):
        for source, target in (("the following arguments are required:", "缺少必填参数："),
                               ("unrecognized arguments:", "无法识别的参数："),
                               ("invalid choice:", "无效取值："), ("(choose from ", "(允许取值："),
                               ("invalid int value:", "请输入整数，收到："),
                               ("not allowed with argument", "不能同时使用"),
                               ("expected one argument", "后面需要填写一个值"), ("argument ", "参数 ")):
            message = message.replace(source, target)
        self.print_usage(sys.stderr)
        self.exit(2, f"参数错误：{message}\n运行 {self.prog} --help 查看示例。\n")


