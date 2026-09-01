"""统一错误格式 —— OpenAI 风格 {"error": {"message", "type", "code"}}。

所有 /v1/* 的非 2xx 响应都走这个形态（技术约定，见 CLAUDE.md）。
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class APIError(HTTPException):
    """业务错误：携带 OpenAI 风格的 type/code。"""

    def __init__(self, status_code: int, message: str, type_: str, code: str | None = None):
        super().__init__(status_code=status_code, detail=message)
        self.type = type_
        self.code = code or type_


def error_body(message: str, type_: str, code: str) -> dict:
    return {"error": {"message": message, "type": type_, "code": code}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(str(exc.detail), exc.type, exc.code),
        )

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(str(exc.detail), "api_error", str(exc.status_code)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body(str(exc.errors()), "invalid_request_error", "validation_error"),
        )

    @app.exception_handler(Exception)
    async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # 兜底：未预期异常也保持 OpenAI 风格错误体（Starlette 默认是纯文本 500）
        return JSONResponse(
            status_code=500,
            content=error_body(f"internal error: {type(exc).__name__}", "api_error", "internal_error"),
        )
