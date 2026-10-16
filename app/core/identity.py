"""Identity resolution: session cookie (T10 real auth) with the X-User-Id
dev shim kept as an explicit test backdoor. Contract v0.2.1 section 3 line 66:
all business endpoints require a valid session; the shim is a dev/test
convenience that MUST be disabled in production via YY_AUTH_STRICT=1
(D-T10-2). The must_change_password gate (section 2 line 65) applies to every
endpoint except /auth/change-password and /auth/logout, which use the
authflow dependency (D-T10-3).
"""

import logging
import os

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.errors import ApiError
from app.domains.auth import service as auth_service

logger = logging.getLogger("yy.identity")


def shim_enabled() -> bool:
    """Single source of truth for the X-User-Id dev-shim door (E18/P01-BE).

    The shim is DISABLED when YY_AUTH_STRICT=1. In any other configuration it
    is OPEN - including a formal startup with no env at all - so every caller
    (identity resolution and the /auth/me fallback) must go through this
    predicate; ad-hoc ``os.environ`` reads are what allowed the /auth/me
    strict-mode bypass.
    """
    return os.environ.get("YY_AUTH_STRICT") != "1"


def warn_if_shim_enabled() -> None:
    """Startup signal mirroring the font-signing-secret pattern (D-T65-6):
    an insecure default must be loud, not silent. Logged once per create_app()
    while the shim door is open."""
    if shim_enabled():
        logger.warning(
            "X-User-Id 开发 shim 处于启用状态（YY_AUTH_STRICT!=1）：任何本地进程"
            "可伪造任意用户身份。生产部署必须设置 YY_AUTH_STRICT=1"
        )


def _shim_user_id(request: Request) -> int:
    raw = request.headers.get("X-User-Id")
    try:
        user_id = int(raw) if raw is not None else -1
    except (TypeError, ValueError):
        user_id = -1
    if user_id <= 0:
        raise ApiError(
            401,
            "UNAUTHORIZED",
            "Unauthorized",
            "missing or invalid X-User-Id header",
        )
    return user_id


def _resolve(request: Request, session, *, allow_must_change: bool) -> int:
    token = request.cookies.get("session")
    if token:
        row, user = auth_service.resolve_session(session, token)
        if user.must_change_password and not allow_must_change:
            # v0.2.1 section 2 line 65 (GWT-G2-admin).
            raise ApiError(403, "PASSWORD_CHANGE_REQUIRED", "Password change required")
        return user.id
    if not shim_enabled():
        raise ApiError(401, "UNAUTHORIZED", "Unauthorized", "authentication required")
    return _shim_user_id(request)


def get_current_user_id(request: Request, session: Session = Depends(get_session)) -> int:
    """Default guard: session cookie first; must_change gate applies."""
    return _resolve(request, session, allow_must_change=False)


def get_current_user_id_authflow(request: Request, session: Session = Depends(get_session)) -> int:
    """Auth-flow guard (change-password/logout): must_change gate exempt."""
    return _resolve(request, session, allow_must_change=True)
