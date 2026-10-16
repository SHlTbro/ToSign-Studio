"""Explicit source admission. UNKNOWN stays UNKNOWN; R04 prohibitions stay hard."""
import json
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import case, or_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.core import storage
from app.core.audit import audit
from app.core.errors import ApiError
from app.domains.assets_upload.models import Asset
from app.domains.styles.models import StyleProfile
from .intake_models import SourceAdmission
from engine.style.dataset.ledger import LedgerIndex, resolve_active_ledger_path
from engine.style.dataset.schema import DatasetError, review_digest
from engine.style.visual_review.pipeline import sha
from engine.style.visual_review.policy import candidate_delegation

ROOT=Path(__file__).resolve().parents[3]

def checked_ledger():
    ledger=LedgerIndex.load(resolve_active_ledger_path(ROOT));ledger.assert_r04_integrity(ROOT);return ledger

def assert_source_permitted(ledger,source_sha):
    component={source_sha}
    while True:
        expanded=component | set().union(*(set(members) for members in ledger.families.values() if component & set(members)))
        if expanded==component:break
        component=expanded
    if component & (ledger.forbidden_hashes | ledger.unknown_hashes):
        raise DatasetError('E_FORBIDDEN_PARENT','source or related family is forbidden/unknown in R04; an upload declaration cannot override it')

def source_path(asset,user_id):
    rel=asset.relative_path
    if isinstance(rel,str):rel=rel.replace('\\','/')
    if not isinstance(rel,str) or rel.startswith('/') or ':' in rel or any(p in ('','.','..') for p in rel.split('/')) or rel.split('/')[:2]!=[str(user_id),'assets']:
        raise DatasetError('E_SOURCE_PATH','source must be in the authenticated owner asset pool')
    return storage.ensure_within_root(storage.storage_root()/rel)

def admission_key(user_id,style_id,asset):
    return review_digest({'user_id':user_id,'style_id':style_id,'asset_id':asset.asset_id,'source_sha256':asset.sha256})

