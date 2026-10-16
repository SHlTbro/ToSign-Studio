"""Stdlib-only, explicit product recipe derived from the reviewed R9 control.

This validates new product training requests. Historical adapters keep their own
recorded preprocessing; resolving a recipe does not migrate or publish a model.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path

RECIPE_ID = 'm2-control-corrected-v1'
OBJECTIVE_PROFILE = 'epsilon-offset-control-v1'
PREPROCESSING_PROFILE = 'fontdiffuser-freetype128-blackwhite-v1'
CHECKPOINT_SELECTION = 'terminal_fixed_budget'
FIXED_TRAIN = {'max_epochs': 8, 'batch': 2, 'lr': 1e-4, 'seed': 20261001,
               'patience': 9, 'conditioning_dropout': 0.1, 'gradient_clip_norm': 1.0,
               'perceptual_coefficient': 0.0, 'cpu_threads': 2, 'max_memory_gib': 5.0}
TRAIN_KEYS = set(FIXED_TRAIN) | {'max_steps', 'max_seconds', 'device'}

def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()

def recipe_contract():
    body = {'schema': 'm2-training-recipe/1', 'recipe_id': RECIPE_ID, 'objective_profile': OBJECTIVE_PROFILE,
            'preprocessing_profile': PREPROCESSING_PROFILE, 'checkpoint_selection': CHECKPOINT_SELECTION,
            'fixed_train': dict(FIXED_TRAIN), 'optimizer': 'AdamW', 'weight_decay': 0.01,
            'prediction': 'epsilon', 'loss': 'mean epsilon MSE + 0.1 * mean(offset_sum ** 2)',
            'VGG_asset_required': False, 'VGG_forward_or_gradient_used': False,
            'train_loader': {'shuffle': True, 'drop_last': True, 'num_workers': 0},
            'max_steps_formula': '8 * floor(verified_train_count / 2)',
            'validation': 'fixed per-sample noise and training-side style references, after every complete epoch; exported terminal state must have matching final validation',
            'budget_failure': 'incomplete epoch/step/time budget is a failed run, not a complete candidate',
            'precision': {'dtype': 'float32', 'AMP': False, 'TF32': False, 'deterministic_algorithms': True},
            'operator_policy': 'cpu-deform-forward-backward-fp32-v1',
            'implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    return {**body, 'sha256': _digest(body)}

def resolve_control_recipe(train_count, config=None):
    """Return canonical recipe fields; never mutate config or import ML packages."""
    if not isinstance(train_count, int) or isinstance(train_count, bool) or train_count < 8:
        raise ValueError('verified train_count must be an integer >=8')
    config = {} if config is None else config
    if not isinstance(config, dict):
        raise ValueError('training config must be an object')
    for name, wanted in [('recipe_id', RECIPE_ID), ('objective_profile', OBJECTIVE_PROFILE),
                         ('preprocessing_profile', PREPROCESSING_PROFILE), ('checkpoint_selection', CHECKPOINT_SELECTION)]:
        if name in config and config[name] != wanted:
            raise ValueError('new control training requires ' + name + '=' + wanted)
    supplied = config.get('train', {})
    if not isinstance(supplied, dict) or set(supplied) - TRAIN_KEYS:
        raise ValueError('unknown or malformed control training fields')
    train = dict(FIXED_TRAIN)
    for name, wanted in FIXED_TRAIN.items():
        if name in supplied:
            value = supplied[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != wanted or not math.isfinite(value):
                raise ValueError('control recipe fixes train.' + name + '=' + str(wanted))
    steps = FIXED_TRAIN['max_epochs'] * (train_count // FIXED_TRAIN['batch'])
    if 'max_steps' in supplied and (isinstance(supplied['max_steps'], bool) or not isinstance(supplied['max_steps'], int) or supplied['max_steps'] != steps):
        raise ValueError('max_steps must equal8 * floor(verified_train_count /2)=' + str(steps))
    seconds = supplied.get('max_seconds', 900.0)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= 86400 or not math.isfinite(seconds):
        raise ValueError('max_seconds must be finite within (0,86400]')
    device = supplied.get('device', 'cpu')
    if device not in ('cpu', 'cuda'):
        raise ValueError('device must be the exact cpu or cuda profile')
    train.update(max_steps=steps, max_seconds=float(seconds), device=device)
    contract = recipe_contract()
    budget = {'schema': 'm2-control-budget/1', 'verified_train_count': train_count,
              'batch': 2, 'drop_last': True, 'steps_per_epoch': train_count // 2,
              'expected_complete_epochs': 8, 'expected_optimizer_steps': steps,
              'max_seconds': float(seconds), 'terminal_validation_required': True}
    return {'recipe_id': RECIPE_ID, 'objective_profile': OBJECTIVE_PROFILE,
            'preprocessing_profile': PREPROCESSING_PROFILE, 'checkpoint_selection': CHECKPOINT_SELECTION,
            'train': train, 'training_recipe': contract, 'training_budget': budget}

def validate_completed_budget(epochs, global_step, validation_matches_export, budget):
    if epochs != budget['expected_complete_epochs'] or global_step != budget['expected_optimizer_steps'] or validation_matches_export is not True:
        raise ValueError('E_TRAINING_BUDGET_INCOMPLETE: all registered epochs/steps and matching terminal validation are required')

def validate_adapter_recipe_metadata(meta, train_count):
    """Validate complete control lineage without importing Torch or altering it."""
    identity = {'recipe_id': RECIPE_ID, 'objective_profile': OBJECTIVE_PROFILE, 'checkpoint_selection': CHECKPOINT_SELECTION}
    if any(meta.get(key) != value for key, value in identity.items()) or meta.get('training_recipe') != recipe_contract():
        raise ValueError('control adapter recipe identity is missing or changed')
    preprocessing = meta.get('preprocessing')
    if not isinstance(preprocessing, dict) or preprocessing.get('profile') != PREPROCESSING_PROFILE:
        raise ValueError('control adapter requires its corrected preprocessing identity')
    budget = meta.get('training_budget')
    if not isinstance(budget, dict):
        raise ValueError('control adapter budget metadata is missing')
    expected = resolve_control_recipe(train_count, {'train': {'max_seconds': budget.get('max_seconds')}})['training_budget']
    if budget != expected:
        raise ValueError('control adapter budget differs from verified training count')
    objective = meta.get('training_objective')
    required = {'schema': 'm2-objective/3-control', 'recipe_id': RECIPE_ID, 'objective_profile': OBJECTIVE_PROFILE,
                'prediction': 'epsilon', 'diffusion_loss': 'mean_squared_error', 'conditioning_dropout': 0.1,
                'offset_loss': '0.1 * mean(offset_sum ** 2)', 'gradient_clip_norm': 1.0,
                'perceptual_loss': {'coefficient': 0.0, 'enabled': False, 'asset_required': False}}
    if not isinstance(objective, dict) or any(objective.get(key) != value for key, value in required.items()):
        raise ValueError('control objective metadata is missing or changed')
    selected = meta.get('selection') or {}
    if selected.get('kind') != 'terminal_state':
        raise ValueError('only a complete terminal control candidate is eligible')
    validate_completed_budget(selected.get('last_completed_epoch'), selected.get('global_step'), selected.get('validation_matches_exported_state'), budget)
    loss = selected.get('validation_loss')
    if isinstance(loss, bool) or not isinstance(loss, (int, float)) or not 0 <= loss < float('inf'):
        raise ValueError('matching finite terminal validation loss is required')
    return identity
