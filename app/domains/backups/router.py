"""Backups domain HTTP endpoints (contract v0.2 section 1.7, five endpoints,
all admin-only per v0.2.1 section 7 GWT-S6-backup)."""

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.envelope import ok
from app.domains.backups import service
from app.domains.backups.models import Backup

router = APIRouter(prefix="/api/v1/backups", tags=["backups"])


@router.post("", status_code=201)
def create_backup(
    request: Request,
    session: Session = Depends(get_session),
    admin_id: int = Depends(service.get_current_admin_id),
):
    row = service.create_backup(session, admin_id)
    # T75 (D-T70-1): the build runs on the worker thread; quiesce briefly so
    # small backups answer succeeded exactly like the old synchronous path,
    # while a long build returns early with the in-progress status (existing
    # status field values). Sync endpoint -> runs on the threadpool.
    service.wait_backup_done(row.id)
    session.expire_all()
    row = service._get(session, row.id)
    return ok(service.backup_dict(row))


@router.get("")
def list_backups(
    session: Session = Depends(get_session),
    admin_id: int = Depends(service.get_current_admin_id),
):
    rows = session.query(Backup).order_by(Backup.created_at.desc()).all()
    return ok({"items": [service.backup_dict(r) for r in rows]})


@router.get("/{backup_id}")
def get_backup(
    backup_id: str,
    session: Session = Depends(get_session),
    admin_id: int = Depends(service.get_current_admin_id),
):
    row = service._get(session, backup_id)
    return ok(service.backup_dict(row))


@router.post("/{backup_id}/verify")
def verify_backup(
    backup_id: str,
    session: Session = Depends(get_session),
    admin_id: int = Depends(service.get_current_admin_id),
):
    return ok(service.verify_backup(session, admin_id, backup_id))


@router.post("/{backup_id}/restore")
def restore_backup(
    backup_id: str,
    payload: dict = Body(default={}),
    session: Session = Depends(get_session),
    admin_id: int = Depends(service.get_current_admin_id),
):
    return ok(service.restore_backup(session, admin_id, backup_id, payload.get("confirm")))