def admit_source(session,user_id,asset_id,payload):
    required={'style_id','source_sha256','origin','history_status','allow_unknown_history','reason','actor_kind'}
    human=isinstance(payload,dict) and payload.get('actor_kind')=='authenticated_user'
    if human:required |= {'rights_confirmed','personal_training_confirmed'}
    if not isinstance(payload,dict) or set(payload)!=required or type(payload.get('style_id')) is not int or payload['style_id']<1:
        raise ApiError(400,'INVALID_REQUEST','explicit source admission fields required')
    if payload['origin']!='user_supplied_attachment' or payload['history_status']!='UNKNOWN' or payload['allow_unknown_history'] is not True or payload['actor_kind'] not in ('delegated_ai','authenticated_user') or not isinstance(payload['reason'],str) or not 1<=len(payload['reason'].strip())<=1000 or (human and (payload['rights_confirmed'] is not True or payload['personal_training_confirmed'] is not True)):
        raise ApiError(400,'INVALID_REQUEST','declare user-supplied origin, retained UNKNOWN history and explicit bounded delegated-AI exception')
    policy=None if human else candidate_delegation(user_id)
    if not human and not policy:raise ApiError(403,'AI_CONFIRMATION_NOT_AUTHORIZED','current scoped delegation required')
    profile=session.query(StyleProfile).filter(StyleProfile.style_id==payload['style_id'],StyleProfile.user_id==user_id).first()
    if profile is None:raise ApiError(404,'STYLE_NOT_FOUND','Style not found')
    if profile.source_type!='HANDWRITING_ASSET':raise ApiError(409,'STYLE_IDENTITY_UNKNOWN','source admission is scoped to a handwriting profile')
    if human and profile.status!='approved':raise ApiError(409,'STYLE_NOT_APPROVED','human source declaration requires an approved handwriting profile')
    asset=session.query(Asset).filter(Asset.asset_id==asset_id,Asset.user_id==str(user_id),Asset.enabled==1).first()
    if asset is None or asset.kind not in ('cos','sample_source'):raise ApiError(404,'ASSET_NOT_FOUND','Source asset not found')
    if payload['source_sha256']!=asset.sha256:raise ApiError(409,'SOURCE_HASH_MISMATCH','source version changed')
    if human and (asset.status!='approved' or not asset.approved_by or not asset.approved_at):raise ApiError(409,'SOURCE_NOT_APPROVED','administrator source approval is required; a user declaration cannot grant it')
    try:
        ledger=checked_ledger();assert_source_permitted(ledger,asset.sha256)
        p=source_path(asset,user_id)
        if not p.is_file() or sha(p)!=asset.sha256:raise DatasetError('E_SOURCE_HASH','source bytes changed')
    except (OSError,ValueError,DatasetError) as exc:raise ApiError(409,'SOURCE_ADMISSION_BLOCKED',str(exc)) from exc
    ident=admission_key(user_id,profile.style_id,asset);old=session.get(SourceAdmission,ident)
    if old:
        receipt=require_training_source(session,asset,user_id,profile.style_id,ledger)
        if receipt['declaration']!=payload:raise ApiError(409,'SOURCE_ADMISSION_CONFLICT','immutable admission already exists with a different declaration')
        return receipt
    if asset.status not in ('pending','approved'):raise ApiError(409,'SOURCE_ADMISSION_BLOCKED','rejected source cannot be silently readmitted')
    at=datetime.now(timezone.utc).isoformat();actor='user:'+str(user_id) if human else 'ai:delegated:'+policy['_sha256']
    if session.get_bind().dialect.name!='sqlite':raise ApiError(409,'SOURCE_ADMISSION_BLOCKED','transaction-safe source admission currently requires SQLite')
    missing=or_(Asset.status=='pending',Asset.approved_by.is_(None),Asset.approved_at.is_(None),Asset.approved_by=='',Asset.approved_at=='')
    if human:
        # Reserve the already-approved version without changing its admin stamp.
        claimed=session.query(Asset).filter(Asset.asset_id==asset_id,Asset.user_id==str(user_id),Asset.enabled==1,Asset.sha256==payload['source_sha256'],Asset.status=='approved',Asset.approved_by==asset.approved_by,Asset.approved_at==asset.approved_at).update({Asset.status:'approved'},synchronize_session=False)
    else:
        claimed=session.query(Asset).filter(Asset.asset_id==asset_id,Asset.user_id==str(user_id),Asset.enabled==1,Asset.sha256==payload['source_sha256'],Asset.kind==asset.kind,Asset.width==asset.width,Asset.height==asset.height,Asset.status.in_(('pending','approved'))).update({Asset.status:'approved',Asset.approved_by:case((missing,actor),else_=Asset.approved_by),Asset.approved_at:case((missing,at),else_=Asset.approved_at)},synchronize_session=False)
    if claimed!=1:raise ApiError(409,'SOURCE_ADMISSION_CHANGED','source was rejected, disabled or changed before admission')
    # Refresh the winning shared approval stamp. Another style's explicit
    # admission must never overwrite it from a stale pending ORM object.
    session.refresh(asset)
    body={'schema':'personal-source-admission/1','admission_id':ident,'user_id':user_id,'style_id':profile.style_id,'asset_id':asset.asset_id,'source_sha256':asset.sha256,'source_kind':asset.kind,'source_width':asset.width,'source_height':asset.height,'origin':'user_supplied_attachment','history_status':'UNKNOWN','parent_origin':'user_upload_confirmed','explicit_split_required':True,'unknown_history_exception':'EXPLICIT_CURRENT_UPLOAD_ONLY_NOT_NEVER_USED','actor_kind':'delegated_ai','actor':actor,'admitted_at':at,'asset_approval_actor':asset.approved_by,'asset_approval_at':asset.approved_at,'policy_sha256':policy['_sha256'] if policy else None,'authorization':policy['candidate_confirmation']['authorization'] if policy else 'EXPLICIT_AUTHENTICATED_CURRENT_SOURCE_USE','ledger_sha256':ledger.sha256,'declaration':payload,'does_not_approve_glyphs':True}
    if human:
        body.update(actor_kind='authenticated_user',policy_sha256=None,authorization='EXPLICIT_AUTHENTICATED_CURRENT_SOURCE_USE')
    body['receipt_sha256']=review_digest(body)
    inserted=session.execute(sqlite_insert(SourceAdmission).values(admission_id=ident,user_id=user_id,style_id=profile.style_id,asset_id=asset.asset_id,receipt_json=json.dumps(body,ensure_ascii=False,allow_nan=False)).on_conflict_do_nothing(index_elements=['admission_id'])).rowcount
    if not inserted:
        existing=require_training_source(session,asset,user_id,profile.style_id,ledger)
        if existing['declaration']!=payload:raise ApiError(409,'SOURCE_ADMISSION_CONFLICT','a different immutable admission won concurrently')
        return existing
    audit(session,action='personal_source_admit',actor=actor,object_ref='assets/'+asset.asset_id,result='explicit_unknown_history_admission')
    session.flush();return body

