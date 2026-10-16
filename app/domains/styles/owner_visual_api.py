"""Owned HTTP adapter; direct acceptance never writes a blind-review row."""
import json
from app.core.errors import ApiError
from app.core.audit import audit
from engine.style.visual_review import owner_direct as D
from . import runstore,service
def context(session,user,run_id):
    run=service._get_owned_run(session,user,run_id)
    if run.status!='succeeded':raise ApiError(409,'OWNER_PREVIEW_NOT_READY','Real M2 run must have succeeded')
    manifest=service._load_verified_manifest(run);config=json.loads(run.config or '{}')
    from engine.style.visual_review.readiness import check_run_readiness
    facts=check_run_readiness(runstore.run_dir(user,run_id),manifest,run.status,run_config=config,user_id=user)
    return run,manifest,config,facts
def discover(session,user,run_id):
    run=service._get_owned_run(session,user,run_id)
    try:
        _,manifest,config,facts=context(session,user,run_id);gates=facts['engineering_gates']
        if not all(gates.get(k)=='PASS' for k in D.CORE):raise ValueError('Real training, inference and hash gates required')
        candidate=D.candidate(runstore.run_dir(user,run_id),manifest,config,user)
        review=D.assess(runstore.run_dir(user,run_id),manifest,config,user,gates)
        images=[{**row,'url':f'/api/v1/style-runs/{run_id}/owner-visual-preview/{i}?candidate_id={candidate["candidate_id"]}'} for i,row in enumerate(candidate['images'])]
        return {'status':'READY','review_mode':D.MODE,'candidate_id':candidate['candidate_id'],'images':images,'model_version':candidate['model_version'],
            'snapshot_sha256':candidate['snapshot_sha256'],'evaluation_gate':gates.get('evaluation'),'AI_VISUAL_REVIEW':facts.get('AI_VISUAL_REVIEW'),
            'acceptance':review,'may_publish':review['may_publish'],'future_outputs_accepted':False}
    except (ApiError,ValueError,OSError,KeyError,TypeError,StopIteration) as exc:
        return {'status':'BLOCKED','review_mode':D.MODE,'images':[],'may_publish':False,'detail':str(exc),'future_outputs_accepted':False}
def accept(session,user,run_id,payload):
    _,manifest,config,facts=context(session,user,run_id)
    try:result=D.submit(runstore.run_dir(user,run_id),manifest,config,user,facts['engineering_gates'],payload)
    except (ValueError,OSError,KeyError,TypeError,StopIteration) as exc:raise ApiError(409,'OWNER_ACCEPTANCE_NOT_RECORDED',str(exc)) from exc
    audit(session,action='owner_direct_visual_acceptance',actor=str(user),object_ref=f'style_runs/{run_id}',result=f'candidate={result["candidate_id"]} receipt={result["receipt_sha256"]} blind_review=false idempotent={result["idempotent"]}')
    return result
def preview(session,user,run_id,index,candidate_id):
    _,manifest,config,facts=context(session,user,run_id)
    try:
        D.require(all(facts['engineering_gates'].get(k)=='PASS' for k in D.CORE),'current engineering preview gates required')
        root=runstore.run_dir(user,run_id);current=D.candidate(root,manifest,config,user)
        D.require(candidate_id==current['candidate_id'] and type(index) is int and 0<=index<len(current['images']),'preview is stale or missing')
        row=current['images'][index];raw=D.contained(root,row['relative_path']).read_bytes()
        import hashlib
        D.require(hashlib.sha256(raw).hexdigest()==row['sha256'],'preview bytes changed')
        return raw,row['sha256']
    except (ValueError,OSError,KeyError,TypeError,StopIteration) as exc:raise ApiError(409,'OWNER_PREVIEW_STALE',str(exc)) from exc


# Imported output evidence shares direct-owner authentication/MAC, and grants
# only exact-output appearance acceptance. Run readiness/publish is untouched.
def _output_owned(session,user,run_id,item_id):
    from app.domains.render.models import RenderJob,RenderItem
    run=service._get_owned_run(session,user,run_id)
    item=session.query(RenderItem).filter(RenderItem.item_id==item_id,RenderItem.user_id==user).first()
    if item is None:raise ApiError(404,'JOB_NOT_FOUND','Owned output not found')
    job=session.query(RenderJob).filter(RenderJob.job_id==item.job_id,RenderJob.user_id==user).first()
    if job is None:raise ApiError(404,'JOB_NOT_FOUND','Owned output not found')
    manifest=service._load_verified_manifest(run)
    adapter=next(f for f in manifest['files'] if f['relative_path']=='artifact/m2_adapter.pt')
    from engine.style.visual_review import output_evidence as E
    D.require(D.sha(D.contained(runstore.run_dir(user,run_id),adapter['relative_path']))==adapter['sha256'],'actual owned model bytes changed or missing')
    binding=E.target(job,item,adapter['sha256'])
    return run,job,item,binding

def _packages_root(user,run_id):return runstore.run_dir(user,run_id)/'owner-output-packages'

