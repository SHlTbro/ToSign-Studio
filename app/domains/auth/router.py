"""Auth domain routes (patch-C CC-9)."""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.envelope import ok
from app.core.errors import ApiError
from app.core.identity import get_current_user_id, get_current_user_id_authflow
from app.domains.auth import service

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_REQUIRED_FIELDS = ("username", "password", "display_name", "invite_code")


def _invalid(session: Session, payload, detail: str) -> ApiError:
    username = ""
    invite_code = ""
    if isinstance(payload, dict):
        if isinstance(payload.get("username"), str):
            username = payload["username"]
        if isinstance(payload.get("invite_code"), str):
            invite_code = payload["invite_code"]
    service.record_register_failure(
        session, username=username, invite_code=invite_code, code="INVALID_REQUEST"
    )
    return ApiError(400, "INVALID_REQUEST", "Invalid request", detail)


@router.post("/register", status_code=201)
async def register(request: Request, session: Session = Depends(get_session)):
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        raise _invalid(session, payload, "request body must be a JSON object")

    for field in _REQUIRED_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or value == "":
            raise _invalid(session, payload, f"missing or empty field: {field}")

    if len(payload["password"]) < service.MIN_PASSWORD_LENGTH:
        raise _invalid(session, payload, "password must be at least 8 characters")

    user = service.register_user(
        session,
        username=payload["username"],
        password=payload["password"],
        display_name=payload["display_name"],
        invite_code=payload["invite_code"],
    )
    # No auto-login: no Set-Cookie, no token/session fields in the body.
    return ok({"user_id": user.id, "username": user.username, "display_name": user.display_name})

@router.post("/login")
async def login(request: Request, response: Response, session: Session = Depends(get_session)):
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "request body must be a JSON object")
    username = payload.get("username")
    password = payload.get("password")
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "username and password are required")
    ip = request.client.host if request.client else ""
    user_agent = request.headers.get("user-agent", "")
    user, token = service.login_user(
        session, username=username, password=password, ip=ip, user_agent=user_agent
    )
    response.set_cookie(
        "session",
        token,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
        max_age=service.SESSION_TTL_SECONDS,
    )
    return ok(
        {
            "user_id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "must_change_password": user.must_change_password,
        }
    )


@router.post("/logout", status_code=204)
def logout(request: Request, session: Session = Depends(get_session)):
    token = request.cookies.get("session")
    if token:
        service.logout_user(session, token)
    resp = Response(status_code=204)
    resp.set_cookie(
        "session",
        "",
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
        max_age=0,
    )
    return resp


@router.get("/me")
def me(request: Request, session: Session = Depends(get_session)):
    from app.domains.auth.models import User

    token = request.cookies.get("session")
    if token:
        row, user = service.resolve_session(session, token)
        if user.must_change_password:
            raise ApiError(403, "PASSWORD_CHANGE_REQUIRED", "Password change required")
        return ok(
            {
                "user_id": user.id,
                "username": user.username,
                "display_name": user.display_name,
                "role": user.role,
                "must_change_password": user.must_change_password,
            }
        )
    # P01-BE (E18): the shim fallback must go through the same guard as every
    # other endpoint. The old direct _shim_user_id call ignored YY_AUTH_STRICT,
    # so a spoofed X-User-Id leaked any user's profile even in strict mode.
    user_id = get_current_user_id(request, session)
    user = session.get(User, user_id)
    if user is not None:
        return ok(
            {
                "user_id": user.id,
                "username": user.username,
                "display_name": user.display_name,
                "role": user.role,
                "must_change_password": user.must_change_password,
            }
        )
    return ok(
        {
            "user_id": user_id,
            "username": f"shim-{user_id}",
            "display_name": f"shim-{user_id}",
            "role": "user",
        }
    )


@router.post("/change-password")
async def change_password(
    request: Request,
    session: Session = Depends(get_session),
    user_id: int = Depends(get_current_user_id_authflow),
):
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "request body must be a JSON object")
    from app.domains.auth.models import User

    user = session.get(User, user_id)
    if user is None:
        raise ApiError(401, "UNAUTHORIZED", "Unauthorized", "unknown user")
    service.change_password(
        session,
        user,
        old_password=payload.get("old_password"),
        new_password=payload.get("new_password"),
    )
    return ok({"user_id": user.id, "must_change_password": user.must_change_password})
