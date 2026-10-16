"""Explicit authenticated pixel review. Append-only; never grants model quality."""
import json
from datetime import datetime, timezone
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.core import storage
from app.core.audit import audit
from app.core.errors import ApiError
from app.domains.assets_upload.models import Asset
from app.domains.styles.models import StyleProfile
from .models import TrainingSample
from .intake_models import HumanGlyphReview
from . import source_admission
from .source_admission import require_training_source
from engine.style.dataset.schema import DatasetError, review_digest
from engine.style.visual_review.pipeline import inspect_glyph

def checked_ledger():
    # Resolve the current ledger authority on every request, including reuse.
    return source_admission.checked_ledger()

CHECKS={'source_label_matches','strokes_complete','alpha_usable','suspicions_checked'}

def profile_for(session,user_id,style_id):
    profile=session.query(StyleProfile).filter(StyleProfile.user_id==user_id,StyleProfile.style_id==style_id).first()
    if profile is None:raise ApiError(404,'STYLE_NOT_FOUND','Style not found')
    if profile.status!='approved' or profile.source_type!='HANDWRITING_ASSET' or not profile.enabled:raise ApiError(409,'STYLE_NOT_APPROVED','enabled approved handwriting profile required')
    return profile

def read_source(session,user_id,asset_id,style_id):
    profile_for(session,user_id,style_id)
    asset=session.query(Asset).filter(Asset.asset_id==asset_id,Asset.user_id==str(user_id),Asset.enabled==1,Asset.kind.in_(('cos','sample_source'))).first()
    if asset is None:raise ApiError(404,'ASSET_NOT_FOUND','Source asset not found')
    receipt=None;blocked=None
    try:receipt=require_training_source(session,asset,user_id,style_id)
    except (DatasetError,OSError,ValueError) as exc:blocked=str(exc)
    return {'asset_id':asset_id,'style_id':style_id,'source_sha256':asset.sha256,'source_status':asset.status,
            'history_status':receipt.get('history_status') if receipt else 'UNKNOWN','receipt':receipt,
            'status':'ADMITTED' if receipt else 'NOT_ADMITTED','blocker':blocked,'read_only':True}

def latest(session,user_id,style_id,sample_id):
    record=session.query(HumanGlyphReview).filter(HumanGlyphReview.user_id==user_id,HumanGlyphReview.style_id==style_id,HumanGlyphReview.sample_id==sample_id).order_by(HumanGlyphReview.review_id.desc()).first()
    if record is None:return None
    receipt=json.loads(record.receipt_json)
    if receipt.get('receipt_sha256')!=record.receipt_sha256 or review_digest({k:v for k,v in receipt.items() if k!='receipt_sha256'})!=record.receipt_sha256:
        raise DatasetError('E_HUMAN_REVIEW_HASH','human review receipt changed')
    return receipt

def binding_for(session,sample,profile,ledger=None):
    if sample.review_status!='owner_confirmed' or sample.owner_confirmed_by!=str(profile.user_id):
        raise DatasetError('E_HUMAN_CONFIRMATION_REQUIRED','explicit current owner character confirmation required')
    from scripts.export_personal_style_dataset import _row_for_confirmed_sample
    row,provenance=_row_for_confirmed_sample(session,sample,profile,ledger or checked_ledger(),storage.storage_root())
    inspection=inspect_glyph(row['alpha_path'],(128,128))
    binding={'user_id':profile.user_id,'style_id':profile.style_id,'sample_id':sample.sample_id,
        'provenance_sha256':provenance['provenance_sha256'],'source_sha256':row['parent_source_hash'],
        'image_sha256':row['final_alpha_sha256'],'label':row['label'],'source_bbox':row['source_bbox'],
        'source_admission_sha256':provenance['source_admission']['receipt_sha256'],'structural_checks':inspection}
    return binding,review_digest(binding)

def verified_receipt(session,sample,profile,ledger=None):
    receipt=latest(session,profile.user_id,profile.style_id,sample.sample_id)
    if receipt is None:return None
    binding,digest=binding_for(session,sample,profile,ledger)
    expected_status={'accept':'human_visual_accepted','reject':'human_visual_rejected'}.get(receipt.get('decision'))
    if (receipt.get('schema')!='human-input-visual-review/1' or receipt.get('candidate_id')!=sample.sample_id
            or receipt.get('image_sha256')!=binding['image_sha256'] or expected_status is None
            or receipt.get('review_status')!=expected_status or receipt.get('model_quality_approved') is not False
            or receipt.get('binding_sha256')!=digest or receipt.get('binding')!=binding or receipt.get('actor')!='user:'+str(profile.user_id) or receipt.get('actor_kind')!='authenticated_user' or receipt.get('owner_confirmation')!='UNCHANGED'):
        raise DatasetError('E_HUMAN_REVIEW_STALE','human pixel review no longer matches current source, label or glyph')
    if set(receipt.get('checks',{}))!=CHECKS or (receipt.get('decision')=='accept' and (any(receipt['checks'][k] is not True for k in CHECKS) or binding['structural_checks']['clear_errors'])):
        raise DatasetError('E_HUMAN_REVIEW_INVALID','human acceptance lacks required checks or has structural errors')
    return receipt

