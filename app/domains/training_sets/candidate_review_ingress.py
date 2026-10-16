"""Visible review selection, with evidence authority retained by the local operator.

HTTP accepts only server identities. It never accepts scores, evidence paths or a
claim of reviewer identity. A real review must bind the server request itself.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import uuid
from sqlalchemy.exc import OperationalError
from app.core import storage
from app.core.audit import AuditLog, audit
from app.core.errors import ApiError
from engine.style.dataset.schema import canonical_json, review_digest
from engine.style.m2_adapter.trainer_resume_protocol import native_path
from .intake_models import CandidateReviewIngress
from . import candidate_review_jobs as jobs


def capabilities():
    return {'visible_request': True, 'http_raw_review_import': False,
            'trusted_import': 'operator_staged_hash_pinned_selection',
            'online_provider_status': 'NOT_CONFIGURED', 'automatic_review_dispatch': False}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _reserve(session):
    # All mutation reads follow the writer reservation. Caller commits/rolls back.
    jobs.require(session.get_bind().dialect.name == 'sqlite', 'REVIEW_INGRESS_ATOMICITY_UNAVAILABLE',
                 'Atomic review ingress requires SQLite')
    try:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
    except OperationalError as exc:
        raise ApiError(409, 'REVIEW_INGRESS_BUSY', 'Review authority is changing; refresh and retry', retryable=True) from exc
    session.expire_all()


def _row(session, user_id, job_id):
    row = session.query(CandidateReviewIngress).filter_by(job_id=job_id).first()
    if row is None:
        marker = session.query(AuditLog.id).filter_by(action='candidate_review_request',
            object_ref='candidate_review_jobs/'+job_id).first()
        jobs.require(marker is None, 'REVIEW_INGRESS_CHANGED',
                     'Previously requested review authority is missing; it cannot become a legacy job')
    jobs.require(row is None or row.user_id == user_id, 'REVIEW_INGRESS_CHANGED',
                 'Stored review request ownership differs from its job')
    return row


def _request(row, job, package):
    body = jobs.strict_json(row.request_json)
    keys = {'schema','request_id','nonce','job_id','user_id','package_sha256','job_nonce',
            'application_version','policy_sha256','created_at'}
    jobs.require(isinstance(body, dict) and set(body) == keys and
                 body['schema'] == 'native-candidate-review-ingress-request/1' and
                 body['request_id'] == row.request_id and row.user_id == job.user_id and
                 row.job_id == job.job_id and body['job_id'] == job.job_id and
                 body['user_id'] == job.user_id and body['package_sha256'] == job.package_sha256 and
                 body['job_nonce'] == package['nonce'] and body['application_version'] == package['application_version'] and
                 body['policy_sha256'] == package['policy_sha256'] and
                 isinstance(body['nonce'], str) and jobs.JOB_RE.fullmatch(body['nonce']) and
                 review_digest(body) == row.request_sha256,
                 'REVIEW_INGRESS_CHANGED', 'Review request authority changed')
    return body


def request_binding(row, request):
    return {'request_id': row.request_id, 'request_sha256': row.request_sha256, 'nonce': request['nonce']}


def _stage_path(user_id, job_id, digest):
    jobs.require(isinstance(digest, str) and jobs.HASH_RE.fullmatch(digest),
                 'REVIEW_INGRESS_CHANGED', 'Invalid staged evidence digest')
    root = jobs.job_root(user_id, job_id)
    path = root / 'staged-evidence' / (digest + '.json')
    safe = storage.ensure_within_root(path, root)
    jobs.require(safe == path, 'REVIEW_INGRESS_CHANGED', 'Staged evidence cannot alias another path')
    return safe


def _validate_review_request(bundle, row, request):
    expected = request_binding(row, request)
    original = jobs.strict_json(bundle['source_review_original_utf8'])
    jobs.require(bundle.get('ingress_request') == expected and original.get('ingress_request') == expected,
                 'REVIEW_INGRESS_BINDING_REQUIRED', 'Actual review and bundle must bind this request ID, nonce and digest')
    stamp = datetime.fromisoformat(bundle['provenance']['reviewed_at'].replace('Z', '+00:00'))
    created = datetime.fromisoformat(request['created_at'])
    jobs.require(created.tzinfo is not None and created <= stamp, 'REVIEW_INGRESS_BINDING_REQUIRED',
                 'Actual current-request review confirmation must be after the request was created')


def _stage(row, job, package, request):
    stage = jobs.strict_json(row.stage_json) if row.stage_json else None
    jobs.require(isinstance(stage, dict) and stage.get('schema') == 'native-candidate-review-operator-stage/1' and
                 stage.get('ingress_request') == request_binding(row, request) and
                 stage.get('job_id') == job.job_id and stage.get('package_sha256') == job.package_sha256 and
                 stage.get('operator_authority') == 'local_operator_independent_hash_pin',
                 'REVIEW_INGRESS_CHANGED', 'Staged authority is missing or changed')
    body = {k:v for k,v in stage.items() if k != 'stage_sha256'}
    jobs.require(stage.get('stage_sha256') == review_digest(body), 'REVIEW_INGRESS_CHANGED', 'Staged authority digest changed')
    path = _stage_path(job.user_id, job.job_id, stage.get('bundle_sha256'))
    path_io = native_path(path)
    jobs.require(path_io.is_file() and path_io.stat().st_size <= 2_000_000 and jobs.sha(path_io) == stage['bundle_sha256'],
                 'REVIEW_INGRESS_CHANGED', 'Staged evidence bytes are missing or changed')
    bundle = jobs.strict_json(path_io.read_text(encoding='utf-8'))
    reviewer = jobs._validate_bundle(bundle, job, package)
    _validate_review_request(bundle, row, request)
    jobs.require(stage.get('reviewer') == reviewer and
                 stage.get('source_review_sha256') == bundle['provenance']['source_review_sha256'] and
                 stage.get('reviewed_at') == bundle['provenance']['reviewed_at'],
                 'REVIEW_INGRESS_CHANGED', 'Staged review identity changed')
    return stage, path


def _view(session, user_id, job_id):
    job, package = jobs.get_job(session, user_id, job_id)
    row = _row(session, user_id, job_id)
    out = {'job_id': job_id, 'package_sha256': job.package_sha256, 'request': None,
           'status': 'not_requested', 'reason': None, 'stage': None,
           'capabilities': capabilities(), 'canonical_samples_created': False}
    if row is None:
        if job.status != 'pending':
            out.update(status='blocked', reason={'code':'REVIEW_JOB_REPLAY','message':'Job is no longer pending'})
        return out
    request = _request(row, job, package)
    out['request'] = dict(request_binding(row, request), created_at=request['created_at'])
    out['status'] = row.status
    if job.status == 'cancelled' or row.status == 'cancelled':
        out['status'] = 'cancelled'
        return out
    jobs.require(row.status in ('requested', 'staged', 'imported'), 'REVIEW_INGRESS_CHANGED', 'Unknown ingress state')
    jobs._validate_current(session, job, package)
    if row.status in ('staged', 'imported'):
        stage, _ = _stage(row, job, package, request)
        out['stage'] = {k:stage[k] for k in ('bundle_sha256','reviewer','source_review_sha256','reviewed_at','staged_at')}
    if row.status in ('requested','staged'):
        jobs.require(job.imported_bundle_sha256 is None, 'REVIEW_INGRESS_CHANGED', 'Evidence bypassed the visible request selection')
    if row.status == 'imported':
        jobs.require(job.imported_bundle_sha256 == out['stage']['bundle_sha256'], 'REVIEW_INGRESS_CHANGED', 'Imported authority changed')
    else:
        jobs.require(job.status == 'pending', 'REVIEW_JOB_REPLAY', 'Review job is no longer pending')
    return out


def view(session, user_id, job_id):
    # Ownership is checked outside diagnostic handling, so other users see no state.
    job, _ = jobs.get_job(session, user_id, job_id)
    try:
        return _view(session, user_id, job_id)
    except (ApiError, OSError, ValueError, KeyError, TypeError) as exc:
        return {'job_id': job_id, 'package_sha256': job.package_sha256, 'request': None,
                'status': 'blocked', 'stage': None,
                'reason': {'code': getattr(exc, 'code', 'REVIEW_INGRESS_CHANGED'),
                           'message': 'Current review request or evidence cannot be verified; create a new job if authority changed'},
                'capabilities': capabilities(), 'canonical_samples_created': False}


def create_request(session, user_id, job_id, payload):
    jobs.require(isinstance(payload, dict) and set(payload) == {'package_sha256'}, 'INVALID_REQUEST', 'Only package_sha256 is accepted')
    _reserve(session)
    job, package = jobs.get_job(session, user_id, job_id)
    jobs.require(payload['package_sha256'] == job.package_sha256, 'REVIEW_JOB_CHANGED', 'Review package changed')
    jobs.require(job.status == 'pending', 'REVIEW_JOB_REPLAY', 'Review job is no longer pending')
    jobs._validate_current(session, job, package)
    row = _row(session, user_id, job_id)
    if row is not None:
        _request(row, job, package)
        jobs.require(row.status != 'cancelled', 'REVIEW_JOB_REPLAY', 'Request was cancelled; create a new job')
        return _view(session, user_id, job_id)
    jobs.require(job.imported_bundle_sha256 is None, 'REVIEW_EVIDENCE_CONFLICT', 'Job already has imported evidence')
    request_id = uuid.uuid4().hex
    body = {'schema':'native-candidate-review-ingress-request/1','request_id':request_id,
            'nonce':uuid.uuid4().hex,'job_id':job_id,'user_id':user_id,'package_sha256':job.package_sha256,
            'job_nonce':package['nonce'],'application_version':package['application_version'],
            'policy_sha256':package['policy_sha256'],'created_at':_now()}
    row = CandidateReviewIngress(request_id=request_id, job_id=job_id, user_id=user_id,
        request_json=canonical_json(body), request_sha256=review_digest(body), status='requested')
    session.add(row)
    audit(session, action='candidate_review_request', actor='authenticated_user:'+str(user_id),
          object_ref='candidate_review_jobs/'+job_id, result=request_id)
    session.flush()
    return _view(session, user_id, job_id)


def export_binding(session, user_id, job_id):
    job, package = jobs.get_job(session, user_id, job_id)
    row = _row(session, user_id, job_id)
    if row is None:
        return None
    request = _request(row, job, package)
    return {'request': request, 'request_sha256': row.request_sha256, 'status': row.status,
            'review_must_bind': request_binding(row, request), 'automatic_review_dispatch': False}


def stage_bundle(session, user_id, job_id, request_id, bundle_path, expected_sha256, expected_provider, expected_model):
    """Privileged local operator entry; deliberately absent from the HTTP router."""
    jobs.require(isinstance(expected_sha256,str) and jobs.HASH_RE.fullmatch(expected_sha256),
                 'REVIEW_EVIDENCE_INVALID','Independent full SHA256 pin required')
    path = Path(bundle_path)
    path_io = native_path(path)
    jobs.require(path_io.is_file() and path_io.stat().st_size <= 2_000_000, 'REVIEW_EVIDENCE_INVALID', 'Bundle missing or too large')
    raw = path_io.read_bytes()
    jobs.require(jobs.review_digest_bytes(raw) == expected_sha256, 'REVIEW_EVIDENCE_INVALID', 'Bundle hash differs')
    bundle = jobs.strict_json(raw.decode('utf-8'))
    _reserve(session)
    job, package = jobs.get_job(session, user_id, job_id)
    jobs.require(job.status == 'pending', 'REVIEW_JOB_REPLAY', 'Job no longer pending')
    jobs._validate_current(session, job, package)
    row = _row(session, user_id, job_id)
    jobs.require(row is not None and row.request_id == request_id and row.status in ('requested','staged'),
                 'REVIEW_INGRESS_CHANGED', 'Current pending review request required')
    request = _request(row, job, package)
    reviewer = jobs._validate_bundle(bundle, job, package)
    _validate_review_request(bundle, row, request)
    jobs.require(reviewer['provider'] == expected_provider and reviewer['model_id'] == expected_model,
                 'REVIEW_EVIDENCE_INVALID', 'Independent reviewer pin differs')
    jobs.require(job.imported_bundle_sha256 is None, 'REVIEW_EVIDENCE_CONFLICT', 'Job already imported evidence')
    if row.status == 'staged':
        stage, _ = _stage(row, job, package, request)
        jobs.require(stage['bundle_sha256'] == expected_sha256, 'REVIEW_EVIDENCE_CONFLICT', 'First staged bundle is immutable')
        return _view(session, user_id, job_id)
    target = _stage_path(user_id, job_id, expected_sha256)
    target_io = native_path(target)
    with storage.QUOTA_LOCK:
        storage.enforce_quota(user_id, len(raw))
        target_io.parent.mkdir(parents=True, exist_ok=True)
        if target_io.exists():
            jobs.require(target_io.read_bytes() == raw, 'REVIEW_EVIDENCE_CONFLICT', 'Immutable staged bytes conflict')
        else:
            fd, name = tempfile.mkstemp(prefix='.stage-', dir=target_io.parent)
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(raw); f.flush(); os.fsync(f.fileno())
                os.replace(name, target_io)
                if os.name != 'nt':
                    directory = os.open(target_io.parent, os.O_RDONLY)
                    try: os.fsync(directory)
                    finally: os.close(directory)
            finally:
                if os.path.exists(name): os.unlink(name)
    stage = {'schema':'native-candidate-review-operator-stage/1', 'ingress_request':request_binding(row,request),
             'job_id':job_id,'package_sha256':job.package_sha256,'bundle_sha256':expected_sha256,
             'reviewer':reviewer,'source_review_sha256':bundle['provenance']['source_review_sha256'],
             'reviewed_at':bundle['provenance']['reviewed_at'],'staged_at':_now(),
             'operator_authority':'local_operator_independent_hash_pin'}
    stage['stage_sha256'] = review_digest(stage)
    row.stage_json = canonical_json(stage); row.status = 'staged'
    audit(session, action='candidate_review_operator_stage', actor='operator:independent_hash_pin',
          object_ref='candidate_review_jobs/'+job_id+'/'+request_id, result=expected_sha256)
    session.flush()
    return _view(session, user_id, job_id)


def import_staged(session, user_id, job_id, payload):
    jobs.require(isinstance(payload,dict) and set(payload) == {'request_id','bundle_sha256'},
                 'INVALID_REQUEST', 'Only server request_id and staged bundle_sha256 are accepted')
    _reserve(session)
    job, package = jobs.get_job(session, user_id, job_id)
    row = _row(session, user_id, job_id)
    jobs.require(row is not None and row.request_id == payload['request_id'] and row.status in ('staged','imported'),
                 'REVIEW_INGRESS_CHANGED', 'Staged server request required')
    request = _request(row, job, package)
    jobs._validate_current(session, job, package)
    stage, path = _stage(row, job, package, request)
    jobs.require(payload['bundle_sha256'] == stage['bundle_sha256'], 'REVIEW_INGRESS_CHANGED', 'Selected evidence changed')
    if row.status == 'imported':
        return _view(session, user_id, job_id)
    jobs.require(job.status == 'pending', 'REVIEW_JOB_REPLAY', 'Job no longer pending')
    jobs._import_validated_bundle(session,user_id,job_id,path,stage['bundle_sha256'],stage['reviewer']['provider'],stage['reviewer']['model_id'])
    row.status = 'imported'
    audit(session, action='candidate_review_user_import_selection', actor='authenticated_user:'+str(user_id),
          object_ref='candidate_review_jobs/'+job_id+'/'+row.request_id, result=stage['bundle_sha256'])
    session.flush()
    return _view(session,user_id,job_id)


def cancel_request(session, user_id, job_id, payload):
    jobs.require(isinstance(payload,dict) and set(payload) == {'request_id'}, 'INVALID_REQUEST', 'Only request_id is accepted')
    _reserve(session)
    job, package = jobs.get_job(session,user_id,job_id)
    row = _row(session,user_id,job_id)
    jobs.require(row is not None and row.request_id == payload['request_id'], 'REVIEW_INGRESS_CHANGED', 'Owned request required')
    _request(row,job,package)
    if row.status == 'cancelled' and job.status == 'cancelled':
        return _view(session,user_id,job_id)
    jobs.cancel_job(session,user_id,job_id)
    row.status = 'cancelled'
    audit(session,action='candidate_review_request_cancel',actor='authenticated_user:'+str(user_id),
          object_ref='candidate_review_jobs/'+job_id+'/'+row.request_id,result='cancelled_evidence_preserved')
    session.flush()
    return _view(session,user_id,job_id)


def assert_apply_authority(session, job, package, digest):
    """Request-bound jobs cannot apply evidence imported through a legacy path."""
    row = _row(session, job.user_id, job.job_id)
    if row is None:
        return
    jobs.require(row.status == 'imported', 'REVIEW_INGRESS_SELECTION_REQUIRED',
                 'Select current operator-staged evidence through the visible import operation before applying')
    request = _request(row, job, package)
    stage, _ = _stage(row, job, package, request)
    jobs.require(stage['bundle_sha256'] == digest == job.imported_bundle_sha256,
                 'REVIEW_INGRESS_CHANGED', 'Applied evidence does not match the selected request')
