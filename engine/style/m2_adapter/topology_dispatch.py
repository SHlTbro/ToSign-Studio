"""New explicit topology dispatcher, preserving both self-hashed legacy contracts."""
from . import adapter_topology as legacy
from .spatial_topology import (SPATIAL_TOPOLOGY_ID,SPATIAL_RECIPE_ID,SPATIAL_HEADS,topology_contract as spatial_contract,topology_fields as spatial_fields,validate_spatial_topology_fields,expected_spatial_shapes)
BASELINE_TOPOLOGY_ID=legacy.BASELINE_TOPOLOGY_ID
OFFSET_TOPOLOGY_ID=legacy.OFFSET_TOPOLOGY_ID
TOPOLOGY_FIELDS=legacy.TOPOLOGY_FIELDS
RESIDUAL_HOOK_KEYS=legacy.RESIDUAL_HOOK_KEYS
RESIDUAL_MODULES=legacy.RESIDUAL_MODULES
OFFSET_HEADS=legacy.OFFSET_HEADS
digest=legacy.digest

def topology_contract(topology_id=BASELINE_TOPOLOGY_ID):
 return spatial_contract() if topology_id==SPATIAL_TOPOLOGY_ID else legacy.topology_contract(topology_id)
def topology_fields(topology_id=BASELINE_TOPOLOGY_ID):
 return spatial_fields() if topology_id==SPATIAL_TOPOLOGY_ID else legacy.topology_fields(topology_id)
def validate_topology_fields(record,*,required=False):
 if isinstance(record,dict) and record.get('adapter_topology_id')==SPATIAL_TOPOLOGY_ID:return validate_spatial_topology_fields(record)
 return legacy.validate_topology_fields(record,required=required)
def expected_offset_shapes(topology_id):
 return expected_spatial_shapes() if topology_id==SPATIAL_TOPOLOGY_ID else legacy.expected_offset_shapes(topology_id)
def validate_inference_topology_execution(receipt):
 from .scr_recipe import RECIPE_ID as SCR_RECIPE_ID
 from .user_scr_recipe import RECIPE_ID as USER_SCR_RECIPE_ID
 from .windows_training_recipe import RECIPE_ID as WINDOWS_SCR_RECIPE_ID
 if not isinstance(receipt,dict):raise ValueError('E_ADAPTER_EXECUTION: receipt object required')
 spatial=receipt.get('adapter_topology_id')==SPATIAL_TOPOLOGY_ID
 if not spatial:
  if receipt.get('recipe_id') in (SPATIAL_RECIPE_ID,SCR_RECIPE_ID,USER_SCR_RECIPE_ID,WINDOWS_SCR_RECIPE_ID) or receipt.get('spatial_offset_hook_calls',{})!={}:raise ValueError('E_ADAPTER_EXECUTION: legacy mode cannot execute spatial offsets')
  return legacy.validate_inference_topology_execution(receipt)
 topology=validate_spatial_topology_fields(receipt)
 if receipt.get('recipe_id') not in (SPATIAL_RECIPE_ID,SCR_RECIPE_ID,USER_SCR_RECIPE_ID,WINDOWS_SCR_RECIPE_ID):raise ValueError('E_ADAPTER_EXECUTION: spatial recipe mismatch')
 source=receipt.get('source');active=source=='M2'
 if source not in ('M1','M2') or receipt.get('adapter_applied') is not active:raise ValueError('E_ADAPTER_EXECUTION: source/adapter mismatch')
 residual=receipt.get('adapter_hook_calls');spatial_calls=receipt.get('spatial_offset_hook_calls');old=receipt.get('offset_projection_hook_calls')
 if not isinstance(residual,dict) or set(residual)!=set(RESIDUAL_HOOK_KEYS) or not isinstance(spatial_calls,dict) or set(spatial_calls)!={x['module'] for x in SPATIAL_HEADS} or old!={}:raise ValueError('E_ADAPTER_EXECUTION: spatial/residual/legacy count key mismatch')
 for count in [*residual.values(),*spatial_calls.values()]:
  if type(count) is not int or (count<=0 if active else count!=0):raise ValueError('E_ADAPTER_EXECUTION: typed spatial execution counts required')
 return topology
