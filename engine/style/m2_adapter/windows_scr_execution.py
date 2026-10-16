"""Private server-owned Windows CPU inference qualification, never training.

Registrations are a separate namespace from training runtimes. Only a monitored
producer launched here can mint an authenticated capability. HTTP callers cannot
supply a PASS object or a capability path to obtain authority.
"""
from pathlib import Path
import hashlib,hmac,json,os,subprocess,time
from .runtime_variants import value_sha,versions_from_lock,safe_relative,validate_binding_shape,verify_asset_root
from .runtime import sha
from .trainer_resume_protocol import native_path

CPU='windows-cp312-cu128-user-scr2n-inference-cpu-v1'
OLD0178='windows-cp312-cu128-scr2n-0178-inference-cpu-v1'
LOCK='requirements-r9-windows-gpu-cu128.lock'
WORKER='engine/style/m2_adapter/windows_scr_probe.py'
SCHEMA='windows-scr-inference-capability/1'

def _regular(path,directory=False):
    p=Path(path).absolute()
    if p.drive.upper()!='E:':raise ValueError('E_WINDOWS_EXECUTION_E_ONLY')
    if any(native_path(x).is_symlink() or native_path(x).is_junction() for x in (p,*p.parents)):raise ValueError('E_WINDOWS_EXECUTION_SYMLINK')
    if not (native_path(p).is_dir() if directory else native_path(p).is_file()):raise ValueError('E_WINDOWS_EXECUTION_MISSING: '+str(p))
    return p.resolve()

def inventory(app):
    from .user_scr_runtime import user_scr_source_hashes
    root=Path(app).resolve();files=user_scr_source_hashes(root)
    for rel in ('installer/runtime_addon.py','installer/probe_runtime_variant.py','scripts/probe_m2_runtime_scr_two_negative.py',LOCK,'requirements-personal-style-runtime.lock','config/m2-runtime-catalog.json','config/m2-runtime-variants.json'):
        files[rel]=sha(safe_relative(root,rel))
    if (root/'scripts/old0178_windows_inference.py').is_file():
        files['scripts/old0178_windows_inference.py']=sha(root/'scripts/old0178_windows_inference.py')
    return dict(sorted(files.items()))

def profile(app,variant=CPU):
    if variant not in (CPU,OLD0178):raise ValueError('E_WINDOWS_EXECUTION_PROFILE_DEVICE')
    root=Path(app).resolve()
    from .spatial_topology import topology_fields
    body={'schema':'windows-scr-inference-profile/1','variant_id':variant,'platform':'win32','device':'cpu',
      'purpose':'inference','lock':LOCK,'lock_sha256':sha(root/LOCK),'source_files':inventory(root),
      'producer_sha256':sha(root/WORKER),'max_seconds':300,'max_rss_bytes':int(4.5*2**30),
      'max_output_bytes':16*2**20,'max_cpu_threads':2,'may_train':False,'may_prepare':False,
      'may_resume':False,'may_publish':False,**topology_fields()}
    if variant==OLD0178:
        from .windows_scr_deployment import identity,OLD0178_MANIFEST_SHA
        pin=identity(OLD0178_MANIFEST_SHA)
        body.update(application_namespace=str(root),artifact_manifest_sha256=pin['artifact_manifest_sha256'],
                    terminal_adapter_sha256=pin['adapter_sha256'],training_receipt_sha256=pin['training_receipt_sha256'],
                    training_runtime_binding_sha256=pin['training_binding_sha256'])
    return {**body,'execution_profile_sha256':value_sha(body)}

def measure(app,python=None):
    """Called inside the actual interpreter, under the declared numerical policy."""
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    import sys,importlib.metadata
    if sys.platform!='win32':raise ValueError('E_WINDOWS_EXECUTION_PLATFORM')
    if python is not None and Path(sys.executable).resolve()!=Path(python).resolve():raise ValueError('E_WINDOWS_EXECUTION_INTERPRETER')
    wanted=versions_from_lock(Path(app)/LOCK);versions={k:importlib.metadata.version(k) for k in wanted}
    if versions!=wanted:raise ValueError('E_WINDOWS_EXECUTION_DEPENDENCIES')
    from .environment import execution_environment
    numeric=execution_environment('cpu')
    policy={'dtype':'float32','amp':False,'default_dtype':'torch.float32','torch_deterministic_algorithms':True,
      'matmul_tf32':False,'cudnn_tf32':False,'cudnn_benchmark':False,'cudnn_deterministic':True,
      'torch_cpu_threads':2,'torch_interop_threads':1,'mixed_cuda_cpu_deform':False,'gpu_name':None,'cublas_workspace_config':':4096:8'}
    if any(type(numeric.get(k))!=type(v) or numeric[k]!=v for k,v in policy.items()):raise ValueError('E_WINDOWS_EXECUTION_NUMERICAL_POLICY')
    return {'schema':'windows-scr-inference-environment/1','versions':versions,'numerical_environment':numeric,
      'identity_sha256':numeric['identity_sha256']}

