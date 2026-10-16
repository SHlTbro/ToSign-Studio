"""Model-free preparation planning shared by CLI and authenticated product jobs.

Product authorization lives in user_scr_product and user_scr_jobs.
The server supplies asset paths and an ownership/export verifier; browser input
can select only a style and snapshot identity. No API client may supply a cache.
Planning is not authorization to execute features, train, or publish a model.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from engine.style.m2_adapter.user_scr_contract import (
    ASSET_PATH_KEYS, OBJECTIVE_PROFILE, RECIPE_ID, SHA256, asset_contract,
    digest, objective_contract, validate_user_scr_asset_path_shape,
)

PREPARATION_REQUIRED = 'PREPARATION_REQUIRED'
FONT_COVERAGE = 'FONT_COVERAGE'
STALE_CACHE = 'STALE_CACHE'
SERVER_ASSET_KEYS = set(ASSET_PATH_KEYS) - {'reference_cache_manifest', 'reference_cache_vectors'}


@dataclass(frozen=True)
class VerifiedSCRExport:
    """Server-verifier return type, never deserialized from an API request.

    An injected planner adapter must check authenticated ownership and the
    canonical export result before returning this record. Formal API routes use
    current database ownership through user_scr_product instead.
    """

    user_id: int
    style_id: int
    snapshot_dir: str
    snapshot_sha256: str
    preparation_authorized: bool
    m2_launch_authorized: bool = False


def _blocked(code, detail):
    return {'schema': 'm2-scr2n-user-preparation-plan/1', 'status': code,
            'detail': detail, 'recipe_id': RECIPE_ID,
            'objective_profile': OBJECTIVE_PROFILE,
            'production_ready': False, 'runtime_qualified': False,
            'm2_launch_authorized': False, 'models_loaded': False,
            'feature_execution_performed': False, 'weights_read': False}


def _owned_paths(cache_root, data_binding, server_assets):
    if not isinstance(server_assets, dict) or set(server_assets) != SERVER_ASSET_KEYS:
        raise ValueError('server configuration requires exactly six non-cache asset paths')
    root = Path(cache_root)
    if not root.is_absolute():
        raise ValueError('server cache root must be absolute')
    root = root.resolve()
    if any((parent / 'FROZEN').exists() for parent in (root, *root.parents)):
        raise ValueError('server cache root cannot be inside an immutable S03 snapshot')
    identity = digest({'recipe_id': RECIPE_ID, 'scr_data_binding_sha256': data_binding['sha256'],
                       'scr_assets_sha256': asset_contract()['sha256'],
                       'scr_objective_sha256': objective_contract()['sha256']})
    directory = root / identity
    paths = {**server_assets,
             'reference_cache_manifest': str(directory / 'manifest.json'),
             'reference_cache_vectors': str(directory / 'reference-vectors.npz')}
    paths = validate_user_scr_asset_path_shape(paths)
    for name in ('reference_cache_manifest', 'reference_cache_vectors'):
        if not Path(paths[name]).resolve().is_relative_to(root):
            raise ValueError('server-owned cache location escapes its configured root')
    return identity, directory, paths


def plan_user_scr_preparation(snapshot_dir, snapshot_sha256, *, cache_root, server_assets,
                              check_font_coverage=False):
    """Read-only filesystem planning; only a server or local CLI may call this.

    Re-read S03 and the ledger every time. Idempotency includes full content,
    normalized target, ledger, objective and asset identities, not row count.
    A valid cache permits reuse planning only; it cannot qualify a runtime.
    """
    from engine.style.m2_adapter.trainer_resume_protocol import native_path
    from engine.style.m2_adapter.user_scr_cache import (
        inspect_font_coverage, inspect_user_snapshot, validate_user_reference_cache,
    )
    from engine.style.m2_adapter.user_scr_recipe import plan_user_scr_recipe
    try:
        if type(check_font_coverage) is not bool:
            raise ValueError('check_font_coverage must be an explicit boolean')
        if not isinstance(server_assets, dict) or set(server_assets) != SERVER_ASSET_KEYS:
            raise ValueError('server configuration requires exactly six non-cache asset paths')
        inspected = inspect_user_snapshot(snapshot_dir, snapshot_sha256,
                                          ledger_path=server_assets['source_ledger'])
        data = inspected['data_binding']
        identity, directory, paths = _owned_paths(cache_root, data, server_assets)
        recipe = plan_user_scr_recipe(data['counts']['train'])
    except Exception as exc:
        # This boundary returns actionable states instead of silently repairing
        # a snapshot, review status, source ledger, or existing cached output.
        return _blocked(PREPARATION_REQUIRED, str(exc))
    plan = _blocked(PREPARATION_REQUIRED, 'Canonical inputs verified; reference cache preparation is required')
    plan.update(idempotency_key=identity, snapshot_binding=copy.deepcopy(inspected['snapshot_binding']),
                scr_data_binding=copy.deepcopy(data), counts=copy.deepcopy(data['counts']),
                cache_dir=str(directory), cache_asset_paths=paths, cache_reusable=False,
                cache_status='ABSENT', font_coverage='DEFERRED_TO_REFERENCE_PREPARATION',
                training_recipe=recipe['training_recipe'], training_budget=recipe['training_budget'],
                remaining_gates=['REFERENCE_CACHE_PREPARATION', 'EXACT_RUNTIME_MEASURED_PROOF',
                                 'AUTHENTICATED_PRODUCT_LAUNCH_AUTHORIZATION'])
    if check_font_coverage:
        try:
            inspect_font_coverage(inspected['rows'], paths)
        except Exception as exc:
            code = FONT_COVERAGE if getattr(exc, 'code', None) in (FONT_COVERAGE, 'E_SCR_FONT_COVERAGE') else PREPARATION_REQUIRED
            plan.update(status=code, detail=str(exc), font_coverage='REJECTED')
            return plan
        plan['font_coverage'] = 'CMAP_VERIFIED_RASTER_CHECK_STILL_REQUIRED'
    files = [Path(paths[key]) for key in ('reference_cache_manifest', 'reference_cache_vectors')]
    if not any(native_path(path).exists() for path in files):
        if native_path(directory).exists():
            plan.update(status=STALE_CACHE, cache_status='REJECTED',
                        detail='Existing cache directory has no complete committed reference cache')
        return plan
    try:
        if not all(native_path(path).is_file() for path in files):
            raise ValueError('partial reference cache; both immutable outputs are required')
        # Reads S03, ledger, manifest and vectors, never source modules/weights.
        # The runtime constructor independently re-verifies actual asset bytes.
        cache = validate_user_reference_cache(paths, inspected['snapshot_binding'],
                                             verify_assets=False)
        if cache['data_binding'] != data:
            raise ValueError('reference cache differs from fresh canonical snapshot data')
    except Exception as exc:
        code = FONT_COVERAGE if getattr(exc, 'code', None) in (FONT_COVERAGE, 'E_SCR_FONT_COVERAGE') else STALE_CACHE
        plan.update(status=code, detail=str(exc), cache_status='REJECTED')
        return plan
    plan.update(status='PREPARED_RUNTIME_UNVERIFIED', cache_status='VALIDATED',
                detail='Reference cache can be reused; exact runtime proof and launch authorization remain required',
                cache_reusable=True, font_coverage='VERIFIED_IN_BOUND_CACHE',
                scr_cache_binding=copy.deepcopy(cache['cache_binding']),
                remaining_gates=['EXACT_RUNTIME_MEASURED_PROOF', 'AUTHENTICATED_PRODUCT_LAUNCH_AUTHORIZATION'])
    return plan


class UserSCRPreparationService:
    """Injectable read-only planning boundary, with no ML callback.

    ``resolve_export`` must be constructed by trusted server code, capturing its
    database/session and checking current authenticated ownership. It accepts
    ``(user_id, style_id, snapshot_sha256)`` and returns VerifiedSCRExport. This
    class cannot create one from client paths, cached vectors, or review flags.
    """

    def __init__(self, *, cache_root, server_assets, resolve_export: Callable,
                 check_font_coverage=True):
        if not callable(resolve_export):
            raise TypeError('trusted server export verifier is required')
        if type(check_font_coverage) is not bool:
            raise TypeError('check_font_coverage must be an explicit boolean')
        self._cache_root = cache_root
        self._server_assets = copy.deepcopy(server_assets)
        self._resolve_export = resolve_export
        self._check_font_coverage = check_font_coverage

    def plan(self, *, authenticated_user_id, style_id, snapshot_sha256):
        if (type(authenticated_user_id) is not int or authenticated_user_id < 1
                or type(style_id) is not int or style_id < 1
                or not isinstance(snapshot_sha256, str) or not SHA256.fullmatch(snapshot_sha256)):
            return _blocked(PREPARATION_REQUIRED, 'Authenticated user, style and exact snapshot identity are required')
        try:
            exported = self._resolve_export(authenticated_user_id, style_id, snapshot_sha256)
        except Exception as exc:
            return _blocked(PREPARATION_REQUIRED, 'Server export verification failed: ' + str(exc))
        if (not isinstance(exported, VerifiedSCRExport)
                or exported.preparation_authorized is not True
                or type(exported.user_id) is not int or type(exported.style_id) is not int
                or exported.user_id != authenticated_user_id or exported.style_id != style_id
                or exported.snapshot_sha256 != snapshot_sha256
                or type(exported.m2_launch_authorized) is not bool):
            return _blocked(PREPARATION_REQUIRED, 'Server-verified export authorization is missing or belongs to another user/style/snapshot')
        plan = plan_user_scr_preparation(exported.snapshot_dir, snapshot_sha256,
                                        cache_root=self._cache_root, server_assets=self._server_assets,
                                        check_font_coverage=self._check_font_coverage)
        if ('snapshot_binding' in plan
                and str(plan['snapshot_binding'].get('style_id')) != str(style_id)):
            return _blocked(PREPARATION_REQUIRED, 'Canonical S03 style differs from the authenticated export')
        # Even a server's prior launch permission does not establish capability
        # for this new recipe. Never upgrade the exporter's false boundary.
        return plan
