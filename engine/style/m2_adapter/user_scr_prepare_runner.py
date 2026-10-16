"""Registered managed-ML subprocess entrypoint for generic SCR preparation.

Use ``python -m engine.style.m2_adapter.user_scr_prepare_runner REQUEST.json``.
The backend creates the immutable request and owns its paths. Runtime, source,
snapshot and local asset identity are verified before the deferred loader is
imported. This entrypoint does not train, infer, publish, or grant launch rights.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

from .user_scr_contract import (RECIPE_ID, OBJECTIVE_PROFILE, digest, asset_contract,
    objective_contract, validate_user_scr_asset_path_shape, validate_data_binding,
    validate_cache_binding)

APP = Path(__file__).resolve().parents[3]


def _load_producer(paths):
    # Deliberately unreachable until qualification + registered path + S03 gates.
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(APP)
    from .user_scr_objective import make_user_scr_reference_producer
    return make_user_scr_reference_producer(paths)


def _bounds(request):
    seconds = request.get('max_seconds', 900)
    if type(seconds) not in (int, float) or not 0 < seconds <= 3600:
        raise ValueError('preparation max_seconds must be finite within (0,3600]')
    return float(seconds)


def _validate_request(request):
    from .windows_training_recipe import RECIPE_ID as WINDOWS_RECIPE
    if not isinstance(request, dict) or request.get('recipe_id') not in (RECIPE_ID,WINDOWS_RECIPE):
        raise ValueError('explicit generic SCR preparation request required')
    if any(type(request.get(k)) is not int or request[k] < 1 for k in ('user_id', 'style_id')):
        raise ValueError('server-bound user/style identity required')
    _bounds(request)
    paths = validate_user_scr_asset_path_shape(request.get('scr_asset_paths'))
    expected = validate_data_binding(request.get('scr_data_binding'))
    if (request.get('snapshot_sha256') != expected['snapshot_sha256']
            or str(request['style_id']) != str(expected['style_id'])):
        raise ValueError('preparation user/style/snapshot request differs from verified data')
    out = Path(request['output_dir'])
    if not out.is_absolute() or out.exists() or any((p / 'FROZEN').exists() for p in (out.resolve(), *out.resolve().parents)):
        raise ValueError('preparation requires a new output outside frozen snapshots')
    if (Path(paths['reference_cache_manifest']) != out / 'manifest.json'
            or Path(paths['reference_cache_vectors']) != out / 'reference-vectors.npz'):
        raise ValueError('request output does not match server-owned cache paths')
    return paths, expected


from .personal_runtime_policy import bound_action

@bound_action('prepare')
def run(request):
    from .release_runtime_policy import assert_m2_execution_allowed
    from .runtime_variants import RuntimeUnavailable
    try:assert_m2_execution_allowed(APP)
    except (RuntimeUnavailable,OSError) as exc:
        return 3,{"schema":"m2-scr2n-user-preparation-receipt/1","ok":False,"status":"BLOCKED","failure":{"code":getattr(exc,'code','BLOCKED_ENV'),"detail":str(exc)},"feature_execution_performed":False,"m2_launch_authorized":False,"training_performed":False,"inference_performed":False,"claims_quality_pass":False}
    from .user_scr_runtime import resolve_user_scr_runtime
    from .windows_training_recipe import RECIPE_ID as WINDOWS_RECIPE
    if request.get('recipe_id')==WINDOWS_RECIPE:
        from .windows_training_recipe import resolve_registered_runtime as resolve_user_scr_runtime
    from .user_scr_cache import prepare_user_reference_cache
    return _run(request, resolve_user_scr_runtime, _load_producer, prepare_user_reference_cache, synthetic=False)


def _run_synthetic_preparation_for_tests(request, *, resolve_runtime, producer_factory, prepare_cache):
    """Explicit internal test seam; never exposed through request fields or CLI."""
    return _run(request, resolve_runtime, producer_factory, prepare_cache, synthetic=True)


def _run(request, runtime_resolver, producer_factory, cache_preparer, *, synthetic):
    if not synthetic:
        from .release_runtime_policy import assert_m2_execution_allowed
        assert_m2_execution_allowed(APP)
    from .user_scr_cache import inspect_user_snapshot
    from .user_scr_runtime import user_scr_source_hashes
    started = time.monotonic()
    try:
        paths, expected = _validate_request(request)
        runtime_request = {**request, 'scr_objective': objective_contract(), 'scr_assets': asset_contract()}
        resolved = runtime_resolver(APP, runtime_request, purpose='prepare')
        if Path(sys.executable).resolve() != Path(resolved['python']).resolve():
            raise ValueError('preparation child interpreter differs from registered runtime')
        source = user_scr_source_hashes(APP)
        verified = inspect_user_snapshot(request['snapshot_dir'], request['snapshot_sha256'], ledger_path=paths['source_ledger'])
        if verified['data_binding'] != expected:
            raise ValueError('preparation canonical snapshot/target identity changed before model load')
        from .windows_training_recipe import RECIPE_ID as WINDOWS_RECIPE
        if request['recipe_id']==WINDOWS_RECIPE and not synthetic:
            from .windows_training_runtime import setup_cpu
            setup_cpu()
        producer = producer_factory(paths)
        prepared = cache_preparer(request['output_dir'], verified['snapshot_binding'], paths, feature_producer=producer)
        if prepared['data_binding'] != expected:
            raise ValueError('prepared cache data binding differs from immutable request')
        if not synthetic:
            validate_cache_binding(prepared['cache_binding'], expected)
        elif prepared['cache_binding'].get('synthetic_test_only') is not True:
            raise ValueError('synthetic preparation must retain its test-only evidence')
        if user_scr_source_hashes(APP) != source:
            raise ValueError('preparation source changed during feature computation')
        if time.monotonic() - started > _bounds(request):
            raise ValueError('preparation time budget exceeded; cache cannot be registered')
        return 0, {'schema': 'm2-scr2n-user-preparation-receipt/1', 'ok': True,
            'recipe_id': request['recipe_id'], 'objective_profile': OBJECTIVE_PROFILE,
            'user_id': request['user_id'], 'style_id': request['style_id'],
            'snapshot_binding': verified['snapshot_binding'], 'scr_data_binding': expected,
            'scr_cache_binding': prepared['cache_binding'], 'runtime_binding': request['runtime_binding'],
            'source_hashes': source, 'elapsed_seconds': time.monotonic() - started,
            'max_seconds': _bounds(request), 'synthetic_test_only': synthetic,
            'feature_execution_performed': True, 'm2_launch_authorized': False,
            'training_performed': False, 'inference_performed': False, 'claims_quality_pass': False}
    except Exception as exc:
        return 3, {'schema': 'm2-scr2n-user-preparation-receipt/1', 'ok': False,
            'recipe_id': request.get('recipe_id'), 'objective_profile': OBJECTIVE_PROFILE,
            'failure': {'code': getattr(exc, 'code', 'E_USER_SCR_PREPARATION'), 'detail': str(exc)},
            'synthetic_test_only': synthetic, 'm2_launch_authorized': False,
            'training_performed': False, 'inference_performed': False, 'claims_quality_pass': False}


def main(argv=None):
    from .release_runtime_policy import assert_m2_execution_allowed
    from .runtime_variants import RuntimeUnavailable
    from .personal_runtime_policy import present,bootstrap
    try:
        if present(APP):bootstrap(APP)
        else:assert_m2_execution_allowed(APP)
    except (RuntimeUnavailable,OSError) as exc:
        result={"schema":"m2-scr2n-user-preparation-receipt/1","ok":False,"status":"BLOCKED","failure":{"code":getattr(exc,'code','BLOCKED_ENV'),"detail":str(exc)},"feature_execution_performed":False,"m2_launch_authorized":False,"training_performed":False,"inference_performed":False,"claims_quality_pass":False}
        print(json.dumps(result,ensure_ascii=False));return 3
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        return 3
    try:
        request = json.loads(Path(argv[0]).read_text(encoding='utf-8'))
        assert_m2_execution_allowed(APP,binding=request.get('runtime_binding'),purpose='prepare')
        receipt = Path(request['out_receipt'])
        if not receipt.is_absolute() or receipt.exists() or any((p / 'FROZEN').exists() for p in receipt.resolve().parents):
            raise ValueError('new absolute receipt path outside frozen snapshots required')
        _validate_request(request)
    except (OSError, ValueError, TypeError, KeyError):
        return 3
    from .runner import start_scr_resource_guard
    guard = start_scr_resource_guard(receipt.parent, seconds=_bounds(request))
    try:
        code, result = run(request)
    finally:
        guard[0].set()
        guard[1].join(timeout=1)
    result['resource_guard'] = guard[2]
    receipt.parent.mkdir(parents=True, exist_ok=True)
    with receipt.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
