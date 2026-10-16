"""Layout-template domain HTTP endpoints (contract v0.2 section 1.2, six endpoints)."""

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.envelope import ok
from app.core.identity import get_current_user_id
from app.domains.templates import service
from app.domains.templates.models import LayoutTemplate

router = APIRouter(prefix="/api/v1/layout-templates", tags=["layout-templates"])


@router.post("", status_code=201)
def create_template(
    payload: dict = Body(default={}),
    session: Session = Depends(get_session),
    user_id: int = Depends(get_current_user_id),
):
    row = service.create_template(session, user_id, payload)
    return ok(service.template_dict(row))


@router.get("")
def list_templates(
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
    session: Session = Depends(get_session),
    user_id: int = Depends(get_current_user_id),
):
    result = service.list_templates(session, user_id, status=status, page=page, page_size=page_size)
    body = ok({"items": result["items"]})
    body["meta"].update({"total": result["total"], "page": result["page"], "page_size": result["page_size"]})
    return body


@router.get("/{template_id}")
def get_template(
    template_id: str,
    session: Session = Depends(get_session),
    user_id: int = Depends(get_current_user_id),
):
    row = service._get_owned(session, user_id, template_id)
    return ok(service.template_dict(row))


@router.put("/{template_id}")
def update_template(
    template_id: str,
    payload: dict = Body(default={}),
    session: Session = Depends(get_session),
    user_id: int = Depends(get_current_user_id),
):
    row = service.update_template(session, user_id, template_id, payload)
    return ok(service.template_dict(row))


@router.post("/{template_id}/approve")
def approve_template(
    template_id: str,
    session: Session = Depends(get_session),
    user_id: int = Depends(get_current_user_id),
):
    row = service.review_template(session, user_id, template_id, "approved")
    return ok(service.template_dict(row))


@router.post("/{template_id}/reject")
def reject_template(
    template_id: str,
    session: Session = Depends(get_session),
    user_id: int = Depends(get_current_user_id),
):
    row = service.review_template(session, user_id, template_id, "rejected")
    return ok(service.template_dict(row))
