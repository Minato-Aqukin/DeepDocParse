"""service token 鉴权。

注意：这里只验内网 service token（backend <-> service）。
用户 API key 的校验、额度、限流全部在 DeepDocParse-Web/backend，service 不感知用户。
"""
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

_bearer = HTTPBearer(auto_error=False)


async def require_service_token(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    if cred is None or not secrets.compare_digest(cred.credentials, settings.service_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid service token")
