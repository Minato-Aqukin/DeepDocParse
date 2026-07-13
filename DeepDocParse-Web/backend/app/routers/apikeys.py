"""API key 管理。

设计要点：
- key 形如 sk-xxx，仅创建时明文返回一次，库中存哈希
- 每 key：额度（按页/按次）、速率限制、过期时间、用量统计（usage_records）
"""
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.post("")
async def create_key():
    raise HTTPException(status_code=501, detail="TODO")


@router.get("")
async def list_keys():
    raise HTTPException(status_code=501, detail="TODO")


@router.delete("/{key_id}")
async def revoke_key(key_id: str):
    raise HTTPException(status_code=501, detail="TODO")
