"""Independent Windows training recipe; authenticated qualification precedes use."""
from pathlib import Path
from .user_scr_contract import RECIPE_ID as LINUX_RECIPE_ID, asset_contract, objective_contract, sealed
from .runtime_variants import sha,RuntimeUnavailable

RECIPE_ID='m2-scr2n-user-windows-spatial-v1'
INFERENCE_ONLY='E_SCR_TRAINING_INFERENCE_ONLY'

def contract(app):
    from .spatial_topology import topology_fields
    return sealed({'schema':'windows-user-scr-training-recipe/1','recipe_id':RECIPE_ID,
        'platform':'win32','purpose':'prepare-and-train','lineage_recipe_id':LINUX_RECIPE_ID,
        'frozen_lineage_implementation_sha256':sha(Path(app)/'engine/style/m2_adapter/user_scr_recipe.py'),
        'windows_implementation_sha256':sha(Path(app)/'engine/style/m2_adapter/windows_training_recipe.py'),
        'scr_assets':asset_contract(),'scr_objective':objective_contract(),**topology_fields(),
        'qualification_required':['fresh-profile-environment-source-S03-cache','real-frozen-SCR195-loss-and-gradients',
            'all56-finite-nonzero-gradients','unchanged-base-and-SCR-state','weights-only-roundtrip',
            'device-specific-native-20-step-generation-and-resource-receipt'],
        'execution_enabled':False,'may_prepare':False,'may_train':False,'may_resume':False,'may_publish':False})

def select_training_registration(app,registration_id):
    """Deny inference namespace before asset/cache work; never promote a receipt."""
    import re
    if not isinstance(registration_id,str) or re.fullmatch('[0-9a-f]{32}',registration_id) is None:
        raise RuntimeUnavailable('RUNTIME_UNQUALIFIED','Exact training registration ID required')
    p=Path(app)/'runtime/windows-scr-inference/registrations'/registration_id/'registration.json'
    # Presence in a private inference namespace only grants a denial, not authority.
    if p.exists():raise RuntimeUnavailable(INFERENCE_ONLY,'Windows inference registration cannot prepare, train or resume SCR')
    from .windows_training_runtime import has_registration,inspect
    if has_registration(app,registration_id):
        selected=inspect(app,registration_id,purpose='prepare')
        from .runtime_variants import make_binding
        return selected,make_binding(selected)
    from .runtime_variants import inspect_registration,make_binding
    from .windows_user_scr import require_full_model_qualification
    from .user_scr_recipe import validate_user_scr_binding
    selected=inspect_registration(app,registration_id)
    binding=make_binding(selected)
    require_full_model_qualification(binding['variant_id'])
    validate_user_scr_binding(binding)
    return selected,binding

def plan_training_recipe(train_count,config=None):
    """Distinct Windows recipe; reuse frozen control math without relabelling models."""
    import copy
    from .user_scr_recipe import plan_user_scr_recipe
    cfg=copy.deepcopy(config or {})
    for k in ('training_recipe','training_budget'):cfg.pop(k,None)
    cfg['recipe_id']=LINUX_RECIPE_ID
    result=plan_user_scr_recipe(train_count,cfg)
    body={k:v for k,v in result['training_recipe'].items() if k!='sha256'}
    body.update(recipe_id=RECIPE_ID,windows_training_contract=contract(Path(__file__).resolve().parents[3]),
        implementation_sha256=sha(Path(__file__)),lineage_recipe_id=LINUX_RECIPE_ID)
    result.update(recipe_id=RECIPE_ID,training_recipe=sealed(body),qualification_status='WINDOWS_EXPLICIT_MEASURED_REGISTRATION_REQUIRED')
    for k in ('training_recipe','training_budget'):
        if k in (config or {}) and config[k]!=result[k]:raise ValueError('Windows canonical '+k+' differs')
    return result

