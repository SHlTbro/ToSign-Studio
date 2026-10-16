"""RFC 9457 problem-details error type and FastAPI exception handler."""

import uuid

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """Domain error carrying the frozen problem+json fields."""

    def __init__(self, status: int, code: str, title: str, detail: str = "", retryable: bool = False, extra_headers: list[tuple[str, str]] | None = None):
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.retryable = retryable
        # T10: carries e.g. the clear-session-cookie Set-Cookie on auth 401/403.
        self.extra_headers = extra_headers or []
        super().__init__(f"{status} {code}: {title}")


async def api_error_handler(request: Request, exc: ApiError):
    """Render ApiError as RFC 9457 application/problem+json with the frozen 8 fields."""
    response = JSONResponse(
        status_code=exc.status,
        media_type="application/problem+json",
        content={
            "type": "about:blank",
            "title": exc.title,
            "status": exc.status,
            "detail": exc.detail,
            "instance": request.url.path,
            "code": exc.code,
            "retryable": exc.retryable,
            "trace_id": uuid.uuid4().hex,
        },
    )
    for name, value in exc.extra_headers:
        response.headers.append(name, value)
    return response