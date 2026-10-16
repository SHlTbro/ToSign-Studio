"""CC-14 blogger name preset business rules (contract: contract-v0.3-patch-C section 7).

Concurrency (C-22 hardening): preset creation enforces the <=5 limit with a
single atomic INSERT ... SELECT ... WHERE (SELECT COUNT(*) ...) < 5 statement.
sqlite serializes writers at the statement level (single-writer semantics,
busy timeout from app.core.db connect args), so concurrent creators can never
jointly overshoot the limit; losers see rowcount=0 and get the domain error
409 PRESET_LIMIT_EXCEEDED instead of a lock error surfacing as HTTP 500.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domains.presets.models import BloggerNamePreset

MAX_PRESETS_PER_USER = 5
# Contract does not define a name length limit; 50 chosen locally, tracked as discrepancy D-T68-1.
NAME_MAX_LEN = 50

_LIMITED_INSERT = text(
    "INSERT INTO blogger_name_presets (preset_id, user_id, name, is_default, updated_at) "
    "SELECT :preset_id, :user_id, :name, :is_default, :updated_at "
    "WHERE (SELECT COUNT(*) FROM blogger_name_presets WHERE user_id = :user_id) < :max_count"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_name(name: object) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "name must be a non-empty, non-whitespace string")
    if len(name) > NAME_MAX_LEN:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", f"name must be at most {NAME_MAX_LEN} characters")


def to_dict(preset: BloggerNamePreset) -> dict:
    return {
        "preset_id": preset.preset_id,
        "name": preset.name,
        "is_default": bool(preset.is_default),
        "updated_at": preset.updated_at,
    }


def list_presets(session: Session, user_id: int) -> list:
    rows = session.scalars(
        select(BloggerNamePreset)
        .where(BloggerNamePreset.user_id == user_id)
        .order_by(BloggerNamePreset.updated_at, BloggerNamePreset.preset_id)
    ).all()
    return [to_dict(row) for row in rows]


def _clear_default(session: Session, user_id: int) -> None:
    rows = session.scalars(
        select(BloggerNamePreset).where(
            BloggerNamePreset.user_id == user_id,
            BloggerNamePreset.is_default.is_(True),
        )
    ).all()
    for row in rows:
        row.is_default = False


def create_preset(session: Session, user_id: int, name: object, is_default: bool) -> dict:
    _validate_name(name)
    if is_default:
        # single default per user: clear siblings in the same transaction (patch-C section 7)
        _clear_default(session, user_id)
        session.flush()
    values = {
        "preset_id": str(uuid.uuid4()),
        "user_id": user_id,
        "name": name,
        "is_default": bool(is_default),
        "updated_at": _utc_now(),
    }
    inserted = session.execute(
        _LIMITED_INSERT, {**values, "max_count": MAX_PRESETS_PER_USER}
    ).rowcount
    if inserted != 1:
        session.rollback()
        raise ApiError(
            409,
            "PRESET_LIMIT_EXCEEDED",
            "Preset limit exceeded",
            f"a user may keep at most {MAX_PRESETS_PER_USER} blogger name presets",
        )
    session.commit()
    return dict(values)


def _get_own(session: Session, user_id: int, preset_id: str) -> BloggerNamePreset:
    row = session.get(BloggerNamePreset, preset_id)
    # missing and foreign presets share one 404 code: anti-enumeration (patch-C section 7)
    if row is None or row.user_id != user_id:
        raise ApiError(404, "PRESET_NOT_FOUND", "Preset not found", "preset does not exist")
    return row


def update_preset(
    session: Session,
    user_id: int,
    preset_id: str,
    name: Optional[object] = None,
    is_default: Optional[bool] = None,
) -> dict:
    row = _get_own(session, user_id, preset_id)
    if name is not None:
        _validate_name(name)
        row.name = name
    if is_default is not None:
        if is_default:
            _clear_default(session, user_id)
        row.is_default = bool(is_default)
    row.updated_at = _utc_now()
    session.commit()
    return to_dict(row)


def delete_preset(session: Session, user_id: int, preset_id: str) -> None:
    row = _get_own(session, user_id, preset_id)
    session.delete(row)
    session.commit()
    # deleting the default must NOT auto-promote another preset (Owner ruling 16.5)
