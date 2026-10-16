"""Owned attempts and full trainer anchors. Metadata only; no ML or keys.

Recovery never kills a process, assumes an unreadable process is dead, or turns
checkpoint metadata into Runtime qualification. New attempts use new directories.
"""
from __future__ import annotations
import ctypes
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from app.core.errors import ApiError
from . import runstore
from .models import StyleRun, StyleRunCheckpoint, StyleRunExecution, StyleRunContinuation
from engine.style.m2_adapter.trainer_resume_protocol import build_binding, digest, verify_manifest

APP = Path(__file__).resolve().parents[3]
SERVER_FIELD = "_server_continuation"

def now():
    return datetime.now(timezone.utc).isoformat()

def source_digest(app=APP):
    from engine.style.m2_adapter.trainer_resume_protocol import source_digest as shared_source_digest
    return shared_source_digest(app)

def checkpoint_binding(run, app=APP):
    from engine.style.m2_adapter.trainer_resume_protocol import trainer_identity
    trainer_version, trainer_code = trainer_identity(app)
    config = json.loads(run.config or '{}')
    continuation = config.pop(SERVER_FIELD, {})
    logical_id = continuation.get('logical_run_id', run.run_id)
    recipe = {k: config[k] for k in ('recipe_id', 'objective_profile', 'checkpoint_selection',
        'adapter_topology_id', 'adapter_topology', 'adapter_topology_sha256', 'preprocessing_profile') if k in config}
    artifacts = {k: config[k] for k in ('base_npz_sha256', 'runtime_identity_sha256',
        'scr_data_binding', 'scr_cache_binding', 'scr_preparation_binding', 'windows_cache_identity') if k in config}
    # Content font bytes are bound even when the configured path is external.
    font = config.get('content_font_path') or (str(Path(config['fd_repo'])/'ttf/KaiXinSongA.ttf') if config.get('fd_repo') else None)
    if font:
        artifacts['content_font_sha256'] = runstore.sha256_file(Path(font))
    artifacts['sample_ids_sha256'] = runstore.canonical_sha256(json.loads(run.sample_ids))
    return build_binding(owner_id=run.user_id, run_id=str(logical_id),
        snapshot_sha256=config.get('snapshot_sha256'), runtime_binding=config.get('runtime_binding'),
        recipe_identity=recipe, source_sha256=source_digest(app), training_budget=config.get('training_budget'),
        config_hash=digest(config), trainer_version=trainer_version,
        trainer_code_sha256=trainer_code,
        artifact_binding=artifacts)

def process_identity(pid=None):
    from engine.style.m2_adapter.personal_runtime_resources import process_start
    return {'pid': os.getpid() if pid is None else int(pid), 'birth': process_start(pid)}

def process_state(identity):
    """Exact recorded PID only; access denial remains unknown. No enumeration."""
    if not isinstance(identity, dict) or set(identity) != {'pid', 'birth'}:
        return 'unknown'
    if type(identity['pid']) is not int or identity['pid'] <= 0 or type(identity['birth']) is not int:
        return 'unknown'
    if os.name != 'nt':
        return 'unknown'
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),)*4)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, identity['pid'])
    if not handle:
        return 'dead' if ctypes.get_last_error() == 87 else 'unknown'
    try:
        exit_code = wintypes.DWORD()
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or not kernel.GetProcessTimes(handle, *(ctypes.byref(x) for x in times)):
            return 'unknown'
        birth = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return 'dead' if birth != identity['birth'] or exit_code.value != 259 else 'alive'
    finally:
        kernel.CloseHandle(handle)

def record_execution(session, run_id, *, child=None, cpu_lease=None, gpu_lease=None, phase=None):
    row = session.get(StyleRunExecution, run_id)
    body = json.loads(row.ownership) if row else {'worker': process_identity(), 'child': None,
        'cpu_lease': None, 'nonce': uuid.uuid4().hex, 'phase': 'claimed'}
    if child is not None:
        body['child'] = process_identity(child.pid)
    if cpu_lease is not None:
        body['cpu_lease'] = cpu_lease[1]
    if gpu_lease is not None:
        body['gpu_lease'] = gpu_lease[1]
    if phase is not None:
        body['phase'] = phase
    if row is None:
        row = StyleRunExecution(run_id=run_id, ownership=json.dumps(body)); session.add(row)
    else:
        row.ownership = json.dumps(body)
    session.flush()
    return body

def execution_state(session, run_id, probe=process_state):
    row = session.get(StyleRunExecution, run_id)
    if row is None:
        return 'OWNERSHIP_UNKNOWN'
    try:
        body = json.loads(row.ownership)
        if body.get('phase') == 'ended': return 'ENDED'
        worker = probe(body.get('worker'))
        child = probe(body['child']) if body.get('child') else 'dead'
        if worker == 'alive' or child == 'alive': return 'EXECUTION_ACTIVE'
        if worker == 'unknown' or child == 'unknown': return 'OWNERSHIP_UNKNOWN'
        return 'EXECUTION_DEAD'
    except (ValueError, KeyError, TypeError, OSError):
        return 'OWNERSHIP_UNKNOWN'

