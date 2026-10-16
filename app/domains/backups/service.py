"""Backups domain service (contract v0.2 section 1.7 lines 123-138,
v0.2.1 section 7 admin-only, patch-C section 10 CC-17 session revoke).

Execution model (T75, D-T70-1): create_backup registers the row (status
queued) and hands the bundle build to a single resident worker thread
(started by create_app); the endpoint quiesces briefly so small backups
answer succeeded exactly like the old synchronous path, while a long build
returns early with the in-progress status carried by the existing status
field. The sqlite database file is snapshotted into the bundle; restoring it
back is a deployment-time offline operation because the running process holds
the connection (D-T70-2) - pools are restored live, sessions are revoked per
CC-17.
"""

import hashlib
import json
import logging
import os
import queue
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePath

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from app.core.audit import audit
from app.core.db import get_engine, get_session
from app.core.errors import ApiError
from app.domains.auth.models import Session as SessionRow, User
from app.domains.backups.models import Backup

POOLS = ("assets", "training_images", "style_samples", "outputs", "models")
DB_SNAPSHOT_NAME = "database.sqlite"

# T75 worker machinery (D-T70-1): FIFO, one daemon thread, own session.
BACKUP_WAIT_SECONDS = 5.0  # response quiescence window (t70 answers succeeded)
_backup_queue: "queue.Queue" = queue.Queue()
_backup_worker_thread: threading.Thread | None = None
_done_events: dict = {}
_done_lock = threading.Lock()
logger = logging.getLogger("yy.backups")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return uuid.uuid4().hex


def storage_root() -> Path:
    return Path(os.environ["YY_STORAGE_ROOT"])


def _db_path():
    url = os.environ.get("YY_DB_URL", "")
    if url.startswith("sqlite:///"):
        p = Path(url[len("sqlite:///"):])
        return p if p.exists() else None
    return None


def get_current_admin_id(request: Request, session: Session = Depends(get_session)) -> int:
    """v0.2.1 section 7 (GWT-S6-backup): backup endpoints are admin-only.
    Cookie path resolves the real user; the X-User-Id shim is allowed only
    when identity.shim_enabled() says the dev door is open AND the referenced
    user exists in the DB with role=admin."""
    token = request.cookies.get("session")
    user = None
    if token:
        from app.domains.auth import service as auth_service

        _, user = auth_service.resolve_session(session, token)
    else:
        from app.core.identity import shim_enabled

        if not shim_enabled():
            # D-T10-2 / P01-BE semantics (identity._resolve): the X-User-Id
            # shim is a dev/test backdoor that MUST answer 401 under
            # YY_AUTH_STRICT=1. Going around identity's single predicate here
            # let a spoofed admin header run every backup/audit admin endpoint
            # in strict mode (B-LIBRARY cross-domain finding, fixed in place).
            raise ApiError(401, "UNAUTHORIZED", "Unauthorized", "authentication required")
        raw = request.headers.get("X-User-Id")
        try:
            uid = int(raw) if raw is not None else -1
        except (TypeError, ValueError):
            uid = -1
        if uid <= 0:
            raise ApiError(401, "UNAUTHORIZED", "Unauthorized", "authentication required")
        user = session.get(User, uid)
        if user is None:
            raise ApiError(401, "UNAUTHORIZED", "Unauthorized", "unknown user")
    if user.role != "admin":
        # section 12: FORBIDDEN is reserved for insufficient role.
        raise ApiError(403, "FORBIDDEN", "Forbidden", "admin role required")
    return user.id


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_tree(src: Path, dst: Path) -> None:
    if not src.is_dir():
        return
    for dirpath, _dirnames, filenames in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in filenames:
            shutil.copy2(Path(dirpath) / name, dst / rel / name)


def create_backup(session: Session, admin_id: int) -> Backup:
    """v0.2 section 1.7: register the backup row, then the worker bundles
    pools + db snapshot + manifest + checksums (D-T70-1)."""
    backup_id = _uuid()
    row = Backup(
        id=backup_id,
        user_id=admin_id,
        created_at=_now(),
        size_bytes=0,
        includes=json.dumps(["manifest.json", "checksums.sha256"]),
        checksum="",
        status="queued",
        path_relative="_backups/" + backup_id,
    )
    session.add(row)
    audit(session, action="backup", actor=str(admin_id), object_ref=backup_id, result="queued")
    session.commit()
    enqueue_backup(backup_id)
    return row


def enqueue_backup(backup_id: str) -> None:
    with _done_lock:
        _done_events.setdefault(backup_id, threading.Event())
    _backup_queue.put(backup_id)


