"""用户注册/登录（JWT session）。开放注册，不做邮箱验证（自部署场景）。"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import current_user
from app.errors import APIError
from app.models import User
from app.security import create_access_token, hash_password, verify_password

router = APIRouter()


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    email: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    username: str


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(req: RegisterRequest, session: AsyncSession = Depends(get_session)):
    user = User(username=req.username, email=req.email,
                password_hash=hash_password(req.password))
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise APIError(409, "username or email already taken", "invalid_request_error",
                       "user_exists")
    return TokenResponse(access_token=create_access_token(user.id),
                         user_id=user.id, username=user.username)


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, session: AsyncSession = Depends(get_session)):
    user = (await session.execute(
        select(User).where(User.username == req.username)
    )).scalar_one_or_none()
    # 用户不存在与密码错误返回同一个错误，避免用户名枚举
    if user is None or not verify_password(req.password, user.password_hash):
        raise APIError(401, "invalid username or password", "authentication_error",
                       "invalid_credentials")
    if not user.is_active:
        raise APIError(403, "user is disabled", "permission_error", "user_disabled")
    return TokenResponse(access_token=create_access_token(user.id),
                         user_id=user.id, username=user.username)


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return {"user_id": user.id, "username": user.username, "email": user.email,
            "created_at": user.created_at}
