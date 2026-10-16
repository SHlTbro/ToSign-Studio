"""Render domain HTTP endpoints (contract v0.2 section 1.6, 14 endpoints)."""

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.audit import audit
from app.core.db import get_session
from app.core.envelope import ok
from app.core.identity import get_current_user_id
from app.domains.backups.service import get_current_admin_id
from app.domains.render import service
from app.domains.render.models import RenderItem, RenderJob
from engine.style.m2_adapter.personal_runtime_policy import operation_entry

router = APIRouter(prefix="/api/v1/render-jobs", tags=["render-jobs"])
# B1-06: GET /api/v1/workers/status (contract v0.2 section 1.8) shares the
# render domain module but carries its own prefix; admin-only per v0.2.1
# section 7 gate pattern (read-only import of the backups gate, mirroring
# the auditlog router).
router_workers = APIRouter(prefix="/api/v1/workers", tags=["workers"])
# P16 coverage precheck (E05/E06 gap 2026-09-27): POST /api/v1/coverage/check
# as declared frozen in the frontend registry. Own prefix, same module.
# NOTE (wiring): app/main.py includes render_router and workers_router by
# explicit import; router_coverage must be added there by the main.py owner
# (app.include_router(coverage_router)) - registered in the TaskResult.
router_coverage = APIRouter(prefix="/api/v1/coverage", tags=["coverage"])


def _item_dict(session: Session, item) -> dict:
    """Serialize the persisted review decision alongside the render status."""
    # Display the same current output-bound authority that export consumes.
    # Historical/unbound or tampered-output approvals remain in audit history
    # but are not projected as current approval after reload.
    decision = service._current_output_review(session, item.user_id, item)
    result=service.item_dict(item, review_decision=decision)
    if item.status in ('failed','cancelled') and item.retryable:
        job=session.get(RenderJob,item.job_id)
        if job is not None and job.user_id==item.user_id:
            from app.domains.render.ink_render_provenance import _plan
            effective_template=None
            try:
                effective_template,slots=_plan(job,item)
                regions={slot['slot_key']:dict(slot['region']) for slot in slots}
                service._retry_validate_region(job,item,regions)
                result['retry_regions']=regions
            except (ValueError,TypeError,KeyError,service.ApiError):
                pass
            from app.domains.assets_upload.models import Asset
            effective_asset_id=(effective_template or {}).get('base_asset_id') or job.base_asset_id
            asset=session.get(Asset,effective_asset_id)
            if (effective_asset_id==job.base_asset_id and asset is not None and asset.user_id==str(item.user_id) and asset.sha256==job.base_checksum
                and asset.enabled==1 and asset.status=='approved'
                and type(asset.width) is int and type(asset.height) is int and asset.width>0 and asset.height>0):
                result['image_size']=[asset.width,asset.height]
    return result


def _job_response(session: Session, result: dict) -> dict:
    body = ok({"job": service.job_dict(result["job"]), "items": [_item_dict(session, i) for i in result["items"]]})
    if result.get("idempotent"):
        body["meta"]["idempotent_replay"] = True
    return body


def _quiesce(session: Session, job_id: str) -> RenderJob:
    """T75 (D-T10R-1): after enqueue, wait the bounded window so small jobs
    answer with their terminal state exactly like the old synchronous path;
    large jobs return queued/running in under 500ms. The session is expired
    so the worker's terminal state is what gets serialized."""
    service.wait_job_done(job_id)
    session.expire_all()
    return session.get(RenderJob, job_id)


def _reviewer_username(session: Session, user_id: int) -> str:
    """C-28: reviewed_by carries the readable username (users table read-only).
    An id with no user row falls back to the numeric id (D-T75 discrepancy)."""
    from app.domains.auth.models import User

    row = session.query(User.username).filter(User.id == user_id).first()
    if row is not None and row[0]:
        return row[0]
    return str(user_id)


