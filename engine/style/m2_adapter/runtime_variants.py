"""Explicit execution bindings. Base asset lineage and numerical runtime are separate.

No model selection or quality gate is changed here. Registrations never become the
implicit default. External environments are read-only and never lifecycle-owned.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import uuid

SCHEMA='m2-runtime-binding/1'
CPU='cpu-cp312-v1'
GPU='fontdiffuser-win312-torch210-cu128-fp32-cpu-dcn-v1'
OFFSET_CPU='cpu-cp312-offset-delta-v1'
OFFSET_RECIPE_ID='m2-control-offset-delta-v1'
OFFSET_CAPABILITY_SCHEMA='m2-offset-delta-capability/2'
OFFSET_CONTRACT_PATH='config/offset-delta-capability-contract.json'
OFFSET_CONTRACT_SHA256='5ac188a1a1d8b33190c0686bf4d1b8b4bf34aa227b29ff66c0190e194355a4b9'
OFFSET_MEMORY_LIMIT=5*2**30
SPATIAL_CPU='cpu-cp312-spatial-offset-v1'
SPATIAL_RECIPE_ID='m2-control-spatial-offset-v1'
SPATIAL_CAPABILITY_SCHEMA='m2-spatial-offset-capability/1'
SPATIAL_CONTRACT_PATH='config/spatial-offset-capability-contract.json'
SPATIAL_CONTRACT_SHA256='032609e10ac3e714257c221db123af19fdf9f1678f6d1dec37b4788a543d89ed'
SCR_CPU='cpu-cp312-scr2n-spatial-v1'
SCR_RECIPE_ID='m2-scr-two-negative-spatial-v1'
SCR_OBJECTIVE_PROFILE='epsilon-offset-scr2n-v1'
SCR_CAPABILITY_SCHEMA='m2-scr-two-negative-capability/1'
SCR_CONTRACT_PATH='config/scr-two-negative-capability-contract.json'
SCR_CONTRACT_SHA256='8927a01cfe24ec6942ec85b7e1f3c6ebc477d651fc221f04df6373910314c4eb'
# Canonical name -> {shape,dtype,numel} projection of the already pinned
# 9bcf1201... state-schema JSON. Verification never opens an SCR asset path.
SCR_STATE_LAYOUT_SHA256='fdc06487ac3e14e5714c4168e94038a319ffdbef3b74a755954177c9d7d2e8be'
from .user_scr_capability import VARIANT_ID as USER_SCR_CPU, CAPABILITY_SCHEMA as USER_SCR_CAPABILITY_SCHEMA
GEOMETRY_CPU_VARIANTS=(OFFSET_CPU,SPATIAL_CPU,SCR_CPU)
BINDING_KEYS=('registration_id','variant_id','device','asset_identity_sha256','dependency_lock_sha256','execution_profile_sha256','environment_identity_sha256','capability_receipt_sha256')

class RuntimeUnavailable(ValueError):
    def __init__(self, code, detail):
        self.code=code;super().__init__(f'{code}: {detail}')

def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def sha(path):
    from .trainer_resume_protocol import native_path
    h=hashlib.sha256()
    with native_path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()
def value_sha(value):return hashlib.sha256(canonical(value)).hexdigest()
def require(condition, code, detail):
    if not condition:raise RuntimeUnavailable(code,detail)

def safe_relative(root, rel):
    require(isinstance(rel,str) and bool(rel),'E_RUNTIME_PATH','empty path')
    # Receipts created on Windows legitimately use backslashes. Normalize once,
    # then reject absolute, traversal, duplicate-normalized and drive paths.
    rel=rel.replace('\\','/')
    p=PurePosixPath(rel)
    require(not p.is_absolute() and ':' not in rel and all(x not in ('','.','..') for x in rel.split('/')),'E_RUNTIME_PATH','unsafe relative path')
    root=Path(root).resolve();target=root.joinpath(*p.parts)
    require(target.resolve().is_relative_to(root),'E_RUNTIME_PATH','path escapes application')
    cur=target
    while cur!=root:
        require(not cur.is_symlink(),'E_RUNTIME_PATH','symlink in runtime path');cur=cur.parent
    return target

def catalog(app):
    app=Path(app).resolve();c=json.loads((app/'config/m2-runtime-variants.json').read_text(encoding='utf-8'))
    require(isinstance(c,dict) and c.get('schema')=='m2-runtime-variants/1' and isinstance(c.get('variants'),dict),'E_RUNTIME_CATALOG','unsupported schema')
    from .release_runtime_policy import validate_catalog
    validate_catalog(c)
    return c

def specification(app, variant_id):
    c=catalog(app);s=c['variants'].get(variant_id)
    require(s is not None,'E_RUNTIME_VARIANT','unknown explicit variant')
    body={k:v for k,v in s.items() if k!='execution_profile_sha256'}
    require(value_sha(body)==s.get('execution_profile_sha256'),'E_RUNTIME_CATALOG','execution profile hash mismatch')
    require(sha(safe_relative(app,s['lock']))==s['lock_sha256'],'E_RUNTIME_LOCK','dependency lock changed')
    if c['release_policy']['variants'][variant_id]['status']=='DISABLED':return c,s
    from .windows_user_scr import VARIANTS as WINDOWS_USER_SCR_VARIANTS, validate_profile as validate_windows_profile
    if variant_id in WINDOWS_USER_SCR_VARIANTS:validate_windows_profile(app,variant_id,s)
    if variant_id==USER_SCR_CPU:
        from .user_scr_capability import validate_profile
        validate_profile(s)
    if variant_id in GEOMETRY_CPU_VARIANTS:
        profile=_cpu_geometry_profile(variant_id)
        require(all(_strict_equal(s.get(key),value) for key,value in profile['topology'].items()),'E_ADAPTER_TOPOLOGY','geometry CPU profile requires its exact canonical topology')
        require(s.get('device')=='cpu' and s.get('full_model_probe_required') is True and s.get('recipe_id')==profile['recipe_id'] and s.get('capability_schema')==profile['schema'],'E_OFFSET_RUNTIME_PROFILE','geometry CPU profile requires its explicit topology and full capability proof')
        if variant_id==SCR_CPU:
            _verify_scr_provenance(s)
            require(s.get('objective_profile')==SCR_OBJECTIVE_PROFILE,'E_SCR_PROVENANCE','SCR catalog objective identity differs')
    return c,s

def versions_from_lock(path):
    versions={}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        m=re.match(r'^([A-Za-z0-9_.-]+)==([^\s\\;]+)',line)
        if m:versions[m[1].lower().replace('_','-')]=m[2]
    require(bool(versions),'E_RUNTIME_LOCK','empty lock')
    return versions

def fresh_environment(app, python, spec):
    """Read-only probe of the selected interpreter; never pip-install into it."""
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', PYGAME_HIDE_SUPPORT_PROMPT='1', CUBLAS_WORKSPACE_CONFIG=':4096:8')
    # Selection is an explicit argument. Legacy overrides cannot redirect this probe.
    for key in ('PYTHONPATH','PYTHONHOME','YY_STYLE_M2_PYTHON','YY_M2_RUNTIME_MANIFEST','YY_M2_DEVICE','YY_RENDER_M2_DEVICE'):
        env.pop(key,None)
    request={'device':spec['device'],'wanted':versions_from_lock(Path(app)/spec['lock']),'cuda_runtime':spec.get('cuda_runtime'),'platforms':spec['platforms']}
    request['observe_gpu_profile']=spec['device']=='cuda' and bool(spec.get('required_engine_files')) and all(safe_relative(app,rel).is_file() and sha(safe_relative(app,rel))==expected for rel,expected in spec.get('required_engine_files',{}).items())
    offset_cpu=spec['device']=='cpu' and spec.get('capability_schema') in (OFFSET_CAPABILITY_SCHEMA,SPATIAL_CAPABILITY_SCHEMA,SCR_CAPABILITY_SCHEMA,USER_SCR_CAPABILITY_SCHEMA)
    if offset_cpu:
        require(spec.get('environment_probe_sha256')==sha(safe_relative(app,'installer/probe_runtime_variant.py')),'E_ENVIRONMENT_PROBE_CODE','CPU environment observer differs from catalog pin')
    request['observe_numerical_profile']=offset_cpu and bool(spec.get('required_engine_files')) and _normalized_hashes(spec['required_engine_files'])==_engine_inventory(app)
    result=subprocess.run([str(python),'-I','-B','-X','utf8',str(Path(app)/'installer/probe_runtime_variant.py'),json.dumps(request),str(Path(app).resolve())],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120,env=env)
    try:body=json.loads(result.stdout.splitlines()[-1])
    except (ValueError,IndexError):raise RuntimeUnavailable('E_ENVIRONMENT_PROBE',result.stderr[-1200:] or 'no valid probe receipt')
    require(result.returncode==0 and body.get('ok') is True,'E_ENVIRONMENT_PROBE',str(body.get('error') or result.stderr[-1200:]))
    require(body.get('versions')==request['wanted'],'E_DEPENDENCY_VERSIONS','observed versions differ from lock')
    return body

def verify_asset_root(app, root):
    from installer.runtime_addon import verify_assets,identity
    m=verify_assets(root,app)
    require(identity(m)==catalog(app)['asset_identity_sha256'],'E_ASSET_IDENTITY','asset catalog mismatch')
    return {'asset_identity_sha256':identity(m),'manifest_path':str((Path(root)/'runtime-manifest.json').resolve()),'fd_repo':str((Path(root)/'FontDiffuser-Gradio').resolve()),'manifest':m,'engine_id':m['engine_id']}

def _normalized_hashes(value):
    require(isinstance(value,dict) and bool(value),'E_PROBE_CODE','missing code hashes')
    normalized={}
    for rel,digest in value.items():
        require(isinstance(rel,str),'E_PROBE_CODE','code path must be a string')
        rel=rel.replace('\\','/')
        require(rel not in normalized,'E_PROBE_CODE','duplicate normalized code path')
        require(isinstance(digest,str) and re.fullmatch('[0-9a-f]{64}',digest) is not None,'E_PROBE_CODE','invalid code digest')
        normalized[rel]=digest
    return normalized

def _strict_equal(actual,expected):
    """JSON identity includes scalar types: true, 1 and 1.0 are distinct."""
    try:
        return json.dumps(actual,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)==json.dumps(expected,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    except (TypeError,ValueError):return False

def _finite_number(value):
    if type(value) not in (int,float):return False
    try:return math.isfinite(value)
    except OverflowError:return False

def _verify_scr_provenance(record):
    """Validate inherited identities without opening any receipt-supplied paths."""
    from .scr_contract import validate_scr_provenance
    try:validate_scr_provenance(record)
    except (ValueError,TypeError,KeyError) as exc:
        raise RuntimeUnavailable('E_SCR_PROVENANCE',str(exc)) from exc
    for key in ('snapshot_sha256','dataset_snapshot_sha256'):
        if key in record:
            require(record[key]==record['scr_assets']['snapshot_sha256'],'E_SCR_PROVENANCE','SCR cached references belong to a different snapshot')

def _verify_scr_training_evidence(proof,*,core=False):
    """Measured training evidence is required independently of capability admission."""
    for path in ('scr_feature_state.weights_only','scr_feature_state.strict_load','scr_feature_state.all_finite',
                 'scr_feature_state.eval','scr_feature_state.all_parameters_frozen','scr_feature_state.parameter_gradients_absent',
                 'scr_feature_state.equal','scr_cached_reference_proof.pixel_hashes_checked','scr_rng_preserved'):
        _offset_assertion(proof,path,True)
    for path,wanted in {'scr_feature_state.state_keys':195,'scr_cached_reference_proof.rows':72,
                        'scr_cached_reference_proof.train_rows':51,'scr_cached_reference_proof.val_rows':21,
                        'scr_cached_reference_proof.negative_count':2,
                        'scr_cached_reference_proof.positive_negative_forward_calls':0}.items():
        _offset_assertion(proof,path,wanted)
    state=proof.get('scr_feature_state')
    require(isinstance(state,dict),'E_SCR_FEATURE_STATE','frozen SCR feature state evidence missing')
    before=state.get('before_sha256');after=state.get('after_sha256')
    require(isinstance(before,str) and re.fullmatch('[0-9a-f]{64}',before) is not None and before==after,'E_SCR_FEATURE_STATE','SCR feature state must remain byte-identical')
    evidence=proof.get('scr_execution_evidence')
    require(isinstance(evidence,dict) and evidence.get('schema')=='m2-scr2n-objective-evidence/1' and evidence.get('synthetic_test_only') is False,'E_SCR_FEATURE_STATE','complete actual SCR execution evidence required')
    require(all(_strict_equal(evidence.get(key),proof.get(key)) for key in ('scr_feature_state','scr_cached_reference_proof','scr_rng_preserved')),'E_SCR_FEATURE_STATE','projected SCR evidence disagrees with its source record')
    states=[evidence.get('model_state_before'),evidence.get('model_state_after')]
    require(all(isinstance(record,dict) and len(record)==195 for record in states) and _strict_equal(*states),'E_SCR_FEATURE_STATE','all 195 SCR state records must remain identical')
    require(all(value_sha(record)==before for record in states),'E_SCR_FEATURE_STATE','SCR state digests do not recompute from full state records')
    require(evidence.get('full_state_sha256')==before,'E_SCR_FEATURE_STATE','full SCR state digest differs')
    observed=evidence.get('checkpoint_observed_state')
    require(isinstance(observed,dict) and set(observed)==set(states[0]),'E_SCR_FEATURE_STATE','exact observed checkpoint state keys required')
    layout={}
    for name,row in states[0].items():
        require(isinstance(name,str) and bool(name) and isinstance(row,dict) and set(row)=={'shape','dtype','sha256'},'E_SCR_FEATURE_STATE','typed named SCR state records required')
        shape=row['shape']
        require(isinstance(shape,list) and all(type(size) is int and size>=0 for size in shape) and row['dtype'] in ('torch.float32','torch.int64'),'E_SCR_FEATURE_STATE','SCR tensor dimensions/dtype differ')
        require(isinstance(row['sha256'],str) and re.fullmatch('[0-9a-f]{64}',row['sha256']) is not None,'E_SCR_FEATURE_STATE','SCR tensor digest is malformed')
        checkpoint=observed[name]
        require(isinstance(checkpoint,dict) and set(checkpoint)=={'shape','dtype','numel','finite','layout'},'E_SCR_FEATURE_STATE','complete observed SCR checkpoint tensor records required')
        require(_strict_equal(checkpoint['shape'],shape) and checkpoint['dtype']==row['dtype'].removeprefix('torch.') and type(checkpoint['numel']) is int and checkpoint['numel']==math.prod(shape) and checkpoint['finite'] is True and checkpoint['layout']=='torch.strided','E_SCR_FEATURE_STATE','observed SCR checkpoint tensor differs from unchanged state')
        layout[name]={key:checkpoint[key] for key in ('shape','dtype','numel')}
    require(value_sha(layout)==SCR_STATE_LAYOUT_SHA256,'E_SCR_FEATURE_STATE','SCR tensor names/shapes/dtypes differ from the pinned 195-key static layout')
    for key,wanted in {'weights_only':True,'map_location':'cpu','partial_load':False,'prefix_stripping':False,
                       'frozen_eval':True,'trainable_parameter_count':0,'reference_feature_forwards':0,
                       'reference_tensors_gradient_absent':True,'generated_feature_cache_lookups':0,
                       'rng_preserved_during_construction':True,'feature_rng_unchanged':True,
                       'registered_in_wrapper_parameters_or_state':False}.items():
        require(_strict_equal(evidence.get(key),wanted),'E_SCR_FEATURE_STATE','SCR loading/freezing invariant differs: '+key)
    snapshot=evidence.get('snapshot_binding')
    require(isinstance(snapshot,dict) and snapshot.get('snapshot_sha256')==proof['scr_assets']['snapshot_sha256'],'E_SCR_PROVENANCE','nested SCR snapshot differs from canonical reference cache')
    for key in ('snapshot','snapshot_binding'):
        if key in proof:require(_strict_equal(proof[key],snapshot),'E_SCR_PROVENANCE','nested SCR snapshot differs from its parent receipt')
    cache_hashes=evidence.get('cache_tensor_sha256')
    require(isinstance(cache_hashes,list) and len(cache_hashes)==2 and all(isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None for value in cache_hashes),'E_SCR_FEATURE_STATE','both detached cache tensor digests required')
    calls=evidence.get('completed_loss_calls');rows=evidence.get('reference_row_lookups')
    require(all(isinstance(record,dict) and set(record)=={'train','val'} and all(type(value) is int and value>=0 for value in record.values()) for record in (calls,rows)),'E_SCR_EXECUTION_COUNTS','typed nonnegative train/val execution totals required')
    require(calls['train']>0 and rows['train']==2*calls['train'] and (calls['val']==0 and rows['val']==0 or calls['val']>0 and calls['val']<=rows['val']<=2*calls['val']),'E_SCR_EXECUTION_COUNTS','SCR phase rows must agree with actual batch calls')
    totals={'prediction_feature_forwards':sum(calls.values()),'prediction_feature_images':sum(rows.values()),
            'positive_vector_lookups':4*sum(rows.values()),'negative_vector_lookups':8*sum(rows.values())}
    for key,wanted in totals.items():
        require(type(evidence.get(key)) is int and evidence[key]==wanted,'E_SCR_EXECUTION_COUNTS','SCR prediction/reference totals differ: '+key)
    checks=proof['scr_cached_reference_proof'].get('pixel_hash_check_count')
    require(type(checks) is int and checks>0 and checks==totals['prediction_feature_images'],'E_SCR_EXECUTION_COUNTS','positive pixel checks must equal all actual prediction images')
    if core:
        require(_strict_equal(calls,{'train':2,'val':0}) and _strict_equal(rows,{'train':4,'val':0}),'E_SCR_EXECUTION_COUNTS','core proof requires exactly two train calls and four prediction images')

def _verify_scr_proof(proof):
    _verify_scr_provenance(proof)
    _verify_scr_training_evidence(proof,core=True)
    gradient=proof.get('gradient_feasibility_receipt_sha256')
    require(isinstance(gradient,str) and re.fullmatch('[0-9a-f]{64}',gradient) is not None,'E_SCR_GRADIENT_PROOF','gradient feasibility receipt identity missing')
    for row in proof['steps']:
        require(all(_finite_number(row.get(key)) and row[key]>=0 for key in ('scr_raw','scr_term')),'E_SCR_LOSS','finite nonnegative SCR loss measurements required')
        require(math.isclose(row['scr_term'],.01*row['scr_raw'],rel_tol=1e-6,abs_tol=1e-8),'E_SCR_LOSS','SCR term must use the fixed 0.01 coefficient')
        require(math.isclose(row['loss'],row['epsilon_mse']+row['offset_term']+row['scr_term'],rel_tol=1e-5,abs_tol=1e-7),'E_SCR_LOSS','total loss must equal epsilon, offset and fixed SCR terms')

def _offset_contract(app):
    path=safe_relative(app,OFFSET_CONTRACT_PATH)
    require(path.is_file() and sha(path)==OFFSET_CONTRACT_SHA256,'E_OFFSET_CAPABILITY_CONTRACT','frozen offset capability assertions changed or missing')
    contract=json.loads(path.read_text(encoding='utf-8'))
    require(contract.get('schema')=='offset-delta-capability-assertions/2' and contract.get('receipt_schema')==OFFSET_CAPABILITY_SCHEMA,'E_OFFSET_CAPABILITY_CONTRACT','revision 2 capability contract required')
    return contract

def _cpu_geometry_profile(variant_id):
    """Only explicitly named experiments reuse the common CPU capability gate."""
    if variant_id==OFFSET_CPU:
        from .adapter_topology import OFFSET_TOPOLOGY_ID,OFFSET_HEADS,topology_fields
        from .offset_recipe import offset_recipe_contract
        return {'variant_id':OFFSET_CPU,'schema':OFFSET_CAPABILITY_SCHEMA,'recipe_id':OFFSET_RECIPE_ID,
                'topology':topology_fields(OFFSET_TOPOLOGY_ID),'recipe':offset_recipe_contract(),
                'contract_path':OFFSET_CONTRACT_PATH,'contract_sha':OFFSET_CONTRACT_SHA256,
                'contract_schema':'offset-delta-capability-assertions/2','heads':[head['module'] for head in OFFSET_HEADS],
                'counter_field':'offset_execution_counts','repeat_counter_field':'offset_projection_hook_calls',
                'producer':'scripts/probe_m2_runtime_offset_delta.py'}
    if variant_id==SCR_CPU:
        from .scr_recipe import scr_recipe_contract
        from .spatial_topology import SPATIAL_TOPOLOGY_ID,SPATIAL_HEADS
        from .topology_dispatch import topology_fields
        return {'variant_id':SCR_CPU,'schema':SCR_CAPABILITY_SCHEMA,'recipe_id':SCR_RECIPE_ID,
                'topology':topology_fields(SPATIAL_TOPOLOGY_ID),'recipe':scr_recipe_contract(),
                'contract_path':SCR_CONTRACT_PATH,'contract_sha':SCR_CONTRACT_SHA256,
                'contract_schema':'scr-two-negative-capability-assertions/1','heads':[head['module'] for head in SPATIAL_HEADS],
                'counter_field':'spatial_execution_counts','repeat_counter_field':'spatial_offset_hook_calls',
                'producer':'scripts/probe_m2_runtime_scr_two_negative.py'}
    require(variant_id==SPATIAL_CPU,'E_RUNTIME_VARIANT','unknown geometry CPU variant')
    from .spatial_topology import SPATIAL_TOPOLOGY_ID,SPATIAL_HEADS
    from .topology_dispatch import topology_fields
    from .spatial_recipe import spatial_recipe_contract
    return {'variant_id':SPATIAL_CPU,'schema':SPATIAL_CAPABILITY_SCHEMA,'recipe_id':SPATIAL_RECIPE_ID,
            'topology':topology_fields(SPATIAL_TOPOLOGY_ID),'recipe':spatial_recipe_contract(),
            'contract_path':SPATIAL_CONTRACT_PATH,'contract_sha':SPATIAL_CONTRACT_SHA256,
            'contract_schema':'spatial-offset-capability-assertions/1','heads':[head['module'] for head in SPATIAL_HEADS],
            'counter_field':'spatial_execution_counts','repeat_counter_field':'spatial_offset_hook_calls',
            'producer':'scripts/probe_m2_runtime_spatial_offset.py'}

def _geometry_contract(app,profile):
    path=safe_relative(app,profile['contract_path'])
    require(path.is_file() and sha(path)==profile['contract_sha'],'E_OFFSET_CAPABILITY_CONTRACT','frozen geometry capability assertions changed or missing')
    contract=json.loads(path.read_text(encoding='utf-8'))
    require(contract.get('schema')==profile['contract_schema'] and contract.get('receipt_schema')==profile['schema'],'E_OFFSET_CAPABILITY_CONTRACT','wrong geometry capability contract')
    return contract

def _verify_spatial_variation(proof,heads):
    variation=proof.get('spatial_variation_probe')
    require(isinstance(variation,dict) and isinstance(variation.get('heads'),dict) and set(variation['heads'])==set(heads),'E_SPATIAL_VARIATION','all six exact spatial heads must have measured variation')
    require(variation.get('activation_archive_file')=='spatial-variation-arrays.npz' and isinstance(variation.get('activation_archive_sha256'),str) and re.fullmatch('[0-9a-f]{64}',variation['activation_archive_sha256']) is not None,'E_SPATIAL_VARIATION','spatial array evidence requires its exact generated basename and byte hash')
    for name,row in variation['heads'].items():
        require(isinstance(row,dict) and row.get('finite') is True,'E_SPATIAL_VARIATION','finite head evidence required: '+name)
        for field in ('normalized_input','learned_delta'):
            measured=row.get(field)
            required={'rms','spatial_centered_rms','relative_spatial_rms','max_abs'}
            require(isinstance(measured,dict) and all(key in measured and _finite_number(measured[key]) for key in required),'E_SPATIAL_VARIATION','finite spatial statistics missing: '+name+'.'+field)
            rms,centered,relative=(measured[key] for key in ('rms','spatial_centered_rms','relative_spatial_rms'))
            require(rms>0 and centered>0 and 0.01<=relative<=1.00001 and centered<=rms*1.00001,'E_SPATIAL_VARIATION','input or learned delta is spatially flat: '+name+'.'+field)
            ratio=centered/rms
            require(ratio>=0.01 and math.isclose(relative,ratio,rel_tol=1e-5,abs_tol=1e-8),'E_SPATIAL_VARIATION','spatial RMS ratio disagrees with measurements: '+name+'.'+field)
            require(measured['max_abs']>0 and rms<=measured['max_abs']*1.00001,'E_SPATIAL_VARIATION','spatial RMS exceeds the measured maximum: '+name+'.'+field)
            if field=='learned_delta':
                require(measured['max_abs']>=1e-7,'E_SPATIAL_VARIATION','learned spatial delta is too small: '+name)

def _engine_inventory(app):
    root=Path(app).resolve()
    return {p.relative_to(root).as_posix():sha(safe_relative(root,p.relative_to(root).as_posix()))
            for p in (root/'engine').rglob('*.py') if '__pycache__' not in p.parts}

def _offset_assertion(proof,path,expected):
    actual=proof
    for key in path.split('.'):
        require(isinstance(actual,dict) and key in actual,'E_OFFSET_CAPABILITY_ASSERTION','missing assertion: '+path)
        actual=actual[key]
    require(_strict_equal(actual,expected),'E_OFFSET_CAPABILITY_ASSERTION','invalid assertion value/type: '+path)

def _verify_offset_repeat_outputs(repeat):
    """Check metadata coherence only; never follow receipt-supplied file paths.

    Source-bound production writes both images before its existing ink guard.
    The separate audit verifies those image bytes against these declarations.
    """
    outputs=repeat.get('outputs')
    require(isinstance(outputs,list) and len(outputs)==2,'E_MODEL_REPEAT','exactly two native repeat output records required')
    for key in ('sha256','raw_rgb_sha256'):
        hashes=repeat.get(key)
        require(isinstance(hashes,list) and len(hashes)==2 and all(isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None for value in hashes) and hashes[0]==hashes[1],'E_MODEL_REPEAT','two identical valid repeat byte hashes required: '+key)
    fields={'index','width','height','raw_rgb_file','raw_rgb_sha256','alpha_file','alpha_sha256','alpha_min','alpha_max','alpha_sum','opaque_pixels_threshold128','opaque_fraction','passes_original_ink_guard','pixels_saved_before_guard'}
    statistics=('width','height','alpha_min','alpha_max','alpha_sum','opaque_pixels_threshold128','opaque_fraction')
    for index,row in enumerate(outputs):
        require(isinstance(row,dict) and set(row)==fields,'E_MODEL_REPEAT','complete native output statistics required')
        require(type(row.get('index')) is int and row['index']==index and type(row.get('width')) is int and row['width']==96 and type(row.get('height')) is int and row['height']==96,'E_MODEL_REPEAT','two ordered native 96 by 96 outputs required')
        require(row.get('raw_rgb_file')==f'probe-train-glyph-{index}-raw-rgb.png' and row.get('alpha_file')==f'probe-train-glyph-{index}.png','E_MODEL_REPEAT','output records must use the exact generated image basenames')
        require(row.get('raw_rgb_sha256')==repeat['raw_rgb_sha256'][index] and row.get('alpha_sha256')==repeat['sha256'][index],'E_MODEL_REPEAT','output byte hashes differ from repeat lists')
        require(row.get('passes_original_ink_guard') is True and row.get('pixels_saved_before_guard') is True,'E_MODEL_REPEAT','both image outputs must be saved before passing the unchanged ink guard')
        require(all(type(row.get(key)) is int for key in ('alpha_min','alpha_max','alpha_sum','opaque_pixels_threshold128')),'E_MODEL_REPEAT','alpha statistics and opaque count must be integers')
        low,high,total,opaque=(row[key] for key in ('alpha_min','alpha_max','alpha_sum','opaque_pixels_threshold128'))
        pixels=row['width']*row['height']
        require(0<=low<128<=high<=255 and 16<=opaque<=.6*pixels,'E_MODEL_REPEAT','native repeat violates the unchanged alpha/ink limits')
        # Both declared extrema must occur at least once. All other opaque
        # pixels lie in [128, high], and all other pixels in [low, 127].
        minimum_sum=high+(opaque-1)*128+(pixels-opaque)*low
        maximum_sum=opaque*high+(pixels-opaque-1)*127+low
        require(minimum_sum<=total<=maximum_sum,'E_MODEL_REPEAT','alpha sum is inconsistent with the declared extrema and opaque count')
        require(type(row.get('opaque_fraction')) is float and math.isfinite(row['opaque_fraction']) and row['opaque_fraction']==opaque/pixels,'E_MODEL_REPEAT','opaque fraction differs from the exact measured count')
    require(all(_strict_equal(outputs[0][key],outputs[1][key]) for key in statistics),'E_MODEL_REPEAT','identical repeat images must have identical pixel statistics')

def _verify_offset_cpu_proof(app,spec,proof,environment,python,*,spatial=False,scr=False):
    """CPU-only model-core capability; this cannot confer product/quality approval."""
    if spatial:
        from .topology_dispatch import validate_inference_topology_execution
    else:
        from .adapter_topology import validate_inference_topology_execution
    profile=_cpu_geometry_profile(SCR_CPU if scr else SPATIAL_CPU if spatial else OFFSET_CPU)
    contract=_geometry_contract(app,profile)
    require(isinstance(proof,dict),'E_FULL_MODEL_PROBE_REQUIRED','run the full offset-delta capability probe on this exact source')
    require(isinstance(environment,dict),'E_ENVIRONMENT_PROBE','current environment observation must be an object')
    fixed={'schema':profile['schema'],'status':'PASS','variant_id':profile['variant_id'],'device':'cpu',
           'recipe_id':profile['recipe_id'],'objective_profile':contract['objective_profile']}
    require(all(_strict_equal(proof.get(key),value) for key,value in fixed.items()),'E_FULL_MODEL_PROBE','explicit CPU offset-delta full-model PASS required')
    require(spec.get('device')=='cpu' and spec.get('full_model_probe_required') is True and spec.get('capability_schema')==profile['schema'] and spec.get('recipe_id')==profile['recipe_id'],'E_OFFSET_RUNTIME_PROFILE','wrong geometry CPU execution profile')
    topology=profile['topology']
    for key,value in topology.items():
        require(_strict_equal(spec.get(key),value) and _strict_equal(proof.get(key),value),'E_ADAPTER_TOPOLOGY','canonical offset topology required: '+key)
    recipe=profile['recipe']
    require(_strict_equal(spec.get('training_recipe'),recipe) and _strict_equal(proof.get('training_recipe'),recipe),'E_PROBE_RECIPE','canonical offset recipe required')
    if scr:_verify_scr_provenance(spec)
    require(spec.get('capability_contract_sha256')==profile['contract_sha'] and proof.get('capability_contract_sha256')==profile['contract_sha'],'E_OFFSET_CAPABILITY_CONTRACT','frozen capability contract binding missing')
    require(spec.get('environment_probe_sha256')==sha(safe_relative(app,'installer/probe_runtime_variant.py')),'E_ENVIRONMENT_PROBE_CODE','CPU environment observer differs from catalog pin')
    require(spec.get('execution_profile_sha256')==value_sha({k:v for k,v in spec.items() if k!='execution_profile_sha256'}),'E_RUNTIME_CATALOG','execution profile changed')
    require(proof.get('runtime_identity_sha256')==catalog(app)['asset_identity_sha256'],'E_FULL_MODEL_PROBE','base asset identity mismatch')
    executable=proof.get('executable')
    require(isinstance(executable,str) and bool(executable) and Path(executable).is_absolute() and os.path.normcase(os.path.abspath(executable))==os.path.normcase(str(Path(python).absolute())),'E_PROBE_INTERPRETER','probe belongs to a different interpreter')
    require(spec.get('lock')=='requirements-personal-style-runtime.lock' and proof.get('dependency_lock_sha256')==spec.get('lock_sha256')==sha(safe_relative(app,spec['lock'])),'E_RUNTIME_LOCK','offset CPU dependency lock differs')
    versions=versions_from_lock(safe_relative(app,spec['lock']))
    require(_strict_equal(proof.get('dependencies'),versions) and _strict_equal(environment.get('versions'),versions),'E_DEPENDENCY_VERSIONS','complete observed versions must match the CPU lock')

    numeric=proof.get('numerical_environment')
    require(isinstance(numeric,dict) and numeric.get('schema')=='r9-numerical-environment/2' and numeric.get('device')=='cpu','E_NUMERICAL_ENVIRONMENT','full CPU numerical schema required')
    numerical_keys={'schema','python','platform','packages','device','cuda_runtime','cudnn_version','gpu_name','freetype_version','torch_deterministic_algorithms','matmul_tf32','cudnn_tf32','dtype','amp','default_dtype','cublas_workspace_config','cudnn_benchmark','cudnn_deterministic','torch_cpu_threads','torch_interop_threads','operator_execution_policy','mixed_cuda_cpu_deform','identity_sha256'}
    require(set(numeric)==numerical_keys,'E_NUMERICAL_ENVIRONMENT','complete numerical profile fields required')
    require(numeric.get('identity_sha256')==value_sha({k:v for k,v in numeric.items() if k!='identity_sha256'}),'E_NUMERICAL_ENVIRONMENT','numerical identity is malformed')
    require(_strict_equal(environment.get('numerical_environment'),numeric),'E_NUMERICAL_ENVIRONMENT_CHANGED','fresh full numerical observation differs from capability proof')
    policy=spec.get('operator_policy')
    require(isinstance(policy,dict) and _strict_equal(numeric.get('operator_execution_policy'),policy),'E_OPERATOR_POLICY','operator implementation differs')
    numeric_policy={'dtype':'float32','default_dtype':'torch.float32','amp':False,'torch_deterministic_algorithms':True,
                    'matmul_tf32':False,'cudnn_tf32':False,'cudnn_benchmark':False,'cudnn_deterministic':True,
                    'torch_cpu_threads':2,'torch_interop_threads':1,'mixed_cuda_cpu_deform':False,'gpu_name':None,
                    'cublas_workspace_config':':4096:8'}
    require(all(key in numeric and _strict_equal(numeric[key],wanted) for key,wanted in numeric_policy.items()),'E_NUMERICAL_POLICY','deterministic CPU FP32 policy required')
    package_names=('torch','torchvision','diffusers','transformers','accelerate','numpy','pillow','pygame','safetensors','fonttools','huggingface-hub')
    require(all(name in versions for name in package_names) and _strict_equal(numeric.get('packages'),{name:versions[name] for name in package_names}),'E_DEPENDENCY_VERSIONS','full numerical package observation differs from lock')
    basic=proof.get('binding_environment')
    require(isinstance(basic,dict) and basic.get('schema')=='m2-basic-numerical-environment/1' and basic.get('device')=='cpu' and _strict_equal(basic.get('versions'),versions),'E_NUMERICAL_ENVIRONMENT','basic CPU binding observation missing')
    require(set(basic)=={'schema','versions','python','platform','device','cuda_runtime','gpu_name','freetype_version','torch_cpu_threads','torch_interop_threads','deterministic_algorithms','dtype','identity_sha256'},'E_NUMERICAL_ENVIRONMENT','complete basic CPU binding profile fields required')
    require(basic.get('identity_sha256')==value_sha({k:v for k,v in basic.items() if k!='identity_sha256'}) and basic.get('identity_sha256')==environment.get('environment_identity_sha256'),'E_NUMERICAL_ENVIRONMENT_CHANGED','basic CPU binding identity differs from current environment')
    same_fields={'python':'python','platform':'platform','cuda_runtime':'cuda_runtime','gpu_name':'gpu_name','freetype_version':'freetype_version','torch_cpu_threads':'torch_cpu_threads','torch_interop_threads':'torch_interop_threads','deterministic_algorithms':'torch_deterministic_algorithms','dtype':'default_dtype'}
    require(all(key in basic and full in numeric and _strict_equal(basic[key],numeric[full]) for key,full in same_fields.items()),'E_NUMERICAL_ENVIRONMENT','basic and full numerical observations disagree')

    for key in ('claims_quality_pass','may_publish','training_candidate_created','app_entrypoint_tested','cuda_evidence'):
        require(proof.get(key) is False,'E_PROBE_SCOPE','CPU model-core capability cannot grant quality, publication, candidate, app-entrypoint or CUDA claims: '+key)
    for path in contract['required_true_fields']:_offset_assertion(proof,path,True)
    for path,wanted in contract['required_exact_fields'].items():_offset_assertion(proof,path,wanted)
    require(_strict_equal(proof['required_operator_parity_tests'].get('names'),['test_cpu_native_bit_exact_output_and_all_gradients','test_cpu_masked_bit_exact_output_and_all_gradients']),'E_OPERATOR_PARITY','the exact two CPU operator parity tests are required')
    steps=proof.get('steps')
    require(isinstance(steps,list) and len(steps)==2 and all(isinstance(row,dict) and type(row.get('step')) is int and row['step']==index for index,row in enumerate(steps,1)),'E_MODEL_STEPS','exactly two measured optimizer steps required')
    for row in steps:
        require(all(_finite_number(row.get(key)) for key in ('seconds','loss','epsilon_mse','offset_term','unclipped_grad_norm')),'E_MODEL_FINITE','nonfinite/missing optimizer step measurements')
        require(row['seconds']>0 and all(row[key]>=0 for key in ('loss','epsilon_mse','offset_term','unclipped_grad_norm')),'E_MODEL_FINITE','invalid optimizer step measurements')
    if scr:_verify_scr_proof(proof)
    modules=[f'unet.up_blocks.{block}.dcn_deforms.{index}' for block in (1,2) for index in range(3)]
    require(_strict_equal(spec.get('operator_modules'),modules),'E_OPERATOR_MODULES','catalog must specify the exact six deform modules')
    require(_strict_equal(proof.get('operator_execution_policy'),{**policy,'module_names':modules}),'E_OPERATOR_POLICY','exact six CPU deform modules and policy required')
    counts=proof.get('operator_execution_counts')
    require(isinstance(counts,dict) and set(counts)==set(modules) and all(isinstance(row,dict) and type(row.get('forward_calls')) is int and row['forward_calls']>=2 and type(row.get('cross_device_calls')) is int and row['cross_device_calls']==0 for row in counts.values()),'E_OPERATOR_EXECUTION','all six CPU deform operators must execute with zero cross-device calls')
    hooks=profile['heads']
    offsets=proof.get(profile['counter_field'])
    require(isinstance(offsets,dict) and set(offsets)==set(hooks) and all(isinstance(value,dict) and type(value.get('forward_calls')) is int and value['forward_calls']>=2 for value in offsets.values()),'E_OFFSET_EXECUTION','all six canonical offset projection hooks must execute')
    if spatial:
        require(_strict_equal(proof.get('n_spatial_offset_heads'),6) and _strict_equal(proof.get('n_offset_projection_heads'),0),'E_ADAPTER_TOPOLOGY','spatial capability requires six spatial heads and zero projection heads')
        require(_strict_equal(proof.get('offset_execution_counts'),{}),'E_OFFSET_EXECUTION','spatial capability cannot claim old projection delta execution')
        _verify_spatial_variation(proof,hooks)
    repeat=proof['repeat_inference'];hashes=repeat.get('sha256')
    require(isinstance(hashes,list) and len(hashes)==2 and all(isinstance(digest,str) and re.fullmatch('[0-9a-f]{64}',digest) is not None for digest in hashes) and hashes[0]==hashes[1],'E_MODEL_REPEAT','two identical valid repeat PNG hashes required')
    _verify_offset_repeat_outputs(repeat)
    require(repeat.get('sampler')=='official DPM core' and repeat.get('generator_device')=='cpu' and repeat.get('not_quality_evidence') is True,'E_MODEL_REPEAT','repeat must use official DPM core and CPU generator without quality claims')
    repeat_execution={**topology,'recipe_id':profile['recipe_id'],'source':'M2','adapter_applied':True,'adapter_hook_calls':repeat.get('adapter_hook_calls'),'offset_projection_hook_calls':repeat.get('offset_projection_hook_calls')}
    if spatial:
        repeat_execution['spatial_offset_hook_calls']=repeat.get('spatial_offset_hook_calls')
        require(_strict_equal(repeat.get('offset_projection_hook_calls'),{}),'E_MODEL_REPEAT','spatial repeat must not execute the old projection delta branch')
    try:validate_inference_topology_execution(repeat_execution)
    except ValueError as exc:raise RuntimeUnavailable('E_MODEL_REPEAT',str(exc)) from exc
    expected_repeat_calls=2*repeat['steps']
    require(all(type(count) is int and count==expected_repeat_calls for field in ('adapter_hook_calls',profile['repeat_counter_field']) for count in repeat[field].values()),'E_MODEL_REPEAT','every residual and geometry hook must execute exactly 40 times across the two 20-step repeats')
    require(all(row['forward_calls']>=expected_repeat_calls+len(steps) for row in (*counts.values(),*offsets.values())),'E_OPERATOR_EXECUTION','cumulative CPU deform/offset counts cannot omit the two optimizer forwards or repeat forwards')
    memory=proof['cpu_memory'];peak=memory.get('peak_rss_bytes')
    require(type(spec.get('max_memory_bytes')) is int and spec['max_memory_bytes']==OFFSET_MEMORY_LIMIT and type(peak) is int and 0<peak<=OFFSET_MEMORY_LIMIT,'E_MODEL_MEMORY','sampled process peak RSS missing or above 5 GiB')

    source=_normalized_hashes(proof.get('source_hashes'));actual=_engine_inventory(app)
    require(bool(actual) and source==actual and _normalized_hashes(spec.get('required_engine_files'))==actual,'E_PROBE_CODE_CHANGED','complete engine inventory must match proof and catalog pins')
    execution=_normalized_hashes(proof.get('execution_hashes'))
    require(set(execution)==set(contract['exact_execution_files']),'E_PROBE_CODE_CHANGED','exact complete execution-file scope required')
    expected_execution={rel:sha(safe_relative(app,rel)) for rel in contract['exact_execution_files']}
    require(execution==expected_execution and _normalized_hashes(spec.get('required_execution_files'))==expected_execution,'E_PROBE_CODE_CHANGED','execution source must match proof and catalog pins')
    require(proof.get('probe_script_sha256')==execution[profile['producer']],'E_PROBE_CODE_CHANGED','geometry capability producer binding missing')
    # CPU run bindings keep the basic identity used by measure_in_process; the
    # full observation is separately compared above on every capability check.
    return basic['identity_sha256']

def verify_full_model_proof(app,spec,proof,environment,python,*,user_scr_purpose='train'):
    """Research success is not portable to different product code or environments."""
    if spec.get('capability_schema')==USER_SCR_CAPABILITY_SCHEMA:
        from .user_scr_capability import verify_capability
        return verify_capability(app,spec,proof,environment,python,verify_feature_assets=user_scr_purpose!='inference')
    require(spec.get('execution_enabled') is True,'E_PRODUCT_ENGINE_NOT_RELEASED',spec.get('execution_blocker','product engine integration is pending'))
    if spec.get('capability_schema')==OFFSET_CAPABILITY_SCHEMA:
        return _verify_offset_cpu_proof(app,spec,proof,environment,python)
    if spec.get('capability_schema')==SPATIAL_CAPABILITY_SCHEMA:
        return _verify_offset_cpu_proof(app,spec,proof,environment,python,spatial=True)
    if spec.get('capability_schema')==SCR_CAPABILITY_SCHEMA:
        return _verify_offset_cpu_proof(app,spec,proof,environment,python,spatial=True,scr=True)
    for rel,expected in spec.get('required_engine_files',{}).items():
        p=safe_relative(app,rel)
        require(p.is_file() and sha(p)==expected,'E_GPU_ENGINE_PROFILE_NOT_INSTALLED','compatible engine implementation missing or changed: '+rel)
    require(isinstance(proof,dict),'E_FULL_MODEL_PROBE_REQUIRED','run the full-model capability probe on this product code')
    require(proof.get('schema') in ('r9-runtime-probe/1','m2-runtime-capability/1') and proof.get('status')=='PASS' and proof.get('device')=='cuda' and proof.get('cuda_evidence') is True,'E_FULL_MODEL_PROBE','actual CUDA full-model PASS required')
    control=proof.get('schema')=='m2-runtime-capability/1'
    if control:require(proof.get('objective_profile')=='epsilon-offset-control-v1','E_PROBE_OBJECTIVE','approved control objective profile required')
    require(proof.get('runtime_identity_sha256')==catalog(app)['asset_identity_sha256'],'E_FULL_MODEL_PROBE','base asset identity mismatch')
    require(os.path.normcase(os.path.abspath(proof.get('executable','')))==os.path.normcase(str(Path(python).absolute())),'E_PROBE_INTERPRETER','probe belongs to a different interpreter')
    numeric=proof.get('numerical_environment') or {}
    require(numeric.get('schema')=='r9-numerical-environment/2','E_NUMERICAL_ENVIRONMENT','full numerical schema required')
    require(numeric.get('identity_sha256')==value_sha({k:v for k,v in numeric.items() if k!='identity_sha256'}),'E_NUMERICAL_ENVIRONMENT','numerical identity is malformed')
    require(environment.get('numerical_environment')==numeric,'E_NUMERICAL_ENVIRONMENT_CHANGED','current numerical environment differs from full-model proof')
    require(numeric.get('operator_execution_policy')==spec['operator_policy'],'E_OPERATOR_POLICY','operator implementation differs')
    require(numeric.get('dtype')=='float32' and numeric.get('amp') is False and numeric.get('torch_deterministic_algorithms') is True and numeric.get('matmul_tf32') is False and numeric.get('cudnn_tf32') is False,'E_NUMERICAL_POLICY','FP32 deterministic policy required')
    require(proof.get('claims_quality_pass') is False and proof.get('may_publish') is False and proof.get('training_candidate_created') is False,'E_PROBE_SCOPE','capability proof cannot grant quality or publication')
    parity=proof.get('required_operator_parity_tests') or {}
    require(parity.get('successful') is True and parity.get('tests_run')==2 and parity.get('skipped')==0,'E_OPERATOR_PARITY','two actual parity tests required')
    require((proof.get('operator_execution_policy') or {}).get('module_names')==spec['operator_modules'],'E_OPERATOR_MODULES','exact six operator modules required')
    counts=proof.get('operator_execution_counts') or {}
    require(set(counts)==set(spec['operator_modules']) and all(isinstance(v,dict) and type(v.get('cross_device_calls')) is int and v['cross_device_calls']>=2 for v in counts.values()),'E_OPERATOR_EXECUTION','actual CPU bridge calls missing')
    gradient=proof.get('adapter_gradient_probe' if control else 'perceptual_gradient_probe') or {}
    frozen=proof.get('base_gradients_absent') if control else proof.get('base_and_vgg_gradients_absent')
    require(gradient.get('finite') is True and gradient.get('nonzero_adapter_tensors')==44 and proof.get('adapter_parameter_changed') is True and frozen is True,'E_MODEL_GRADIENT','finite adapter updates and frozen base required; perceptual probes additionally require frozen VGG')
    steps=proof.get('steps') or [];cap=spec['max_memory_bytes']
    require(len(steps)==2 and [x.get('step') for x in steps]==[1,2],'E_MODEL_STEPS','two measured optimizer steps required')
    for step in steps:
        require(all(isinstance(step.get(k),(int,float)) and not isinstance(step[k],bool) and math.isfinite(step[k]) for k in ('seconds','loss','unclipped_grad_norm')),'E_MODEL_FINITE','nonfinite/missing measured step')
        require(all(type(step.get(k)) is int and 0<step[k]<=cap for k in ('peak_allocated','peak_reserved')),'E_MODEL_MEMORY','step memory exceeds cap or missing')
    require(proof.get('memory_cap_bytes')==cap and all(type(proof.get(k)) is int and 0<proof[k]<=cap for k in ('peak_allocated_complete','peak_reserved_complete')),'E_MODEL_MEMORY','complete probe peak exceeds cap or missing')
    repeat=proof.get('train_side_repeat_inference') or {};hashes=repeat.get('sha256') or []
    require(repeat.get('equal') is True and len(hashes)==2 and hashes[0]==hashes[1] and re.fullmatch('[0-9a-f]{64}',str(hashes[0])) is not None,'E_MODEL_REPEAT','seeded repeat inference proof missing')
    source=_normalized_hashes(proof.get('source_hashes'))
    actual={p.relative_to(Path(app)).as_posix():sha(safe_relative(app,p.relative_to(Path(app)).as_posix())) for p in (Path(app)/'engine').rglob('*.py') if '__pycache__' not in p.parts}
    require(source==actual,'E_PROBE_CODE_CHANGED','full engine source inventory differs from successful probe')
    for rel,expected in _normalized_hashes(proof.get('execution_hashes')).items():
        p=safe_relative(app,rel);require(p.is_file() and sha(p)==expected,'E_PROBE_CODE_CHANGED','execution source differs: '+rel)
    producer='scripts/probe_m2_runtime_control.py' if control else 'scripts/probe_r9_runtime.py'
    require(proof.get('probe_script_sha256')==proof['execution_hashes'].get(producer),'E_PROBE_CODE_CHANGED','probe script binding missing')
    if control:
        require(proof.get('app_entrypoint_tested') is False,'E_PROBE_SCOPE','bootstrap model probe must distinguish app-entrypoint smoke')
        require(proof.get('dependency_lock_sha256')==spec['lock_sha256'],'E_RUNTIME_LOCK','control probe dependency lock differs')
    return numeric['identity_sha256']

def _registry(app):return safe_relative(app,'runtime/variants/registrations')
def _registration_path(app,registration_id):
    require(re.fullmatch('[0-9a-f]{32}',str(registration_id)) is not None,'E_REGISTRATION_ID','invalid registration ID')
    return safe_relative(app,'runtime/variants/registrations/'+registration_id+'/registration.json')

VALIDATION_SCOPE_FIELDS=('variant_id','ownership','python','python_sha256','asset_root','dependency_lock_sha256','execution_profile_sha256','capability_receipt_sha256','environment_identity_sha256')

def _check_validation_scope(app,record,*,partial=False):
    marker=safe_relative(app,'runtime/validation-scope.json')
    _,profile=specification(app,record['variant_id'])
    if not marker.exists():
        require(profile.get('validation_scope_required') is not True,'E_VALIDATION_SCOPE','target-scoped catalog requires its validation marker')
        return
    require(marker.is_file() and marker.stat().st_size<=100000,'E_VALIDATION_SCOPE','invalid validation scope marker')
    scope=json.loads(marker.read_text(encoding='utf-8'))
    require(isinstance(scope,dict) and scope.get('schema')=='m2-runtime-validation-scope/1','E_VALIDATION_SCOPE','unsupported validation scope')
    require(scope.get('scope_sha256')==value_sha({k:v for k,v in scope.items() if k!='scope_sha256'}),'E_VALIDATION_SCOPE','validation scope changed')
    target=scope.get('target')
    require(isinstance(target,dict) and set(target)==set(VALIDATION_SCOPE_FIELDS),'E_VALIDATION_SCOPE','complete target identity required')
    if profile.get('validation_scope_required') is True:
        require(profile.get('validation_target_sha256')==value_sha({k:v for k,v in target.items() if k!='execution_profile_sha256'}),'E_VALIDATION_SCOPE','scope target differs from catalog-pinned validation target')
    if record.get('variant_id')!=target['variant_id']:return
    for key in VALIDATION_SCOPE_FIELDS:
        if partial and key not in record:continue
        require(record.get(key)==target[key],'E_VALIDATION_SCOPE','this validation copy permits only the exact registered target: '+key)

def _assess(app,record,environment=None,*,user_scr_purpose='train'):
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    _,spec=specification(app,record['variant_id'])
    from .windows_user_scr import require_full_model_qualification
    require_full_model_qualification(record['variant_id'])
    require(record['schema']=='m2-runtime-registration/1','E_REGISTRATION','unsupported registration schema')
    require(record.get('registration_id')==value_sha({k:v for k,v in record.items() if k!='registration_id'})[:32],'E_REGISTRATION','registration contents changed')
    require(record['dependency_lock_sha256']==spec['lock_sha256'] and record['execution_profile_sha256']==spec['execution_profile_sha256'],'E_REGISTRATION_PROFILE_CHANGED','catalog changed; use a new explicit registration')
    _check_validation_scope(app,record)
    python=Path(record['python'])
    require(python.is_file() and sha(python)==record['python_sha256'],'E_INTERPRETER_CHANGED','selected interpreter missing or modified')
    assets=verify_asset_root(app,record['asset_root'])
    require(assets['asset_identity_sha256']==record['asset_identity_sha256'],'E_ASSET_IDENTITY','asset identity changed')
    env=environment or fresh_environment(app,python,spec)
    proof=record.get('capability_receipt')
    if spec['full_model_probe_required']:
        try:identity=verify_full_model_proof(app,spec,proof,env,python,user_scr_purpose=user_scr_purpose)
        except RuntimeUnavailable as exc:
            exc.environment_ready=True;exc.variant_id=record['variant_id'];exc.device=spec['device']
            raise
    else:identity=env['environment_identity_sha256']
    require(identity==record['environment_identity_sha256'],'E_NUMERICAL_ENVIRONMENT_CHANGED','registration environment drifted')
    return {'status':'READY','ready':True,'environment_ready':True,'full_model_ready':True,'registration_id':record['registration_id'],'variant_id':record['variant_id'],'device':spec['device'],'python':str(python),'environment':env,'registration':record,**assets}

def inspect_registration(app,registration_id,*,user_scr_purpose='train'):
    try:
        from .windows_training_runtime import has_registration,inspect as inspect_windows
        if has_registration(app,registration_id):return inspect_windows(app,registration_id,purpose=user_scr_purpose)
        from .release_runtime_policy import assert_m2_execution_allowed
        assert_m2_execution_allowed(app)
        path=_registration_path(app,registration_id)
        require(path.is_file(),'E_RUNTIME_REGISTRATION_MISSING','runtime disabled or not installed')
        record=json.loads(path.read_text(encoding='utf-8'))
        require(isinstance(record,dict) and record.get('registration_id')==registration_id,'E_REGISTRATION_ID','registration path and embedded identity differ')
        return _assess(Path(app).resolve(),record,user_scr_purpose=user_scr_purpose)
    except (OSError,KeyError,TypeError,AttributeError,ValueError,subprocess.SubprocessError) as exc:
        return {'status':'BLOCKED','ready':False,'registration_id':registration_id,'code':getattr(exc,'code','E_RUNTIME_INVALID'),'reason':str(exc),'variant_id':getattr(exc,'variant_id',None),'device':getattr(exc,'device',None),'environment_ready':getattr(exc,'environment_ready',False),'full_model_ready':False,'personal_style_available':False,'base_app_may_continue':True}

def register_external(app,variant_id,python,asset_root,probe_receipt=None,*,_managed=False):
    """Explicit opt-in. Records a descriptor; never writes to the external venv."""
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    app=Path(app).resolve();python=Path(python).absolute();asset_root=Path(asset_root).resolve()
    _,spec=specification(app,variant_id)
    from .windows_user_scr import require_full_model_qualification
    require_full_model_qualification(variant_id)
    if _managed:
        managed=safe_relative(app,'runtime/variants/managed').resolve()
        require(python.is_relative_to(managed) and asset_root.is_relative_to(managed),'E_MANAGED_OWNERSHIP','managed registration must refer to owned install paths')
        require(python.relative_to(managed).parts[0]==asset_root.relative_to(managed).parts[0],'E_MANAGED_OWNERSHIP','managed interpreter and assets must share one installation')
        marker=managed/python.relative_to(managed).parts[0]/'installation.json'
        require(marker.is_file(),'E_MANAGED_OWNERSHIP','managed installation marker missing')
        installed=json.loads(marker.read_text(encoding='utf-8'))
        require(installed.get('ownership')=='managed' and installed.get('variant_id')==variant_id and Path(installed.get('python','')).absolute()==python and Path(installed.get('asset_root','')).resolve()==asset_root and installed.get('dependency_lock_sha256')==spec['lock_sha256'],'E_MANAGED_OWNERSHIP','managed installation marker differs')
    require(python.is_file(),'E_INTERPRETER_MISSING','explicit interpreter path not found')
    proof_path=Path(probe_receipt) if probe_receipt else None
    if proof_path:require(proof_path.is_file() and proof_path.stat().st_size<=2000000,'E_FULL_MODEL_PROBE','invalid or oversized capability receipt')
    proof=json.loads(proof_path.read_text(encoding='utf-8')) if proof_path else None
    proof_sha=sha(proof_path) if proof_path else None
    scope_candidate={'variant_id':variant_id,'ownership':'managed' if _managed else 'external_read_only','python':str(python),'python_sha256':sha(python),'asset_root':str(asset_root),'dependency_lock_sha256':spec['lock_sha256'],'execution_profile_sha256':spec['execution_profile_sha256'],'capability_receipt_sha256':proof_sha}
    _check_validation_scope(app,scope_candidate,partial=True)
    assets=verify_asset_root(app,asset_root);env=fresh_environment(app,python,spec)
    numeric=((env.get('numerical_environment') or {}).get('identity_sha256') or env['environment_identity_sha256']) if spec['full_model_probe_required'] and spec['device']=='cuda' else env['environment_identity_sha256']
    record={'schema':'m2-runtime-registration/1','ownership':'managed' if _managed else 'external_read_only','variant_id':variant_id,'python':str(python),'python_sha256':sha(python),'asset_root':str(asset_root),'asset_identity_sha256':assets['asset_identity_sha256'],'dependency_lock_sha256':spec['lock_sha256'],'execution_profile_sha256':spec['execution_profile_sha256'],'environment_identity_sha256':numeric,'capability_receipt_sha256':proof_sha,'capability_receipt':proof,'uninstall_policy':'disable_and_preserve_owned_environment' if _managed else 'deregister_only_never_modify_external_environment'}
    record['registration_id']=value_sha(record)[:32]
    path=_registration_path(app,record['registration_id']);path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():require(json.loads(path.read_text(encoding='utf-8'))==record,'E_REGISTRATION_CONFLICT','preserve existing registration')
    else:
        with path.open('x',encoding='utf-8') as f:f.write(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    try:result=_assess(app,record,environment=env)
    except (OSError,ValueError,KeyError,TypeError,AttributeError) as exc:result={'status':'REGISTERED_BLOCKED','ready':False,'environment_ready':getattr(exc,'environment_ready',False),'full_model_ready':False,'code':getattr(exc,'code','E_RUNTIME_INVALID'),'reason':str(exc)}
    return {**result,'registration_id':record['registration_id'],'variant_id':variant_id,'device':spec['device'],'ownership':record['ownership'],'external_environment_written':False,'model_promoted':False}

def unregister(app,registration_id):
    path=_registration_path(app,registration_id)
    require(path.is_file(),'E_RUNTIME_REGISTRATION_MISSING','registration already absent')
    disabled=safe_relative(app,'runtime/variants/disabled');disabled.mkdir(parents=True,exist_ok=True)
    target=disabled/(registration_id+'-'+uuid.uuid4().hex)
    path.parent.rename(target)
    return {'status':'DISABLED','registration_id':registration_id,'preserved_registration':str(target),'external_environment_written':False,'user_data_touched':False,'model_promoted':False}

def make_binding(resolved):
    require(resolved.get('ready') is True,'E_RUNTIME_BLOCKED','runtime is not full-model ready')
    r=resolved['registration'];body={'schema':SCHEMA,**{k:r[k] for k in BINDING_KEYS if k!='device'},'device':resolved['device']}
    return {**body,'binding_sha256':value_sha(body)}

def validate_binding_shape(binding):
    require(isinstance(binding,dict) and binding.get('schema')==SCHEMA,'E_RUNTIME_BINDING','missing explicit runtime binding')
    require(set(binding)==set(BINDING_KEYS)|{'schema','binding_sha256'},'E_RUNTIME_BINDING','binding fields differ')
    require(binding.get('device') in ('cpu','cuda') and isinstance(binding.get('variant_id'),str) and bool(binding['variant_id']),'E_RUNTIME_BINDING','invalid bound variant/device')
    require(re.fullmatch('[0-9a-f]{32}',str(binding.get('registration_id'))) is not None,'E_RUNTIME_BINDING','invalid bound registration')
    for key in ('asset_identity_sha256','dependency_lock_sha256','execution_profile_sha256','environment_identity_sha256','binding_sha256'):
        require(re.fullmatch('[0-9a-f]{64}',str(binding.get(key))) is not None,'E_RUNTIME_BINDING','invalid '+key)
    capability=binding.get('capability_receipt_sha256')
    require((capability is None and binding['device']=='cpu') or re.fullmatch('[0-9a-f]{64}',str(capability)) is not None,'E_RUNTIME_BINDING','invalid capability receipt identity')
    if binding['variant_id'] in (*GEOMETRY_CPU_VARIANTS,USER_SCR_CPU):
        require(binding['device']=='cpu' and re.fullmatch('[0-9a-f]{64}',str(capability)) is not None,'E_OFFSET_RUNTIME_BINDING_REQUIRED','offset CPU binding requires a full capability receipt')
    require(binding['binding_sha256']==value_sha({k:v for k,v in binding.items() if k!='binding_sha256'}),'E_RUNTIME_BINDING','binding hash mismatch')
    return binding

def resolve_binding(app,binding,*,user_scr_purpose='train'):
    from .windows_user_scr import VARIANTS as WINDOWS_VARIANTS
    if isinstance(binding,dict) and binding.get('variant_id') in WINDOWS_VARIANTS:
        from .windows_training_runtime import resolve
        return resolve(app,binding,purpose=user_scr_purpose)
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    require(isinstance(binding,dict) and binding.get('schema')==SCHEMA,'E_RUNTIME_BINDING','missing explicit runtime binding')
    require(set(binding)==set(BINDING_KEYS)|{'schema','binding_sha256'},'E_RUNTIME_BINDING','binding fields differ')
    require(binding['binding_sha256']==value_sha({k:v for k,v in binding.items() if k!='binding_sha256'}),'E_RUNTIME_BINDING','binding hash mismatch')
    require(user_scr_purpose in ('prepare','train','inference'),'E_RUNTIME_BINDING','unsupported runtime purpose')
    result=(inspect_registration(app,binding['registration_id'],user_scr_purpose=user_scr_purpose)
            if user_scr_purpose=='inference' else inspect_registration(app,binding['registration_id']))
    require(result.get('ready') is True,result.get('code','E_RUNTIME_BLOCKED'),result.get('reason','selected runtime not ready'))
    require(make_binding(result)==binding,'E_RUNTIME_BINDING_CHANGED','run binding differs from selected immutable registration')
    return result

def binding_for_config(app,config,*,user_scr_purpose='train'):
    from .windows_training_recipe import RECIPE_ID as WINDOWS_SCR_RECIPE_ID
    if isinstance(config,dict) and config.get('recipe_id')==WINDOWS_SCR_RECIPE_ID:
        from .windows_training_recipe import resolve_registered_runtime
        return resolve_registered_runtime(app,config,purpose=user_scr_purpose)
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    from .user_scr_contract import RECIPE_ID as USER_SCR_RECIPE_ID
    if config.get('recipe_id') == USER_SCR_RECIPE_ID:
        from .user_scr_runtime import resolve_user_scr_runtime
        from .spatial_topology import validate_spatial_topology_fields
        from .user_scr_contract import user_scr_identity
        validate_spatial_topology_fields(config)
        user_scr_identity(config)
        return resolve_user_scr_runtime(app, config, purpose=user_scr_purpose)
    from .topology_dispatch import validate_topology_fields
    from .adapter_topology import OFFSET_TOPOLOGY_ID
    from .spatial_topology import SPATIAL_TOPOLOGY_ID
    recipes={OFFSET_RECIPE_ID:(OFFSET_TOPOLOGY_ID,OFFSET_CPU),SPATIAL_RECIPE_ID:(SPATIAL_TOPOLOGY_ID,SPATIAL_CPU),SCR_RECIPE_ID:(SPATIAL_TOPOLOGY_ID,SCR_CPU)}
    topology=validate_topology_fields(config,required=config.get('recipe_id') in recipes)
    selected=recipes.get(config.get('recipe_id'))
    require((selected is not None)==(topology['id'] in (OFFSET_TOPOLOGY_ID,SPATIAL_TOPOLOGY_ID)) and (selected is None or selected[0]==topology['id']),'E_ADAPTER_TOPOLOGY','recipe and topology differ')
    binding=config.get('runtime_binding')
    if config.get('recipe_id')==SCR_RECIPE_ID:
        _verify_scr_provenance(config)
        require(config.get('snapshot_sha256')==config['scr_assets']['snapshot_sha256'],'E_SCR_PROVENANCE','SCR training requires its exact cached-reference snapshot')
    if selected is not None:
        require(isinstance(binding,dict) and binding.get('variant_id')==selected[1] and binding.get('device')=='cpu','E_OFFSET_RUNTIME_BINDING_REQUIRED','geometry execution requires its explicit CPU experiment registration')
    elif isinstance(binding,dict):
        require(binding.get('variant_id') not in (*GEOMETRY_CPU_VARIANTS,USER_SCR_CPU),'E_ADAPTER_TOPOLOGY','geometry registration requires its explicit recipe/topology')
    if binding is None:
        require((config.get('train') or {}).get('device','cpu')=='cpu','E_GPU_BINDING_REQUIRED','GPU execution requires an explicit verified runtime binding')
        return None
    resolved=resolve_binding(app,binding)
    require((config.get('train') or {}).get('device')==resolved['device'],'E_RUNTIME_DEVICE','requested device differs from bound runtime')
    require(all(isinstance(config.get(k),str) and bool(config[k].strip()) for k in ('runtime_manifest_path','fd_repo','content_font_path','runtime_identity_sha256')) and isinstance(config.get('base_npz_sha256'),dict) and bool(config['base_npz_sha256']),'E_RUNTIME_ASSET','bound runs must carry explicit registered manifest, repo, font, asset identity and base hashes; legacy defaults are forbidden')
    require(config.get('runtime_identity_sha256') in (None,resolved['asset_identity_sha256']),'E_RUNTIME_ASSET','run asset identity differs')
    if config.get('runtime_manifest_path'):
        require(Path(config['runtime_manifest_path']).resolve()==Path(resolved['manifest_path']),'E_RUNTIME_ASSET','run manifest path differs')
    for key,expected in [('fd_repo',resolved['fd_repo']),('content_font_path',str(Path(resolved['fd_repo'])/'ttf/KaiXinSongA.ttf'))]:
        if config.get(key):require(Path(config[key]).resolve()==Path(expected).resolve(),'E_RUNTIME_ASSET','run '+key+' differs')
    if config.get('base_npz_sha256'):require(config['base_npz_sha256']==resolved['manifest']['base_npz_sha256'],'E_RUNTIME_ASSET','run base hashes differ')
    return resolved

def bound_subprocess_env(binding=None,*,base_env=None):
    env=os.environ.copy() if base_env is None else dict(base_env)
    if binding is not None:
        for k in ('YY_STYLE_M2_PYTHON','YY_M2_RUNTIME_MANIFEST','YY_M2_DEVICE','YY_RENDER_M2_DEVICE','YY_RENDER_M2_BASE_REPO','YY_RENDER_M2_CONTENT_TTF','YY_RENDER_M2_SNAPSHOT_DIR','PYTHONPATH','PYTHONHOME','PYTHONUSERBASE'):env.pop(k,None)
        env.update(CUBLAS_WORKSPACE_CONFIG=':4096:8',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',PYTHONDONTWRITEBYTECODE='1',PYTHONNOUSERSITE='1')
    return env

def discover_installed(app):
    try:root=_registry(app)
    except (OSError,ValueError) as exc:return [{'status':'BLOCKED','ready':False,'code':'E_RUNTIME_REGISTRY','reason':str(exc),'environment_ready':False,'full_model_ready':False}]
    from .windows_training_runtime import _root as windows_root
    windows=windows_root(app)/'registrations'
    if not root.is_dir() and not windows.is_dir():return []
    reports=[]
    for p in sorted(root.glob('*/registration.json'))[:32]:
        report=inspect_registration(app,p.parent.name)
        reports.append({k:v for k,v in report.items() if k in ('registration_id','variant_id','device','status','ready','environment_ready','full_model_ready','code','reason')})
    for p in sorted(windows.glob('*/registration.json'))[:16]:
        try:report=inspect_registration(app,p.parent.name)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            report={'registration_id':p.parent.name,'status':'BLOCKED','ready':False,'code':getattr(exc,'code','E_WINDOWS_SCR_REGISTRATION'),'reason':str(exc)}
        reports.append({k:v for k,v in report.items() if k in ('registration_id','variant_id','device','status','ready','environment_ready','full_model_ready','code','reason')})
    return reports

def verify_execution_receipt(binding,receipt,kind='training'):
    """Do not retrofit a new binding onto unmeasured GPU engine output."""
    validate_binding_shape(binding)
    from .windows_user_scr import VARIANTS as WINDOWS_VARIANTS
    if binding['variant_id'] in WINDOWS_VARIANTS:
        require(kind in ('training','inference'),'E_WINDOWS_SCR_PURPOSE','Windows CPU purpose differs')
        from .windows_training_recipe import RECIPE_ID as WINDOWS_RECIPE,plan_training_recipe
        from .user_scr_contract import user_scr_identity
        from .user_scr_runtime import verify_user_scr_training_evidence
        require(receipt.get('runtime_binding')==binding and receipt.get('recipe_id')==WINDOWS_RECIPE and receipt.get('ok') is True,'E_WINDOWS_SCR_RECEIPT','actual successful Windows-bound training receipt required')
        actual=receipt.get('training_environment' if kind=='training' else 'inference_environment') or {}
        require(actual.get('identity_sha256')==binding['environment_identity_sha256'] and actual['identity_sha256']==value_sha({k:v for k,v in actual.items() if k!='identity_sha256'}),'E_WINDOWS_SCR_ENVIRONMENT','actual Windows training measurement differs')
        require(receipt.get('runtime_identity_sha256')==binding['asset_identity_sha256'] and (receipt.get('process' if kind=='training' else 'inputs') or {}).get('device')==binding['device'],'E_WINDOWS_SCR_RECEIPT','actual base/device differs')
        identity=user_scr_identity(receipt);expected=plan_training_recipe(identity['scr_data_binding']['counts']['train'],{'train':{'device':binding['device'],'max_seconds':receipt.get('training_budget',{}).get('max_seconds')}})
        require(receipt.get('training_recipe')==expected['training_recipe'] and receipt.get('training_budget')==expected['training_budget'],'E_WINDOWS_SCR_RECIPE','actual Windows recipe differs')
        if kind=='training':verify_user_scr_training_evidence(receipt)
        else:
            from .topology_dispatch import validate_inference_topology_execution
            validate_inference_topology_execution(receipt)
            require(receipt.get('inference_scr_feature_forwards')==0,'E_WINDOWS_SCR_RECEIPT','inference cannot run SCR feature encoder')
        if binding['device']=='cuda':
            from .windows_gpu_support import GPU_LIMIT
            counts=receipt.get('operator_execution_counts') or {}
            modules={f'unet.up_blocks.{b}.dcn_deforms.{i}' for b in (1,2) for i in range(3)}
            require(set(counts)==modules and all(type(r.get('forward_calls')) is int and r['forward_calls']>0
                and type(r.get('cross_device_calls')) is int and r['cross_device_calls']==r['forward_calls'] for r in counts.values()),
                'E_WINDOWS_SCR_GPU_EVIDENCE','actual receipt must retain every differentiable CPU deform bridge')
            memory=(receipt.get('process') or {}) if kind=='training' else (receipt.get('inference_memory') or {})
            allocated=memory.get('cuda_peak_allocated' if kind=='training' else 'peak_allocated')
            reserved=memory.get('cuda_peak_reserved' if kind=='training' else 'peak_reserved')
            require(type(allocated) is int and type(reserved) is int and 0<allocated<=reserved<=GPU_LIMIT,
                'E_WINDOWS_SCR_GPU_BUDGET','actual training/inference GPU allocator receipt must stay within 3 GiB')
        return
    require(receipt.get('runtime_binding')==binding,'E_RUNTIME_BINDING_RECEIPT','actual engine receipt belongs to a different or absent binding')
    actual_environment=receipt.get('runtime_environment') or {}
    require(isinstance(actual_environment,dict) and actual_environment.get('identity_sha256')==binding['environment_identity_sha256'] and actual_environment.get('identity_sha256')==value_sha({k:v for k,v in actual_environment.items() if k!='identity_sha256'}),'E_RUNTIME_NUMERICAL_RECEIPT','actual model process numerical measurement is missing or changed')
    require(receipt.get('ok') is True,'E_RUNTIME_RECEIPT','engine did not succeed')
    require(receipt.get('runtime_identity_sha256')==binding['asset_identity_sha256'],'E_RUNTIME_RECEIPT','receipt base identity differs')
    observed_device=(receipt.get('process') or {}).get('device') if kind=='training' else (receipt.get('inputs') or {}).get('device')
    require(observed_device==binding['device'],'E_RUNTIME_DEVICE','engine used a different device')
    from .user_scr_contract import RECIPE_ID as USER_SCR_RECIPE_ID, user_scr_identity
    if binding['variant_id']==USER_SCR_CPU:
        require(receipt.get('recipe_id')==USER_SCR_RECIPE_ID,'E_ADAPTER_TOPOLOGY','generic CPU registration requires the generic SCR recipe')
    if receipt.get('recipe_id') == USER_SCR_RECIPE_ID:
        from .user_scr_recipe import validate_user_scr_binding, plan_user_scr_recipe
        from .user_scr_runtime import verify_user_scr_training_evidence
        from .spatial_topology import validate_spatial_topology_fields
        from .topology_dispatch import validate_inference_topology_execution
        validate_user_scr_binding(binding)
        identity=user_scr_identity(receipt);validate_spatial_topology_fields(receipt)
        require(receipt.get('dataset_snapshot_sha256')==identity['scr_data_binding']['snapshot_sha256'],'E_SCR_PROVENANCE','generic SCR receipt snapshot differs')
        expected=plan_user_scr_recipe(identity['scr_data_binding']['counts']['train'],{'train':{'max_seconds':receipt.get('training_budget',{}).get('max_seconds'),'device':binding['device']}})
        require(_strict_equal(receipt.get('training_recipe'),expected['training_recipe']) and _strict_equal(receipt.get('training_budget'),expected['training_budget']),'E_PROBE_RECIPE','generic SCR receipt budget/recipe differs')
        if kind=='training':verify_user_scr_training_evidence(receipt)
        else:validate_inference_topology_execution(receipt)
    if binding['variant_id'] in GEOMETRY_CPU_VARIANTS:
        from .topology_dispatch import validate_inference_topology_execution
        profile=_cpu_geometry_profile(binding['variant_id'])
        require(receipt.get('recipe_id')==profile['recipe_id'] and receipt.get('objective_profile')==profile['recipe']['objective_profile'],'E_ADAPTER_TOPOLOGY','geometry runtime receipt requires its explicit recipe')
        require(all(_strict_equal(receipt.get(key),value) for key,value in profile['topology'].items()),'E_ADAPTER_TOPOLOGY','geometry runtime receipt topology differs')
        require(_strict_equal(receipt.get('training_recipe'),profile['recipe']),'E_PROBE_RECIPE','geometry runtime receipt recipe contract differs')
        if binding['variant_id']==SCR_CPU:
            _verify_scr_provenance(receipt)
            require(receipt.get('dataset_snapshot_sha256')==receipt['scr_assets']['snapshot_sha256'],'E_SCR_PROVENANCE','SCR receipt requires its exact cached-reference snapshot')
            if kind=='training':_verify_scr_training_evidence(receipt)
        if binding['variant_id'] in (SPATIAL_CPU,SCR_CPU):
            require(_strict_equal(receipt.get('n_spatial_offset_heads'),6) and _strict_equal(receipt.get('n_offset_projection_heads'),0),'E_ADAPTER_EXECUTION','spatial runtime receipt requires six spatial heads and zero projection heads')
        if kind!='training':
            try:validate_inference_topology_execution(receipt)
            except ValueError as exc:raise RuntimeUnavailable('E_ADAPTER_EXECUTION',str(exc)) from exc
    if binding['device']=='cuda':
        observed=receipt.get('training_environment' if kind=='training' else 'inference_environment') or {}
        require(observed.get('identity_sha256')==binding['environment_identity_sha256'] and observed.get('device')=='cuda','E_RUNTIME_NUMERICAL_RECEIPT','GPU numerical environment absent or different')
        require(observed.get('identity_sha256')==value_sha({k:v for k,v in observed.items() if k!='identity_sha256'}),'E_RUNTIME_NUMERICAL_RECEIPT','GPU numerical environment hash malformed')
        if kind=='training':require((receipt.get('runtime_evidence') or {}).get('cuda_used') is True,'E_RUNTIME_DEVICE','no actual CUDA training evidence')
    return True

def validate_manifest_binding(config,manifest):
    wanted=config.get('runtime_binding');actual=manifest.get('runtime_binding')
    require(wanted==actual,'E_RUNTIME_BINDING_CHANGED','run config and artifact runtime binding differ')
    if wanted is not None:validate_binding_shape(wanted)
    return wanted


def restore_registration(app,disabled_folder):
    root=safe_relative(app,'runtime/variants/disabled').resolve();original=Path(disabled_folder).absolute();folder=original.resolve()
    require(original.parent.resolve()==root and folder.parent==root and not original.is_symlink(),'E_RUNTIME_PATH','restore requires a preserved direct registration directory')
    record=json.loads((folder/'registration.json').read_text(encoding='utf-8'))
    result=_assess(Path(app).resolve(),record)
    dest=_registration_path(app,record['registration_id']).parent
    require(not dest.exists(),'E_REGISTRATION_CONFLICT','active registration already exists')
    dest.parent.mkdir(parents=True,exist_ok=True);folder.rename(dest)
    return {**result,'action':'RESTORED','external_environment_written':False,'model_promoted':False}

def basic_environment_body(versions,device):
    import platform
    import torch
    import pygame.freetype
    return {'schema':'m2-basic-numerical-environment/1','versions':versions,'python':platform.python_version(),'platform':platform.platform(),'device':device,'cuda_runtime':torch.version.cuda,'gpu_name':torch.cuda.get_device_name(0) if device=='cuda' else None,'freetype_version':list(pygame.freetype.get_version()),'torch_cpu_threads':torch.get_num_threads(),'torch_interop_threads':torch.get_num_interop_threads(),'deterministic_algorithms':torch.are_deterministic_algorithms_enabled(),'dtype':str(torch.get_default_dtype())}

def measure_in_process(app,binding):
    """Measure in the actual model process; never borrow parent/probe identity."""
    from .windows_user_scr import VARIANTS as WINDOWS_VARIANTS
    if binding['variant_id'] in WINDOWS_VARIANTS:
        from .windows_training_runtime import measure
        observed=measure(app,variant=binding['variant_id'])['numerical_environment']
        require(observed['identity_sha256']==binding['environment_identity_sha256'],'E_NUMERICAL_ENVIRONMENT_CHANGED','actual Windows CPU measurement differs')
        return observed
    import importlib.metadata
    _,spec=specification(app,binding['variant_id'])
    wanted=versions_from_lock(Path(app)/spec['lock']);actual={k:importlib.metadata.version(k) for k in wanted}
    require(actual==wanted,'E_DEPENDENCY_VERSIONS','actual model process dependencies differ')
    if binding['device']=='cuda':
        from .environment import execution_environment
        observed=execution_environment('cuda')
    else:
        body=basic_environment_body(actual,'cpu');observed={**body,'identity_sha256':value_sha(body)}
    require(observed.get('identity_sha256')==binding['environment_identity_sha256'],'E_NUMERICAL_ENVIRONMENT_CHANGED','actual model process numerical environment differs')
    return observed
