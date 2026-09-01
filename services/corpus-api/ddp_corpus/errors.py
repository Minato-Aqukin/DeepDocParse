"""统一错误体 —— OpenAI 风格 {"error": {"message", "type", "code"}}。

形态对齐 DeepDocParse gateway（对外 /v1/* 的调用方看到的错误必须一致），
但**独立实现**：本层只依赖 openapi.yaml 契约，不 import service 的任何模块。
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class APIError(HTTPException):
    def __init__(self, status_code: int, message: str, type_: str, code: str | None = None,
                 headers: dict | None = None):
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.type = type_
        self.code = code or type_


def error_body(message: str, type_: str, code: str) -> dict:
    return {"error": {"message": message, "type": type_, "code": code}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code,
                            content=error_body(str(exc.detail), exc.type, exc.code),
                            headers=exc.headers)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code,
                            content=error_body(str(exc.detail), "api_error", str(exc.status_code)),
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422,
                            content=error_body(str(exc.errors()), "invalid_request_error",
                                               "validation_error"))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500,
                            content=error_body(f"internal error: {type(exc).__name__}",
                                               "api_error", "internal_error"))