def _root(app):return Path(app).resolve()/'runtime/windows-scr-inference'
def _read(path):
    p=_regular(path)
    if native_path(p).stat().st_size>2*2**20:raise ValueError('E_WINDOWS_EXECUTION_OVERSIZED')
    return json.loads(native_path(p).read_text(encoding='utf-8'))
def _write(path,value):
    path=Path(path);native_path(path.parent).mkdir(parents=True,exist_ok=True)
    if native_path(path).exists():
        if _read(path)!=value:raise ValueError('E_WINDOWS_EXECUTION_IMMUTABLE_CONFLICT')
        return
    with native_path(path).open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)
def _key(app,create=False):
    path=_root(app)/'private/capability-key'
    if create:
        path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists():
            with path.open('xb') as f:f.write(os.urandom(32))
    key=_regular(path).read_bytes()
    if len(key)!=32:raise ValueError('E_WINDOWS_EXECUTION_AUTHORITY')
    return key
def _mac(key,value):return hmac.new(key,json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode(),hashlib.sha256).hexdigest()

def _verify_probe(app,report,resource,python,asset_root,snapshot):
    from .windows_scr_deployment import ADAPTER_SHA,RECEIPT_SHA,SNAPSHOT_SHA,TRAINING_BINDING_SHA
    from engine.style.dataset.binding import verify_snapshot_binding
    from .spatial_topology import validate_spatial_topology_fields,SPATIAL_HEADS
    from .adapter_topology import RESIDUAL_HOOK_KEYS
    variant=report.get('variant_id',CPU)
    source=None
    if variant==OLD0178:
        from .windows_scr_deployment import verify_source,identity,OLD0178_MANIFEST_SHA
        source=verify_source(report.get('source'))
        pin=identity(OLD0178_MANIFEST_SHA)
        if source['artifact_manifest_sha256']!=pin['artifact_manifest_sha256']:raise ValueError('E_WINDOWS_EXECUTION_SOURCE_MODEL')
        ADAPTER_SHA,RECEIPT_SHA,TRAINING_BINDING_SHA=pin['adapter_sha256'],pin['training_receipt_sha256'],pin['training_binding_sha256']
    exact={'schema':SCHEMA,'status':'PASS','variant_id':variant,'device':'cpu','purpose':'inference',
      'terminal_adapter_sha256':ADAPTER_SHA,'training_receipt_sha256':RECEIPT_SHA,
      'training_runtime_binding_sha256':TRAINING_BINDING_SHA,'snapshot_sha256':SNAPSHOT_SHA,
      'weights_only':True,'strict_load':True,'FP32_finite_tensor_count':56,'optimizer_updates':0,
      'generation_calls':1,'may_train':False,'may_prepare':False,'may_resume':False,'may_publish':False,'claims_quality_pass':False}
    if any(type(report.get(k))!=type(v) or report[k]!=v for k,v in exact.items()):raise ValueError('E_WINDOWS_EXECUTION_FULL_PROOF')
    p=profile(app,variant)
    if report.get('profile')!=p:raise ValueError('E_WINDOWS_EXECUTION_SOURCE_CHANGED')
    if report.get('python')!=str(Path(python).resolve()) or report.get('python_sha256')!=sha(python):raise ValueError('E_WINDOWS_EXECUTION_INTERPRETER')
    assets=verify_asset_root(app,asset_root)
    if report.get('asset_identity_sha256')!=assets['asset_identity_sha256'] or report.get('base_npz_sha256')!=assets['manifest']['base_npz_sha256']:raise ValueError('E_WINDOWS_EXECUTION_ASSETS')
    if source is not None and source['base_npz_sha256']!=assets['manifest']['base_npz_sha256']:raise ValueError('E_WINDOWS_EXECUTION_ASSETS')
    if report.get('content_font_sha256')!=assets['manifest']['content_font']['sha256']:raise ValueError('E_WINDOWS_EXECUTION_FONT')
    if report.get('snapshot_binding')!=verify_snapshot_binding(snapshot,SNAPSHOT_SHA):raise ValueError('E_WINDOWS_EXECUTION_SNAPSHOT')
    validate_spatial_topology_fields(report)
    if report.get('adapter_hook_calls')!={k:20 for k in RESIDUAL_HOOK_KEYS}:raise ValueError('E_WINDOWS_EXECUTION_RESIDUAL_HOOKS')
    if report.get('spatial_offset_hook_calls')!={h['module']:20 for h in SPATIAL_HEADS}:raise ValueError('E_WINDOWS_EXECUTION_SPATIAL_HOOKS')
    counts=report.get('operator_execution_counts',{})
    if len(counts)!=6 or any(v!={'forward_calls':20,'cross_device_calls':0} for v in counts.values()):raise ValueError('E_WINDOWS_EXECUTION_OPERATORS')
    if report.get('base_state_before_sha256')!=report.get('base_state_after_sha256'):raise ValueError('E_WINDOWS_EXECUTION_BASE_MUTATION')
    if resource.get('status')!='PASS' or resource.get('exit_code')!=0 or resource.get('stop_reason') is not None or resource.get('existing_processes_stopped') is not False:raise ValueError('E_WINDOWS_EXECUTION_RESOURCE')
    if resource.get('peak_rss_bytes',p['max_rss_bytes']+1)>p['max_rss_bytes'] or resource.get('seconds',301)>p['max_seconds']:raise ValueError('E_WINDOWS_EXECUTION_RESOURCE')
    output=report.get('output',{})
    if output.get('passes_original_ink_guard') is not True:raise ValueError('E_WINDOWS_EXECUTION_INK_GUARD')
    for path_key,sha_key in (('raw_rgb_path','raw_rgb_sha256'),('alpha_path','alpha_sha256')):
        if sha(_regular(output[path_key]))!=output[sha_key]:raise ValueError('E_WINDOWS_EXECUTION_PIXELS_CHANGED')
    return assets

