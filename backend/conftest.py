# pytest 根配置：保证从任意目录运行都能导入 backend 下的 app 包
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