def output_import(session,user,run_id,raw,expected_sha):
    from engine.style.visual_review import output_evidence as E
    service._get_owned_run(session,user,run_id)
    try:m,files=E.unpack(raw,expected_sha)
    except (OSError,ValueError,KeyError,TypeError,__import__('zipfile').BadZipFile) as exc:raise ApiError(409,'OWNER_EVIDENCE_PACKAGE_INVALID',str(exc)) from exc
    root=_packages_root(user,run_id)/expected_sha
    root.parent.mkdir(parents=True,exist_ok=True)
    if root.exists():
        prior=root/'package.zip'
        if not prior.is_file() or prior.read_bytes()!=raw:raise ApiError(409,'OWNER_EVIDENCE_PACKAGE_CONFLICT','Existing import differs; no overwrite')
    else:
        if sum(1 for p in root.parent.iterdir() if p.is_dir())>=16:raise ApiError(409,'OWNER_EVIDENCE_PACKAGE_LIMIT','At most16 immutable packages per run; existing imports remain usable')
        try:
            root.mkdir(exist_ok=True)
            with (root/'package.zip').open('xb') as f:f.write(raw)
        except FileExistsError:
            if (root/'package.zip').read_bytes()!=raw:raise ApiError(409,'OWNER_EVIDENCE_PACKAGE_CONFLICT','Concurrent import differs; refresh to verify')
    audit(session,action='owner_output_evidence_import',actor=str(user),object_ref=f'style_runs/{run_id}',result=f'package={expected_sha} acceptance_recorded=false')
    return {'package_ok':True,'package_id':expected_sha,'profile_id':m['profile_id'],'model_sha256':m['model_sha256'],'acceptance_recorded':False,'may_publish':False}

def _output_package(user,run_id,package_id):
    from engine.style.visual_review import output_evidence as E
    E.pin(package_id);root=_packages_root(user,run_id)/package_id
    raw=D.contained(_packages_root(user,run_id),root/'package.zip').read_bytes()
    return E.unpack(raw,package_id)

def output_targets(session,user,run_id):
    from app.domains.render.models import RenderItem
    service._get_owned_run(session,user,run_id);items=[]
    rows=session.query(RenderItem).filter(RenderItem.user_id==user,RenderItem.status.in_(('completed','completed_with_warning'))).order_by(RenderItem.created_at.desc()).limit(100).all()
    for item in rows:
        try:
            _,job,_,binding=_output_owned(session,user,run_id,item.item_id)
            items.append({'item_id':item.item_id,'job_id':job.job_id,'label':item.cn,'render_version':item.render_version,'status':'READY','layout_profile':binding['layout_profile']})
        except (ApiError,OSError,ValueError,KeyError,TypeError,StopIteration):continue
    return {'items':items,'may_publish':False}

def output_context(session,user,run_id,package_id,item_id):
    from engine.style.visual_review import output_evidence as E
    run,job,item,binding=_output_owned(session,user,run_id,item_id)
    manifest,files=_output_package(user,run_id,package_id)
    current=E.candidate(package_id,manifest,binding,user,run_id)
    return run,job,item,manifest,files,current

def output_discover(session,user,run_id,package_id,item_id):
    from engine.style.visual_review import output_evidence as E
    service._get_owned_run(session,user,run_id)
    try:
        run,job,item,manifest,files,current=output_context(session,user,run_id,package_id,item_id)
        base=f'/api/v1/style-runs/{run_id}/owner-output-preview/{item_id}'
        query=f'?package_id={package_id}&candidate_id={current["candidate_id"]}'
        return {'status':'READY','package_ok':True,'target_binding':{'ok':True},'candidate_id':current['candidate_id'],'model_sha256':current['target_binding']['model_sha256'],
            'acceptance_scope':'exact_render_output','item_id':item_id,'render_version':item.render_version,'profile_id':manifest['profile_id'],
            'images':[{'url':base+'/reference'+query,'label':'Imported reference'},{'url':base+'/target'+query,'label':'Current owned output'}],
            'acceptance':E.assess(runstore.run_dir(user,run_id),current),'future_outputs_accepted':False,'may_publish':False,'blind_review_performed':False}
    except (ApiError,OSError,ValueError,KeyError,TypeError,StopIteration) as exc:
        return {'status':'BLOCKED','package_ok':False,'target_binding':{'ok':False},'images':[],'detail':str(exc),'may_publish':False}

def output_accept(session,user,run_id,package_id,item_id,payload):
    from engine.style.visual_review import output_evidence as E
    try:
        *_,current=output_context(session,user,run_id,package_id,item_id)
        result=E.submit(runstore.run_dir(user,run_id),current,payload)
    except (ApiError,OSError,ValueError,KeyError,TypeError,StopIteration) as exc:
        if isinstance(exc,ApiError) and exc.status==404:raise
        raise ApiError(409,'OWNER_OUTPUT_ACCEPTANCE_NOT_RECORDED',str(exc)) from exc
    audit(session,action='owner_direct_output_acceptance',actor=str(user),object_ref=item_id,result=f'candidate={current["candidate_id"]} package={package_id} scope=exact_render_output blind_review=false may_publish=false')
    return result

def output_preview(session,user,run_id,package_id,item_id,candidate_id,kind):
    from app.core.storage import storage_root
    *_,manifest,files,current=output_context(session,user,run_id,package_id,item_id)
    D.require(current['candidate_id']==candidate_id,'target changed; refresh before viewing')
    if kind=='reference':raw=files[manifest['reference_image']]
    elif kind=='target':raw=D.contained(storage_root(),current['target_binding']['output_relative_path']).read_bytes()
    else:raise ApiError(404,'JOB_NOT_FOUND','Output preview not found')
    pin=__import__('hashlib').sha256(raw).hexdigest()
    D.require(pin==current['target_binding']['image_sha256'],'preview bytes changed after target check')
    return raw,pin
