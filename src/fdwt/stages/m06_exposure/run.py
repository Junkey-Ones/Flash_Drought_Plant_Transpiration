"""M6 独立命令入口，处理细节见同目录阶段脚本。

输入为冻结 M0 配置及本阶段上游完成清单。输出按本阶段目录发布。
默认沿用上游的完整范围。试跑结果位于 data/frozen/test__...。
最小示例：python -B src/fdwt/stages/m06_exposure/run.py --help
退出码：0 成功，1 执行失败，2 预检失败，130 中断。
"""
from pathlib import Path
import sys


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(root / "workflow"))
    from downstream import main

    sys.exit(main(stage=6))
