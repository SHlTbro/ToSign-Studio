"""Training-image set endpoints (patch-C section 3, CC-10).

GET    /api/v1/training-sets                  paginated list, caller-scoped
POST   /api/v1/training-sets/images           multipart single/batch upload
DELETE /api/v1/training-sets/images/{id}      delete one image
POST   /api/v1/training-samples               register one confirmed character
                                              sample (T2, batch B4-01; spec
                                              t44-r1-extraction-spec-v2 §3 v3)
"""

from fastapi import APIRouter, Depends, File, Request, UploadFile
import os

from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.core.db import get_session
from app.core.envelope import ok
from app.core.errors import ApiError
from app.core.identity import get_current_user_id
from app.domains.training_sets import service
from app.domains.training_sets.models import TrainingImage
from app.domains.usage.service import storage_root

router = APIRouter()


@router.get("/api/v1/training-sets")
def list_training_sets(
    page: int = 1,
    page_size: int = 20,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    return ok(service.list_images(session, user_id, page, page_size))



@router.post("/api/v1/training-sets/images")
async def upload_training_images(
    files: list[UploadFile] = File(...),
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    payloads = [(f.filename, await f.read()) for f in files]
    rows = service.store_batch(session, user_id, payloads)
    return ok({"items": [service.image_dict(row) for row in rows]})



@router.delete("/api/v1/training-sets/images/{image_id}")
def delete_training_image(
    image_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    service.delete_image(session, user_id, image_id)
    return ok({"deleted_image_id": image_id})



@router.get("/api/v1/training-sets/images/{image_id}/file")
def get_training_image_file(
    image_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    """Read back an owned training image by its persistent row id."""
    row = (
        session.query(TrainingImage)
        .filter(TrainingImage.image_id == image_id, TrainingImage.user_id == user_id)
        .first()
    )
    if row is None:
        raise ApiError(404, "TRAINING_IMAGE_NOT_FOUND", "Training image not found")
    path = os.path.join(storage_root(), row.relative_path)
    if not os.path.isfile(path):
        raise ApiError(404, "TRAINING_IMAGE_NOT_FOUND", "Training image file not found")
    media_type = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    return FileResponse(path, media_type=media_type)



@router.post("/api/v1/training-samples")
async def register_training_sample(
    request: Request,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    """Register one touchup-confirmed character sample (T2, B4-01).

    Raw-body JSON so a missing/bool-typed ``touchup_confirmed`` reaches the
    service and answers 400 TOUCHUP_REQUIRED via the frozen RFC 9457
    problem+json handler, instead of a 422 from pydantic body validation.
    Success is HTTP 201 (new resource created) carrying the 0.4 response
    envelope, per B4-01 dispatch GWT.

    The authenticated caller id is handed to the service (P09-BE, E10
    closure): glyph ownership and field validation are enforced against the
    actual user, not dropped on the floor.
    """
    try:
        payload = await request.json()
    except Exception:
        raise ApiError(400, "INVALID_REQUEST", "request body must be valid JSON")
    data = service.register_sample(session, payload, user_id)
    return JSONResponse(status_code=201, content=ok(data))



@router.get("/api/v1/training-samples")
def list_training_samples(
    style_id: int,
    review_status: str | None = None,
    page: int = 1,
    page_size: int = 100,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    return ok(service.list_personal_style_samples(
        session, user_id, style_id, review_status=review_status,
        page=page, page_size=page_size,
    ))



@router.post("/api/v1/training-samples/batch-review")
async def review_training_samples(
    request: Request,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    """One explicit Owner action; service writes an individual timestamp/audit per row."""
    try:
        payload = await request.json()
    except Exception:
        raise ApiError(400, "INVALID_REQUEST", "request body must be valid JSON")
    return ok(service.review_personal_style_samples(session, user_id, payload))



@router.post("/api/v1/training-samples/ai-confirm")
async def ai_confirm_training_samples(
    request: Request,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    """Fresh pixel/context review under the authenticated user's bounded delegation."""
    try:
        payload = await request.json()
    except Exception:
        raise ApiError(400, "INVALID_REQUEST", "request body must be valid JSON")
    return ok(await run_in_threadpool(service.ai_confirm_personal_style_samples, session, user_id, payload))


@router.get("/api/v1/training-samples/snapshot-preflight")
async def read_training_snapshot_preflight(
    style_id: int,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    return ok(await run_in_threadpool(service.snapshot_preflight, session, user_id, style_id))



@router.post("/api/v1/training-samples/prepare-snapshot")
async def prepare_training_snapshot(
    request: Request,
    user_id: int = Depends(get_current_user_id),
    session=Depends(get_session),
):
    """Prepare a fresh audited S03 snapshot without launching training."""
    try:
        payload = await request.json()
    except Exception:
        raise ApiError(400, "INVALID_REQUEST", "request body must be valid JSON")
    return ok(await run_in_threadpool(service.prepare_personal_style_snapshot, session, user_id, payload))



def _intake_call(function,*args):
    from engine.style.dataset.schema import DatasetError
    try:return function(*args)
    except ApiError:raise
    except (DatasetError,OSError,ValueError,KeyError,TypeError,AttributeError) as exc:
        raise ApiError(409,'INTAKE_BLOCKED','Intake evidence or source prerequisite is unavailable',str(exc)) from exc


@router.post('/api/v1/training-sources/{asset_id}/admit')
async def admit_training_source(asset_id: str, request: Request, user_id: int = Depends(get_current_user_id), session=Depends(get_session)):
    from .source_admission import admit_source
    try:payload=await request.json()
    except Exception:raise ApiError(400,'INVALID_REQUEST','JSON body required')
    return ok(await run_in_threadpool(_intake_call,admit_source,session,user_id,asset_id,payload))


@router.post('/api/v1/candidate-review-jobs',status_code=201)
async def create_native_review_job(request: Request,user_id: int = Depends(get_current_user_id),session=Depends(get_session)):
    from .candidate_review_jobs import create_job
    try:payload=await request.json()
    except Exception:raise ApiError(400,'INVALID_REQUEST','JSON body required')
    return ok(await run_in_threadpool(_intake_call,create_job,session,user_id,payload))


@router.get('/api/v1/candidate-review-jobs')
def list_native_review_jobs(style_id: int,limit: int=50,after_job_id: str | None=None,user_id: int = Depends(get_current_user_id),session=Depends(get_session)):
    from .candidate_review_jobs import list_jobs
    return ok(_intake_call(list_jobs,session,user_id,style_id,limit,after_job_id))


@router.get('/api/v1/candidate-review-jobs/{job_id}')
def read_native_review_job(job_id: str,user_id: int = Depends(get_current_user_id),session=Depends(get_session)):
    from .candidate_review_jobs import job_view
    return ok(_intake_call(job_view,session,user_id,job_id))


@router.get('/api/v1/candidate-review-jobs/{job_id}/export')
async def export_native_review_request(job_id: str, user_id: int = Depends(get_current_user_id), session=Depends(get_session)):
    from .candidate_review_export import export_request
    archive = await run_in_threadpool(_intake_call, export_request, session, user_id, job_id)
    return StreamingResponse(archive.chunks(), media_type='application/zip',
        background=BackgroundTask(archive.close), headers={
            'Content-Disposition': f'attachment; filename="candidate-review-{archive.job_id}.zip"',
            'Content-Length': str(archive.size_bytes), 'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff', 'X-Review-Job-Id': archive.job_id,
            'X-Review-Package-SHA256': archive.package_sha256,
            'X-Review-Archive-SHA256': archive.archive_sha256,
            'X-Review-Code-Version-Matches': str(archive.code_version_matches).lower(),
        })


@router.get('/api/v1/candidate-review-jobs/{job_id}/candidates/{candidate_id}/{kind}')
def read_native_review_pixels(job_id: str,candidate_id: str,kind: str,user_id: int = Depends(get_current_user_id),session=Depends(get_session)):
    from .candidate_review_jobs import get_job,job_root
    from engine.style.visual_review.pipeline import sha
    _,package=_intake_call(get_job,session,user_id,job_id)
    item=next((x for x in package['items'] if x['candidate_id']==candidate_id),None)
    if item is None or kind not in ('alpha','source-context'):raise ApiError(404,'REVIEW_IMAGE_NOT_FOUND','Review image not found')
    from app.core import storage
    root=job_root(user_id,job_id)
    storage.check_segment(candidate_id,'candidate_id')
    path=storage.ensure_within_root(root/candidate_id/(kind+'.png'),root)
    expected=item['image_sha256'] if kind=='alpha' else item['source_context_sha256']
    if not path.is_file() or sha(path)!=expected:raise ApiError(409,'REVIEW_JOB_CHANGED','Review pixels changed')
    return FileResponse(path,media_type='image/png')


@router.post('/api/v1/candidate-review-jobs/{job_id}/apply')
async def apply_native_review_job(job_id: str,request: Request,user_id: int = Depends(get_current_user_id),session=Depends(get_session)):
    from .candidate_review_jobs import apply_bundle
    try:payload=await request.json()
    except Exception:raise ApiError(400,'INVALID_REQUEST','JSON body required')
    return ok(await run_in_threadpool(_intake_call,apply_bundle,session,user_id,job_id,payload))


@router.post('/api/v1/candidate-review-jobs/{job_id}/cancel')
def cancel_native_review_job(job_id: str,user_id: int = Depends(get_current_user_id),session=Depends(get_session)):
    from .candidate_review_jobs import cancel_job
    return ok(_intake_call(cancel_job,session,user_id,job_id))


@router.get('/api/v1/candidate-review-jobs/{job_id}/review-ingress')
def read_native_review_ingress(job_id: str,user_id: int = Depends(get_current_user_id),session=Depends(get_session)):
    from .candidate_review_ingress import view
    return ok(_intake_call(view,session,user_id,job_id))


@router.post('/api/v1/candidate-review-jobs/{job_id}/review-ingress')
async def request_native_review_ingress(job_id: str,request: Request,user_id: int = Depends(get_current_user_id),session=Depends(get_session)):
    from .candidate_review_ingress import create_request
    try:payload=await request.json()
    except Exception:raise ApiError(400,'INVALID_REQUEST','JSON body required')
    return ok(await run_in_threadpool(_intake_call,create_request,session,user_id,job_id,payload))


@router.post('/api/v1/candidate-review-jobs/{job_id}/review-ingress/import')
async def import_native_review_ingress(job_id: str,request: Request,user_id: int = Depends(get_current_user_id),session=Depends(get_session)):
    from .candidate_review_ingress import import_staged
    try:payload=await request.json()
    except Exception:raise ApiError(400,'INVALID_REQUEST','JSON body required')
    return ok(await run_in_threadpool(_intake_call,import_staged,session,user_id,job_id,payload))


@router.post('/api/v1/candidate-review-jobs/{job_id}/review-ingress/cancel')
async def cancel_native_review_ingress(job_id: str,request: Request,user_id: int = Depends(get_current_user_id),session=Depends(get_session)):
    from .candidate_review_ingress import cancel_request
    try:payload=await request.json()
    except Exception:raise ApiError(400,'INVALID_REQUEST','JSON body required')
    return ok(await run_in_threadpool(_intake_call,cancel_request,session,user_id,job_id,payload))


@router.get('/api/v1/training-sources/{asset_id}/admission')
def read_source_admission(asset_id: str,style_id: int,user_id: int=Depends(get_current_user_id),session=Depends(get_session)):
    from .human_review import read_source
    return ok(read_source(session,user_id,asset_id,style_id))



@router.post('/api/v1/training-sources/{asset_id}/admission')
async def submit_source_admission(asset_id: str,request: Request,user_id: int=Depends(get_current_user_id),session=Depends(get_session)):
    from .source_admission import admit_source
    try:payload=await request.json()
    except Exception:raise ApiError(400,'INVALID_REQUEST','request body must be valid JSON')
    return ok(admit_source(session,user_id,asset_id,payload))



@router.get('/api/v1/training-samples/human-visual-review')
def read_human_visual_reviews(style_id: int,user_id: int=Depends(get_current_user_id),session=Depends(get_session)):
    from .human_review import list_reviews
    return ok(list_reviews(session,user_id,style_id))



@router.post('/api/v1/training-samples/{sample_id}/human-visual-review')
async def submit_human_visual_review(sample_id: str,request: Request,user_id: int=Depends(get_current_user_id),session=Depends(get_session)):
    from .human_review import submit_review
    try:payload=await request.json()
    except Exception:raise ApiError(400,'INVALID_REQUEST','request body must be valid JSON')
    return ok(submit_review(session,user_id,sample_id,payload))

