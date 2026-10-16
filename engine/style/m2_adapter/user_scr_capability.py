"""Model-free admission of measured, source-bound generic SCR CPU capability.

The supported profile is never an implicit runtime. Only the existing explicit
registration workflow can admit a complete receipt. A component proof makes no
quality, repeat-determinism, full-Trainer-resume, or publication claim.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import re

from .user_scr_contract import (RECIPE_ID, OBJECTIVE_PROFILE, asset_contract,
    objective_contract, validate_data_binding, validate_cache_binding,
    validate_user_scr_provenance, project_user_scr_execution_evidence)

VARIANT_ID = 'cloud-linux-cp312-user-scr2n-spatial-v1'
CAPABILITY_SCHEMA = 'm2-scr2n-user-runtime-capability/1'
ADMISSION_POLICY = 'explicit-verified-component-registration-v1'
PRODUCER_SHA256 = '8fe11f5f655dcd8eba237f3728d5fc14e40c1dfdcfedcd4b7b695bfe697858ad'
FEATURE_PATH_KEYS = ('module', 'checkpoint', 'state_schema', 'font_sans', 'font_serif')
EXECUTION_FILES = ('installer/probe_runtime_variant.py', 'installer/runtime_variant.py',
    'requirements-personal-style-runtime.lock')


def _need(condition, detail, code='E_USER_SCR_CAPABILITY'):
    from .runtime_variants import require
    require(condition, code, detail)


def _same(left, right):
    from .runtime_variants import _strict_equal
    return _strict_equal(left, right)


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _finite(value, *, positive=False):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        return False


def execution_hashes(app):
    from .runtime_variants import safe_relative, sha
    return {rel: sha(safe_relative(app, rel)) for rel in EXECUTION_FILES}


def profile_for_source(app):
    """Prepare disabled catalog metadata before measuring this exact source.

    This pure builder performs no registration, environment probe, or model work.
    Rebuilding pins after a source edit invalidates earlier capability receipts.
    """
    from .runtime_variants import value_sha, sha, safe_relative, _engine_inventory
    from .user_scr_runtime import user_scr_source_hashes
    from .spatial_topology import topology_fields
    # Mirror the public policy metadata without importing the Torch operator.
    policy = {'schema': 'r9-operator-execution/1', 'policy': 'cpu-deform-forward-backward-fp32-v1',
        'operator': 'torchvision.ops.deform_conv2d',
        'execution': 'CPU forward and backward; differentiable device copies',
        'precision': 'float32 without autocast or detach',
        'scope': 'same six frozen-base modules for training and inference',
        'implementation_sha256': sha(safe_relative(app, 'engine/style/m2_adapter/deterministic_ops.py'))}
    body = {'device': 'cpu', 'python': '3.12', 'platforms': ['linux'],
        'recipe_id': RECIPE_ID, 'objective_profile': OBJECTIVE_PROFILE,
        'full_model_probe_required': True, 'execution_enabled': False,
        'registration_admission': ADMISSION_POLICY, 'capability_schema': CAPABILITY_SCHEMA,
        'capability_producer_sha256': PRODUCER_SHA256,
        'lock': 'requirements-personal-style-runtime.lock',
        'lock_sha256': sha(safe_relative(app, 'requirements-personal-style-runtime.lock')),
        'label': 'Cloud Linux CPU generic user SCR; explicit measured registration only',
        'max_memory_bytes': 5 * 2**30, 'operator_policy': policy,
        'scr_assets': asset_contract(), 'scr_objective': objective_contract(), **topology_fields(),
        'required_engine_files': _engine_inventory(app), 'required_source_files': user_scr_source_hashes(app),
        'required_execution_files': execution_hashes(app),
        'environment_probe_sha256': sha(safe_relative(app, EXECUTION_FILES[0])),
        'execution_blocker': 'No implicit activation; explicit exact-source component receipt and registration required',
        'capability_scope': 'fresh reference cache, two optimizer updates, small state roundtrip, one native generation',
        'not_claimed': ['quality approval', 'repeat determinism', 'full Trainer resume', 'Windows or CUDA support']}
    return {**body, 'execution_profile_sha256': value_sha(body)}


def build_capability_receipt(app, component, resource_receipt, *, python,
        binding_environment, dependencies, runtime_identity_sha256, scr_feature_asset_paths):
    """Package actual observations; this neither verifies nor imports a receipt.

    Scope flags below are negative claims. Positive measured observations are
    copied unchanged from the caller and checked independently at registration.
    """
    import copy
    from .runtime_variants import specification, sha
    from .user_scr_runtime import user_scr_source_hashes
    _, spec = specification(app, VARIANT_ID)
    return copy.deepcopy({'schema': CAPABILITY_SCHEMA, 'status': 'PASS_MODEL_CORE_ONLY',
        'variant_id': VARIANT_ID, 'recipe_id': RECIPE_ID, 'objective_profile': OBJECTIVE_PROFILE,
        'device': 'cpu', 'synthetic_test_only': False, 'claims_quality_pass': False,
        'may_publish': False, 'm2_launch_authorized': False, 'repeat_determinism_claimed': False,
        'full_trainer_resume_claimed': False, 'execution_profile_sha256': spec['execution_profile_sha256'],
        'dependency_lock_sha256': spec['lock_sha256'], 'runtime_identity_sha256': runtime_identity_sha256,
        'executable': str(Path(python).absolute()), 'python_sha256': sha(python),
        'dependencies': dependencies, 'binding_environment': binding_environment,
        'numerical_environment': component.get('numerical_environment'),
        'source_hashes': user_scr_source_hashes(app), 'execution_hashes': execution_hashes(app),
        'scr_assets': asset_contract(), 'scr_objective': objective_contract(),
        'scr_feature_asset_paths': scr_feature_asset_paths, 'producer_sha256': component.get('driver_sha256'),
        'component': component, 'resource_receipt': resource_receipt})


def validate_profile(spec):
    from .spatial_topology import topology_fields
    fixed = {'device': 'cpu', 'python': '3.12', 'platforms': ['linux'],
        'recipe_id': RECIPE_ID, 'objective_profile': OBJECTIVE_PROFILE,
        'full_model_probe_required': True, 'execution_enabled': False,
        'registration_admission': ADMISSION_POLICY, 'capability_schema': CAPABILITY_SCHEMA,
        'capability_producer_sha256': PRODUCER_SHA256,
        'lock': 'requirements-personal-style-runtime.lock', 'max_memory_bytes': 5 * 2**30,
        'scr_assets': asset_contract(), 'scr_objective': objective_contract(), **topology_fields()}
    _need(all(_same(spec.get(k), v) for k, v in fixed.items()),
        'generic CPU profile must keep its exact disabled, explicit-registration policy', 'E_USER_SCR_PROFILE')


def verify_feature_paths(paths):
    """Hash registered official assets; never import their code or load weights."""
    from .runtime_variants import sha
    _need(isinstance(paths, dict) and set(paths) == set(FEATURE_PATH_KEYS), 'exact five registered feature/font paths required')
    assets = asset_contract()
    for key in FEATURE_PATH_KEYS:
        value = paths[key]
        _need(isinstance(value, str) and Path(value).is_absolute(), 'absolute feature asset path required: ' + key)
        path = Path(value)
        expected = (assets['font_sha256'][('font_sans', 'font_serif').index(key)]
            if key.startswith('font_') else assets[key + '_sha256'])
        _need(path.is_file() and sha(path) == expected, 'registered SCR/font bytes changed: ' + key, 'E_SCR_PROVENANCE')
        _need(str(path.resolve()) == value, 'registered feature path must be canonical: ' + key)
    _need(Path(paths['checkpoint']).stat().st_size == assets['checkpoint_size_bytes'], 'SCR checkpoint size differs')
    return paths


def _environment(app, spec, proof, observation, python):
    from .runtime_variants import value_sha, versions_from_lock, safe_relative, sha
    _need(isinstance(observation, dict), 'fresh environment observation required')
    executable = proof.get('executable')
    _need(isinstance(executable, str) and Path(executable).is_absolute()
        and os.path.normcase(os.path.abspath(executable)) == os.path.normcase(str(Path(python).absolute())),
        'receipt belongs to a different interpreter', 'E_PROBE_INTERPRETER')
    _need(proof.get('python_sha256') == sha(python), 'receipt interpreter bytes changed', 'E_INTERPRETER_CHANGED')
    versions = versions_from_lock(safe_relative(app, spec['lock']))
    _need(_same(proof.get('dependencies'), versions) and _same(observation.get('versions'), versions),
        'all package versions must match the CPU lock', 'E_DEPENDENCY_VERSIONS')
    basic, numeric = proof.get('binding_environment'), proof.get('numerical_environment')
    basic_keys = {'schema', 'versions', 'python', 'platform', 'device', 'cuda_runtime', 'gpu_name',
        'freetype_version', 'torch_cpu_threads', 'torch_interop_threads', 'deterministic_algorithms', 'dtype', 'identity_sha256'}
    numeric_keys = {'schema', 'python', 'platform', 'packages', 'device', 'cuda_runtime', 'cudnn_version', 'gpu_name',
        'freetype_version', 'torch_deterministic_algorithms', 'matmul_tf32', 'cudnn_tf32', 'dtype', 'amp',
        'default_dtype', 'cublas_workspace_config', 'cudnn_benchmark', 'cudnn_deterministic', 'torch_cpu_threads',
        'torch_interop_threads', 'operator_execution_policy', 'mixed_cuda_cpu_deform', 'identity_sha256'}
    _need(isinstance(basic, dict) and set(basic) == basic_keys and isinstance(numeric, dict) and set(numeric) == numeric_keys,
        'complete basic and full numerical environment observations required', 'E_NUMERICAL_ENVIRONMENT')
    for row in (basic, numeric):
        _need(row['identity_sha256'] == value_sha({k: v for k, v in row.items() if k != 'identity_sha256'}), 'environment hash is malformed')
    _need(basic['identity_sha256'] == observation.get('environment_identity_sha256')
        and _same(numeric, observation.get('numerical_environment')),
        'fresh numerical environment differs from measured receipt', 'E_NUMERICAL_ENVIRONMENT_CHANGED')
    fixed = {'schema': 'r9-numerical-environment/2', 'device': 'cpu', 'cuda_runtime': None,
        'gpu_name': None, 'dtype': 'float32', 'default_dtype': 'torch.float32', 'amp': False,
        'torch_deterministic_algorithms': True, 'matmul_tf32': False, 'cudnn_tf32': False,
        'cudnn_benchmark': False, 'cudnn_deterministic': True, 'torch_cpu_threads': 2,
        'torch_interop_threads': 1, 'mixed_cuda_cpu_deform': False, 'cublas_workspace_config': ':4096:8',
        'operator_execution_policy': spec['operator_policy']}
    _need(all(_same(numeric.get(k), v) for k, v in fixed.items())
        and str(numeric['platform']).startswith('Linux-') and str(numeric['python']).startswith('3.12.'),
        'cloud Linux deterministic FP32 CPU environment required', 'E_NUMERICAL_POLICY')
    names = ('torch', 'torchvision', 'diffusers', 'transformers', 'accelerate', 'numpy', 'pillow', 'pygame', 'safetensors', 'fonttools', 'huggingface-hub')
    _need(_same(numeric['packages'], {k: versions[k] for k in names}), 'numerical packages differ from lock')
    _need(basic['schema'] == 'm2-basic-numerical-environment/1' and _same(basic['versions'], versions), 'basic environment schema differs')
    mapping = {'python': 'python', 'platform': 'platform', 'device': 'device', 'cuda_runtime': 'cuda_runtime',
        'gpu_name': 'gpu_name', 'freetype_version': 'freetype_version', 'torch_cpu_threads': 'torch_cpu_threads',
        'torch_interop_threads': 'torch_interop_threads', 'deterministic_algorithms': 'torch_deterministic_algorithms', 'dtype': 'default_dtype'}
    _need(all(_same(basic[k], numeric[v]) for k, v in mapping.items()), 'basic and full environment disagree')
    return basic['identity_sha256']


def _component(component, proof):
    from .spatial_topology import SPATIAL_HEADS
    from .user_scr_runtime import verify_user_scr_training_evidence
    fixed = {'schema': 'generic-scr-qualification-core-trial/1', 'status': 'PASS_MODEL_CORE_ONLY',
        'synthetic_test_only': False, 'production_runtime_qualified': False, 'registered_runtime_created': False,
        'preparation_authority_created': False, 'training_candidate_created': False, 'full_trainer_resume_claimed': False,
        'may_publish': False, 'claims_quality_pass': False, 'device': 'cpu', 'threads': 2,
        'interop_threads': 1, 'optimizer_steps': 2, 'generation_calls': 1}
    _need(isinstance(component, dict) and all(_same(component.get(k), v) for k, v in fixed.items()),
        'complete actual bounded component evidence required; historical capability schemas cannot substitute')
    validate_user_scr_provenance(component)
    data = validate_data_binding(component.get('scr_data_binding'))
    cache = validate_cache_binding(component.get('scr_cache_binding'), data)
    batch = component.get('fixed_batch', {}).get('rows')
    indexed = {row['sample_id']: row for row in data['rows']}
    _need(isinstance(batch, list) and len(batch) == 2 and len({row.get('sample_id') for row in batch}) == 2,
        'two distinct actual optimizer occurrences required')
    for row in batch:
        found = indexed.get(row.get('sample_id'), {})
        _need(found.get('split') == 'train' and row.get('label') == found.get('label')
            and row.get('png_sha256') == found.get('source_png_sha256'), 'optimizer occurrence is not the bound training row')
    _need(component.get('executable') == proof['executable'] and component.get('source_hashes') == proof['source_hashes']
        and _same(component.get('numerical_environment'), proof['numerical_environment']), 'component source/interpreter/environment differs')
    _need(component.get('driver_sha256') == proof.get('producer_sha256') == PRODUCER_SHA256,
        'component producer differs from the exact pinned driver')
    _need(_same(component.get('snapshot_binding'), component.get('scr_execution_evidence', {}).get('snapshot_binding')), 'component snapshot differs from objective')
    projected = {**component, **project_user_scr_execution_evidence(component.get('scr_execution_evidence')),
        'dataset_snapshot_sha256': data['snapshot_sha256']}
    verify_user_scr_training_evidence(projected, core=True, require_preparation=False)
    reference = component.get('reference_feature_execution', {})
    labels = len({row['label'] for row in data['rows']})
    for key, value in {'mode': 'frozen_eval_fp32', 'author_images': data['counts']['total'], 'negative_images': 2 * labels,
            'reference_feature_forwards': data['counts']['total'] + 2 * labels, 'reused_historical_vectors': False,
            'rng_preserved': True, 'synthetic_test_only': False}.items():
        _need(_same(reference.get(key), value), 'fresh reference computation evidence differs: ' + key)
    before = reference.get('producer_state_before_sha256')
    _need(_hash(before) and before == reference.get('producer_state_after_sha256'), 'reference encoder state changed')
    steps = component.get('steps')
    _need(isinstance(steps, list) and len(steps) == 2, 'exactly two measured optimizer updates required')
    for index, row in enumerate(steps, 1):
        _need(isinstance(row, dict) and type(row.get('step')) is int and row['step'] == index, 'ordered optimizer steps required')
        _need(all(_finite(row.get(k)) for k in ('epsilon_mse', 'offset_term', 'scr_raw', 'scr_term', 'total', 'unclipped_gradient_norm'))
            and _finite(row.get('seconds'), positive=True) and _finite(row.get('scr_query_gradient_abs_sum'), positive=True),
            'finite losses and live nonzero SCR query gradients must be measured')
        _need(math.isclose(row['scr_term'], .01 * row['scr_raw'], rel_tol=1e-6, abs_tol=1e-8)
            and math.isclose(row['total'], row['epsilon_mse'] + row['offset_term'] + row['scr_term'], rel_tol=1e-5, abs_tol=1e-7),
            'fixed SCR coefficient or total loss differs')
        _need(type(row.get('nonzero_adapter_gradient_tensors')) is int and row['nonzero_adapter_gradient_tensors'] == 56
            and row.get('base_gradients_absent') is True, 'all 56 adapter gradients and absent base gradients required')
    state = component.get('base_state_before_sha256')
    _need(_hash(state) and state == component.get('base_state_after_sha256'), 'base changed through training/generation')
    delta = component.get('delta_roundtrip', {})
    _need(delta.get('weights_only') is True and delta.get('equal') is True and delta.get('full_trainer_resume_claimed') is False
        and type(delta.get('bytes')) is int and 0 < delta['bytes'] <= 128 * 2**20
        and all(_hash(delta.get(k)) for k in ('adapter_sha256', 'optimizer_sha256', 'state_file_sha256')),
        'small weights-only adapter/optimizer state roundtrip required')
    generation = component.get('generation', {})
    _need(type(generation.get('steps')) is int and generation['steps'] == 20 and _same(generation.get('output_count'), 1)
        and generation.get('not_blind_quality_evidence') is True and generation.get('pixels_saved_before_guard') is True,
        'one native 20-step generation with saved original-guard pixels required')
    _need(component.get('inference_scr_feature_forwards') == 0 and type(component.get('inference_scr_feature_forwards')) is int,
        'generation must measure zero SCR feature forwards')
    reference_row = generation.get('reference', {})
    found = indexed.get(reference_row.get('sample_id'), {})
    _need(found.get('split') == 'train' and reference_row.get('label') == found.get('label')
        and reference_row.get('png_sha256') == found.get('source_png_sha256'), 'generation reference must be a bound training occurrence')
    outputs = generation.get('outputs')
    _need(isinstance(outputs, list) and len(outputs) == 1, 'one observed native output required')
    _verify_single_output(outputs[0])
    heads = {row['module'] for row in SPATIAL_HEADS}
    counters = component.get('spatial_generation_hook_calls')
    _need(isinstance(counters, dict) and set(counters) == heads and all(type(v) is int and v == 20 for v in counters.values()), 'six spatial heads must execute 20 generation forwards')
    counts = component.get('operator_execution_counts')
    modules = {f'unet.up_blocks.{block}.dcn_deforms.{i}' for block in (1, 2) for i in range(3)}
    _need(isinstance(counts, dict) and set(counts) == modules and all(isinstance(row, dict)
        and type(row.get('forward_calls')) is int and row['forward_calls'] >= 22
        and type(row.get('cross_device_calls')) is int and row['cross_device_calls'] == 0 for row in counts.values()),
        'all six CPU deform operators must include optimizer and generation forwards')
    _need(cache['data_binding_sha256'] == data['sha256'], 'component cache differs')


def _verify_single_output(row):
    # Same unchanged pixel limits as the historical native output guard,
    # applied to exactly one observed image; no repeat is fabricated.
    fields = {'index', 'width', 'height', 'raw_rgb_file', 'raw_rgb_sha256', 'alpha_file',
        'alpha_sha256', 'alpha_min', 'alpha_max', 'alpha_sum', 'opaque_pixels_threshold128',
        'opaque_fraction', 'passes_original_ink_guard', 'pixels_saved_before_guard'}
    _need(isinstance(row, dict) and set(row) == fields, 'complete single native pixel record required')
    _need(all(type(row.get(k)) is int for k in ('index', 'width', 'height', 'alpha_min', 'alpha_max', 'alpha_sum', 'opaque_pixels_threshold128'))
        and row['index'] == 0 and row['width'] == row['height'] == 96, 'ordered native 96px output required')
    _need(row['raw_rgb_file'] == 'probe-train-glyph-0-raw-rgb.png' and row['alpha_file'] == 'probe-train-glyph-0.png'
        and _hash(row['raw_rgb_sha256']) and _hash(row['alpha_sha256']), 'native output filename/hash differs')
    _need(row['passes_original_ink_guard'] is True and row['pixels_saved_before_guard'] is True, 'original ink guard must pass after saving pixels')
    low, high, total, opaque = (row[k] for k in ('alpha_min', 'alpha_max', 'alpha_sum', 'opaque_pixels_threshold128'))
    pixels = 96 * 96
    _need(0 <= low < 128 <= high <= 255 and 16 <= opaque <= .6 * pixels, 'unchanged native ink limits failed')
    _need(high + (opaque - 1) * 128 + (pixels - opaque) * low <= total
        <= opaque * high + (pixels - opaque - 1) * 127 + low, 'alpha sum contradicts extrema/count')
    _need(type(row['opaque_fraction']) is float and math.isfinite(row['opaque_fraction'])
        and row['opaque_fraction'] == opaque / pixels, 'opaque fraction differs from pixel count')


def _resources(resource):
    _need(isinstance(resource, dict) and resource.get('schema') == 'generic-scr-trial-resource-receipt/1'
        and type(resource.get('returncode')) is int and resource['returncode'] == 0
        and resource.get('stop_reason') is None and resource.get('monitor_failure') is None
        and resource.get('cleanup_failures') == [], 'successful monitored resource receipt required')
    fixed = {'stop_bytes': int(4.5 * 2**30), 'absolute_budget_bytes': 5 * 2**30,
        'max_new_output_bytes': 128 * 2**20, 'existing_tmp_data_deleted': False,
        'only_identity_verified_owned_processes_terminated': True, 'retry_count': 0}
    _need(all(_same(resource.get(k), v) for k, v in fixed.items()), 'resource guard limits or scope differ')
    peak = resource.get('peak', {})
    _need(all(type(peak.get(k)) is int and peak[k] >= 0 for k in ('tree_rss_bytes', 'tmp_used_bytes', 'combined_bytes', 'new_output_bytes'))
        and 0 < peak['tree_rss_bytes'] <= peak['combined_bytes'] < 4.5 * 2**30
        and peak['new_output_bytes'] <= 128 * 2**20 and _finite(resource.get('elapsed_seconds'), positive=True)
        and resource['elapsed_seconds'] <= 900, 'bounded observed memory, output, and elapsed time required')


def verify_capability(app, spec, proof, environment, python, *, verify_feature_assets=True):
    from .runtime_variants import catalog, value_sha, sha, safe_relative, _engine_inventory
    from .user_scr_runtime import user_scr_source_hashes
    validate_profile(spec)
    _need(_same(spec, profile_for_source(app)), 'catalog differs from exact canonical source profile', 'E_PROBE_CODE_CHANGED')
    fixed = {'schema': CAPABILITY_SCHEMA, 'status': 'PASS_MODEL_CORE_ONLY', 'variant_id': VARIANT_ID,
        'recipe_id': RECIPE_ID, 'objective_profile': OBJECTIVE_PROFILE, 'device': 'cpu',
        'synthetic_test_only': False, 'claims_quality_pass': False, 'may_publish': False,
        'm2_launch_authorized': False, 'repeat_determinism_claimed': False, 'full_trainer_resume_claimed': False}
    _need(isinstance(proof, dict) and all(_same(proof.get(k), v) for k, v in fixed.items()), 'new generic component capability receipt required')
    _need(proof.get('execution_profile_sha256') == spec['execution_profile_sha256']
        == value_sha({k: v for k, v in spec.items() if k != 'execution_profile_sha256'}), 'receipt execution profile differs')
    _need(proof.get('runtime_identity_sha256') == catalog(app)['asset_identity_sha256']
        and proof.get('dependency_lock_sha256') == spec['lock_sha256'] == sha(safe_relative(app, spec['lock'])), 'base assets or lock differs')
    source = user_scr_source_hashes(app)
    _need(proof.get('source_hashes') == spec.get('required_source_files') == source
        and spec.get('required_engine_files') == _engine_inventory(app)
        and proof.get('execution_hashes') == spec.get('required_execution_files') == execution_hashes(app),
        'final source inventory differs from measured receipt and catalog', 'E_PROBE_CODE_CHANGED')
    _need(spec.get('environment_probe_sha256') == sha(safe_relative(app, EXECUTION_FILES[0])), 'environment observer source changed')
    validate_user_scr_provenance(proof)
    identity = _environment(app, spec, proof, environment, python)
    _component(proof.get('component'), proof)
    _resources(proof.get('resource_receipt'))
    if verify_feature_assets:
        verify_feature_paths(proof.get('scr_feature_asset_paths'))
    return identity
