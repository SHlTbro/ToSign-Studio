"""Pixel-bound review artifacts with fail-closed, separate AI/Owner gates.

The default reviewer is unavailable. Structural pixel checks are not AI vision
and cannot create AI_REVIEW_COMPLETE. A configured, authorized reviewer must
actually inspect the supplied pixels; this module never calls external APIs.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path

QUALITY_FIELDS = ('image_quality','glyph_presence','background_contamination','split_quality')
SCORE_FIELDS = ('style_similarity','glyph_correctness','artifact_quality','human_likeness','consistency')
RECOMMENDATIONS = ('AI_ACCEPT','AI_REJECT','AI_NEEDS_HUMAN')
_REVIEWER = None


def configure_reviewer(reviewer):
    """Application bootstrap only: inject an explicitly authorized vision adapter.

    Callable receives pixels/paths and rubric; never pass database writes or an
    Owner-confirm endpoint to it. No provider is selected or paid for here.
    """
    global _REVIEWER
    _REVIEWER = reviewer


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def now(): return datetime.now(timezone.utc).isoformat()


def write_new(path, payload):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as f:json.dump(payload,f,ensure_ascii=False,indent=2,allow_nan=False)
    return path


def inspect_glyph(path, expected_size=None):
    from PIL import Image
    import numpy as np
    errors=[];suspicions=[]
    try:
        with Image.open(path) as im:
            im.load();mode=im.mode;size=list(im.size)
            if mode!='RGBA':errors.append('ALPHA_CHANNEL_MISSING')
            a=np.asarray(im.convert('RGBA').getchannel('A'))
        strong=a>=128;ink=int(strong.sum());total=int(a.size)
        if ink<16:errors.append('BLANK_OR_TOO_LITTLE_INK')
        if ink/total>0.60:errors.append('OPAQUE_BLOCK_OR_INVERTED_ALPHA')
        if expected_size and tuple(size)!=tuple(expected_size):errors.append('WRONG_CANVAS_SIZE')
        edge=int(strong[0,:].sum()+strong[-1,:].sum()+strong[1:-1,0].sum()+strong[1:-1,-1].sum())
        if edge:suspicions.append('INK_TOUCHES_CANVAS_EDGE_POSSIBLE_CROP')
        # Fragment checks are only numeric suspicion, never semantic OCR.
        seen=np.zeros_like(strong,dtype=bool);areas=[];h,w=strong.shape
        for y,x in zip(*np.nonzero(strong)):
            if seen[y,x]:continue
            stack=[(int(y),int(x))];seen[y,x]=True;area=0
            while stack:
                yy,xx=stack.pop();area+=1
                for dy,dx in ((1,0),(-1,0),(0,1),(0,-1)):
                    ny,nx=yy+dy,xx+dx
                    if 0<=ny<h and 0<=nx<w and strong[ny,nx] and not seen[ny,nx]:
                        seen[ny,nx]=True;stack.append((ny,nx))
            areas.append(area)
        tiny=sum(n for n in areas if n<=3)
        if len([n for n in areas if n<=3])>=8:suspicions.append('MANY_TINY_FRAGMENTS_POSSIBLE_RESIDUE')
        return {'image_sha256':sha(path),'mode':mode,'size':size,'opaque_ink_pixels':ink,
                'ink_ratio':ink/total,'edge_ink_pixels':edge,'components':len(areas),
                'tiny_fragment_pixels':tiny,'clear_errors':errors,'suspicious_reasons':suspicions,
                'mislabel_check':'REQUIRES_PIXEL_VIEWING_AI_OR_OWNER'}
    except (OSError,ValueError) as exc:
        return {'image_sha256':sha(path) if Path(path).is_file() else None,
                'clear_errors':['UNREADABLE_GLYPH'],'suspicious_reasons':[str(exc)]}


def _valid_provider(result, expected_sha):
    return (isinstance(result,dict) and result.get('image_sha256')==expected_sha
            and result.get('pixels_inspected') is True
            and isinstance(result.get('model_id'),str) and bool(result['model_id'].strip())
            and isinstance(result.get('provider'),str) and bool(result['provider'].strip())
            and result.get('reviewer_kind')=='vision_model')


def candidate_review(candidate_id, image_path, *, label=None, reviewer=None, expected_size=(128,128)):
    checks=inspect_glyph(image_path,expected_size)
    result={'schema':'candidate-visual-review/1','candidate_id':str(candidate_id),
            'image_sha256':checks['image_sha256'],'label':label,
            **{k:'UNREVIEWED' for k in QUALITY_FIELDS},'confidence':None,
            'recommendation':'AI_NEEDS_HUMAN','review_state':'BLOCKED_AI_PROVIDER',
            'structural_checks':checks,'created_at':now(),'owner_confirmation':'UNCHANGED'}
    provider=reviewer if reviewer is not None else _REVIEWER
    if provider is not None:
        try:
            response=provider({'task':'candidate_glyph_review','image_path':str(Path(image_path).resolve()),
                               'image_sha256':checks['image_sha256'],'label':label,
                               'rubric':list(QUALITY_FIELDS)+['mislabel','cut_strokes','alpha_polarity'],
                               'permitted_recommendations':list(RECOMMENDATIONS)})
            confidence=response.get('confidence') if isinstance(response,dict) else None
            valid=(_valid_provider(response,checks['image_sha256'])
                   and all(isinstance(response.get(k),str) and response[k] for k in QUALITY_FIELDS)
                   and response.get('recommendation') in RECOMMENDATIONS
                   and isinstance(confidence,(int,float)) and not isinstance(confidence,bool)
                   and math.isfinite(confidence) and 0<=confidence<=1)
            if not valid:raise ValueError('invalid or unbound AI review response')
            result.update({k:response[k] for k in (*QUALITY_FIELDS,'confidence','recommendation')})
            result.update({'review_state':'AI_REVIEW_COMPLETE','reviewer':{k:response[k] for k in ('model_id','provider','reviewer_kind')},
                           'reason':str(response.get('reason',''))})
        except Exception as exc:result['review_error']=str(exc)
    if checks['clear_errors']:
        result['recommendation']='AI_REJECT'
        result['structural_block']=True
    return result


def dataset_audit(rows, *, snapshot_dir=None, reviewer=None):
    """Inspect every glyph. Read-only for an existing frozen snapshot."""
    reviews=[]
    for row in rows:
        path=Path(snapshot_dir)/row['snapshot_file'] if snapshot_dir else Path(row['alpha_path'])
        reviews.append(candidate_review(row['sample_id'],path,label=row.get('label'),reviewer=reviewer))
    rejected=[r for r in reviews if r['recommendation']=='AI_REJECT']
    unresolved=[r for r in reviews if r['review_state']!='AI_REVIEW_COMPLETE' or r['recommendation']=='AI_NEEDS_HUMAN']
    status='FAIL' if rejected else ('BLOCKED' if unresolved or not reviews else 'PASS')
    return {'schema':'dataset-visual-audit/1','status':status,'created_at':now(),'sample_count':len(reviews),
            'accepted_count':sum(r['recommendation']=='AI_ACCEPT' for r in reviews),
            'rejected_count':len(rejected),'unresolved_count':len(unresolved),
            'suspicious_candidates':[{'candidate_id':r['candidate_id'],'reasons':r['structural_checks']['clear_errors']+r['structural_checks']['suspicious_reasons']+([r.get('reason','AI review unresolved')] if r['recommendation']!='AI_ACCEPT' else [])} for r in reviews if r['recommendation']!='AI_ACCEPT' or r['structural_checks']['suspicious_reasons']],
            'reviews':reviews,'existing_frozen_snapshot_readonly':snapshot_dir is not None,
            'owner_confirmation':'UNCHANGED','may_freeze_new_snapshot':status=='PASS'}


def audit_frozen_snapshot(snapshot_dir, out_path):
    root=Path(snapshot_dir).resolve();out=Path(out_path).resolve()
    if out.is_relative_to(root):raise ValueError('read-only audit output must be outside frozen snapshot')
    rows=[json.loads(line) for line in (root/'samples.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    report=dataset_audit(rows,snapshot_dir=root)
    report['snapshot_index_sha256']=sha(root/'index.json')
    return write_new(out,report)


def render_blind_panel_pixels(images, identities):
    """Shared deterministic renderer used to independently verify panel pixels."""
    from PIL import Image, ImageDraw
    panel=Image.new('RGB',(4*150,180),'white');draw=ImageDraw.Draw(panel)
    for i,identity in enumerate(identities):
        with Image.open(images[identity]) as im:
            rgba=im.convert('RGBA');bg=Image.new('RGBA',rgba.size,'white')
            if im.mode=='RGBA':
                ink=Image.new('RGBA',rgba.size,(0,0,0,0));ink.putalpha(rgba.getchannel('A'));bg.alpha_composite(ink)
            else:bg.alpha_composite(rgba)
            panel.paste(bg.convert('RGB').resize((128,128)),(i*150+10,28))
        draw.text((i*150+12,8),chr(65+i),fill='black')
    return panel


def build_blind_panel(images, out_dir, *, target_char, rng=None):
    """Exactly M0/M1/M2/reference pixels; identities stay outside reviewer input."""
    if set(images)!={'M0','M1','M2','reference'}:raise ValueError('all four actual images are required')
    root=Path(out_dir);root.mkdir(parents=True,exist_ok=False)
    identities=list(images);(rng or random.SystemRandom()).shuffle(identities)
    panel=render_blind_panel_pixels(images,identities)
    mapping={chr(65+i):identity for i,identity in enumerate(identities)}
    bindings={name:{'image_sha256':sha(images[identity])} for name,identity in mapping.items()}
    path=root/'blind_panel.png';panel.save(path)
    private=root/'private';private.mkdir()
    write_new(private/'identity-map.json',{'identities':mapping,'source_paths':{k:str(Path(v).resolve()) for k,v in images.items()}})
    payload={'schema':'blind-visual-input/1','panel_path':str(path.resolve()),'panel_sha256':sha(path),
             'target_char':target_char,'anonymous_images':bindings,'scores':list(SCORE_FIELDS),
             'reference_anonymous_id':next(k for k,v in mapping.items() if v=='reference'),
             'reference_role':'Style reference may show a different character; engine identities remain hidden',
             'identity_randomized':True,'no_identity_labels_in_panel':True}
    write_new(root/'blind-input.json',payload)
    return payload


def review_blind_panel(blind_input, *, reviewer=None):
    """Completeness does not assert owner approval or automatically pass quality."""
    report={'schema':'m2-visual-review/1','review_state':'BLOCKED_AI_PROVIDER','AI_VISUAL_REVIEW':'BLOCKED',
            'OWNER_BLIND_REVIEW':'PENDING','panel_path':blind_input['panel_path'],'panel_sha256':blind_input['panel_sha256'],
            'anonymous_images':blind_input['anonymous_images'],'identity_randomized':True,
            'scores':{},'created_at':now()}
    provider=reviewer if reviewer is not None else _REVIEWER
    if provider is None:return report
    if sha(blind_input['panel_path'])!=blind_input['panel_sha256']:raise ValueError('panel hash changed')
    response=provider({'task':'blind_glyph_visual_review',**blind_input,'identity_map':None})
    if not _valid_provider(response,blind_input['panel_sha256']):raise ValueError('AI review not bound to viewed panel')
    scores=response.get('scores',{})
    if set(scores)!=set(blind_input['anonymous_images']):raise ValueError('missing anonymous image scores')
    for item in scores.values():
        for field in SCORE_FIELDS:
            v=item.get(field)
            if not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) or not 0<=v<=5:
                raise ValueError('all five finite 0..5 scores required')
    report.update({'review_state':'AI_REVIEW_COMPLETE','scores':scores,
                   'reviewer':{k:response[k] for k in ('model_id','provider','reviewer_kind')},
                   'reason':str(response.get('reason','')),'review_response':response,
                   'review_response_sha256':hashlib.sha256(json.dumps(response,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest(),
                   'AI_VISUAL_REVIEW':'FAIL' if response.get('clear_visual_errors') else 'BLOCKED',
                   'quality_decision':'REQUIRES_PREREGISTERED_THRESHOLDS_AND_UNBLINDED_CHECK'})
    return report


def publish_readiness(gates):
    names=('training','inference','ai_visual','hash_verification','evaluation')
    missing={k:gates.get(k,'BLOCKED') for k in names if gates.get(k)!='PASS'}
    return {'status':'PUBLISH_READY_PENDING_OWNER' if not missing else 'BLOCKED',
            'engineering_gates':{k:gates.get(k,'BLOCKED') for k in names},'blockers':missing,
            'AI_VISUAL_REVIEW':gates.get('ai_visual','BLOCKED'),'OWNER_BLIND_REVIEW':'PENDING',
            'may_publish':False,'independent_engineering_may_continue':True}


def finalize_visual_quality(report, identity_map, thresholds):
    """Unblind only after completed AI scores; policy must be preregistered."""
    if report.get('review_state')!='AI_REVIEW_COMPLETE':return report
    if set(thresholds)!=set(SCORE_FIELDS):raise ValueError('five preregistered thresholds required')
    if set(identity_map.values())!={'M0','M1','M2','reference'}:raise ValueError('invalid blind identity map')
    for value in thresholds.values():
        if not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value) or not 0<=value<=5:
            raise ValueError('thresholds must be finite 0..5 values')
    key=next(k for k,v in identity_map.items() if v=='M2')
    scores=report['scores'][key]
    failed=[field for field in SCORE_FIELDS if scores[field]<thresholds[field]]
    out=dict(report)
    out.update({'AI_VISUAL_REVIEW':'FAIL' if failed or report.get('AI_VISUAL_REVIEW')=='FAIL' else 'PASS',
                'm2_anonymous_id':key,'thresholds':dict(thresholds),
                'failed_dimensions':failed,'quality_decision':'PREREGISTERED_AI_THRESHOLDS_ONLY',
                'OWNER_BLIND_REVIEW':'PENDING'})
    return out
