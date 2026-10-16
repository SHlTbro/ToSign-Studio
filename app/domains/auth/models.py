"""users / invites tables (dispatch T63 spec; SQLAlchemy 2.x, sqlite)."""

from sqlalchemy import Boolean, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    # v0.2.1 section 1.1 additions (T10 login/session task):
    role: Mapped[str] = mapped_column(Text, nullable=False, default="user", server_default="user")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_login_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class Invite(Base):
    __tablename__ = "invites"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    uses_total: Mapped[int] = mapped_column(Integer, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

class Session(Base):
    """v0.2.1 section 1.2 (8 fields, GWT-S1-generation). Only the SHA-256 of
    the session token is stored; the raw token lives only in the cookie."""

    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    last_used_at: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)


class LoginLockout(Base):
    """v0.2.1 section 9 (S8): persistent per-(username, ip) failure counter.
    5 failures inside a 15-minute sliding window -> locked_until = now+15min.
    Correct passwords are also rejected while locked (GWT-S8-lockout)."""

    __tablename__ = "login_lockouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    ip_address: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    window_start: Mapped[str] = mapped_column(Text, nullable=False)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[str | None] = mapped_column(Text, nullable=True)
