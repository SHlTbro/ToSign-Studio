"""Server-owned Windows CPU SCR qualification and independent training registry.

Only this monitored launch signs a registration. Caller PASS files, inference
keys and Linux capabilities are never imported. Registration grants prepare and
fresh training and inference only; resume/quality/publication remain separate gates.
"""
from pathlib import Path
import hashlib,hmac,json,os,re,subprocess,sys,time
from . import personal_runtime_policy as Personal
from .runtime_variants import RuntimeUnavailable,require,sha,value_sha,make_binding
from .windows_training_recipe import RECIPE_ID,contract
from .windows_user_scr import CPU,GPU,profile_for_source,cache_identity
from .windows_gpu_support import device_for_variant,acquire_gpu_lease,release_gpu_lease,gpu_lock_path
from .windows_training_capability import SCHEMA,PRODUCER,VERIFIER,verify_complete_candidate
from .trainer_resume_protocol import native_path

def acquire_cpu_lease(app,task):
    """Exclusive owned CPU execution lease; never consumes the GPU lock."""
    from .personal_runtime_resources import preflight as resource_preflight
    resource_preflight(app)
    p=_root(app)/'cpu-execution.lock';p.parent.mkdir(parents=True,exist_ok=True)
    from .personal_runtime_resources import process_start
    token={'pid':os.getpid(),'process_start':process_start(),'nonce':os.urandom(16).hex(),'task':task}
    try:
        with p.open('x',encoding='utf-8') as f:json.dump(token,f)
    except FileExistsError as exc:raise RuntimeUnavailable('E_WINDOWS_SCR_BUSY','CPU execution lease already exists; no existing process was changed') from exc
    return p,token

def release_cpu_lease(lease):
    p,token=lease
    require(_read(p)==token,'E_WINDOWS_SCR_LEASE','CPU lease ownership changed')
    p.unlink()

def _root(app):return Path(app).resolve()/'runtime/windows-scr-training'
def _regular(p,directory=False):
    p=Path(p).absolute()
    require(p.drive.upper()=='E:' and not any(native_path(x).is_symlink() or native_path(x).is_junction() for x in (p,*p.parents)),'E_WINDOWS_SCR_PATH','E-drive regular path required')
    require(native_path(p).is_dir() if directory else native_path(p).is_file(),'E_WINDOWS_SCR_INPUT_MISSING',str(p))
    require(p.resolve().drive.upper()=='E:','E_WINDOWS_SCR_PATH','resolved input must stay on E');return p.resolve()
def _read(p):
    p=_regular(p);require(native_path(p).stat().st_size<=2*2**20,'E_WINDOWS_SCR_SIZE','record oversized');return json.loads(native_path(p).read_text(encoding='utf-8'))
def _write(p,v):
    p=Path(p);native_path(p.parent).mkdir(parents=True,exist_ok=True)
    if native_path(p).exists():require(_read(p)==v,'E_WINDOWS_SCR_IMMUTABLE','record changed');return
    with native_path(p).open('x',encoding='utf-8') as f:json.dump(v,f,indent=2,ensure_ascii=False,allow_nan=False)
def _key(app,create=False):
    require(not create,'E_WINDOWS_SCR_KEY_ENROLL','execution cannot enroll issuer credentials')
    from .windows_qualification_key import read_key,current_user_sid
    import socket
    p=_root(app)/'private/qualification-key'
    try:raw=read_key(p)
    except (OSError,ValueError) as exc:
        raise RuntimeUnavailable('E_WINDOWS_SCR_KEY_ACCESS','protected candidate-local issuer required: '+str(exc)) from exc
    receipt=_read(p.parent/'enrollment.json')
    body=receipt.get('body',{})
    policy=_read(Path(app)/'config/personal-style-runtime-policy.json')
    identity=native_path(p).stat()
    expected={'schema':'tosign-local-qualification-enrollment/1','app':str(Path(app).resolve()),
        'catalog_sha256':sha(Path(app)/'config/m2-cpu-deployment-source.json'),
        'policy_sha256':sha(Path(app)/'config/personal-style-runtime-policy.json'),
        'execution_profile_sha256':policy['execution_profile_sha256'],
        'machine':socket.gethostname(),'owner_sid':current_user_sid(),
        'file_device':identity.st_dev,'file_id':identity.st_ino}
    require(set(body)==set(expected)|{'created_at','nonce'}
        and all(body[k]==v for k,v in expected.items())
        and type(body['created_at']) is int and 0<body['created_at']<=time.time()+60
        and isinstance(body['nonce'],str) and re.fullmatch('[0-9a-f]{32}',body['nonce']) is not None,
        'E_WINDOWS_SCR_KEY_ENROLL','issuer enrollment does not match this source/device/file')
    mac=hmac.new(raw,json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode(),hashlib.sha256).hexdigest()
    require(set(receipt)=={'body','hmac_sha256'} and hmac.compare_digest(str(receipt.get('hmac_sha256')),mac),
        'E_WINDOWS_SCR_KEY_ENROLL','issuer enrollment authentication failed')
    return raw
