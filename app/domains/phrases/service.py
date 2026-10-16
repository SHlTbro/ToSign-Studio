"""CC-15 phrase template business rules.

Field set per frozen contract v0.2 sections 1.3/4: id, text, enabled, created_by, updated_at.
Contract gaps (types, validation, 404 code, used_by_job_ids) tracked as discrepancy D-T68-5.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domains.phrases.models import BlessingTemplate


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_text(text: object) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "text must be a non-empty, non-whitespace string")


def to_dict(template: BlessingTemplate) -> dict:
    return {
        "id": template.id,
        "text": template.text,
        "enabled": bool(template.enabled),
        "created_by": template.created_by,
        "updated_at": template.updated_at,
    }


def list_templates(session: Session, enabled: Optional[bool] = None) -> list:
    stmt = select(BlessingTemplate).order_by(BlessingTemplate.updated_at, BlessingTemplate.id)
    if enabled is not None:
        stmt = stmt.where(BlessingTemplate.enabled.is_(enabled))
    rows = session.scalars(stmt).all()
    return [to_dict(row) for row in rows]


def create_template(session: Session, user_id: int, text: object, enabled: bool = True) -> dict:
    _validate_text(text)
    template = BlessingTemplate(
        id=str(uuid.uuid4()),
        text=text,
        enabled=bool(enabled),
        created_by=user_id,
        updated_at=_utc_now(),
    )
    session.add(template)
    session.commit()
    return to_dict(template)


def _get(session: Session, template_id: str) -> BlessingTemplate:
    row = session.get(BlessingTemplate, template_id)
    if row is None:
        # v0.2 section 3 defines no blessing-template-specific 404 code; see discrepancy D-T68-5
        raise ApiError(404, "TEMPLATE_NOT_FOUND", "Template not found", "phrase template does not exist")
    return row


def get_template(session: Session, template_id: str) -> dict:
    return to_dict(_get(session, template_id))


def update_template(
    session: Session,
    template_id: str,
    text: Optional[object] = None,
    enabled: Optional[bool] = None,
) -> dict:
    row = _get(session, template_id)
    if text is not None:
        _validate_text(text)
        row.text = text
    if enabled is not None:
        row.enabled = bool(enabled)
    row.updated_at = _utc_now()
    session.commit()
    return to_dict(row)


def set_enabled(session: Session, template_id: str, enabled: bool) -> dict:
    row = _get(session, template_id)
    row.enabled = enabled
    row.updated_at = _utc_now()
    session.commit()
    return to_dict(row)
