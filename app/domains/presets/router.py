"""CC-14 blogger name preset HTTP endpoints (contract: contract-v0.3-patch-C section 7)."""

from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.envelope import ok
from app.core.identity import get_current_user_id
from app.domains.presets import service

router = APIRouter(prefix="/api/v1/blogger-name-presets", tags=["blogger-name-presets"])


class CreatePresetBody(BaseModel):
    name: str
    is_default: bool = False


class UpdatePresetBody(BaseModel):
    name: Optional[str] = None
    is_default: Optional[bool] = None


@router.get("")
def list_presets(request: Request, session: Session = Depends(get_session)):
    user_id = get_current_user_id(request, session)
    return ok({"items": service.list_presets(session, user_id)})


@router.post("", status_code=201)
def create_preset(body: CreatePresetBody, request: Request, session: Session = Depends(get_session)):
    user_id = get_current_user_id(request, session)
    return ok(service.create_preset(session, user_id, body.name, body.is_default))


@router.patch("/{preset_id}")
def update_preset(preset_id: str, body: UpdatePresetBody, request: Request, session: Session = Depends(get_session)):
    user_id = get_current_user_id(request, session)
    return ok(service.update_preset(session, user_id, preset_id, name=body.name, is_default=body.is_default))


@router.delete("/{preset_id}")
def delete_preset(preset_id: str, request: Request, session: Session = Depends(get_session)):
    user_id = get_current_user_id(request, session)
    service.delete_preset(session, user_id, preset_id)
    return ok({"deleted": True})
