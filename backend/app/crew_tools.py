"""CrewAI 版内置工具（Bash / 文件读写 / 任务规划）的组装与权限门控。

安全模型与原 AgentScope 版一致（详见 ARCHITECTURE.md「内置工具与权限模型」）：
- 模式由请求指定（默认 DEFAULT，前端可切换；BYPASS 永不开放）：
  DEFAULT/ACCEPT_EDITS 下未授权操作产出 ASK → 工具内在 ConfirmHub 挂起，
  推 permission_ask 给前端应答（超时自动拒绝）；DONT_ASK 下 ASK 一律转 DENY。
- Read/Glob/Grep 与 Task 四件套恒放行；Write/Edit 仅工作目录内自动放行
  （ACCEPT_EDITS/DONT_ASK）；Bash 只读白名单命令放行、文件变更命令仅限
  工作目录内（判定逻辑见 crew_permissions.py）。
- deny 规则在所有模式中优先级最高：封禁 .env/.db/.git/.ssh 等敏感路径，
  以及应用自身目录（backend/ 下除工作区外的全部内容）。
- 与原实现的已知差异（沿 LangGraph 迁移版取舍）：①不复刻原框架把服务器
  进程 cwd 也视为工作目录的残留，工作目录只认会话工作区；②Windows
  PowerShell 工具不提供（部署环境为 macOS/Linux）。

工具名 / 参数 schema / 描述文本与原内置工具保持一致，模型侧行为与提示词
（AGENT_TOOLS_PROMPT）无需调整。权限 ASK 语义从原框架的 park/resume 改为
工具内阻塞等待：crew 在工作线程里跑，工具用 threading.Event 等确认接口
唤醒，SSE 流全程保活不断。
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
import os
import re
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from .config import settings
from .confirm import AskAnswer, PendingAsk, confirm_hub
from .crew_events import tool_query
from .crew_permissions import (
    Behavior,
    Decision,
    PermissionContext,
    PermissionMode,
    SessionRule,
    apply_session_rules,
    decide,
)

logger = logging.getLogger("friday.crew_tools")

__all__ = [
    "WORKSPACES_DIR",
    "AskFlow",
    "PermissionContext",
    "SessionRule",
    "apply_session_rules",
    "build_builtin_tools",
    "conversation_workspace_dir",
    "resolve_workspace",
]

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

# 受 deny 规则约束的文件类工具（与权限引擎的 _FILE_PATH_ARGS 对应）
_FILE_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]

# Bash 输出截断上限（超长时保留头尾）
_BASH_OUTPUT_LIMIT = 30000


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


# ------------------------------------------------------------- 权限 ASK 流 --

@dataclass
class AskFlow:
    """工具内权限确认流：注册 PendingAsk → 推 permission_ask SSE → 阻塞等应答。

    emit 经 StreamHub 线程安全地把事件送回 SSE 请求协程；等待在工具所在的
    工作线程里用 threading.Event 阻塞（确认接口 settle 时置位唤醒）。
    """

    conversation_id: UUID
    user_id: UUID
    # 归属 agent 名（团队模式为「执行员」，单 Agent 模式为 Friday）
    agent_name: str = "Friday"
    emit: Callable[[dict], None] = lambda event: None

    def ask(self, tool_name: str, tool_input: dict, decision: Decision) -> tuple[bool, list[SessionRule]]:
        """推卡片并阻塞等待；返回 (是否批准, 需立即生效的 always 规则)。"""
        reply_id = uuid4().hex
        pending = PendingAsk(
            conversation_id=self.conversation_id,
            user_id=self.user_id,
            reply_id=reply_id,
            calls=[{"id": reply_id, "name": tool_name, "query": tool_query(tool_input)}],
        )
        confirm_hub.register(pending)
        logger.info(
            "节点[权限确认] conversation=%s agent=%s tool=%s query=%r",
            self.conversation_id, self.agent_name, tool_name, pending.calls[0]["query"],
        )
        self.emit({
            "type": "permission_ask",
            "reply_id": reply_id,
            "calls": pending.calls,
            "agent": self.agent_name,
        })
        timed_out = not pending.event.wait(timeout=settings.permission_confirm_timeout_seconds)
        confirm_hub.cancel(self.conversation_id)
        answer: AskAnswer = pending.answer or AskAnswer(approved=False)
        self.emit({
            "type": "permission_resolved",
            "approved": answer.approved,
            "timed_out": timed_out,
        })
        # 「总是允许」：建议规则落会话级规则表（跨轮重放），并返回给调用方
        # 立即注入本轮上下文（本 reply 内马上生效）
        rules: list[SessionRule] = []
        if answer.approved and answer.always and decision.suggested_rules:
            confirm_hub.add_session_rules(self.conversation_id, decision.suggested_rules)
            rules = decision.suggested_rules
        return answer.approved, rules


# ---------------------------------------------------------------- 工具基类 --

class GatedTool(BaseTool):
    """带权限门控的同步工具壳：裁决 → （ASK 时）确认流 → 执行 async 实现。

    fn/permission_ctx/ask/stop 为装配参数（不进模型入参 schema）。
    """

    fn: Any = Field(default=None, exclude=True)
    permission_ctx: Any = Field(default=None, exclude=True)
    ask: Any = Field(default=None, exclude=True)
    stop: Any = Field(default=None, exclude=True)

    def _run(self, **kwargs: Any) -> str:
        if self.stop is not None and self.stop.is_set():
            return "Error: 本轮请求已停止，跳过工具执行。"
        if self.permission_ctx is not None:
            decision = decide(self.name, kwargs, self.permission_ctx)
            if decision.behavior is Behavior.DENY:
                return f"权限拒绝：{decision.message}。请换用安全的方式完成任务，不要原样重试。"
            if decision.behavior is Behavior.ASK:
                if self.ask is None:  # 无人值守兜底（不应发生：dont_ask 模式 ASK 已转 DENY）
                    return f"权限拒绝：{decision.message}（无人工确认通道）。"
                approved, rules = self.ask.ask(self.name, kwargs, decision)
                if not approved:
                    return "用户未批准本次操作。请换一种安全的方式完成任务，或先向用户说明意图。"
                if rules:
                    apply_session_rules(self.permission_ctx, rules)
        return asyncio.run(self.fn(**kwargs))


# ------------------------------------------------------------------ Bash 工具 --

_BASH_DESCRIPTION = """Executes a bash command and returns its output.