def qualify(app,python,asset_root,snapshot,core_run,out,*,variant=CPU):
    if variant==OLD0178:
        from . import personal_runtime_policy as Personal
        from .windows_training_runtime import acquire_cpu_lease,release_cpu_lease
        policy=Personal.bootstrap(app)
        preflight_old0178(app,python,asset_root,snapshot,core_run)
        shared=acquire_cpu_lease(app,'old0178-inference-qualification')
        checked={'execution_owned_active':False,'lease':shared[1]}
        try:
            with Personal._verified_scope(app,policy,'qualification'):
                return _qualify(app,python,asset_root,snapshot,core_run,out,variant=variant,policy=policy,checked=checked)
        finally:
            if not checked['execution_owned_active']:release_cpu_lease(shared)
    return _qualify(app,python,asset_root,snapshot,core_run,out,variant=variant)


def _qualify(app,python,asset_root,snapshot,core_run,out,*,variant=CPU,policy=None,checked=None):
    """Authorized server operation: own subprocess, measurement, signed admission."""
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    import psutil
    app=_regular(app,True);python=_regular(python);asset_root=_regular(asset_root,True);snapshot=_regular(snapshot,True);core_run=_regular(core_run,True)
    out=Path(out).absolute()
    if (out.drive.upper()!='E:' or out.exists() or out.resolve().is_relative_to(snapshot)
        or out.resolve().is_relative_to(core_run)
        or any(x.is_symlink() or x.is_junction() or (x/'FROZEN').exists() for x in (out,*out.parents))):
        raise ValueError('E_WINDOWS_EXECUTION_OUTPUT')
    source=None
    if variant==OLD0178:
        from .windows_scr_deployment import admit_source,OLD0178_MANIFEST_SHA
        source=admit_source(core_run,core_run/'artifact/artifact.json')
        if source['artifact_manifest_sha256']!=OLD0178_MANIFEST_SHA:raise ValueError('E_WINDOWS_EXECUTION_SOURCE_MODEL')
        # Reuse the already enrolled issuer. This path never creates a key.
        _old_key(app)
    out.mkdir(parents=True);p=profile(app,variant);lock=_root(app)/'qualification.lock';lock.parent.mkdir(parents=True,exist_ok=True)
    lease=lock.open('x',encoding='utf-8');lease.write(str(os.getpid()));lease.flush()
    env=dict(os.environ,CUDA_VISIBLE_DEVICES='',PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1',PYTHONIOENCODING='utf-8',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',CUBLAS_WORKSPACE_CONFIG=':4096:8',OMP_NUM_THREADS='2',MKL_NUM_THREADS='2')
    for key in ('TEMP','TMP','TMPDIR','HOME','USERPROFILE','APPDATA','LOCALAPPDATA','XDG_CACHE_HOME','TORCH_HOME','HF_HOME','CUDA_CACHE_PATH'):
        folder=out/'private-env'/key.lower();folder.mkdir(parents=True);env[key]=str(folder)
    for key in ('PYTHONPATH','PYTHONHOME','PYTHONUSERBASE'):env.pop(key,None)
    request={'app':str(app),'python':str(python),'asset_root':str(asset_root),'snapshot':str(snapshot),'core_run':str(core_run),'out':str(out/'result'),'profile':p}
    if source is not None:
        request.update(source=source,variant_id=variant)
        token=checked['lease']
        request.update(issuer_pid=token['pid'],issuer_process_start=token['process_start'],lease_nonce=token['nonce'])
        request=launch_request(app,request,policy,'qualification')
    _write(out/'request.json',request);start=time.monotonic();peak=0;reason=None;child=None
    try:
        with (out/'stdout.log').open('xb') as stdout,(out/'stderr.log').open('xb') as stderr:
            child=subprocess.Popen([str(python),'-I','-B','-X','utf8',str(app/WORKER),str(out/'request.json')],cwd=app,env=env,stdout=stdout,stderr=stderr)
            if checked is not None:checked['execution_owned_active']=True
            owned=psutil.Process(child.pid);identity=(owned.pid,owned.create_time())
            while child.poll() is None:
                try:
                    rss=sum(x.memory_info().rss for x in [owned,*owned.children(recursive=True)] if x.is_running());peak=max(peak,rss)
                    size=sum(x.stat().st_size for x in out.rglob('*') if x.is_file())
                    if rss>p['max_rss_bytes']:reason='RSS_LIMIT'
                    elif time.monotonic()-start>p['max_seconds']:reason='TIME_LIMIT'
                    elif size>p['max_output_bytes']:reason='OUTPUT_LIMIT'
                except psutil.NoSuchProcess:break
                except (OSError,psutil.AccessDenied) as exc:reason='MEASUREMENT_FAILED:'+str(exc)
                if reason:
                    if owned.is_running() and (owned.pid,owned.create_time())==identity:child.terminate()
                    break
                time.sleep(.25)
            code=child.wait(timeout=15)
        resource={'schema':'windows-scr-inference-resource/1','status':'PASS' if code==0 and reason is None else 'BLOCKED',
          'exit_code':code,'stop_reason':reason,'peak_rss_bytes':peak,'seconds':time.monotonic()-start,
          'tracked_pid':identity[0],'tracked_create_time':identity[1],'existing_processes_stopped':False}
        _write(out/'RESOURCE.json',resource)
        report=_read(out/'result/RECEIPT.json');assets=_verify_probe(app,report,resource,python,asset_root,snapshot)
        proof={'schema':'windows-scr-inference-server-admission/1','report':report,'resource':resource,
          'producer_sha256':p['producer_sha256'],'report_sha256':sha(out/'result/RECEIPT.json')}
        envelope={'proof':proof,'hmac_sha256':_mac(_old_key(app) if source is not None else _key(app,True),proof)};capability=value_sha(envelope)
        _write(_root(app)/'capabilities'/f'{capability}.json',envelope)
        record={'schema':'windows-scr-inference-registration/1','ownership':'external_read_only','variant_id':variant,
          'python':str(python),'python_sha256':sha(python),'asset_root':str(asset_root),'snapshot':str(snapshot),
          'asset_identity_sha256':assets['asset_identity_sha256'],'dependency_lock_sha256':p['lock_sha256'],
          'execution_profile_sha256':p['execution_profile_sha256'],'environment_identity_sha256':report['environment']['identity_sha256'],
          'capability_receipt_sha256':capability,'purpose':'inference','may_train':False,'may_publish':False}
        if source is not None:
            issued=int(time.time())
            record.update(personal_policy_sha256=policy['sha256'],source_sha256=source['sha256'],
                          artifact_manifest_sha256=source['artifact_manifest_sha256'],issued_at=issued,
                          expires_at=issued+policy['body']['qualification_ttl_seconds'],synthetic_test_only=False)
        rid=value_sha(record)[:32];record['registration_id']=rid
        _write(_root(app)/'registrations'/rid/'registration.json',record)
        return inspect(app,rid,observe=False,variant=variant)
    finally:
        dead=child is None
        try:
            if child is not None:
                if child.poll() is None:child.terminate();child.wait(timeout=15)
                dead=child.poll() is not None
        finally:
            lease.close()
            if dead:
                lock.unlink()
                if checked is not None:checked['execution_owned_active']=False

