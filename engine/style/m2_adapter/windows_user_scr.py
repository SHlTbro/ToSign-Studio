"""Independent disabled Windows generic SCR profiles and qualification planning.

Profiles bind Windows, exact source, dependency lock, topology and device. A
component smoke receipt is never accepted as full-model capability or registration.
Linux receipts, legacy 44-tensor receipts and caller ready flags confer no rights.
"""
from pathlib import Path
from .user_scr_contract import OBJECTIVE_PROFILE,asset_contract,objective_contract
from .windows_training_recipe import RECIPE_ID,contract as training_recipe_contract
from .spatial_topology import topology_fields

CPU='windows-cp312-cu128-user-scr2n-spatial-cpu-v1'
GPU='windows-cp312-cu128-user-scr2n-spatial-gpu-v1'
VARIANTS=frozenset((CPU,GPU))
LOCK='requirements-r9-windows-gpu-cu128.lock'
BLOCKER='E_WINDOWS_GENERIC_QUALIFICATION_REQUIRED'

def profile_for_source(app,variant):
    from .runtime_variants import sha,safe_relative,value_sha,_engine_inventory
    from .user_scr_runtime import user_scr_source_hashes
    if variant not in VARIANTS:raise ValueError('unknown Windows generic SCR variant')
    device='cpu' if variant==CPU else 'cuda'
    body={'variant_id':variant,'device':device,'python':'3.12','platforms':['win32'],
      'recipe_id':RECIPE_ID,'objective_profile':OBJECTIVE_PROFILE,
      'purpose':'prepare-and-train','training_recipe':training_recipe_contract(app),
      'may_prepare':False,'may_train':False,'may_resume':False,'may_publish':False,
      'full_model_probe_required':True,'execution_enabled':False,
      'registration_admission':'blocked-until-independent-windows-full-model-verifier-v1',
      'capability_schema':'windows-user-scr-full-model-capability/1',
      'evidence_verifier_sha256':sha(safe_relative(app,'engine/style/m2_adapter/windows_training_capability.py')),
      'qualification_admission_status':'server-owned monitored device-specific trial required; no caller proof admission',
      'training_producer_sha256':sha(safe_relative(app,'engine/style/m2_adapter/windows_scr_training_probe.py')),
      'training_executor_sha256':sha(safe_relative(app,'engine/style/m2_adapter/windows_training_executor.py')),
      'required_execution_files':{rel:sha(safe_relative(app,rel)) for rel in (
          'scripts/probe_m2_runtime_scr_two_negative.py','scripts/windows_scr_training.py','installer/runtime_addon.py',
          'config/windows-gpu-resources.json')},
      'lock':LOCK,'lock_sha256':sha(safe_relative(app,LOCK)),
      'cuda_runtime':'12.8' if device=='cuda' else None,
      'label':'Windows generic SCR '+device+'; qualification pending',
      'operator_policy':{'policy':'cpu-deform-forward-backward-fp32-v1','precision':'float32',
        'cross_device_copies':'differentiable CUDA/CPU copies' if device=='cuda' else 'none',
        'implementation_sha256':sha(safe_relative(app,'engine/style/m2_adapter/deterministic_ops.py'))},
      'max_memory_bytes':5*2**30,'max_gpu_allocated_bytes':3*2**30 if device=='cuda' else 0,
      'max_gpu_reserved_bytes':3*2**30 if device=='cuda' else 0,
      'gpu_memory_scope':'PyTorch allocator in owned process; excludes CUDA context, driver and other processes',
      'scr_assets':asset_contract(),'scr_objective':objective_contract(),**topology_fields(),
      'required_engine_files':_engine_inventory(app),'required_source_files':user_scr_source_hashes(app),
      'environment_probe_sha256':sha(safe_relative(app,'installer/probe_runtime_variant.py')),
      'execution_blocker':BLOCKER,
      'not_claimed':['Windows full model qualification','model quality','publication','Linux receipt portability']}
    return {**body,'execution_profile_sha256':value_sha(body)}

def validate_profile(app,variant,spec):
    if spec!=profile_for_source(app,variant):raise ValueError('Windows generic SCR profile/source identity differs')
    return spec

