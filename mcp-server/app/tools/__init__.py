"""工具自动注册：遍历本包下的子模块并 import，触发模块内 @mcp.tool 装饰完成注册。

新增公共工具的约定：
1. 在本目录新建一个模块文件（如 time_convert.py）；
2. 模块内 `from ..server import mcp` 后用 `@mcp.tool` 装饰工具函数；
3. 无需改动任何既有代码，重启服务即生效。
以下划线开头的模块不会被自动加载（预留给内部辅助代码）。
"""

import importlib
import pkgutil

for _module_info in pkgutil.iter_modules(__path__):
    if not _module_info.name.startswith("_"):
        importlib.import_module(f"{__name__}.{_module_info.name}")
