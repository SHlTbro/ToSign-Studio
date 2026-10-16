"""Audit log table (core-owned) and writer, aligned with contract v0.2 section 5."""

from datetime import datetime, timezone

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.db import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    object_ref: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)


def audit(
    session: Session,
    *,
    action: str,
    actor: str,
    object_ref: str,
    result: str,
    error_code: str | None = None,
) -> None:
    """Append one audit row (UTC ISO8601 timestamp); caller controls the transaction."""
    session.add(
        AuditLog(
            actor=actor,
            timestamp=datetime.now(timezone.utc).isoformat(),
            action=action,
            object_ref=object_ref,
            result=result,
            error_code=error_code,
        )
    )