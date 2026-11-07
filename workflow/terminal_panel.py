"""M1–M10 的终端显示组件。

输入为标题、运行范围和阶段处理状态，向标准错误输出面板或文本进度。
由各阶段入口调用，不读取数据文件，无独立命令入口。
"""
import argparse
import os
import sys
from threading import Event, Thread
from importlib import import_module


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


class TerminalPanel:
    """用 Rich Live 在当前命令下方维护一块固定的交互区域。"""

    def __init__(self, stream, interactive):
        self.stream = stream
        self.interactive = interactive
        self.restore = self.live = None

    def write(self, text):
        self.stream.write(text)
        self.stream.flush()

    def __enter__(self):
        if self.interactive:
            self.restore = terminal_restore(self.stream)
        if self.restore is None:
            return self
        try:
            try:
                from rich.console import Console
                from rich.live import Live
                self.backend = "rich"
            except ModuleNotFoundError as error:
                if error.name != "rich":
                    raise
                from pip._vendor.rich.console import Console
                from pip._vendor.rich.live import Live
                self.backend = "pip._vendor.rich"
            # 以已通过的 Windows VT 探测为准，避免 ConPTY 的 TERM=dumb 误导 Rich。
            environ = dict(os.environ)
            if os.name == "nt" and environ.get("TERM") in ("dumb", "unknown"):
                environ["TERM"] = "xterm"
            console = Console(file=self.stream, force_terminal=True, force_interactive=True,
                              color_system="standard", legacy_windows=False, _environ=environ)
            self.live = Live(console=console, auto_refresh=False, transient=True,
                             redirect_stdout=False, redirect_stderr=False)
            self.live.start()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        try:
            if self.live is not None:
                self.live.stop()
                self.live = None
        finally:
            if self.restore is not None:
                self.restore()
                self.restore = None

    def draw(self, title, context, step, detail, statistics, elapsed, fraction, task=None, *, slots=()):
        """交给 Rich 排版中文、边框及进度条，超宽文本按终端宽度折行。"""
        def component(module, name):
            return getattr(import_module(f"{self.backend}.{module}"), name)

        Group = component("console", "Group")
        Panel = component("panel", "Panel")
        ProgressBar = component("progress_bar", "ProgressBar")
        Text = component("text", "Text")

        body = [Text(context, style="dim"), Text(step, style="bold"), *(Text(slot) for slot in slots),
                Text(detail), ProgressBar(total=1, completed=fraction, width=None)]
        if task is not None:
            body.extend((Text("当前任务", style="dim"), ProgressBar(total=1, completed=task, width=None)))
        body.extend((Text(statistics), Text(elapsed, style="dim")))
        self.live.update(Panel(Group(*body), title=Text(title, style="bold cyan"),
                               title_align="left", border_style="cyan",
                               subtitle=Text("Ctrl+C 安全中断", style="dim")), refresh=True)


def terminal_restore(stream):
    """探测显示流是否支持光标更新，返回原终端模式的恢复操作。"""
    try:
        return enable_vt(stream)
    except (OSError, ValueError):
        return None


def validate_progress(parser, args):
    """显式要求面板时，在读写业务数据前拒绝不支持的输出环境。"""
    if args.progress == "panel" and not args.dry_run:
        restore = terminal_restore(sys.stderr)
        if restore is None:
            parser.error("当前标准错误未连接到支持光标更新的终端。请在 Windows Terminal 中直接运行，或使用 --progress plain")
        restore()


class LiveStatus:
    """为阻塞式读取定时刷新，由入口更新状态并处理退出。"""

    def __init__(self, mode, render):
        self.mode = mode
        interactive = mode != "plain" and sys.stderr.isatty()
        self.panel = TerminalPanel(sys.stderr, interactive)
        self.render = render
        self.stop = Event()
        self.thread = Thread(target=self.refresh, daemon=True)

    def start(self):
        self.panel.__enter__()
        try:
            if self.mode == "plain" or self.panel.restore is not None:
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