The working directory persists between commands, but shell state does
not. The shell environment is initialized from the user's profile
(bash or zsh).

IMPORTANT: Avoid using this tool to run `find`, `grep`, `cat`, `head`,
`tail`, `sed`, `awk`, or `echo` commands, unless explicitly instructed
or after you have verified that a dedicated tool cannot accomplish your
task. Instead, use the appropriate dedicated tool as this will give a
much better experience for the user:

 - File search: Use Glob (NOT find or ls)
 - Content search: Use Grep (NOT grep or rg)
 - Read files: Use Read (NOT cat/head/tail)
 - Edit files: Use Edit (NOT sed/awk)
 - Write files: Use Write (NOT echo >/cat <<EOF)
 - Communication: Output text directly (NOT echo/printf)

While the Bash tool can do similar things, it's better to use the
built-in tools as they give a much better experience for the user and
easier to review tool calls and permission.

# Instructions
 - If your command will create new directories or files, first use
   this tool to run `ls` to verify the parent directory exists and is
   the correct location.
 - Always quote file paths that contain spaces with double quotes in
   your command (e.g., cd "path with spaces/file.txt")
 - Try to maintain your current working directory throughout the
   session by using absolute paths and avoiding usage of `cd`. You may
   use `cd` if the User explicitly requests it.
 - You may specify an optional timeout in milliseconds (up to 600000ms
   / 10 minutes). By default, your command will timeout after 120000ms
   (2 minutes).
 - Write a clear, concise description of what your command does. For
   simple commands, keep it brief (5-10 words). For complex commands
   (piped commands, obscure flags, or anything hard to understand at a
   glance), include enough context so that the user can understand what
   your command will do.
 - When issuing multiple commands:
  - If the commands are independent and can run in parallel, make
    multiple Bash tool calls in a single message. Example: if you need
    to run "git status" and "git diff", send a single message with two
    Bash tool calls in parallel.
  - If the commands depend on each other and must be run sequentially,
    use a single Bash call with '&&' to chain them together.
  - Use ';' only when you want to run commands sequentially but don't
   care about earlier failures.
  - DO NOT use newlines to separate commands (newlines are ok in
   quoted strings).
 - For git commands:
  - Prefer to create a new commit rather than amending an existing
   commit.
  - Before running destructive operations (e.g., git reset --hard, git
   push --force, git checkout --), consider whether there is a safer
   alternative that achieves the same goal. Only use destructive
   operations when they are truly the best approach.
  - Never skip hooks (--no-verify) or bypass signing (--no-gpg-sign,
   -c commit.gpgsign=false) unless the user has explicitly asked for
   it. If a hook fails, investigate and fix the underlying issue.
 - Avoid unnecessary `sleep` commands:
  - Do not sleep between commands that can run immediately — just run
   them.
  - Do not retry failing commands in a sleep loop — diagnose the root
   cause or consider an alternative.
  - If you must sleep, keep the duration short (1-5 seconds)."""


class _BashArgs(BaseModel):
    command: str
    description: str = ""
    timeout: int = Field(default=120000, ge=0, le=600000)


def _truncate_output(text: str, limit: int = _BASH_OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit // 2], text[-limit // 2 :]
    return f"{head}\n...[输出过长，中间已截断 {len(text) - limit} 字符]...\n{tail}"


def _make_bash_fn(cwd: Path) -> Callable[..., Any]:
    async def _bash(command: str, description: str = "", timeout: int = 120000) -> str:
        seconds = min(max(timeout, 0), 600_000) / 1000
        try:
            # start_new_session：超时强杀整个进程组，避免孤儿子进程残留
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return f"Error: 无法启动命令：{exc}"
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=seconds)
        except TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()  # 回收子进程，关闭传输，避免句柄泄漏
            return f"Error: 命令超时（>{seconds:.0f}s）被强制终止：{command[:100]}"
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        parts = []
        if out.strip():
            parts.append(out.rstrip())
        if err.strip():
            parts.append(f"[stderr]\n{err.rstrip()}")
        body = "\n".join(parts) or "（命令执行成功，无输出）"
        if proc.returncode != 0:
            body = f"{body}\n[exit code: {proc.returncode}]"
        return _truncate_output(body)

    return _bash


# ------------------------------------------------------------------ 文件工具 --

_READ_DESCRIPTION = """Reads a file from the local filesystem. You can access any file directly by using this tool.
Assume this tool is able to read all files on the machine. If the User provides a path to a file assume that path is valid. It is okay to read this tool that does not exist; an error will be returned.

