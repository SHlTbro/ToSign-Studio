"""Style domain endpoints (contract v0.2 section 1.4/1.8, patch-B CC-2/CC-3,
patch-C section 9 CC-16; S06/S07/S10 batch by B-DATA-STYLES 2026-09-27).

POST   /api/v1/styles                                    create -> draft
GET    /api/v1/styles                                    list (status/source_type/enabled filters; meta.total)
GET    /api/v1/styles/{style_id}                         detail
POST   /api/v1/styles/{style_id}/approve                 draft|pending_review -> approved
POST   /api/v1/styles/{style_id}/reject                  -> rejected (reason required)
POST   /api/v1/styles/{style_id}/enable                  enabled -> True (batch B1-03, D-T69-2 closed)
POST   /api/v1/styles/{style_id}/disable                 enabled -> False (batch B1-03, D-T69-2 closed)
POST   /api/v1/styles/{style_id}/samples                 register sample metadata
GET    /api/v1/styles/{style_id}/samples                 sample list
GET    /api/v1/styles/{style_id}/artifact                S11 handover: approved profile -> verified artifact
POST   /api/v1/style-runs                                create run (sample_ids locked) -> created
GET    /api/v1/style-runs/{run_id}                       run status
GET    /api/v1/style-runs/{run_id}/samples               S07: locked sample set (paged snapshot)
GET    /api/v1/style-runs/{run_id}/progress              S07: worker-heartbeat polling (no fake values)
GET    /api/v1/style-runs/{run_id}/metrics               S07: pending/unavailable/receipt-bound metrics
GET    /api/v1/style-runs/{run_id}/evaluate              S07: evaluation report reader (S08 receipt)
POST   /api/v1/style-runs/{run_id}/start                 created -> queued
POST   /api/v1/style-runs/{run_id}/cancel                queued|running -> cancelled
PUT    /api/v1/style-runs/{run_id}                       config writable in created only
DELETE /api/v1/style-runs/{run_id}/samples/{sample_id}   removable in created|queued (204)
POST   /api/v1/style-runs/{run_id}/blind-review          record blind review -> {review_id}
POST   /api/v1/style-runs/{run_id}/publish               S10: succeeded run + verified artifact -> pending_review profile
POST   /api/v1/style-runs/{run_id}/rollback              S10: flip the active artifact pointer (files untouched)

The former "deliberately absent: metrics/progress/evaluate" note (T69
discrepancy) is closed by this batch; the read faces point at real worker
data and answer explicit pending/unavailable structures instead of
fabricated numbers (see service docstrings + styles-api-gap-list.md).
"""

import json

from fastapi import APIRouter, Body, Depends, Query, Request, Response

from app.core.db import get_session
from app.core.envelope import ok
from app.core.errors import ApiError
from app.core.identity import get_current_user_id
from app.domains.styles import service
from app.domains.styles import unpublished_models

router = APIRouter()


def _parse_enabled(raw):
    """Parse the enabled filter (v0.2 line 66) into 0/1/None."""
    if raw is None:
        return None
    if raw in ("true", "1"):
        return 1
    if raw in ("false", "0"):
        return 0
    raise ApiError(400, "INVALID_REQUEST", "enabled must be true/false")


