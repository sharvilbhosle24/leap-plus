"""One error shape for the whole API: {"error": {"code": ..., "message": ...}}.

The frontend shows `message` to the user as-is, so it must be plain English
and never leak stack traces, SQL or keys.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("leapplus")


class ApiError(Exception):
    """Raise this from any route for an expected, user-facing failure."""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        code = "not_found" if exc.status_code == 404 else "http_error"
        message = "Not found." if exc.status_code == 404 else str(exc.detail)
        return error_response(exc.status_code, code, message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", [])[1:]) or "request"
        return error_response(422, "invalid_input", f"Invalid {field}: {first.get('msg', 'bad value')}.")

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        log.exception("Unhandled error", exc_info=exc)
        return error_response(500, "internal_error", "Something went wrong on our side. Please try again.")
