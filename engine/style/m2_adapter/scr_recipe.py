"""Explicit fixed-data SCR2N ablation; historical self-hashed recipes unchanged."""
from __future__ import annotations
import copy,hashlib
from pathlib import Path
from .control_recipe import (RECIPE_ID as BASELINE_RECIPE_ID, OBJECTIVE_PROFILE as BASELINE_OBJECTIVE,
    PREPROCESSING_PROFILE,CHECKPOINT_SELECTION,resolve_control_recipe,validate_adapter_recipe_metadata)
from .adapter_topology import TOPOLOGY_FIELDS
from .spatial_topology import SPATIAL_TOPOLOGY_ID,topology_fields,validate_spatial_topology_fields
from .scr_contract import digest,objective_contract,asset_contract,validate_scr_provenance,validate_scr_asset_path_shape

RECIPE_ID='m2-scr-two-negative-spatial-v1'
OBJECTIVE_PROFILE='epsilon-offset-scr2n-v1'
RUNTIME_VARIANT_ID='cpu-cp312-scr2n-spatial-v1'


def _canonical_recipe(train_count,config=None):
    if type(train_count) is not int or train_count!=51:raise ValueError('SCR2N cache fixes51 training occurrences')
    config={} if config is None else config
    if not isinstance(config,dict):raise ValueError('SCR2N config must be an object')
    for key,wanted in [('recipe_id',RECIPE_ID),('objective_profile',OBJECTIVE_PROFILE),('preprocessing_profile',PREPROCESSING_PROFILE),('checkpoint_selection',CHECKPOINT_SELECTION)]:
        if key in config and config[key]!=wanted:raise ValueError('SCR2N fixes '+key)
    if 'train' in config and not isinstance(config['train'],dict):raise ValueError('SCR2N train must be an object')
    if 'train' in config and config['train'].get('max_seconds',3600)!=3600:raise ValueError('SCR2N fixes3600-second training cap')
    base_input=copy.deepcopy(config)
    for key in ('training_recipe','training_budget'):base_input.pop(key,None)
    base_input.update(recipe_id=BASELINE_RECIPE_ID,objective_profile=BASELINE_OBJECTIVE)
    base_input['train']={**base_input.get('train',{}),'max_seconds':3600,'device':'cpu'}
    if config.get('train',{}).get('device','cpu')!='cpu':raise ValueError('SCR2N requires CPU')
    resolved=resolve_control_recipe(train_count,base_input)
    fields=topology_fields();provenance={'scr_objective':objective_contract(),'scr_assets':asset_contract()}
    body={k:v for k,v in resolved['training_recipe'].items() if k!='sha256'}
    body.update(recipe_id=RECIPE_ID,objective_profile=OBJECTIVE_PROFILE,**fields,**provenance,
        loss='mean epsilon MSE + 0.1 * mean(offset_sum ** 2) + 0.01 * mean four-layer two-negative paired InfoNCE',
        SCR_asset_required_for_training=True,SCR_asset_required_for_inference=False,
        implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        baseline_recipe_sha256=resolved['training_recipe']['sha256'])
    resolved.update(recipe_id=RECIPE_ID,objective_profile=OBJECTIVE_PROFILE,**fields,**provenance,training_recipe={**body,'sha256':digest(body)})
    for name in ('training_recipe','training_budget'):
        if name in config and digest(config[name])!=digest(resolved[name]):raise ValueError('SCR2N '+name+' differs')
    return resolved


def scr_recipe_contract():return _canonical_recipe(51)['training_recipe']


def validate_scr_binding(binding):
    from .runtime_variants import validate_binding_shape
    validate_binding_shape(binding)
    if binding['variant_id']!=RUNTIME_VARIANT_ID or binding['device']!='cpu' or binding.get('capability_receipt_sha256') is None:
        raise ValueError('SCR2N requires its explicit exact-source CPU capability')
    return binding


def resolve_scr_recipe(train_count,config=None):
    if not isinstance(config,dict) or config.get('recipe_id')!=RECIPE_ID:raise ValueError('explicit SCR2N recipe required')
    validate_spatial_topology_fields(config);validate_scr_binding(config.get('runtime_binding'));validate_scr_provenance(config)
    if config.get('snapshot_sha256')!=asset_contract()['snapshot_sha256']:raise ValueError('SCR2N snapshot differs from fixed feature cache')
    paths=validate_scr_asset_path_shape(config.get('scr_asset_paths'))
    return {**_canonical_recipe(train_count,config),'scr_asset_paths':paths}


def validate_scr_adapter_metadata(meta,train_count):
    selected=meta.get('selection')
    if not isinstance(selected,dict) or any(type(selected.get(k)) is not int for k in ('global_step','last_completed_epoch')):raise ValueError('SCR2N terminal counts must be integers')
    topology=validate_spatial_topology_fields(meta);validate_scr_binding(meta.get('runtime_binding'));validate_scr_provenance(meta)
    if meta.get('recipe_id')!=RECIPE_ID or meta.get('objective_profile')!=OBJECTIVE_PROFILE:raise ValueError('SCR2N adapter recipe/objective differs')
    if meta.get('snapshot_sha256')!=asset_contract()['snapshot_sha256']:raise ValueError('SCR2N adapter snapshot differs')
    for key,value in (('n_hook_points',22),('n_offset_projection_heads',0),('n_spatial_offset_heads',6)):
        if type(meta.get(key)) is not int or meta[key]!=value:raise ValueError('SCR2N actual head count mismatch')
    expected=_canonical_recipe(train_count)
    for key in ('training_recipe','training_budget'):
        if digest(meta.get(key))!=digest(expected[key]):raise ValueError('SCR2N adapter '+key+' mismatch')
    objective=meta.get('training_objective')
    if not isinstance(objective,dict) or objective.get('schema')!='m2-objective/4-scr2n':raise ValueError('SCR2N objective schema missing')
    validate_scr_provenance(objective);validate_spatial_topology_fields(objective)
    if objective.get('recipe_id')!=RECIPE_ID or objective.get('objective_profile')!=OBJECTIVE_PROFILE or objective.get('adapter_topology_sha256')!=topology['sha256']:
        raise ValueError('SCR2N objective lineage differs')
    # Reuse unchanged terminal/base-objective validation on an explicit local view.
    view=copy.deepcopy(meta);view.update(recipe_id=BASELINE_RECIPE_ID,objective_profile=BASELINE_OBJECTIVE,
        training_recipe=resolve_control_recipe(train_count,{'train':{'max_seconds':3600}})['training_recipe'])
    view['training_objective'].update(schema='m2-objective/3-control',recipe_id=BASELINE_RECIPE_ID,objective_profile=BASELINE_OBJECTIVE)
    validate_adapter_recipe_metadata(view,train_count)
    return {key:meta[key] for key in ('recipe_id','objective_profile','checkpoint_selection',*TOPOLOGY_FIELDS,'scr_objective','scr_assets')}
