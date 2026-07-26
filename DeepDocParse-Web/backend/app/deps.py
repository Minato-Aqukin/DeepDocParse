"""三种身份的依赖，对应三类调用方：

- Web 前端用户 -> JWT      (current_user)
- 第三方开发者   -> sk- key  (require_api_key)
- service 回调   -> SERVICE_TOKEN (require_service_token)

三者互不重叠：service 永远不该拿着用户凭据出现，用户也拿不到 SERVICE_TOKEN。
"""
from datetime import UTC, datetime

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.errors import APIError
from app.models import ApiKey, User, as_aware
from app.security import KEY_PREFIX, constant_time_equals, decode_access_token, hash_api_key


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise APIError(401, "missing bearer credentials", "authentication_error", "missing_token")
    return authorization[7:].strip()


async def current_user(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> User:
    user_id = decode_access_token(_bearer(authorization))
    if user_id is None:
        raise APIError(401, "invalid or expired session", "authentication_error", "invalid_token")
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise APIError(401, "user not found or disabled", "authentication_error", "invalid_token")
    return user


async def require_api_key(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> ApiKey:
    """对外 /v1/* 与 /mcp 的入口鉴权。额度与限速在 metering 里做，这里只认身份。"""
    plain = _bearer(authorization)
    if not plain.startswith(KEY_PREFIX):
        raise APIError(401, "API key must start with 'sk-'", "authentication_error", "invalid_key")
    key = (await session.execute(
        select(ApiKey).where(ApiKey.key_hash == hash_api_key(plain))
    )).scalar_one_or_none()
    if key is None:
        raise APIError(401, "invalid API key", "authentication_error", "invalid_key")
    if key.revoked_at is not None:
        raise APIError(401, "API key has been revoked", "authentication_error", "revoked_key")
    if key.expires_at is not None and as_aware(key.expires_at) < datetime.now(UTC):
        raise APIError(401, "API key has expired", "authentication_error", "expired_key")
    return key


async def require_service_token(authorization: str | None = Header(default=None)) -> None:
    """service -> backend 的回调入口（/internal/*）。"""
    if not constant_time_equals(_bearer(authorization), settings.service_token):
        raise APIError(401, "invalid service token", "authentication_error", "invalid_service_token")


def get_storage(request: Request):
    return request.app.state.storage


def get_service_client(request: Request):
    return request.app.state.service_client
