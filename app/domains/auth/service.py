"""Register flow: atomic invite consumption + user insert in one transaction.

Concurrency (C-22 hardening): the conditional UPDATE runs BEFORE the
existence SELECT. A writer's first lock acquisition is then the statement-
level RESERVED lock of the UPDATE itself, so concurrent registrations on the
same invite serialize on sqlite's single-writer semantics (busy timeout from
app.core.db connect args) instead of dying on a SHARED->RESERVED upgrade
deadlock (SQLITE_BUSY surfacing as HTTP 500). Exactly one registration
consumes the invite; losers read rowcount=0 and map to the domain codes
INVITE_INVALID (unknown code) / INVITE_EXHAUSTED (fully used).
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import ApiError
from app.domains.auth.models import Invite, LoginLockout, Session, User

PBKDF2_ITERATIONS = 200_000
MIN_PASSWORD_LENGTH = 8
BCRYPT_COST = 12  # v0.2.1 section 10.1 (GWT-G1-password)
SESSION_TTL_SECONDS = 604800  # section 4.1: Max-Age 7 days
LOCKOUT_WINDOW_SECONDS = 900  # section 9: 15-minute sliding window
LOCKOUT_THRESHOLD = 5
LOCKOUT_SECONDS = 900

_WEAK_CACHE: list[str] | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _weak_passwords() -> list[str]:
    global _WEAK_CACHE
    if _WEAK_CACHE is None:
        path = Path(__file__).resolve().parents[3] / "weak-passwords.txt"
        try:
            _WEAK_CACHE = [
                line.strip()
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip()
            ][:1000]
        except OSError:
            _WEAK_CACHE = []
    return _WEAK_CACHE


def is_weak_password(password: str) -> bool:
    """v0.2.1 section 10.1: reject passwords from the OWASP top-1000 list."""
    return password.lower() in {w.lower() for w in _weak_passwords()}


def hash_password(password: str) -> str:
    """bcrypt cost 12 (v0.2.1 section 10.1, GWT-G1-password)."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_COST)).decode("ascii")


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify against bcrypt (contract format) or legacy T63 PBKDF2 hashes."""
    if stored_hash.startswith("pbkdf2$"):
        try:
            _, rounds, salt_hex, digest_hex = stored_hash.split("$")
            digest = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
            )
            return secrets.compare_digest(digest.hex(), digest_hex)
        except (ValueError, TypeError):
            return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """Legacy T63 PBKDF2 hashes are transparently upgraded to bcrypt on login."""
    return stored_hash.startswith("pbkdf2$")


def record_register_failure(session: Session, *, username: str, invite_code: str, code: str) -> None:
    """Persist a fail audit row in its own transaction (safe after rollback)."""
    audit(
        session,
        action="register",
        actor=username,
        object_ref=invite_code,
        result="fail",
        error_code=code,
    )
    session.commit()


def register_user(
    session: Session,
    *,
    username: str,
    password: str,
    display_name: str,
    invite_code: str,
) -> User:
    consumed = session.execute(
        update(Invite)
        .where(Invite.code == invite_code, Invite.used_count < Invite.uses_total)
        .values(used_count=Invite.used_count + 1)
    ).rowcount
    if consumed != 1:
        # Distinguish 403 (code unknown) from 410 (code exists but exhausted),
        # then roll back before auditing so the failure row commits cleanly.
        exists = session.execute(
            select(Invite.code).where(Invite.code == invite_code)
        ).scalar_one_or_none()
        session.rollback()
        if exists is None:
            record_register_failure(session, username=username, invite_code=invite_code, code="INVITE_INVALID")
            raise ApiError(403, "INVITE_INVALID", "Invite invalid", "invite code not found")
        record_register_failure(session, username=username, invite_code=invite_code, code="INVITE_EXHAUSTED")
        raise ApiError(410, "INVITE_EXHAUSTED", "Invite exhausted", "invite code exhausted")

    # v0.2.1 section 10.2 (GWT-G2-admin): the first registered user is the
    # admin and must change password before doing anything else.
    first_user = session.query(User).count() == 0
    user = User(
        username=username,
        display_name=display_name,
        password_hash=hash_password(password),
        role=("admin" if first_user else "user"),
        must_change_password=first_user,
        created_at=_utc_now(),
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError:
        # UNIQUE(username) violation: roll back so the invite consumption is undone too.
        session.rollback()
        record_register_failure(session, username=username, invite_code=invite_code, code="USERNAME_TAKEN")
        raise ApiError(409, "USERNAME_TAKEN", "Username taken", "username already exists")

    audit(session, action="register", actor=username, object_ref=str(user.id), result="ok")
    session.commit()
    return user

CLEAR_SESSION_COOKIE = (
    "session=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0"
)


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _get_lockout(session: Session, username: str, ip: str) -> LoginLockout | None:
    return (
        session.query(LoginLockout)
        .filter(LoginLockout.username == username, LoginLockout.ip_address == ip)
        .first()
    )


def is_locked_out(session: Session, username: str, ip: str) -> bool:
    row = _get_lockout(session, username, ip)
    if row is None or row.locked_until is None:
        return False
    return datetime.fromisoformat(row.locked_until) > datetime.now(timezone.utc)


def _record_login_failure(session: Session, username: str, ip: str) -> None:
    """v0.2.1 section 9: (username, ip) sliding-window counter, persisted."""
    now = datetime.now(timezone.utc)
    row = _get_lockout(session, username, ip)
    if row is None:
        row = LoginLockout(
            username=username, ip_address=ip, window_start=_utc_now(), fail_count=1
        )
        session.add(row)
        session.flush()
        return
    if row.locked_until and datetime.fromisoformat(row.locked_until) > now:
        return  # already locked; keep the lock
    window_start = datetime.fromisoformat(row.window_start)
    if (now - window_start).total_seconds() > LOCKOUT_WINDOW_SECONDS:
        row.window_start = _utc_now()
        row.fail_count = 1
    else:
        row.fail_count += 1
    if row.fail_count >= LOCKOUT_THRESHOLD:
        row.locked_until = (now + timedelta(seconds=LOCKOUT_SECONDS)).isoformat()
        # GWT-S8-lockout: the lockout event is audited with actor = username.
        audit(
            session,
            action="login_lockout",
            actor=username,
            object_ref=ip,
            result="locked",
            error_code=None,
        )
    session.flush()


def login_user(
    session: Session,
    *,
    username: str,
    password: str,
    ip: str = "",
    user_agent: str = "",
) -> tuple[User, str]:
    """v0.2.1 sections 3/4/9/10: full login flow. Returns (user, raw_token);
    the raw token is only ever echoed into the Set-Cookie header."""
    locked = is_locked_out(session, username, ip)

    user = session.query(User).filter(User.username == username).first()

    if not locked and user is not None and verify_password(password, user.password_hash):
        if not user.enabled:
            # section 3 semantic 3: disabled accounts are refused at login.
            audit(
                session,
                action="login_failed",
                actor="system",
                object_ref=username,
                result="fail",
                error_code="USER_DISABLED",
            )
            session.commit()
            raise ApiError(403, "USER_DISABLED", "Account disabled")
        if needs_rehash(user.password_hash):
            # Legacy T63 PBKDF2 hashes transparently upgrade to bcrypt cost 12.
            user.password_hash = hash_password(password)
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        row = Session(
            session_id=secrets.token_hex(16),
            user_id=user.id,
            token_hash=_sha256(token),
            created_at=_utc_now(),
            expires_at=(now + timedelta(seconds=SESSION_TTL_SECONDS)).isoformat(),
            last_used_at=_utc_now(),
            user_agent=user_agent[:512] if user_agent else None,
            ip_address=ip or None,
        )
        session.add(row)
        user.last_login_at = _utc_now()
        # A successful login clears the (username, ip) failure window.
        lk = _get_lockout(session, username, ip)
        if lk is not None:
            session.delete(lk)
        audit(session, action="login", actor=username, object_ref=str(user.id), result="ok")
        session.commit()
        return user, token

    # Failure path (wrong username, wrong password, or locked): count first,
    # then audit with actor=system (GWT-G5) unless this event IS the lockout.
    if user is not None and verify_password(password, user.password_hash) and locked:
        # section 9: correct passwords are also rejected while locked.
        audit(
            session,
            action="login_failed",
            actor="system",
            object_ref=username,
            result="fail",
            error_code="LOCKED",
        )
        session.commit()
        raise ApiError(401, "UNAUTHORIZED", "Unauthorized", "account temporarily locked")

    was_lockout = False
    if not locked:
        before = _get_lockout(session, username, ip)
        _record_login_failure(session, username, ip)
        after = _get_lockout(session, username, ip)
        was_lockout = after is not None and after.locked_until is not None and (
            before is None or before.locked_until is None
        )
    if not was_lockout:
        audit(
            session,
            action="login_failed",
            actor="system",
            object_ref=username,
            result="fail",
            error_code="INVALID_CREDENTIALS",
        )
    session.commit()
    raise ApiError(401, "UNAUTHORIZED", "Unauthorized", "invalid credentials or account locked")


def resolve_session(session: Session, token: str) -> tuple[Session, User]:
    """v0.2.1 section 4.2: cookie-token -> (session_row, user); expired or
    unknown tokens raise 401 UNAUTHORIZED with a clear-cookie header
    (GWT-S1-expired). Disabled users raise 403 USER_DISABLED."""
    row = (
        session.query(Session)
        .filter(Session.token_hash == _sha256(token))
        .first()
    )
    if row is None:
        raise ApiError(
            401, "UNAUTHORIZED", "Unauthorized", "unknown session",
            extra_headers=[("set-cookie", CLEAR_SESSION_COOKIE)],
        )
    if datetime.fromisoformat(row.expires_at) <= datetime.now(timezone.utc):
        session.delete(row)
        session.commit()
        raise ApiError(
            401, "UNAUTHORIZED", "Unauthorized", "session expired",
            extra_headers=[("set-cookie", CLEAR_SESSION_COOKIE)],
        )
    user = session.get(User, row.user_id)
    if user is None or not user.enabled:
        raise ApiError(
            403, "USER_DISABLED", "Account disabled",
            extra_headers=[("set-cookie", CLEAR_SESSION_COOKIE)],
        )
    row.last_used_at = _utc_now()
    session.commit()
    return row, user


def logout_user(session: Session, token: str) -> None:
    """v0.2.1 section 4.3: delete the session row; clearing the cookie is the
    router's job (Set-Cookie on the 204)."""
    row = (
        session.query(Session)
        .filter(Session.token_hash == _sha256(token))
        .first()
    )
    if row is not None:
        audit(session, action="logout", actor=str(row.user_id), object_ref=row.session_id, result="ok")
        session.delete(row)
        session.commit()


def change_password(
    session: Session, user: User, *, old_password: str, new_password: str
) -> None:
    """v0.2.1 sections 10.1/10.2 (GWT-G1/G2): verify old, strength-check new,
    store bcrypt cost 12, clear must_change_password."""
    if not verify_password(old_password, user.password_hash):
        audit(
            session,
            action="change_password_failed",
            actor="system",
            object_ref=str(user.id),
            result="fail",
            error_code="INVALID_CREDENTIALS",
        )
        session.commit()
        raise ApiError(401, "UNAUTHORIZED", "Unauthorized", "old password incorrect")
    if not isinstance(new_password, str) or len(new_password) < MIN_PASSWORD_LENGTH:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "password must be at least 8 characters")
    if is_weak_password(new_password):
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "password is in the weak-password list")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.updated_at = _utc_now()
    audit(
        session,
        action="change_password",
        actor=user.username,
        object_ref=str(user.id),
        result="ok",
    )
    session.commit()
