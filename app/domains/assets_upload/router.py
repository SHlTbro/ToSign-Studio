"""POST /api/v1/assets - multipart multi-file upload with quota/limits gates."""
import os
from fastapi import APIRouter, Body, Depends, File, Form, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from app.core.db import get_session
from app.core.envelope import ok
from app.core.errors import ApiError
from app.core.identity import get_current_user_id
from app.domains.backups.service import get_current_admin_id

from app.domains.assets_upload import service
from app.domains.assets_upload.models import Asset
from app.domains.usage.service import storage_root

router = APIRouter()


@router.post("/api/v1/assets", status_code=201)
async def upload_assets(
    response: Response,
    files: list[UploadFile] = File(...),
    # B1-P04 (2026-09-27): None sentinel so an EXPLICIT asset_type=cos is
    # distinguishable from the default; previously `type=sample_source` plus
    # an explicit `asset_type=cos` silently re-bucketed to sample_source.
    asset_type: str | None = Form(default=None),
    legacy_type: str | None = Form(default=None, alias="type"),
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    if legacy_type is not None:
        if asset_type is not None and asset_type != legacy_type:
            raise ApiError(400, "INVALID_REQUEST", "type and asset_type must match")
        asset_type = legacy_type
    if asset_type is None:
        asset_type = "cos"
    if asset_type not in {"cos", "sample_source"}:
        raise ApiError(400, "INVALID_REQUEST", "asset_type must be cos or sample_source")
    payloads = [(f.filename, await f.read()) for f in files]
    # C-26: PIL decode / thumbnails / disk writes never run on the event loop;
    # the session is used strictly sequentially across the two threads.
    results = await run_in_threadpool(
        service.store_batch, session, str(user_id), payloads, asset_type
    )
    body_assets = [
        {
            "asset_id": item["asset"].asset_id,
            "filename": item["filename"],
            "reused": item["reused"],
            "sha256": item["sha256"],
                    "thumbnail_path": (item["asset"].thumbnail_path if "asset" in item else None),
            "width": item["width"],
            "height": item["height"],
            "bytes": len(item["stored_bytes"]),
        }
        for item in results
    ]
    if all(item["reused"] for item in results):
        response.status_code = 200
    return ok({"items": body_assets})

@router.get('/api/v1/assets')
def list_assets(
    request: Request,
    type: str | None = None,
    status: str | None = None,
    sha256: str | None = None,
    scope: str | None = None,
    page: int = 1,
    page_size: int = 20,
    session=Depends(get_session),
    user_id: int = Depends(get_current_user_id),
):
    """Owner-scoped asset list. P05 (2026-09-27): ``scope=all`` switches to the
    admin review listing across ALL owners (so an admin can actually find a
    creator's pending upload); it goes through the exact same admin gate as
    approve/reject (401 unauthenticated / 403 non-admin). Default (no scope)
    behavior is unchanged: strictly the caller's own live assets."""
    from app.core.envelope import ok as _ok

    if page < 1 or not 1 <= page_size <= 100:
        raise ApiError(400, 'INVALID_REQUEST', 'Invalid request', 'bad pagination')

    q = session.query(Asset).filter(Asset.enabled == 1)
    if scope == "all":
        get_current_admin_id(request, session)
    else:
        q = q.filter(Asset.user_id == str(user_id))
    if type:
        q = q.filter(Asset.kind == type)
    if status:
        q = q.filter(Asset.status == status)
    if sha256:
        q = q.filter(Asset.sha256 == sha256)
    total = q.count()
    rows = q.order_by(Asset.uploaded_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    items = [
        {
            'asset_id': r.asset_id,
            'type': r.kind,
            'status': r.status,
            'sha256': r.sha256,
            'bytes': r.bytes,
            'width': r.width,
            'height': r.height,
            'thumbnail_path': r.thumbnail_path or r.relative_path,
            'created_at': r.uploaded_at.isoformat() if r.uploaded_at else None,
            'approved_at': r.approved_at,
        }
        for r in rows
    ]
    body = _ok({'items': items})
    body['meta'].update({'total': total, 'page': page, 'page_size': page_size})
    return body


def _owned_asset(session, user_id: int, asset_id: str) -> Asset:
    row = session.query(Asset).filter(Asset.asset_id == asset_id, Asset.user_id == str(user_id)).first()
    if row is None or row.enabled != 1:
        from app.core.errors import ApiError
        raise ApiError(404, 'ASSET_NOT_FOUND', 'Asset not found')
    return row


@router.get('/api/v1/assets/{asset_id}')
def get_asset(asset_id: str, session=Depends(get_session), user_id: int = Depends(get_current_user_id)):
    from app.core.envelope import ok as _ok

    row = _owned_asset(session, user_id, asset_id)
    return _ok({
        'asset_id': row.asset_id, 'type': row.kind, 'status': row.status,
        'sha256': row.sha256, 'bytes': row.bytes, 'width': row.width, 'height': row.height,
        'relative_path': row.relative_path, 'thumbnail_path': row.thumbnail_path or row.relative_path,
        'created_at': row.uploaded_at.isoformat() if row.uploaded_at else None,
    })


def _reviewable_asset(session, asset_id: str) -> Asset:
    """Admin review lookup: any owner's live asset (no user_id filter)."""
    row = session.query(Asset).filter(Asset.asset_id == asset_id, Asset.enabled == 1).first()
    if row is None:
        from app.core.errors import ApiError
        raise ApiError(404, 'ASSET_NOT_FOUND', 'Asset not found')
    return row


def _decide_asset(session, admin_id: int, asset_id: str, decision: str) -> Asset:
    """T58B (D-T12-3) approval flow, anchored to the templates domain review
    semantics: pending -> approved|rejected only; a repeated decision on a
    terminal state answers 409 like templates review_template; the decision
    (who/when) lands in approved_at/approved_by; every decision writes audit.
    Admin gate per G58B-01 (non-admin 403)."""
    from datetime import datetime, timezone

    row = _reviewable_asset(session, asset_id)
    if row.status != 'pending':
        from app.core.errors import ApiError
        raise ApiError(409, 'INVALID_REQUEST', 'Invalid request', f'cannot {decision} from {row.status}')
    row.status = decision
    row.approved_at = datetime.now(timezone.utc).isoformat()
    row.approved_by = str(admin_id)
    # Audit action keeps the established asset_approve/asset_reject names
    # (contract v0.2 section 8); the decision value is the new status.
    _ok_audit(session, admin_id, 'asset_approve' if decision == 'approved' else 'asset_reject', asset_id)
    return row


@router.post('/api/v1/assets/{asset_id}/approve')
def approve_asset(asset_id: str, session=Depends(get_session), admin_id: int = Depends(get_current_admin_id)):
    from app.core.envelope import ok as _ok

    row = _decide_asset(session, admin_id, asset_id, 'approved')
    return _ok({'asset_id': row.asset_id, 'status': row.status})


@router.post('/api/v1/assets/{asset_id}/reject')
def reject_asset(asset_id: str, payload: dict = Body(default={}), session=Depends(get_session), admin_id: int = Depends(get_current_admin_id)):
    from app.core.envelope import ok as _ok

    # reason accepted for API compatibility; not a contract field, so the
    # audit row keeps the templates-anchored result='ok' shape.
    _ = payload.get('reason')
    row = _decide_asset(session, admin_id, asset_id, 'rejected')
    return _ok({'asset_id': row.asset_id, 'status': row.status})


@router.delete('/api/v1/assets/{asset_id}')
def delete_asset(asset_id: str, force: bool = False, session=Depends(get_session), user_id: int = Depends(get_current_user_id)):
    from app.core.envelope import ok as _ok

    row = _owned_asset(session, user_id, asset_id)
    if force:
        # P05 (2026-09-27): refuse hard delete while a live template or render
        # job still references the asset - a dangling base_asset_id makes the
        # template unusable and crashes render execution, which re-reads the
        # asset row at run time (render/service.py job run path).
        from app.domains.render.models import RenderJob
        from app.domains.templates.models import LayoutTemplate

        refs = session.query(LayoutTemplate).filter(LayoutTemplate.base_asset_id == asset_id).count()
        refs += session.query(RenderJob).filter(RenderJob.base_asset_id == asset_id).count()
        if refs:
            raise ApiError(409, 'ASSET_IN_USE', 'Asset in use', f'referenced by {refs} template/render-job row(s); remove the references first')
        # hard delete: file + row (section 1.1 line 27); the thumbnail is part
        # of the same asset, so it must not survive as quota-counted residue.
        p = os.path.join(storage_root(), row.relative_path)
        if os.path.exists(p):
            os.remove(p)
        if row.thumbnail_path:
            tp = os.path.join(storage_root(), row.thumbnail_path)
            if os.path.exists(tp):
                os.remove(tp)
        session.delete(row)
        _ok_audit(session, user_id, 'asset_hard_delete', asset_id)
        return _ok({'asset_id': asset_id, 'deleted': 'hard'})
    row.enabled = 0
    _ok_audit(session, user_id, 'asset_soft_delete', asset_id)
    return _ok({'asset_id': asset_id, 'deleted': 'soft'})


def _ok_audit(session, user_id, action, asset_id, extra=None):
    from app.core.audit import audit as _audit

    _audit(session, action=action, actor=str(user_id), object_ref=asset_id, result='ok', error_code=extra)
    session.commit()

@router.get('/api/v1/assets/{asset_id}/thumbnail')
def get_asset_thumbnail(asset_id: str, session=Depends(get_session), user_id: int = Depends(get_current_user_id)):
    from fastapi.responses import FileResponse

    row = _owned_asset(session, user_id, asset_id)
    thumb_rel = row.thumbnail_path or row.relative_path
    fp = os.path.join(storage_root(), thumb_rel)
    if not os.path.exists(fp):
        from app.core.errors import ApiError
        raise ApiError(404, 'ASSET_NOT_FOUND', 'Thumbnail not found')
    return FileResponse(fp, media_type='image/png')


@router.get('/api/v1/assets/{asset_id}/file')
def get_asset_file(asset_id: str, session=Depends(get_session), user_id: int = Depends(get_current_user_id)):
    """Full-resolution original file for the annotation canvas (T58 Phase 2).
    Lossless red line: bytes are served as stored, no re-encode."""
    from fastapi.responses import FileResponse

    row = _owned_asset(session, user_id, asset_id)
    fp = os.path.join(storage_root(), row.relative_path)
    if not os.path.exists(fp):
        from app.core.errors import ApiError
        raise ApiError(404, 'ASSET_NOT_FOUND', 'Asset file not found')
    media = 'image/png' if fp.lower().endswith('.png') else 'image/jpeg'
    return FileResponse(fp, media_type=media)
