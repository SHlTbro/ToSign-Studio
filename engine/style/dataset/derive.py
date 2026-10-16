"""Create a fresh, audited subset of an immutable S03. Never edit its parent."""
import json
import shutil
import uuid
from datetime import datetime,timezone
from pathlib import Path
from .binding import verify_snapshot_binding
from .schema import canonical_json,sha256_bytes,DatasetError
from .snapshot import _content_hash
from .split import SplitConfig


def derive_reviewed_subset(parent_dir,parent_sha,audit_path,out_dir):
    parent=Path(parent_dir).resolve();out=Path(out_dir).resolve();audit_path=Path(audit_path).resolve()
    binding=verify_snapshot_binding(parent,parent_sha)
    if out==parent or out.is_relative_to(parent) or out.exists():raise DatasetError('E_SNAPSHOT_EXISTS','derived snapshot needs a separate fresh directory')
    index=json.loads((parent/'index.json').read_text(encoding='utf-8'))
    rows=[json.loads(line) for line in (parent/'samples.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    split=json.loads((parent/'split.json').read_text(encoding='utf-8'))
    audit=json.loads(audit_path.read_text(encoding='utf-8'))
    if audit.get('review_state')!='AI_REVIEW_COMPLETE' or audit.get('snapshot_index_sha256')!=binding['index_sha256']:
        raise DatasetError('E_VISUAL_AUDIT_BINDING','actual visual audit must bind the exact frozen parent')
    evidence=audit.get('visual_evidence') or {}
    sheet=audit_path.parent/str(evidence.get('contact_sheet',''))
    if evidence.get('pixels_actually_inspected') is not True or not sheet.is_file() or sha256_bytes(sheet.read_bytes())!=evidence.get('sha256'):
        raise DatasetError('E_VISUAL_AUDIT_BINDING','contact-sheet evidence missing or changed')
    reviews={r['candidate_id']:r for r in audit['reviews']}
    if set(reviews)!=set(index['files']):raise DatasetError('E_VISUAL_AUDIT_BINDING','every parent glyph must be reviewed')
    for sid,meta in index['files'].items():
        if reviews[sid].get('image_sha256')!=meta['sha256']:raise DatasetError('E_VISUAL_AUDIT_BINDING','reviewed glyph hash differs')
    kept=[r for r in rows if reviews[r['sample_id']].get('recommendation')=='AI_ACCEPT']
    excluded=[{'sample_id':r['sample_id'],'label':r['label'],'reason':reviews[r['sample_id']].get('reason'),'recommendation':reviews[r['sample_id']]['recommendation']} for r in rows if r not in kept]
    assignments={r['sample_id']:split['assignments'][r['sample_id']] for r in kept}
    groups={side:{r['parent_source_hash'] for r in kept if assignments[r['sample_id']]==side} for side in ('train','val')}
    counts={side:sum(assignments[r['sample_id']]==side for r in kept) for side in ('train','val')}
    cfg=SplitConfig();labels=len({r['label'] for r in kept});parents={r['parent_source_hash'] for r in kept}
    gates={'min_train_samples':counts['train']>=cfg.min_train_samples,'min_val_samples':counts['val']>=cfg.min_val_samples,
           'min_train_groups':len(groups['train'])>=cfg.min_train_groups,'min_val_groups':len(groups['val'])>=cfg.min_val_groups,
           'min_distinct_parents':len(parents)>=cfg.min_distinct_parents,'product_distinct_labels_20':labels>=20,
           'no_parent_cross_split':not bool(groups['train']&groups['val'])}
    if not all(gates.values()):raise DatasetError('INSUFFICIENT_DATA',str(gates))
    stamp=datetime.now(timezone.utc).isoformat();snapshot_id='ps-ai-subset-'+uuid.uuid4().hex
    provenance=json.loads((parent/'provenance.json').read_text(encoding='utf-8'))
    ids=set(assignments)
    for field in ('source_data_hashes','provenance_hashes'):
        if isinstance(provenance.get(field),dict):provenance[field]={k:v for k,v in provenance[field].items() if k in ids}
    for field in ('confirmed_sample_ids','train_sample_ids','validation_sample_ids'):
        if isinstance(provenance.get(field),list):provenance[field]=[x for x in provenance[field] if x in ids]
    provenance.update({'snapshot_id':snapshot_id,'created_at':stamp,
        'derivation':{'kind':'AI_REVIEWED_SUBSET','parent_snapshot_sha256':parent_sha,'parent_index_sha256':binding['index_sha256'],
                      'visual_audit_sha256':sha256_bytes(audit_path.read_bytes()),'excluded':excluded,'gates':gates,
                      'owner_confirmation':'Preserved historical confirmation; no new human act claimed'}})
    samples_bytes=('\n'.join(canonical_json(r) for r in sorted(kept,key=lambda r:r['sample_id']))+'\n').encode()
    derived_split=dict(split);derived_split['assignments']=assignments
    derived_split['plan']={'status':'OK','derived_from_parent_split':parent_sha,'gates':gates,
                           'n_train':counts['train'],'n_val':counts['val'],'n_distinct_labels':labels}
    split_bytes=(canonical_json(derived_split)+'\n').encode();prov_bytes=(canonical_json(provenance)+'\n').encode()
    files={sid:index['files'][sid] for sid in sorted(ids)}
    digest=_content_hash(samples_bytes,split_bytes,{k:v['sha256'] for k,v in files.items()},prov_bytes)
    new_index={**index,'snapshot_id':snapshot_id,'created_at':stamp,'snapshot_sha256':digest,'content_sha256':digest,
               'n_samples':len(kept),'files':files,'provenance_sha256':sha256_bytes(prov_bytes),
               'counts':{'pool':{'n_samples':len(kept),'n_distinct_labels':labels,'n_parents':len(parents)},
                         **{k:{'n_samples':counts[k],'n_parents':len(groups[k]),'n_labels':len({r['label'] for r in kept if assignments[r['sample_id']]==k})} for k in counts}},
               'derived_parent_snapshot_sha256':parent_sha}
    out.mkdir(parents=True)
    for meta in files.values():
        dest=out/meta['path'];dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(parent/meta['path'],dest)
    for name,data in [('samples.jsonl',samples_bytes),('split.json',split_bytes),('provenance.json',prov_bytes),
                      ('index.json',(canonical_json(new_index)+'\n').encode()),('FROZEN',f'FROZEN {snapshot_id} {digest}\n'.encode())]:
        (out/name).write_bytes(data)
    verify_snapshot_binding(out,digest)
    for p in out.rglob('*'):
        if p.is_file():p.chmod(0o444)
    for p in sorted((p for p in out.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True):p.chmod(0o555)
    out.chmod(0o555)
    return {'snapshot_dir':str(out),'snapshot_sha256':digest,'parent_snapshot_sha256':parent_sha,
            'n_samples':len(kept),'n_distinct_labels':labels,'counts':counts,'gates':gates,'excluded':excluded}
