"""
GLEAM SFTP 交互式浏览器
输入命令来浏览服务器目录，输入 quit 退出
"""

import os
import paramiko
import stat
import logging

SFTP_HOST = "aether.ugent.be"
SFTP_PORT = 2225
SFTP_USERNAME = os.environ.get("GLEAM_SFTP_USERNAME", "gleamuser")
SFTP_PASSWORD = os.environ.get("GLEAM_SFTP_PASSWORD")

logging.basicConfig(level=logging.WARNING)


def format_size(size_bytes):
    """格式化文件大小"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def main():
    # 连接
    print(f"正在连接 {SFTP_HOST}:{SFTP_PORT}...")
    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
    transport.connect(username=SFTP_USERNAME, password=SFTP_PASSWORD)
    sftp = paramiko.SFTPClient.from_transport(transport)
    print("✅ 连接成功！\n")

    current_path = "."

    while True:
        try:
            cmd = input(f"sftp:{current_path}> ").strip()

            if not cmd:
                continue

            parts = cmd.split(maxsplit=1)
            action = parts[0].lower()

            if action in ("quit", "exit", "q"):
                break

            elif action == "ls":
                # 列出当前目录
                target = parts[1] if len(parts) > 1 else current_path
                try:
                    items = sftp.listdir_attr(target)
                    for item in sorted(items, key=lambda x: (not stat.S_ISDIR(x.st_mode), x.filename)):
                        is_dir = stat.S_ISDIR(item.st_mode)
                        if is_dir:
                            print(f"  📁 {item.filename}/")
                        else:
                            print(f"  📄 {item.filename}  ({format_size(item.st_size)})")
                    print(f"\n共 {len(items)} 项")
                except Exception as e:
                    print(f"❌ 错误: {e}")

            elif action == "cd":
                if len(parts) < 2:
                    print("用法: cd <目录名>")
                    continue
                target = parts[1]
                if target == "..":
                    # 返回上级
                    if current_path == ".":
                        print("已经在根目录")
                    else:
                        current_path = "/".join(current_path.split("/")[:-1]) or "."
                elif target == "/":
                    current_path = "."
                else:
                    new_path = f"{current_path}/{target}" if current_path != "." else target
                    # 验证路径是否存在
                    try:
                        sftp.listdir_attr(new_path)
                        current_path = new_path
                    except Exception as e:
                        print(f"❌ 目录不存在: {e}")

            elif action == "pwd":
                print(current_path)

            elif action == "tree":
                # 显示目录树（递归2层）
                target = parts[1] if len(parts) > 1 else current_path
                depth = 2
                try:
                    items = sftp.listdir_attr(target)
                    print(f"📁 {target}/")
                    for item in sorted(items, key=lambda x: (not stat.S_ISDIR(x.st_mode), x.filename)):
                        is_dir = stat.S_ISDIR(item.st_mode)
                        if is_dir:
                            print(f"  📁 {item.filename}/")
                            # 递归一层
                            try:
                                sub_items = sftp.listdir_attr(f"{target}/{item.filename}")
                                for sub in sub_items[:10]:
                                    sub_is_dir = stat.S_ISDIR(sub.st_mode)
                                    if sub_is_dir:
                                        print(f"    📁 {sub.filename}/")
                                    else:
                                        print(f"    📄 {sub.filename}  ({format_size(sub.st_size)})")
                                if len(sub_items) > 10:
                                    print(f"    ... 还有 {len(sub_items) - 10} 项")
                            except:
                                pass
                        else:
                            print(f"  📄 {item.filename}  ({format_size(item.st_size)})")
                except Exception as e:
                    print(f"❌ 错误: {e}")

            elif action == "help":
                print("""
可用命令:
  ls [路径]        - 列出目录内容
  cd <目录名>      - 进入目录 (cd .. 返回上级)
  pwd              - 显示当前路径
  tree [路径]      - 显示目录树
  quit / exit / q  - 退出
                """)

            else:
                print(f"未知命令: {action}，输入 help 查看帮助")

        except KeyboardInterrupt:
            print("\n使用 quit 退出")
        except EOFError:
            break

    sftp.close()
    transport.close()
    print("\n👋 已断开连接")


if __name__ == "__main__":
    main()
