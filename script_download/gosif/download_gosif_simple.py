"""
GOSIF v2 数据集下载脚本 (简化版)
下载地址: https://data.globalecology.unh.edu/data/GOSIF_v2/8day/
时间范围: 2000-2024年
"""

import os
import requests
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import re

# ============ 配置参数 ============
BASE_URL = "https://data.globalecology.unh.edu/data/GOSIF_v2/8day/"
OUTPUT_DIR = r"C:\Users\Junkey\MyCode\Flash_Drought_Green\data_raw\gosif\v2"
START_YEAR = 2003
END_YEAR = 2022
MAX_WORKERS = 8
CHUNK_SIZE = 8192

# 统计数据
stats = {
    'total': 0,
    'completed': 0,
    'failed': 0,
    'skipped': 0,
    'bytes_downloaded': 0,
    'start_time': 0
}
stats_lock = threading.Lock()


def get_file_list_from_server():
    """从服务器获取实际的文件列表"""
    try:
        print("[信息] 正在从服务器获取文件列表...")
        response = requests.get(BASE_URL, timeout=30)
        response.raise_for_status()

        # 提取所有文件名
        pattern = r'GOSIF_\d{7}\.tif\.gz'
        files = sorted(set(re.findall(pattern, response.text)))

        print(f"[信息] 从服务器获取到 {len(files)} 个文件")
        return files

    except Exception as e:
        print(f"[错误] 获取文件列表失败: {e}")
        return []


def download_file(filename):
    """下载单个文件"""
    url = BASE_URL + filename
    filepath = os.path.join(OUTPUT_DIR, filename)

    try:
        # 检查文件是否已存在
        if os.path.exists(filepath):
            local_size = os.path.getsize(filepath)
            if local_size > 0:
                # 检查远程文件大小
                try:
                    head_response = requests.head(url, timeout=10)
                    remote_size = int(head_response.headers.get('content-length', 0))
                    if remote_size > 0 and local_size == remote_size:
                        with stats_lock:
                            stats['skipped'] += 1
                        return {'file': filename, 'status': 'skipped', 'success': True}
                except:
                    pass

        # 下载文件
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()

        downloaded_bytes = 0
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
                    downloaded_bytes += len(chunk)

        with stats_lock:
            stats['completed'] += 1
            stats['bytes_downloaded'] += downloaded_bytes

        return {'file': filename, 'status': 'completed', 'size': downloaded_bytes, 'success': True}

    except Exception as e:
        with stats_lock:
            stats['failed'] += 1
        return {'file': filename, 'status': 'failed', 'error': str(e), 'success': False}


def display_progress():
    """显示下载进度"""
    while True:
        time.sleep(5)
        with stats_lock:
            completed = stats['completed'] + stats['skipped']
            total = stats['total']
            failed = stats['failed']
            elapsed = time.time() - stats['start_time']
            speed = stats['bytes_downloaded'] / elapsed if elapsed > 0 else 0

            if completed > 0:
                avg_time = elapsed / completed
                remaining = (total - completed) * avg_time
                eta = time.strftime("%H:%M:%S", time.gmtime(remaining))
            else:
                eta = "计算中..."

            print(f"\r[进度] {completed}/{total} | 失败: {failed} | 速度: {speed/1024/1024:.2f} MB/s | 已用时: {time.strftime('%H:%M:%S', time.gmtime(elapsed))} | 预计剩余: {eta}", end='', flush=True)

            if completed >= total:
                break


def main():
    """主函数"""
    print("=" * 70)
    print("[启动] GOSIF v2 数据集下载 (简化版)")
    print(f"[时间] 时间范围: {START_YEAR}-{END_YEAR}")
    print(f"[目录] 下载目录: {OUTPUT_DIR}")
    print(f"[线程] 并行线程数: {MAX_WORKERS}")
    print("=" * 70)

    # 创建输出目录
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 获取文件列表
    server_files = get_file_list_from_server()
    if not server_files:
        print("[错误] 无法获取文件列表，退出")
        return

    # 筛选指定年份的文件
    file_list = []
    for filename in server_files:
        year = int(filename[6:10])
        if START_YEAR <= year <= END_YEAR:
            file_list.append(filename)

    stats['total'] = len(file_list)
    print(f"[数量] 共需下载 {stats['total']} 个文件")

    # 记录开始时间
    stats['start_time'] = time.time()

    # 启动进度显示线程
    progress_thread = threading.Thread(target=display_progress, daemon=True)
    progress_thread.start()

    # 使用线程池下载
    failed_files = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(download_file, f): f for f in file_list}
        for future in as_completed(futures):
            result = future.result()
            if not result['success']:
                failed_files.append(result['file'])

    # 等待进度线程结束
    progress_thread.join(timeout=10)

    # 最终统计
    elapsed = time.time() - stats['start_time']
    print("\n" + "=" * 70)
    print("[统计] 下载完成统计:")
    print(f"   总文件数: {stats['total']}")
    print(f"   成功下载: {stats['completed']}")
    print(f"   跳过(已存在): {stats['skipped']}")
    print(f"   下载失败: {stats['failed']}")
    print(f"   总下载量: {stats['bytes_downloaded'] / (1024**3):.2f} GB")
    print(f"   总耗时: {time.strftime('%H:%M:%S', time.gmtime(elapsed))}")

    if failed_files:
        print(f"\n[失败] 失败文件列表 ({len(failed_files)} 个):")
        for f in failed_files[:20]:
            print(f"   - {f}")
        if len(failed_files) > 20:
            print(f"   ... 还有 {len(failed_files) - 20} 个文件")

        # 保存失败列表
        with open('failed_downloads.txt', 'w', encoding='utf-8') as f:
            for filename in failed_files:
                f.write(filename + '\n')
        print("   失败文件列表已保存到: failed_downloads.txt")

    print("=" * 70)


if __name__ == "__main__":
    main()
