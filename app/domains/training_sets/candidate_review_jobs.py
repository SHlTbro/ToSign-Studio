"""Native-draft AI review jobs with a separate operator-authorized evidence ingress.

Creating a job does not label, confirm, approve or train anything. HTTP cannot
submit scores. Applying a hash-pinned, trusted local bundle preserves AI identity.
"""
import io
import math
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image
from app.core import storage
from app.core.audit import audit
from app.core.errors import ApiError
from app.domains.assets_upload.models import Asset
from app.domains.styles.models import StyleProfile, StyleSample
from app.domains.training_sets.models import TrainingImage, TrainingSample
from .intake_models import CandidateReviewJob
from .source_admission import checked_ledger,require_training_source,source_path
from .visual_review import review_candidate_for_confirmation,save_confirmation_receipt
from engine.style.dataset.schema import DatasetError,review_digest,validate_candidate_response
from engine.style.visual_review.pipeline import sha,write_new
from engine.style.visual_review.policy import candidate_delegation
from engine.style.m2_adapter.trainer_resume_protocol import native_path

ROOT=Path(__file__).resolve().parents[3]
LOCK=threading.RLock()
JOB_RE=re.compile(r'[0-9a-f]{32}')
HASH_RE=re.compile(r'[0-9a-f]{64}')
CODE_FILES=('app/domains/training_sets/candidate_review_ingress.py','scripts/stage_candidate_review.py','app/domains/training_sets/candidate_review_export.py','app/domains/training_sets/candidate_review_jobs.py','app/domains/training_sets/source_admission.py','app/domains/training_sets/visual_review.py','app/domains/training_sets/intake_models.py','app/domains/extraction/service.py','app/domains/extraction/color_text.py','engine/style/dataset/schema.py','engine/style/visual_review/pipeline.py','config/personal-acceptance-policy.json','config/candidate-review-import-policy.json','app/domains/training_sets/router.py','app/domains/training_sets/service.py','app/domains/training_sets/models.py','app/domains/styles/service.py','app/core/storage.py','app/core/identity.py','scripts/import_candidate_review.py','config/r04-metadata-allowlist.json')

def require(ok,code,detail):
    if not ok:raise ApiError(409,code,detail)

def code_version():return {p:sha(ROOT/p) for p in CODE_FILES}

def policy_for(user_id):
    p=candidate_delegation(user_id)
    require(p is not None,'AI_CONFIRMATION_NOT_AUTHORIZED','current scoped candidate delegation required');return p

def profile_for(session,user_id,style_id,*,approved=False):
    require(type(style_id) is int and style_id>0,'INVALID_REQUEST','positive style_id required')
    p=session.query(StyleProfile).filter(StyleProfile.style_id==style_id,StyleProfile.user_id==user_id).first()
    require(p is not None and p.source_type=='HANDWRITING_ASSET','STYLE_NOT_FOUND','owned handwriting profile required')
    require(not approved or p.status=='approved','STYLE_NOT_APPROVED','normal profile approval is required before creating training candidates');return p

def job_root(user_id,job_id):
    require(type(user_id) is int and user_id>0 and isinstance(job_id,str) and JOB_RE.fullmatch(job_id),'INVALID_REQUEST','invalid job identity')
    candidate=storage.storage_root().resolve()/str(user_id)/'style_samples'/'candidate-review-jobs'/job_id
    resolved=storage.ensure_within_root(candidate)
    require(resolved==candidate,'REVIEW_JOB_CHANGED','review job directory cannot alias another storage location')
    return resolved

def get_job(session,user_id,job_id):
    row=session.query(CandidateReviewJob).filter(CandidateReviewJob.job_id==job_id,CandidateReviewJob.user_id==user_id).first()
    require(row is not None,'REVIEW_JOB_NOT_FOUND','review job not found')
    package=json.loads(row.package_json)
    require(isinstance(package,dict) and package.get('schema')=='native-candidate-review-job/1' and package.get('job_id')==job_id and package.get('user_id')==user_id and package.get('style_id')==row.style_id and isinstance(package.get('items'),list) and 1<=len(package['items'])<=100 and all(isinstance(x,dict) for x in package['items']),'REVIEW_JOB_CHANGED','malformed authoritative review package')
    require(review_digest(package)==row.package_sha256,'REVIEW_JOB_CHANGED','stored review package checksum changed')
    root=job_root(user_id,job_id)
    disk=storage.ensure_within_root(root/'package.json',root)
    require(disk.is_file() and json.loads(disk.read_text(encoding='utf-8'))==package,'REVIEW_JOB_CHANGED','package file and authoritative job differ')
    return row,package

