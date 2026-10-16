"""Bounded managed-interpreter preparation jobs; the backend never imports ML."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading

from sqlalchemy.orm import Session
from app.core.db import get_engine
from app.core.storage import ensure_within_root, storage_root
from . import runstore
from .user_scr_product import (PROJ, UserSCRProductError, _owner_samples, cache_root,
                               register_prepared_user_scr)
from .user_scr_preparation import SERVER_ASSET_KEYS, plan_user_scr_preparation
from engine.style.dataset.binding import verify_snapshot_binding
from engine.style.dataset.schema import DatasetError
from engine.style.m2_adapter.user_scr_contract import RECIPE_ID, SHA256, asset_contract, digest, objective_contract

ASSET_CONFIG = PROJ / 'config/user-scr-assets.json'
_LOCK = threading.Lock()
_THREADS = {}


def _directory(user_id, style_id, job_id):
    if not isinstance(job_id, str) or not SHA256.fullmatch(job_id):
        raise UserSCRProductError('PREPARATION_REQUIRED', 'Invalid preparation job identity')
    root = storage_root()
    return ensure_within_root(root / 'users' / str(user_id) / 'styles' / 'user-scr-jobs' /
                              str(style_id) / job_id, root)


def _owned_style(session, user_id, style_id):
    from .models import StyleProfile
    row = session.query(StyleProfile).filter(StyleProfile.user_id == user_id, StyleProfile.style_id == style_id).first()
    if row is None:
        raise UserSCRProductError('STYLE_NOT_FOUND', 'Owned style not found')
    if row.status != 'approved' or row.source_type != 'HANDWRITING_ASSET':
        raise UserSCRProductError('PREPARATION_REQUIRED', 'Approved owned handwriting style required')


def _snapshot(user_id, style_id, snapshot_sha256):
    roots = [storage_root() / 'users' / str(user_id) / 'styles/snapshots', PROJ / 'artifacts/style-datasets']
    if os.environ.get('YY_M2_SNAPSHOT_ROOT'):
        roots.insert(0, Path(os.environ['YY_M2_SNAPSHOT_ROOT']))
    for root in roots:
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir())[:200]:
            try:
                index = json.loads((folder / 'index.json').read_text(encoding='utf-8'))
                if index.get('snapshot_sha256') != snapshot_sha256 or str(index.get('style_id')) != str(style_id):
                    continue
                return verify_snapshot_binding(folder, snapshot_sha256)
            except (OSError, ValueError, KeyError, TypeError, DatasetError):
                continue
    raise UserSCRProductError('PREPARATION_REQUIRED', 'Selected owned canonical S03 snapshot is unavailable')


def _configured_assets():
    try:
        value = json.loads(ASSET_CONFIG.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or set(value) != SERVER_ASSET_KEYS:
            raise ValueError('exact six server-owned feature/font/ledger paths required')
        if any(not isinstance(p, str) or not Path(p).is_absolute() for p in value.values()):
            raise ValueError('asset paths must be absolute in the managed consumer environment')
        return value
    except (OSError, ValueError) as exc:
        raise UserSCRProductError('PREPARATION_REQUIRED', 'Server SCR asset configuration is unavailable: ' + str(exc)) from exc


def start_preparation(session, user_id, style_id, payload):
    _owned_style(session, user_id, style_id)
    if (not isinstance(payload, dict) or set(payload) != {'snapshot_sha256', 'runtime_registration_id'}
            or not SHA256.fullmatch(str(payload.get('snapshot_sha256', '')))):
        raise UserSCRProductError('PREPARATION_REQUIRED', 'Preparation accepts only snapshot and runtime-registration IDs')
    from engine.style.m2_adapter.windows_training_recipe import select_training_registration,INFERENCE_ONLY
    try:selected,runtime_binding=select_training_registration(PROJ,payload['runtime_registration_id'])
    except (OSError,ValueError,KeyError,TypeError) as exc:
        code=getattr(exc,'code','RUNTIME_UNQUALIFIED')
        raise UserSCRProductError(code if code in (INFERENCE_ONLY,'E_WINDOWS_GENERIC_QUALIFICATION_REQUIRED') else 'RUNTIME_UNQUALIFIED',str(exc)) from exc
    binding = _snapshot(user_id, style_id, payload['snapshot_sha256'])
    assets = _configured_assets()
    from engine.style.m2_adapter.windows_user_scr import VARIANTS as WINDOWS_VARIANTS
    windows=runtime_binding['variant_id'] in WINDOWS_VARIANTS
    if windows:
        from .windows_scr_product import plan as windows_plan,cache_root as windows_cache_root
        plan=windows_plan(binding['snapshot_dir'],binding['snapshot_sha256'],windows_cache_root(user_id,style_id),assets,selected)
    else:plan = plan_user_scr_preparation(binding['snapshot_dir'], binding['snapshot_sha256'],cache_root=cache_root(user_id, style_id), server_assets=assets)
    if 'scr_data_binding' not in plan or plan['status'] in ('FONT_COVERAGE', 'STALE_CACHE'):
        raise UserSCRProductError(plan['status'], plan['detail'])
    _owner_samples(session, user_id, style_id, plan['scr_data_binding'])
    from engine.style.m2_adapter.user_scr_runtime import resolve_user_scr_runtime
    if windows:
        from engine.style.m2_adapter.windows_training_recipe import resolve_registered_runtime as resolve_user_scr_runtime,RECIPE_ID as selected_recipe
    else:selected_recipe=RECIPE_ID
    try:
        runtime = resolve_user_scr_runtime(PROJ, {'recipe_id': selected_recipe, 'runtime_binding': runtime_binding,
            'scr_assets': asset_contract(), 'scr_objective': objective_contract(),
            'scr_asset_paths': plan['cache_asset_paths']}, purpose='prepare')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UserSCRProductError('RUNTIME_UNQUALIFIED', str(exc)) from exc
    if plan.get('cache_reusable'):
        if windows:
            from .windows_scr_product import register
            result=register(session,user_id,style_id,binding,plan['cache_asset_paths'],runtime_binding)
        else:result = register_prepared_user_scr(session, user_id, style_id, binding, plan['cache_asset_paths'])
        return {'status': 'PREPARED', **result, 'production_ready': False}
    job_id = digest({'user_id': user_id, 'style_id': style_id, 'data_sha256': plan['scr_data_binding']['sha256'],
                     'runtime_binding': runtime_binding, 'cache_key': plan['idempotency_key']})
    directory = _directory(user_id, style_id, job_id)
    request = {'recipe_id': selected_recipe, 'user_id': user_id, 'style_id': style_id,
        'snapshot_dir': binding['snapshot_dir'], 'snapshot_sha256': binding['snapshot_sha256'],
        'scr_data_binding': plan['scr_data_binding'], 'scr_asset_paths': plan['cache_asset_paths'],
        'runtime_binding': runtime_binding, 'output_dir': plan['cache_dir'],
        'out_receipt': str(directory / 'receipt.json'), 'max_seconds': 900}
    state = {'schema': 'm2-scr2n-user-preparation-job/1', 'status': 'PREPARING', 'job_id': job_id,
        'style_id': style_id, 'snapshot_sha256': binding['snapshot_sha256'], 'counts': plan['counts'],
        'runtime_binding_sha256': runtime_binding['binding_sha256'], 'request_sha256': digest(request),
        'm2_launch_authorized': False, 'production_ready': False}
    with _LOCK:
        old = runstore.read_json(directory / 'state.json')
        if old is not None:
            if old.get('request_sha256') != state['request_sha256']:
                raise UserSCRProductError('STALE_CACHE', 'Preparation job identity changed')
            if old.get('status') == 'PREPARING' and job_id not in _THREADS:
                old.update(status='PREPARATION_REQUIRED', detail='Preparation was interrupted; outputs require review before retry')
                runstore.write_json_atomic(directory / 'state.json', old)
            return old
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / 'request.json').open('x', encoding='utf-8') as stream:
            json.dump(request, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
        runstore.write_json_atomic(directory / 'state.json', state)
        thread = threading.Thread(target=_run_preparation_job,
            args=(user_id, style_id, job_id, runtime['python']), daemon=True)
        _THREADS[job_id] = thread
        thread.start()
    return state


def _run_preparation_job(user_id, style_id, job_id, interpreter):
    from engine.style.m2_adapter.runtime_variants import bound_subprocess_env
    from .worker import gpu_lock_acquire, gpu_lock_release
    directory = _directory(user_id, style_id, job_id)
    state = runstore.read_json(directory / 'state.json')
    request = runstore.read_json(directory / 'request.json')
    process = None
    locked = False
    cpu_lease = None
    scr_gpu_lease = None
    try:
        if digest(request) != state['request_sha256']:
            raise UserSCRProductError('STALE_CACHE', 'Immutable preparation request changed')
        from engine.style.m2_adapter.windows_training_recipe import RECIPE_ID as WINDOWS_RECIPE
        if request['recipe_id']==WINDOWS_RECIPE:
            from engine.style.m2_adapter.windows_training_runtime import acquire_cpu_lease,_env
            cpu_lease=acquire_cpu_lease(PROJ,'preparation/'+job_id)
            if request['runtime_binding']['device']=='cuda':
                from engine.style.m2_adapter.windows_gpu_support import acquire_gpu_lease
                scr_gpu_lease=acquire_gpu_lease(PROJ,'preparation/'+job_id)
            child_env=_env(directory/'private-env',request['runtime_binding']['variant_id'])
        else:
            locked, reason = gpu_lock_acquire('user-scr-preparation/' + job_id, 0)
            if not locked:raise UserSCRProductError('PREPARATION_REQUIRED', 'Model runtime is busy: ' + str(reason))
            child_env=bound_subprocess_env(request['runtime_binding'])
        process = subprocess.Popen([str(interpreter), '-B', '-m', 'engine.style.m2_adapter.user_scr_prepare_runner',
            str(directory / 'request.json')], cwd=str(PROJ), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            env=child_env)
        process.communicate(timeout=request['max_seconds'] + 30)
        receipt = runstore.read_json(Path(request['out_receipt']))
        if (process.returncode != 0 or not isinstance(receipt, dict) or receipt.get('ok') is not True
                or receipt.get('schema') != 'm2-scr2n-user-preparation-receipt/1'
                or receipt.get('synthetic_test_only') is not False or receipt.get('m2_launch_authorized') is not False
                or receipt.get('user_id') != user_id or receipt.get('style_id') != style_id
                or receipt.get('scr_data_binding') != request['scr_data_binding']
                or receipt.get('runtime_binding') != request['runtime_binding']):
            raise UserSCRProductError('PREPARATION_REQUIRED', 'Managed preparation did not return an exact successful reference receipt')
        from engine.style.m2_adapter.user_scr_cache import validate_user_reference_cache
        verified = validate_user_reference_cache(request['scr_asset_paths'], receipt['snapshot_binding'], verify_assets=False)
        if (verified['cache_binding'] != receipt.get('scr_cache_binding')
                or verified['data_binding'] != request['scr_data_binding']):
            raise UserSCRProductError('STALE_CACHE', 'Managed receipt differs from independently verified cache')
        with Session(get_engine()) as session:
            from engine.style.m2_adapter.windows_training_recipe import RECIPE_ID as WINDOWS_RECIPE
            if request['recipe_id']==WINDOWS_RECIPE:
                from .windows_scr_product import register
                result=register(session,user_id,style_id,receipt['snapshot_binding'],request['scr_asset_paths'],request['runtime_binding'])
            else:result = register_prepared_user_scr(session, user_id, style_id,receipt['snapshot_binding'], request['scr_asset_paths'])
        state.update(status='PREPARED', **result)
    except Exception as exc:
        if isinstance(exc, subprocess.TimeoutExpired) and process is not None:
            process.kill()
            process.communicate()
        state.update(status=getattr(exc, 'code', 'PREPARATION_REQUIRED'), detail=str(exc))
    finally:
        execution_ended = process is None or getattr(process, 'returncode', None) is not None
        if locked and execution_ended:
            gpu_lock_release()
        if not execution_ended and (locked or scr_gpu_lease is not None or cpu_lease is not None):
            state['execution_end_unconfirmed'] = True
            state['detail'] = 'Owned preparation child has not confirmed termination; execution leases retained'
        if scr_gpu_lease is not None and execution_ended:
            from engine.style.m2_adapter.windows_gpu_support import release_gpu_lease
            release_gpu_lease(scr_gpu_lease)
        if cpu_lease is not None and execution_ended:
            from engine.style.m2_adapter.windows_training_runtime import release_cpu_lease
            release_cpu_lease(cpu_lease)
        runstore.write_json_atomic(directory / 'state.json', state)
        _THREADS.pop(job_id, None)


def get_preparation(session, user_id, style_id, job_id):
    _owned_style(session, user_id, style_id)
    state = runstore.read_json(_directory(user_id, style_id, job_id) / 'state.json')
    if state is None:
        raise UserSCRProductError('PREPARATION_REQUIRED', 'Owned preparation job not found')
    return state
