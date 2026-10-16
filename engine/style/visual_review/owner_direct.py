"""Direct owner appearance acceptance, separate from blind/AI review.

The server binds exactly displayed native images, model, S03, configuration and
evaluation facts. An acceptance cannot authorize execution or erase a failed
objective gate. No caller-supplied receipt, path or owner identity is admitted.
"""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,hmac,json,os,re
MODE='owner_direct_visual_acceptance'
QUALITY='OWNER_DIRECT_VISUAL_ACCEPTED'
CORE=('training','inference','hash_verification')
def require(ok,message):
    if not ok:raise ValueError(message)
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False,ensure_ascii=False).encode()).hexdigest()
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(2**20),b''):h.update(block)
    return h.hexdigest()
def contained(root,value):
    root=Path(root).resolve();p=Path(value);p=p if p.is_absolute() else root/p
    require(not any(x.is_symlink() for x in (p,*p.parents)),'owner evidence symlink rejected')
    p=p.resolve();require(p.is_relative_to(root) and p.is_file(),'owner evidence must be a contained regular file');return p
def read(root,value):
    p=contained(root,value);require(p.stat().st_size<=2*2**20,'owner evidence oversized');return json.loads(p.read_text(encoding='utf-8'))
def candidate(root,manifest,config,user_id):
    root=Path(root).resolve();raw=read(root,'artifact/artifact.json')
    require(raw=={k:v for k,v in manifest.items() if k!='_manifest_sha256'},'current manifest differs from file')
    require(type(user_id) is int and raw.get('user_id')==user_id and type(raw.get('run_id')) is int,'owner/run identity differs')
    require(raw.get('engine_mode')=='M2_ADAPTER','direct appearance acceptance requires real M2 artifact')
    adapter=next(f for f in raw['files'] if f['relative_path']=='artifact/m2_adapter.pt')
    require(sha(contained(root,adapter['relative_path']))==adapter['sha256'],'adapter bytes changed')
    inference=read(root,'receipts/m2_inference.json');evaluation=read(root,'receipts/eval_receipt.json')
    require(inference.get('source')=='M2' and inference.get('ok') is True and inference.get('adapter_applied') is True
        and inference.get('adapter_sha256')==adapter['sha256'],'current native M2 preview required')
    require(inference.get('dataset_snapshot_sha256')==raw['dataset_snapshot_sha256'],'preview S03 changed')
    images=[]
    def add(path,pin,label,role):
        p=contained(root,path);require(p.stat().st_size<=32*2**20 and sha(p)==pin,'preview bytes changed')
        images.append({'relative_path':p.relative_to(root).as_posix(),'sha256':pin,'label':label,'role':role})
    add(inference['output']['png'],inference['output']['png_sha256'],inference.get('inputs',{}).get('target_char'),'native_unseen')
    for row in evaluation.get('per_char',[]):add(row['out_png'],row['out_png_sha256'],row.get('label'),'evaluation')
    require(len(images)>=3,'current native preview and at least two evaluation images required')
    files={n:sha(contained(root,n)) for n in ('artifact/artifact.json','m2-receipt.json','receipts/m2_inference.json','receipts/eval_receipt.json',raw['metrics_receipt_relative_path'])}
    body={'schema':'owner-direct-visual-candidate/1','review_mode':MODE,'user_id':user_id,'run_id':raw['run_id'],
        'adapter_sha256':adapter['sha256'],'model_version':raw['engine_version'],'snapshot_sha256':raw['dataset_snapshot_sha256'],
        'runtime_identity_sha256':raw['runtime_identity_sha256'],'runtime_binding':raw.get('runtime_binding'),
        'base_ckpt_sha256':raw['base_ckpt_sha256'],'config_sha256':raw['config_sha256'],
        'files':files,'images':images,'evaluation_summary':evaluation.get('summary'),
        'future_outputs_accepted':False,'blind_review_performed':False}
    require(re.fullmatch('[0-9a-f]{64}',str(body['config_sha256'])) is not None,'complete config hash required')
    from app.domains.styles.runstore import canonical_sha256
    require(canonical_sha256(config)==body['config_sha256'],'immutable configuration differs')
    return {**body,'candidate_id':digest(body)}
