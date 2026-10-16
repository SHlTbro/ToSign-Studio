"""Append-only, independent resume authority for a measured primary runtime.

There is no caller proof import and no key creation. A primary two-update
registration remains immutable and still has may_resume=False. Only a monitored
three-process trial can append an independently verified resume capability.
"""
from pathlib import Path
import hmac, json, os, re, time
import shutil, subprocess, threading, copy
from .runtime_variants import require, sha, value_sha, make_binding
from .windows_resume_recipe import capability_scope, plan
from .trainer_resume_protocol import new_trial_nonce
from . import windows_training_runtime as W
from . import personal_runtime_policy as Personal

TASK = 'full-trainer-resume-qualification'
SCHEMA = 'windows-full-trainer-resume-authority/1'
PRODUCER = 'engine/style/m2_adapter/windows_resume_probe.py'

def _context(app, selected, binding, config, snapshot, nonce, out):
    from .windows_resume_capability import build_context
    _, _, _, _, config_sha = plan(config, binding, snapshot, out)
    root = Path(selected['fd_repo'])/'ckpt'
    return build_context(app, trial_nonce=nonce, resume_binding=binding,
        expected_trainer_config_hash=config_sha,
        profile_sha256=selected['registration']['execution_profile_sha256'],
        python=selected['python'], primary_registration_id=selected['registration_id'],
        snapshot=snapshot, base_state_files={f'base.{name}':str(root/f'{name}.npz')
            for name in ('unet','style_encoder','content_encoder')},
        environment_identity_sha256=selected['registration']['environment_identity_sha256'])

def inspect_capability(app, selected, policy):
    """Re-read authenticated local bytes; absent/unmeasured proofs stay closed."""
    from .windows_resume_capability import verify_exports
    rid = selected['registration_id']
    index = W._root(app)/'resume-attestations'/rid
    require(index.is_dir(), 'FULL_TRAINER_RESUME_QUALIFICATION_REQUIRED',
            'Independent full-checkpoint trial has not been measured for this registration')
    valid = []
    for pointer in sorted(index.glob('*.json')):
        try:
            cap = pointer.stem
            require(re.fullmatch('[0-9a-f]{64}', cap) is not None, 'E_RESUME_AUTH', 'invalid capability name')
            require(W._read(pointer) == {'capability_sha256':cap}, 'E_RESUME_AUTH', 'immutable index differs')
            envelope = W._read(W._root(app)/'resume-capabilities'/f'{cap}.json')
            body = envelope['body']
            require(value_sha(envelope) == cap and hmac.compare_digest(str(envelope.get('hmac_sha256')), W._mac(app,body)),
                    'E_RESUME_AUTH', 'untrusted resume capability')
            require(body['schema'] == SCHEMA and body['primary_registration_id'] == rid
                and body['personal_policy_sha256'] == policy['sha256']
                and body['synthetic_test_only'] is False and body['may_publish'] is False
                and body['claims_quality_pass'] is False, 'E_RESUME_AUTH', 'capability purpose differs')
            require(body['issued_at'] <= time.time() < body['expires_at']
                and body['expires_at'] <= selected['registration']['expires_at'],
                'E_RESUME_EXPIRED', 'resume capability expired')
            context = _context(app,selected,body['resume_binding'],body['config'],body['snapshot'],
                               body['proof']['trial_nonce'],body['out'])
            require(context == body['context'], 'E_RESUME_CONTEXT', 'current source/data/config/runtime differs')
            resource = W._read(Path(body['out'])/'RESOURCE.json')
            require(value_sha(resource) == body['resource_sha256'], 'E_RESUME_RESOURCE', 'server monitor receipt changed')
            checked = verify_exports(app, body['out'], body['proof'], context, resource)
            require(checked['status'] == 'VALIDATED_LOCAL_BYTES_ONLY', 'E_RESUME_VERIFICATION', 'independent verification failed')
            valid.append({'capability_sha256':cap,'scope':capability_scope(body['resume_binding']),
                          'verification':checked,'expires_at':body['expires_at']})
        except (ValueError, OSError, KeyError, TypeError):
            # One expired/stale append must not shadow a later independently
            # valid capability. No file is rewritten or removed.
            continue
    require(valid, 'FULL_TRAINER_RESUME_QUALIFICATION_REQUIRED', 'No current independently measured full-checkpoint capability')
    return valid