def wait_backup_done(backup_id: str, timeout: float = BACKUP_WAIT_SECONDS) -> bool:
    with _done_lock:
        ev = _done_events.get(backup_id)
    if ev is None:
        return True  # already completed and reaped
    return ev.wait(timeout)


def _build_bundle(backup_id: str, admin_id: int) -> tuple:
    """Filesystem work only: bundle pools + db snapshot + manifest +
    checksums; returns (includes, total_bytes, checksum)."""
    root = storage_root()
    bdir = root / "_backups" / backup_id
    bdir.mkdir(parents=True, exist_ok=True)

    db_src = _db_path()
    includes = ["manifest.json", "checksums.sha256"]
    if db_src is not None:
        shutil.copy2(db_src, bdir / DB_SNAPSHOT_NAME)
        includes.append(DB_SNAPSHOT_NAME)
    for entry in sorted(root.iterdir()):
        if entry.name == "_backups":
            continue
        if entry.is_dir():
            _copy_tree(entry, bdir / entry.name)
            includes.append(entry.name + "/")
        else:
            shutil.copy2(entry, bdir / entry.name)
            includes.append(entry.name)

    manifest = {
        "backup_id": backup_id,
        "created_at": _now(),
        "created_by": admin_id,
        "includes": includes,
        "db_snapshot_at": _now() if db_src is not None else None,
        "encryption": None,  # section 1.7 line 138: P1 field reserved
    }
    (bdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = []
    total = 0
    for dirpath, _dirnames, filenames in os.walk(bdir):
        for name in filenames:
            fp = Path(dirpath) / name
            if fp.name == "checksums.sha256":
                continue
            rel = fp.relative_to(bdir).as_posix()
            lines.append(_file_sha256(fp) + "  " + rel)
            total += fp.stat().st_size
    (bdir / "checksums.sha256").write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    return includes, total + (bdir / "checksums.sha256").stat().st_size, _file_sha256(bdir / "checksums.sha256")


def _execute_backup(backup_id: str) -> None:
    """Worker-side run: fresh session, full failure containment - a failed
    build marks the row failed and never answers 500 (G75-11)."""
    s = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)()
    try:
        row = s.get(Backup, backup_id)
        if row is None or row.status != "queued":
            return
        admin_id = row.user_id
        row.status = "running"
        s.commit()
        includes, total, checksum = _build_bundle(backup_id, admin_id)
        row = s.get(Backup, backup_id)
        if row is not None:
            row.size_bytes = total
            row.includes = json.dumps(includes)
            row.checksum = checksum
            row.status = "succeeded"
            audit(s, action="backup", actor=str(admin_id), object_ref=backup_id, result="ok")
            s.commit()
    except Exception as exc:  # noqa: BLE001 - containment is the point
        logger.exception("backup %s failed", backup_id)
        try:
            s.rollback()
            row = s.get(Backup, backup_id)
            if row is not None:
                row.status = "failed"
                audit(s, action="backup", actor=str(row.user_id), object_ref=backup_id, result="fail", error_code=str(exc)[:200])
                s.commit()
        except Exception:  # noqa: BLE001 - never let the worker die
            s.rollback()
    finally:
        s.close()
        with _done_lock:
            ev = _done_events.pop(backup_id, None)
        if ev is not None:
            ev.set()


def _backup_worker_loop() -> None:
    while True:
        backup_id = _backup_queue.get()
        try:
            _execute_backup(backup_id)
        finally:
            _backup_queue.task_done()


def start_worker() -> threading.Thread:
    """create_app() wiring: one resident daemon worker per process."""
    global _backup_worker_thread
    if _backup_worker_thread is not None and _backup_worker_thread.is_alive():
        return _backup_worker_thread
    _backup_worker_thread = threading.Thread(target=_backup_worker_loop, name="yy-backup-worker", daemon=True)
    _backup_worker_thread.start()
    return _backup_worker_thread


def queue_status() -> dict:
    """Read-only telemetry snapshot for GET /api/v1/workers/status (B1-06).
    Never touches the queue or the worker thread."""
    return {
        "worker_id": "backup-worker-01",
        "alive": bool(_backup_worker_thread is not None and _backup_worker_thread.is_alive()),
        "queue_depth": _backup_queue.qsize(),
        "max_concurrency": 1,  # single resident worker thread per process
        "last_heartbeat": None,  # backup worker idles on a blocking get(); no loop heartbeat
    }