@router.post("/api/v1/styles", status_code=201)
def create_style(
    payload: dict = Body(default={}),
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    row = service.create_style(
        session, user_id, payload.get("source_type"), payload.get("name"), font_url=payload.get("font_url")
    )
    return ok(service.profile_dict(row))


@router.get("/api/v1/styles")
def list_styles(
    status: str | None = None,
    source_type: str | None = None,
    enabled: str | None = None,
    page: int = 1,
    page_size: int = 20,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    result = service.list_styles(
        session,
        user_id,
        status=status,
        source_type=source_type,
        enabled=_parse_enabled(enabled),
        page=page,
        page_size=page_size,
    )
    body = ok({"items": result["items"]})
    # meta.total is the pending-review notification carrier (patch-C 9.5).
    body["meta"].update(
        {"total": result["total"], "page": result["page"], "page_size": result["page_size"]}
    )
    return body


@router.get("/api/v1/styles/unpublished-models")
def list_unpublished_models(
    response: Response,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user_id: int = Depends(unpublished_models.session_account),
    session=Depends(get_session),
):
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['Vary'] = 'Cookie'
    return ok(unpublished_models.list_models(session, user_id, page=page, page_size=page_size))


@router.get("/api/v1/styles/{style_id}")
def get_style(
    style_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    return ok(service.profile_dict(service.get_style(session, user_id, style_id)))


@router.post("/api/v1/styles/{style_id}/approve")
def approve_style(
    style_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    return ok(service.profile_dict(service.approve_style(session, user_id, style_id)))


@router.post("/api/v1/styles/{style_id}/reject")
def reject_style(
    style_id: int,
    payload: dict = Body(default={}),
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    row = service.reject_style(session, user_id, style_id, payload.get("reason"))
    return ok(service.profile_dict(row))


@router.post("/api/v1/styles/{style_id}/enable")
def enable_style(
    style_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    """Owner-scoped enable (v0.2 line 69; batch B1-03)."""
    return ok(service.profile_dict(service.enable_style(session, user_id, style_id)))


@router.post("/api/v1/styles/{style_id}/disable")
def disable_style(
    style_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    """Owner-scoped disable (v0.2 line 68; batch B1-03)."""
    return ok(service.profile_dict(service.disable_style(session, user_id, style_id)))


@router.post("/api/v1/styles/{style_id}/samples", status_code=201)
async def register_sample(
    style_id: int,
    request: Request,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    try:
        payload = await request.json()
    except Exception:
        raise ApiError(400, "INVALID_REQUEST", "request body must be valid JSON")
    if not isinstance(payload, dict):
        raise ApiError(400, "INVALID_REQUEST", "request body must be a JSON object")
    if payload.get("touchup_confirmed") is not True:
        raise ApiError(
            400,
            "TOUCHUP_REQUIRED",
            "sample registration requires touchup_confirmed=true",
        )
    row = service.register_sample(
        session,
        user_id,
        style_id,
        source_training_image_id=payload.get("source_training_image_id"),
        text_label=payload.get("text_label"),
        relative_path=payload.get("relative_path"),
        sha256=payload.get("sha256"),
        touchup_confirmed=payload.get("touchup_confirmed"),
        touchup_edit_count=payload.get("touchup_edit_count"),
        source_asset_id=payload.get("source_asset_id"),
        source_bbox_norm=payload.get("source_bbox_norm"),
        draft_alpha_path=payload.get("draft_alpha_path"),
    )
    return ok(service.sample_dict(row))


@router.get("/api/v1/styles/{style_id}/samples")
def list_samples(
    style_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    return ok({"items": service.list_samples(session, user_id, style_id)})


@router.post("/api/v1/style-runs", status_code=201)
def create_run(
    request: Request,
    payload: dict = Body(default={}),
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    row = service.create_run(
        session, user_id, payload.get("sample_ids"), payload.get("config"), idempotency_key=request.headers.get("Idempotency-Key")
    )
    return ok(service.run_dict(row))


@router.get("/api/v1/style-runs/{run_id}")
def get_run(
    run_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    return ok(service.run_dict(service.get_run(session, user_id, run_id)))


@router.post("/api/v1/style-runs/{run_id}/start")
def start_run(
    run_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    return ok(service.run_dict(service.start_run(session, user_id, run_id)))


@router.post("/api/v1/style-runs/{run_id}/cancel")
def cancel_run(
    run_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    return ok(service.run_dict(service.cancel_run(session, user_id, run_id)))


@router.put("/api/v1/style-runs/{run_id}")
def update_run(
    run_id: int,
    payload: dict = Body(default={}),
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    row = service.update_run_config(session, user_id, run_id, payload.get("config"))
    return ok(service.run_dict(row))


@router.delete("/api/v1/style-runs/{run_id}/samples/{sample_id}", status_code=204)
def delete_run_sample(
    run_id: int,
    sample_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    service.delete_run_sample(session, user_id, run_id, sample_id)
    return Response(status_code=204)


@router.post("/api/v1/style-runs/{run_id}/blind-review", status_code=201)
def submit_blind_review(
    run_id: int,
    payload: dict = Body(default={}),
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    row = service.submit_blind_review(
        session, user_id, run_id, payload.get("items"), payload.get("verdict")
    )
    return ok({"review_id": row.review_id})


@router.post("/api/v1/style-runs/{run_id}/publish")
def publish_run(
    run_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    artifact, profile = service.publish_run(session, user_id, run_id)
    body = ok(service.profile_dict(profile))
    body["data"]["artifact"] = service.artifact_dict(artifact)
    return body

@router.get('/api/v1/style-runs/{run_id}/owner-visual-acceptance')
def owner_visual_context(run_id:int,user_id:int=Depends(get_current_user_id),session=Depends(get_session)):
    from .owner_visual_api import discover
    return ok(discover(session,user_id,run_id))

@router.post('/api/v1/style-runs/{run_id}/owner-visual-acceptance')
def owner_visual_accept(run_id:int,request:Request,payload:dict=Body(default={}),user_id:int=Depends(get_current_user_id),session=Depends(get_session)):
    if not request.cookies.get('session'):
        raise ApiError(401,'OWNER_SESSION_REQUIRED','Direct owner acceptance requires an authenticated login session')
    from .owner_visual_api import accept
    return ok(accept(session,user_id,run_id,payload))

@router.get('/api/v1/style-runs/{run_id}/owner-visual-preview/{index}')
def owner_visual_preview(run_id:int,index:int,candidate_id:str,user_id:int=Depends(get_current_user_id),session=Depends(get_session)):
    from .owner_visual_api import preview
    raw,pin=preview(session,user_id,run_id,index,candidate_id)
    return Response(content=raw,media_type='image/png',headers={'Cache-Control':'no-store','X-Image-SHA256':pin})


@router.post("/api/v1/style-runs/{run_id}/rollback")
def rollback_run(
    run_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    """S10 rollback: activation-pointer flip only; artifact files are never
    deleted (service docstring)."""
    artifact = service.rollback_run(session, user_id, run_id)
    return ok(service.artifact_dict(artifact))


@router.get("/api/v1/styles/{style_id}/artifact")
def get_style_artifact(
    style_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    """S11 handover: resolve an APPROVED profile to its verified artifact
    (stable ID + hash chain; render consumption binding is S11's wave)."""
    profile, artifact, verification = service.resolve_style_artifact(session, user_id, style_id)
    body = ok(service.artifact_dict(artifact))
    body["data"]["profile"] = service.profile_dict(profile)
    body["data"]["verification"] = verification
    return body


@router.get("/api/v1/style-runs/{run_id}/samples")
def run_samples(
    run_id: int,
    page: int = 1,
    page_size: int = 20,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    """S07: the locked sample set as a fixed, paged snapshot."""
    result = service.run_samples(session, user_id, run_id, page=page, page_size=page_size)
    body = ok({"items": result["items"]})
    body["meta"].update(
        {"total": result["total"], "page": result["page"], "page_size": result["page_size"]}
    )
    return body


@router.get("/api/v1/style-runs/{run_id}/progress")
def run_progress(
    run_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    """S07: worker-heartbeat polling payload (no timer-fabricated values)."""
    return ok(service.run_progress(session, user_id, run_id))


@router.get('/api/v1/style-runs/{run_id}/recovery')
def run_recovery(run_id: int, user_id: int = Depends(get_current_user_id), session=Depends(get_session)):
    from .run_recovery import recovery_status
    return ok(recovery_status(session, user_id, run_id))


@router.post('/api/v1/style-runs/{run_id}/retry', status_code=201)
def run_retry(run_id: int, request: Request, payload: dict = Body(default={}),
              user_id: int = Depends(get_current_user_id), session=Depends(get_session)):
    if payload:
        raise ApiError(400, 'INVALID_REQUEST', 'Retry accepts no client config, paths or evidence')
    from .run_recovery import continue_run
    return ok(service.run_dict(continue_run(session, user_id, run_id, mode='retry',
        request_key=request.headers.get('Idempotency-Key'))))


@router.post('/api/v1/style-runs/{run_id}/resume', status_code=201)
def run_resume(run_id: int, request: Request, payload: dict = Body(default={}),
               user_id: int = Depends(get_current_user_id), session=Depends(get_session)):
    if set(payload) != {'checkpoint_id'} or type(payload['checkpoint_id']) is not int:
        raise ApiError(400, 'INVALID_REQUEST', 'Resume accepts only a server checkpoint_id')
    from .run_recovery import continue_run
    return ok(service.run_dict(continue_run(session, user_id, run_id, mode='resume',
        request_key=request.headers.get('Idempotency-Key'), checkpoint_id=payload['checkpoint_id'])))


@router.get("/api/v1/style-runs/{run_id}/metrics")
def run_metrics(
    run_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    """S07: metrics; pending/unavailable without results, receipt-bound with."""
    return ok(service.run_metrics(session, user_id, run_id))


@router.get("/api/v1/style-runs/{run_id}/evaluate")
def run_evaluate(
    run_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    """S07: evaluation report reader (GET - triggering evaluation is S08)."""
    return ok(service.run_evaluate(session, user_id, run_id))


@router.get("/api/v1/style-runs/{run_id}/readiness")
def run_readiness(run_id: int, user_id: int = Depends(get_current_user_id), session=Depends(get_session)):
    return ok(service.run_readiness(session, user_id, run_id))


@router.get("/api/v1/styles/{style_id}/m2-runtime")
def m2_runtime(style_id: int, runtime_registration_id: str | None = None, recipe_id: str | None = None, user_id: int = Depends(get_current_user_id), session=Depends(get_session)):
    from .m2_runtime_api import discover
    return ok(discover(session,user_id,style_id,runtime_registration_id,recipe_id=recipe_id))


@router.post("/api/v1/style-runs/{run_id}/evaluate")
def start_evaluation(run_id: int, payload: dict = Body(default={}), user_id: int = Depends(get_current_user_id), session=Depends(get_session)):
    if payload:raise ApiError(400,"INVALID_REQUEST","Evaluation accepts no client evidence or paths")
    run=service.get_run(session,user_id,run_id)
    if run.status!="succeeded":raise ApiError(409,"RUN_NOT_EVALUABLE","Training must succeed before evaluation")
    manifest=service._load_verified_manifest(run)
    from .m2_jobs import start_evaluation as start
    result=start(user_id,run_id,json.loads(run.config or "{}"),manifest)
    if result.get('prior_binding_recovered'):
        from app.core.audit import audit
        audit(session,action='style_evaluation_prior_binding_recovered',actor=str(user_id),object_ref=f'style_runs/{run_id}',result='Exact owned prior request has no process, controller or execution lease; old terminal attempt preserved; independent deployment selected')
        session.flush()
    return ok(result)


@router.post('/api/v1/style-runs/{run_id}/inference-deployment')
def register_inference_deployment(run_id:int,payload:dict=Body(...),user_id:int=Depends(get_current_user_id),session=Depends(get_session)):
    if set(payload)!={'runtime_registration_id'}:raise ApiError(400,'INVALID_REQUEST','Only current runtime_registration_id is accepted')
    return ok(service.register_inference_deployment(session,user_id,run_id,payload['runtime_registration_id']))


@router.get('/api/v1/style-runs/{run_id}/inference-deployment')
def inference_deployment_status(run_id:int,user_id:int=Depends(get_current_user_id),session=Depends(get_session)):
    return ok(service.inference_deployment_status(session,user_id,run_id))


@router.post("/api/v1/style-runs/{run_id}/ai-review")
def start_ai_review(run_id: int, payload: dict = Body(default={}), user_id: int = Depends(get_current_user_id), session=Depends(get_session)):
    if payload:raise ApiError(400,"INVALID_REQUEST","AI review accepts no client-posted scores or statuses")
    run=service.get_run(session,user_id,run_id)
    if run.status!="succeeded":raise ApiError(409,"RUN_NOT_EVALUABLE","Training must succeed first")
    from .m2_jobs import start_ai_review as start
    return ok(start(user_id,run_id,json.loads(run.config or "{}")))


@router.post("/api/v1/styles/{style_id}/scr-preparations", status_code=202)
def start_scr_preparation(style_id: int, payload: dict = Body(default={}),
                          user_id: int = Depends(get_current_user_id), session=Depends(get_session)):
    from .user_scr_jobs import start_preparation
    from .user_scr_product import UserSCRProductError
    try:return ok(start_preparation(session,user_id,style_id,payload))
    except UserSCRProductError as exc:raise ApiError(404 if exc.code=='STYLE_NOT_FOUND' else 409,exc.code,str(exc)) from exc


@router.get("/api/v1/styles/{style_id}/scr-preparations/{job_id}")
def get_scr_preparation(style_id: int, job_id: str,
                        user_id: int = Depends(get_current_user_id), session=Depends(get_session)):
    from .user_scr_jobs import get_preparation
    from .user_scr_product import UserSCRProductError
    try:return ok(get_preparation(session,user_id,style_id,job_id))
    except UserSCRProductError as exc:raise ApiError(404 if exc.code=='STYLE_NOT_FOUND' else 409,exc.code,str(exc)) from exc


@router.post('/api/v1/style-runs/{run_id}/owner-output-packages')
async def owner_output_import(run_id:int,request:Request,user_id:int=Depends(get_current_user_id),session=Depends(get_session)):
    if not request.cookies.get('session'):raise ApiError(401,'OWNER_SESSION_REQUIRED','Authenticated login session required')
    from engine.style.visual_review.output_evidence import CAP
    if request.headers.get('Content-Type','').split(';')[0]!='application/zip':raise ApiError(400,'INVALID_REQUEST','Raw evidence ZIP required')
    parts=[];size=0
    async for chunk in request.stream():
        size+=len(chunk)
        if size>CAP:raise ApiError(413,'UPLOAD_TOO_LARGE','Evidence ZIP exceeds16MiB')
        parts.append(chunk)
    from .owner_visual_api import output_import
    return ok(output_import(session,user_id,run_id,b''.join(parts),request.headers.get('X-Evidence-SHA256')))

@router.get('/api/v1/style-runs/{run_id}/owner-output-targets')
def owner_output_targets(run_id:int,user_id:int=Depends(get_current_user_id),session=Depends(get_session)):
    from .owner_visual_api import output_targets
    return ok(output_targets(session,user_id,run_id))

@router.get('/api/v1/style-runs/{run_id}/owner-output-evidence/{item_id}')
def owner_output_context(run_id:int,item_id:str,package_id:str,user_id:int=Depends(get_current_user_id),session=Depends(get_session)):
    from .owner_visual_api import output_discover
    return ok(output_discover(session,user_id,run_id,package_id,item_id))

@router.post('/api/v1/style-runs/{run_id}/owner-output-evidence/{item_id}')
def owner_output_accept(run_id:int,item_id:str,package_id:str,request:Request,payload:dict=Body(default={}),user_id:int=Depends(get_current_user_id),session=Depends(get_session)):
    if not request.cookies.get('session'):raise ApiError(401,'OWNER_SESSION_REQUIRED','Authenticated login session required')
    from .owner_visual_api import output_accept
    return ok(output_accept(session,user_id,run_id,package_id,item_id,payload))

@router.get('/api/v1/style-runs/{run_id}/owner-output-preview/{item_id}/{kind}')
def owner_output_preview(run_id:int,item_id:str,kind:str,package_id:str,candidate_id:str,user_id:int=Depends(get_current_user_id),session=Depends(get_session)):
    from .owner_visual_api import output_preview
    try:raw,pin=output_preview(session,user_id,run_id,package_id,item_id,candidate_id,kind)
    except (OSError,ValueError,KeyError,TypeError,StopIteration) as exc:raise ApiError(409,'OWNER_OUTPUT_PREVIEW_STALE',str(exc)) from exc
    return Response(content=raw,media_type='image/png',headers={'Cache-Control':'no-store','X-Image-SHA256':pin})
