"""用量查询（Web 用户视角）。前端用量图表的唯一数据源。"""
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import current_user
from app.models import UsageRecord, User, utcnow

router = APIRouter()


@router.get("")
async def get_usage(days: int = 30, user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)):
    """按天聚合 + 按平面汇总。days 上限 365，避免全表扫。"""
    since = utcnow() - timedelta(days=min(max(days, 1), 365))
    # date() 在 PG 与 SQLite 上都可用（避免 date_trunc 这类方言函数）
    day = func.date(UsageRecord.created_at).label("day")

    daily = (await session.execute(
        select(day, func.sum(UsageRecord.pages), func.sum(UsageRecord.requests))
        .where(UsageRecord.user_id == user.id, UsageRecord.created_at >= since)
        .group_by(day).order_by(day)
    )).all()

    by_kind = (await session.execute(
        select(UsageRecord.kind, func.sum(UsageRecord.pages), func.sum(UsageRecord.requests))
        .where(UsageRecord.user_id == user.id, UsageRecord.created_at >= since)
        .group_by(UsageRecord.kind)
    )).all()

    return {
        "days": days,
        "daily": [{"date": str(d), "pages": p or 0, "requests": r or 0} for d, p, r in daily],
        "by_kind": [{"kind": k, "pages": p or 0, "requests": r or 0} for k, p, r in by_kind],
        "total_pages": sum((p or 0) for _, p, _ in by_kind),
        "total_requests": sum((r or 0) for _, _, r in by_kind),
    }
