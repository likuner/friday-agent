"""friday-agent 子项目共享基础设施。

目前只含统一日志配置（logging_setup），供 backend / mcp-server 两个独立
venv 以 `-e ../shared` editable 方式安装使用；后续跨子项目的通用工具放这里。
"""

from .logging_setup import setup_service_logging

__all__ = ["setup_service_logging"]
