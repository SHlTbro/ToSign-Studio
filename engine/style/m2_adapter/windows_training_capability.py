"""Read-only Windows full SCR evidence validation; never execution authority.

A complete record is not a signed server capability. Registration remains closed
until a monitored Windows producer and new recipe executor are implemented.
"""
from pathlib import Path
import math
from .runtime_variants import require,_strict_equal,sha,value_sha,versions_from_lock
from .windows_training_recipe import RECIPE_ID,contract
from .windows_user_scr import CPU,GPU,profile_for_source,cache_identity
from .windows_gpu_support import device_for_variant,validate_cuda_observation,validate_mixed_counts,GPU_LIMIT
from .user_scr_contract import (asset_contract,objective_contract,validate_data_binding,
    validate_cache_binding,project_user_scr_execution_evidence)

SCHEMA='windows-user-scr-full-model-capability/1'
PRODUCER='engine/style/m2_adapter/windows_scr_training_probe.py'
VERIFIER='engine/style/m2_adapter/windows_training_capability.py'

def _need(ok,message):require(ok,'E_WINDOWS_SCR_EVIDENCE',message)
def _equal(a,b):return _strict_equal(a,b)
def _finite(v,positive=False):
    if type(v) not in (int,float):return False
    try:return math.isfinite(v) and (v>0 if positive else v>=0)
    except OverflowError:return False
def _hash(v):
    import re
    return isinstance(v,str) and re.fullmatch('[0-9a-f]{64}',v) is not None

def expected_adapter_shapes():
    from .adapter_topology import residual_key
    from .spatial_topology import expected_spatial_shapes
    rows={}
    modules=[(f'down_blocks.{b}.resnets.{i}',c) for b,c in enumerate((64,128,256,512)) for i in range(2)]
    modules += [(f'mid_block.resnets.{i}',512) for i in range(2)]
    modules += [(f'up_blocks.{b}.resnets.{i}',c) for b,c in enumerate((512,256,128,64)) for i in range(3)]
    for name,c in modules:
        prefix='blocks.'+residual_key(name)
        rows[prefix+'.weight']=[c,c,1,1];rows[prefix+'.bias']=[c]
    rows.update({k:list(v) for k,v in expected_spatial_shapes().items()})
    return dict(sorted(rows.items()))

def _environment(app,proof,observed,variant=CPU):
    device=device_for_variant(variant)
    from .windows_user_scr import LOCK
    wanted=versions_from_lock(Path(app)/LOCK)
    _need(isinstance(observed,dict) and observed.get('schema')=='windows-scr-training-environment/1',
        'fresh training environment, not inference or Linux observation, required')
    _need(_equal(proof.get('environment'),observed) and _equal(observed.get('versions'),wanted),'observed versions/environment differs')
    numeric=observed.get('numerical_environment');_need(isinstance(numeric,dict),'complete numerical observation required')
    numeric_keys={'schema','python','platform','packages','device','cuda_runtime','cudnn_version','gpu_name',
        'freetype_version','torch_deterministic_algorithms','matmul_tf32','cudnn_tf32','dtype','amp',
        'default_dtype','cublas_workspace_config','cudnn_benchmark','cudnn_deterministic','torch_cpu_threads',
        'torch_interop_threads','operator_execution_policy','mixed_cuda_cpu_deform','identity_sha256'}
    _need(set(observed)=={'schema','versions','numerical_environment','identity_sha256'} and set(numeric)==numeric_keys,'complete typed environment fields required')
    _need(observed.get('identity_sha256')==numeric.get('identity_sha256')==value_sha({k:v for k,v in numeric.items() if k!='identity_sha256'}),'numerical digest differs')
    fixed={'schema':'r9-numerical-environment/2','device':device,'cuda_runtime':'12.8',
        'dtype':'float32','default_dtype':'torch.float32','amp':False,'torch_deterministic_algorithms':True,
        'matmul_tf32':False,'cudnn_tf32':False,'cudnn_benchmark':False,'cudnn_deterministic':True,
        'torch_cpu_threads':2,'torch_interop_threads':1,'mixed_cuda_cpu_deform':variant==GPU,'cublas_workspace_config':':4096:8'}
    _need(all(_equal(numeric.get(k),v) for k,v in fixed.items()),'Windows deterministic device policy differs')
    _need(isinstance(numeric.get('gpu_name'),str) and bool(numeric['gpu_name'].strip()) if variant==GPU else numeric.get('gpu_name') is None,'actual selected GPU identity differs')
    _need(isinstance(numeric.get('platform'),str) and numeric['platform'].startswith('Windows-')
        and isinstance(numeric.get('python'),str) and numeric['python'].startswith('3.12.'),'Windows CPython312 required')
    names=('torch','torchvision','diffusers','transformers','accelerate','numpy','pillow','pygame','safetensors','fonttools','huggingface-hub')
    _need(_equal(numeric.get('packages'),{k:wanted[k] for k in names}),'numerical package versions differ')
    policy={'schema':'r9-operator-execution/1','policy':'cpu-deform-forward-backward-fp32-v1',
        'operator':'torchvision.ops.deform_conv2d','execution':'CPU forward and backward; differentiable device copies',
        'precision':'float32 without autocast or detach','scope':'same six frozen-base modules for training and inference',
        'implementation_sha256':sha(Path(app)/'engine/style/m2_adapter/deterministic_ops.py')}
    _need(_equal(numeric.get('operator_execution_policy'),policy),'actual operator execution policy differs')
    _need(type(numeric.get('cudnn_version')) is int and numeric['cudnn_version']>0,'actual cuDNN library observation required')
    _need(isinstance(numeric.get('freetype_version'),list) and len(numeric['freetype_version'])==3
        and all(type(v) is int and v>=0 for v in numeric['freetype_version']),'actual FreeType library observation required')
    return observed['identity_sha256']

