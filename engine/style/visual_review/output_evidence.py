"""Authenticated owner-output evidence; no model approval or blind vote.

ZIP inventory and producer MAC are recomputed. Imported examples/ok flags never
create target state. The target is an owned current render item on this server.
"""
from pathlib import Path,PurePosixPath
import io,json,re,zipfile,hashlib,hmac,stat
from . import owner_direct as D
SCHEMA='owner-output-evidence-package/1'
PRODUCER='render-owner-output-binding/1'
V3_MODEL='017881442b7f8b909f7d8d8c4c5e7ad1893d0caa3c3267ae3c793a9a96175f81'
CAP=16*2**20

def pin(value):
    D.require(isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None,'full lowercase SHA256 required');return value

def safe_name(value):
    D.require(isinstance(value,str) and value and '\\' not in value and ':' not in value and '\x00' not in value,'unsafe package path')
    p=PurePosixPath(value)
    D.require(not p.is_absolute() and all(x not in ('','.','..') for x in value.split('/')),'unsafe package path')
    D.require(not any('EXAMPLE' in x.upper() for x in p.parts),'examples are not evidence');return value

def unique_object(pairs):
    value={}
    for key,item in pairs:
        D.require(key not in value,'duplicate JSON field rejected');value[key]=item
    return value

def unpack(raw,expected_sha256):
    D.require(isinstance(raw,bytes) and 0<len(raw)<=CAP,'evidence ZIP exceeds limit')
    D.require(hashlib.sha256(raw).hexdigest()==pin(expected_sha256),'evidence ZIP checksum differs')
    files={};total=0
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        infos=z.infolist();D.require(1<=len(infos)<=64,'bounded evidence inventory required')
        folded=set()
        for info in infos:
            name=safe_name(info.filename)
            D.require(not info.is_dir() and stat.S_IFMT(info.external_attr>>16) in (0,stat.S_IFREG),'regular evidence files only')
            D.require(name.casefold() not in folded and not info.flag_bits&1,'duplicate or encrypted evidence file');folded.add(name.casefold())
            total+=info.file_size
            D.require(total<=32*2**20 and info.file_size<=CAP and info.file_size<=max(1,info.compress_size)*100,'evidence expansion exceeds limit')
            D.require(PurePosixPath(name).suffix.lower() in ('.json','.png'),'evidence cannot install models, fonts, or code')
            files[name]=z.read(info)
    D.require('EVIDENCE-MANIFEST.json' in files,'actual evidence manifest required; examples rejected')
    D.require(len(files['EVIDENCE-MANIFEST.json'])<=2**20,'manifest oversized')
    m=json.loads(files['EVIDENCE-MANIFEST.json'],object_pairs_hook=unique_object)
    D.require(isinstance(m,dict) and set(m)=={'schema','profile_id','model_sha256','files','reference_image','expected_target'},'exact evidence manifest fields required')
    D.require(m['schema']==SCHEMA and isinstance(m['profile_id'],str) and 1<=len(m['profile_id'])<=80,'unsupported evidence schema')
    pin(m['model_sha256'])
    if m['profile_id']=='accepted-v3-20261005':D.require(m['model_sha256']==V3_MODEL,'v3 acceptance belongs exclusively to original017 model')
    inventory=m['files'];D.require(isinstance(inventory,dict) and set(inventory)==set(files)-{'EVIDENCE-MANIFEST.json'},'exact package inventory required')
    for name,h in inventory.items():D.require(hashlib.sha256(files[safe_name(name)]).hexdigest()==pin(h),'package member bytes differ')
    ref=safe_name(m['reference_image']);D.require(ref in inventory and ref.endswith('.png'),'contained reference PNG required')
    from PIL import Image
    with Image.open(io.BytesIO(files[ref])) as image:
        D.require(image.format=='PNG' and image.width*image.height<=16_000_000,'bounded real reference PNG required');image.verify()
    t=m['expected_target']
    D.require(isinstance(t,dict) and set(t)=={'image_sha256','model_sha256','fonts','layout_profile','layout_binding_sha256','seed'},'exact output expectations required; client target_binding is not accepted')
    D.require(t['model_sha256']==m['model_sha256'] and pin(t['image_sha256'])==inventory[ref],'reference/target model and image must agree')
    D.require(isinstance(t['fonts'],dict) and bool(t['fonts']),'exact font identities required')
    for role,font in t['fonts'].items():
        D.require(isinstance(role,str) and isinstance(font,dict) and set(font)=={'id','sha256'} and isinstance(font['id'],str) and font['id'],'font identity malformed');pin(font['sha256'])
    D.require(t['layout_profile'] in ('baseline-v1','natural-layout-v1'),'known layout required');pin(t['layout_binding_sha256'])
    D.require((t['layout_profile']=='baseline-v1' and t['seed'] is None) or (t['layout_profile']=='natural-layout-v1' and type(t['seed']) is int and 0<=t['seed']<=0xffffffff),'explicit valid layout seed required')
    return m,files