@router.post("")
async def create_render_job(
    request: Request,
    session: Session = Depends(get_session),
    user_id: int = Depends(get_current_user_id),
):
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        raise service.ApiError(400, "INVALID_REQUEST", "Invalid request", "request body must be a JSON object")
    idem = request.headers.get("Idempotency-Key") or payload.get("idempotency_key")
    result = service.create_job(session, user_id, payload, idem)
    if not result.get("idempotent"):
        job = _quiesce(session, result["job"].job_id)
        result = {"job": job, "items": service.existing_items(session, job.job_id), "idempotent": False}
    return _job_response(session, result)


@router.get("")
def list_render_jobs(
    status: str | None = None,
    template_id: str | None = None,
    style_id: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    page: int = 1,
    page_size: int = 20,
    session: Session = Depends(get_session),
    user_id: int = Depends(get_current_user_id),
):
    q = session.query(RenderJob).filter(RenderJob.user_id == user_id)
    if status:
        q = q.filter(RenderJob.status == status)
    if created_after:
        q = q.filter(RenderJob.created_at >= created_after)
    if created_before:
        q = q.filter(RenderJob.created_at <= created_before)
    # P15 (E13 2026-09-27): template_id/style_id were accepted but never
    # applied. RenderJob carries only snapshots; the per-item refs written by
    # create_job (item.template_id = template_id, item.style_id = style_id)
    # are the queryable authority, so filter via a distinct-safe subquery.
    if template_id:
        q = q.filter(
            RenderJob.job_id.in_(
                session.query(RenderItem.job_id).filter(RenderItem.template_id == template_id)
            )
        )
    if style_id:
        q = q.filter(
            RenderJob.job_id.in_(
                session.query(RenderItem.job_id).filter(RenderItem.style_id == style_id)
            )
        )
    total = q.count()
    rows = (
        q.order_by(RenderJob.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    body = ok({"items": [service.job_dict(j) for j in rows]})
    body["meta"].update({"total": total, "page": page, "page_size": page_size})
    return body


@router.get("/capabilities")
@operation_entry
def get_render_capabilities(style_id: int | None = None, include_author_bound: bool = False, session: Session = Depends(get_session), user_id: int = Depends(get_current_user_id)):
    return ok(service.render_capabilities(session, user_id, style_id, include_author_bound=include_author_bound))


@router.get("/{job_id}")
@operation_entry
def get_render_job(job_id: str, session: Session = Depends(get_session), user_id: int = Depends(get_current_user_id)):
    job = service.get_job(session, user_id, job_id)
    return ok({"job": service.job_dict(job), "items": [_item_dict(session, i) for i in service.existing_items(session, job_id)]})


@router.get("/{job_id}/items")
@operation_entry
def list_job_items(job_id: str, session: Session = Depends(get_session), user_id: int = Depends(get_current_user_id)):
    service.get_job(session, user_id, job_id)
    return ok({"items": [_item_dict(session, i) for i in service.existing_items(session, job_id)]})


@router.get("/{job_id}/items/{item_id}/image")
def get_render_item_image(
    job_id: str,
    item_id: str,
    session: Session = Depends(get_session),
    user_id: int = Depends(get_current_user_id),
):
    """Rendered PNG for the review carousel (T58 Phase 4). Owner-checked."""
    from fastapi.responses import Response

    job = service.get_job(session, user_id, job_id)
    item = next((i for i in service.existing_items(session, job_id) if i.item_id == item_id), None)
    if item is None or not item.output_relative_path:
        raise service.ApiError(404, "JOB_NOT_FOUND", "Render output not found")
    from app.core.storage import ensure_within_root
    from .ink_render_provenance import validate_ink_output
    try:
        root=service.storage_root().resolve()/str(user_id)/'outputs'/job_id
        if root.resolve()!=root:raise ValueError('Owned output directory cannot alias another path')
        fp = ensure_within_root(service.storage_root() / item.output_relative_path,root)
        if not fp.is_file():raise ValueError('Render output file missing')
        if fp.stat().st_size > service.EXPORT_SINGLE_CAP:raise ValueError('Render output exceeds read limit')
        payload=fp.read_bytes()
        if len(payload) > service.EXPORT_SINGLE_CAP:raise ValueError('Render output exceeds read limit')
        validate_ink_output(session,job,item,output_bytes=payload)
    except (OSError,ValueError,TypeError,KeyError,service.ApiError) as exc:
        raise service.ApiError(409,'INK_OUTPUT_PROVENANCE_INVALID','Current output evidence is unavailable',str(exc)) from exc
    return Response(payload, media_type="image/png", headers={'Cache-Control':'no-store'})


def _retry_response(session: Session, result: dict) -> dict:
    """Shared shape for the three retry routes: the new job, its cloned items,
    and the retried_item_ids echo. The clone job holds exactly one new item per
    retried item (new item_ids) - the previous code intersected the OLD ids
    with the NEW job's items and always answered an empty items array (E13
    "空item")."""
    job = _quiesce(session, result["job"].job_id)
    return ok(
        {
            "job": service.job_dict(job),
            "items": [_item_dict(session, i) for i in service.existing_items(session, job.job_id)],
            "retried_item_ids": result["retried_item_ids"],
        }
    )


@router.post("/{job_id}/cancel")
def cancel_render_job(job_id: str, session: Session = Depends(get_session), user_id: int = Depends(get_current_user_id)):
    job = service.cancel_job(session, user_id, job_id)
    return ok({"job": service.job_dict(job)})


@router.post("/{job_id}/retry")
def retry_render_job(job_id: str, session: Session = Depends(get_session), user_id: int = Depends(get_current_user_id)):
    result = service.retry_job(session, user_id, job_id)
    return _retry_response(session, result)


@router.post("/{job_id}/retry-cancelled")
def retry_cancelled_render_job(job_id: str, session: Session = Depends(get_session), user_id: int = Depends(get_current_user_id)):
    """P15 gap (E13/E06 2026-09-27): POST /api/v1/render-jobs/{id}/retry-cancelled
    as declared in the frontend registry - retry ONLY the cancelled items
    (generic retry never auto re-runs a cancellation)."""
    result = service.retry_cancelled_job(session, user_id, job_id)
    return _retry_response(session, result)


@router.post("/{job_id}/items/{item_id}/retry")
def retry_render_item(job_id: str, item_id: str, request: Request, payload: dict | None = Body(default=None), session: Session = Depends(get_session), user_id: int = Depends(get_current_user_id)):
    """P15 gap (E13/E06 2026-09-27): POST /api/v1/render-jobs/{id}/items/{itemId}/retry
    as declared in the frontend registry - retry one explicitly targeted item."""
    if payload is not None and (set(payload)!={'region_override'} or not isinstance(payload['region_override'],dict)):
        from app.core.errors import ApiError
        raise ApiError(400,'INVALID_REQUEST','Only an explicit region_override object is accepted')
    result = service.retry_item(session, user_id, job_id, item_id, idempotency_key=request.headers.get('Idempotency-Key'),region_override=payload['region_override'] if payload is not None else None)
    return _retry_response(session, result)


@router.patch("/{job_id}/items/{item_id}")
def patch_render_item(
    job_id: str,
    item_id: str,
    payload: dict = Body(default={}),
    session: Session = Depends(get_session),
    user_id: int = Depends(get_current_user_id),
):
    item = service.patch_item(session, user_id, job_id, item_id, payload)
    return ok(_item_dict(session, item))


@router.post("/{job_id}/review")
@operation_entry
def review_render_items(
    job_id: str,
    payload: dict = Body(default={}),
    session: Session = Depends(get_session),
    user_id: int = Depends(get_current_user_id),
):
    items = service.review_items(
        session,
        user_id,
        job_id,
        payload.get("item_ids") or [],
        payload.get("decision"),
        payload.get("comment"),
        reviewer=_reviewer_username(session, user_id),
    )
    return ok({"items": [_item_dict(session, i) for i in items], "review_batch_id": items[0].review_batch_id if items else None})


@router.post("/{job_id}/items/{item_id}/approve")
@operation_entry
def approve_render_item(job_id: str, item_id: str, session: Session = Depends(get_session), user_id: int = Depends(get_current_user_id)):
    items = service.review_items(session, user_id, job_id, [item_id], "approved", None, reviewer=_reviewer_username(session, user_id))
    if not items:
        raise service.ApiError(404, "JOB_NOT_FOUND", "Item not found", "not reviewable in this state")
    return ok(_item_dict(session, items[0]))


@router.post("/{job_id}/items/{item_id}/reject")
def reject_render_item(
    job_id: str,
    item_id: str,
    payload: dict = Body(default={}),
    session: Session = Depends(get_session),
    user_id: int = Depends(get_current_user_id),
):
    items = service.review_items(session, user_id, job_id, [item_id], "rejected", payload.get("comment"), reviewer=_reviewer_username(session, user_id))
    if not items:
        raise service.ApiError(404, "JOB_NOT_FOUND", "Item not found", "not reviewable in this state")
    return ok(_item_dict(session, items[0]))


@router.post("/{job_id}/export")
@operation_entry
def export_render_job(job_id: str, session: Session = Depends(get_session), user_id: int = Depends(get_current_user_id)):
    return ok(service.export_job(session, user_id, job_id))


@router.post('/{job_id}/export-linked')
@operation_entry
def export_linked_render_job(job_id: str, session: Session = Depends(get_session), user_id: int = Depends(get_current_user_id)):
    """Explicit recovery-chain export; original records and old exports remain intact."""
    return ok(service.export_job(session,user_id,job_id,include_retries=True))


@router.get("/{job_id}/outputs")
def get_render_outputs(job_id: str, session: Session = Depends(get_session), user_id: int = Depends(get_current_user_id)):
    return ok(service.list_outputs(session, user_id, job_id))


@router.get("/{job_id}/exports/{artifact_id}/file")
@operation_entry
def get_export_artifact_file(
    job_id: str,
    artifact_id: str,
    session: Session = Depends(get_session),
    user_id: int = Depends(get_current_user_id),
):
    """Export artifact download (T58 Phase 4). Owner-checked."""
    from fastapi.responses import FileResponse

    from app.domains.render.models import ExportArtifact

    service.get_job(session, user_id, job_id)
    art = (
        session.query(ExportArtifact)
        .filter(
            ExportArtifact.artifact_id == artifact_id,
            ExportArtifact.job_id == job_id,
            ExportArtifact.user_id == user_id,
        )
        .first()
    )
    if art is None:
        raise service.ApiError(404, "JOB_NOT_FOUND", "Export artifact not found")
    from app.core.storage import ensure_within_root
    from app.domains.export_guard.service import ZIP_CAP
    from fastapi.responses import StreamingResponse
    from starlette.background import BackgroundTask
    import tempfile
    from sqlalchemy.exc import SQLAlchemyError
    from urllib.parse import quote
    sealed=tempfile.SpooledTemporaryFile(max_size=8*1024*1024,mode='w+b')
    try:
        root=service.storage_root().resolve()/str(user_id)/'outputs'/'export'
        if root.resolve()!=root:raise ValueError('Owned export directory cannot alias another path')
        fp = ensure_within_root(service.storage_root() / art.relative_path,root)
        if not fp.is_file():raise ValueError('Export artifact file missing')
        expected_size=art.size_bytes
        if type(expected_size) is not int or not 0<expected_size<=ZIP_CAP:raise ValueError('Invalid stored export size')
        total=0;digest=service.hashlib.sha256()
        with fp.open('rb') as source:
            while chunk:=source.read(1024*1024):
                total+=len(chunk)
                if total>expected_size:raise ValueError('Export artifact size changed')
                digest.update(chunk);sealed.write(chunk)
        if total!=expected_size or digest.hexdigest()!=art.sha256:raise ValueError('Export artifact byte identity changed')
        # The exact sealed copy is served, eliminating a hash/reopen swap window.
        sealed.seek(0)
        service.verify_export_linked_mapping(session,user_id,job_id,art,sealed)
        sealed.seek(0)
        service.verify_export_ink_mapping(session,user_id,job_id,art,sealed)
        sealed.seek(0)
    except (OSError,ValueError,TypeError,KeyError,SQLAlchemyError,service.ApiError) as exc:
        sealed.close()
        raise service.ApiError(409,'EXPORT_ARTIFACT_INVALID','Export artifact is unavailable',str(exc)) from exc
    def chunks():
        try:
            while chunk:=sealed.read(1024*1024):yield chunk
        finally:sealed.close()
    media_type = "application/zip" if art.relative_path.lower().endswith(".zip") else "image/png"
    return StreamingResponse(chunks(),media_type=media_type,background=BackgroundTask(sealed.close),
        headers={'Content-Length':str(expected_size),'Cache-Control':'no-store',
                 'Content-Disposition':"attachment; filename*=utf-8''"+quote(fp.name,safe='')})


@router_coverage.post("/check")
async def coverage_check(
    request: Request,
    session: Session = Depends(get_session),
    user_id: int = Depends(get_current_user_id),
):
    """P16 gap (E05/E06 2026-09-27): POST /api/v1/coverage/check as declared
    frozen in the frontend registry. Thin read-only precheck over the render
    core's own font/glyph judgment (service.coverage_check reuses the exact
    _resolve_font/_expand_slot_text/_char_set_ok path); returns the
    missing/fallback/unresolved picture without creating any row."""
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        raise service.ApiError(400, "INVALID_REQUEST", "Invalid request", "request body must be a JSON object")
    return ok(service.coverage_check(session, user_id, payload))


@router_workers.get("/status")
def get_workers_status(
    session: Session = Depends(get_session),
    admin_id: int = Depends(get_current_admin_id),
):
    """GET /api/v1/workers/status (contract v0.2 section 1.8; B1-06).

    Real numbers only: queue_depth/running_tasks come from the render
    dual-lane queue snapshot (service.queue_status) plus the in-flight DB
    rows, the backup worker contributes its own read-only snapshot. The
    headline fields follow the P11 contract shape (worker_id / queue_depth /
    running_tasks / max_concurrency); gpu_available is honestly false (CPU-
    only PIL render). Audit: one workers_status_query row per admin read
    (shared discipline for new endpoints)."""
    from app.domains.backups.models import Backup
    from app.domains.backups import service as backups_service

    render_snapshot = service.queue_status()
    backup_snapshot = backups_service.queue_status()
    render_running = session.query(RenderJob).filter(RenderJob.status == "running").count()
    backup_running = session.query(Backup).filter(Backup.status == "running").count()
    render_snapshot["running_tasks"] = render_running
    backup_snapshot["running_tasks"] = backup_running
    workers = [render_snapshot, backup_snapshot]
    audit(
        session,
        action="workers_status_query",
        actor=str(admin_id),
        object_ref="workers/status",
        result=(
            f"alive={sum(1 for w in workers if w['alive'])}"
            f" queue_depth={render_snapshot['queue_depth']}"
            f" running_tasks={render_running}"
        ),
    )
    session.commit()
    return ok(
        {
            "worker_id": render_snapshot["worker_id"],
            "queue_depth": render_snapshot["queue_depth"],
            "running_tasks": render_running,
            "max_concurrency": render_snapshot["max_concurrency"],
            "worker_count": len(workers),
            "gpu_available": False,
            "last_heartbeat": render_snapshot["last_heartbeat"],
            "workers": workers,
        }
    )
