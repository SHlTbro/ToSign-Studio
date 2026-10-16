"""Explicit new recipe route; legacy recipe implementations remain byte-identical."""
from .offset_recipe import resolve_selected_recipe as resolve_legacy_recipe
from .spatial_recipe import RECIPE_ID as SPATIAL_RECIPE_ID,resolve_spatial_recipe
from .scr_recipe import RECIPE_ID as SCR_RECIPE_ID,resolve_scr_recipe
from .user_scr_recipe import RECIPE_ID as USER_SCR_RECIPE_ID,resolve_user_scr_recipe

def resolve_selected_recipe(train_count,config):
 if not isinstance(config,dict):raise ValueError('training config must be an object')
 from .windows_training_recipe import RECIPE_ID as WINDOWS_RECIPE,resolve_training_recipe
 if config.get('recipe_id')==WINDOWS_RECIPE:return resolve_training_recipe(train_count,config)
 if config.get('recipe_id')==USER_SCR_RECIPE_ID:return resolve_user_scr_recipe(train_count,config)
 if config.get('recipe_id')==SCR_RECIPE_ID:return resolve_scr_recipe(train_count,config)
 if any(k in config for k in ('scr_objective','scr_assets','scr_asset_paths','scr_data_binding','scr_cache_binding','scr_preparation_binding')):raise ValueError('SCR provenance requires explicit SCR recipe')
 if config.get('recipe_id')==SPATIAL_RECIPE_ID:return resolve_spatial_recipe(train_count,config)
 return resolve_legacy_recipe(train_count,config)

def validate_recipe_topology_binding(record):
 """Validate explicit geometry lineage; absent historical identity stays baseline only."""
 from .topology_dispatch import validate_topology_fields,BASELINE_TOPOLOGY_ID,OFFSET_TOPOLOGY_ID
 from .offset_recipe import RECIPE_ID as OFFSET_RECIPE_ID,RUNTIME_VARIANT_ID as OFFSET_VARIANT,validate_offset_binding
 from .control_recipe import RECIPE_ID as BASELINE_RECIPE_ID
 from .spatial_topology import SPATIAL_TOPOLOGY_ID
 from .spatial_recipe import RUNTIME_VARIANT_ID as SPATIAL_VARIANT,validate_spatial_binding
 from .scr_recipe import RUNTIME_VARIANT_ID as SCR_VARIANT,validate_scr_binding
 from .scr_contract import validate_scr_provenance,asset_contract
 if not isinstance(record,dict):raise ValueError('recipe/topology metadata must be an object')
 from .windows_training_recipe import RECIPE_ID as WINDOWS_RECIPE
 recipe=record.get('recipe_id');topology=validate_topology_fields(record,required=recipe in (OFFSET_RECIPE_ID,SPATIAL_RECIPE_ID,SCR_RECIPE_ID,USER_SCR_RECIPE_ID,WINDOWS_RECIPE))
 if recipe==WINDOWS_RECIPE:
  from .windows_user_scr import VARIANTS
  from .windows_gpu_support import device_for_variant
  from .runtime_variants import validate_binding_shape
  from .user_scr_contract import user_scr_identity
  binding=record.get('runtime_binding');validate_binding_shape(binding)
  if topology['id']!=SPATIAL_TOPOLOGY_ID or binding['variant_id'] not in VARIANTS or binding['device']!=device_for_variant(binding['variant_id']) or binding['capability_receipt_sha256'] is None:raise ValueError('Windows training recipe requires independent measured CPU registration')
  user_scr_identity(record);return topology
 if recipe==USER_SCR_RECIPE_ID:
  from .user_scr_recipe import validate_user_scr_binding
  from .user_scr_contract import user_scr_identity
  if topology['id']!=SPATIAL_TOPOLOGY_ID:raise ValueError('user SCR recipe/topology mismatch')
  from .windows_scr_deployment import is_windows,validate_lineage
  if is_windows(record.get('deployment_binding')):
   from pathlib import Path
   validate_lineage(Path(__file__).resolve().parents[3],record)
  else:validate_user_scr_binding(record.get('runtime_binding'))
  user_scr_identity(record)
 elif recipe==SCR_RECIPE_ID:
  if topology['id']!=SPATIAL_TOPOLOGY_ID:raise ValueError('SCR recipe/topology mismatch')
  from .windows_scr_deployment import is_windows,validate_lineage
  if is_windows(record.get('deployment_binding')):
   from pathlib import Path
   validate_lineage(Path(__file__).resolve().parents[3],record)
  else:validate_scr_binding(record.get('runtime_binding'))
  validate_scr_provenance(record)
  if 'snapshot_sha256' in record and record['snapshot_sha256']!=asset_contract()['snapshot_sha256']:raise ValueError('SCR snapshot differs')
 elif recipe==SPATIAL_RECIPE_ID:
  if topology['id']!=SPATIAL_TOPOLOGY_ID:raise ValueError('spatial recipe/topology mismatch')
  validate_spatial_binding(record.get('runtime_binding'))
 elif recipe==OFFSET_RECIPE_ID:
  if topology['id']!=OFFSET_TOPOLOGY_ID:raise ValueError('offset recipe/topology mismatch')
  validate_offset_binding(record.get('runtime_binding'))
 else:
  if recipe not in (None,BASELINE_RECIPE_ID) or topology['id']!=BASELINE_TOPOLOGY_ID:raise ValueError('legacy recipe/topology mismatch')
  binding=record.get('runtime_binding')
  if binding is not None:
   from .runtime_variants import validate_binding_shape
   validate_binding_shape(binding)
   if binding['variant_id'] in (OFFSET_VARIANT,SPATIAL_VARIANT,SCR_VARIANT):raise ValueError('geometry runtime requires matching explicit recipe')
 return topology