def verify_trial_records(app,variant,proof,observed):
    """Validate full observations structurally. This result cannot register or run."""
    device=device_for_variant(variant)
    spec=profile_for_source(app,variant)
    fixed={'schema':SCHEMA,'status':'MEASURED_FULL_SCR_GPU_TRIAL' if variant==GPU else 'MEASURED_FULL_SCR_CPU_TRIAL','variant_id':variant,'recipe_id':RECIPE_ID,
        'purpose':'full-scr-qualification','device':device,'synthetic_test_only':False,'GPU_used':variant==GPU,
        'may_prepare':False,'may_train':False,'may_resume':False,'may_publish':False,'claims_quality_pass':False}
    _need(isinstance(proof,dict) and all(_equal(proof.get(k),v) for k,v in fixed.items()),'independent Windows full SCR trial record required')
    _need(_equal(proof.get('training_recipe'),contract(app)) and proof.get('execution_profile_sha256')==spec['execution_profile_sha256'], 'recipe/profile drift')
    _need(_equal(proof.get('source_hashes'),spec['required_source_files']) and proof.get('verifier_sha256')==sha(Path(app)/VERIFIER),'final measured source/verifier differs')
    _need(proof.get('dependency_lock_sha256')==spec['lock_sha256'],'lock drift')
    env=_environment(app,proof,observed,variant)
    component=proof.get('component');_need(isinstance(component,dict),'full SCR component missing')
    component_fixed={'schema':'windows-scr-qualification-core-trial/1','variant_id':variant,'recipe_id':RECIPE_ID,
        'platform':'win32','device':device,'threads':2,'interop_threads':1,'optimizer_steps':2,'generation_calls':1,
        'synthetic_test_only':False,'may_publish':False,'claims_quality_pass':False,'preparation_authority_created':False,
        'registered_runtime_created':False,'full_trainer_resume_claimed':False}
    _need(all(_equal(component.get(k),v) for k,v in component_fixed.items()),'complete Windows full SCR component identity required')
    _need(_equal(component.get('source_hashes'),proof['source_hashes']) and _equal(component.get('numerical_environment'),observed['numerical_environment'])
        and _hash(proof.get('producer_sha256')) and component.get('driver_sha256')==proof['producer_sha256'],'component source/environment/producer differs')
    _need(_equal(component.get('scr_assets'),asset_contract()) and _equal(component.get('scr_objective'),objective_contract()),'objective/font-feature identities differ')
    data=validate_data_binding(component.get('scr_data_binding'));cache=validate_cache_binding(component.get('scr_cache_binding'),data)
    _need(_equal(proof.get('windows_cache_identity'),cache_identity(app,variant,env,data['snapshot_sha256'])),'Windows cache namespace differs')
    _need(_equal(component.get('snapshot_binding'),component.get('scr_execution_evidence',{}).get('snapshot_binding')),'objective snapshot differs')
    projected={**component,**project_user_scr_execution_evidence(component.get('scr_execution_evidence')),'dataset_snapshot_sha256':data['snapshot_sha256']}
    from .user_scr_runtime import verify_user_scr_training_evidence
    verify_user_scr_training_evidence(projected,core=True,require_preparation=False)
    reference=component.get('reference_feature_execution',{});labels=len({r['label'] for r in data['rows']})
    expected={'mode':'frozen_eval_fp32','author_images':data['counts']['total'],'negative_images':2*labels,
        'reference_feature_forwards':data['counts']['total']+2*labels,'reused_historical_vectors':False,
        'rng_preserved':True,'synthetic_test_only':False,'created_fresh_in_trial':True}
    _need(all(_equal(reference.get(k),v) for k,v in expected.items()),'fresh actual reference computation required')
    _need(_hash(reference.get('producer_state_before_sha256')) and reference['producer_state_before_sha256']==reference.get('producer_state_after_sha256'),'reference encoder state changed')
    indexed={r['sample_id']:r for r in data['rows']};batch=component.get('fixed_batch',{}).get('rows')
    _need(isinstance(batch,list) and len(batch)==2 and len({r.get('sample_id') for r in batch})==2,'two distinct fixed training occurrences required')
    for row in batch:
        actual=indexed.get(row.get('sample_id'),{})
        _need(actual.get('split')=='train' and row.get('label')==actual.get('label') and row.get('png_sha256')==actual.get('source_png_sha256'),'optimizer occurrence is not canonical train data')
    shapes=expected_adapter_shapes();state=component.get('adapter_state_observation')
    _need(_equal(component.get('adapter_load'),{'weights_only':True,'strict_load':True,'map_location':'cpu','partial_load':False,'prefix_stripping':False}),'strict weights-only adapter observation required')
    _need(isinstance(state,dict) and set(state)==set(shapes),'all 56 named observed adapter tensors required')
    for key,row in state.items():
        _need(isinstance(row,dict) and _equal(row.get('shape'),shapes[key]) and row.get('dtype')=='float32'
            and row.get('finite') is True and _hash(row.get('sha256')),'strict FP32 adapter tensor record differs: '+key)
    if variant==GPU:
        _need(all(row.get('device')=='cuda' for row in state.values()),'all adapter states must reside on CUDA')
    steps=component.get('steps');_need(isinstance(steps,list) and len(steps)==2,'exactly two measured SCR optimizer updates required')
    for i,row in enumerate(steps,1):
        _need(type(row.get('step')) is int and row['step']==i,'ordered typed update index required')
        _need(all(_finite(row.get(k)) for k in ('epsilon_mse','offset_term','scr_raw','scr_term','total','unclipped_gradient_norm'))
            and _finite(row.get('seconds'),True) and _finite(row.get('scr_query_gradient_abs_sum'),True),'finite live SCR losses/query gradients required')
        _need(math.isclose(row['scr_term'],.01*row['scr_raw'],rel_tol=1e-6,abs_tol=1e-8)
            and math.isclose(row['total'],row['epsilon_mse']+row['offset_term']+row['scr_term'],rel_tol=1e-5,abs_tol=1e-7),'fixed SCR loss coefficient/total differs')
        gradients=row.get('adapter_gradients')
        _need(isinstance(gradients,dict) and set(gradients)==set(shapes),'all 56 named gradients required; a count cannot substitute')
        for key,g in gradients.items():
            _need(isinstance(g,dict) and _equal(g.get('shape'),shapes[key]) and g.get('dtype')=='float32'
                and g.get('finite') is True and _finite(g.get('abs_sum'),True),'finite nonzero named gradient required: '+key)
        _need(row.get('base_gradients_absent') is True,'base gradients must remain absent')
        if variant==GPU:
            _need(row.get('scr_query_gradient_device')=='cuda' and all(g.get('device')=='cuda' for g in gradients.values()),'live query and all56 gradients must reside on CUDA')
            validate_cuda_observation(row.get('cuda_allocation_observation'))
    before=component.get('base_state_before_sha256')
    _need(_hash(before) and before==component.get('base_state_after_sha256'),'base tensors changed')
    delta=component.get('delta_roundtrip',{})
    _need(delta.get('weights_only') is True and delta.get('strict_load') is True and delta.get('equal') is True
        and delta.get('full_trainer_resume_claimed') is False and type(delta.get('bytes')) is int
        and 0<delta['bytes']<=128*2**20 and all(_hash(delta.get(k)) for k in ('adapter_sha256','optimizer_sha256','state_file_sha256')),'small strict weights-only roundtrip required')
    from .spatial_topology import SPATIAL_HEADS
    from .adapter_topology import RESIDUAL_HOOK_KEYS
    _need(_equal(component.get('spatial_generation_hook_calls'),{h['module']:20 for h in SPATIAL_HEADS})
        and _equal(component.get('residual_generation_hook_calls'),{k:20 for k in RESIDUAL_HOOK_KEYS}),'all residual/spatial generation hooks must execute 20 times')
    counts=component.get('operator_execution_counts');modules={f'unet.up_blocks.{b}.dcn_deforms.{i}' for b in (1,2) for i in range(3)}
    if variant==GPU:
        from .windows_gpu_support import validate_bridge_parity
        validate_bridge_parity(component.get('cuda_cpu_deform_parity'))
        validate_mixed_counts(counts)
    else:_need(isinstance(counts,dict) and set(counts)==modules and all(type(r.get('forward_calls')) is int and r['forward_calls']>=22
        and type(r.get('cross_device_calls')) is int and r['cross_device_calls']==0 for r in counts.values()),'six CPU deform operators require both updates and native generation')
    gen=component.get('generation',{});ref=gen.get('reference',{});found=indexed.get(ref.get('sample_id'),{})
    _need(_equal(gen.get('steps'),20) and _equal(gen.get('output_count'),1) and gen.get('not_blind_quality_evidence') is True
        and gen.get('pixels_saved_before_guard') is True and found.get('split')=='train' and ref.get('label')==found.get('label')
        and ref.get('png_sha256')==found.get('source_png_sha256'),'one native generation with bound train reference and pre-guard pixels required')
    _need(_equal(component.get('inference_scr_feature_forwards'),0),'no SCR feature forward in inference')
    outputs=gen.get('outputs');_need(isinstance(outputs,list) and len(outputs)==1,'one complete native output required')
    from .user_scr_capability import _verify_single_output
    _verify_single_output(outputs[0])
    if variant==GPU:
        allocation=validate_cuda_observation(component.get('cuda_allocation_observation'))
        _need(all(row['cuda_allocation_observation']['peak_allocated_bytes']<=allocation['peak_allocated_bytes']
            and row['cuda_allocation_observation']['peak_reserved_bytes']<=allocation['peak_reserved_bytes']
            and all(row['cuda_allocation_observation'][k]==allocation[k] for k in ('device_index','free_before_bytes','total_bytes','allocator_limit_bytes','minimum_free_bytes','allocator_enforced')) for row in steps),'CUDA allocator peaks must be monotonic from the same device budget')
        _need(_equal(proof.get('resource_receipt',{}).get('cuda_allocation_observation'),allocation),'monitored CUDA resource observation differs')
    _resources(proof.get('resource_receipt'),variant)
    return {'status':'RECORDS_VALIDATED_ONLY','environment_identity_sha256':env,'verified_named_tensors':56,
        'server_capability_created':False,'may_prepare':False,'may_train':False,'may_resume':False,'may_publish':False}