Usage:
- The file_path parameter must be an absolute path, not a relative path
- By default, it reads up to 2000 lines starting from the beginning of the file
- You can optionally specify a line offset and limit (especially handy for long files), but it's recommended to read the whole file without using these parameters
- Results are returned using cat -n format, with line numbers starting at 1"""

_WRITE_DESCRIPTION = """Writes a file to the local filesystem.

Usage:
- This tool will OVERWRITE the existing file if one exists at the provided path.
- If this is an existing file, you MUST use the Read tool first to read the file's contents. This tool will fail if you did not read the file first.
- ALWAYS prefer editing existing files in the codebase. Never write new files unless explicitly required.
- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested by the User.
- Only use emojis if the user explicitly requests it. Avoid writing emojis to files unless asked."""

_EDIT_DESCRIPTION = """Performs exact string replacements in files.

Usage:
- You must use your `Read` tool at least once in the conversation
  before editing. This tool will error if you attempt an edit without
  reading the file.
- When editing text from Read tool output, ensure you preserve the
  exact indentation (tabs/spaces) as it appears AFTER the line number
  prefix. The line number prefix format is: line number + tab.
  Everything after that is the actual file content to match. Never
  include any part of the line number prefix in the old_string or
  new_string.
- ALWAYS prefer editing existing files in the codebase. NEVER write
  new files unless explicitly required.
