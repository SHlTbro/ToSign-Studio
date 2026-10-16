"""POST /api/v1/users/me/export (contract v0.2.1 section 7 GWT-S6-export,
D-T10-6a, batch task B1-04).

Packs the caller's own data into a ZIP that is streamed back as the
response body (FileResponse reads from disk in chunks - the browser
downloads via URL streaming, no JS-side mega-blob is required by the
server). This endpoint is completely separate from the admin global
backup domain (GWT-S6-backup): no _backups involvement, no DB snapshot,
no session revocation.

ZIP content (GWT-S6-export: "assets / jobs / outputs"):
- ``assets/`` and ``outputs/``: the caller's own pool files from
  ``<YY_STORAGE_ROOT>/<user_id>/<pool>/`` (the same directories the
  usage endpoint walks, so export content and usage numbers reconcile).
- ``jobs/``: the caller's render job records (render_jobs rows) as one
  JSON file per job - jobs live in the DB, not on disk.
- ``manifest.json`` + ``checksums.sha256``: backups-service packaging
  pattern (manifest first, sha256 line per member, checksum file
  excludes itself).

Size caps reuse the export_guard domain (patch-C section 11.6):
56MB single file -> EXPORT_TOO_LARGE, 500MB ZIP -> ZIP_TOO_LARGE,
atomic write via ``.part`` + ``os.replace`` so no half-written zip is
ever left behind.
"""

import hashlib
import json
import os
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import ApiError
from app.domains.export_guard import service as guard
from app.domains.usage.service import storage_root

MANIFEST_SCHEMA_VERSION = "0.3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _user_base(user_id: int) -> Path:
    return Path(storage_root()) / str(user_id)


def _collect_pool_members(user_id: int) -> list:
    """[(absolute_path, arcname, size)] for the caller's assets/outputs pools."""
    base = _user_base(user_id)
    members = []
    for pool in ("assets", "outputs"):
        pool_dir = base / pool
        if not pool_dir.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(pool_dir):
            for name in sorted(filenames):
                fp = Path(dirpath) / name
                rel = fp.relative_to(base)
                members.append((fp, pool + "/" + rel.relative_to(pool).as_posix(), fp.stat().st_size))
    return members


def _collect_job_records(session: Session, user_id: int) -> list:
    """The caller's render job rows as JSON-able dicts (jobs/ section)."""
    from app.domains.render.models import RenderJob

    rows = session.query(RenderJob).filter(RenderJob.user_id == user_id).order_by(RenderJob.created_at).all()
    return [
        {
            "job_id": r.job_id,
            "status": r.status,
            "draft": bool(r.draft),
            "base_asset_id": r.base_asset_id,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_user_export_zip(session: Session, user_id: int, dest_dir: Path) -> tuple:
    """Build the caller's export ZIP at ``dest_dir``; returns (zip_path, summary).

    Caps and atomic-write semantics follow export_guard (patch-C section 11.6).
    Raises ApiError(413, EXPORT_TOO_LARGE / ZIP_TOO_LARGE) without leaving a
    partial archive; the caller's pools are never written to.
    """
    export_id = uuid.uuid4().hex
    pool_members = _collect_pool_members(user_id)
    jobs = _collect_job_records(session, user_id)

    for _fp, arcname, size in pool_members:
        # patch-C section 11.6: single-file cap (EXPORT_TOO_LARGE).
        try:
            guard.check_single_export(size)
        except ApiError as exc:
            raise ApiError(exc.status, exc.code, exc.title, f"{arcname}: {exc.detail}", exc.retryable) from exc

    # _zip_cap is export_guard's env-aware cap resolver (YY_ZIP_CAP for tests);
    # kept as the single source of cap semantics (patch-C section 11.6).
    zip_cap = guard._zip_cap()  # noqa: SLF001 - same-app reuse of guard constants
    planned = sum(size for _fp, _arcname, size in pool_members)
    if planned > zip_cap:
        raise ApiError(413, "ZIP_TOO_LARGE", "ZIP too large", f"planned zip payload is {planned} bytes, cap is {zip_cap}")

    pool_summary = {}
    for pool in ("assets", "outputs"):
        pool_summary[pool] = {
            "file_count": sum(1 for _fp, arc, _s in pool_members if arc.startswith(pool + "/")),
            "bytes": sum(s for _fp, arc, s in pool_members if arc.startswith(pool + "/")),
        }

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "export_id": export_id,
        "kind": "user_export",
        "user_id": user_id,
        "generated_at": _now(),
        "includes": ["assets/", "jobs/", "outputs/", "manifest.json", "checksums.sha256"],
        "pools": pool_summary,
        "jobs_count": len(jobs),
        "note": "user-scoped export (v0.2.1 section 7 GWT-S6-export); separate from admin global backup",
    }

    # checksums.sha256 covers every member except itself (backups pattern).
    checksum_lines = []

    def _add_line(digest: str, rel: str) -> None:
        checksum_lines.append(digest + "  " + rel)

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"user-export-{export_id}.zip"
    tmp = dest.with_name(dest.name + ".part")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            zf.writestr("manifest.json", manifest_bytes)
            _add_line(_bytes_sha256(manifest_bytes), "manifest.json")
            written = 0
            for fp, arcname, _size in pool_members:
                zf.write(fp, arcname)
                _add_line(_file_sha256(fp), arcname)
                written += fp.stat().st_size
                if written > zip_cap:
                    raise ApiError(413, "ZIP_TOO_LARGE", "ZIP too large", f"zip payload exceeded {zip_cap} bytes while packing")
            for job in jobs:
                data = json.dumps(job, ensure_ascii=False, indent=2).encode("utf-8")
                arcname = f"jobs/{job['job_id']}.json"
                zf.writestr(arcname, data)
                _add_line(_bytes_sha256(data), arcname)
            zf.writestr("checksums.sha256", chr(10).join(checksum_lines) + chr(10))
        os.replace(tmp, dest)
    except BaseException:
        if tmp.exists():
            os.remove(tmp)
        raise

    total_bytes = planned + sum(len(json.dumps(j).encode("utf-8")) for j in jobs)
    summary = {
        "export_id": export_id,
        "file_count": len(pool_members) + len(jobs),
        "total_bytes": total_bytes,
        "jobs_count": len(jobs),
    }
    audit(session, action="user_export", actor=str(user_id), object_ref=export_id,
          result=f"{summary['file_count']} files, {total_bytes} bytes")
    session.commit()
    return dest, summary
