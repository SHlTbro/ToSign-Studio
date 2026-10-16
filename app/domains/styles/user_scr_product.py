"""Owned prepared-artifact admission for the generic SCR create/start path.

Only server code can register a prepared cache. HTTP requests select immutable
IDs; they cannot upload paths, vectors, provenance, or authorization flags here.
Everything in this module is model-free and safe in the backend interpreter.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from app.core.storage import ensure_within_root, storage_root
from app.domains.training_sets.models import TrainingSample
from .models import StyleProfile
from engine.style.m2_adapter.user_scr_contract import (
    RECIPE_ID, SHA256, asset_contract, digest, objective_contract, require_sealed,
    sealed, validate_cache_binding, validate_data_binding,
)
from .user_scr_preparation import SERVER_ASSET_KEYS, plan_user_scr_preparation

PROJ = Path(__file__).resolve().parents[3]
CLIENT_FIELDS = {'engine', 'recipe_id', 'style_id', 'snapshot_sha256', 'preparation_id',
                 'runtime_registration_id', 'train', 'acceptance_targets', 'acceptance_reference_label'}


class UserSCRProductError(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)


def cache_root(user_id, style_id):
    root = storage_root()
    return ensure_within_root(root / 'users' / str(user_id) / 'styles' / 'user-scr-cache' / str(style_id), root)


def _record_path(user_id, style_id, preparation_id):
    if (type(user_id) is not int or user_id < 1 or type(style_id) is not int or style_id < 1
            or not isinstance(preparation_id, str) or not SHA256.fullmatch(preparation_id)):
        raise UserSCRProductError('PREPARATION_REQUIRED', 'Exact owned preparation identity is required')
    root = storage_root()
    return ensure_within_root(root / 'users' / str(user_id) / 'styles' / 'user-scr-preparations' /
                              str(style_id) / (preparation_id + '.json'), root)


def _owner_samples(session, user_id, style_id, data_binding):
    profile = session.query(StyleProfile).filter(StyleProfile.user_id == user_id,
                                                StyleProfile.style_id == style_id).first()
    if profile is None:
        raise UserSCRProductError('STYLE_NOT_FOUND', 'Owned style not found')
    if profile.status != 'approved' or profile.source_type != 'HANDWRITING_ASSET':
        raise UserSCRProductError('PREPARATION_REQUIRED', 'An approved owned handwriting style is required')
    if str(data_binding['style_id']) != str(style_id):
        raise UserSCRProductError('STALE_CACHE', 'Snapshot style differs from the selected owned style')
    rows = data_binding['rows']
    if len({row['label'] for row in rows}) < 20:
        raise UserSCRProductError('PREPARATION_REQUIRED', 'The unchanged product minimum is 20 distinct confirmed characters')
    current = session.query(TrainingSample).filter(TrainingSample.user_id == user_id,
        TrainingSample.style_id == str(style_id), TrainingSample.sample_id.in_([r['sample_id'] for r in rows])).all()
    by_id = {row.sample_id: row for row in current}
    if set(by_id) != {row['sample_id'] for row in rows}:
        raise UserSCRProductError('PREPARATION_REQUIRED', 'Canonical snapshot occurrences are not owned by this user/style')
    bound = []
    for row in rows:
        current = by_id[row['sample_id']]
        if (current.review_status not in ('owner_confirmed', 'ai_confirmed_authorized')
                or current.text_label != row['label']
                or current.final_alpha_sha256 != row['source_png_sha256']
                or current.source_asset_sha256 != row['parent_source_hash']
                or type(current.style_sample_id) is not int):
            raise UserSCRProductError('STALE_CACHE', 'Current confirmed occurrence differs from the prepared snapshot: ' + row['sample_id'])
        bound.append({'sample_id': current.sample_id, 'style_sample_id': current.style_sample_id,
                      'label': current.text_label, 'alpha_sha256': current.final_alpha_sha256,
                      'parent_sha256': current.source_asset_sha256, 'review_status': current.review_status})
    return bound


def register_prepared_user_scr(session, user_id, style_id, snapshot_binding, asset_paths):
    """Register already computed references; never create features or authorize a run.

    Called by trusted server orchestration after the isolated ML preparation
    phase. The supplied cache must occupy this owner's derived cache directory.
    No HTTP endpoint exposes this function or accepts this record from clients.
    """
    assets = {key: asset_paths[key] for key in SERVER_ASSET_KEYS}
    plan = plan_user_scr_preparation(snapshot_binding['snapshot_dir'], snapshot_binding['snapshot_sha256'],
        cache_root=cache_root(user_id, style_id), server_assets=assets)
    if plan.get('cache_reusable') is not True:
        raise UserSCRProductError(plan['status'], plan['detail'])
    if asset_paths != plan['cache_asset_paths'] or snapshot_binding != plan['snapshot_binding']:
        raise UserSCRProductError('STALE_CACHE', 'Prepared cache paths/snapshot differ from server-owned canonical inputs')
    owners = _owner_samples(session, user_id, style_id, plan['scr_data_binding'])
    record = sealed({'schema': 'm2-scr2n-user-prepared-record/1', 'user_id': user_id, 'style_id': style_id,
        'snapshot_binding': plan['snapshot_binding'], 'scr_asset_paths': plan['cache_asset_paths'],
        'scr_data_binding': plan['scr_data_binding'], 'scr_cache_binding': plan['scr_cache_binding'],
        'owner_samples': owners, 'm2_launch_authorized': False})
    path = _record_path(user_id, style_id, record['sha256'])
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except FileExistsError:
        if json.loads(path.read_text(encoding='utf-8')) != record:
            raise UserSCRProductError('STALE_CACHE', 'An existing preparation record changed')
    return {'preparation_id': record['sha256'], 'snapshot_sha256': snapshot_binding['snapshot_sha256'],
            'style_id': style_id, 'cache_prepared': True, 'm2_launch_authorized': False}


def _read_prepared(session, user_id, style_id, preparation_id, snapshot_sha256):
    path = _record_path(user_id, style_id, preparation_id)
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        require_sealed(record, 'm2-scr2n-user-prepared-record/1')
    except FileNotFoundError as exc:
        raise UserSCRProductError('PREPARATION_REQUIRED', 'A server-prepared reference cache is required for this snapshot') from exc
    except (OSError, ValueError, TypeError) as exc:
        raise UserSCRProductError('STALE_CACHE', 'Prepared reference record is invalid: ' + str(exc)) from exc
    if (record['sha256'] != preparation_id or record.get('user_id') != user_id
            or record.get('style_id') != style_id or record.get('m2_launch_authorized') is not False
            or record.get('snapshot_binding', {}).get('snapshot_sha256') != snapshot_sha256):
        raise UserSCRProductError('STALE_CACHE', 'Prepared reference record belongs to another user/style/snapshot')
    try:
        data = validate_data_binding(record['scr_data_binding'])
        validate_cache_binding(record['scr_cache_binding'], data)
        assets = {key: record['scr_asset_paths'][key] for key in SERVER_ASSET_KEYS}
        plan = plan_user_scr_preparation(record['snapshot_binding']['snapshot_dir'], snapshot_sha256,
            cache_root=cache_root(user_id, style_id), server_assets=assets)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UserSCRProductError('STALE_CACHE', str(exc)) from exc
    if (plan.get('cache_reusable') is not True or plan.get('snapshot_binding') != record['snapshot_binding']
            or plan.get('scr_data_binding') != data or plan.get('scr_cache_binding') != record['scr_cache_binding']
            or plan.get('cache_asset_paths') != record['scr_asset_paths']):
        raise UserSCRProductError('STALE_CACHE', 'Prepared references changed or no longer match canonical S03')
    if _owner_samples(session, user_id, style_id, data) != record['owner_samples']:
        raise UserSCRProductError('STALE_CACHE', 'Current ownership/review binding changed after preparation')
    return record


def canonical_user_scr_config(session, user_id, config, sample_ids, *, require_existing=False):
    """Resolve an ID-only selection, or revalidate an immutable saved run."""
    if require_existing:
        request = config.get('user_scr_request')
    else:
        request = config
    if (not isinstance(request, dict) or set(request) - CLIENT_FIELDS
            or request.get('engine') != 'm2' or request.get('recipe_id') != RECIPE_ID
            or type(request.get('style_id')) is not int
            or not SHA256.fullmatch(str(request.get('snapshot_sha256', '')))
            or not SHA256.fullmatch(str(request.get('preparation_id', '')))
            or not re.fullmatch('[0-9a-f]{32}', str(request.get('runtime_registration_id', '')))):
        raise UserSCRProductError('PREPARATION_REQUIRED', 'Select a server preparation and registered runtime by exact IDs; client paths/provenance/flags are forbidden')
    from .user_scr_jobs import _owned_style
    _owned_style(session,user_id,request['style_id'])
    from engine.style.m2_adapter.windows_training_recipe import select_training_registration,INFERENCE_ONLY
    try:selected,binding=select_training_registration(PROJ,request['runtime_registration_id'])
    except (OSError,ValueError,KeyError,TypeError) as exc:
        code=getattr(exc,'code','RUNTIME_UNQUALIFIED')
        raise UserSCRProductError(code if code in (INFERENCE_ONLY,'E_WINDOWS_GENERIC_QUALIFICATION_REQUIRED') else 'RUNTIME_UNQUALIFIED',str(exc)) from exc
    record = _read_prepared(session, user_id, request['style_id'], request['preparation_id'], request['snapshot_sha256'])
    ids = sorted(row['style_sample_id'] for row in record['owner_samples'])
    if (require_existing and sample_ids != ids) or (not require_existing and sample_ids and sorted(sample_ids) != ids):
        raise UserSCRProductError('STALE_CACHE', 'Locked sample selection differs from the canonical prepared snapshot')
    from engine.style.m2_adapter import runtime_variants as runtimes
    preparation = sealed({'schema': 'm2-scr2n-user-preparation-binding/1',
        'user_id': user_id, 'style_id': request['style_id'], 'snapshot_sha256': request['snapshot_sha256'],
        'data_binding_sha256': record['scr_data_binding']['sha256'],
        'cache_binding_sha256': record['scr_cache_binding']['sha256'], 'preparation_id': request['preparation_id']})
    from engine.style.m2_adapter.spatial_topology import topology_fields
    result = {**copy.deepcopy(request), 'user_scr_request': copy.deepcopy(request),
        'snapshot_dir': record['snapshot_binding']['snapshot_dir'], 'runtime_binding': binding,
        'runtime_manifest_path': selected['manifest_path'], 'runtime_identity_sha256': selected['asset_identity_sha256'],
        'fd_repo': selected['fd_repo'], 'content_font_path': str(Path(selected['fd_repo']) / 'ttf/KaiXinSongA.ttf'),
        'base_npz_sha256': selected['manifest']['base_npz_sha256'],
        'train': {'device': selected['device'], **request.get('train', {})},
        'scr_objective': objective_contract(), 'scr_assets': asset_contract(),
        'scr_data_binding': record['scr_data_binding'], 'scr_cache_binding': record['scr_cache_binding'],
        'scr_preparation_binding': preparation, 'scr_asset_paths': record['scr_asset_paths'], **topology_fields()}
    from .m2_product_config import canonical_control_config
    try:
        result = canonical_control_config(result)
        runtimes.binding_for_config(PROJ, result)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UserSCRProductError('RUNTIME_UNQUALIFIED', str(exc)) from exc
    if require_existing and any(config.get(key) != value for key, value in result.items()):
        raise UserSCRProductError('STALE_CACHE', 'Immutable run config differs from its server preparation/runtime')
    return result, ids


def discover_user_scr(session, user_id, style_id, runtime_registration_id=None):
    """Expose ID-only prepared selections; readiness still requires qualification."""
    from app.core.errors import ApiError
    from .user_scr_jobs import _owned_style
    try:_owned_style(session,user_id,style_id)
    except UserSCRProductError as exc:raise ApiError(404 if exc.code=='STYLE_NOT_FOUND' else 409,exc.code,str(exc)) from exc
    folder = _record_path(user_id,style_id,'0'*64).parent
    prepared = []
    if folder.is_dir():
        for path in sorted(folder.glob('*.json'), key=lambda p:p.stat().st_mtime, reverse=True)[:100]:
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
                record = _read_prepared(session,user_id,style_id,path.stem,raw['snapshot_binding']['snapshot_sha256'])
                prepared.append({'preparation_id':path.stem,'snapshot_sha256':record['snapshot_binding']['snapshot_sha256'],
                                 'counts':record['scr_data_binding']['counts']})
            except (OSError,ValueError,KeyError,TypeError):continue
    result = {'status':'BLOCKED','ready':False,'config':None,'sample_ids':[],
              'prepared_references':prepared,'blockers':[],'m2_launch_authorized':False,
              'capabilities':{'create_m2_run':False,'prepare_user_scr':False}}
    # Discovery may expose immutable selections before the first cache exists.
    # Never copy control config/readiness or offer its runtime as an SCR fallback.
    from .m2_runtime_api import discover
    from engine.style.m2_adapter.user_scr_recipe import SUPPORTED_RUNTIME_VARIANTS
    selections = discover(session, user_id, style_id, recipe_id=None)
    for key in ('snapshot', 'snapshot_preparation_dependencies', 'acceptance_policy',
                'provider_mode', 'license_scope'):
        result[key] = selections.get(key)
    result['runtime_options'] = [option for option in selections.get('runtime_options', [])
                                 if option.get('variant_id') in SUPPORTED_RUNTIME_VARIANTS]
    result['capabilities']['prepare_user_scr'] = bool(result.get('snapshot') and any(
        option.get('ready') is True for option in result['runtime_options']))
    if not prepared:
        result['blockers'].append({'code':'PREPARATION_REQUIRED','detail':'Prepare this owned S03 through the managed SCR preparation endpoint'})
        return result
    if not runtime_registration_id:
        result['blockers'].append({'code':'RUNTIME_UNQUALIFIED','detail':'Select a runtime qualified for the generic SCR recipe'})
        return result
    request = {'engine':'m2','recipe_id':RECIPE_ID,'style_id':style_id,
               'snapshot_sha256':prepared[0]['snapshot_sha256'],'preparation_id':prepared[0]['preparation_id'],
               'runtime_registration_id':runtime_registration_id}
    try:
        _, ids = canonical_user_scr_config(session,user_id,request,[])
    except UserSCRProductError as exc:
        result['blockers'].append({'code':exc.code,'detail':str(exc)})
        return result
    result.update(status='READY',ready=True,config=request,sample_ids=ids,
                  capabilities={'create_m2_run':True,'prepare_user_scr':True})
    return result
