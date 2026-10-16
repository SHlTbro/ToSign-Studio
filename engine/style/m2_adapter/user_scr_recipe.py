"""Bounded, data-derived SCR recipe planning; no unqualified runtime activation.

The cloud CPU profile is supported but never qualified by its name. Explicit
registration validates fresh exact-source component evidence before execution.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from .control_recipe import (RECIPE_ID as BASELINE_RECIPE_ID,
    OBJECTIVE_PROFILE as BASELINE_OBJECTIVE, PREPROCESSING_PROFILE, CHECKPOINT_SELECTION,
    resolve_control_recipe, validate_adapter_recipe_metadata)
from .spatial_topology import topology_fields, validate_spatial_topology_fields
from .user_scr_contract import (RECIPE_ID, OBJECTIVE_PROFILE, asset_contract, objective_contract,
    digest, sealed, validate_cache_binding, validate_data_binding,
    validate_user_scr_asset_path_shape, validate_user_scr_provenance, user_scr_identity)

# Support is a schema dispatch allowlist, not a qualification or readiness claim.
from .user_scr_capability import VARIANT_ID
SUPPORTED_RUNTIME_VARIANTS = frozenset({VARIANT_ID})


def plan_user_scr_recipe(train_count, config=None):
    """Pure bounded plan from a verified train count; cannot authorize execution."""
    config = {} if config is None else config
    if not isinstance(config, dict):
        raise ValueError('user SCR config must be an object')
    for key, wanted in (('recipe_id', RECIPE_ID), ('objective_profile', OBJECTIVE_PROFILE),
                        ('preprocessing_profile', PREPROCESSING_PROFILE),
                        ('checkpoint_selection', CHECKPOINT_SELECTION)):
        if key in config and config[key] != wanted:
            raise ValueError('user SCR fixes ' + key)
    base_input = copy.deepcopy(config)
    for key in ('training_recipe', 'training_budget'):
        base_input.pop(key, None)
    base_input.update(recipe_id=BASELINE_RECIPE_ID, objective_profile=BASELINE_OBJECTIVE)
    resolved = resolve_control_recipe(train_count, base_input)
    baseline_recipe = resolved['training_recipe']
    body = {k: v for k, v in baseline_recipe.items() if k != 'sha256'}
    body.update(recipe_id=RECIPE_ID, objective_profile=OBJECTIVE_PROFILE,
        **topology_fields(), scr_objective=objective_contract(), scr_assets=asset_contract(),
        loss='mean epsilon MSE + 0.1 * mean(offset_sum ** 2) + 0.01 * mean four-layer two-negative paired InfoNCE',
        SCR_asset_required_for_training=True, SCR_asset_required_for_inference=False,
        dataset_binding='fresh canonical S03; all ordered occurrence, parent/family, split and target hashes',
        cache_binding='new complete verified per-snapshot reference cache; historical vectors forbidden',
        implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        baseline_recipe_sha256=baseline_recipe['sha256'])
    resolved.update(recipe_id=RECIPE_ID, objective_profile=OBJECTIVE_PROFILE,
        **topology_fields(), scr_objective=objective_contract(), scr_assets=asset_contract(),
        training_recipe=sealed(body), production_qualified=False,
        qualification_status='REQUIRES_VERIFIED_EXPLICIT_REGISTRATION')
    for name in ('training_recipe', 'training_budget'):
        if name in config and digest(config[name]) != digest(resolved[name]):
            raise ValueError('user SCR ' + name + ' differs from canonical bounded recipe')
    return resolved


def validate_user_scr_binding(binding):
    from .runtime_variants import validate_binding_shape
    validate_binding_shape(binding)
    if binding.get('capability_receipt_sha256') is None:
        raise ValueError('user SCR requires an exact-source measured capability receipt')
    if binding['variant_id'] not in SUPPORTED_RUNTIME_VARIANTS:
        raise ValueError('E_SCR_RUNTIME_UNQUALIFIED: unsupported profile for this new user SCR recipe')
    return binding


def resolve_user_scr_recipe(train_count, config=None):
    if not isinstance(config, dict) or config.get('recipe_id') != RECIPE_ID:
        raise ValueError('explicit user SCR recipe required')
    validate_spatial_topology_fields(config)
    identity = user_scr_identity(config)
    data, cache = identity['scr_data_binding'], identity['scr_cache_binding']
    if type(train_count) is not int or train_count != data['counts']['train']:
        raise ValueError('user SCR train count differs from verified S03 data binding')
    if config.get('snapshot_sha256') != data['snapshot_sha256']:
        raise ValueError('user SCR snapshot differs from data/cache binding')
    paths = validate_user_scr_asset_path_shape(config.get('scr_asset_paths'))
    plan = plan_user_scr_recipe(train_count, config)
    binding = validate_user_scr_binding(config.get('runtime_binding'))
    if plan['train']['device'] != binding['device']:
        raise ValueError('user SCR device differs from registered runtime')
    return {**plan, 'scr_asset_paths': paths, **identity,
            'qualification_status': 'EXPLICIT_REGISTRATION_BOUND'}


def validate_user_scr_adapter_metadata(meta, train_count):
    """No existing adapter may claim the new recipe before its runtime qualifies."""
    if not isinstance(meta, dict):
        raise ValueError('user SCR metadata must be an object')
    identity = user_scr_identity(meta)
    validate_spatial_topology_fields(meta)
    binding = validate_user_scr_binding(meta.get('runtime_binding'))
    if (meta.get('recipe_id') != RECIPE_ID or meta.get('objective_profile') != OBJECTIVE_PROFILE
            or type(train_count) is not int or train_count != identity['scr_data_binding']['counts']['train']):
        raise ValueError('user SCR metadata recipe/count differs')
    budget = meta.get('training_budget')
    if not isinstance(budget, dict):
        raise ValueError('user SCR metadata budget missing')
    resolved = {**plan_user_scr_recipe(train_count, {'training_recipe': meta.get('training_recipe'),
        'training_budget': budget, 'train': {'max_seconds': budget.get('max_seconds'), 'device': binding['device']}}), **identity}
    selected = meta.get('selection')
    if not isinstance(selected, dict) or any(type(selected.get(k)) is not int for k in ('global_step', 'last_completed_epoch')):
        raise ValueError('user SCR terminal counts must be integers')
    for key, expected in (('n_hook_points', 22), ('n_offset_projection_heads', 0), ('n_spatial_offset_heads', 6)):
        if type(meta.get(key)) is not int or meta[key] != expected:
            raise ValueError('user SCR actual topology head count differs')
    objective = meta.get('training_objective')
    if not isinstance(objective, dict) or objective.get('schema') != 'm2-objective/5-scr2n-user':
        raise ValueError('user SCR objective lineage missing')
    validate_user_scr_provenance(objective)
    validate_spatial_topology_fields(objective)
    if (objective.get('recipe_id') != RECIPE_ID or objective.get('objective_profile') != OBJECTIVE_PROFILE
            or objective.get('scr_data_binding') != resolved['scr_data_binding']
            or objective.get('scr_cache_binding') != resolved['scr_cache_binding']
            or objective.get('scr_preparation_binding') != resolved['scr_preparation_binding']):
        raise ValueError('user SCR objective per-run lineage differs')
    view = copy.deepcopy(meta)
    view.update(recipe_id=BASELINE_RECIPE_ID, objective_profile=BASELINE_OBJECTIVE,
        training_recipe=resolve_control_recipe(train_count, {'train': {
            'max_seconds': resolved['training_budget']['max_seconds']}})['training_recipe'])
    view['training_objective'].update(schema='m2-objective/3-control', recipe_id=BASELINE_RECIPE_ID,
                                      objective_profile=BASELINE_OBJECTIVE)
    validate_adapter_recipe_metadata(view, train_count)
    return {key: meta[key] for key in ('recipe_id', 'objective_profile', 'checkpoint_selection',
                'scr_objective', 'scr_assets', 'scr_data_binding', 'scr_cache_binding', 'scr_preparation_binding')}