- Only use emojis if the user explicitly requests it. Avoid adding
  emojis to files unless asked.
- The edit will FAIL if `old_string` is not unique in the file."""

_GLOB_DESCRIPTION = """Fast file pattern matching tool that works with
any codebase size.

Supports glob patterns like "**/*.js" or "src/**/*.ts" and returns
matching file paths sorted by modification time (newest first).

Use this tool when you need to find files by pattern across the
codebase."""

_GREP_DESCRIPTION = """A powerful search tool for file contents

  Usage:
- ALWAYS use Grep for search tasks. NEVER invoke `grep` or `rg` as a Bash command.
- Supports full regex syntax (e.g., "log.*Error", "function\\s+\\w+")
- Filter files with glob parameter (e.g., "*.js", "**/*.tsx") or type parameter (e.g., "js", "py", "rust")
- Output modes: "content" shows matching lines, "files_with_matches" shows only file paths (default), "count" shows match counts per file
- Context lines: use context parameter for lines after/before/around matches
- Case-insensitive search: set i to true
- Multiline regex: set multiline to true for patterns spanning multiple lines
- Limit results: use head_limit to cap the number of results returned"""


class _ReadArgs(BaseModel):
    file_path: str
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=2000, ge=1, le=2000)


class _WriteArgs(BaseModel):
    file_path: str
    content: str


class _EditArgs(BaseModel):
    file_path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class _GlobArgs(BaseModel):
    pattern: str
    path: str = ""


class _GrepArgs(BaseModel):
    pattern: str
    path: str = ""
    output_mode: str = "files_with_matches"
    glob: str | None = None
    type: str | None = None
    context: int | None = None
    n: bool = True
    i: bool = False
    case_insensitive: bool = False
    multiline: bool = False
    head_limit: int | None = Field(default=None, ge=0)
    offset: int = Field(default=0, ge=0)


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


async def _read(file_path: str, offset: int = 1, limit: int = 2000) -> str:
    if not os.path.isabs(os.path.expanduser(file_path)):
        return f"Error: file_path 必须是绝对路径，收到：{file_path}"
    path = _expand(file_path)
    if not path.exists():
        return f"Error: 文件不存在：{file_path}"
    if path.is_dir():
        return f"Error: {file_path} 是目录，请用 Glob 列出文件"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return f"Error: 读取失败：{exc}"
    if b"\x00" in raw[:8192]:
        return "Error: 二进制文件（图片/PDF 等媒体文件暂不支持内容读取）"
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if offset > 1 or len(lines) > offset - 1 + limit:
        snippet = lines[offset - 1 : offset - 1 + limit]
        body = "".join(f"{idx}\t{line}\n" for idx, line in enumerate(snippet, start=offset))
        note = f"\n（文件共 {len(lines)} 行，当前显示第 {offset}–{offset - 1 + len(snippet)} 行）"
        return body.rstrip("\n") + note
    return "".join(f"{idx}\t{line}\n" for idx, line in enumerate(lines, start=1)).rstrip("\n")


async def _write(file_path: str, content: str) -> str:
    if not os.path.isabs(os.path.expanduser(file_path)):
        return f"Error: file_path 必须是绝对路径，收到：{file_path}"
    path = _expand(file_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"Error: 写入失败：{exc}"
    return f"已写入 {path}（{len(content)} 字符）"


async def _edit(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    if not os.path.isabs(os.path.expanduser(file_path)):
        return f"Error: file_path 必须是绝对路径，收到：{file_path}"
    path = _expand(file_path)
    if not path.exists():
        return f"Error: 文件不存在：{file_path}"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"Error: 读取失败：{exc}"
    count = text.count(old_string)
    if count == 0:
        return "Error: 未找到要替换的内容（old_string 不在文件中）"
    if count > 1 and not replace_all:
        return f"Error: old_string 出现 {count} 次，不唯一；请提供更长的上下文或设置 replace_all=true"
    try:
        path.write_text(text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1), encoding="utf-8")
    except OSError as exc:
        return f"Error: 写回失败：{exc}"
    return f"已替换 {count if replace_all else 1} 处：{path}"


async def _glob(pattern: str, path: str = "") -> str:
    base = _expand(path) if path else Path.cwd()
    if not base.exists():
        return f"Error: 目录不存在：{path}"
    try:
        matches = [p for p in base.glob(pattern) if p.is_file()]
    except (OSError, ValueError) as exc:
        return f"Error: 匹配失败：{exc}"
    if not matches:
        return "未找到匹配文件"
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    shown = matches[:100]
    body = "\n".join(str(p) for p in shown)
    if len(matches) > 100:
        body += f"\n（共 {len(matches)} 个匹配，仅显示前 100 个）"
    return body


# Grep 的 type 参数 → 扩展名映射（rg --type 的常用子集）
_TYPE_EXTS = {
    "js": {".js", ".jsx", ".mjs"}, "ts": {".ts", ".tsx"},
    "py": {".py"}, "rust": {".rs"}, "go": {".go"}, "java": {".java"},
    "c": {".c", ".h"}, "cpp": {".cpp", ".cc", ".hpp"}, "md": {".md"},
    "json": {".json"}, "yaml": {".yaml", ".yml"}, "html": {".html", ".htm"},
    "css": {".css"}, "sh": {".sh", ".bash", ".zsh"},
}


async def _grep(
    pattern: str,
    path: str = "",
    output_mode: str = "files_with_matches",
    glob: str | None = None,
    type: str | None = None,
    context: int | None = None,
    n: bool = True,
    i: bool = False,
    case_insensitive: bool = False,
    multiline: bool = False,
    head_limit: int | None = None,
    offset: int = 0,
) -> str:
    flags = re.MULTILINE
    if i or case_insensitive:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.DOTALL
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return f"Error: 非法正则：{exc}"
    base = _expand(path) if path else Path.cwd()
    if base.is_file():
        files = [base]
    elif base.is_dir():
        exts = _TYPE_EXTS.get(type or "", None) if type else None
        files = []
        for root, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv"}]
            for name in names:
                if glob and not fnmatch.fnmatch(name, glob):
                    continue
                if exts and Path(name).suffix not in exts:
                    continue
                files.append(Path(root) / name)
            if len(files) > 2000:
                break
    else:
        return f"Error: 路径不存在：{path}"

    limit = head_limit if head_limit is not None else 250
    matched_files: list[str] = []
    content_lines: list[str] = []
    counts: list[str] = []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\x00" in text[:4096]:
            continue
        lines = text.splitlines()
        hits = [idx for idx, line in enumerate(lines) if regex.search(line)] or (
            [0] if multiline and regex.search(text) else []
        )
        if not hits:
            continue
        matched_files.append(str(file))
        counts.append(f"{file}: {len(hits)}")
        if output_mode == "content":
            for hit in hits:
                block = []
                if context:
                    start, end = max(0, hit - context), min(len(lines), hit + context + 1)
                    for idx in range(start, end):
                        prefix = f"{idx + 1}:" if n else ""
                        marker = "->" if idx == hit else "  "
                        block.append(f"{file}{':' if n else ''}{prefix}{marker} {lines[idx]}")
                else:
                    prefix = f"{hit + 1}:" if n else ""
                    block.append(f"{file}:{prefix}{lines[hit].strip()}")
                content_lines.append("\n".join(block))

    if output_mode == "files_with_matches":
        results = matched_files
    elif output_mode == "count":
        results = counts
    else:
        results = content_lines
    total = len(results)
    window = results[offset : offset + limit] if limit else results[offset:]
    if not window:
        return "未找到匹配内容"
    body = "\n".join(window)
    suffix = f"（共 {total} 条结果" + (f"，显示 {offset + 1}–{offset + len(window)}" if total > len(window) else "") + "）"
    return body + ("\n" + suffix if total > len(window) or offset else "")


# ----------------------------------------------------------------- 任务工具 --

class _Task:
    __slots__ = ("id", "subject", "description", "status", "owner", "blocks", "blocked_by", "metadata")

    def __init__(self, task_id: int, subject: str, description: str, metadata: dict | None) -> None:
        self.id = str(task_id)
        self.subject = subject
        self.description = description
        self.status = "pending"
        self.owner = ""
        self.blocks: list[str] = []
        self.blocked_by: list[str] = []
        self.metadata: dict = dict(metadata or {})


class TaskStore:
    """每请求独立的任务清单（与原 AgentState.tasks_context 同生命周期，不跨轮持久）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, _Task] = {}
        self._next = 1

    def create(self, subject: str, description: str, metadata: dict | None) -> str:
        task = _Task(self._next, subject, description, metadata)
        self._tasks[task.id] = task
        self._next += 1
        return f"已创建任务 {task.id}：{subject}"

    def get(self, task_id: str) -> str:
        task = self._tasks.get(task_id)
        if not task:
            return f"Error: 任务 {task_id} 不存在"
        return self._render(task)

    def listing(self) -> str:
        if not self._tasks:
            return "当前没有任务"
        return "\n".join(self._summary(t) for t in self._tasks.values())

    def update(self, task_id: str, **changes: Any) -> str:
        task = self._tasks.get(task_id)
        if not task:
            return f"Error: 任务 {task_id} 不存在"
        if changes.get("status") == "deleted":
            del self._tasks[task_id]
            return f"已删除任务 {task_id}"
        for key in ("subject", "description", "status", "owner"):
            value = changes.get(key)
            if value is not None:
                setattr(task, key, value)
        for key in ("blocks", "blocked_by"):
            additions = changes.get(f"add_{key}")
            if additions:
                for other in additions:
                    if other not in getattr(task, key):
                        getattr(task, key).append(other)
        meta = changes.get("metadata")
        if isinstance(meta, dict):
            for key, value in meta.items():
                if value is None:
                    task.metadata.pop(key, None)
                else:
                    task.metadata[key] = value
        return self._summary(task)

    @staticmethod
    def _summary(task: _Task) -> str:
        flags = []
        if task.owner:
            flags.append(f"owner={task.owner}")
        if task.blocks:
            flags.append(f"blocks={','.join(task.blocks)}")
        if task.blocked_by:
            flags.append(f"blocked_by={','.join(task.blocked_by)}")
        suffix = f"（{'，'.join(flags)}）" if flags else ""
        return f"[{task.id}] {task.status:11s} {task.subject}{suffix}"

    def _render(self, task: _Task) -> str:
        body = self._summary(task)
        if task.description:
            body += f"\n  描述：{task.description}"
        if task.metadata:
            body += f"\n  元数据：{task.metadata}"
        return body


