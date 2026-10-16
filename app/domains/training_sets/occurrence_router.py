"""Authenticated occurrence selection, separate from frozen native review routes."""
from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from app.core.db import get_session
from app.core.envelope import ok
from app.core.errors import ApiError
from app.core.identity import get_current_user_id
from engine.style.dataset.schema import DatasetError
from .occurrence_selection import occurrence_inventory, select_occurrences

router = APIRouter()


@router.get("/api/v1/styles/{style_id}/occurrence-selection")
def get_occurrences(style_id: int, user_id: int = Depends(get_current_user_id), session=Depends(get_session)):
    try:
        return ok(occurrence_inventory(session, user_id, style_id))
    except (DatasetError, OSError, ValueError, TypeError, KeyError) as exc:
        raise ApiError(409, "OCCURRENCE_SELECTION_BLOCKED", str(exc)) from exc


@router.post("/api/v1/styles/{style_id}/occurrence-selection")
async def post_occurrences(style_id: int, request: Request, user_id: int = Depends(get_current_user_id),
                           session=Depends(get_session)):
    try:
        payload = await request.json()
    except (ValueError, TypeError) as exc:
        raise ApiError(400, "INVALID_REQUEST", "JSON occurrence selection required") from exc
    try:
        return ok(await run_in_threadpool(select_occurrences, session, user_id, style_id, payload))
    except (DatasetError, OSError, ValueError, TypeError, KeyError) as exc:
        raise ApiError(409, "OCCURRENCE_SELECTION_BLOCKED", str(exc)) from exc
