"""
GLEAM v4.3a Daily 数据下载脚本 (使用 WinSCP)
变量: SMrz, SMs, Et
时间范围: 2000-2025
支持并行下载 + 实时进度面板
"""

import os
import sys
import subprocess
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote

# ============ 配置参数 ============
SFTP_HOST = "aether.ugent.be"
SFTP_PORT = 2225
SFTP_USERNAME = "gleamuser"
SFTP_PASSWORD = "GLEAM4#h-cel_111"

VARIABLES = ["SMrz", "SMs", "Et"]
GLEAM_VERSION = "v4.3a"
START_YEAR = 2000
END_YEAR = 2025

REMOTE_BASE = "/data/v4.3a/daily"
LOCAL_DIR = r"C:\Users\Junkey\MyCode\Flash_Drought_Green\data_raw\gleam"
TEMP_DIR = r"C:\Users\Junkey\MyCode\Flash_Drought_Green\download_tmp\gleam"
DOWNLOAD_LOG_DIR = r"C:\Users\Junkey\MyCode\Flash_Drought_Green\logs\download\gleam"
WINSCP_PATH = r"C:\Program Files (x86)\WinSCP\WinSCP.com"
LOG_FILE = os.path.join(DOWNLOAD_LOG_DIR, "download_gleam.log")

# 并行下载数 (同时下载的文件数，建议 2-4)
MAX_WORKERS = max(1, int(os.environ.get("GLEAM_MAX_WORKERS", "4")))
REFRESH_INTERVAL = max(0.2, float(os.environ.get("GLEAM_REFRESH_INTERVAL", "0.5")))
# 仅在无法读取远端文件大小时使用；正常情况下会通过 SFTP stat 获取准确大小。
DEFAULT_FILE_SIZE = float(os.environ.get("GLEAM_DEFAULT_FILE_SIZE_GB", "6.0")) * 1024**3
# WinSCP 官方建议的连接缓冲区设置，可通过环境变量调整或设为 0 关闭。
WINSCP_SEND_BUF = os.environ.get("GLEAM_WINSCP_SEND_BUF", "262144")