def _mac(app,v):return hmac.new(_key(app),json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode(),hashlib.sha256).hexdigest()
def setup_cpu():
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(Path(__file__).resolve().parents[3])
    import torch
    torch.set_num_threads(2);torch.set_num_interop_threads(1);torch.use_deterministic_algorithms(True)
    torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
    torch.backends.cudnn.benchmark=False;torch.backends.cudnn.deterministic=True
def measure(app,python=None,variant=CPU):
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    from .windows_scr_execution import measure as observed_cpu
    if variant==CPU:
        v=observed_cpu(app,python);return {**v,'schema':'windows-scr-training-environment/1'}
    device=device_for_variant(variant)
    import importlib.metadata
    from .runtime_variants import versions_from_lock
    from .windows_user_scr import LOCK
    from .environment import execution_environment
    require(sys.platform=='win32' and (python is None or Path(sys.executable).resolve()==Path(python).resolve()),'E_WINDOWS_SCR_ENVIRONMENT','actual Windows selected interpreter required')
    wanted=versions_from_lock(Path(app)/LOCK);versions={k:importlib.metadata.version(k) for k in wanted}
    require(versions==wanted,'E_WINDOWS_SCR_ENVIRONMENT','complete pinned dependencies required')
    numeric=execution_environment(device)
    observed={'schema':'windows-scr-training-environment/1','versions':versions,'numerical_environment':numeric,'identity_sha256':numeric['identity_sha256']}
    from .windows_training_capability import _environment
    _environment(app,{'environment':observed},observed,variant)
    return observed
