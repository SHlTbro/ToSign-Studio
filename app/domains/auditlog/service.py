"""Audit-log query service: read-only projections over app/core/audit.py rows."""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.audit import AuditLog
from app.core.errors import ApiError

MAX_PAGE_SIZE = 100


def normalize_since(since: str) -> str:
    """Validate ``since`` and normalize it to the exact UTC text format the
    audit writer stores (``datetime.now(timezone.utc).isoformat()``, i.e.
    ``2026-09-27T12:34:56.789012+00:00``), so the SQL-side lexicographic
    ``>=`` comparison stays a true inclusive UTC bound (P19: since非法时区).

    - Any offset the caller sends (+08:00, Z) is converted to UTC +00:00.
    - A naive value is interpreted as UTC (audit timestamps carry no local
      time anywhere in the pipeline).
    - Unparseable input raises 400 INVALID_REQUEST instead of silently
      matching nothing - an error must never masquerade as "no records".
    """
    try:
        parsed = datetime.fromisoformat(since)
    except ValueError:
        raise ApiError(
            400,
            "INVALID_REQUEST",
            "Invalid request",
            "since must be an ISO-8601 timestamp, e.g. 2026-09-27T00:00:00+00:00",
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def log_dict(row: AuditLog) -> dict:
    return {
        "log_id": row.id,
        "actor": row.actor,
        "timestamp": row.timestamp,
        "action": row.action,
        "object_ref": row.object_ref,
        "result": row.result,
        "error_code": row.error_code,
    }


def list_logs(
    session: Session,
    *,
    action: str | None = None,
    actor: str | None = None,
    object_ref: str | None = None,
    since: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Server-side filtered, paginated audit query (newest first).

    ``since`` is an inclusive lower bound on the timestamp; it is validated
    and normalized to UTC by :func:`normalize_since` before comparison, so
    both plain dates and offset/Z forms compare correctly against the stored
    ``+00:00`` text.
    """
    if page < 1 or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "page must be >= 1 and page_size within 1..100")
    q = session.query(AuditLog)
    if action:
        q = q.filter(AuditLog.action == action)
    if actor:
        q = q.filter(AuditLog.actor == actor)
    if object_ref:
        q = q.filter(AuditLog.object_ref == object_ref)
    if since:
        q = q.filter(AuditLog.timestamp >= normalize_since(since))
    total = q.count()
    rows = q.order_by(AuditLog.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"items": [log_dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}
