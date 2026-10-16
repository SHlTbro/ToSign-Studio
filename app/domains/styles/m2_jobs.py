"""Idempotent, observable evaluation/AI jobs. No client-posted quality statuses."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import threading
import uuid
from app.core.errors import ApiError
from . import runstore

PROJ=Path(__file__).resolve().parents[3]
_LOCK=threading.RLock()
_THREADS={}


def _root(user_id,run_id):return runstore.run_dir(user_id,run_id)
def _state_path(user_id,run_id,kind):return _root(user_id,run_id)/'jobs'/f'{kind}.json'
def _write_state(user_id,run_id,kind,body):
    previous=runstore.read_json(_state_path(user_id,run_id,kind)) or {}
    body=dict(body)
    for key in ('runtime_binding_sha256','config_sha256','pid','pid_birth','log'):
        if key in ('pid','pid_birth','log') and body.get('attempt')!=previous.get('attempt'):continue
        if key not in body and key in previous:body[key]=previous[key]
    runstore.write_json_atomic(_state_path(user_id,run_id,kind),body)


def state(user_id,run_id,kind):
    body=runstore.read_json(_state_path(user_id,run_id,kind)) or {}
    value=body.get('state','not_started')
    if value=='running' and (user_id,run_id,kind) not in _THREADS:
        # A controller restart is not a completed evaluation. Never silently
        # launch a duplicate; reconciliation inspects the recorded attempt.
        return 'blocked'
    return value


def _bundle_provider(user_id,run_id):
    path=os.environ.get('YY_AI_REVIEW_BUNDLE')
    if not path:return None
    package=_root(user_id,run_id)/'review-package.json'
    if not package.is_file():return None
    from engine.style.visual_review.offline import BoundSessionReviewer
    return BoundSessionReviewer(path,package,user_id,run_id,expected_sha256=os.environ.get('YY_AI_REVIEW_BUNDLE_SHA256'))


def ai_available(user_id,run_id):
    from engine.style.visual_review import pipeline
    if pipeline._REVIEWER is not None:return True
    try:return _bundle_provider(user_id,run_id) is not None
    except (OSError,ValueError,KeyError):return False


def _process_may_be_alive(pid,pid_birth=None):
    """Read-only liveness probe; uncertainty must never launch duplicate work."""
    if not isinstance(pid,int) or isinstance(pid,bool) or pid<=0:return True
    if os.name=='nt':
        # Never use os.kill(pid,0) on Windows (different signal semantics).
        # Query and compare identity on one handle: PID reuse is not this attempt.
        if pid>0xffffffff:return True
        if pid_birth is not None and (type(pid_birth) is not int or not 0<pid_birth<2**64):return True
        try:
            import ctypes
            from ctypes import wintypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
            kernel.OpenProcess.restype=wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes=(wintypes.HANDLE,wintypes.DWORD)
            kernel.WaitForSingleObject.restype=wintypes.DWORD
            kernel.GetProcessTimes.argtypes=(wintypes.HANDLE,*(ctypes.POINTER(wintypes.FILETIME),)*4)
            kernel.GetProcessTimes.restype=wintypes.BOOL
            kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
            kernel.CloseHandle.restype=wintypes.BOOL
            handle=kernel.OpenProcess(0x1000|0x100000,False,pid)  # query limited + synchronize
            if not handle:return ctypes.get_last_error()!=87  # exact PID does not exist
            try:
                wait=kernel.WaitForSingleObject(handle,0)
                if wait==0:return False
                if wait!=258:return True  # failed/unknown observation
                if pid_birth is None:return True
                created,ended,cpu,user=(wintypes.FILETIME() for _ in range(4))
                if not kernel.GetProcessTimes(handle,ctypes.byref(created),ctypes.byref(ended),ctypes.byref(cpu),ctypes.byref(user)):return True
                return ((created.dwHighDateTime<<32)|created.dwLowDateTime)==pid_birth
            finally:kernel.CloseHandle(handle)
        except (OSError,AttributeError,ValueError):return True
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False
    except (PermissionError,OSError):return True


def _attempt_process_may_be_alive(user_id,run_id,recorded):
    if isinstance(recorded.get('pid'),int):return _process_may_be_alive(recorded['pid'],recorded.get('pid_birth'))
    # Older blocked receipts did not retain PID. Match the exact server-owned
    # immutable request path; any missing evidence/query error stays blocked.
    attempt=recorded.get('attempt');root=_root(user_id,run_id).resolve()
    if os.name!='nt' or not isinstance(attempt,str) or not re.fullmatch('[0-9a-f]{32}',attempt):return True
    request=root/'jobs'/f'evaluation-{attempt}.request.json'
    out=(root/'evaluations'/attempt).resolve()
    if not request.is_file() or Path(recorded.get('output_dir','')).resolve()!=out:return True
    report=runstore.read_json(out/'acceptance-report.json') or {}
    if not report.get('failure'):return True
    wanted=str(request).replace("'","''")
    script="$ErrorActionPreference='Stop';$p=@(Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -and $_.CommandLine.Contains('"+wanted+"') });$p.Count"
    try:
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return result.returncode!=0 or not isinstance(result.stdout,str) or result.stdout.strip()!='0'
    except (OSError,subprocess.TimeoutExpired,UnicodeError):return True


def start_evaluation(user_id,run_id,config,manifest):
    from engine.style.m2_adapter.runtime import runtime_python,verify_runtime
    if manifest.get('engine_mode')!='M2_ADAPTER':raise ApiError(409,'RUN_NOT_EVALUABLE','Real M2 artifact required')
    from engine.style.m2_adapter.runtime_variants import binding_for_config,validate_manifest_binding
    try:
        from .m2_topology import validate_topology_lineage
        validate_topology_lineage(config,manifest)
        validate_manifest_binding(config,manifest)
        from engine.style.m2_adapter import preserved_training_deployment as D
        preserved=D.lookup(PROJ,_root(user_id,run_id),user_id=user_id,config=config)
        selected=preserved['execution'] if preserved else binding_for_config(PROJ,config,user_scr_purpose='inference')
        execution_binding=preserved['runtime_binding'] if preserved else config.get('runtime_binding')
        runtime=verify_runtime(config.get('runtime_manifest_path'),config.get('fd_repo'))
    except (OSError,ValueError,KeyError) as exc:raise ApiError(409,'RUNTIME_UNAVAILABLE',str(exc))
    python=Path(selected['python']) if selected else runtime_python(config.get('runtime_manifest_path'))
    if not python.is_file():raise ApiError(409,'RUNTIME_UNAVAILABLE','ML interpreter missing')
    key=(user_id,run_id,'evaluation')
    with _LOCK:
        recorded_state=runstore.read_json(_state_path(user_id,run_id,'evaluation')) or {}
        changed=bool(execution_binding and recorded_state and recorded_state.get('runtime_binding_sha256')!=execution_binding['binding_sha256'])
        if execution_binding and recorded_state and recorded_state.get('config_sha256')!=runstore.canonical_sha256(config):
            raise ApiError(409,'EVALUATION_RUNTIME_CHANGED','Immutable training configuration differs')
        if changed:
            if not preserved or key in _THREADS or recorded_state.get('state') not in ('blocked','failed','running') or _attempt_process_may_be_alive(user_id,run_id,recorded_state):
                raise ApiError(409,'EVALUATION_RUNTIME_CHANGED','Old execution is not proven ended; preserve it')
            from engine.style.m2_adapter.windows_gpu_support import gpu_lock_path
            from engine.style.m2_adapter.windows_training_runtime import _root as windows_root
            lock_paths=[gpu_lock_path(PROJ),windows_root(PROJ)/'cpu-execution.lock',windows_root(preserved['source']['producer_app'])/'cpu-execution.lock']
            if any(path.exists() for path in lock_paths):raise ApiError(409,'EVALUATION_RECOVERY_REQUIRED','Shared GPU or owned CPU execution lease is still present')
            archive=_root(user_id,run_id)/'jobs'/f'evaluation-prior-binding-{uuid.uuid4().hex}.json'
            runstore.write_json_atomic(archive,{**recorded_state,'recovery_observation':'EXACT_OWNED_REQUEST_NO_PROCESS_AND_NO_EXECUTION_LEASE_PRIOR_BINDING_PRESERVED'})
        previous='not_started' if changed else state(user_id,run_id,'evaluation')
        if previous in ('running','complete'):return {'state':previous,'run_id':run_id}
        old=runstore.read_json(_state_path(user_id,run_id,'evaluation')) or {}
        if old.get('state')=='running' and not changed:
            recorded=Path(old.get('output_dir','')).resolve()
            allowed=(_root(user_id,run_id)/'evaluations').resolve()
            if not recorded.is_relative_to(allowed):raise ApiError(409,'EVALUATION_RECOVERY_REQUIRED','Recorded attempt path is invalid')
            result=runstore.read_json(recorded/'acceptance-report.json') or {}
            if result.get('standalone_unseen_M2')=='PASS':
                try:
                    commit_evaluation(user_id,run_id,config,manifest,recorded,result)
                    return {'state':'complete','run_id':run_id,'recovered':True}
                except (ValueError,OSError,KeyError,TypeError):pass
            if _process_may_be_alive(old.get('pid'),old.get('pid_birth')):
                raise ApiError(409,'EVALUATION_RECOVERY_REQUIRED','Previous process may still be running; retry after it finishes, without launching a duplicate')
            # Child is demonstrably gone. Preserve the incomplete attempt;
            # a retry receives a fresh directory and cannot overwrite it.
            archive=_root(user_id,run_id)/'jobs'/f'evaluation-interrupted-{uuid.uuid4().hex}.json'
            runstore.write_json_atomic(archive,{**old,'recovery_observation':'RECORDED_PROCESS_EXITED_NO_VALID_TERMINAL_RECEIPT'})
        token=uuid.uuid4().hex;out=_root(user_id,run_id)/'evaluations'/token
        _write_state(user_id,run_id,'evaluation',{'state':'running','attempt':token,'output_dir':str(out),'runtime_binding_sha256':(execution_binding or {}).get('binding_sha256'),'config_sha256':runstore.canonical_sha256(config)})
        thread=threading.Thread(target=_evaluate,args=(user_id,run_id,dict(config),dict(manifest),out,python,preserved),daemon=True)
        _THREADS[key]=thread;thread.start()
    return {'state':'running','run_id':run_id,'prior_binding_recovered':changed}


def _evaluate(user_id,run_id,config,manifest,out,python,preserved=None):
    key=(user_id,run_id,'evaluation');root=_root(user_id,run_id)
    try:
        entry=next(f for f in manifest['files'] if f['relative_path']=='artifact/m2_adapter.pt')
        snap=Path(config['snapshot_dir']);rows=[json.loads(l) for l in (snap/'samples.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
        labels={r['label'] for r in rows}
        targets=[c for c in config.get('acceptance_targets',['晴','梦','星','春','海','光','雪','山','月','云']) if c not in labels][:3]
        if len(targets)!=3:raise ValueError('three distinct unseen acceptance targets are required')
        from engine.style.visual_review.contracts import validate_preprocessing_lineage
        preprocessing = validate_preprocessing_lineage(manifest, requested_profile=config.get('preprocessing_profile'))
        inference={'adapter_path':str(root/entry['relative_path']),'adapter_sha256':entry['sha256'],
                   'snapshot_dir':str(snap),'snapshot_sha256':config['snapshot_sha256'],
                   'fd_repo':config['fd_repo'],'base_npz_sha256':config['base_npz_sha256'],
                   'runtime_manifest_path':config['runtime_manifest_path'],'content_ttf':config['content_font_path'],
                   'device':config.get('train',{}).get('device','cpu'),'steps':20,'sampler':'dpm',
                   'guidance_scale':7.5,'cpu_threads':2,'preprocessing_profile':preprocessing['profile']}
        from .m2_topology import inference_identity,validate_topology_lineage
        validate_topology_lineage(config,manifest)
        inference.update(inference_identity(manifest))
        protocol={'inference':inference,'target_chars':targets,'seed':20261001,'reference_label':config.get('acceptance_reference_label','永'),
                  'm0_font':config['content_font_path'],'visual_thresholds':config['visual_thresholds']}
        execution_binding=preserved['runtime_binding'] if preserved else config.get('runtime_binding')
        if execution_binding is not None:
            protocol['runtime_binding']=execution_binding
            inference['runtime_binding']=execution_binding
        if preserved:
            inference.update(deployment_binding=preserved['deployment_binding'],deployment_user_id=user_id)
        request=root/'jobs'/f'evaluation-{out.name}.request.json';runstore.write_json_atomic(request,protocol)
        log=root/'jobs'/f'evaluation-{out.name}.log'
        from engine.style.m2_adapter.runtime_variants import bound_subprocess_env
        env=bound_subprocess_env(execution_binding)
        from engine.style.m2_adapter.windows_training_recipe import RECIPE_ID as WINDOWS_RECIPE
        if config.get('recipe_id')==WINDOWS_RECIPE:
            from engine.style.m2_adapter.windows_training_runtime import _env
            env=bound_subprocess_env(execution_binding,base_env=_env(root/'jobs'/('evaluation-'+out.name+'-private'),execution_binding['variant_id']))
        env.update({'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','PYTHONDONTWRITEBYTECODE':'1'})
        with log.open('wb') as stream:
            proc=subprocess.Popen([str(python),'-u',str(PROJ/'scripts/run_m2_acceptance.py'),'prepare','--config',str(request),'--out-dir',str(out)],
                                   cwd=str(PROJ),stdout=stream,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,env=env)
            birth=None
            if os.name=='nt':
                try:
                    from engine.style.m2_adapter.personal_runtime_resources import process_start
                    birth=process_start(proc.pid)
                except (OSError,ValueError):pass
            _write_state(user_id,run_id,'evaluation',{'state':'running','attempt':out.name,'output_dir':str(out),'pid':proc.pid,'pid_birth':birth,'log':str(log)})
            try:code=proc.wait(timeout=1800)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=10)
                raise RuntimeError('bounded evaluation time expired')
        result=runstore.read_json(out/'acceptance-report.json') or {}
        if code!=0 or result.get('standalone_unseen_M2')!='PASS':raise RuntimeError(str(result.get('failure') or {'exit_code':code}))
        current=runstore.read_json(_state_path(user_id,run_id,'evaluation')) or {}
        if current.get('attempt')!=out.name:return  # superseded recovery owns current state
        commit_evaluation(user_id,run_id,config,manifest,out,result)
    except Exception as exc:
        current=runstore.read_json(_state_path(user_id,run_id,'evaluation')) or {}
        if current.get('attempt')==out.name:
            _write_state(user_id,run_id,'evaluation',{'state':'blocked','attempt':out.name,'output_dir':str(out),'detail':str(exc)})
    finally:
        with _LOCK:_THREADS.pop(key,None)



def build_review_request(package,package_path):
    """Export the actual frozen panel request without choosing a model/provider."""
    from engine.style.visual_review.offline import review_source_hashes
    return {'schema':'blinded-ai-review-request/2','user_id':package['user_id'],'run_id':package['run_id'],
            'nonce':package['nonce'],'package_sha256':runstore.sha256_file(package_path),
            'panels':package['panels'],'categories':package['categories'],'score_scale':'0..5 for each required dimension',
            'review_consumer_sha256':review_source_hashes(),
            'instructions':'Review actual panel pixels; reference tile is marked in metadata. Do not inspect private identity maps or model-output receipts before freezing scores.'}

def commit_evaluation(user_id,run_id,config,manifest,out,result):
    """Accept only local verified outputs; used by normal controller and recovery."""
    from engine.style.visual_review.contracts import validate_evaluation_binding, validate_preprocessing_lineage
    from engine.style.evaluator.receipt import generation_completeness, verify_receipt
    root=_root(user_id,run_id);out=Path(out).resolve()
    if not out.is_relative_to((root/'evaluations').resolve()):raise ValueError('attempt escapes authenticated run')
    from engine.style.m2_adapter.runtime_variants import validate_manifest_binding,resolve_binding,verify_execution_receipt
    runtime_binding=validate_manifest_binding(config,manifest)
    from engine.style.m2_adapter import preserved_training_deployment as D
    preserved=D.lookup(PROJ,root,user_id=user_id,config=config)
    execution_manifest=D.execution_view(manifest,preserved) if preserved else manifest
    execution_config=D.execution_view(config,preserved) if preserved else config
    if preserved:runtime_binding=preserved['runtime_binding']
    from .m2_topology import validate_topology_lineage
    topology=validate_topology_lineage(config,manifest)
    if runtime_binding is not None:
        resolve_binding(PROJ,runtime_binding,user_scr_purpose='inference')
        protocol=runstore.read_json(out/'protocol.json') or {}
        if protocol.get('runtime_binding')!=runtime_binding or result.get('protocol_sha256')!=runstore.sha256_file(out/'protocol.json'):
            raise ValueError('evaluation protocol runtime binding differs')
        from engine.style.m2_adapter.topology_dispatch import BASELINE_TOPOLOGY_ID
        if topology['id']!=BASELINE_TOPOLOGY_ID:
            validate_topology_lineage(execution_config,protocol.get('inference') or {})
    categories=result.get('categories') or {}
    if set(categories.values())!={'seen_train','validation','unseen'} or set(categories)!=set(result.get('panels',{})):
        raise ValueError('all three visual categories are required')
    entry=next(f for f in manifest['files'] if f['relative_path']=='artifact/m2_adapter.pt')
    eval_path=Path(result['evaluation_receipt']).resolve()
    if not eval_path.is_relative_to(out):raise ValueError('evaluation receipt escapes attempt')
    evaluation=runstore.read_json(eval_path)
    valid,errors=verify_receipt(evaluation)
    if not valid:raise ValueError(str(errors))
    complete,errors=generation_completeness(evaluation)
    if not complete:raise ValueError('incomplete evaluation generation: '+str(errors))
    # A metric FAIL is preserved for readiness; this validates provenance, not quality.
    preprocessing = validate_preprocessing_lineage(manifest, requested_profile=config.get('preprocessing_profile'))
    validate_evaluation_binding(evaluation,entry['sha256'],manifest['base_ckpt_sha256'],config['snapshot_sha256'],manifest.get('runtime_identity_sha256'),manifest.get('preprocessing'),runtime_binding,model_identity=execution_manifest,preserved_deployment=preserved)
    receipts={}
    for char,panel in result['panels'].items():
        panel_path=Path(panel['panel_path']).resolve()
        if not panel_path.is_relative_to(out) or runstore.sha256_file(panel_path)!=panel['panel_sha256']:raise ValueError('panel hash/path mismatch')
        ip=out/f'U+{ord(char):04X}'/'m2_inference.json';inf=runstore.read_json(ip)
        if inf.get('adapter_sha256')!=entry['sha256'] or inf.get('source')!='M2' or inf.get('quality_eligible') is not True or inf.get('dataset_snapshot_sha256')!=config['snapshot_sha256']:raise ValueError('inference identity mismatch')
        validate_preprocessing_lineage(manifest, inf)
        from .m2_topology import validate_topology_lineage
        validate_topology_lineage(execution_config,execution_manifest,inf)
        if runtime_binding is not None:verify_execution_receipt(runtime_binding,inf,'inference')
        if preserved:D.verify_inference_receipt(preserved,inf)
        if inf.get('inputs',{}).get('target_char')!=char or runstore.sha256_file(inf['output']['png'])!=inf['output']['png_sha256']:raise ValueError('inference pixel/target mismatch')
        receipts[char]=str(ip)
    first=next(c for c in categories if categories[c]=='unseen')
    receipt=root/'receipts';receipt.mkdir(exist_ok=True)
    shutil.copyfile(receipts[first],receipt/'m2_inference.json');shutil.copyfile(eval_path,receipt/'eval_receipt.json')
    package={'schema':'authenticated-ai-review-package/2','nonce':uuid.uuid4().hex,'user_id':user_id,'run_id':run_id,
             'attempt':out.name,'adapter_sha256':entry['sha256'],'snapshot_sha256':config['snapshot_sha256'],
             'runtime_identity_sha256':manifest.get('runtime_identity_sha256'),'preprocessing':preprocessing,'panels':result['panels'],
             'categories':categories,'primary_target':first,'inference_receipts':receipts,
             'protocol_sha256':result['protocol_sha256'],'scope':'Exact immutable displayed panel pixels only; no general vision capability implied'}
    if runtime_binding is not None:package['runtime_binding']=runtime_binding
    from .m2_topology import inference_identity
    package.update(inference_identity(manifest))
    package_path=root/'review-package.json'
    previous_package=runstore.read_json(package_path)
    if previous_package:
        if previous_package.get('attempt')==out.name:
            if 'preprocessing' not in previous_package:
                validate_preprocessing_lineage(manifest, previous_package)
                package.pop('preprocessing')
            package['nonce']=previous_package['nonce']
            if package!=previous_package:raise ValueError('existing review package changed within one attempt')
        else:
            archive=root/'jobs'/f"review-package-{previous_package.get('attempt','unknown')}.preserved.json"
            if not archive.exists():runstore.write_json_atomic(archive,previous_package)
    if not previous_package or package!=previous_package:runstore.write_json_atomic(package_path,package)
    request=build_review_request(package,package_path)
    runstore.write_json_atomic(root/'blinded-review-request.json',request)
    previous=runstore.read_json(_state_path(user_id,run_id,'evaluation')) or {}
    if previous and previous.get('attempt')!=out.name:
        history=root/'jobs'/f"evaluation-{previous.get('attempt','unknown')}.preserved.json"
        if not history.exists():runstore.write_json_atomic(history,previous)
    _write_state(user_id,run_id,'evaluation',{'state':'complete','attempt':out.name,'output_dir':str(out),'package_sha256':runstore.sha256_file(root/'review-package.json')})

def start_ai_review(user_id,run_id,config):
    if state(user_id,run_id,'evaluation')!='complete':raise ApiError(409,'EVALUATION_REQUIRED','Complete actual evaluation first')
    from engine.style.visual_review import pipeline
    try:provider=pipeline._REVIEWER or _bundle_provider(user_id,run_id)
    except (OSError,ValueError,KeyError) as exc:raise ApiError(409,'AI_PROVIDER_UNAVAILABLE',str(exc))
    if provider is None:raise ApiError(409,'AI_PROVIDER_UNAVAILABLE','No authorized pixel-viewing provider or exact session review bundle is configured')
    key=(user_id,run_id,'ai_review')
    with _LOCK:
        previous=state(user_id,run_id,'ai_review')
        if previous in ('running','complete'):return {'state':previous,'run_id':run_id}
        token=uuid.uuid4().hex
        previous_state=runstore.read_json(_state_path(user_id,run_id,'ai_review')) or {}
        if previous_state:
            archive=_root(user_id,run_id)/'jobs'/f"ai-review-{previous_state.get('attempt','legacy')}-{uuid.uuid4().hex}.preserved.json"
            runstore.write_json_atomic(archive,previous_state)
        _write_state(user_id,run_id,'ai_review',{'state':'running','attempt':token})
        thread=threading.Thread(target=_review,args=(user_id,run_id,config,provider,token),daemon=True);_THREADS[key]=thread;thread.start()
    return {'state':'running','run_id':run_id}


def _review(user_id,run_id,config,provider,token=None):
    from engine.style.visual_review.pipeline import review_blind_panel,finalize_visual_quality
    from engine.style.visual_review.contracts import validate_visual_receipt
    from engine.style.visual_review.pipeline import write_new
    root=_root(user_id,run_id);key=(user_id,run_id,'ai_review');token=token or uuid.uuid4().hex
    attempt=root/'reviews'/token;attempt.mkdir(parents=True,exist_ok=False)
    def terminal(body):
        body={**body,'attempt':token};write_new(attempt/'state.json',body)
        current=runstore.read_json(_state_path(user_id,run_id,'ai_review')) or {}
        if current.get('attempt')==token:_write_state(user_id,run_id,'ai_review',body)
    try:
        package_path=root/'review-package.json';package=runstore.read_json(package_path);reviews=[];first=None
        keys=list(package['panels']);primary=package.get('primary_target',keys[0]);keys=[primary]+[c for c in keys if c!=primary]
        for char in keys:
            panel=package['panels'][char]
            panel_path=Path(panel['panel_path']).resolve()
            if not panel_path.is_relative_to(root.resolve()):raise ValueError('panel escapes authenticated run')
            mapping=panel_path.parent/'private/identity-map.json';identity=runstore.read_json(mapping)
            response=review_blind_panel(panel,reviewer=provider)
            result=finalize_visual_quality(response,identity['identities'],config['visual_thresholds'])
            result['identity_map']={'path':str(mapping),'sha256':runstore.sha256_file(mapping)}
            inf_path=Path(package['inference_receipts'][char]);inference=runstore.read_json(inf_path)
            validate_visual_receipt(result,identity,config['visual_thresholds'],inference['output']['png_sha256'])
            result['provider_mode']=getattr(provider,'provider_mode','CONFIGURED_VISION_PROVIDER')
            result['review_package_sha256']=runstore.sha256_file(package_path);result['review_nonce']=package['nonce']
            path=attempt/f'U+{ord(char):04X}.m2_visual_review.json';write_new(path,result)
            reviews.append({'target_char':char,'visual_path':str(path),'visual_sha256':runstore.sha256_file(path),
                            'inference_path':str(inf_path),'inference_sha256':runstore.sha256_file(inf_path)})
            if first is None:first=result
        if not first:raise ValueError('empty review package')
        first=dict(first);first['review_set']=reviews
        write_new(attempt/'aggregate.json',first)
        current=runstore.read_json(_state_path(user_id,run_id,'ai_review')) or {}
        if current.get('attempt')!=token:return terminal({'state':'superseded'})
        pointer=root/'receipts/m2_visual_review.json'
        if pointer.is_file():shutil.copyfile(pointer,attempt/'prior-aggregate.json')
        runstore.write_json_atomic(pointer,first)
        terminal({'state':'complete','provider_mode':getattr(provider,'provider_mode','CONFIGURED_VISION_PROVIDER'),
                  'review_package_sha256':runstore.sha256_file(package_path)})
    except Exception as exc:terminal({'state':'blocked','detail':str(exc)})
    finally:
        with _LOCK:
            if _THREADS.get(key) is threading.current_thread():_THREADS.pop(key,None)



