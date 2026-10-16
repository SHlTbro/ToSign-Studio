"""Pure planning for the independent four-update full-checkpoint trial."""
from dataclasses import replace
from pathlib import Path
import json
from .runtime_variants import require
from .trainer_resume_protocol import validate_binding, digest

STEPS = 4
SECONDS = 900
OUTPUT_BYTES = 3 * 2**30
STOP_BYTES = int(4.5 * 2**30)

def capability_scope(binding):
    validate_binding(binding)
    # The trial has its own logical attempt. Every data/runtime/math/owner field
    # is retained; a qualified protocol may serve a later attempt of that scope.
    return {k:v for k,v in binding.items() if k != 'run_id'}

def plan(config, binding, snapshot, out):
    from .windows_training_recipe import RECIPE_ID, resolve_training_recipe
    from .native_training_session import objective_metadata, training_config
    from .preprocessing import preprocessing_contract
    from engine.style.trainer.config import hash_config
    validate_binding(binding)
    require(config.get('recipe_id') == RECIPE_ID, 'E_RESUME_RECIPE', 'independent Windows SCR recipe required')
    require(digest({k:v for k,v in config.items() if k != '_server_continuation'}) == binding['config_hash'],
            'E_RESUME_CONFIG', 'server frozen config differs from binding')
    resolved = resolve_training_recipe(config['scr_data_binding']['counts']['train'], config)
    require(resolved['train']['batch'] == 2 and resolved['train']['device'] == 'cpu',
            'E_RESUME_PROTOCOL', 'two-image CPU recipe required')
    require(config['scr_data_binding']['counts']['train'] // 2 > STEPS,
            'E_RESUME_PROTOCOL', 'mid-epoch qualification requires more than four batches')
    req = {**config, **resolved, 'run_id':binding['run_id'], 'user_id':binding['owner_id'],
           'snapshot_dir':str(Path(snapshot).resolve()), 'out_dir':str(Path(out).absolute())}
    req.pop('_server_continuation', None)
    objective, _, _, _ = objective_metadata(req, resolved, user_scr_enabled=True)
    preprocessing = preprocessing_contract(resolved['preprocessing_profile'])
    cfg = training_config(req, tcfg=resolved['train'], out_dir=Path(out), snapshot_dir=Path(snapshot),
        snapshot_sha=binding['snapshot_sha256'], engine_id=json.loads(Path(config['runtime_manifest_path']).read_text(encoding='utf-8'))['engine_id'],
        preprocessing=preprocessing, training_objective=objective, device='cpu')
    # No product caller can request this budget through runner.py. This plan is
    # used only behind the fresh MAC launch and CPU lease of the native probe.
    cfg = replace(cfg, max_epochs=1, max_steps=STEPS, max_seconds=SECONDS)
    return req, cfg, resolved, preprocessing, hash_config(cfg)
