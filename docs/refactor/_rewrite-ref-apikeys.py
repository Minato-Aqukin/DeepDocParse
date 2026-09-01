"""API key 管理（Web 用户视角，JWT 鉴权）。

设计要点：
- 明文只在创建时返回一次，库里只有 sha256（丢了只能重建，不能找回）
- 每 key 独立额度（按页）与限速（按次/分钟），默认值来自 settings
"""
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.deps import current_user
from app.errors import APIError
from app.models import ApiKey, User
from app.security import generate_api_key

router = APIRouter()


class CreateKeyRequest(BaseModel):
    name: str = Field(default="default", max_length=64)
    quota_pages: int | None = None          # 不传用 settings 默认；显式 null 需用 unlimited
    unlimited: bool = False
    rate_limit_per_min: int | None = None
    expires_in_days: int | None = None


class KeyInfo(BaseModel):
    id: str
    name: str
    key_prefix: str
    quota_pages: int | None
    used_pages: int
    rate_limit_per_min: int
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime


class CreatedKey(KeyInfo):
    key: str    # 明文，仅此一次


def _to_info(key: ApiKey) -> dict:
    return {c: getattr(key, c) for c in KeyInfo.model_fields}


@router.post("", response_model=CreatedKey, status_code=201)
async def create_key(req: CreateKeyRequest, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    plain, prefix, key_hash = generate_api_key()
    key = ApiKey(
        user_id=user.id,
        name=req.name,
        key_prefix=prefix,
        key_hash=key_hash,
        quota_pages=None if req.unlimited else (req.quota_pages or settings.default_quota_pages),
        rate_limit_per_min=req.rate_limit_per_min or settings.default_rate_limit_per_min,
        expires_at=(datetime.now(UTC) + timedelta(days=req.expires_in_days))
        if req.expires_in_days else None,
    )
    session.add(key)
    await session.commit()
    return CreatedKey(key=plain, **_to_info(key))


@router.get("", response_model=list[KeyInfo])
async def list_keys(user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)):
    keys = (await session.execute(
        select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
    )).scalars().all()
    return [KeyInfo(**_to_info(k)) for k in keys]


@router.delete("/{key_id}", status_code=204)
async def revoke_key(key_id: str, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    key = await session.get(ApiKey, key_id)
    # 不属于本人的 key 也报 404：不泄露 key 是否存在
    if key is None or key.user_id != user.id:
        raise APIError(404, "API key not found", "invalid_request_error", "key_not_found")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        await session.commit()
