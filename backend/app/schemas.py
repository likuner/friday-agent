from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CaptchaResponse(BaseModel):
    image: str
    token: str


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_\u4e00-\u9fff]+$")
    password: str = Field(min_length=6, max_length=72)
    captcha: str = Field(min_length=4, max_length=8)
    captcha_token: str


class LoginRequest(RegisterRequest):
    pass


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: str
    content: str
    meta: dict
    created_at: str


class ConversationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0
    last_message: str | None = None
    # 会话设置（无行 = 均为 None）：侧边栏按有无 workspace_root 分组用
    workspace_root: str | None = None
    permission_mode: str | None = None


class ConversationDetail(ConversationSummary):
    messages: list[MessageResponse]


class ConversationPage(BaseModel):
    """会话列表的一页（侧边栏两组与历史记录页共用）。

    total 是当前过滤条件下的总条数（用于「还有 N 条」/「共 N 个对话」），
    has_more 便于调用方直接判断，也可用 offset + len(items) < total 自行推算。
    """

    items: list[ConversationSummary]
    total: int
    has_more: bool


class ConversationCreate(BaseModel):
    title: str = Field(default="新的对话", max_length=200)


class ChatRequest(BaseModel):
    # 允许只发图片不带文字，因此 content 可以为空（在路由里校验二者至少有其一）
    content: str = Field(default="", max_length=20000)
    deep_thinking: bool = False
    web_search: bool = False
    # Agent 内置工具（Bash/文件/任务）：需服务端 AGENT_TOOLS_ENABLED 同时开启才生效
    agent_tools: bool = False
    # 内置工具的权限模式（仅 agent_tools 开启时有意义）：default=未授权操作弹确认
    # （需前端确认卡片应答），accept_edits=工作区写入免确认其余弹确认，
    # explore=只读，dont_ask=未授权直接拒绝（无人值守）。BYPASS 不开放。
    permission_mode: Literal["default", "accept_edits", "explore", "dont_ask"] = "default"
    # 已上传图片的文件名列表（POST /api/files 返回的 name）
    attachments: list[str] = Field(default_factory=list, max_length=9)


class PermissionConfirmRequest(BaseModel):
    # 对 SSE permission_ask 事件的应答：approved=false 时模型收到 denied 结果并继续
    approved: bool
    # true = 同时把该工具调用的建议规则存为会话级「总是允许」（本进程内跨轮生效）
    always: bool = False


class WorkspaceSetRequest(BaseModel):
    # 会话工作区设置：选定后不可更改（再调返回 409），path=null 不再支持恢复默认
    path: str


class PermissionModeSetRequest(BaseModel):
    # 会话记住的权限模式；取值域与 ChatRequest.permission_mode 一致（BYPASS 不开放）
    mode: Literal["default", "accept_edits", "explore", "dont_ask"]
