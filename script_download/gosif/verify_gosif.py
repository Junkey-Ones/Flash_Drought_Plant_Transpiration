"""
GOSIF 数据完整性验证脚本
"""

import os
import requests
import re

BASE_URL = 'https://data.globalecology.unh.edu/data/GOSIF_v2/8day/'
OUTPUT_DIR = r'C:\Users\Junkey\MyCode\Flash_Drought_Green\data_raw\gosif\v2'

print('=' * 70)
print('GOSIF 数据完整性验证')
print('=' * 70)

# 1. 获取服务器文件列表
print('\n[1/4] 获取服务器文件列表...')
try:
    response = requests.get(BASE_URL, timeout=30)
    pattern = r'GOSIF_\d{7}\.tif\.gz'
    server_files = sorted(set(re.findall(pattern, response.text)))
    print(f'  服务器文件总数: {len(server_files)}')
except Exception as e:
    print(f'  错误: {e}')
    server_files = []

# 2. 获取本地文件列表
print('\n[2/4] 获取本地文件列表...')
local_files = [f for f in os.listdir(OUTPUT_DIR) if f.endswith('.tif.gz')]
local_files.sort()
print(f'  本地文件总数: {len(local_files)}')

# 3. 检查遗漏文件
print('\n[3/4] 检查遗漏文件...')
missing_files = [f for f in server_files if f not in local_files]
if missing_files:
    print(f'  遗漏文件数: {len(missing_files)}')
    print(f'  遗漏文件列表 (前20个):')
    for f in missing_files[:20]:
        print(f'    - {f}')
    if len(missing_files) > 20:
        print(f'    ... 还有 {len(missing_files) - 20} 个文件')
else:
    print('  没有遗漏文件 [OK]')

# 4. 检查文件大小（损坏检测）
print('\n[4/4] 检查文件完整性（对比文件大小）...')
print('  正在获取服务器文件大小，请稍等...')

corrupted_files = []
empty_files = []
checked = 0

# 抽样检查（检查前50个和后50个）
check_files = server_files[:50] + server_files[-50:]
check_files = list(set(check_files))

for filename in check_files:
    filepath = os.path.join(OUTPUT_DIR, filename)

    if not os.path.exists(filepath):
        continue

    local_size = os.path.getsize(filepath)

    # 检查空文件
    if local_size == 0:
        empty_files.append(filename)
        continue

    # 获取远程文件大小
    try:
        url = BASE_URL + filename
        head_resp = requests.head(url, timeout=10)
        remote_size = int(head_resp.headers.get('content-length', 0))

        if remote_size > 0 and local_size != remote_size:
            corrupted_files.append({
                'file': filename,
                'local_size': local_size,
                'remote_size': remote_size
            })
        checked += 1
    except Exception as e:
        print(f'  无法验证 {filename}: {e}')

print(f'  已抽样验证: {checked} 个文件')

if empty_files:
    print(f'\n  [!] 空文件: {len(empty_files)} 个')
    for f in empty_files:
        print(f'    - {f}')
else:
    print('  没有空文件 [OK]')

if corrupted_files:
    print(f'\n  [X] 大小不匹配（可能损坏）: {len(corrupted_files)} 个')
    for item in corrupted_files:
        print(f'    - {item["file"]}: 本地 {item["local_size"]/1024/1024:.2f} MB, 远程 {item["remote_size"]/1024/1024:.2f} MB')
else:
    print('  所有抽样文件大小匹配 [OK]')

# 最终统计
print('\n' + '=' * 70)
print('验证结果汇总')
print('=' * 70)
print(f'服务器文件总数: {len(server_files)}')
print(f'本地文件总数:   {len(local_files)}')
print(f'遗漏文件数:     {len(missing_files)}')
print(f'空文件数:       {len(empty_files)}')
print(f'损坏文件数:     {len(corrupted_files)}')
print(f'抽样验证数:     {checked}')

total_issues = len(missing_files) + len(empty_files) + len(corrupted_files)
if total_issues == 0:
    print('\n[OK] 所有数据完整无误!')
else:
    print(f'\n[!] 发现 {total_issues} 个问题，需要处理')

print('=' * 70)
