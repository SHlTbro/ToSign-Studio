"""CC-15 phrase template HTTP endpoints (contract: v0.2 section 1.3 + patch-C section 8).

Six endpoints mounted at /api/v1/phrase-templates. The deprecated alias
/api/v1/blessing-templates (patch-C section 8) was removed in v0.4 (patch-E E-6.5,
Owner ruling; executed by batch B3-04): requests to the old path now return 404.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.envelope import ok
from app.core.identity import get_current_user_id
from app.domains.phrases import service

NEW_PREFIX = "/api/v1/phrase-templates"

router = APIRouter(tags=["phrase-templates"])


class CreateTemplateBody(BaseModel):
    text: str
    enabled: bool = True


class UpdateTemplateBody(BaseModel):
    text: Optional[str] = None
    enabled: Optional[bool] = None


def list_templates(
    request: Request,
    enabled: Optional[bool] = Query(None),
    session: Session = Depends(get_session),
):
    user_id = get_current_user_id(request, session)
    return ok({"items": service.list_templates(session, enabled)})


def create_template(
    body: CreateTemplateBody,
    request: Request,
    session: Session = Depends(get_session),
):
    user_id = get_current_user_id(request, session)
    return ok(service.create_template(session, user_id, body.text, body.enabled))


def get_template(template_id: str, request: Request, session: Session = Depends(get_session)):
    user_id = get_current_user_id(request, session)
    return ok(service.get_template(session, template_id))


def update_template(
    template_id: str,
    body: UpdateTemplateBody,
    request: Request,
    session: Session = Depends(get_session),
):
    user_id = get_current_user_id(request, session)
    return ok(service.update_template(session, template_id, text=body.text, enabled=body.enabled))


def disable_template(template_id: str, request: Request, session: Session = Depends(get_session)):
    user_id = get_current_user_id(request, session)
    return ok(service.set_enabled(session, template_id, False))


def enable_template(template_id: str, request: Request, session: Session = Depends(get_session)):
    user_id = get_current_user_id(request, session)
    return ok(service.set_enabled(session, template_id, True))


for _prefix in (NEW_PREFIX,):
    router.add_api_route(_prefix, list_templates, methods=["GET"])
    router.add_api_route(_prefix, create_template, methods=["POST"], status_code=201)
    router.add_api_route(_prefix + "/{template_id}", get_template, methods=["GET"])
    router.add_api_route(_prefix + "/{template_id}", update_template, methods=["PUT"])
    router.add_api_route(_prefix + "/{template_id}/disable", disable_template, methods=["POST"])
    router.add_api_route(_prefix + "/{template_id}/enable", enable_template, methods=["POST"])
