"""Layout-template domain service (contract v0.2 section 1.2 lines 29-35,
data model section 4 lines 201-202, state machine section 5).

The LayoutTemplate model physically lives in app.domains.templates.models
since B2-03 (D-T12-1; single definition source). Slots carry a variable
whitelist ({cn}/{blessing}/{blogger}) - any other {var} is rejected
(contract: text_template variables, no scripts).
"""

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import ApiError
from app.core.text_style import validate_text_style
from app.domains.assets_upload.models import Asset
from app.domains.templates.models import LayoutTemplate

ALLOWED_KEYS = ("to_cn", "blessing", "signature")
ALLOWED_VARS = ("{cn}", "{blessing}", "{blogger}")
REQUIRES_TO_CN = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return uuid.uuid4().hex


def _validate_slots(slots) -> list:
    """Contract section 4 line 202 + section 1.2 line 33: whitelisted keys,
    normalized regions within [0,1], variable whitelist (no scripts)."""
    if not isinstance(slots, list) or not slots:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "slots must be a non-empty list")
    seen = set()
    has_to_cn = False
    for slot in slots:
        if not isinstance(slot, dict):
            raise ApiError(400, "INVALID_REQUEST", "Invalid request", "each slot must be an object")
        key = slot.get("key")
        if key not in ALLOWED_KEYS:
            raise ApiError(400, "INVALID_REQUEST", "Invalid request", f"slot key must be one of {ALLOWED_KEYS}")
        if key in seen:
            raise ApiError(400, "INVALID_REQUEST", "Invalid request", f"duplicate slot key: {key}")
        seen.add(key)
        if key == "to_cn":
            has_to_cn = True
        region = slot.get("region")
        if not isinstance(region, dict):
            raise ApiError(400, "INVALID_REQUEST", "Invalid request", f"slot {key} needs a region object")
        for field in ("x_norm", "y_norm", "w_norm", "h_norm"):
            val = region.get(field)
            if not isinstance(val, (int, float)) or not 0 <= val <= 1:
                raise ApiError(400, "INVALID_REQUEST", "Invalid request", f"slot {key} region.{field} must be within [0,1]")
        if region.get("w_norm", 0) <= 0 or region.get("h_norm", 0) <= 0:
            raise ApiError(400, "INVALID_REQUEST", "Invalid request", f"slot {key} region must have positive w/h")
        if region.get("x_norm", 0) + region.get("w_norm", 0) > 1 or region.get("y_norm", 0) + region.get("h_norm", 0) > 1:
            raise ApiError(400, "INVALID_REQUEST", "Invalid request", f"slot {key} region exceeds the canvas")
        if "text_style" in slot:
            slot["text_style"] = validate_text_style(slot["text_style"])
        tpl = slot.get("text_template") or ""
        for ch_start in range(len(tpl)):
            if tpl[ch_start] == "{":
                end = tpl.find("}", ch_start)
                if end == -1:
                    raise ApiError(400, "INVALID_REQUEST", "Invalid request", f"slot {key} text_template has an unclosed variable")
                var = tpl[ch_start:end + 1]
                if var not in ALLOWED_VARS:
                    raise ApiError(400, "INVALID_REQUEST", "Invalid request", f"slot {key} text_template variable {var} is not allowed")
    if REQUIRES_TO_CN and not has_to_cn:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "slots must contain a to_cn slot")
    return slots


def _check_base_asset(session: Session, user_id: int, base_asset_id) -> None:
    if not isinstance(base_asset_id, str) or not base_asset_id:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "base_asset_id is required")
    asset = session.get(Asset, base_asset_id)
    if asset is None or asset.user_id != str(user_id):
        raise ApiError(404, "ASSET_NOT_FOUND", "Asset not found")


def create_template(session: Session, user_id: int, payload: dict) -> LayoutTemplate:
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "name is required")
    slots = _validate_slots(payload.get("slots"))
    base_asset_id = payload.get("base_asset_id")
    _check_base_asset(session, user_id, base_asset_id)
    canvas_w = payload.get("canvas_width")
    canvas_h = payload.get("canvas_height")
    for field, val in (("canvas_width", canvas_w), ("canvas_height", canvas_h)):
        if not isinstance(val, int) or val <= 0:
            raise ApiError(400, "INVALID_REQUEST", "Invalid request", f"{field} must be a positive integer")
    row = LayoutTemplate(
        template_id=_uuid(),
        user_id=user_id,
        name=name.strip(),
        version=1,
        status="draft",
        slots=json.dumps(slots, ensure_ascii=False),
        base_asset_id=base_asset_id,
        style_id=payload.get("style_id"),
        canvas_width=canvas_w,
        canvas_height=canvas_h,
        created_at=_now(),
    )
    session.add(row)
    audit(session, action="template_create", actor=str(user_id), object_ref=row.template_id, result="ok")
    session.commit()
    return row