def _native_item(session,user_id,style_id,item,ledger):
    from app.domains.extraction import service as E
    required={'candidate_id','source_asset_id','source_bbox_norm','draft_alpha_path'}
    require(isinstance(item,dict) and set(item)==required,'INVALID_REQUEST','native item requires exactly candidate_id/source_asset_id/source_bbox_norm/draft_alpha_path')
    cid=item['candidate_id'];require(isinstance(cid,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}',cid),'INVALID_REQUEST','invalid native candidate id')
    asset=session.query(Asset).filter(Asset.asset_id==item['source_asset_id'],Asset.user_id==str(user_id),Asset.enabled==1).first()
    admission=require_training_source(session,asset,user_id,style_id,ledger)
    box=E._validate_box_norm(item['source_bbox_norm'])
    match=re.fullmatch(r'users/(\d+)/styles/drafts/([0-9a-f]{16}\.png)',str(item['draft_alpha_path']))
    require(match is not None and match[1]==str(user_id),'INVALID_REQUEST','draft must be owned by this user')
    path=storage.draft_path(user_id,match[2]);require(path.is_file(),'DRAFT_MISSING','native draft no longer exists')
    # Existing intake drafts have no persisted source-box sidecar. Reproduce the
    # unchanged native algorithm and compare exact PNG bytes; never infer lineage.
    source=source_path(asset,user_id);before=sha(source);draft=path.read_bytes()
    img=E._load_asset_image(session,user_id,asset.asset_id);crop=E._crop_norm(img,box)
    mask=E.color_stroke_mask(crop)
    if mask is None:mask=E.DEFAULT_SEPARATOR.separate(crop)
    min_area=max(E.DESPECKLE_FLOOR_PX,int(E.DESPECKLE_RATE*mask.width*mask.height))
    comps=E._filter_components(E._connected_components(mask),min_area)
    require(bool(comps),'NATIVE_DRAFT_MISMATCH','source box has no native strokes')
    glyph=E._normalize_glyph(E._rebuild(mask,comps,min_area));buf=io.BytesIO();glyph.save(buf,format='PNG')
    require(buf.getvalue()==draft,'NATIVE_DRAFT_MISMATCH','draft is not the exact current native source-box extraction; edited pixels need a separate audited edit path')
    require(before==asset.sha256==sha(source) and path.read_bytes()==draft,'SOURCE_HASH_MISMATCH','source/draft changed during job construction')
    context=io.BytesIO();crop.save(context,format='PNG');context=context.getvalue()
    body={'candidate_id':cid,'source_asset_id':asset.asset_id,'source_image_sha256':asset.sha256,'source_width':asset.width,'source_height':asset.height,'source_bbox_norm':box,'draft_alpha_path':item['draft_alpha_path'],'image_sha256':review_digest_bytes(draft),'source_context_sha256':review_digest_bytes(context),'source_admission_sha256':admission['receipt_sha256'],'source_family_id':'sha256:'+asset.sha256,'history_status':admission['history_status'],'label':None,'needs_touchup':True,'native_lineage':'EXACT_RECOMPUTED_SOURCE_BOX_EXTRACTION_BYTES','original_candidate_unchanged':True}
    body['candidate_version_sha256']=review_digest(body)
    return body,draft,context

def strict_json(raw):
    def reject(value):raise ValueError('nonfinite JSON constant is forbidden: '+value)
    return json.loads(raw,parse_constant=reject)

def review_digest_bytes(b):
    import hashlib
    return hashlib.sha256(b).hexdigest()

def create_job(session,user_id,payload):
    require(isinstance(payload,dict) and set(payload)=={'style_id','items'},'INVALID_REQUEST','only style_id and native items accepted')
    style_id=payload['style_id'];profile_for(session,user_id,style_id);policy=policy_for(user_id)
    items=payload['items'];require(isinstance(items,list) and 1<=len(items)<=100,'INVALID_REQUEST','1..100 native items required')
    ledger=checked_ledger();prepared=[];ids=set();bindings=set()
    for item in items:
        body,draft,context=_native_item(session,user_id,style_id,item,ledger)
        key=(body['source_image_sha256'],review_digest(body['source_bbox_norm']),body['image_sha256'])
        require(body['candidate_id'] not in ids and key not in bindings,'INVALID_REQUEST','duplicate native candidate/lineage');ids.add(body['candidate_id']);bindings.add(key);prepared.append((body,draft,context))
    job_id=uuid.uuid4().hex;root=job_root(user_id,job_id)
    package={'schema':'native-candidate-review-job/1','job_id':job_id,'nonce':uuid.uuid4().hex,'user_id':user_id,'style_id':style_id,'created_at':datetime.now(timezone.utc).isoformat(),'policy_sha256':policy['_sha256'],'authorization':policy['candidate_confirmation']['authorization'],'ledger_sha256':ledger.sha256,'application_version':code_version(),'items':[x[0] for x in prepared],'status':'AWAITING_REAL_AI_PIXEL_REVIEW','human_confirmation':False,'canonical_samples_created':False}
    package_sha=review_digest(package);blob=(json.dumps(package,ensure_ascii=False,indent=2)+'\n').encode()
    with storage.QUOTA_LOCK:
        storage.enforce_quota(user_id,len(blob)+sum(len(d)+len(c) for _,d,c in prepared))
        for body,draft,context in prepared:
            folder=root/body['candidate_id'];folder.mkdir(parents=True,exist_ok=False)
            (folder/'alpha.png').write_bytes(draft);(folder/'source-context.png').write_bytes(context)
        write_new(root/'package.json',package)
    session.add(CandidateReviewJob(job_id=job_id,user_id=user_id,style_id=style_id,package_json=json.dumps(package,ensure_ascii=False,allow_nan=False),package_sha256=package_sha,status='pending'))
    audit(session,action='candidate_review_job_create',actor='ai:delegated:'+policy['_sha256'],object_ref='candidate_review_jobs/'+job_id,result='awaiting_real_review')
    session.flush();return {'job_id':job_id,'package_sha256':package_sha,'package':package,'status':'AWAITING_REAL_AI_PIXEL_REVIEW','canonical_samples_created':False,'visible_P4_UI_integrated':False}

def _validate_current(session,job,package):
    policy=policy_for(job.user_id);require(policy['_sha256']==package['policy_sha256'] and policy['candidate_confirmation']['authorization']==package['authorization'],'REVIEW_JOB_STALE','delegation changed')
    require(package['application_version']==code_version(),'REVIEW_JOB_STALE','candidate review implementation changed')
    ledger=checked_ledger();require(ledger.sha256==package['ledger_sha256'],'REVIEW_JOB_STALE','source ledger changed')
    for old in package['items']:
        item={k:old[k] for k in ('candidate_id','source_asset_id','source_bbox_norm','draft_alpha_path')}
        now,_,_=_native_item(session,job.user_id,job.style_id,item,ledger)
        require(now==old,'REVIEW_JOB_STALE','candidate/source/admission version changed')
        folder=job_root(job.user_id,job.job_id)/old['candidate_id']
        require(sha(folder/'alpha.png')==old['image_sha256'] and sha(folder/'source-context.png')==old['source_context_sha256'],'REVIEW_JOB_STALE','displayed review pixels changed')

def reviewer_allowed(configured, reviewer, user_id, reviewer_task_id):
    if not isinstance(reviewer, dict):
        return False
    if reviewer in configured['reviewers']:
        return True
    return any(entry.get('user_id') == user_id
               and entry.get('reviewer_task_id') == reviewer_task_id
               and entry.get('reviewer') == reviewer
               for entry in configured.get('scoped_reviewers', []))


def _validate_bundle(bundle,job,package):
    require(isinstance(bundle,dict) and bundle.get('schema')=='native-candidate-review-bundle/1','REVIEW_EVIDENCE_INVALID','unsupported review bundle')
    for key,expected in {'job_id':job.job_id,'user_id':job.user_id,'style_id':job.style_id,'package_sha256':job.package_sha256,'nonce':package['nonce'],'policy_sha256':package['policy_sha256']}.items():
        require(bundle.get(key)==expected,'REVIEW_EVIDENCE_INVALID','review bundle belongs to another job/user/style/version: '+key)
    reviewer=bundle.get('reviewer');configured=json.loads((ROOT/'config/candidate-review-import-policy.json').read_text())
    provenance=bundle.get('provenance');require(isinstance(provenance,dict) and provenance.get('actual_pixels_reviewed') is True and provenance.get('actor_kind')=='ai','REVIEW_EVIDENCE_INVALID','actual delegated AI review provenance required')
    require(reviewer_allowed(configured, reviewer, job.user_id,
                            provenance.get('reviewer_task_id')),
            'REVIEW_EVIDENCE_INVALID','reviewer identity is not configured for this user/session')
    for key in ('evidence_id','reviewed_at','reviewer_task_id'):
        require(isinstance(provenance.get(key),str) and bool(provenance[key].strip()),'REVIEW_EVIDENCE_INVALID',key+' is required')
    try:stamp=datetime.fromisoformat(provenance['reviewed_at'].replace('Z','+00:00'))
    except ValueError:raise ApiError(409,'REVIEW_EVIDENCE_INVALID','review timestamp must be ISO8601')
    require(stamp.tzinfo is not None and datetime.fromisoformat(package['created_at'])<=stamp<=datetime.now(timezone.utc),'REVIEW_EVIDENCE_INVALID','review timestamp requires a valid timezone and cannot be future')
    source_text=bundle.get('source_review_original_utf8')
    require(isinstance(source_text,str) and len(source_text.encode('utf-8'))<=1_000_000 and review_digest_bytes(source_text.encode('utf-8'))==provenance.get('source_review_sha256'),'REVIEW_EVIDENCE_INVALID','exact original pixel-review artifact and SHA256 are required')
    source_review=strict_json(source_text)
    require(isinstance(source_review,dict) and source_review.get('schema')=='fresh-native-candidate-ai-visual-review/1' and source_review.get('review_actor')=='AI' and source_review.get('human_review_performed') is False and source_review.get('owner_confirmed') is False,'REVIEW_EVIDENCE_INVALID','original review must retain truthful AI provenance')
    require(source_review.get('reviewer')==reviewer and source_review.get('reviewer_task_id')==provenance['reviewer_task_id'] and source_review.get('evidence_id')==provenance['evidence_id'] and source_review.get('reviewed_at_utc')==provenance['reviewed_at'],'REVIEW_EVIDENCE_INVALID','reviewer/task/evidence/time must come from the actual original artifact, not wrapper metadata')
    for key,expected in (('job_id',job.job_id),('package_sha256',job.package_sha256),('nonce',package['nonce'])):
        require(source_review.get(key)==expected,'REVIEW_EVIDENCE_INVALID','actual review artifact does not bind this current job: '+key)
    original_rows=source_review.get('candidates');require(isinstance(original_rows,list) and all(isinstance(x,dict) for x in original_rows),'REVIEW_EVIDENCE_INVALID','original candidate dispositions missing')
    originals={x.get('candidate_id'):x for x in original_rows};require(len(originals)==len(original_rows),'REVIEW_EVIDENCE_INVALID','duplicate original candidate identities')
    responses=bundle.get('responses');require(isinstance(responses,dict) and set(responses)=={x['candidate_id'] for x in package['items']},'REVIEW_EVIDENCE_INVALID','every exact native candidate needs a disposition')
    for item in package['items']:
        r=responses[item['candidate_id']];require(isinstance(r,dict),'REVIEW_EVIDENCE_INVALID','response object required')
        for key in ('candidate_id','candidate_version_sha256','image_sha256','source_image_sha256','source_bbox_norm','source_context_sha256'):
            require(r.get(key)==item[key],'REVIEW_EVIDENCE_INVALID','response candidate binding differs: '+key)
        require(all(r.get(k)==v for k,v in reviewer.items()),'REVIEW_EVIDENCE_INVALID','response reviewer identity differs')
        original=originals.get(item['candidate_id']);require(isinstance(original,dict),'REVIEW_EVIDENCE_INVALID','original review does not cover this candidate')
        for old_key,current_key in (('alpha_sha256','image_sha256'),('source_sha256','source_image_sha256'),('native_box','source_bbox_norm'),('source_context_sha256','source_context_sha256')):
            require(original.get(old_key)==item[current_key],'REVIEW_EVIDENCE_INVALID','original inspected pixel binding differs: '+old_key)
        allowed={'AI_ACCEPT':{'AI_ACCEPT','AI_NEEDS_HUMAN','AI_REJECT'},'AI_NEEDS_HUMAN':{'AI_NEEDS_HUMAN','AI_REJECT'},'AI_REJECT':{'AI_REJECT'}}
        require(r.get('recommendation') in allowed.get(original.get('review_status'),set()),'REVIEW_EVIDENCE_INVALID','a prior rejection or uncertainty cannot be upgraded by the importer')
        if r.get('recommendation')=='AI_ACCEPT':
            require(r.get('label')==original.get('label'),'REVIEW_EVIDENCE_INVALID','accepted label differs from actual original review')
            confidence=original.get('confidence')
            require(isinstance(confidence,dict) and type(confidence.get('decision')) in (int,float) and type(confidence.get('label')) in (int,float) and type(r.get('confidence')) in (int,float) and all(math.isfinite(confidence[k]) and 0<=confidence[k]<=1 for k in ('decision','label')) and r['confidence']<=min(confidence['decision'],confidence['label']),'REVIEW_EVIDENCE_INVALID','import cannot inflate or invent original decision/label confidence')
        label=r.get('label');require(label is None or isinstance(label,str) and 1<=len(label)<=16,'REVIEW_EVIDENCE_INVALID','explicit reviewed label or null required')
        validate_candidate_response(r,{k:r[k] for k in ('image_sha256','source_image_sha256','source_bbox_norm','label')})
        require(r.get('touchup_decision') in ('NO_CHANGE_REQUIRED','NEEDS_EDIT','REJECT'),'REVIEW_EVIDENCE_INVALID','explicit touchup disposition required')
    return reviewer

def import_bundle(session,user_id,job_id,bundle_path,expected_sha256,expected_provider,expected_model):
    """Legacy operator entry for jobs without the visible request workflow."""
    from . import candidate_review_ingress as ingress
    ingress._reserve(session)
    get_job(session,user_id,job_id)
    require(ingress._row(session,user_id,job_id) is None,'REVIEW_INGRESS_SELECTION_REQUIRED',
            'This job has a visible review request; stage evidence then select it through the owned import operation')
    return _import_validated_bundle(session,user_id,job_id,bundle_path,expected_sha256,expected_provider,expected_model)


def _import_validated_bundle(session,user_id,job_id,bundle_path,expected_sha256,expected_provider,expected_model):
    """Internal import after a serialized legacy or staged-selection authority check."""
    require(isinstance(expected_sha256,str) and HASH_RE.fullmatch(expected_sha256),'REVIEW_EVIDENCE_INVALID','operator must supply the independently verified exact bundle hash')
    path=Path(bundle_path);native=native_path(path);require(native.is_file() and native.stat().st_size<=2_000_000 and sha(native)==expected_sha256,'REVIEW_EVIDENCE_INVALID','review bundle bytes/hash differ')
    job,package=get_job(session,user_id,job_id);require(job.status=='pending','REVIEW_JOB_REPLAY','review job already consumed')
    _validate_current(session,job,package);bundle=strict_json(native.read_text(encoding='utf-8'));reviewer=_validate_bundle(bundle,job,package)
    require(reviewer['provider']==expected_provider and reviewer['model_id']==expected_model,'REVIEW_EVIDENCE_INVALID','operator reviewer pin differs from bundle')
    require(job.imported_bundle_sha256 in (None,expected_sha256),'REVIEW_EVIDENCE_CONFLICT','first imported bundle is immutable; cancel this job and create a new review job for a different decision')
    if job.imported_bundle_sha256 is None:
        claimed=session.query(CandidateReviewJob).filter(CandidateReviewJob.job_id==job_id,CandidateReviewJob.user_id==user_id,CandidateReviewJob.status=='pending',CandidateReviewJob.imported_bundle_sha256.is_(None)).update({CandidateReviewJob.imported_bundle_sha256:expected_sha256},synchronize_session=False)
        if claimed!=1:
            session.refresh(job);require(job.imported_bundle_sha256==expected_sha256,'REVIEW_EVIDENCE_CONFLICT','another bundle was imported concurrently')
        job.imported_bundle_sha256=expected_sha256
    root=job_root(user_id,job_id);target=root/'trusted-evidence'/f'{expected_sha256}.json';raw=native.read_bytes();target_io=native_path(target)
    require(review_digest_bytes(raw)==expected_sha256,'REVIEW_EVIDENCE_INVALID','bundle changed during import')
    with storage.QUOTA_LOCK:
        storage.enforce_quota(user_id,len(raw));target_io.parent.mkdir(parents=True,exist_ok=True)
        if target_io.exists():require(target_io.read_bytes()==raw,'REVIEW_EVIDENCE_INVALID','immutable evidence conflict')
        else:
            with target_io.open('xb') as f:f.write(raw)
    audit(session,action='candidate_review_evidence_import',actor='operator:hash-pinned-ai-evidence',object_ref='candidate_review_jobs/'+job_id,result=expected_sha256)
    session.flush();return {'status':'IMPORTED_NOT_APPLIED','job_id':job_id,'bundle_sha256':expected_sha256,'reviewer':reviewer,'canonical_samples_created':False}

def apply_bundle(session,user_id,job_id,payload):
    require(isinstance(payload,dict) and set(payload)=={'bundle_sha256'} and isinstance(payload['bundle_sha256'],str) and HASH_RE.fullmatch(payload['bundle_sha256']),'INVALID_REQUEST','apply accepts only the already imported bundle SHA256')
    with LOCK:
        job,package=get_job(session,user_id,job_id);require(job.status=='pending','REVIEW_JOB_REPLAY','job has already been consumed; read its result')
        profile_for(session,user_id,job.style_id,approved=True);_validate_current(session,job,package)
        from .candidate_review_ingress import assert_apply_authority
        assert_apply_authority(session,job,package,payload['bundle_sha256'])
        require(job.imported_bundle_sha256==payload['bundle_sha256'],'REVIEW_EVIDENCE_MISSING','requested bundle is not this job authoritative imported evidence')
        path=job_root(user_id,job_id)/'trusted-evidence'/f'{payload["bundle_sha256"]}.json'
        path_io=native_path(path)
        require(path_io.is_file() and sha(path_io)==payload['bundle_sha256'],'REVIEW_EVIDENCE_MISSING','trusted job-specific evidence has not been imported')
        bundle=strict_json(path_io.read_text(encoding='utf-8'));_validate_bundle(bundle,job,package)
        prepared=[]
        for item in package['items']:
            response=bundle['responses'][item['candidate_id']];sid='ss_ai_'+review_digest({'job_id':job_id,'candidate_id':item['candidate_id']})[:32]
            context={'schema':'candidate-offline-evidence/1','job_id':job_id,'package_sha256':job.package_sha256,'bundle_sha256':payload['bundle_sha256'],'candidate_id':item['candidate_id'],'candidate_version_sha256':item['candidate_version_sha256'],'provenance':bundle['provenance'],'source_review_limitations':json.loads(bundle['source_review_original_utf8']).get('limitations',[])}
            asset=session.get(Asset,item['source_asset_id']);folder=job_root(user_id,job_id)/item['candidate_id']
            def bound_reviewer(request,response=response):
                require(all(request.get(k)==response.get(k) for k in ('image_sha256','source_image_sha256','source_bbox_norm','label')),'REVIEW_EVIDENCE_INVALID','canonical review request differs from inspected pixels');return dict(response)
            receipt=review_candidate_for_confirmation(user_id=user_id,style_id=job.style_id,sample_id=sid,image_path=folder/'alpha.png',source_image_path=source_path(asset,user_id),source_bbox_norm=item['source_bbox_norm'],label=response.get('label'),reviewer=bound_reviewer,review_context=context,reviewed_at=bundle['provenance']['reviewed_at'])
            from .service import _is_cjk_ideograph
            label=response.get('label');label_ok=isinstance(label,str) and len(label)==1 and _is_cjk_ideograph(ord(label))
            if not label_ok or response['touchup_decision']!='NO_CHANGE_REQUIRED':
                receipt.update(review_status='candidate',recommendation='AI_NEEDS_HUMAN' if response['recommendation']!='AI_REJECT' else 'AI_REJECT')
                receipt['isolation_reasons']=sorted(set(receipt['isolation_reasons']+(['NON_CJK_OR_UNRESOLVED_LABEL'] if not label_ok else [])+(['TOUCHUP_REQUIRED'] if response['touchup_decision']!='NO_CHANGE_REQUIRED' else [])))
                receipt['receipt_sha256']=review_digest({k:v for k,v in receipt.items() if k!='receipt_sha256'})
            # A separately reviewed revision may have identical normalized pixels
            # but a different source box. Preserve both immutable review versions;
            # the mandatory occurrence-selection gate chooses one for any dataset.
            prepared.append((item,sid,receipt,folder/'alpha.png'))
        _validate_current(session,job,package)
        claimed=session.query(CandidateReviewJob).filter(CandidateReviewJob.job_id==job_id,CandidateReviewJob.user_id==user_id,CandidateReviewJob.status=='pending',CandidateReviewJob.package_sha256==job.package_sha256).update({CandidateReviewJob.status:'applying'},synchronize_session=False)
        require(claimed==1,'REVIEW_JOB_REPLAY','job changed concurrently')
        results=[]
        for item,sid,receipt,pixels in prepared:
            reference=save_confirmation_receipt(receipt);result={'candidate_id':item['candidate_id'],'candidate_version_sha256':item['candidate_version_sha256'],'review_status':receipt['review_status'],'recommendation':receipt['recommendation'],'receipt_sha256':receipt['receipt_sha256'],'isolation_reasons':receipt['isolation_reasons'],'canonical_sample_id':None,'owner_confirmed':False,'reviewed_label':receipt['label'],'reason':receipt['reason'],'reviewer':receipt.get('reviewer'),'source_review_sha256':bundle['provenance']['source_review_sha256'],'touchup_decision':bundle['responses'][item['candidate_id']]['touchup_decision']}
            if receipt['review_status']=='ai_confirmed_authorized':
                raw=pixels.read_bytes();require(review_digest_bytes(raw)==item['image_sha256'],'SOURCE_HASH_MISMATCH','reviewed alpha changed')
                dest=storage.ensure_within_root(storage.storage_root()/str(user_id)/'training_images'/(sid+'.png'))
                with storage.QUOTA_LOCK:
                    storage.enforce_quota(user_id,len(raw));dest.parent.mkdir(parents=True,exist_ok=True)
                    if dest.exists():require(dest.read_bytes()==raw,'SAMPLE_PROVENANCE_INVALID','existing glyph conflict')
                    else:
                        with dest.open('xb') as f:f.write(raw)
                rel=dest.relative_to(storage.storage_root()).as_posix();at=receipt['reviewed_at'];label=receipt['label']
                image=TrainingImage(user_id=user_id,file_name=sid+'.png',width=128,height=128,size_bytes=len(raw),status='uploaded',relative_path=rel,sha256=item['image_sha256'],uploaded_at=at);session.add(image);session.flush()
                ss=StyleSample(style_id=job.style_id,source_training_image_id=image.image_id,text_label=label,relative_path=rel,sha256=image.sha256,uploaded_at=at);session.add(ss);session.flush()
                sample=TrainingSample(sample_id=sid,user_id=user_id,style_id=str(job.style_id),source_asset_id=item['source_asset_id'],glyph_path=rel,text_label=label,char_unicode=f'U+{ord(label):04X}',text_region_bbox_norm=json.dumps(item['source_bbox_norm']),output_size=128,extracted_by='p4_native_delegated_ai_noop_review',touchup_confirmed=True,draft_alpha_path=item['draft_alpha_path'],confirmed_alpha_path=rel,created_at=at,source_training_image_id=image.image_id,style_sample_id=ss.sample_id,source_asset_sha256=item['source_image_sha256'],source_asset_width=item['source_width'],source_asset_height=item['source_height'],source_family_id=item['source_family_id'],source_training_image_sha256=image.sha256,draft_alpha_sha256=item['image_sha256'],final_alpha_sha256=image.sha256,touchup_edit_count=0,touchup_confirmed_at=at,review_status='ai_confirmed_authorized',reviewed_by=receipt['actor'],reviewed_at=at,review_reason=reference,provenance_version='p4-s02/v1',owner_confirmed_by=None,owner_confirmed_at=None)
                session.add(sample);result['canonical_sample_id']=sid;result['style_sample_id']=ss.sample_id
            audit(session,action='native_candidate_ai_accept' if result['canonical_sample_id'] else 'native_candidate_ai_isolate',actor=receipt['actor'],object_ref='candidate_review_jobs/'+job_id+'/'+item['candidate_id'],result=result['review_status']);results.append(result)
        result={'job_id':job_id,'status':'APPLIED','bundle_sha256':payload['bundle_sha256'],'items':results,'accepted':sum(x['canonical_sample_id'] is not None for x in results),'isolated':sum(x['canonical_sample_id'] is None for x in results),'model_trained':False,'owner_confirmations_created':False,'visible_P4_UI_integrated':False}
        job.status='applied';job.result_json=json.dumps(result,ensure_ascii=False,allow_nan=False);session.flush();return result


def cancel_job(session,user_id,job_id):
    job,_=get_job(session,user_id,job_id)
    require(job.status=='pending','REVIEW_JOB_REPLAY','only pending jobs may be cancelled')
    claimed=session.query(CandidateReviewJob).filter(CandidateReviewJob.job_id==job_id,CandidateReviewJob.user_id==user_id,CandidateReviewJob.status=='pending').update({CandidateReviewJob.status:'cancelled'},synchronize_session=False)
    require(claimed==1,'REVIEW_JOB_REPLAY','job changed concurrently');job.status='cancelled'
    audit(session,action='candidate_review_job_cancel',actor='authenticated_user:'+str(user_id),object_ref='candidate_review_jobs/'+job_id,result='cancelled_evidence_preserved');session.flush()
    return {'job_id':job_id,'status':'CANCELLED','evidence_preserved':True,'canonical_samples_created':False}


def list_jobs(session,user_id,style_id,limit=50,after_job_id=None):
    profile_for(session,user_id,style_id)
    require(type(limit) is int and 1<=limit<=100,'INVALID_REQUEST','limit must be1..100')
    require(after_job_id is None or isinstance(after_job_id,str) and JOB_RE.fullmatch(after_job_id),'INVALID_REQUEST','invalid opaque job cursor')
    query=session.query(CandidateReviewJob).filter(CandidateReviewJob.user_id==user_id,CandidateReviewJob.style_id==style_id)
    if after_job_id:query=query.filter(CandidateReviewJob.job_id>after_job_id)
    records=query.order_by(CandidateReviewJob.job_id).limit(limit+1).all();items=[];version=code_version()
    for record in records[:limit]:
        try:
            row,package=get_job(session,user_id,record.job_id)
            result=json.loads(row.result_json) if row.result_json else None
            require(result is None or isinstance(result,dict),'REVIEW_JOB_CHANGED','malformed job result')
            items.append({'job_id':row.job_id,'style_id':row.style_id,'status':row.status,'created_at':package['created_at'],'package_sha256':row.package_sha256,'imported_bundle_sha256':row.imported_bundle_sha256,'code_version_matches':package.get('application_version')==version,'result_summary':{k:result[k] for k in ('accepted','isolated','status') if k in result} if result else None})
        except (ApiError,OSError,ValueError,TypeError,KeyError,AttributeError) as exc:
            items.append({'job_id':record.job_id,'style_id':record.style_id,'status':'blocked','reason':str(exc),'code_version_matches':False,'imported_bundle_sha256':None,'result_summary':None})
    return {'items':items,'next_after_job_id':records[limit-1].job_id if len(records)>limit else None,'ordering':'opaque_job_id_ascending','pixel_prerequisites_rechecked_on_apply':True}


def job_view(session,user_id,job_id):
    job,package=get_job(session,user_id,job_id)
    result=strict_json(job.result_json) if job.result_json else None
    require(result is None or isinstance(result,dict),'REVIEW_JOB_CHANGED','malformed stored job result')
    if job.status=='applied':
        require(isinstance(result,dict) and result.get('job_id')==job_id and result.get('status')=='APPLIED' and result.get('bundle_sha256')==job.imported_bundle_sha256 and isinstance(result.get('items'),list) and all(isinstance(x,dict) for x in result['items']),'REVIEW_JOB_CHANGED','applied job result is incomplete or changed')
    from .candidate_review_export import capabilities
    return {'job_id':job_id,'status':job.status,'package_sha256':job.package_sha256,'package':package,'code_version_matches':package.get('application_version')==code_version(),'imported_bundle_sha256':job.imported_bundle_sha256,'result':result,'capabilities':capabilities()}