# ============ 工具函数 ============
def log(msg, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {msg}"
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def format_size(b):
    if b < 1024: return f"{b} B"
    if b < 1024**2: return f"{b/1024:.1f} KB"
    if b < 1024**3: return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"


def format_time(s):
    if s < 0: return "--"
    if s < 60: return f"{s:.0f}s"
    if s < 3600: return f"{int(s//60)}m {int(s%60):02d}s"
    return f"{int(s//3600)}h {int((s%3600)//60):02d}m"


def get_file_size(fp):
    return os.path.getsize(fp) if os.path.exists(fp) else 0


def check_file_exists(var, year):
    return os.path.exists(os.path.join(LOCAL_DIR, var, GLEAM_VERSION,
                           f"{var}_{year}_GLEAM_{GLEAM_VERSION}.nc"))


def get_remote_file_sizes(file_list):
    """一次建立 SFTP 元数据连接，获取待处理文件的准确大小。"""
    sizes = {}
    try:
        import paramiko
    except ImportError:
        log("未安装 paramiko，进度百分比将使用默认文件大小", "WARNING")
        return sizes

    if not SFTP_PASSWORD:
        log("未设置 GLEAM_SFTP_PASSWORD，无法读取远端文件大小", "WARNING")
        return sizes

    transport = None
    sftp = None
    try:
        transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
        transport.connect(username=SFTP_USERNAME, password=SFTP_PASSWORD)
        sftp = paramiko.SFTPClient.from_transport(transport)
        for var, year in file_list:
            remote = f"{REMOTE_BASE}/{year}/{var}_{year}_GLEAM_{GLEAM_VERSION}.nc"
            try:
                sizes[(var, year)] = int(sftp.stat(remote).st_size)
            except Exception as exc:
                log(f"读取远端大小失败 {var}_{year}: {exc}", "WARNING")
    except Exception as exc:
        log(f"远端文件大小查询失败，将使用默认大小: {exc}", "WARNING")
    finally:
        if sftp is not None:
            sftp.close()
        if transport is not None:
            transport.close()
    return sizes


def get_local_progress_size(local_file, filepart):
    """返回 WinSCP 可能正在写入的文件大小，兼容断点续传。"""
    return max(get_file_size(local_file), get_file_size(filepart))


def winscp_session_url():
    """生成可正确处理特殊字符密码的 WinSCP SFTP URL。"""
    username = quote(SFTP_USERNAME or "", safe="")
    password = quote(SFTP_PASSWORD or "", safe="")
    return f"sftp://{username}:{password}@{SFTP_HOST}:{SFTP_PORT}/"


# ============ 并行下载面板 ============
class ParallelDisplay:
    BOX_WIDTH = 74
    BAR_W = 20

    C = {"cls": "\033[2K", "up": "\033[A", "hide": "\033[?25l",
         "show": "\033[?25h", "bold": "\033[1m", "dim": "\033[2m",
         "rst": "\033[0m", "g": "\033[32m", "c": "\033[36m",
         "y": "\033[33m", "r": "\033[31m", "w": "\033[97m"}

    def __init__(self, total, done, total_size, done_size):
        self.total = total
        self.done_init = done
        self.completed = done
        self.failed = 0
        self.total_size = total_size
        self.done_size = done_size
        self.start = time.time()
        self.total_bytes = 0          # 成功完成的文件数据量
        self.transferred_bytes = 0    # 已传输数据量（含当前文件和失败文件）
        self.lock = threading.Lock()
        self.workers = {}  # slot -> {name, pct, speed, downloaded, status}
        self._lines = 0
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetConsoleMode(
                ctypes.windll.kernel32.GetStdHandle(-11), 7)

    def _vis(self, s):
        """Visible width (strip ANSI)"""
        w, i = 0, 0
        while i < len(s):
            if s[i] == '\033':
                i = s.index('m', i) + 1
            else:
                w += 1; i += 1
        return w

    def _bar(self, pct):
        f = round(self.BAR_W * pct / 100)
        return f"{self.C['g']}{'█'*f}{'░'*(self.BAR_W-f)}{self.C['rst']}"

    def _padded(self, text, width):
        vw = self._vis(text)
        return text + ' ' * max(0, width - vw)

    def render(self):
        c = self.C
        W = self.BOX_WIDTH
        lines = []

        # 工作线程会同时更新状态，先复制快照，避免渲染过程中读到半更新数据。
        with self.lock:
            workers = {slot: dict(worker) for slot, worker in self.workers.items()}
            completed = self.completed
            transferred_bytes = self.transferred_bytes
        active_bytes = sum(
            worker['downloaded']
            for worker in workers.values()
            if worker['status'] not in ('done', 'fail', 'idle')
        )
        run_bytes = transferred_bytes + active_bytes
        overall_bytes = self.done_size + run_bytes

        # Header
        title = f"{c['bold']}{c['w']} GLEAM {GLEAM_VERSION} 并行下载 ×{MAX_WORKERS} {c['rst']}"
        lines.append(f"┌{'─'*W}┐")
        tw = self._vis(title)
        pad = W - 2 - tw
        lines.append(f"│{' '*(pad//2)}{title}{' '*(pad - pad//2)}│")
        lines.append(f"├{'─'*W}┤")

        # Worker slots
        active = 0
        for slot in range(MAX_WORKERS):
            w = workers.get(slot)
            if not w:
                line = f" {c['dim']}[空闲]{' '*62}{c['rst']}"
            else:
                active += 1
                name = w['name'][:12].ljust(12)
                bar = self._bar(w['pct'])
                pct = f"{w['pct']:5.1f}%"
                sz = format_size(w['downloaded'])
                spd = f"{w['speed']/(1024**2):.1f} MB/s"
                if w['status'] == 'done':
                    tag = f"{c['g']}✓ 完成{c['rst']}"
                elif w['status'] == 'fail':
                    tag = f"{c['r']}✗ 失败{c['rst']}"
                else:
                    tag = f"{c['y']}↓ {spd}{c['rst']}"
                line = f" {c['c']}{name}{c['rst']} {bar} {c['w']}{pct}{c['rst']}  {sz}  {tag}"
            lines.append(f"│{self._padded(line, W-2)}│")

        lines.append(f"├{'─'*W}┤")

        # Overall
        done = completed
        total_pct = overall_bytes / self.total_size * 100 if self.total_size else 0
        elapsed = time.time() - self.start
        overall_spd = run_bytes / elapsed / (1024**2) if elapsed > 0 else 0

        # 使用剩余字节/当前整体速度，避免并行下载时按文件平均耗时估算。
        if overall_spd > 0 and self.total_size:
            eta = max(0, self.total_size - overall_bytes) / (overall_spd * 1024**2)
        else:
            eta = -1

        obar = self._bar(min(total_pct, 100))
        oinfo = f"[{done}/{self.total}] {min(total_pct, 100):5.1f}%"
        oline = f" {c['y']}总进度{c['rst']} {obar} {c['w']}{oinfo}{c['rst']}"
        lines.append(f"│{self._padded(oline, W-2)}│")

        spd_line = (f" {c['g']}速度:{c['rst']} {overall_spd:.1f} MB/s  "
                     f"{c['c']}已用:{c['rst']} {format_time(elapsed)}  "
                     f"{c['y']}剩余:{c['rst']} {format_time(eta)}  "
                     f"{c['y']}活跃:{c['rst']} {active}/{MAX_WORKERS}")
        lines.append(f"│{self._padded(spd_line, W-2)}│")

        lines.append(f"└{'─'*W}┘")

        # Move cursor up and redraw
        if self._lines > 0:
            sys.stdout.write(c['up'] * self._lines)
        for ln in lines:
            sys.stdout.write(f"{c['cls']}{ln}\n")
        sys.stdout.flush()
        self._lines = len(lines)

    def update_worker(self, slot, **kw):
        with self.lock:
            if slot not in self.workers:
                self.workers[slot] = {'name': '', 'pct': 0, 'speed': 0,
                                      'downloaded': 0, 'status': 'downloading'}
            self.workers[slot].update(kw)

    def mark_done(self, slot, downloaded):
        with self.lock:
            self.workers[slot]['status'] = 'done'
            self.workers[slot]['pct'] = 100.0
            self.workers[slot]['downloaded'] = downloaded
            self.completed += 1
            self.transferred_bytes += downloaded
            self.total_bytes += downloaded

    def mark_fail(self, slot, downloaded=0):
        with self.lock:
            self.workers[slot]['downloaded'] = downloaded
            self.workers[slot]['status'] = 'fail'
            self.failed += 1
            self.transferred_bytes += downloaded

    def summary(self, success, fail, elapsed):
        c = self.C
        W = self.BOX_WIDTH
        avg = self.total_bytes / elapsed / (1024**2) if elapsed > 0 else 0

        rows = [
            (f"{c['g']}成功下载:{c['rst']}", f"{success} 个文件"),
            (f"{c['r']}失　　败:{c['rst']}", f"{fail} 个文件"),
            (f"{c['c']}总数据量:{c['rst']}", format_size(self.total_bytes)),
            (f"{c['y']}总 耗 时:{c['rst']}", format_time(elapsed)),
            (f"{c['g']}平均速度:{c['rst']}", f"{avg:.1f} MB/s"),
        ]
        print(f"\n┌{'─'*W}┐")
        title = f"{c['bold']}{c['w']} 下载完成 {c['rst']}"
        tw = self._vis(title)
        pad = W - 2 - tw
        print(f"│{' '*(pad//2)}{title}{' '*(pad - pad//2)}│")
        print(f"├{'─'*W}┤")
        for lv, rv in rows:
            ltext = f" {lv}"
            rtext = f"{rv} "
            space = W - 2 - self._vis(ltext) - self._vis(rtext)
            print(f"│{ltext}{' '*max(1,space)}{rtext}│")
        print(f"├{'─'*W}┤")
        ltext = f" {c['dim']}存放位置:{c['rst']}"
        rtext = f"{c['dim']}{LOCAL_DIR}{c['rst']} "
        space = W - 2 - self._vis(ltext) - self._vis(rtext)
        print(f"│{ltext}{' '*max(1,space)}{rtext}│")
        print(f"└{'─'*W}┘")


# ============ 单文件下载 (在工作线程中运行) ============
def download_worker(slot, var, year, display, remote_sizes):
    """下载单个文件，在线程中运行"""
    remote = f"{REMOTE_BASE}/{year}/{var}_{year}_GLEAM_{GLEAM_VERSION}.nc"
    local_dir = os.path.join(LOCAL_DIR, var, GLEAM_VERSION).replace("\\", "/")
    local_file = os.path.join(LOCAL_DIR, var, GLEAM_VERSION,
                              f"{var}_{year}_GLEAM_{GLEAM_VERSION}.nc")
    filepart = local_file + ".filepart"
    name = f"{var}_{year}"
    known_size = remote_sizes.get((var, year), DEFAULT_FILE_SIZE)

    display.update_worker(slot, name=name, pct=0, speed=0,
                          downloaded=0, status='downloading')

    # WinSCP 脚本 (每个线程独立脚本和日志)
    script_path = os.path.join(TEMP_DIR, f"_dl_{slot}_{var}_{year}.txt")
    log_path = os.path.join(TEMP_DIR, f"_dl_{slot}_{var}_{year}.log")

    with open(script_path, 'w', encoding='utf-8', newline='\n') as f:
        open_command = f"open {winscp_session_url()} -hostkey=*"
        if WINSCP_SEND_BUF and WINSCP_SEND_BUF != "0":
            open_command += f" -rawsettings SendBuf={WINSCP_SEND_BUF}"
        f.write(open_command + "\n"
                f'lcd "{local_dir}"\n'
                f'get -resume -transfer=binary "{remote}"\n'
                f"exit\n")

    size_before = get_local_progress_size(local_file, filepart)
    proc = subprocess.Popen(
        [WINSCP_PATH, f"/script={script_path}", f"/log={log_path}", "/loglevel=0"],
        # 不要把 WinSCP 输出接到 PIPE 后却不读取，否则输出缓冲区满时会卡死下载。
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    start = time.time()
    last_size = size_before
    samples = [(start, size_before)]

    while proc.poll() is None:
        time.sleep(REFRESH_INTERVAL)
        cur = get_local_progress_size(local_file, filepart)
        now = time.time()

        if cur != last_size:
            samples.append((now, cur))
            last_size = cur
        # 用时间窗口计算速度，而不是简单平均采样值；这样不会因连接阶段的
        # 0 采样把真实速度压成 0，也能反映最近几秒是否暂时停顿。
        cutoff = now - 10
        while len(samples) > 2 and samples[1][0] < cutoff:
            samples.pop(0)
        sample_dt = now - samples[0][0]
        avg_spd = max(0, cur - samples[0][1]) / sample_dt if sample_dt > 0 else 0
        dl = cur - size_before
        pct = cur / known_size * 100 if known_size else 0

        display.update_worker(slot, name=name, pct=min(pct, 99.9),
                              speed=avg_spd, downloaded=dl,
                              status='downloading')

    proc.wait()
    size_after = get_local_progress_size(local_file, filepart)
    downloaded = max(0, size_after - size_before)
    elapsed = time.time() - start

    # 最后一小段可能发生在上次轮询之后，先把它刷新到面板再标记结果。
    final_pct = size_after / known_size * 100 if known_size else 0
    display.update_worker(slot, name=name, pct=min(final_pct, 100),
                          speed=downloaded / elapsed if elapsed > 0 else 0,
                          downloaded=downloaded,
                          status='downloading')

    # 脚本文件包含密码，无论成功失败都删除；失败时只保留 WinSCP 日志供排查。
    try:
        os.remove(script_path)
    except OSError:
        pass

    if proc.returncode == 0 and check_file_exists(var, year):
        try:
            os.remove(log_path)
        except OSError:
            pass
        display.mark_done(slot, downloaded)
        speed = downloaded / elapsed / (1024**2) if elapsed > 0 else 0
        log(f"[完成] {name} - {format_size(downloaded)}, {format_time(elapsed)}, {speed:.1f} MB/s")
        return True
    else:
        display.mark_fail(slot, downloaded)
        log(f"[失败] {name}，WinSCP 日志保留: {log_path}", "ERROR")
        return False


# ============ 主函数 ============
def main():
    os.makedirs(DOWNLOAD_LOG_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    for var in VARIABLES:
        os.makedirs(os.path.join(LOCAL_DIR, var, GLEAM_VERSION), exist_ok=True)

    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        f.write("=" * 60 + '\n')
        f.write("GLEAM v4.3a 并行下载日志\n")
        f.write("=" * 60 + '\n')

    log("=" * 60)
    log(f"GLEAM {GLEAM_VERSION} 并行下载 (×{MAX_WORKERS})")
    log(f"变量: {', '.join(VARIABLES)} | 年份: {START_YEAR}-{END_YEAR}")
    log("=" * 60)

    file_list = [(v, y) for v in VARIABLES for y in range(START_YEAR, END_YEAR + 1)]
    total = len(file_list)
    existing = {f for f in file_list if check_file_exists(*f)}
    pending = [f for f in file_list if f not in existing]

    log(f"总文件: {total} | 已有: {len(existing)} | 待下载: {len(pending)}")

    if not pending:
        print("所有文件已下载完成！")
        return

    print(f"待下载文件: {len(pending)} 个 | 并发数: {MAX_WORKERS}")

    if not SFTP_PASSWORD:
        print("错误：请先设置环境变量 GLEAM_SFTP_PASSWORD。")
        log("未设置 GLEAM_SFTP_PASSWORD，任务未启动", "ERROR")
        return
    if not os.path.isfile(WINSCP_PATH):
        print(f"错误：找不到 WinSCP.com：{WINSCP_PATH}")
        log(f"找不到 WinSCP.com: {WINSCP_PATH}", "ERROR")
        return

    # 远端大小只查询一次，避免每个文件下载前额外建立连接。
    print(f"正在连接 SFTP 查询 {len(file_list)} 个远端文件的大小，请稍候...")
    remote_sizes = get_remote_file_sizes(file_list)
    if len(remote_sizes) == len(file_list):
        print("远端文件大小查询完成，正在启动下载面板...")
    elif remote_sizes:
        print(f"远端大小已查询 {len(remote_sizes)}/{len(file_list)} 个，其余使用默认大小，正在启动下载面板...")
    else:
        print("远端大小查询失败，将使用默认大小，正在启动下载面板...")
    total_size = sum(remote_sizes.get(item, DEFAULT_FILE_SIZE) for item in file_list)
    # 总进度的初始字节数包括已完成文件和所有 .filepart 断点文件，
    # 否则恢复任务时总体百分比会从 0% 重新开始。
    initial_size = 0
    for var, year in file_list:
        local_file = os.path.join(LOCAL_DIR, var, GLEAM_VERSION,
                                  f"{var}_{year}_GLEAM_{GLEAM_VERSION}.nc")
        initial_size += min(
            get_local_progress_size(local_file, local_file + ".filepart"),
            remote_sizes.get((var, year), DEFAULT_FILE_SIZE),
        )
    log(f"远端大小: {format_size(total_size)} | 本地已有数据: {format_size(initial_size)}")

    print(ParallelDisplay.C["hide"], end="", flush=True)
    display = ParallelDisplay(total, len(existing), total_size, initial_size)

    # 首次渲染空面板
    for s in range(MAX_WORKERS):
        display.workers[s] = {'name': '', 'pct': 0, 'speed': 0,
                              'downloaded': 0, 'status': 'idle'}
    display.render()

    success = 0
    fail = 0
    pending_iter = iter(pending)

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {}
            # 填充初始工作槽
            for slot in range(MAX_WORKERS):
                try:
                    var, year = next(pending_iter)
                    display.workers[slot] = {
                        'name': f"{var}_{year}", 'pct': 0, 'speed': 0,
                        'downloaded': 0, 'status': 'starting'}
                    f = pool.submit(download_worker, slot, var, year, display, remote_sizes)
                    futures[f] = slot
                except StopIteration:
                    break

            while futures:
                # 刷新面板
                display.render()
                time.sleep(REFRESH_INTERVAL)

                # 检查完成的工作
                done = [f for f in futures if f.done()]
                for f in done:
                    slot = futures.pop(f)
                    try:
                        ok = f.result()
                    except Exception as exc:
                        log(f"工作线程异常 (slot={slot}): {exc}", "ERROR")
                        display.mark_fail(slot)
                        ok = False
                    if ok:
                        success += 1
                    else:
                        fail += 1

                    # 填充新任务
                    try:
                        var, year = next(pending_iter)
                        display.workers[slot] = {
                            'name': f"{var}_{year}", 'pct': 0, 'speed': 0,
                            'downloaded': 0, 'status': 'starting'}
                        nf = pool.submit(download_worker, slot, var, year, display, remote_sizes)
                        futures[nf] = slot
                    except StopIteration:
                        display.workers[slot] = {
                            'name': '', 'pct': 0, 'speed': 0,
                            'downloaded': 0, 'status': 'idle'}

    except KeyboardInterrupt:
        log("用户中断下载！", "WARNING")
        print(ParallelDisplay.C["show"], end="", flush=True)
        print(f"\n⚠️  用户中断！成功: {success}, 失败: {fail}")
        print("重新运行可继续（已有文件自动跳过）")
        return

    print(ParallelDisplay.C["show"], end="", flush=True)
    elapsed = time.time() - display.start
    display.summary(success, fail, elapsed)

    log("=" * 60)
    log(f"完成! 成功: {success} | 失败: {fail} | "
        f"数据量: {format_size(display.total_bytes)} | 耗时: {format_time(elapsed)}")
    log("=" * 60)


if __name__ == "__main__":
    main()