def _resources(r,variant=CPU):
    _need(isinstance(r,dict) and r.get('schema')=='windows-scr-training-resource/1' and _equal(r.get('returncode'),0)
        and r.get('stop_reason') is None and r.get('monitor_failure') is None and r.get('cleanup_failures')==[], 'successful owned resource monitor required')
    fixed={'stop_bytes':int(4.5*2**30),'absolute_budget_bytes':5*2**30,'max_new_output_bytes':128*2**20,
        'existing_tmp_data_deleted':False,'only_identity_verified_owned_processes_terminated':True,'retry_count':0,
        'existing_processes_stopped':False,'GPU_used':variant==GPU}
    _need(all(_equal(r.get(k),v) for k,v in fixed.items()),'resource limits/process policy differs')
    if variant==GPU:validate_cuda_observation(r.get('cuda_allocation_observation'))
    peak=r.get('peak',{})
    _need(all(type(peak.get(k)) is int and peak[k]>=0 for k in ('tree_rss_bytes','tmp_used_bytes','combined_bytes','new_output_bytes'))
        and 0<peak['tree_rss_bytes']<=peak['combined_bytes']==peak['tree_rss_bytes']+peak['tmp_used_bytes']<4.5*2**30
        and peak['new_output_bytes']<=128*2**20 and _finite(r.get('elapsed_seconds'),True) and r['elapsed_seconds']<=900,'resource observations exceed the proposed budget')
    _need(type(r.get('tracked_pid')) is int and r['tracked_pid']>0 and _finite(r.get('tracked_create_time'),True),'exact newly owned process identity required')

