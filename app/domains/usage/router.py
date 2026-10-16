"""GET /api/v1/usage - per-pool quota breakdown (patch-C section 11.7)."""
from fastapi import APIRouter, Depends

from app.core.envelope import ok
from app.core.identity import get_current_user_id

from app.domains.usage import service

router = APIRouter()


@router.get("/api/v1/usage")
def read_usage(user_id: int = Depends(get_current_user_id)):
    return ok(service.get_usage(str(user_id)))