class _TaskCreateArgs(BaseModel):
    subject: str = Field(description="A brief title for the task")
    description: str = Field(description="What needs to be done")
    metadata: dict | None = None


class _TaskGetArgs(BaseModel):
    task_id: str


class _TaskListArgs(BaseModel):
    pass


class _TaskUpdateArgs(BaseModel):
    task_id: str
    subject: str | None = None
    description: str | None = None
    status: str | None = Field(default=None, pattern="^(pending|in_progress|completed|deleted)$")
    owner: str | None = None
    add_blocks: list[str] | None = None
    add_blocked_by: list[str] | None = None
    metadata: dict | None = None


_TASK_CREATE_DESCRIPTION = """Use this tool to create a structured task list for your current session. This helps you track progress, organize complex tasks, and demonstrate thoroughness to the user.
It also helps the user understand the progress of their current requests.

## When to Use This Tool

Use this tool proactively in these scenarios:

- Complex multi-step tasks - When a task requires 3 or more distinct steps or actions
- Non-trivial or complex tasks that requires planning, tracking, or coordination
- The user explicitly requests todo list - When the user provides multiple items to be done (numbered or comma-separated)
- After receiving new instructions: Immediately capture user requirements as tasks
- While working on tasks: Mark an item as in_progress BEFORE starting that work. Quickly mark it completed AFTER finishing. If you defer minor items to a later time, you will get a chance to report them in your final response

## When NOT to Use This Tool

Skip using this tool when:
- The task is trivial or tracking it provides no organizational benefit
- The task is purely conversational or informational

## Task Fields

- **subject**: A brief, actionable title in imperative form (e.g., "Fix authentication bug in login flow")
- **description**: What needs to be done

All tasks are created with status `pending`.

## Tips

- Create tasks with clear, specific subjects that describe the outcome
- Create tasks with responsibility or dependencies (blocks/blocked_by) if needed
- Check TaskList first to avoid creating duplicate tasks"""