def archive_dead_cpu_lease(session, run, app=APP):
    """Archive only this dead attempt's exact token; preserve mismatches."""
    row = session.get(StyleRunExecution, run.run_id)
    body = json.loads(row.ownership)
    token = body.get('cpu_lease')
    if token is None: return
    if token.get('task') != f'style_run/{run.run_id}' or token.get('pid') != body['worker']['pid'] or token.get('process_start') != body['worker']['birth']:
        raise ValueError('CPU lease ownership differs')
    path = Path(app)/'runtime/windows-scr-training/cpu-execution.lock'
    if not path.exists(): return
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)(): raise ValueError('linked CPU lease')
    if runstore.read_json(path) != token: raise ValueError('CPU lease token changed; kept untouched')
    target = path.with_name(f'cpu-execution-recovered-{run.run_id}-{uuid.uuid4().hex}.json')
    path.rename(target)


def archive_dead_gpu_lease(session, run, app=APP):
    """Archive a GPU token only after both exact recorded processes are dead."""
    if execution_state(session,run.run_id)!='EXECUTION_DEAD':
        raise ValueError('GPU execution ownership is live or unresolved')
    body=json.loads(session.get(StyleRunExecution,run.run_id).ownership)
    token=body.get('gpu_lease')
    if token is None:return
    if token.get('task')!=f'style_run/{run.run_id}' or token.get('pid')!=body['worker']['pid'] or token.get('process_start')!=body['worker']['birth']:
        raise ValueError('GPU lease ownership differs')
    from engine.style.m2_adapter.windows_gpu_support import gpu_lock_path
    path=gpu_lock_path(app)
    if not path.exists():return
    if any(p.is_symlink() or p.is_junction() for p in (path,*path.parents)):
        raise ValueError('linked GPU lease')
    from engine.style.m2_adapter.windows_qualification_recovery import _locked_lease
    with _locked_lease(path) as (raw,rename):
        if json.loads(raw)!=token:raise ValueError('GPU lease token changed; preserved')
        rename(path.with_name(f'gpu-recovered-{run.run_id}-{uuid.uuid4().hex}.json'))

def register_checkpoint(session, run, event, expected_binding):
    execution = session.get(StyleRunExecution, run.run_id)
    if execution is None or json.loads(execution.ownership).get('worker') != process_identity():
        raise ValueError('checkpoint is not from this owned worker')
    base = runstore.run_dir(run.user_id, run.run_id)
    directory = Path(event['manifest_path']).absolute().parent
    if not directory.is_relative_to(base/'train_run/resume_generations'):
        raise ValueError('checkpoint outside owned immutable generations')
    checked = verify_manifest(directory, expected_binding, event.get('manifest_sha256'))
    existing = session.query(StyleRunCheckpoint).filter_by(manifest_sha256=checked['manifest_sha256']).first()
    if existing:
        if (existing.user_id, existing.run_id) != (run.user_id, run.run_id): raise ValueError('checkpoint anchor owner changed')
        return existing
    row = StyleRunCheckpoint(user_id=run.user_id, run_id=run.run_id,
        manifest_sha256=checked['manifest_sha256'], relative_directory=directory.relative_to(base).as_posix(),
        binding=json.dumps(expected_binding), position=json.dumps(checked['manifest']['position']), created_at=now())
    session.add(row); session.flush()
    return row

def verified_checkpoint(session, run, checkpoint_id):
    row = session.get(StyleRunCheckpoint, checkpoint_id)
    if row is None or (row.user_id, row.run_id) != (run.user_id, run.run_id):
        raise ApiError(404, 'CHECKPOINT_NOT_FOUND', 'Checkpoint not found')
    rel = Path(row.relative_directory)
    if rel.is_absolute() or rel.drive or '..' in rel.parts:
        raise ApiError(409, 'E_RESUME_PATH', 'Stored checkpoint path invalid')
    expected = checkpoint_binding(run)
    if digest(expected) != digest(json.loads(row.binding)):
        raise ApiError(409, 'E_RESUME_IDENTITY_MISMATCH', 'Current source/data/runtime/config differs from saved checkpoint')
    try:
        return row, verify_manifest(runstore.run_dir(run.user_id, run.run_id)/rel, expected, row.manifest_sha256)
    except (ValueError, OSError) as exc:
        raise ApiError(409, getattr(exc, 'code', 'E_RESUME_INVALID'), str(exc)) from exc