def list_reviews(session,user_id,style_id):
    profile=profile_for(session,user_id,style_id);items=[]
    ledger=checked_ledger()
    samples=session.query(TrainingSample).filter(TrainingSample.user_id==user_id,TrainingSample.style_id==str(style_id),TrainingSample.review_status=='owner_confirmed').order_by(TrainingSample.sample_id).all()
    for sample in samples:
        item={'sample_id':sample.sample_id,'label':sample.text_label,'source_asset_id':sample.source_asset_id,
              'source_image_url':f'/api/v1/assets/{sample.source_asset_id}/file',
              'glyph_image_url':f'/api/v1/training-sets/images/{sample.source_training_image_id}/file',
              'source_bbox_norm':json.loads(sample.text_region_bbox_norm),'binding_sha256':None,'receipt':None,'blocker':None}
        try:
            binding,digest=binding_for(session,sample,profile,ledger)
            item.update(binding_sha256=digest,structural_checks=binding['structural_checks'],source_sha256=binding['source_sha256'],image_sha256=binding['image_sha256'])
            item['receipt']=verified_receipt(session,sample,profile,ledger)
        except (DatasetError,OSError,ValueError,TypeError,KeyError) as exc:item['blocker']=str(exc)
        items.append(item)
    return {'style_id':style_id,'items':items,'read_only':True,'ai_provider_required':False,'model_quality_approved':False}

def submit_review(session,user_id,sample_id,payload):
    fields={'style_id','expected_binding_sha256','decision','checks','reason'}
    if not isinstance(payload,dict) or set(payload)!=fields or type(payload['style_id']) is not int or payload['decision'] not in ('accept','reject') or not isinstance(payload['reason'],str) or not 1<=len(payload['reason'].strip())<=1000 or not isinstance(payload['checks'],dict) or set(payload['checks'])!=CHECKS or any(type(v) is not bool for v in payload['checks'].values()):
        raise ApiError(400,'INVALID_REQUEST','exact explicit human review fields and bounded reason required')
    # Same reservation as freezing prevents a concurrent review/approval drift.
    from .service import _reserve_explicit_snapshot_intake
    _reserve_explicit_snapshot_intake(session)
    profile=profile_for(session,user_id,payload['style_id'])
    sample=session.query(TrainingSample).filter(TrainingSample.user_id==user_id,TrainingSample.style_id==str(profile.style_id),TrainingSample.sample_id==sample_id).first()
    if sample is None:raise ApiError(404,'SAMPLE_NOT_FOUND','Sample not found')
    try:binding,digest=binding_for(session,sample,profile)
    except (DatasetError,OSError,ValueError) as exc:raise ApiError(409,'HUMAN_REVIEW_BLOCKED',str(exc)) from exc
    if payload['expected_binding_sha256']!=digest:raise ApiError(409,'HUMAN_REVIEW_STALE','pixels or authority changed; reload and inspect current images')
    if payload['decision']=='accept' and (not all(payload['checks'].values()) or binding['structural_checks']['clear_errors']):raise ApiError(409,'HUMAN_REVIEW_INCOMPLETE','all visual checks required; structural failures cannot be waived')
    old=latest(session,user_id,profile.style_id,sample_id)
    if old and old['binding_sha256']==digest and old['decision']==payload['decision'] and old['checks']==payload['checks'] and old['reason']==payload['reason']:
        return {**old,'reused':True}
    receipt={'schema':'human-input-visual-review/1','candidate_id':sample_id,'binding':binding,'binding_sha256':digest,
        'image_sha256':binding['image_sha256'],'decision':payload['decision'],'checks':payload['checks'],'reason':payload['reason'],
        'actor_kind':'authenticated_user','actor':'user:'+str(user_id),'created_at':datetime.now(timezone.utc).isoformat(),
        'previous_receipt_sha256':old['receipt_sha256'] if old else None,'owner_confirmation':'UNCHANGED','model_quality_approved':False,
        'review_status':'human_visual_accepted' if payload['decision']=='accept' else 'human_visual_rejected',
        'recommendation':'HUMAN_ACCEPT' if payload['decision']=='accept' else 'HUMAN_REJECT'}
    receipt['receipt_sha256']=review_digest(receipt)
    session.execute(sqlite_insert(HumanGlyphReview).values(receipt_sha256=receipt['receipt_sha256'],user_id=user_id,style_id=profile.style_id,sample_id=sample_id,receipt_json=json.dumps(receipt,ensure_ascii=False,allow_nan=False)).on_conflict_do_nothing(index_elements=['receipt_sha256']))
    audit(session,action='human_input_visual_review',actor=receipt['actor'],object_ref='training_samples/'+sample_id,result=payload['decision'])
    session.flush();return receipt

def assert_frozen_reviews_current(session,profile,samples,metadata):
    audit_body=metadata.get('dataset_visual_audit') or {}
    reviews=audit_body.get('reviews')
    if (audit_body.get('schema')!='dataset-visual-audit/2' or audit_body.get('status')!='PASS'
            or audit_body.get('may_freeze_new_snapshot') is not True or not isinstance(reviews,list)
            or len(reviews)!=len(samples) or {r.get('candidate_id') for r in reviews}!=set(samples)):
        raise DatasetError('E_SNAPSHOT_VISUAL_REVIEW','snapshot lacks complete accepted input visual audit')
    for frozen in reviews:
        sample=samples[frozen['candidate_id']]
        from scripts.export_personal_style_dataset import _row_for_confirmed_sample
        row,_=_row_for_confirmed_sample(session,sample,profile,checked_ledger(),storage.storage_root())
        if frozen.get('image_sha256')!=row['final_alpha_sha256'] or frozen.get('review_status') not in ('human_visual_accepted','ai_confirmed_authorized'):
            raise DatasetError('E_SNAPSHOT_VISUAL_REVIEW','frozen input audit does not bind accepted current glyph')
        if frozen.get('schema')!='human-input-visual-review/1':continue
        current=verified_receipt(session,sample,profile)
        if current is None or current.get('decision')!='accept' or current.get('receipt_sha256')!=frozen.get('receipt_sha256'):
            raise DatasetError('E_HUMAN_REVIEW_STALE','frozen human review revoked or replaced; old snapshot remains immutable')
