"""Explicit experimental geometry recipe; baseline recipe bytes stay unchanged."""
from __future__ import annotations
import copy,hashlib
from pathlib import Path
from .control_recipe import (RECIPE_ID as BASELINE_RECIPE_ID,OBJECTIVE_PROFILE,PREPROCESSING_PROFILE,
                            CHECKPOINT_SELECTION,resolve_control_recipe,validate_adapter_recipe_metadata)
from .adapter_topology import (BASELINE_TOPOLOGY_ID,OFFSET_TOPOLOGY_ID,TOPOLOGY_FIELDS,
                               digest,topology_fields,validate_topology_fields)
RECIPE_ID='m2-control-offset-delta-v1'
RUNTIME_VARIANT_ID='cpu-cp312-offset-delta-v1'

def _canonical_offset_recipe(train_count,config=None):
 config={} if config is None else config
 if not isinstance(config,dict):raise ValueError('offset training config must be an object')
 if config.get('recipe_id',RECIPE_ID)!=RECIPE_ID:raise ValueError('geometry recipe ID mismatch')
 if config.get('adapter_topology_id',OFFSET_TOPOLOGY_ID)!=OFFSET_TOPOLOGY_ID:raise ValueError('geometry topology ID mismatch')
 if any(k in config for k in TOPOLOGY_FIELDS):validate_topology_fields(config,required=True)
 baseline_input=copy.deepcopy(config);baseline_input['recipe_id']=BASELINE_RECIPE_ID
 resolved=resolve_control_recipe(train_count,baseline_input);fields=topology_fields(OFFSET_TOPOLOGY_ID)
 contract={k:v for k,v in resolved['training_recipe'].items() if k!='sha256'}
 contract.update(recipe_id=RECIPE_ID,**fields,implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),baseline_recipe_sha256=resolved['training_recipe']['sha256'])
 resolved.update(recipe_id=RECIPE_ID,**fields,training_recipe={**contract,'sha256':digest(contract)})
 for name in ('training_recipe','training_budget'):
  if name in config and digest(config[name])!=digest(resolved[name]):raise ValueError('geometry '+name+' differs from canonical recipe')
 return resolved

def offset_recipe_contract():
 """Declarative contract for discovery/probes; grants no execution authority."""
 return _canonical_offset_recipe(8)['training_recipe']

def validate_offset_binding(binding):
 from .runtime_variants import validate_binding_shape
 validate_binding_shape(binding)
 if binding['variant_id']!=RUNTIME_VARIANT_ID or binding['device']!='cpu' or binding.get('capability_receipt_sha256') is None:
  raise ValueError('geometry recipe requires its explicit source-bound CPU capability')
 return binding

def resolve_offset_recipe(train_count,config=None):
 if not isinstance(config,dict) or config.get('recipe_id')!=RECIPE_ID:raise ValueError('explicit bound geometry recipe ID/config required')
 validate_topology_fields(config,required=True)
 validate_offset_binding(config.get('runtime_binding'))
 return _canonical_offset_recipe(train_count,config)

def resolve_selected_recipe(train_count,config):
 if config.get('recipe_id')==RECIPE_ID:return resolve_offset_recipe(train_count,config)
 topology=validate_topology_fields(config)
 if topology['id']!=BASELINE_TOPOLOGY_ID:raise ValueError('geometry topology requires explicit geometry recipe')
 resolved=resolve_control_recipe(train_count,config)
 if any(k in config for k in TOPOLOGY_FIELDS):resolved.update(topology_fields(BASELINE_TOPOLOGY_ID))
 return resolved

def validate_offset_adapter_metadata(meta,train_count):
 topology=validate_topology_fields(meta,required=True)
 if meta.get('recipe_id')!=RECIPE_ID or topology['id']!=OFFSET_TOPOLOGY_ID:raise ValueError('geometry metadata recipe/topology mismatch')
 budget=meta.get('training_budget')
 if not isinstance(budget,dict):raise ValueError('geometry budget missing')
 validate_offset_binding(meta.get('runtime_binding'))
 expected=_canonical_offset_recipe(train_count,{'train':{'max_seconds':budget.get('max_seconds')}})
 actual_recipe=meta.get('training_recipe')
 if not isinstance(actual_recipe,dict) or digest({k:v for k,v in actual_recipe.items() if k!='sha256'})!=actual_recipe.get('sha256') or actual_recipe.get('sha256')!=expected['training_recipe']['sha256'] or digest(budget)!=digest(expected['training_budget']):raise ValueError('geometry recipe/budget mismatch')
 objective=meta.get('training_objective')
 validate_topology_fields(objective,required=True)
 if not isinstance(objective,dict) or objective.get('recipe_id')!=RECIPE_ID or objective.get('adapter_topology_sha256')!=topology['sha256'] or objective.get('adapter_topology_id')!=OFFSET_TOPOLOGY_ID:raise ValueError('geometry objective/topology mismatch')
 # Reuse unchanged baseline checks on a temporary validation view. This never
 # changes stored metadata, runtime binding, selected state or model bytes.
 view=copy.deepcopy(meta);view['recipe_id']=BASELINE_RECIPE_ID;view['training_recipe']=resolve_control_recipe(train_count,{'train':{'max_seconds':budget['max_seconds']}})['training_recipe'];view['training_objective']['recipe_id']=BASELINE_RECIPE_ID
 validate_adapter_recipe_metadata(view,train_count)
 return {key:meta[key] for key in ('recipe_id','objective_profile','checkpoint_selection',*TOPOLOGY_FIELDS)}
