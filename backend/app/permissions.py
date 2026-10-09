"""自研权限引擎：工具调用的 ALLOW/DENY/ASK 裁决（LangGraph 迁移版）。

替代原 AgentScope 的 permission 子包，判定顺序与其引擎保持一致：

1. 显式放行工具（medical_rag_search / web_search / Task 四件套）→ ALLOW
   （对应原 FunctionTool 的恒 ALLOW permission 与「Task 恒放行」）；
2. deny 规则 → DENY（所有模式最高优先）；
3. 只读快路径 → ALLOW（Read/Glob/Grep 与 Bash 只读白名单命令，所有模式生效）；
4. 安全 ASK（危险命令/敏感路径/注入结构，不可被 allow 规则覆盖）；
5. 工作目录内写入 → ALLOW（仅 ACCEPT_EDITS / DONT_ASK，Write/Edit 与
   mkdir/rm/mv/cp 等文件系统命令且全部目标路径在工作区内）；
6. allow 规则 → ALLOW（含会话级「总是允许」重放与 BASH_ALLOW_PREFIXES 逃生门）；
7. 模式兜底：default/accept_edits → ASK；explore → DENY（只读模式，
   不经过安全 ASK 与 allow 规则）；dont_ask → DENY（无人值守，绝不产出 ASK）。

规则匹配语义（与原实现一致）：
- 文件类工具（Read/Write/Edit/Glob/Grep）：rule_content 为 glob，fnmatch 路径参数；
- Bash：``前缀:*`` 形式按前缀匹配（``git commit:*`` 命中 ``git commit -m …``），
  含 ``*`` 的按通配符全匹配，纯文本按子串匹配；
- 其余工具：子串匹配。

与原实现的已知差异（简化取舍，已确认）：
- 用 shlex + 引号状态机替代 tree-sitter 解析 Bash：复合命令拆分、
  注入结构（``$(...)``、`` `...` ``、``<(...)``、循环/分支）保守判为需确认；
- Bash 工作区判定对**每个**子命令的命令名都要求在文件系统命令白名单内
  （原实现只看第一个子命令，此处更严格即更安全）；
- 原实现「把服务器进程 cwd 也当工作目录」的已知残留不复刻：工作目录
  只认会话工作区（默认 backend/workspaces/<会话id>/ 或用户自选目录）。
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class PermissionMode(str, Enum):
    """权限模式（BYPASS 永不开放，故不定义）。"""

    DEFAULT = "default"
    ACCEPT_EDITS = "accept_edits"
    EXPLORE = "explore"
    DONT_ASK = "dont_ask"


class Behavior(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass
class SessionRule:
    """一条权限规则：tool_name + 匹配模式（glob / 前缀 / 子串，见模块说明）。"""

    tool_name: str
    rule_content: str
    source: str = "suggested"


@dataclass
class Decision:
    behavior: Behavior
    message: str
    # ASK 时附带的「总是允许」建议规则（用户勾选 always 后跨轮重放）
    suggested_rules: list[SessionRule] = field(default_factory=list)


@dataclass
class PermissionContext:
    """一次请求的权限上下文：模式 + 工作区 + 规则集（deny 优先于 allow）。"""

    mode: PermissionMode = PermissionMode.DONT_ASK
    working_directories: list[Path] = field(default_factory=list)
    deny_rules: dict[str, list[SessionRule]] = field(default_factory=dict)
    allow_rules: dict[str, list[SessionRule]] = field(default_factory=dict)


# 显式放行（无副作用或纯只读检索），对应原 FunctionTool 的恒 ALLOW
AUTO_ALLOW_TOOLS = {"medical_rag_search", "web_search", "TaskCreate", "TaskGet", "TaskList", "TaskUpdate"}

# 只读工具：check_read_only 恒真 → 所有模式走快路径放行
READ_ONLY_TOOLS = {"Read", "Glob", "Grep"}

# 文件类工具 → 取路径的参数名（deny/allow 规则与敏感路径判定用）
_FILE_PATH_ARGS = {"Read": "file_path", "Write": "file_path", "Edit": "file_path", "Glob": "path", "Grep": "path"}

# 只读 Bash 命令清单（沿用原实现，含 git/docker/gh 只读子命令）
_READ_ONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "less", "more", "file", "stat", "wc",
    "grep", "rg", "ag", "ack", "find", "tree", "pwd", "which", "whereis", "type",
    "git status", "git log", "git diff", "git show", "git branch", "git tag",
    "git remote", "git ls-files", "git ls-tree", "git cat-file", "git rev-parse",
    "git rev-list", "git describe", "git shortlog", "git blame", "git grep",
    "git reflog", "git config --get", "git config --list",
    "docker ps", "docker images", "docker inspect", "docker logs",
    "docker version", "docker info",
    "gh repo view", "gh issue list", "gh pr list", "gh status",
    "python --version", "python -V", "node --version", "node -v",
    "npm list", "npm ls", "pip list", "pip show",
}

# 危险命令模式（子串命中即安全 ASK）
_DANGEROUS_COMMANDS = [
    "rm -rf", "sudo rm", "dd ", "dd of=", "mkfs", "fdisk", "format",
    "chmod 777", "chmod -R 777", "chown -R", "kill -9", "> /dev/",
]

# 敏感文件（basename 命中）与敏感目录（路径任一段命中）→ 安全 ASK
_DANGEROUS_FILES = {
    ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc", ".zprofile",
    ".profile", ".ssh/config", ".ssh/authorized_keys", ".netrc", ".npmrc",
    ".pypirc", ".env", ".envrc", ".env.local", ".env.development",
    ".env.development.local", ".env.test", ".env.test.local", ".env.staging",
    ".env.production", ".env.production.local",
}
_DANGEROUS_DIRS = {".git", ".vscode", ".idea", ".ssh"}

# rm/rmdir 绝不允许静默删除的系统根（相对/绝对/~ 形式）
_SYSTEM_ROOTS = {"/", "/usr", "/etc", "/bin", "/sbin", "/var", "/boot", "/dev", "/lib", "/opt", "/home", "/Users", "/System", "/Library", "~"}

# 工作区内自动放行的文件系统命令（全部目标路径须落在工作区）
_FILESYSTEM_COMMANDS = {"mkdir", "touch", "rm", "rmdir", "mv", "cp", "sed"}

# 无法静态分析的注入结构（出现即按安全 ASK 处理）
_INJECTION_PATTERNS = re.compile(r"\$\(|`|<\(|\$\{|(^|\s|;|&|\|)(for|while|until|if|case|function)\s|\(&|\(\s*\)")


# ---------------------------------------------------------------- Bash 解析 --

def split_subcommands(command: str) -> list[str]:
    """按 && / || / ; / | 拆分复合命令（引号内不拆），供逐段判定。"""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch in "|&;" and i + 1 < len(command) and command[i + 1] == ch and ch != ";":
            parts.append("".join(buf))
            buf = []
            i += 1
        elif ch in "|&;":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _command_head(subcommand: str, words: int) -> str:
    """取子命令头 N 个词（剥引号），用于白名单/前缀匹配。"""
    try:
        tokens = [t for t in shlex.split(subcommand) if t]
    except ValueError:
        tokens = subcommand.split()
    return " ".join(tokens[:words])


# 安全基础命令（跳过环境变量前缀后的首词命中即视为只读，与原 SAFE_COMMANDS 一致）
_SAFE_COMMANDS = {"echo", "cat", "ls", "pwd", "cd", "true", "false", "printf", "grep", "tee"}

# find 的可变更谓词：命中则该 find 不再算只读
_FIND_MUTATING_PREDICATES = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls", "-fprint", "-fprint0")


def _is_single_read_only(sub: str) -> bool:
    """单条子命令的只读判定：白名单前缀匹配 + 安全基础命令（跳过环境变量前缀）。"""
    if sub.split() and sub.split()[0] == "find":
        if any(p in sub for p in _FIND_MUTATING_PREDICATES):
            return False
    for entry in _READ_ONLY_COMMANDS:
        if sub == entry or sub.startswith(entry + " "):
            return True
    try:
        tokens = shlex.split(sub)
    except ValueError:
        tokens = sub.split()
    idx = 0
    while idx < len(tokens) and "=" in tokens[idx]:
        idx += 1
    return idx < len(tokens) and tokens[idx] in _SAFE_COMMANDS


def _is_read_only_bash(command: str) -> bool:
    """只读判定：含重定向/注入结构一律非只读；复合命令须全部子命令只读。"""
    if ">" in command or _INJECTION_PATTERNS.search(command):
        return False
    return all(_is_single_read_only(sub) for sub in split_subcommands(command))


def _path_tokens(command: str) -> list[str]:
    """收集命令里「像路径」的裸参数（含 /、~ 开头或磁盘上存在的相对路径）。"""
    tokens: list[str] = []
    for sub in split_subcommands(command):
        try:
            parts = shlex.split(sub)
        except ValueError:
            parts = sub.split()
        for tok in parts[1:]:
            if tok.startswith("-"):
                continue
            if "/" in tok or tok.startswith("~") or os.path.exists(tok):
                tokens.append(tok)
    return tokens


# ------------------------------------------------------------- 安全 ASK 判定 --

def _is_dangerous_path(path: str) -> bool:
    """路径命中敏感文件名或敏感目录段（~/… 展开后按段判断）。"""
    expanded = os.path.expanduser(path)
    parts = Path(expanded).parts
    if not parts:
        return False
    tail = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    if tail in _DANGEROUS_FILES or parts[-1] in _DANGEROUS_FILES:
        return True
    return any(seg in _DANGEROUS_DIRS for seg in parts)


def _safety_ask_reason(tool_name: str, tool_input: dict) -> str | None:
    """危险操作的确认理由（不可被 allow 规则覆盖；返回 None 表示无危险）。"""
    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        if _INJECTION_PATTERNS.search(command):
            return f"命令包含无法静态分析的动态结构（命令替换/循环/子 shell 等）：{command[:80]}"
        for pattern in _DANGEROUS_COMMANDS:
            if pattern in command:
                return f"命令包含危险模式：{pattern}"
        for tok in _path_tokens(command):
            if _is_dangerous_path(tok):
                return f"命令操作敏感路径：{tok}"
        # rm/rmdir 指向系统根目录：绝不静默放行
        for sub in split_subcommands(command):
            head = _command_head(sub, 1)
            if head in ("rm", "rmdir"):
                for tok in _path_tokens(sub):
                    expanded = os.path.expanduser(tok)
                    if expanded in _SYSTEM_ROOTS or os.path.isabs(expanded) and expanded in _SYSTEM_ROOTS:
                        return f"危险删除操作：目标 {tok} 是系统关键目录"
        return None
    if tool_name in ("Write", "Edit"):
        file_path = str(tool_input.get("file_path", ""))
        if _is_dangerous_path(file_path):
            return f"写入操作涉及敏感文件：{file_path}"
        return None
    return None


# ------------------------------------------------------------ 工作区写入判定 --

def _within(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _workspace_write_ok(tool_name: str, tool_input: dict, context: PermissionContext) -> bool:
    """Write/Edit 或文件系统 Bash 命令，全部目标路径落在工作区内。"""
    if not context.working_directories:
        return False
    if tool_name in ("Write", "Edit"):
        file_path = str(tool_input.get("file_path", ""))
        if not file_path:
            return False
        expanded = Path(os.path.expanduser(file_path))
        return any(_within(expanded, d) for d in context.working_directories)
    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        subs = split_subcommands(command)
        if not subs:
            return False
        # 每个子命令的命令名都必须是文件系统命令（比原实现只看首个更严格）
        if any(_command_head(sub, 1) not in _FILESYSTEM_COMMANDS for sub in subs):
            return False
        targets = _path_tokens(command)
        # 一个可判定目标都没有时不放行（保守，与原实现一致）
        if not targets:
            return False
        for tok in targets:
            expanded = Path(os.path.expanduser(tok))
            if not any(_within(expanded, d) for d in context.working_directories):
                return False
        return True
    return False


# ----------------------------------------------------------------- 规则匹配 --

def _rule_matches(tool_name: str, rule: SessionRule, tool_input: dict) -> bool:
    """按工具类型匹配规则：文件类 glob / Bash 前缀与通配 / 其余子串。"""
    content = rule.rule_content
    if not content:
        return True
    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        if content.endswith(":*"):
            prefix = content[:-2].strip()
            return command.startswith(prefix + " ") or command == prefix
        if "*" in content:
            return fnmatch.fnmatch(command, content)
        return content in command
    path_arg = _FILE_PATH_ARGS.get(tool_name)
    if path_arg:
        return fnmatch.fnmatch(str(tool_input.get(path_arg, "")), content)
    return content in str(tool_input)


def _hit_rules(rules: dict[str, list[SessionRule]], tool_name: str, tool_input: dict) -> SessionRule | None:
    for rule in rules.get(tool_name, []):
        if _rule_matches(tool_name, rule, tool_input):
            return rule
    return None


def generate_suggestions(tool_name: str, tool_input: dict) -> list[SessionRule]:
    """「总是允许」建议规则：Bash 取命令前缀（git commit:*），文件类取目录 glob。"""
    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        prefixes: list[str] = []
        for sub in split_subcommands(command)[:5]:
            prefix = _command_head(sub, 2)
            if prefix and prefix not in prefixes:
                prefixes.append(prefix)
        return [SessionRule("Bash", f"{p}:*", "suggested") for p in prefixes]
    path_arg = _FILE_PATH_ARGS.get(tool_name)
    if path_arg:
        path = str(tool_input.get(path_arg, ""))
        if path:
            parent = os.path.dirname(os.path.expanduser(path)).rstrip("/\\")
            return [SessionRule(tool_name, f"{parent}/**" if parent else "**", "suggested")]
    return []


# ------------------------------------------------------------------ 主裁决 --

def decide(tool_name: str, tool_input: dict, context: PermissionContext) -> Decision:
    """裁决一次工具调用：ALLOW 直接执行，ASK 交人工确认，DENY 拒绝并回填原因。"""
    # 1. 显式放行工具
    if tool_name in AUTO_ALLOW_TOOLS:
        return Decision(Behavior.ALLOW, f"{tool_name} 只读/无副作用，自动允许")
    # 2. deny 规则（所有模式最高优先）
    hit = _hit_rules(context.deny_rules, tool_name, tool_input)
    if hit:
        return Decision(Behavior.DENY, f"权限规则拒绝：{hit.rule_content}")
    # 3. 只读快路径（所有模式）
    if tool_name in READ_ONLY_TOOLS or (tool_name == "Bash" and _is_read_only_bash(str(tool_input.get("command", "")))):
        return Decision(Behavior.ALLOW, "只读操作自动放行")
    # 4. explore：只读模式，非只读一律拒绝（不看安全 ASK / allow 规则，与原引擎一致）
    if context.mode is PermissionMode.EXPLORE:
        return Decision(Behavior.DENY, "explore 模式为只读模式，拒绝修改类操作")
    # 5. 安全 ASK（dont_ask 转 DENY；default/accept_edits 产出 ASK，不可被 allow 规则覆盖）
    safety = _safety_ask_reason(tool_name, tool_input)
    if safety:
        if context.mode is PermissionMode.DONT_ASK:
            return Decision(Behavior.DENY, f"危险操作且无人工确认通道，已拒绝：{safety}")
        return Decision(Behavior.ASK, f"需要确认：{safety}", suggested_rules=generate_suggestions(tool_name, tool_input))
    # 6. 工作目录内写入（accept_edits / dont_ask）
    if context.mode in (PermissionMode.ACCEPT_EDITS, PermissionMode.DONT_ASK) and _workspace_write_ok(tool_name, tool_input, context):
        return Decision(Behavior.ALLOW, "目标路径在会话工作目录内，自动放行")
    # 7. allow 规则（会话级 always 重放 + 配置逃生门）
    hit = _hit_rules(context.allow_rules, tool_name, tool_input)
    if hit:
        return Decision(Behavior.ALLOW, f"allow 规则命中：{hit.rule_content}")
    # 8. 模式兜底
    if context.mode is PermissionMode.DONT_ASK:
        return Decision(Behavior.DENY, "dont_ask 模式无人工确认通道，按拒绝处理")
    return Decision(
        Behavior.ASK,
        f"需要用户确认是否执行 {tool_name}",
        suggested_rules=generate_suggestions(tool_name, tool_input),
    )


def apply_session_rules(context: PermissionContext, rules: list[SessionRule]) -> None:
    """把会话级「总是允许」规则重放进权限上下文（每轮重建后调用，跨轮生效）。"""
    for rule in rules:
        bucket = context.allow_rules.setdefault(rule.tool_name, [])
        if not any(r.rule_content == rule.rule_content for r in bucket):
            bucket.append(rule)