_TASK_GET_DESCRIPTION = """Use this tool to retrieve a task by its ID from the task list.

## When to Use This Tool

- When you need to full description and context before starting work
- To understand task dependencies (what it blocks, what blocks it)

## Output

Returns full task details:
- **subject**: Task title
- **description**: Detailed requirements and context
- **status**: 'pending', 'in_progress', 'completed'
- **blocks**: Tasks waiting on this one to complete
- **blocked_by**: Tasks that must be completed before this one can be

## Tips

- After fetching a task, verify its blocked_by list is empty before beginning work
- Use TaskList to see all tasks in summary form."""

_TASK_LIST_DESCRIPTION = """Use this tool to list all tasks in the task list.

## When to Use This Tool

- To see what tasks are available to work on (status 'pending', no owner, not blocked)
- To see overall progress on the project and find which tasks are blocked

## Output

Returns a summary of each task:
- **id**: Task identifier (use with TaskGet, TaskUpdate)
- **subject**: Brief description
- **status**: 'pending' | 'in_progress' | 'completed'
- **owner**: Agent ID if assigned, empty if available
- **blocked_by**: List of open task IDs blocking this task (tasks with blocked_by cannot be started until resolved)

Use TaskGet with a specific task ID to view full details."""

_TASK_UPDATE_DESCRIPTION = """Use this tool to update the status of a task in the task list.

## When to Use This Tool

**Mark tasks as resolved:**
- When you have completed the work described in the task
- IMPORTANT: Always mark your tasks as completed when you finish them
- After resolving, use TaskList to find your next available task

**Update task details:**
- When requirements change or become clearer, or when establishing dependencies between tasks

## Fields You Can Update

- **status**: The task status ('pending' → 'in_progress' → 'completed')
- **subject**: Change the task title (imperative form, e.g., "Run tests")
- **description**: Change the task description
- **owner**: Change the task owner
- **metadata**: Merge metadata keys into the task (set a key to null to delete it)
- **add_blocks**: Mark tasks that cannot start until this one completes
- **add_blocked_by**: Mark tasks that must be completed before this one can be

## Status Workflow

Status progresses: `pending` → `in_progress` → `completed`

Use `deleted` to permanently remove a task.

## Examples

Mark as in progress:
```json
{"task_id": "1", "status": "in_progress"}
```

Mark as completed:
```json
{"task_id": "1", "status": "completed"}
```"""


