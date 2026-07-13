"""DeepDocParse-Web backend 入口。

鉴权边界在本层（ARCHITECTURE.md 决策 #2/#3）：
- Web 用户：JWT session
- 第三方开发者：API key（sk-xxx）
- 对 DeepDocParse：统一以 SERVICE_TOKEN 转发，service 不感知用户
"""
from fastapi import FastAPI

from app.routers import auth, apikeys, tasks, proxy

app = FastAPI(title="DeepDocParse-Web backend", version="0.1.0")

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(apikeys.router, prefix="/api/keys", tags=["apikeys"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
app.include_router(proxy.router, tags=["proxy"])  # /v1/* 对外 API + /mcp 反代
