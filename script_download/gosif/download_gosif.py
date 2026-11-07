"""
GOSIF v2 数据集下载脚本
下载地址: https://data.globalecology.unh.edu/data/GOSIF_v2/8day/
时间范围: 2000-2024年
数据格式: GOSIF_YYYYDDD.tif.gz (8天合成)
"""

import os
import requests
import time
from datetime import datetime, timedelta
from tqdm import tqdm
import logging

# ============ 配置参数 ============
BASE_URL = "https://data.globalecology.unh.edu/data/GOSIF_v2/8day/"
OUTPUT_DIR = r"C:\Users\Junkey\MyCode\Flash_Drought_Green\data_raw\gosif\v2"
START_YEAR = 2003
END_YEAR = 2022
MAX_RETRIES = 3
RETRY_DELAY = 5  # 秒
CHUNK_SIZE = 8192

# ============ 日志配置 ============
LOG_FILE = r"C:\Users\Junkey\MyCode\Flash_Drought_Green\logs\download\gosif\download_gosif.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def get_8day_dates(year):
    """
    获取指定年份的所有8天合成日期（儒略日）
    返回格式: [001, 009, 017, ..., 361]
    """
    dates = []
    # 从1月1日开始，每8天一个
    start = datetime(year, 1, 1)
    end = datetime(year, 12, 31)

    current = start
    while current <= end:
        day_of_year = current.timetuple().tm_yday
        dates.append(f"{day_of_year:03d}")
        current += timedelta(days=8)

    return dates


def generate_file_list(start_year, end_year):
    """
    生成所有需要下载的文件URL列表
    """
    file_list = []

    for year in range(start_year, end_year + 1):
        dates = get_8day_dates(year)
        for day in dates:
            filename = f"GOSIF_{year}{day}.tif.gz"
            url = BASE_URL + filename
            file_list.append({
                'url': url,
                'filename': filename,
                'year': year,
                'day': day
            })

    return file_list


def download_file(url, filepath, retries=MAX_RETRIES):
    """
    下载单个文件，支持断点续传和重试
    """
    for attempt in range(retries):
        try:
            # 检查文件是否已存在
            if os.path.exists(filepath):
                local_size = os.path.getsize(filepath)
                # 如果文件已下载完成，跳过
                if local_size > 0:
                    # 尝试获取远程文件大小
                    try:
                        head_response = requests.head(url, timeout=10)
                        remote_size = int(head_response.headers.get('content-length', 0))
                        if remote_size > 0 and local_size == remote_size:
                            logger.info(f"文件已存在，跳过: {os.path.basename(filepath)}")
                            return True
                    except:
                        pass

            # 支持断点续传
            headers = {}
            initial_pos = 0
            if os.path.exists(filepath):
                initial_pos = os.path.getsize(filepath)
                headers['Range'] = f'bytes={initial_pos}-'

            response = requests.get(url, headers=headers, stream=True, timeout=30)

            # 处理响应状态
            if response.status_code == 416:  # Range Not Satisfiable - 文件已下载完成
                logger.info(f"文件已下载完成: {os.path.basename(filepath)}")
                return True

            if response.status_code == 404:
                logger.warning(f"文件不存在 (404): {os.path.basename(filepath)}")
                return False

            response.raise_for_status()

            # 获取文件总大小
            total_size = int(response.headers.get('content-length', 0))
            if initial_pos > 0 and response.status_code == 206:
                total_size += initial_pos

            # 下载文件
            mode = 'ab' if initial_pos > 0 and response.status_code == 206 else 'wb'
            with open(filepath, mode) as f:
                with tqdm(
                    total=total_size,
                    initial=initial_pos,
                    unit='B',
                    unit_scale=True,
                    desc=os.path.basename(filepath),
                    ncols=80
                ) as pbar:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            pbar.update(len(chunk))

            logger.info(f"下载完成: {os.path.basename(filepath)}")
            return True

        except requests.exceptions.RequestException as e:
            logger.warning(f"下载失败 (尝试 {attempt + 1}/{retries}): {os.path.basename(filepath)} - {str(e)}")
            if attempt < retries - 1:
                time.sleep(RETRY_DELAY)
            else:
                logger.error(f"下载失败，已重试 {retries} 次: {os.path.basename(filepath)}")
                return False

        except Exception as e:
            logger.error(f"未知错误: {os.path.basename(filepath)} - {str(e)}")
            return False

    return False


def main():
    """
    主函数
    """
    logger.info("=" * 60)
    logger.info("GOSIF v2 数据集下载开始")
    logger.info(f"时间范围: {START_YEAR}-{END_YEAR}")
    logger.info(f"下载目录: {OUTPUT_DIR}")
    logger.info("=" * 60)

    # 创建输出目录
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 生成文件列表
    file_list = generate_file_list(START_YEAR, END_YEAR)
    total_files = len(file_list)
    logger.info(f"共需下载 {total_files} 个文件")

    # 统计信息
    success_count = 0
    fail_count = 0
    skip_count = 0
    failed_files = []

    # 开始下载
    start_time = time.time()

    for i, file_info in enumerate(file_list, 1):
        url = file_info['url']
        filename = file_info['filename']
        filepath = os.path.join(OUTPUT_DIR, filename)

        logger.info(f"[{i}/{total_files}] 正在处理: {filename}")

        success = download_file(url, filepath)

        if success:
            if os.path.exists(filepath):
                success_count += 1
            else:
                skip_count += 1
        else:
            fail_count += 1
            failed_files.append(filename)

        # 每下载100个文件显示一次统计
        if i % 100 == 0:
            elapsed = time.time() - start_time
            logger.info(f"进度: {i}/{total_files} | 成功: {success_count} | 失败: {fail_count} | 耗时: {elapsed:.1f}秒")

    # 最终统计
    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info("下载完成!")
    logger.info(f"总文件数: {total_files}")
    logger.info(f"成功下载: {success_count}")
    logger.info(f"跳过(已存在): {skip_count}")
    logger.info(f"下载失败: {fail_count}")
    logger.info(f"总耗时: {elapsed:.1f}秒 ({elapsed/60:.1f}分钟)")

    if failed_files:
        logger.info("\n失败的文件列表:")
        for f in failed_files:
            logger.info(f"  - {f}")

        # 保存失败列表到文件
        with open('failed_downloads.txt', 'w') as f:
            for filename in failed_files:
                f.write(filename + '\n')
        logger.info("失败文件列表已保存到: failed_downloads.txt")

    logger.info("=" * 60)


if __name__ == "__main__":
    main()