def produce(job,item,payload,fonts,model_sha256):
    """Called only by the real render producer after PNG commit. No owner decision."""
    from app.core.storage import storage_root
    from app.domains.render.ink_render_provenance import _context
    snapshot=json.loads(job.style_snapshot)
    D.require(payload['outcome']=='rendered' and payload['mode']=='personal','actual personal output required')
    pin(model_sha256);D.require(isinstance(fonts,dict) and bool(fonts),'actual render font identities required')
    records=payload.get('chars',[])
    D.require(any(r.get('source')=='M2' and r.get('generation_kind')=='adapter_inference' for r in records),'native adapter output required')
    for r in records:
        if r.get('source')=='M2':D.require(r.get('adapter_sha256')==model_sha256,'rendered model identity differs')
    root=storage_root();expected=f'{job.user_id}/outputs/{job.job_id}/{item.item_id}_v{item.render_version}.png'
    D.require(item.output_relative_path==expected,'current output path required');image=D.contained(root,expected)
    body={'schema':PRODUCER,'producer_kind':'render_worker','user_id':job.user_id,'job_id':job.job_id,'item_id':item.item_id,'render_version':item.render_version,
        'image_sha256':D.sha(image),'model_sha256':model_sha256,'fonts':fonts,'layout_profile':payload['layout_profile'],
        'layout_binding_sha256':D.digest(payload.get('layout_binding')),'seed':payload['layout_seed'],
        'context_sha256':D.digest(_context(job,item,snapshot)),'sidecar_payload_sha256':D.digest(payload),'owner_acceptance_recorded':False}
    return {'body':body,'hmac_sha256':D._mac(body,True)}

def target(job,item,model_sha256):
    """Fresh target_binding.ok is derived from real files and producer authentication."""
    from app.core.storage import storage_root
    from app.domains.render.glyph_provider import glyph_meta_relative_path
    from app.domains.render.ink_render_provenance import _context
    D.require(job.user_id==item.user_id and job.job_id==item.job_id and item.status in ('completed','completed_with_warning'),'owned completed target required')
    root=storage_root();image=D.contained(root,item.output_relative_path);sidecar=D.contained(root,glyph_meta_relative_path(job,item))
    meta=D.read(root,sidecar);envelope=meta.get('owner_output_producer');D.require(isinstance(envelope,dict),'real producer binding missing; imported examples cannot replace it')
    body=envelope['body'];D.require(isinstance(body,dict) and body.get('schema')==PRODUCER and body.get('producer_kind')=='render_worker' and body.get('owner_acceptance_recorded') is False,'actual producer binding required')
    D.require(hmac.compare_digest(envelope.get('hmac_sha256',''),D._mac(body)),'producer authentication failed')
    plain={k:v for k,v in meta.items() if k!='owner_output_producer'}
    expected={'user_id':job.user_id,'job_id':job.job_id,'item_id':item.item_id,'render_version':item.render_version,'model_sha256':pin(model_sha256),
        'image_sha256':D.sha(image),'sidecar_payload_sha256':D.digest(plain),'context_sha256':D.digest(_context(job,item,json.loads(job.style_snapshot)))}
    D.require(all(D.digest(body.get(k))==D.digest(v) for k,v in expected.items()),'target image/sidecar/model/context changed')
    from app.domains.render.service import _resolve_font
    from app.domains.render.mixed_fonts import resolve_frozen
    snapshot=json.loads(job.style_snapshot);default_path,default_id=_resolve_font(snapshot);mixed=resolve_frozen(snapshot)
    for role,font in body['fonts'].items():
        if role.startswith('mixed:'):
            D.require(mixed is not None and role[6:] in mixed[0]['fonts'],'current mixed font selection missing')
            current=mixed[0]['fonts'][role[6:]];font_path=mixed[1][role[6:]]
            D.require(font['id']==current['id'],'current font identity differs')
        else:font_path=default_path;D.require(font['id']==default_id,'current render font identity differs')
        D.require(D.sha(Path(font_path))==font['sha256'],'actual target font bytes changed or missing')
    D.require(body['layout_profile']==meta.get('layout_profile') and D.digest(meta.get('layout_binding'))==body['layout_binding_sha256']
        and D.digest(meta.get('layout_seed'))==D.digest(body['seed']),'actual target layout/seed differs')
    from PIL import Image
    with Image.open(image) as im:D.require(im.format=='PNG','target must be a real PNG');im.verify()
    return {'ok':True,**body,'sidecar_sha256':D.sha(sidecar),'output_relative_path':item.output_relative_path}