def _env(folder,variant=CPU):
    from .trainer_resume_protocol import native_path
    device=device_for_variant(variant)
    folder=Path(folder).absolute()
    require(folder.drive.upper()=='E:' and not any(native_path(p).is_symlink() or native_path(p).is_junction() for p in (folder,*folder.parents)),'E_WINDOWS_SCR_PATH','E-only private environment required')
    env=dict(os.environ,CUDA_VISIBLE_DEVICES='0' if device=='cuda' else '',PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1',PYTHONIOENCODING='utf-8',
        HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',CUBLAS_WORKSPACE_CONFIG=':4096:8',OMP_NUM_THREADS='2',MKL_NUM_THREADS='2')
    for k in ('TEMP','TMP','TMPDIR','HOME','USERPROFILE','APPDATA','LOCALAPPDATA','XDG_CACHE_HOME','TORCH_HOME','HF_HOME','CUDA_CACHE_PATH','TORCHINDUCTOR_CACHE_DIR'):
        p=folder/k.lower();native_path(p).mkdir(parents=True,exist_ok=True)
        # Keep authority paths ordinary; long child I/O needs Windows native syntax.
        # Inductor appends Dynamo/hash descendants even when its root is short.
        env[k]=str(native_path(p) if os.name=='nt' and (k=='TORCHINDUCTOR_CACHE_DIR' or len(str(p))>=248) else p)
    for k in ('PYTHONPATH','PYTHONHOME','PYTHONUSERBASE'):env.pop(k,None)
    return env
def observe(app,python,env,variant=CPU):
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    policy=Personal.verify(app);issued=int(time.time())
    request={'schema':'windows-scr-environment-launch/1','app':str(Path(app).resolve()),
        'python':str(Path(python).resolve()),'python_sha256':sha(python),
        'personal_policy_sha256':policy['sha256'],'issued_at':issued,'expires_at':issued+60,
        'variant_id':variant,'device':device_for_variant(variant),'profile_sha256':profile_for_source(app,variant)['execution_profile_sha256'],'nonce':os.urandom(32).hex()}
    request['hmac_sha256']=_mac(app,request)
    path=_root(app)/'observations/requests'/f"{request['nonce']}.json";_write(path,request)
    child=subprocess.run([str(python),'-I','-B','-X','utf8',str(Path(app)/PRODUCER),'--environment',str(path)],
        cwd=app,env=env,capture_output=True,text=True,encoding='utf-8',timeout=45)
    require(child.returncode==0,'E_WINDOWS_SCR_ENVIRONMENT',child.stderr[-1000:])
    value=json.loads(child.stdout.splitlines()[-1]);require(value.get('schema')=='windows-scr-training-environment/1','E_WINDOWS_SCR_ENVIRONMENT','wrong observer');return value

def validate_environment_launch(request):
    app=Path(__file__).resolve().parents[3];policy=Personal.bootstrap(app)
    require(isinstance(request,dict) and request.get('schema')=='windows-scr-environment-launch/1'
        and Path(request.get('app','')).resolve()==app,'E_WINDOWS_SCR_AUTH','owned environment launch required')
    body={k:v for k,v in request.items() if k!='hmac_sha256'}
    require(hmac.compare_digest(str(request.get('hmac_sha256')),_mac(app,body)),'E_WINDOWS_SCR_AUTH','untrusted environment request')
    Personal.assert_launch_fresh(request,policy)
    variant=request.get('variant_id');device=device_for_variant(variant)
    require(policy['body']['variant_id']==variant and request.get('device')==device,'E_WINDOWS_SCR_ENVIRONMENT','observer must match signed device policy')
    require(request['profile_sha256']==profile_for_source(app,variant)['execution_profile_sha256']
        and Path(sys.executable).resolve()==Path(request['python']).resolve()
        and sha(sys.executable)==request['python_sha256'],'E_WINDOWS_SCR_ENVIRONMENT','observer source/interpreter changed')
    return app,policy

def preflight(app,python,asset_root,snapshot,snapshot_sha,assets,*,variant=CPU):
    """Report every local dependency before any Torch/model work."""
    device_for_variant(variant)
    errors=[];verified=None;base=None
    from .user_scr_contract import asset_contract
    from .user_scr_cache import inspect_user_snapshot
    from engine.style.dataset.schema import DatasetError
    from .runtime_variants import verify_asset_root
    wanted=asset_contract();keys={'module','checkpoint','state_schema','font_sans','font_serif','source_ledger'}
    try:_regular(app,True);_regular(python)
    except (OSError,ValueError) as exc:errors.append({'key':'interpreter/source','detail':str(exc)})
    if not isinstance(assets,dict) or set(assets)!=keys:errors.append({'key':'asset_configuration','detail':'exact six consumer-local SCR assets required'})
    else:
        for k in sorted(keys):
            expected=(wanted['font_sha256'][('font_sans','font_serif').index(k)] if k.startswith('font_') else wanted.get(k+'_sha256'))
            try:
                p=_regular(assets[k])
                if expected is not None:require(sha(p)==expected,'E_WINDOWS_SCR_ASSET_HASH','pinned bytes differ: '+k)
                if k=='checkpoint':require(p.stat().st_size==wanted['checkpoint_size_bytes'],'E_WINDOWS_SCR_ASSET_SIZE','SCR checkpoint size')
            except (OSError,ValueError) as exc:errors.append({'key':k,'expected_sha256':expected,'detail':str(exc)})
        try:verified=inspect_user_snapshot(snapshot,snapshot_sha,ledger_path=assets['source_ledger'])
        except (OSError,ValueError,KeyError,TypeError,DatasetError) as exc:errors.append({'key':'S03/ledger','detail':str(exc)})
    try:base=verify_asset_root(app,_regular(asset_root,True))
    except (OSError,ValueError,KeyError,TypeError) as exc:errors.append({'key':'full_base/vendor/content_font','detail':str(exc)})
    try:
        from .windows_user_scr import validate_profile
        from .runtime_variants import specification
        if Personal.present(app):
            policy=Personal.verify(app)
            require(policy['body']['variant_id']==variant,'E_PERSONAL_RUNTIME_DEVICE','preflight device must match signed policy')
            validate_profile(app,variant,profile_for_source(app,variant))
        else:
            _,s=specification(app,variant);validate_profile(app,variant,s)
        _regular(Path(app)/PRODUCER);_regular(Path(app)/VERIFIER)
    except (OSError,ValueError,KeyError,TypeError) as exc:errors.append({'key':'final_source/profile/producer','detail':str(exc)})
    input_binding=None
    if not errors:
        body={'schema':'windows-scr-qualification-inputs/1','snapshot_binding':verified['snapshot_binding'],
            'scr_data_binding_sha256':value_sha(verified['data_binding']),
            'asset_sha256':{k:sha(assets[k]) for k in sorted(keys)},'python_sha256':sha(python),
            'runtime_manifest_sha256':sha(base['manifest_path']),'asset_identity_sha256':base['asset_identity_sha256'],
            'execution_profile_sha256':profile_for_source(app,variant)['execution_profile_sha256']}
        input_binding={**body,'sha256':value_sha(body)}
    return {'status':'INPUTS_PRESENT_NOT_QUALIFIED' if not errors else 'BLOCKED_ENV','errors':errors,'verified':verified,'base':base,'input_binding':input_binding,'models_loaded':False,'training_started':False}
def _require_owned_launcher(request):
    """A persisted MAC request cannot start an unmonitored replay after a crash."""
    try:import psutil
    except ImportError as exc:raise RuntimeUnavailable('BLOCKED_ENV','existing ML interpreter requires psutil for owned launcher identity') from exc
    from .personal_runtime_resources import process_start
    issuer=request.get('issuer_pid');started=request.get('issuer_process_start')
    require(type(issuer) is int and issuer>0 and type(started) is int and started>0,
        'E_WINDOWS_SCR_AUTH','complete issuer identity required')
    try:
        parents=psutil.Process().parents()
        require(any(parent.pid==issuer for parent in parents)
            and process_start(issuer)==started,'E_WINDOWS_SCR_AUTH','worker must descend from this live authenticated launcher')
    except (psutil.Error,OSError) as exc:
        raise RuntimeUnavailable('BLOCKED_ENV','own launcher ancestry cannot be verified') from exc

def validate_launch(request):
    policy=Personal.bootstrap(Path(__file__).resolve().parents[3])
    require(isinstance(request,dict) and request.get('schema')=='windows-scr-server-launch/1','E_WINDOWS_SCR_AUTH','signed server request required')
    app=_regular(request['app'],True);require(app==Path(__file__).resolve().parents[3],'E_WINDOWS_SCR_AUTH','launch belongs to another source')
    body={k:v for k,v in request.items() if k!='hmac_sha256'}
    require(hmac.compare_digest(str(request.get('hmac_sha256')),_mac(app,body)),'E_WINDOWS_SCR_AUTH','untrusted launch request')
    Personal.assert_launch_fresh(request,policy)
    require(isinstance(request.get('nonce'),str) and re.fullmatch('[0-9a-f]{64}',request['nonce']),
        'E_WINDOWS_SCR_AUTH','server nonce required')
    lease=_read(_root(app)/'cpu-execution.lock')
    require(lease.get('pid')==request.get('issuer_pid') and lease.get('nonce')==request.get('lease_nonce')
        and lease.get('process_start')==request.get('issuer_process_start')
        and lease.get('task')=='two-update-qualification','E_WINDOWS_SCR_BUSY','owned qualification CPU lease required')
    _require_owned_launcher(request)
    variant=request['profile']['variant_id']
    require(variant==policy['body']['variant_id'] and request['profile']==profile_for_source(app,variant),'E_WINDOWS_SCR_SOURCE','source/device/profile changed')
    if variant==GPU:
        gpu_lease=_read(gpu_lock_path(app))
        require(gpu_lease.get('pid')==request['issuer_pid'] and gpu_lease.get('process_start')==request['issuer_process_start']
            and gpu_lease.get('nonce')==request.get('gpu_lease_nonce') and gpu_lease.get('task')=='two-update-qualification',
            'E_WINDOWS_SCR_BUSY','authenticated owned GPU lease required')
    require(Path(sys.executable).resolve()==Path(request['python']).resolve(),'E_WINDOWS_SCR_INTERPRETER','wrong actual interpreter')
    result=preflight(app,request['python'],request['base_asset_root'],request['snapshot'],request['snapshot_sha256'],request['assets'],variant=variant)
    require(not result['errors'],'E_WINDOWS_SCR_INPUTS',json.dumps(result['errors']))
    require(result['input_binding']==request.get('input_binding'),'E_WINDOWS_SCR_INPUTS','SCR/ledger/S03/base/interpreter input binding changed')
    out=Path(request['out']).absolute();snapshot=Path(request['snapshot']).resolve()
    require(out.drive.upper()=='E:' and not out.resolve().is_relative_to(snapshot)
        and not any((p/'FROZEN').exists() for p in (out,*out.parents)),'E_WINDOWS_SCR_OUTPUT','new E output outside frozen data required')
    return app,result['verified'],result['base']
def qualify(app,python,asset_root,snapshot,snapshot_sha,assets,out,*,variant=CPU):
    """Actual monitored two-update qualification. No public API accepts a proof."""
    policy=Personal.bootstrap(app)
    require(variant==policy['body']['variant_id'],'E_PERSONAL_RUNTIME_DEVICE','qualification requires matching signed device policy')
    app=_regular(app,True);python=_regular(python);out=Path(out).absolute()
    require(out.drive.upper()=='E:' and not out.exists(),'E_WINDOWS_SCR_OUTPUT','new E qualification output required')
    checked=preflight(app,python,asset_root,snapshot,snapshot_sha,assets,variant=variant)
    require(not checked['errors'],'BLOCKED_ENV',json.dumps(checked['errors']))
    # Reuse an already enrolled server key. Never mint credentials in this flow.
    try:_key(app)
    except (OSError,ValueError) as exc:raise RuntimeUnavailable('BLOCKED_ENV_AUTHORITY','existing Windows server qualification authority unavailable') from exc
    lease=acquire_cpu_lease(app,'two-update-qualification')
    checked['lease']=lease[1];checked['policy']=policy;checked['variant']=variant
    checked['execution_owned_active']=False
    gpu_lease=None
    try:
        if variant==GPU:
            gpu_lease=acquire_gpu_lease(app,'two-update-qualification');checked['gpu_lease']=gpu_lease[1]
        with Personal._verified_scope(app,policy,'qualification'):
            return _qualify_locked(app,python,asset_root,snapshot,snapshot_sha,assets,out,checked)
    finally:
        # A cleanup timeout or unobservable child preserves both leases. Only
        # authenticated dead-owner recovery may later archive these tokens.
        if not checked['execution_owned_active']:
            try:
                if gpu_lease is not None:release_gpu_lease(gpu_lease)
            finally:release_cpu_lease(lease)

def _qualify_locked(app,python,asset_root,snapshot,snapshot_sha,assets,out,checked):
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    start=time.monotonic()
    variant=checked['variant'];device=device_for_variant(variant)
    out.mkdir(parents=True);env=_env(out/'private-env',variant);environment=observe(app,python,env,variant);profile=profile_for_source(app,variant)
    rows=checked['verified']['data_binding']['rows'];train=[r for r in rows if r['split']=='train']
    preferred=['ss_ai_03f07f322c563240bf61185ef8980d1d','ss_ai_137489b9593957ac7c935259297ba817'];indexed={r['sample_id']:r for r in train}
    selected=[indexed[i] for i in preferred] if all(i in indexed for i in preferred) else train[:2]
    require(len(selected)==2,'E_WINDOWS_SCR_DATA','two training occurrences required')
    fixed=[{'sample_id':r['sample_id'],'label':r['label'],'png_sha256':r['source_png_sha256']} for r in selected]
    launch_issued=int(time.time())
    request={'schema':'windows-scr-server-launch/1','app':str(app),'python':str(python),'base_asset_root':str(Path(asset_root).resolve()),
        'snapshot':str(Path(snapshot).resolve()),'snapshot_sha256':snapshot_sha,'assets':{k:str(Path(v).resolve()) for k,v in assets.items()},
        'out':str(out/'result'),'profile':profile,'environment':environment,'fixed_rows':fixed,'reference':fixed[0],
        'windows_cache_identity':cache_identity(app,variant,environment['identity_sha256'],snapshot_sha),'nonce':os.urandom(32).hex(),
        'personal_policy_sha256':checked['policy']['sha256'],'issued_at':launch_issued,
        'expires_at':launch_issued+900,'input_binding':checked['input_binding'],
        'issuer_pid':checked['lease']['pid'],'issuer_process_start':checked['lease']['process_start'],
        'lease_nonce':checked['lease']['nonce']}
    if variant==GPU:request['gpu_lease_nonce']=checked['gpu_lease']['nonce']
    _key(app);request['hmac_sha256']=_mac(app,request);_write(out/'request.json',request)
    import psutil
    peak={'tree_rss_bytes':0,'tmp_used_bytes':0,'combined_bytes':0,'new_output_bytes':0}
    child=None;owned=None;identity=None;reason=None;failure=None;cleanup=[];code=None
    def stop_owned():
        if owned is None or identity is None:return
        try:
            if not owned.is_running() or (owned.pid,owned.create_time())!=identity:return
            children=[]
            for c in owned.children(recursive=True):
                try:children.append((c,c.create_time()))
                except psutil.NoSuchProcess:pass
            for c,created in reversed(children):
                try:
                    if c.is_running() and c.create_time()==created:c.terminate()
                except psutil.NoSuchProcess:pass
            if owned.is_running() and (owned.pid,owned.create_time())==identity:child.terminate()
        except (psutil.NoSuchProcess,psutil.AccessDenied) as exc:cleanup.append(str(exc))
    try:
        with (out/'stdout.log').open('xb') as stdout,(out/'stderr.log').open('xb') as stderr:
            child=subprocess.Popen([str(python),'-I','-B','-X','utf8',str(app/PRODUCER),str(out/'request.json')],cwd=app,env=env,stdout=stdout,stderr=stderr)
            checked['execution_owned_active']=True
            owned=psutil.Process(child.pid);identity=(owned.pid,owned.create_time());_write(out/'START.json',{'pid':identity[0],'create_time':identity[1],'max_seconds':900,'max_rss_bytes':int(4.5*2**30),'GPU_used':variant==GPU})
            while child.poll() is None:
                try:
                    rss=psutil.Process().memory_info().rss
                    for c in (owned,*owned.children(recursive=True)):
                        try:rss+=c.memory_info().rss
                        except psutil.NoSuchProcess:pass
                    written=sum(p.stat().st_size for p in out.rglob('*') if p.is_file())
                    temporary=sum(p.stat().st_size for p in (out/'private-env').rglob('*') if p.is_file())
                    peak['tree_rss_bytes']=max(peak['tree_rss_bytes'],rss);peak['tmp_used_bytes']=max(peak['tmp_used_bytes'],temporary)
                    peak['combined_bytes']=peak['tree_rss_bytes']+peak['tmp_used_bytes'];peak['new_output_bytes']=max(peak['new_output_bytes'],written)
                    if peak['combined_bytes']>=4.5*2**30:reason='MEMORY_BUDGET'
                    elif written>128*2**20:reason='OUTPUT_BUDGET'
                    elif time.monotonic()-start>900:reason='TIME_BUDGET'
                except psutil.NoSuchProcess:
                    if child.poll() is not None:break
                except (OSError,psutil.AccessDenied) as exc:reason='MONITOR_FAILURE';failure=str(exc)
                if reason:stop_owned();break
                time.sleep(.2)
            code=child.wait(timeout=15)
        resource={'schema':'windows-scr-training-resource/1','returncode':code,'stop_reason':reason,'monitor_failure':failure,'cleanup_failures':cleanup,'peak':peak,
            'elapsed_seconds':time.monotonic()-start,'stop_bytes':int(4.5*2**30),'absolute_budget_bytes':5*2**30,'max_new_output_bytes':128*2**20,
            'existing_tmp_data_deleted':False,'only_identity_verified_owned_processes_terminated':True,'retry_count':0,'existing_processes_stopped':False,'GPU_used':variant==GPU,
            'tracked_pid':identity[0],'tracked_create_time':identity[1]}
        if code!=0 or reason is not None:_write(out/'RESOURCE.json',resource)
        require(code==0 and reason is None,'E_WINDOWS_SCR_TRIAL_FAILED','real producer failed; preserved outputs: '+str(out))
        result=_read(out/'result/EXECUTOR-RESULT.json');require(result['environment']==environment,'E_WINDOWS_SCR_ENVIRONMENT','observed worker environment differs')
        if variant==GPU:resource['cuda_allocation_observation']=result['component']['cuda_allocation_observation']
        proof={'schema':SCHEMA,'status':'MEASURED_FULL_SCR_GPU_TRIAL' if variant==GPU else 'MEASURED_FULL_SCR_CPU_TRIAL','variant_id':variant,'recipe_id':RECIPE_ID,'purpose':'full-scr-qualification',
            'device':device,'synthetic_test_only':False,'GPU_used':variant==GPU,'may_prepare':False,'may_train':False,'may_resume':False,'may_publish':False,'claims_quality_pass':False,
            'training_recipe':contract(app),'execution_profile_sha256':profile['execution_profile_sha256'],'source_hashes':profile['required_source_files'],
            'verifier_sha256':sha(app/VERIFIER),'producer_sha256':sha(app/PRODUCER),'dependency_lock_sha256':profile['lock_sha256'],
            'environment':environment,'windows_cache_identity':request['windows_cache_identity'],'component':result['component'],'resource_receipt':resource,
            'python_sha256':sha(python),'runtime_identity_sha256':checked['base']['asset_identity_sha256'],'base_npz_sha256':checked['base']['manifest']['base_npz_sha256']}
        verified=verify_complete_candidate(app,variant,proof,environment,python,snapshot,result['asset_paths'],out/'result',base_asset_root=asset_root)
        require(verified['status']=='RECORDS_AND_LOCAL_BYTES_VALIDATED_ONLY','E_WINDOWS_SCR_VERIFICATION','complete independent checks failed')
        require(profile==profile_for_source(app,variant) and observe(app,python,env,variant)==environment,'E_WINDOWS_SCR_SOURCE','post-trial source/environment drift')
        # Include independent post-verification and fresh environment observation
        # in the bounded trial. Nothing is signed after an exceeded budget.
        resource['elapsed_seconds']=time.monotonic()-start
        peak['tree_rss_bytes']=max(peak['tree_rss_bytes'],psutil.Process().memory_info().rss)
        peak['tmp_used_bytes']=max(peak['tmp_used_bytes'],sum(p.stat().st_size for p in (out/'private-env').rglob('*') if p.is_file()))
        peak['combined_bytes']=peak['tree_rss_bytes']+peak['tmp_used_bytes']
        peak['new_output_bytes']=max(peak['new_output_bytes'],sum(p.stat().st_size for p in out.rglob('*') if p.is_file()))
        # Reserve 2MiB for the final proof copies and registration records.
        require(resource['elapsed_seconds']<=900 and peak['combined_bytes']<4.5*2**30
            and peak['new_output_bytes']+2*2**20<=128*2**20,'E_WINDOWS_SCR_BUDGET','whole verification exceeded bounded qualification budget')
        from .windows_training_capability import verify_trial_records
        verify_trial_records(app,variant,proof,environment)
        _write(out/'RESOURCE.json',resource)
        body={'schema':'windows-scr-server-capability/1','proof':proof,'python':str(python),'asset_root':str(Path(asset_root).resolve()),
            'snapshot':str(Path(snapshot).resolve()),'asset_paths':result['asset_paths'],'result':str((out/'result').resolve()),'request_sha256':sha(out/'request.json'),
            'personal_policy_sha256':checked['policy']['sha256'],'input_binding':checked['input_binding']}
        envelope={'body':body,'hmac_sha256':_mac(app,body)};cap=value_sha(envelope);_write(_root(app)/'capabilities'/f'{cap}.json',envelope)
        record_issued=int(time.time())
        record={'schema':'windows-scr-training-registration/1','ownership':'external_read_only','variant_id':variant,'device':device,'python':str(python),
            'python_sha256':sha(python),'asset_root':str(Path(asset_root).resolve()),'asset_identity_sha256':proof['runtime_identity_sha256'],
            'dependency_lock_sha256':profile['lock_sha256'],'execution_profile_sha256':profile['execution_profile_sha256'],
            'environment_identity_sha256':environment['identity_sha256'],'capability_receipt_sha256':cap,'purpose':'prepare-train-infer',
            'personal_policy_sha256':checked['policy']['sha256'],'issued_at':record_issued,
            'expires_at':record_issued+Personal.TTL,'synthetic_test_only':False,'may_infer':True,
            'scr_feature_asset_paths':{k:request['assets'][k] for k in ('module','checkpoint','state_schema','font_sans','font_serif')},
            'may_resume':False,'may_publish':False,'claims_quality_pass':False}
        record['hmac_sha256']=_mac(app,record)
        rid=value_sha(record)[:32];record['registration_id']=rid;_write(_root(app)/'registrations'/rid/'registration.json',record)
        _write(out/'CAPABILITY.json',envelope);_write(out/'REGISTRATION.json',record);return inspect(app,rid,observe_environment=False)
    finally:
        if child is not None and child.poll() is None:stop_owned();child.wait(timeout=15)
        if child is not None and child.poll() is not None:checked['execution_owned_active']=False
def has_registration(app,rid):return isinstance(rid,str) and re.fullmatch('[0-9a-f]{32}',rid) is not None and (_root(app)/'registrations'/rid/'registration.json').is_file()
def inspect(app,rid,*,purpose='train',observe_environment=True):
    policy=Personal.bootstrap(app)
    with Personal._verified_scope(app,policy,'verification',rid):
        return _inspect_verified(app,rid,purpose,observe_environment,policy)

def _inspect_verified(app,rid,purpose,observe_environment,policy):
    require(purpose in ('prepare','train','inference','resume'),'E_WINDOWS_SCR_PURPOSE','unsupported Windows training operation')
    require(has_registration(app,rid),'E_WINDOWS_SCR_REGISTRATION_MISSING','no measured Windows training registration')
    if purpose == 'resume':
        require(policy['body']['device']=='cpu', 'FULL_TRAINER_RESUME_QUALIFICATION_REQUIRED',
            'GPU full trainer resume has no independent qualification implementation')
        require((_root(app)/'resume-attestations'/rid).is_dir(),
            'FULL_TRAINER_RESUME_QUALIFICATION_REQUIRED','no independent full-checkpoint trial for this registration')
    record=_read(_root(app)/'registrations'/rid/'registration.json')
    require(record['registration_id']==rid==value_sha({k:v for k,v in record.items() if k!='registration_id'})[:32],'E_WINDOWS_SCR_REGISTRATION','record changed')
    Personal.assert_fresh_record(record,policy)
    signed={k:v for k,v in record.items() if k not in ('registration_id','hmac_sha256')}
    require(hmac.compare_digest(str(record.get('hmac_sha256')),_mac(app,signed)),'E_WINDOWS_SCR_AUTH','untrusted registration')
    variant=record.get('variant_id');device=device_for_variant(variant)
    require(record.get('schema')=='windows-scr-training-registration/1' and record.get('device')==device
        and record.get('purpose')=='prepare-train-infer' and record.get('may_infer') is True and record.get('may_resume') is False and record.get('may_publish') is False
        and record.get('claims_quality_pass') is False,'E_WINDOWS_SCR_PURPOSE','registration purpose differs')
    env=_read(_root(app)/'capabilities'/f"{record['capability_receipt_sha256']}.json")
    require(value_sha(env)==record['capability_receipt_sha256'] and hmac.compare_digest(str(env.get('hmac_sha256')),_mac(app,env['body'])),'E_WINDOWS_SCR_AUTH','untrusted capability')
    body=env['body'];proof=body['proof'];observed=proof['environment']
    require(body.get('personal_policy_sha256')==policy['sha256'],'E_PERSONAL_RUNTIME_BINDING','capability policy differs')
    require(isinstance(body.get('input_binding'),dict) and body['input_binding'].get('sha256')
        ==value_sha({k:v for k,v in body['input_binding'].items() if k!='sha256'}),
        'E_WINDOWS_SCR_INPUTS','complete authenticated qualification input binding required')
    request=_read(Path(body['result']).parent/'request.json')
    require(sha(Path(body['result']).parent/'request.json')==body['request_sha256']
        and request.get('input_binding')==body['input_binding']
        and request.get('personal_policy_sha256')==policy['sha256']
        and hmac.compare_digest(str(request.get('hmac_sha256')),_mac(app,{k:v for k,v in request.items() if k!='hmac_sha256'})),
        'E_WINDOWS_SCR_AUTH','original authenticated producer launch differs')
    fresh=preflight(app,record['python'],record['asset_root'],body['snapshot'],
        proof['component']['scr_data_binding']['snapshot_sha256'],{k:body['asset_paths'][k] for k in ('module','checkpoint','state_schema','font_sans','font_serif','source_ledger')},variant=variant)
    require(not fresh['errors'] and fresh['input_binding']==body['input_binding'],
        'E_WINDOWS_SCR_INPUTS','current qualification inputs changed')
    verified=verify_complete_candidate(app,variant,proof,observed,record['python'],body['snapshot'],body['asset_paths'],body['result'],base_asset_root=record['asset_root'])
    if observe_environment:
        folder=_root(app)/'observations';folder.mkdir(parents=True,exist_ok=True)
        observed=observe(app,record['python'],_env(folder,variant),variant)
        require(observed==proof['environment'],'E_WINDOWS_SCR_ENVIRONMENT','actual environment changed')
    require(sha(record['python'])==record['python_sha256'] and record['execution_profile_sha256']==profile_for_source(app,variant)['execution_profile_sha256']
        and record['environment_identity_sha256']==observed['identity_sha256'],'E_WINDOWS_SCR_BINDING','current identities differ')
    from .runtime_variants import verify_asset_root
    assets=verify_asset_root(app,record['asset_root'])
    Personal.assert_fresh_record(record,policy)
    selected = {'status':'READY','ready':True,'full_model_ready':True,'environment_ready':True,'device':device,'variant_id':variant,
        'registration_id':rid,'registration':record,'python':record['python'],**assets,'scr_feature_asset_paths':record['scr_feature_asset_paths'],
        'may_prepare':True,'may_train':True,'may_infer':True,'may_resume':False,'may_publish':False,'claims_quality_pass':False,'verification':verified}
    if purpose == 'resume':
        from .windows_resume_runtime import inspect_capability
        selected['resume_capability'] = inspect_capability(app, selected, policy)
        selected['may_resume'] = True
    return selected
def resolve(app,binding,*,purpose='train'):
    from .runtime_variants import validate_binding_shape
    validate_binding_shape(binding);require(binding['device']==device_for_variant(binding['variant_id']),'E_WINDOWS_SCR_VARIANT','independent matching device training binding required')
    result=inspect(app,binding['registration_id'],purpose=purpose);require(make_binding(result)==binding,'E_WINDOWS_SCR_BINDING','complete binding changed');return result
