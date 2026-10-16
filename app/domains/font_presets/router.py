"""Font-preset domain endpoints (patch-C section 4, CC-11).

``/api/v1/font-downloads/{token}`` is intentionally exempt from the
X-User-Id dependency: the HMAC ticket is the credential, because
``@font-face`` fetches cannot carry custom headers (D-T65-3).
"""

from fastapi import APIRouter, Depends, Response

from app.core.db import get_session
from app.core.envelope import ok
from app.core.identity import get_current_user_id
from app.domains.font_presets import service
from app.domains.styles import service as styles_service

router = APIRouter()


@router.get("/api/v1/style-presets")
def list_style_presets(
    user_id: int = Depends(get_current_user_id), session=Depends(get_session)
):
    catalog = service.list_catalog(session, user_id)
    response = ok(catalog)
    if catalog["catalog"]["status"] != "ready":
        response["warnings"] = [{"code": "PRESET_CATALOG_UNAVAILABLE", "message": catalog["catalog"]["message"]}]
    return response


@router.post("/api/v1/style-presets/{preset_id}/activate", status_code=201)
def activate_style_preset(
    preset_id: str,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    profile = service.activate_preset(session, user_id, preset_id)
    return ok(styles_service.profile_dict(profile))


@router.get("/api/v1/style-presets/{preset_id}/download")
def issue_style_preset_download(
    preset_id: str, user_id: int = Depends(get_current_user_id)
):
    return ok(service.issue_download(preset_id))


@router.get("/api/v1/font-downloads/{token}")
def download_font(token: str):
    data = service.resolve_download(token)
    return Response(content=data, media_type="font/ttf")