def candidate(package_sha,m,binding,user_id,run_id):
    D.require(binding.get('ok') is True,'fresh server target_binding.ok required')
    expected=m['expected_target'];D.require(all(D.digest(binding.get(k))==D.digest(v) for k,v in expected.items()),'package and actual target image/model/fonts/layout/seed differ')
    body={'schema':'owner-direct-output-candidate/1','user_id':user_id,'run_id':run_id,'package_sha256':pin(package_sha),'profile_id':m['profile_id'],
        'target_binding':binding,'review_mode':D.MODE,'acceptance_scope':'exact_render_output','future_outputs_accepted':False,'blind_review_performed':False,'may_publish':False}
    return {**body,'candidate_id':D.digest(body)}

def receipt_path(root,current):return Path(root)/'receipts/owner-output-evidence'/(current['candidate_id']+'.json')
def assess(root,current):
    path=receipt_path(root,current)
    if not path.is_file():return {'status':'NOT_RECORDED','may_publish':False,'blind_review_performed':False}
    record=D.read(root,path);body=record['body']
    D.require(body.get('schema')=='owner-direct-output-evidence-receipt/1' and body.get('candidate')==current and body.get('decision')=='accept'
        and body.get('reviewer_kind')=='authenticated_owner' and body.get('review_mode')==D.MODE and body.get('blind_review_performed') is False and body.get('future_outputs_accepted') is False and body.get('may_publish') is False,'limited output receipt scope differs')
    D.require(hmac.compare_digest(record.get('hmac_sha256',''),D._mac(body)),'output receipt authentication failed')
    return {'status':'ACCEPTED','receipt_sha256':D.sha(path),'candidate_id':current['candidate_id'],'may_publish':False,'blind_review_performed':False}

def submit(root,current,payload):
    D.require(isinstance(payload,dict) and set(payload)=={'candidate_id','decision','acknowledge_viewed_images','statement'},'exact explicit owner confirmation required')
    D.require(payload.get('candidate_id')==current['candidate_id'] and payload.get('decision')=='accept' and payload.get('acknowledge_viewed_images') is True,'target changed or owner did not confirm')
    statement=payload.get('statement');D.require(isinstance(statement,str) and 1<=len(statement.strip())<=1000,'owner statement required')
    path=receipt_path(root,current)
    if path.exists():
        prior=assess(root,current);D.require(D.read(root,path)['body']['owner_statement']==statement.strip(),'repeat statement differs; historical receipt cannot be overwritten');return {**prior,'idempotent':True}
    body={'schema':'owner-direct-output-evidence-receipt/1','candidate':current,'review_mode':D.MODE,'decision':'accept','reviewer_kind':'authenticated_owner',
        'owner_statement':statement.strip(),'created_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
        'future_outputs_accepted':False,'blind_review_performed':False,'may_publish':False}
    raw={'body':body,'hmac_sha256':D._mac(body,True)};path.parent.mkdir(parents=True,exist_ok=True)
    try:
        with path.open('x',encoding='utf-8') as f:json.dump(raw,f,ensure_ascii=False,indent=2,allow_nan=False)
    except FileExistsError:return submit(root,current,payload)
    return {**assess(root,current),'idempotent':False}