# ------------------------------------------------------------------- 组装 --

def _make_tool(
    name: str,
    description: str,
    args_schema: type[BaseModel],
    fn: Callable[..., Any],
    ctx: PermissionContext | None = None,
    ask: AskFlow | None = None,
    stop: threading.Event | None = None,
) -> GatedTool:
    return GatedTool(
        name=name,
        description=description,
        args_schema=args_schema,
        fn=fn,
        permission_ctx=ctx,
        ask=ask,
        stop=stop,
    )


def build_builtin_tools(
    workspace_dir: Path,
    mode: PermissionMode = PermissionMode.DEFAULT,
    ask: AskFlow | None = None,
    stop: threading.Event | None = None,
) -> tuple[list[GatedTool], PermissionContext]:
    """组装内置工具实例与权限上下文。workspace_dir 由调用方创建。

    mode 由请求指定（默认 DEFAULT，前端可切换）：DEFAULT/ACCEPT_EDITS 下
    未授权操作会产出 ASK → 工具内在 AskFlow 挂起等待前端确认。
    stop 置位后（SSE 中止）所有工具直接跳过执行。
    """
    tasks = TaskStore()

    async def _task_create(subject: str, description: str, metadata: dict | None = None) -> str:
        return tasks.create(subject, description, metadata)

    async def _task_get(task_id: str) -> str:
        return tasks.get(task_id)

    async def _task_list() -> str:
        return tasks.listing()

    async def _task_update(
        task_id: str,
        subject: str | None = None,
        description: str | None = None,
        status: str | None = None,
        owner: str | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
        metadata: dict | None = None,
    ) -> str:
        return tasks.update(
            task_id,
            subject=subject, description=description, status=status, owner=owner,
            add_blocks=add_blocks, add_blocked_by=add_blocked_by, metadata=metadata,
        )

    context = PermissionContext(mode=mode, working_directories=[workspace_dir])
    context.deny_rules = {
        tool_name: [SessionRule(tool_name, pattern, "appSettings") for pattern in _deny_patterns()]
        for tool_name in _FILE_TOOLS
    }
    # 本机开发逃生门：DEFAULT 兜底会 ASK 所有非白名单 Bash 命令，
    # AGENT_TOOLS_BASH_ALLOW_PREFIXES 配置的前缀经 allow 规则放行（deny 仍最高优先）。
    # 命令在后端所在机器上执行，勿在多用户部署中开启。
    prefixes = [p.strip() for p in settings.agent_tools_bash_allow_prefixes.split(",") if p.strip()]
    if prefixes:
        context.allow_rules["Bash"] = [SessionRule("Bash", prefix, "appSettings") for prefix in prefixes]

    tools: list[GatedTool] = [
        _make_tool("Bash", _BASH_DESCRIPTION, _BashArgs, _make_bash_fn(workspace_dir), context, ask, stop),
        _make_tool("Read", _READ_DESCRIPTION, _ReadArgs, _read, context, ask, stop),
        _make_tool("Write", _WRITE_DESCRIPTION, _WriteArgs, _write, context, ask, stop),
        _make_tool("Edit", _EDIT_DESCRIPTION, _EditArgs, _edit, context, ask, stop),
        _make_tool("Glob", _GLOB_DESCRIPTION, _GlobArgs, _glob, context, ask, stop),
        _make_tool("Grep", _GREP_DESCRIPTION, _GrepArgs, _grep, context, ask, stop),
        _make_tool("TaskCreate", _TASK_CREATE_DESCRIPTION, _TaskCreateArgs, _task_create, context, ask, stop),
        _make_tool("TaskGet", _TASK_GET_DESCRIPTION, _TaskGetArgs, _task_get, context, ask, stop),
        _make_tool("TaskList", _TASK_LIST_DESCRIPTION, _TaskListArgs, _task_list, context, ask, stop),
        _make_tool("TaskUpdate", _TASK_UPDATE_DESCRIPTION, _TaskUpdateArgs, _task_update, context, ask, stop),
    ]
    return tools, context
