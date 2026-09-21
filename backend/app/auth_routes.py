from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import create_token, current_user, hash_password, verify_password
from .captcha import create_captcha, verify_captcha
from .db import get_db
from .models import User
from .schemas import AuthResponse, CaptchaResponse, LoginRequest, RegisterRequest, UserResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/captcha", response_model=CaptchaResponse)
async def captcha():
    image, token = create_captcha()
    return CaptchaResponse(image=image, token=token)


@router.post("/register", response_model=AuthResponse)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)):
    if not verify_captcha(payload.captcha, payload.captcha_token):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="验证码错误或已过期")
    exists = await db.scalar(select(User).where(User.username == payload.username))
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已存在")
    user = User(username=payload.username, password_hash=hash_password(payload.password))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return AuthResponse(access_token=create_token(user.id), user=UserResponse.model_validate(user))


@router.post("/login", response_model=AuthResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    if not verify_captcha(payload.captcha, payload.captcha_token):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="验证码错误或已过期")
    user = await db.scalar(select(User).where(User.username == payload.username))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    return AuthResponse(access_token=create_token(user.id), user=UserResponse.model_validate(user))


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(current_user)):
    return UserResponse.model_validate(user)