def resolve_training_recipe(train_count,config):
    from .user_scr_contract import user_scr_identity,validate_user_scr_asset_path_shape
    from .spatial_topology import validate_spatial_topology_fields
    from .windows_user_scr import CPU,GPU
    from .windows_gpu_support import device_for_variant
    from .runtime_variants import validate_binding_shape
    validate_spatial_topology_fields(config);identity=user_scr_identity(config)
    binding=config.get('runtime_binding');validate_binding_shape(binding)
    if config.get('recipe_id')!=RECIPE_ID or binding['variant_id'] not in (CPU,GPU) or binding['device']!=device_for_variant(binding['variant_id']) or binding['capability_receipt_sha256'] is None:
        raise ValueError('independent measured Windows device-specific training binding required')
    if train_count!=identity['scr_data_binding']['counts']['train'] or config.get('snapshot_sha256')!=identity['scr_data_binding']['snapshot_sha256']:
        raise ValueError('Windows recipe S03 train count differs')
    from .windows_user_scr import validate_cache_identity
    cache=validate_cache_identity(Path(__file__).resolve().parents[3],binding['variant_id'],config.get('windows_cache_identity'),binding['environment_identity_sha256'],config['snapshot_sha256'])
    plan=plan_training_recipe(train_count,config)
    if plan['train']['device']!=binding['device']:raise ValueError('Windows training device differs from binding')
    return {**plan,**identity,'windows_cache_identity':cache,'scr_asset_paths':validate_user_scr_asset_path_shape(config['scr_asset_paths'])}

def resolve_registered_runtime(app,config,*,purpose='train'):
    from .windows_training_runtime import resolve
    from .user_scr_contract import validate_user_scr_provenance
    if config.get('recipe_id')!=RECIPE_ID:raise ValueError('explicit Windows training recipe required')
    validate_user_scr_provenance(config);selected=resolve(app,config.get('runtime_binding'),purpose=purpose)
    # Preparation has no train block and computes frozen references on CPU.
    # Its namespace is still derived from the verified selected device binding.
    default_device=selected['device'] if purpose=='prepare' else 'cpu'
    if (config.get('train') or {}).get('device',default_device)!=selected['device']:raise ValueError('Windows training device differs')
    if config.get('scr_asset_paths'):
        for k,v in selected['scr_feature_asset_paths'].items():
            if Path(config['scr_asset_paths'][k]).resolve()!=Path(v).resolve():raise ValueError('Windows SCR assets differ from registration: '+k)
    if purpose!='prepare':
        for k,wanted in {'runtime_manifest_path':selected['manifest_path'],'fd_repo':selected['fd_repo'],'content_font_path':str(Path(selected['fd_repo'])/'ttf/KaiXinSongA.ttf')}.items():
            if Path(config.get(k,'')).resolve()!=Path(wanted).resolve():raise ValueError('Windows registered run path differs: '+k)
        if config.get('runtime_identity_sha256')!=selected['asset_identity_sha256'] or config.get('base_npz_sha256')!=selected['manifest']['base_npz_sha256']:
            raise ValueError('Windows registered base differs')
    return selected

def validate_adapter_metadata(meta,train_count):
    """Validate only newly trained Windows metadata; old Linux models stay frozen."""
    import copy
    from .user_scr_contract import user_scr_identity
    from .spatial_topology import validate_spatial_topology_fields
    from .control_recipe import (RECIPE_ID as BASELINE,OBJECTIVE_PROFILE as BASE_OBJECTIVE,
        resolve_control_recipe,validate_adapter_recipe_metadata)
    from .runtime_variants import validate_binding_shape
    from .windows_user_scr import CPU,GPU
    from .windows_gpu_support import device_for_variant
    identity=user_scr_identity(meta);validate_spatial_topology_fields(meta);binding=meta.get('runtime_binding');validate_binding_shape(binding)
    if meta.get('recipe_id')!=RECIPE_ID or binding['variant_id'] not in (CPU,GPU) or binding['device']!=device_for_variant(binding['variant_id']) or binding['capability_receipt_sha256'] is None:
        raise ValueError('new measured Windows training metadata required')
    if train_count!=identity['scr_data_binding']['counts']['train']:raise ValueError('Windows metadata count differs')
    expected=plan_training_recipe(train_count,{'train':{'device':binding['device'],'max_seconds':meta.get('training_budget',{}).get('max_seconds')}})
    if meta.get('training_recipe')!=expected['training_recipe'] or meta.get('training_budget')!=expected['training_budget']:
        raise ValueError('Windows metadata recipe/budget differs')
    objective=meta.get('training_objective') or {}
    if objective.get('recipe_id')!=RECIPE_ID or any(objective.get(k)!=v for k,v in identity.items()):raise ValueError('Windows objective lineage differs')
    view=copy.deepcopy(meta);view.update(recipe_id=BASELINE,objective_profile=BASE_OBJECTIVE,
        training_recipe=resolve_control_recipe(train_count,{'train':{'max_seconds':expected['training_budget']['max_seconds']}})['training_recipe'])
    view['training_objective'].update(schema='m2-objective/3-control',recipe_id=BASELINE,objective_profile=BASE_OBJECTIVE)
    validate_adapter_recipe_metadata(view,train_count)
    return {'recipe_id':RECIPE_ID,**identity}