def require_training_source(session,asset,user_id,style_id,ledger=None):
    """Return verified authority, never infer user provenance from missing fields."""
    if asset is None or str(asset.user_id)!=str(user_id) or not asset.enabled or asset.kind not in ('cos','sample_source') or asset.status!='approved':
        raise DatasetError('E_SOURCE_NOT_APPROVED','source is unavailable or has not been explicitly admitted/approved')
    ledger=ledger or checked_ledger();assert_source_permitted(ledger,asset.sha256)
    record=session.get(SourceAdmission,admission_key(user_id,style_id,asset))
    if record is None:
        status=ledger.status_of(asset.sha256)
        if asset.kind=='cos' and status and status.get('training_allowed') is True and asset.approved_by and asset.approved_at:
            result={'schema':'approved-ledger-source/1','admission_id':None,'user_id':user_id,'style_id':style_id,'asset_id':asset.asset_id,'source_sha256':asset.sha256,'source_kind':asset.kind,'history_status':status.get('history_status'),'ledger_sha256':ledger.sha256,'approval_actor':asset.approved_by,'approved_at':asset.approved_at,'explicit_split_required':False}
            result['receipt_sha256']=review_digest(result);return result
        raise DatasetError('E_SOURCE_ADMISSION_REQUIRED','unlisted source history remains UNKNOWN; explicit source admission required')
    receipt=json.loads(record.receipt_json);digest=review_digest({k:v for k,v in receipt.items() if k!='receipt_sha256'})
    expected={'admission_id':record.admission_id,'user_id':user_id,'style_id':style_id,'asset_id':asset.asset_id,'source_sha256':asset.sha256,'source_kind':asset.kind,'source_width':asset.width,'source_height':asset.height}
    if receipt.get('schema')!='personal-source-admission/1' or receipt.get('receipt_sha256')!=digest or any(receipt.get(k)!=v for k,v in expected.items()) or record.user_id!=user_id or record.style_id!=style_id or record.asset_id!=asset.asset_id:
        raise DatasetError('E_SOURCE_ADMISSION_CHANGED','source admission binding changed')
    human=receipt.get('actor_kind')=='authenticated_user'
    policy=None if human else candidate_delegation(user_id)
    if receipt.get('ledger_sha256')!=ledger.sha256:
        raise DatasetError('E_SOURCE_ADMISSION_AUTHORITY','active ledger changed; admission needs fresh authority')
    if human:
        if receipt.get('policy_sha256') is not None or receipt.get('authorization')!='EXPLICIT_AUTHENTICATED_CURRENT_SOURCE_USE' or receipt.get('actor')!='user:'+str(user_id):
            raise DatasetError('E_SOURCE_ADMISSION_AUTHORITY','human source authority changed')
    elif not policy or receipt.get('policy_sha256')!=policy['_sha256'] or receipt.get('authorization')!=policy['candidate_confirmation']['authorization'] or receipt.get('actor')!='ai:delegated:'+policy['_sha256']:
        raise DatasetError('E_SOURCE_ADMISSION_AUTHORITY','AI admission needs current scoped delegation')
    declaration=receipt.get('declaration')
    required={'style_id','source_sha256','origin','history_status','allow_unknown_history','reason','actor_kind'}
    if human:required |= {'rights_confirmed','personal_training_confirmed'}
    actor_kind='authenticated_user' if human else 'delegated_ai'
    if not isinstance(declaration,dict) or set(declaration)!=required or declaration.get('style_id')!=style_id or declaration.get('source_sha256')!=asset.sha256 or declaration.get('origin')!='user_supplied_attachment' or declaration.get('history_status')!='UNKNOWN' or declaration.get('allow_unknown_history') is not True or declaration.get('actor_kind')!=actor_kind or not isinstance(declaration.get('reason'),str) or not 1<=len(declaration['reason'].strip())<=1000:
        raise DatasetError('E_SOURCE_ADMISSION_CHANGED','explicit source declaration changed')
    if human and (declaration.get('rights_confirmed') is not True or declaration.get('personal_training_confirmed') is not True):
        raise DatasetError('E_SOURCE_ADMISSION_CHANGED','explicit human use confirmation missing')
    if receipt.get('parent_origin')!='user_upload_confirmed' or receipt.get('explicit_split_required') is not True or receipt.get('origin')!='user_supplied_attachment' or receipt.get('unknown_history_exception')!='EXPLICIT_CURRENT_UPLOAD_ONLY_NOT_NEVER_USED' or receipt.get('does_not_approve_glyphs') is not True:
        raise DatasetError('E_SOURCE_ADMISSION_CHANGED','source admission semantics changed')
    if receipt.get('history_status')!='UNKNOWN' or receipt.get('actor_kind')!=actor_kind or not receipt.get('asset_approval_actor') or not receipt.get('asset_approval_at') or asset.approved_by!=receipt.get('asset_approval_actor') or asset.approved_at!=receipt.get('asset_approval_at'):
        raise DatasetError('E_SOURCE_ADMISSION_CHANGED','history/actor/admin approval drift')
    path=source_path(asset,user_id)
    if not path.is_file() or sha(path)!=asset.sha256:raise DatasetError('E_SOURCE_HASH','admitted source bytes changed')
    return receipt