def authorize_checkpoint(app, runtime_binding, checkpoint_binding):
    from .trainer_resume_protocol import validate_binding
    validate_binding(checkpoint_binding)
    require(checkpoint_binding['runtime_binding'] == runtime_binding, 'E_RESUME_RUNTIME', 'checkpoint runtime differs')
    _, selected = Personal.authorize_binding(app, runtime_binding, 'resume')
    require(make_binding(selected) == runtime_binding and selected.get('may_resume') is True,
            'FULL_TRAINER_RESUME_QUALIFICATION_REQUIRED', 'independent resume capability required')
    expected = capability_scope(checkpoint_binding)
    matching = [item for item in selected['resume_capability'] if item['scope'] == expected]
    require(matching, 'E_RESUME_SCOPE', 'measured resume scope differs from owner/S03/source/config/recipe/artifacts')
    return {'status':'READY','capability_sha256':matching[-1]['capability_sha256'],
            'may_resume':True,'may_publish':False,'claims_quality_pass':False}

def validate_launch(request):
    """Validate live monitor/lease/source before any deferred ML import."""
    app = Path(__file__).resolve().parents[3]
    policy = Personal.bootstrap(app)
    require(isinstance(request,dict) and request.get('schema') == 'windows-full-trainer-resume-launch/1'
        and Path(request.get('app','')).resolve() == app, 'E_RESUME_AUTH', 'owned resume launch required')
    body = {k:v for k,v in request.items() if k != 'hmac_sha256'}
    require(hmac.compare_digest(str(request.get('hmac_sha256')),W._mac(app,body)), 'E_RESUME_AUTH','untrusted launch')
    Personal.assert_launch_fresh(request,policy)
    lease = W._read(W._root(app)/'cpu-execution.lock')
    require(lease == request['lease'] and lease.get('task') == TASK
        and lease['pid'] == request['issuer_pid'] and lease['process_start'] == request['issuer_process_start'],
        'E_RESUME_LEASE','live owned CPU lease required')
    W._require_owned_launcher(request)
    require(request['branch'] in ('continuous','cancelled','resumed')
        and Path(os.sys.executable).resolve() == Path(request['python']).resolve(),
        'E_RESUME_PROCESS','fixed branch and actual interpreter required')
    # Primary authority never borrows resume; trial execution has qualification
    # scope and the fresh native proof is checked only after all three exits.
    selected = W.inspect(app,request['registration_id'],purpose='train',observe_environment=False)
    require(make_binding(selected) == request['resume_binding']['runtime_binding'], 'E_RESUME_RUNTIME','primary binding differs')
    context = _context(app,selected,request['resume_binding'],request['config'],request['snapshot'],request['nonce'],request['trial_out'])
    require(context == request['context'], 'E_RESUME_CONTEXT','launch identities changed')
    target = Path(request['out']).absolute(); root = Path(request['trial_out']).absolute()
    require(target == root/request['branch'] and target.drive.upper() == 'E:'
        and not target.exists() and not any(p.is_symlink() or p.is_junction() or (p/'FROZEN').exists() for p in (target,*target.parents))
        and not target.is_relative_to(Path(request['snapshot']).resolve()), 'E_RESUME_OUTPUT','new regular E branch directory required')
    return app,policy,selected

def check_run(app, run):
    """Metadata preflight only. It never reads a key, observes ML, or registers."""
    from app.domains.styles.run_recovery import checkpoint_binding
    from .windows_training_recipe import RECIPE_ID
    from .windows_user_scr import profile_for_source, CPU
    config=json.loads(run.config or '{}'); config.pop('_server_continuation',None)
    require(config.get('recipe_id') == RECIPE_ID,'E_RESUME_RECIPE','server Windows SCR run required')
    binding=checkpoint_binding(run,app)
    rid=(config.get('runtime_binding') or {}).get('registration_id')
    require(W.has_registration(app,rid),'E_WINDOWS_SCR_REGISTRATION_MISSING','same-source primary qualification must be completed first')
    # This public existence check is explicitly not primary authentication.
    snapshot=config.get('snapshot_dir')
    require(isinstance(snapshot,str) and bool(snapshot),'E_RESUME_SNAPSHOT','server frozen S03 directory required')
    plan(config,binding,snapshot,Path(app)/'runtime/resume-preflight-placeholder')
    return {'status':'METADATA_ONLY_NOT_QUALIFIED','primary_registration_id':rid,
        'source_profile_sha256':profile_for_source(app,CPU)['execution_profile_sha256'],
        'snapshot':str(Path(snapshot).resolve()),'resume_binding':binding,'config':config,
        'models_loaded':False,'training_started':False,'key_read':False,'may_resume':False,
        'may_publish':False,'claims_quality_pass':False}