def _regular_e(path):
    p=Path(path).absolute()
    _need(p.drive.upper()=='E:' and not any(x.is_symlink() for x in (p,*p.parents)) and p.is_file(),'regular E-drive consumer file required')
    return p.resolve()

def verify_complete_candidate(app,variant,proof,observed,python,snapshot,asset_paths,out,*,base_asset_root=None):
    """Independent file observations still cannot mint execution authority."""
    producer=Path(app)/PRODUCER
    require(producer.is_file(),'E_WINDOWS_SCR_PRODUCER_MISSING','Monitored Windows full SCR producer is not implemented; raw PASS files cannot qualify')
    result=verify_trial_records(app,variant,proof,observed)
    _need(proof.get('producer_sha256')==sha(producer) and proof.get('python_sha256')==sha(_regular_e(python)),'producer/interpreter bytes differ')
    from .runtime_variants import verify_asset_root
    _need(isinstance(base_asset_root,(str,Path)),'real consumer-local full base required')
    base=verify_asset_root(app,base_asset_root)
    _need(base['asset_identity_sha256']==proof.get('runtime_identity_sha256')
        and _equal(base['manifest']['base_npz_sha256'],proof.get('base_npz_sha256')),'actual full base identity differs')
    from engine.style.dataset.binding import verify_snapshot_binding
    component=proof['component'];binding=verify_snapshot_binding(snapshot,component['scr_data_binding']['snapshot_sha256'])
    _need(_equal(binding,component['snapshot_binding']),'actual frozen S03 differs')
    for path in asset_paths.values():_regular_e(path)
    from .user_scr_cache import validate_user_reference_cache
    cache=validate_user_reference_cache(asset_paths,binding)
    _need(_equal(cache['data_binding'],component['scr_data_binding']) and _equal(cache['cache_binding'],component['scr_cache_binding']),'actual cache bytes differ')
    parent=Path(out).resolve()/'cache'/proof['windows_cache_identity']['sha256']
    _need(_regular_e(asset_paths['reference_cache_manifest']).parent==parent
        and _regular_e(asset_paths['reference_cache_vectors']).parent==parent,'fresh Windows cache must occupy this trial namespace')
    output=component['generation']['outputs'][0]
    for name,digest_key in (('raw_rgb_file','raw_rgb_sha256'),('alpha_file','alpha_sha256')):
        _need(sha(_regular_e(Path(out)/output[name]))==output[digest_key],'saved native pixels changed')
    state=_regular_e(Path(out)/'adapter-optimizer-state.pt')
    _need(sha(state)==component['delta_roundtrip']['state_file_sha256'] and state.stat().st_size==component['delta_roundtrip']['bytes'],'small state bytes differ')
    return {**result,'status':'RECORDS_AND_LOCAL_BYTES_VALIDATED_ONLY','server_capability_created':False}