def require_full_model_qualification(variant):
    if variant in VARIANTS:
        from .runtime_variants import RuntimeUnavailable
        raise RuntimeUnavailable(BLOCKER,'Monitored Windows full SCR producer, executor and authenticated registration are required; record validation, inference, Linux and legacy44 receipts cannot authorize this profile')

def cache_identity(app,variant,environment_sha256,snapshot_sha256):
    """Pure Windows cache namespace; never imports historical reference vectors."""
    import re
    from .runtime_variants import value_sha
    if not all(isinstance(v,str) and re.fullmatch('[0-9a-f]{64}',v) for v in (environment_sha256,snapshot_sha256)):
        raise ValueError('complete environment and frozen snapshot SHA256 required')
    spec=profile_for_source(app,variant)
    body={'schema':'windows-user-scr-cache-identity/1','variant_id':variant,'device':spec['device'],
      'platform':'win32','recipe_id':RECIPE_ID,'expected_adapter_tensors':56,
      'adapter_topology_sha256':spec['adapter_topology_sha256'],
      'execution_profile_sha256':spec['execution_profile_sha256'],'dependency_lock_sha256':spec['lock_sha256'],
      'environment_identity_sha256':environment_sha256,'snapshot_sha256':snapshot_sha256,
      'source_inventory_sha256':value_sha(spec['required_source_files']),
      'scr_assets_sha256':spec['scr_assets']['sha256'],'scr_objective_sha256':spec['scr_objective']['sha256']}
    return {**body,'sha256':value_sha(body)}

def validate_cache_identity(app,variant,record,environment_sha256,snapshot_sha256):
    if record!=cache_identity(app,variant,environment_sha256,snapshot_sha256):
        raise ValueError('Windows SCR cache profile/environment/source/snapshot/topology identity differs')
    return record

def qualification_plan(app,variant):
    profile=profile_for_source(app,variant)
    return {'schema':'windows-user-scr-qualification-plan/1','variant_id':variant,
      'execution_profile_sha256':profile['execution_profile_sha256'],'device':profile['device'],
      'recipe_id':RECIPE_ID,'training_recipe_sha256':profile['training_recipe']['sha256'],
      'component_smoke':{'max_seconds':90,'max_new_output_bytes':8*2**20,
        'max_rss_bytes':2*2**30,'max_gpu_allocated_bytes':128*2**20,
        'operations':['weights-only terminal56 state shape/dtype/finite audit','one tiny CPU deform forward/backward',
          'one tiny target-device forward/backward','measure CPU-deform CUDA copies when CUDA selected'],
        'loads_base_model':False,'optimizer_updates':0,'registration_allowed':False},
      'full_model_trial':{'status':'BLOCKED_REQUIRES_COMPLETE_LOCAL_INPUTS_AND_VERIFIER',
        'evidence_validator_available':True,'monitored_producer_available':True,'windows_recipe_executor_available':True,
        'max_seconds':900,'max_rss_bytes':int(4.5*2**30),'absolute_memory_budget_bytes':5*2**30,
        'max_gpu_allocated_bytes':3*2**30 if variant==GPU else 0,'minimum_free_gpu_bytes':4*2**30 if variant==GPU else 0,
        'max_new_output_bytes':128*2**20,'optimizer_updates':2,'native_generations':1,'generation_steps':20,
        'snapshot':'current verified owned frozen S03 and source ledger; whole photo-family split; at least 8 train and 2 val occurrences',
        'cache':'new exact Windows profile/environment/source/snapshot cache; no historical vectors',
        'proof':['all56 trainable deltas nonzero finite gradients','all original base/SCR195 states frozen and unchanged',
          'small weights-only adapter/optimizer roundtrip','six spatial and six CPU deform execution counters',
          'current-source tiny masked/unmasked CUDA-CPU parity with repeated exact output and all gradient hashes',
          'actual GPU allocations/copies when CUDA selected','save raw and alpha pixels before unchanged ink guard'],
        'process_policy':'only tracked newly created qualification process; never stop EduAgent or existing processes'},
      'execution_enabled':False,'may_train':False,'may_publish':False,'claims_quality_pass':False}
