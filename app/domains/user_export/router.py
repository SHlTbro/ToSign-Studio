"""POST /api/v1/users/me/export (v0.2.1 section 7 GWT-S6-export, B1-04).

The endpoint answers with the raw ZIP bytes (media_type application/zip,
Content-Disposition attachment). The FileResponse streams the archive from
disk in chunks, so the browser consumes it as a URL-streamed download; the
archive is built outside the caller's pools (system temp dir) and deleted
via the response background task once the body is sent.
"""
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.core.db import get_session
from app.core.identity import get_current_user_id

from app.domains.user_export import service

router = APIRouter()


@router.post("/api/v1/users/me/export")
def export_my_data(session=Depends(get_session), user_id: int = Depends(get_current_user_id)):
    dest_dir = Path(tempfile.gettempdir()) / "yy_user_exports"
    dest, summary = service.build_user_export_zip(session, user_id, dest_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return FileResponse(
        dest,
        media_type="application/zip",
        filename=f"yy-user-{user_id}-export-{stamp}.zip",
        background=BackgroundTask(os.remove, str(dest)),
    )