def inspect(app,rid,*,observe=True,purpose='inference',variant=CPU):
    if variant==OLD0178:
        from . import personal_runtime_policy as Personal
        if purpose!='inference':raise ValueError('E_WINDOWS_EXECUTION_INFERENCE_ONLY')
        policy=Personal.bootstrap(app)
        with Personal._verified_scope(app,policy,'verification'):
            return _inspect(app,rid,observe=observe,purpose=purpose,variant=variant,policy=policy)
    return _inspect(app,rid,observe=observe,purpose=purpose,variant=variant)


def _inspect(app,rid,*,observe=True,purpose='inference',variant=CPU,policy=None):
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    import re
    if purpose!='inference':raise ValueError('E_WINDOWS_EXECUTION_INFERENCE_ONLY')
    if not re.fullmatch('[0-9a-f]{32}',str(rid)):raise ValueError('E_WINDOWS_EXECUTION_REGISTRATION')
    record=_read(_root(app)/'registrations'/rid/'registration.json')
    if value_sha({k:v for k,v in record.items() if k!='registration_id'})[:32]!=rid:raise ValueError('E_WINDOWS_EXECUTION_REGISTRATION_CHANGED')
    envelope=_read(_root(app)/'capabilities'/(record['capability_receipt_sha256']+'.json'))
    key=_old_key(app) if variant==OLD0178 else _key(app)
    if value_sha(envelope)!=record['capability_receipt_sha256'] or not hmac.compare_digest(envelope['hmac_sha256'],_mac(key,envelope['proof'])):raise ValueError('E_WINDOWS_EXECUTION_UNTRUSTED_PASS')
    proof=envelope['proof'];report=proof['report']
    assets=_verify_probe(app,report,proof['resource'],record['python'],record['asset_root'],record['snapshot'])
    if record['variant_id']!=variant or record['execution_profile_sha256']!=profile(app,variant)['execution_profile_sha256']:raise ValueError('E_WINDOWS_EXECUTION_PROFILE_CHANGED')
    if variant==OLD0178:
        from .runtime_variants import require
        require(record.get('personal_policy_sha256')==policy['sha256'] and record.get('synthetic_test_only') is False
                and record.get('artifact_manifest_sha256')==report['source']['artifact_manifest_sha256']
                and record.get('source_sha256')==report['source']['sha256'], 'E_WINDOWS_EXECUTION_SOURCE_CHANGED','independent model/source qualification required')
        issued,expires=record.get('issued_at'),record.get('expires_at')
        require(type(issued) is int and type(expires) is int and issued<=time.time()<expires
                and expires-issued==policy['body']['qualification_ttl_seconds'], 'E_WINDOWS_EXECUTION_EXPIRED','qualification is absent, expired or future dated')
    if record['environment_identity_sha256']!=report['environment']['identity_sha256']:raise ValueError('E_WINDOWS_EXECUTION_ENVIRONMENT_CHANGED')
    local_observation=None
    if observe and variant==OLD0178:
        from . import personal_runtime_policy as Personal
        local_observation=Personal._environment_observation(app,record,report['environment'],policy)
    if observe and (local_observation is None or local_observation[1] not in local_observation[0]['observed']):
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',CUDA_VISIBLE_DEVICES='',PYTHONUTF8='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',CUBLAS_WORKSPACE_CONFIG=':4096:8')
        observer=str(Path(app).resolve())
        if variant==OLD0178:
            from .windows_training_runtime import _env
            request=launch_request(app,{'app':str(Path(app).resolve()),'python':record['python'],'profile':profile(app,variant),
                'variant_id':variant,'source':report['source']},policy,'environment')
            path=_root(app)/'observations'/request['nonce']/'request.json';_write(path,request)
            env=_env(path.parent/'private-env')
            observer=str(path)
        observed=subprocess.run([record['python'],'-I','-B','-X','utf8',str(Path(app)/WORKER),'--environment',observer],cwd=app,env=env,capture_output=True,text=True,encoding='utf-8',timeout=60)
        if observed.returncode!=0 or json.loads(observed.stdout.splitlines()[-1])!=report['environment']:raise ValueError('E_WINDOWS_EXECUTION_ENVIRONMENT_CHANGED')
        if local_observation is not None:
            current=Personal._environment_observation(app,record,report['environment'],policy)
            if current is not None and current[0] is local_observation[0] and current[1]==local_observation[1]:
                current[0]['observed'].add(current[1])
    body={'schema':'m2-runtime-binding/1','registration_id':rid,'variant_id':variant,'device':'cpu',
      **{k:record[k] for k in ('asset_identity_sha256','dependency_lock_sha256','execution_profile_sha256','environment_identity_sha256','capability_receipt_sha256')}}
    binding={**body,'binding_sha256':value_sha(body)};validate_binding_shape(binding)
    return {**assets,'registration':record,'runtime_binding':binding,'python':record['python'],'device':'cpu','ready':True,
      'environment':report['environment'],'capability':proof,'may_train':False,'may_publish':False}

