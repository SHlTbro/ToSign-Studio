"""Audit-log read-only HTTP endpoint (contract v0.2 section 8: queries go
through GET /api/v1/audit-logs). Admin-only per v0.2.1 section 7 gate
pattern; the gate is imported read-only from the backups domain."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.envelope import ok
from app.domains.auditlog import service
from app.domains.backups.service import get_current_admin_id

router = APIRouter(prefix="/api/v1/audit-logs", tags=["audit-logs"])


@router.get("")
def list_audit_logs(
    action: str | None = None,
    actor: str | None = None,
    object_ref: str | None = None,
    since: str | None = None,
    page: int = 1,
    page_size: int = 20,
    session: Session = Depends(get_session),
    admin_id: int = Depends(get_current_admin_id),
):
    result = service.list_logs(
        session,
        action=action,
        actor=actor,
        object_ref=object_ref,
        since=since,
        page=page,
        page_size=page_size,
    )
    body = ok({"items": result["items"]})
    body["meta"].update(
        {
            "total": result["total"],
            "page": result["page"],
            "page_size": result["page_size"],
            "queried_by": str(admin_id),
        }
    )
    return body