def _secret(create=False):
    from app.core.storage import storage_root
    p=storage_root()/'.server-owner-visual/receipt-key'
    require(not any(x.is_symlink() for x in (p,*p.parents)),'owner key symlink rejected')
    if create:
        p.parent.mkdir(parents=True,exist_ok=True)
        if not p.exists():
            with p.open('xb') as f:f.write(os.urandom(32))
    key=p.read_bytes();require(len(key)==32,'owner server key invalid');return key
def _mac(body,create=False):return hmac.new(_secret(create),json.dumps(body,sort_keys=True,separators=(',',':'),allow_nan=False,ensure_ascii=False).encode(),hashlib.sha256).hexdigest()
def assess(root,manifest,config,user_id,gates):
    """Read-only: no receipt is minted and no AI/metric verdict is replaced."""
    result={'status':'NOT_RECORDED','review_mode':MODE,'may_publish':False,'blind_review_performed':False}
    if not (Path(root)/'receipts/owner-direct-visual.json').is_file():return result
    try:
        current=candidate(root,manifest,config,user_id);envelope=read(root,'receipts/owner-direct-visual.json');body=envelope['body']
        require(body.get('schema')=='owner-direct-visual-receipt/1' and body.get('review_mode')==MODE
            and body.get('decision')=='accept' and body.get('reviewer_kind')=='authenticated_owner'
            and body.get('user_id')==user_id and body.get('candidate')==current and body.get('future_outputs_accepted') is False
            and body.get('blind_review_performed') is False,'owner receipt scope changed')
        require(hmac.compare_digest(str(envelope.get('hmac_sha256')),_mac(body)),'owner receipt authentication failed')
        result.update(status='ACCEPTED',receipt_sha256=sha(contained(root,'receipts/owner-direct-visual.json')),candidate_id=current['candidate_id'])
        result['may_publish']=all(gates.get(k)=='PASS' for k in (*CORE,'evaluation'))
    except (ValueError,OSError,KeyError,TypeError,StopIteration) as exc:result.update(status='STALE_OR_INVALID',detail=str(exc))
    return result
def submit(root,manifest,config,user_id,gates,payload):
    require(isinstance(payload,dict) and set(payload)=={'candidate_id','decision','acknowledge_viewed_images','statement'},'exact owner acceptance fields required')
    require(payload.get('decision')=='accept' and payload.get('acknowledge_viewed_images') is True,'explicit direct visual acceptance required')
    statement=payload.get('statement');require(isinstance(statement,str) and 1<=len(statement.strip())<=1000,'owner statement required')
    require(all(gates.get(k)=='PASS' for k in CORE),'training, inference and hash gates required before owner preview acceptance')
    current=candidate(root,manifest,config,user_id);require(payload['candidate_id']==current['candidate_id'],'preview changed; refresh before accepting')
    path=Path(root)/'receipts/owner-direct-visual.json'
    if path.exists():
        prior=assess(root,manifest,config,user_id,gates)
        require(prior['status']=='ACCEPTED','historical owner receipt is stale; new run/evidence required, no overwrite')
        require(read(root,path)['body']['owner_statement']==statement.strip(),'repeated acceptance statement differs')
        return {**prior,'idempotent':True}
    body={'schema':'owner-direct-visual-receipt/1','review_mode':MODE,'user_id':user_id,'run_id':current['run_id'],
        'candidate':current,'decision':'accept','reviewer_kind':'authenticated_owner','owner_statement':statement.strip(),
        'created_at':datetime.now(timezone.utc).isoformat(),'blind_review_performed':False,'future_outputs_accepted':False}
    envelope={'body':body,'hmac_sha256':_mac(body,True)};path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as f:json.dump(envelope,f,indent=2,ensure_ascii=False,allow_nan=False)
    return {**assess(root,manifest,config,user_id,gates),'idempotent':False}
