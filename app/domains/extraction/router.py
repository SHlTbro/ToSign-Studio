"""Extraction endpoints (T59-R1, batch B5-01; patch-A §3.1 + patch-D §3.1/§3.4/§8).

POST /api/v1/detect-text   candidate box locating only (patch-D §3.4;
                           baseline candidate generator, PP-OCRv5 pending
                           its own dependency change order)
POST /api/v1/extract       half-product alpha draft; EVERY response carries
                           needs_touchup=true (patch-D §3.1 - no pass-through
                           path exists anywhere in this domain)

Error codes follow the frozen patch-D §8 table, mirrored in
contracts/style-font-pipeline-v1.json ``error_codes_full_table``.
"""

import os
import re

from fastapi import APIRouter, Body, Depends
from fastapi.responses import FileResponse

from app.core import storage
from app.core.db import get_session
from app.core.envelope import ok
from app.core.errors import ApiError
from app.core.identity import get_current_user_id
from app.domains.extraction import service

router = APIRouter()


@router.get("/api/v1/extract/drafts/{filename}")
def get_draft_file(
    filename: str,
    user_id: int = Depends(get_current_user_id),
):
    """Serve only the caller's generated alpha drafts, by opaque draft name.

    D01 wiring (b2-extraction-data 2026-09-27): the path is built by the
    single contained builder ``storage.draft_path`` (segment validation +
    realpath containment) instead of a hand-rolled join - defense in depth
    behind the ``[a-f0-9]{16}.png`` regex, and one path convention shared
    with the writer."""
    if re.fullmatch(r"[a-f0-9]{16}\.png", filename) is None:
        raise ApiError(404, "ASSET_NOT_FOUND", "Draft not found")
    try:
        path = storage.draft_path(user_id, filename)
    except storage.StoragePathError:
        # Containment/segment rejection maps to the same anti-enumeration 404.
        raise ApiError(404, "ASSET_NOT_FOUND", "Draft not found")
    if not os.path.isfile(path):
        raise ApiError(404, "ASSET_NOT_FOUND", "Draft not found")
    return FileResponse(os.fspath(path), media_type="image/png")


@router.post("/api/v1/detect-text")
def detect_text(
    payload: dict = Body(default={}),
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    """Locate candidate boxes for one asset (locate-only, patch-D §3.4)."""
    return ok(service.detect_text(session, user_id, payload))


@router.post("/api/v1/extract")
def extract(
    payload: dict = Body(default={}),
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    """Half-product generator: draft alpha PNG + needs_touchup=true (patch-D §3.1)."""
    return ok(service.extract(session, user_id, payload))