def resolve(app,binding,*,observe=True,purpose='inference'):
    if isinstance(binding,dict) and binding.get('variant_id')==OLD0178:
        from . import personal_runtime_policy as Personal
        policy=Personal.bootstrap(app)
        with Personal._verified_scope(app,policy,'verification'):
            return _resolve(app,binding,observe=observe,purpose=purpose)
    return _resolve(app,binding,observe=observe,purpose=purpose)


def _resolve(app,binding,*,observe=True,purpose='inference'):
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    validate_binding_shape(binding)
    if binding['variant_id'] not in (CPU,OLD0178) or binding['device']!='cpu':raise ValueError('E_WINDOWS_EXECUTION_PROFILE_DEVICE')
    selected=inspect(app,binding['registration_id'],observe=observe,purpose=purpose,variant=binding['variant_id'])
    if selected['runtime_binding']!=binding:raise ValueError('E_WINDOWS_EXECUTION_BINDING_CHANGED')
    return selected


def preflight_old0178(app,python,asset_root,snapshot,core_run):
    """Exact inputs and installed versions only; never import ML or mint authority."""
    from . import personal_runtime_policy as Personal
    from .windows_scr_deployment import admit_source,OLD0178_MANIFEST_SHA,SNAPSHOT_SHA
    from engine.style.dataset.binding import verify_snapshot_binding
    policy=Personal.bootstrap(app)
    app,python,asset_root,snapshot,core_run=(_regular(app,True),_regular(python),_regular(asset_root,True),_regular(snapshot,True),_regular(core_run,True))
    source=admit_source(core_run,core_run/'artifact/artifact.json')
    if source['artifact_manifest_sha256']!=OLD0178_MANIFEST_SHA:raise ValueError('E_WINDOWS_EXECUTION_SOURCE_MODEL')
    with Personal._verified_scope(app,policy,'verification'):
        assets=verify_asset_root(app,asset_root)
        binding=verify_snapshot_binding(snapshot,SNAPSHOT_SHA)
    if assets['manifest']['base_npz_sha256']!=source['base_npz_sha256']:raise ValueError('E_WINDOWS_EXECUTION_ASSETS')
    # Ensure the registered issuer exists before any bounded model launch.
    _old_key(app)
    wanted=versions_from_lock(app/LOCK)
    code='import importlib.metadata,json,sys; names=json.loads(sys.argv[1]); print(json.dumps({n:importlib.metadata.version(n) for n in names}))'
    observer=_root(app)/'dependency-observations'/os.urandom(16).hex()
    from .windows_training_runtime import _env
    observed=subprocess.run([str(python),'-I','-B','-X','utf8','-c',code,json.dumps(list(wanted))],
                            cwd=app,env=_env(observer),capture_output=True,text=True,encoding='utf-8',timeout=30)
    if observed.returncode!=0:raise ValueError('E_WINDOWS_EXECUTION_DEPENDENCIES: '+observed.stderr[-1200:])
    versions=json.loads(observed.stdout.splitlines()[-1])
    if versions!=wanted:raise ValueError('E_WINDOWS_EXECUTION_DEPENDENCIES')
    import psutil,shutil
    if psutil.virtual_memory().available<5*2**30 or shutil.disk_usage(app).free<2*2**30:raise ValueError('E_WINDOWS_EXECUTION_RESOURCES')
    return {'status':'READY_FOR_QUALIFICATION','source_sha256':source['sha256'],'adapter_sha256':source['adapter_sha256'],
            'manifest_sha256':source['artifact_manifest_sha256'],'snapshot_sha256':binding['snapshot_sha256'],
            'asset_identity_sha256':assets['asset_identity_sha256'],'versions':versions,
            'execution_profile_sha256':profile(app,OLD0178)['execution_profile_sha256'],
            'inference_ready':False,'may_publish':False,'claims_quality_pass':False}


