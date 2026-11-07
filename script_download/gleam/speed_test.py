"""GLEAM SFTP 下载测速脚本 (延长版)"""
import os, time, subprocess, tempfile, shutil

WINSCP = r"C:\Program Files (x86)\WinSCP\WinSCP.com"
HOST = "aether.ugent.be"
PORT = 2225
USER = "gleamuser"
PWD = os.environ.get("GLEAM_SFTP_PASSWORD", "")

TEST_REMOTE = "/data/v4.3a/daily/2000/SMs_2000_GLEAM_v4.3a.nc"

def main():
    if not PWD:
        print("请设置环境变量 GLEAM_SFTP_PASSWORD")
        return

    tmp = tempfile.mkdtemp()
    local_file = os.path.join(tmp, "SMs_2000_GLEAM_v4.3a.nc")
    log_file = os.path.join(tmp, "winscp.log")

    script = f"""open sftp://{USER}:{PWD}@{HOST}:{PORT}/ -hostkey=*
lcd "{tmp}"
get -transfer=binary "{TEST_REMOTE}"
exit"""

    sp = os.path.join(tmp, "test.txt")
    with open(sp, "w") as f:
        f.write(script)

    print("=" * 50)
    print("  GLEAM SFTP 下载测速")
    print("=" * 50)
    print(f"  服务器: {HOST}:{PORT}")
    print(f"  测试文件: {TEST_REMOTE}")
    print(f"  测试时长: 120 秒")
    print("-" * 50)
    print("  连接中...")

    start = time.time()
    proc = subprocess.Popen(
        [WINSCP, f"/script={sp}", f"/log={log_file}", "/loglevel=0"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace"
    )

    last_size = 0
    samples = []
    connected = False
    while time.time() - start < 120:
        time.sleep(3)
        cur_size = os.path.getsize(local_file) if os.path.exists(local_file) else 0
        now = time.time()
        dt = now - start

        if not connected and cur_size > 0:
            connected = True
            print(f"  [{dt:5.0f}s]  连接成功，开始下载...")

        if cur_size > last_size:
            speed = (cur_size - last_size) / 3
            samples.append(speed)
            mb = cur_size / (1024 * 1024)
            spd = speed / (1024 * 1024)
            print(f"  [{dt:5.0f}s]  已下载: {mb:8.1f} MB  |  速度: {spd:6.1f} MB/s")
            last_size = cur_size
        elif connected:
            print(f"  [{dt:5.0f}s]  等待数据... (当前: {cur_size/(1024*1024):.1f} MB)")

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    total_time = time.time() - start
    total_size = os.path.getsize(local_file) if os.path.exists(local_file) else 0

    if samples:
        avg_speed = sum(samples) / len(samples) / (1024 * 1024)
        max_speed = max(samples) / (1024 * 1024)
        min_speed = min(samples) / (1024 * 1024)
    else:
        avg_speed = max_speed = min_speed = 0

    overall = total_size / total_time / (1024 * 1024) if total_time > 0 else 0

    print("-" * 50)
    print(f"  测试时长:   {total_time:.0f} 秒")
    print(f"  下载数据:   {total_size / (1024*1024):.1f} MB")
    print(f"  平均速度:   {avg_speed:.1f} MB/s")
    print(f"  最高速度:   {max_speed:.1f} MB/s")
    print(f"  最低速度:   {min_speed:.1f} MB/s")
    print(f"  整体速度:   {overall:.1f} MB/s")

    file_size_gb = 6.0
    files_remaining = 52
    if avg_speed > 0:
        per_file = file_size_gb * 1024 / avg_speed
        total_hours = per_file * files_remaining / 3600
        print("-" * 50)
        print(f"  预估单文件: {per_file / 60:.0f} 分钟")
        print(f"  预估总任务: {total_hours:.1f} 小时 ({files_remaining} 个文件)")
    elif total_size == 0:
        print("-" * 50)
        print("  ⚠ 未能下载数据，可能连接或认证失败")
        # 输出 WinSCP 日志最后几行
        if os.path.exists(log_file):
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            print("  WinSCP 日志:")
            for line in lines[-10:]:
                print(f"    {line.rstrip()}")
    print("=" * 50)

    shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    main()
