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


class ConversationDetail(ConversationSummary):
    messages: list[MessageResponse]


class ConversationCreate(BaseModel):
    title: str = Field(default="新的对话", max_length=200)


class ChatRequest(BaseModel):
    # 允许只发图片不带文字，因此 content 可以为空（在路由里校验二者至少有其一）
    content: str = Field(default="", max_length=20000)
    deep_thinking: bool = False
    web_search: bool = False
    # 已上传图片的文件名列表（POST /api/files 返回的 name）
    attachments: list[str] = Field(default_factory=list, max_length=9)