def _verify_checksums(bdir: Path) -> None:
    """Section 1.7: checksums first; mismatch refuses restore (422)."""
    sums = bdir / "checksums.sha256"
    if not sums.exists():
        raise ApiError(422, "BACKUP_CHECKSUM_MISMATCH", "Checksum mismatch", "checksums.sha256 missing")
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        if rel.startswith(("/", "\\")) or ":" in rel or "\\" in rel or ".." in PurePath(rel).parts:
            # P20 zip-slip guard (2026-09-27): a checksums entry must never
            # resolve outside the bundle directory, not even for a read.
            raise ApiError(422, "BACKUP_CHECKSUM_MISMATCH", "Checksum mismatch", "unsafe path in checksums: " + rel)
        fp = bdir / rel
        if not fp.exists() or _file_sha256(fp) != digest:
            raise ApiError(422, "BACKUP_CHECKSUM_MISMATCH", "Checksum mismatch", rel)


def _verify_bundle_integrity(row: Backup, bdir: Path) -> None:
    """P20 receipt (2026-09-27 wave 2), called before ANY restore write:
    (1) tamper-evidence - the bundle's checksums.sha256 must still hash to
    the checksum recorded in the backups row at build time, so a tampered
    bundle cannot re-sign itself past verification (pre-fix repro: a
    regenerated checksums.sha256 verified 200 and restored tampered files);
    (2) corruption - every listed file must match its digest."""
    sums = bdir / "checksums.sha256"
    if row.checksum and sums.exists() and _file_sha256(sums) != row.checksum:
        raise ApiError(
            422,
            "BACKUP_CHECKSUM_MISMATCH",
            "Checksum mismatch",
            "checksums.sha256 does not match the checksum recorded when the backup was created",
        )
    _verify_checksums(bdir)


def _get(session: Session, backup_id: str) -> Backup:
    row = session.get(Backup, backup_id)
    if row is None:
        raise ApiError(404, "BACKUP_NOT_FOUND", "Backup not found")
    return row


def verify_backup(session: Session, admin_id: int, backup_id: str) -> dict:
    row = _get(session, backup_id)
    bdir = storage_root() / row.path_relative
    _verify_bundle_integrity(row, bdir)
    audit(session, action="backup_verify", actor=str(admin_id), object_ref=backup_id, result="ok")
    session.commit()
    return {"backup_id": backup_id, "status": "checksum_ok"}


def restore_backup(session: Session, admin_id: int, backup_id: str, confirm) -> dict:
    """Section 1.7 restore rules + patch-C section 10 (CC-17): checksums
    first, explicit confirmation, staged copy then swap, failure never
    overwrites, full audit, ALL sessions revoked afterwards."""
    row = _get(session, backup_id)
    if not confirm:
        raise ApiError(409, "RESTORE_CONFIRMATION_REQUIRED", "Confirmation required", "restore is destructive; send confirm=true")
    bdir = storage_root() / row.path_relative
    _verify_bundle_integrity(row, bdir)

    root = storage_root()
    staged = root / "_backups" / ("_staging_" + backup_id)
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(parents=True)
    try:
        for entry in sorted(bdir.iterdir()):
            if entry.name in ("manifest.json", "checksums.sha256", DB_SNAPSHOT_NAME):
                continue
            if entry.is_dir():
                _copy_tree(entry, staged / entry.name)
            else:
                shutil.copy2(entry, staged / entry.name)
    except Exception as exc:
        shutil.rmtree(staged, ignore_errors=True)
        raise ApiError(500, "RESTORE_FAILED", "Restore failed", str(exc)[:300], True) from exc

    revoked = session.query(SessionRow).count()
    for entry in sorted(staged.iterdir()):
        live = root / entry.name
        if live.exists():
            if live.is_dir():
                shutil.rmtree(live)
            else:
                live.unlink()
        if entry.is_dir():
            _copy_tree(entry, live)
        else:
            shutil.copy2(entry, live)

    # CC-17: every session for this DATA_ROOT is revoked; the admin's own
    # session dies with the rest - next request answers 401.
    session.query(SessionRow).delete()
    audit(session, action="restore", actor=str(admin_id), object_ref=backup_id, result="sessions_revoked=" + str(revoked))
    session.commit()
    return {"backup_id": backup_id, "restored": True, "sessions_revoked": revoked}


def backup_dict(row: Backup) -> dict:
    return {
        "backup_id": row.id,
        "status": row.status,
        "created_at": row.created_at,
        "size_bytes": row.size_bytes,
        "includes": json.loads(row.includes),
        "checksum": row.checksum,
        "path_relative": row.path_relative,
    }