def assert_resume_qualified(config, binding=None):
    from engine.style.m2_adapter.windows_resume_runtime import authorize_checkpoint
    try:
        return authorize_checkpoint(APP, config.get('runtime_binding'), binding)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise ApiError(409, getattr(exc, 'code', 'FULL_TRAINER_RESUME_QUALIFICATION_REQUIRED'), str(exc)) from exc

def recovery_status(session, user_id, run_id):
    from .service import _get_owned_run
    run = _get_owned_run(session, user_id, run_id)
    items = []
    for row in session.query(StyleRunCheckpoint).filter_by(user_id=user_id, run_id=run_id).order_by(StyleRunCheckpoint.checkpoint_id.desc()).limit(20):
        item = {'checkpoint_id': row.checkpoint_id, 'position': json.loads(row.position), 'created_at': row.created_at, 'verified': False}
        try:
            verified_checkpoint(session, run, row.checkpoint_id); item['verified'] = True
        except (ApiError, OSError, ValueError) as exc:
            item['failure_code'] = getattr(exc, 'code', 'E_RESUME_INVALID')
        items.append(item)
    state = execution_state(session, run_id)
    child = session.get(StyleRunContinuation, run_id)
    available = run.status in ('failed', 'cancelled') and state in ('ENDED', 'EXECUTION_DEAD') and child is None
    resume_allowed, resume_gate = False, 'NO_VERIFIED_CHECKPOINT'
    if any(item['verified'] for item in items):
        try:
            assert_resume_qualified(json.loads(run.config), checkpoint_binding(run))
            resume_allowed = available
            resume_gate = 'READY' if available else 'RUN_EXECUTION_UNRESOLVED_OR_CONTINUED'
        except (ApiError, OSError, ValueError) as exc:
            resume_gate = getattr(exc, 'code', 'FULL_TRAINER_RESUME_QUALIFICATION_REQUIRED')
    return {'run_id': run_id, 'status': run.status, 'execution_state': state,
        'checkpoints': items, 'continuation_run_id': child.child_run_id if child else None,
        'retry_allowed': available,
        'resume_allowed': resume_allowed, 'resume_gate': resume_gate,
        'claims_quality_pass': False}

def continue_run(session, user_id, run_id, *, mode, request_key, checkpoint_id=None):
    from . import service
    parent = service._get_owned_run(session, user_id, run_id)
    if mode not in ('retry', 'resume') or not isinstance(request_key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', request_key):
        raise ApiError(400, 'INVALID_REQUEST', 'An explicit Idempotency-Key is required')
    fingerprint = digest({'mode': mode, 'checkpoint_id': checkpoint_id})
    def previous():
        prior = session.get(StyleRunContinuation, run_id)
        if prior is None: return None
        if prior.request_key != request_key or prior.request_sha256 != fingerprint:
            raise ApiError(409, 'CONTINUATION_EXISTS', 'Continue from the existing new attempt')
        return service._get_owned_run(session, user_id, prior.child_run_id)
    old = previous()
    if old: return old
    if parent.status not in ('failed', 'cancelled'):
        raise ApiError(409, 'RUN_NOT_RECOVERABLE', 'Only failed or cancelled runs can continue')
    if execution_state(session, run_id) not in ('ENDED', 'EXECUTION_DEAD'):
        raise ApiError(409, 'RUN_EXECUTION_UNRESOLVED', 'The recorded execution must be confirmed ended')
    config = json.loads(parent.config)
    marker = config.pop(SERVER_FIELD, {})
    logical_id = marker.get('logical_run_id', parent.run_id)
    saved = None
    if mode == 'resume':
        if type(checkpoint_id) is not int:
            raise ApiError(400, 'INVALID_REQUEST', 'A server checkpoint_id is required')
        saved, checked = verified_checkpoint(session, parent, checkpoint_id)
        assert_resume_qualified(config, json.loads(saved.binding))
    elif checkpoint_id is not None:
        raise ApiError(400, 'INVALID_REQUEST', 'Retry starts a fresh attempt and accepts no checkpoint')
    try:
        with session.begin_nested():
            # Re-run all current owner/snapshot/runtime/cache qualification gates.
            child = service._create_run_unkeyed(session, user_id, json.loads(parent.sample_ids), config)
            if saved:
                canonical = json.loads(child.config)
                canonical[SERVER_FIELD] = {'parent_run_id': parent.run_id, 'logical_run_id': logical_id,
                    'mode': mode, 'checkpoint_id': saved.checkpoint_id}
                child.config = json.dumps(canonical)
            session.add(StyleRunContinuation(parent_run_id=run_id, user_id=user_id,
                request_key=request_key, request_sha256=fingerprint, child_run_id=child.run_id))
            session.flush()
        return child
    except IntegrityError:
        prior = previous()
        if prior: return prior
        raise ApiError(409, 'CONTINUATION_PENDING', 'Retry the same key after the concurrent operation completes')
