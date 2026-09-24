"""AgentScope 内置工具（Bash / 文件读写 / 任务规划）的组装与权限边界。

安全模型（详见 ARCHITECTURE.md「内置工具与权限模型」一节）：
- 模式由请求指定（默认 DEFAULT，前端可切换；BYPASS 永不开放）：
  DEFAULT/ACCEPT_EDITS 下未授权操作产出 ASK → agent park，由 confirm.py
  的确认链路推给前端应答；DONT_ASK 下 ASK 一律转 DENY（无人值守）。
- Read/Glob/Grep 只读放行；Write/Edit 仅工作目录内自动放行（ACCEPT_EDITS/DONT_ASK）；
  Task 四件套恒放行；Bash 只读白名单命令放行、文件变更命令仅限工作目录。
- deny 规则在所有模式中优先级最高：封禁 .env/.db/.git/.ssh 等敏感路径，
  以及应用自身目录（backend/ 下除工作区外的全部内容）。
- 已知残留（非硬隔离）：框架把服务器进程 cwd 也视为工作目录，Bash 对 cwd 内
  路径的文件命令可写（Write/Edit 已被 deny 规则挡住）；Bash 只读白名单命令
  可读宿主上未被 deny 的普通文件。硬隔离升级路径为 DockerWorkspace。
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from agentscope.permission import (
    AdditionalWorkingDirectory,
    PermissionBehavior,
    PermissionContext,
    PermissionMode,
    PermissionRule,
)
from agentscope.tool import (
    Bash,
    Edit,
    Glob,
    Grep,
    PowerShell,
    Read,
    TaskCreate,
    TaskGet,
    TaskList,
    TaskUpdate,
    ToolBase,
    Write,
)

from .config import settings

# 工作区根目录：与 files/（上传图片）同级，每个会话一个子目录
WORKSPACES_DIR = Path(__file__).resolve().parent.parent / settings.workspaces_dir

# 敏感路径 glob（fnmatch 语义，`*` 跨目录分隔符）：Read/Write/Edit 按 file_path 匹配，
# Glob/Grep 按其 path 参数匹配（后者保护有限，聊胜于无）
_SENSITIVE_PATTERNS = [
    "**/.env*",
    "**/*.db",
    "**/.git*",
    "**/.ssh*",
    "**/.aws*",
    "**/.gnupg*",
    "**/.kube*",
    "**/*.pem",
    "**/id_rsa*",
]

# 受 deny 规则约束的文件类工具（工具名与 AgentScope 内置类一致）
_FILE_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]


def conversation_workspace_dir(conversation_id: UUID) -> Path:
    """本会话专属工作目录：workspaces/<conversation_id>/。"""
    return WORKSPACES_DIR / str(conversation_id)


def resolve_workspace(conversation_id: UUID, custom_root: str | None) -> Path:
    """解析本轮工作区：会话自选目录优先（已存在，不 mkdir），否则默认会话目录。

    返回值由调用方决定是否 mkdir：默认目录需要创建，自选目录必已存在
    （validate_workspace_root 在落库时已校验）。
    """
    if custom_root:
        return Path(custom_root).expanduser().resolve()
    return conversation_workspace_dir(conversation_id)


def _deny_patterns() -> list[str]:
    """deny 规则的 glob 清单：敏感路径 + 应用自身目录（工作区是唯一豁免）。

    backend/ 顶层按 os.listdir 动态枚举而非硬编码，新增源码/配置/日志文件
    自动进入保护范围；工作区子树必须豁免，否则写入功能自相矛盾。
    """
    backend_dir = Path(__file__).resolve().parent.parent
    patterns = list(_SENSITIVE_PATTERNS)
    for name in os.listdir(backend_dir):
        if name == settings.workspaces_dir:
            continue
        patterns.append(f"{backend_dir}/{name}*")
    return patterns


def build_builtin_tools(
    workspace_dir: Path,
    mode: PermissionMode = PermissionMode.DONT_ASK,
) -> tuple[list[ToolBase], PermissionContext]:
    """组装内置工具实例与权限上下文。workspace_dir 由调用方创建。

    mode 由请求指定（默认 DEFAULT，前端可切换）：DEFAULT/ACCEPT_EDITS 下
    未授权操作会产出 ASK → agent park，由 confirm.py 的确认链路接手。
    """
    tools: list[ToolBase] = [
        Bash(cwd=str(workspace_dir)),
        Read(),
        Write(),
        Edit(),
        Glob(),
        Grep(),
        TaskCreate(),
        TaskGet(),
        TaskList(),
        TaskUpdate(),
    ]
    # Windows 才有 PowerShell（惰性探测可执行文件，macOS/Linux 实例化安全）
    if os.name == "nt":
        tools.append(PowerShell(cwd=str(workspace_dir)))

    patterns = _deny_patterns()
    deny_rules: dict[str, list[PermissionRule]] = {
        tool_name: [
            PermissionRule(
                tool_name=tool_name,
                rule_content=pattern,
                behavior=PermissionBehavior.DENY,
                source="appSettings",
            )
            for pattern in patterns
        ]
        for tool_name in _FILE_TOOLS
    }

    # 本机开发逃生门：DONT_ASK 兜底会拒绝所有非白名单 Bash 命令，
    # AGENT_TOOLS_BASH_ALLOW_PREFIXES 配置的前缀经 allow 规则放行（子串匹配，
    # deny 规则仍最高优先）。命令在后端所在机器上执行，勿在多用户部署中开启。
    allow_rules: dict[str, list[PermissionRule]] = {}
    prefixes = [p.strip() for p in settings.agent_tools_bash_allow_prefixes.split(",") if p.strip()]
    if prefixes:
        allow_rules["Bash"] = [
            PermissionRule(
                tool_name="Bash",
                rule_content=prefix,
                behavior=PermissionBehavior.ALLOW,
                source="appSettings",
            )
            for prefix in prefixes
        ]

    context = PermissionContext(
        mode=mode,
        working_directories={
            str(workspace_dir): AdditionalWorkingDirectory(
                path=str(workspace_dir),
                source="session",
            ),
        },
        deny_rules=deny_rules,
        allow_rules=allow_rules,
    )
    return tools, context


def apply_session_rules(context: PermissionContext, rules: list[PermissionRule]) -> None:
    """把会话级「总是允许」规则重放进权限上下文（每轮新建 Agent 后调用）。"""
    for rule in rules:
        context.allow_rules.setdefault(rule.tool_name, []).append(rule)
