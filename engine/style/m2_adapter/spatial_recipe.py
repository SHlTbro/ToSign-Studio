"""Explicit spatial input ablation; all original objective and budget rules retained."""
from __future__ import annotations
import copy,hashlib
from pathlib import Path
from .control_recipe import (RECIPE_ID as BASELINE_RECIPE_ID,OBJECTIVE_PROFILE,PREPROCESSING_PROFILE,CHECKPOINT_SELECTION,resolve_control_recipe,validate_adapter_recipe_metadata)
from .adapter_topology import digest,TOPOLOGY_FIELDS
from .spatial_topology import SPATIAL_TOPOLOGY_ID,topology_fields,validate_spatial_topology_fields
RECIPE_ID='m2-control-spatial-offset-v1'
RUNTIME_VARIANT_ID='cpu-cp312-spatial-offset-v1'

def _canonical_spatial_recipe(train_count,config=None):
 config={} if config is None else config
 if not isinstance(config,dict) or config.get('recipe_id',RECIPE_ID)!=RECIPE_ID:raise ValueError('spatial recipe ID/config mismatch')
 if any(k in config for k in TOPOLOGY_FIELDS):validate_spatial_topology_fields(config)
 baseline_input=copy.deepcopy(config);baseline_input['recipe_id']=BASELINE_RECIPE_ID
 resolved=resolve_control_recipe(train_count,baseline_input);fields=topology_fields();contract={k:v for k,v in resolved['training_recipe'].items() if k!='sha256'};contract.update(recipe_id=RECIPE_ID,**fields,implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),baseline_recipe_sha256=resolved['training_recipe']['sha256']);resolved.update(recipe_id=RECIPE_ID,**fields,training_recipe={**contract,'sha256':digest(contract)})
 for name in ('training_recipe','training_budget'):
  if name in config and digest(config[name])!=digest(resolved[name]):raise ValueError('spatial '+name+' differs from canonical recipe')
 return resolved

def spatial_recipe_contract():return _canonical_spatial_recipe(8)['training_recipe']
def validate_spatial_binding(binding):
 from .runtime_variants import validate_binding_shape
 validate_binding_shape(binding)
 if binding['variant_id']!=RUNTIME_VARIANT_ID or binding['device']!='cpu' or binding.get('capability_receipt_sha256') is None:raise ValueError('spatial recipe requires its explicit source-bound CPU capability')
 return binding
def resolve_spatial_recipe(train_count,config=None):
 if not isinstance(config,dict) or config.get('recipe_id')!=RECIPE_ID:raise ValueError('explicit spatial recipe ID/config required')
 validate_spatial_topology_fields(config);validate_spatial_binding(config.get('runtime_binding'));return _canonical_spatial_recipe(train_count,config)
def validate_spatial_adapter_metadata(meta,train_count):
 topology=validate_spatial_topology_fields(meta)
 selected=meta.get('selection')
 if not isinstance(selected,dict) or any(type(selected.get(k)) is not int for k in ('last_completed_epoch','global_step')):raise ValueError('spatial terminal counts must be integers')
 for key,value in (('n_hook_points',22),('n_offset_projection_heads',0),('n_spatial_offset_heads',6)):
  if type(meta.get(key)) is not int or meta[key]!=value:raise ValueError('spatial declared head counts differ from topology')
 if meta.get('recipe_id')!=RECIPE_ID:raise ValueError('spatial metadata recipe mismatch')
 budget=meta.get('training_budget')
 if not isinstance(budget,dict):raise ValueError('spatial budget missing')
 validate_spatial_binding(meta.get('runtime_binding'));expected=_canonical_spatial_recipe(train_count,{'train':{'max_seconds':budget.get('max_seconds')}});actual=meta.get('training_recipe')
 if not isinstance(actual,dict) or digest({k:v for k,v in actual.items() if k!='sha256'})!=actual.get('sha256') or actual.get('sha256')!=expected['training_recipe']['sha256'] or digest(budget)!=digest(expected['training_budget']):raise ValueError('spatial recipe/budget mismatch')
 objective=meta.get('training_objective');validate_spatial_topology_fields(objective)
 if objective.get('recipe_id')!=RECIPE_ID or objective.get('adapter_topology_sha256')!=topology['sha256']:raise ValueError('spatial objective/topology mismatch')
 view=copy.deepcopy(meta);view['recipe_id']=BASELINE_RECIPE_ID;view['training_recipe']=resolve_control_recipe(train_count,{'train':{'max_seconds':budget.get('max_seconds')}})['training_recipe'];view['training_objective']['recipe_id']=BASELINE_RECIPE_ID
 validate_adapter_recipe_metadata(view,train_count)
 return {key:meta[key] for key in ('recipe_id','objective_profile','checkpoint_selection',*TOPOLOGY_FIELDS)}