def _get_owned(session: Session, user_id: int, template_id: str) -> LayoutTemplate:
    row = (
        session.query(LayoutTemplate)
        .filter(LayoutTemplate.template_id == template_id, LayoutTemplate.user_id == user_id)
        .first()
    )
    if row is None:
        raise ApiError(404, "INVALID_REQUEST", "Template not found")
    return row


def list_templates(session: Session, user_id: int, status=None, page: int = 1, page_size: int = 20) -> dict:
    if page < 1 or not 1 <= page_size <= 100:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "bad pagination")
    q = session.query(LayoutTemplate).filter(LayoutTemplate.user_id == user_id)
    if status:
        q = q.filter(LayoutTemplate.status == status)
    total = q.count()
    rows = q.order_by(LayoutTemplate.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"items": [template_dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}


def update_template(session: Session, user_id: int, template_id: str, payload: dict) -> LayoutTemplate:
    """Section 1.2 line 33: PUT bumps version; a mismatched expected_version
    answers 409 VERSION_CONFLICT (retryable). Content edits return the
    template to draft so the approval gate re-applies (D-T12-2)."""
    row = _get_owned(session, user_id, template_id)
    expected = payload.get("expected_version")
    if expected is not None and expected != row.version:
        raise ApiError(409, "VERSION_CONFLICT", "Version conflict", f"expected {expected}, current {row.version}", True)
    changed = False
    if isinstance(payload.get("name"), str) and payload["name"].strip():
        row.name = payload["name"].strip()
        changed = True
    if "slots" in payload:
        row.slots = json.dumps(_validate_slots(payload["slots"]), ensure_ascii=False)
        changed = True
    if "base_asset_id" in payload:
        _check_base_asset(session, user_id, payload["base_asset_id"])
        row.base_asset_id = payload["base_asset_id"]
        changed = True
    if "style_id" in payload:
        row.style_id = payload.get("style_id")
        changed = True
    for field in ("canvas_width", "canvas_height"):
        if field in payload:
            val = payload[field]
            if not isinstance(val, int) or val <= 0:
                raise ApiError(400, "INVALID_REQUEST", "Invalid request", f"{field} must be a positive integer")
            setattr(row, field, val)
            changed = True
    if not changed:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "nothing to update")
    row.version += 1
    if row.status not in ("draft",):
        row.status = "draft"  # content changed -> approval gate re-applies
    audit(
        session,
        action="template_update",
        actor=str(user_id),
        object_ref=template_id,
        result=f"version={row.version} status={row.status}",
    )
    session.commit()
    return row


def review_template(session: Session, user_id: int, template_id: str, decision: str) -> LayoutTemplate:
    """Section 5: draft|pending_review -> approved | rejected."""
    row = _get_owned(session, user_id, template_id)
    if decision == "approved":
        if row.status not in ("draft", "pending_review"):
            raise ApiError(409, "INVALID_REQUEST", "Invalid request", f"cannot approve from {row.status}")
        row.status = "approved"
    elif decision == "rejected":
        if row.status not in ("draft", "pending_review"):
            raise ApiError(409, "INVALID_REQUEST", "Invalid request", f"cannot reject from {row.status}")
        row.status = "rejected"
    else:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "decision must be approved|rejected")
    audit(session, action=f"template_{decision}", actor=str(user_id), object_ref=template_id, result="ok")
    session.commit()
    return row


def template_dict(row: LayoutTemplate) -> dict:
    return {
        "template_id": row.template_id,
        "name": row.name,
        "version": row.version,
        "status": row.status,
        "slots": json.loads(row.slots),
        "base_asset_id": row.base_asset_id,
        "style_id": row.style_id,
        "canvas_width": row.canvas_width,
        "canvas_height": row.canvas_height,
        "created_at": row.created_at,
    }