def _old_key(app):
    from .windows_training_runtime import _key
    return _key(app,create=False)


def launch_request(app,request,policy,purpose):
    from .personal_runtime_resources import process_start
    issued=int(time.time())
    body={**request,'schema':'windows-scr-inference-launch/1','purpose':purpose,
          'personal_policy_sha256':policy['sha256'],'issued_at':issued,
          'expires_at':issued+(300 if purpose=='qualification' else 60),'nonce':os.urandom(32).hex(),
          'issuer_pid':os.getpid(),'issuer_process_start':process_start()}
    return {**body,'hmac_sha256':_mac(_old_key(app),body)}


def validate_launch(request,*,purpose=None):
    from . import personal_runtime_policy as Personal
    from .runtime_variants import require
    from .windows_scr_deployment import verify_source,OLD0178_MANIFEST_SHA
    import sys
    app=Path(__file__).resolve().parents[3];policy=Personal.bootstrap(app)
    require(isinstance(request,dict) and request.get('schema')=='windows-scr-inference-launch/1'
            and request.get('variant_id')==OLD0178 and request.get('purpose') in ('qualification','environment')
            and (purpose is None or request.get('purpose')==purpose)
            and Path(request.get('app','')).resolve()==app,'E_WINDOWS_EXECUTION_LAUNCH','owned old0178 launch required')
    body={k:v for k,v in request.items() if k!='hmac_sha256'}
    require(hmac.compare_digest(str(request.get('hmac_sha256')),_mac(_old_key(app),body)),
            'E_WINDOWS_EXECUTION_AUTHORITY','untrusted launch request')
    Personal.assert_launch_fresh(request,policy)
    if type(request.get('issuer_pid')) is not int or type(request.get('issuer_process_start')) is not int:
        raise ValueError('E_WINDOWS_EXECUTION_LAUNCHER')
    from .windows_training_runtime import _require_owned_launcher,_read as read_training,_root as training_root
    if request['purpose']=='qualification':
        if not isinstance(request.get('lease_nonce'),str):raise ValueError('E_WINDOWS_EXECUTION_LEASE')
        lease=read_training(training_root(app)/'cpu-execution.lock')
        if any(lease.get(k)!=request.get(v) for k,v in (('pid','issuer_pid'),('process_start','issuer_process_start'),('nonce','lease_nonce'))) or lease.get('task')!='old0178-inference-qualification':
            raise ValueError('E_WINDOWS_EXECUTION_LEASE')
    _require_owned_launcher(request)
    source=verify_source(request.get('source'))
    require(source['artifact_manifest_sha256']==OLD0178_MANIFEST_SHA and request.get('profile')==profile(app,OLD0178)
            and Path(sys.executable).resolve()==Path(request['python']).resolve(),
            'E_WINDOWS_EXECUTION_SOURCE_CHANGED','launch source/interpreter differs')
    return app,policy