def _directory_bytes(root):
    """Count only this new tree; fail on links before traversing subdirectories."""
    total=0
    for directory,dirs,files in os.walk(root,followlinks=False):
        for name in [*dirs,*files]:
            path=Path(directory)/name
            require(not path.is_symlink() and not path.is_junction(),'E_RESUME_OUTPUT','linked trial output')
        for name in files:
            try:total+=(Path(directory)/name).stat().st_size
            except FileNotFoundError:pass  # owned atomic checkpoint replacement
    return total

def qualify_for_run(app, run, out):
    """Future explicit operator operation. Never auto-run from POST /resume."""
    from .windows_resume_recipe import SECONDS, OUTPUT_BYTES, STOP_BYTES
    from .windows_resume_capability import verify_exports
    from .personal_runtime_resources import process_start
    import psutil
    app=Path(app).resolve(); policy=Personal.bootstrap(app)
    checked=check_run(app,run);config=checked['config'];binding=checked['resume_binding']
    selected=W.inspect(app,checked['primary_registration_id'],purpose='train',observe_environment=False)
    require(make_binding(selected)==binding['runtime_binding'],'E_RESUME_RUNTIME','server primary runtime differs')
    primary=W._read(W._root(app)/'capabilities'/f"{selected['registration']['capability_receipt_sha256']}.json")['body']
    snapshot=Path(checked['snapshot']).resolve()
    require(str(snapshot)==str(Path(primary['snapshot']).resolve()),'E_RESUME_SNAPSHOT','same primary frozen S03 required')
    out=Path(out).absolute()
    require(out.drive.upper()=='E:' and not out.exists() and not out.is_relative_to(snapshot)
        and not any(p.is_symlink() or p.is_junction() or (p/'FROZEN').exists() for p in (out,*out.parents)),
        'E_RESUME_OUTPUT','new regular E trial directory outside frozen data required')
    require(shutil.disk_usage(out.anchor).free>=4*2**30,'E_RESUME_DISK','at least4GiB free E required')
    # This merely reuses an already approved private key; no creation path exists.
    W._key(app)
    lease=W.acquire_cpu_lease(app,TASK)
    child=None;owned_identity=None;cleanup=[];start=time.monotonic();nonce=new_trial_nonce()
    stop_guard=threading.Event();guard_failures=[];guard_thread=None;monitor_lock=threading.RLock()
    resource={'schema':'windows-trainer-resume-resource/1','returncode':0,'stop_reason':None,
        'monitor_failure':None,'cleanup_failures':cleanup,'max_new_output_bytes':OUTPUT_BYTES,
        'max_seconds':SECONDS,'stop_bytes':STOP_BYTES,'absolute_budget_bytes':5*2**30,
        'minimum_free_E_bytes':4*2**30,'GPU_used':False,'existing_processes_stopped':False,
        'existing_tmp_data_deleted':False,'only_identity_verified_owned_processes_terminated':True,
        'retry_count':0,'peak':{'tree_rss_bytes':0,'tmp_used_bytes':0,'combined_bytes':0,'new_output_bytes':0},
        'elapsed_seconds':0,'free_E_before_bytes':shutil.disk_usage(out.anchor).free,'owned_processes':{}}
    def stop_owned():
        if child is None or child.poll() is not None:return
        # Never target an inherited process or a recycled PID; no global process
        # enumeration, process-name matching, or pre-existing process stops.
        require(owned_identity is not None and process_start(child.pid)==owned_identity['birth'],
                'E_RESUME_PROCESS','owned child identity cannot be confirmed')
        child.terminate();child.wait(timeout=15)
    def observe_resources():
        rss=psutil.Process().memory_info().rss
        if child is not None and child.poll() is None:
            require(process_start(child.pid)==owned_identity['birth'],'E_RESUME_PROCESS','owned child identity changed')
            actual=psutil.Process(child.pid)
            require(not actual.children(),'E_RESUME_PROCESS','zero-worker trial must not spawn descendants')
            rss+=actual.memory_info().rss
        peak=resource['peak'];peak['tree_rss_bytes']=max(peak['tree_rss_bytes'],rss)
        peak['tmp_used_bytes']=max(peak['tmp_used_bytes'],_directory_bytes(out/'private-env'))
        peak['combined_bytes']=peak['tree_rss_bytes']+peak['tmp_used_bytes']
        peak['new_output_bytes']=max(peak['new_output_bytes'],_directory_bytes(out))
        resource['elapsed_seconds']=time.monotonic()-start
        require(peak['combined_bytes']<STOP_BYTES and peak['new_output_bytes']+2*2**20<=OUTPUT_BYTES
            and resource['elapsed_seconds']<=SECONDS,'E_RESUME_BUDGET','independent whole-trial budget exceeded')
    def monitor():
        with monitor_lock:
            require(not guard_failures,'E_RESUME_MONITOR',str(guard_failures))
            observe_resources()
    def guard():
        # Continues during the parent's independent NPZ verification, where an
        # occasional before/after memory sample could miss a temporary peak.
        while not stop_guard.is_set():
            try:
                with monitor_lock:observe_resources()
            except psutil.NoSuchProcess:
                if child is not None and child.poll() is None:guard_failures.append('owned child became unobservable')
            except (OSError,ValueError,psutil.Error) as exc:
                guard_failures.append(str(exc));return
            stop_guard.wait(.1)
    try:
        out.mkdir(parents=True,exist_ok=False);env=W._env(out/'private-env')
        guard_thread=threading.Thread(target=guard,name='owned-resume-resource-guard',daemon=True);guard_thread.start()
        environment=W.observe(app,selected['python'],env)
        require(environment['identity_sha256']==selected['registration']['environment_identity_sha256'],
                'E_RESUME_ENVIRONMENT','current primary numerical environment changed')
        context=_context(app,selected,binding,config,snapshot,nonce,out)
        points={};branches={};checkpoint=None
        for role in ('continuous','cancelled','resumed'):
            issued=int(time.time());request={'schema':'windows-full-trainer-resume-launch/1',
                'app':str(app),'python':selected['python'],'registration_id':selected['registration_id'],
                'config':config,'resume_binding':binding,'snapshot':str(snapshot),'nonce':nonce,
                'context':context,'environment':environment,'trial_out':str(out),'out':str(out/role),'branch':role,
                'checkpoint':checkpoint if role=='resumed' else None,'lease':lease[1],
                'issuer_pid':lease[1]['pid'],'issuer_process_start':lease[1]['process_start'],
                'personal_policy_sha256':policy['sha256'],'issued_at':issued,
                'expires_at':min(issued+SECONDS,int(start+SECONDS-time.monotonic()+time.time()))}
            request['hmac_sha256']=W._mac(app,request);launch=out/f'{role}-launch.json';W._write(launch,request)
            with (out/f'{role}-stdout.log').open('xb') as stdout,(out/f'{role}-stderr.log').open('xb') as stderr:
                with monitor_lock:
                    child=subprocess.Popen([selected['python'],'-I','-B','-X','utf8',str(app/PRODUCER),str(launch)],
                        cwd=app,env=env,stdout=stdout,stderr=stderr)
                    actual=psutil.Process(child.pid)
                    owned_identity={'pid':child.pid,'birth':process_start(child.pid),'create_time':actual.create_time(),
                                    'launch_nonce':nonce+':'+role}
                resource['owned_processes'][role]={k:owned_identity[k] for k in ('pid','create_time','launch_nonce')}
                W._write(out/f'{role}-START.json',owned_identity)
                while child.poll() is None:
                    try:monitor()
                    except psutil.NoSuchProcess:
                        if child.poll() is None:raise
                    time.sleep(.2)
                resource['returncode']=child.wait(timeout=15)
                require(resource['returncode']==0,'E_RESUME_TRIAL','native branch failed; preserved '+str(out))
            result=W._read(out/role/'BRANCH.json')
            require(result['pid']==child.pid and result['branch']==role and result['environment']==environment,
                    'E_RESUME_PROCESS','actual native branch differs from owned monitor')
            branches[role]={**resource['owned_processes'][role],'status':result['status'],
                'optimizer_steps':result['global_step'],'restored_step':2 if role=='resumed' else 0}
            points.update(result['points'])
            if role=='cancelled':checkpoint=result['checkpoint']
            monitor()
            with monitor_lock:child=None;owned_identity=None
        require(_context(app,selected,binding,config,snapshot,nonce,out)==context,'E_RESUME_CONTEXT','post-trial identities changed')
        require(W.observe(app,selected['python'],env)==environment,'E_RESUME_ENVIRONMENT','post-trial numerical environment changed')
        directory=Path(checkpoint['directory']);saved_sha=checkpoint['manifest_sha256']
        proof={'schema':'windows-full-trainer-resume-proof/1','trial_nonce':nonce,'context_sha256':context['context_sha256'],
            'synthetic_test_only':False,'GPU_used':False,'points':points,'branches':branches,
            'checkpoint':{'generation_dir':directory.relative_to(out).as_posix(),'manifest_sha256':saved_sha,
                'manifest_path':(directory/f'resume-{saved_sha}.json').relative_to(out).as_posix()},
            'resource_receipt':resource}
        monitor()
        observed=copy.deepcopy(resource);proof['resource_receipt']=observed
        verified=verify_exports(app,out,proof,context,observed)
        monitor();stop_guard.set();guard_thread.join(timeout=2)
        require(not guard_thread.is_alive() and not guard_failures,'E_RESUME_MONITOR','whole-trial resource guard failed')
        # The heavy verifier ran under the guard. Re-check the final external
        # resource numbers without repeating the heavy tensor read after stop.
        from .windows_resume_capability import _resources
        _resources(resource,branches,context)
        proof['resource_receipt']=copy.deepcopy(resource)
        require(verified['status']=='VALIDATED_LOCAL_BYTES_ONLY','E_RESUME_VERIFICATION','independent bytes not validated')
        require(time.monotonic()-start<=SECONDS,'E_RESUME_BUDGET','verification exceeded whole trial deadline')
        W._write(out/'RESOURCE.json',resource);W._write(out/'PROOF.json',proof)
        issued=int(time.time())
        body={'schema':SCHEMA,'primary_registration_id':selected['registration_id'],
            'personal_policy_sha256':policy['sha256'],'issued_at':issued,
            'expires_at':min(issued+Personal.TTL,selected['registration']['expires_at']),
            'synthetic_test_only':False,'may_publish':False,'claims_quality_pass':False,
            'resume_binding':binding,'config':config,'snapshot':str(snapshot),'out':str(out),
            'context':context,'proof':proof,'resource_sha256':value_sha(resource)}
        envelope={'body':body,'hmac_sha256':W._mac(app,body)};cap=value_sha(envelope)
        W._write(W._root(app)/'resume-capabilities'/f'{cap}.json',envelope)
        W._write(W._root(app)/'resume-attestations'/selected['registration_id']/f'{cap}.json',{'capability_sha256':cap})
        W._write(out/'CAPABILITY.json',envelope)
        return {'status':'QUALIFIED_PROTOCOL_ONLY','primary_registration_id':selected['registration_id'],
            'resume_capability_sha256':cap,'may_resume':True,'may_publish':False,'claims_quality_pass':False}
    except BaseException as exc:
        resource['stop_reason']=getattr(exc,'code','E_RESUME_MONITOR');resource['monitor_failure']=str(exc)
        resource['elapsed_seconds']=time.monotonic()-start
        if out.is_dir() and not (out/'FAILED-RESOURCE.json').exists():W._write(out/'FAILED-RESOURCE.json',resource)
        raise
    finally:
        stop_guard.set()
        if guard_thread is not None:guard_thread.join(timeout=2)
        if child is not None and child.poll() is None:
            try:stop_owned()
            except (OSError,ValueError,subprocess.TimeoutExpired) as exc:cleanup.append(str(exc))
        # Keep an unresolved execution lease for operator review, never allow a
        # second qualification to overlap a child whose exit is unconfirmed.
        if child is None or child.poll() is not None:W.release_cpu_lease(lease)
