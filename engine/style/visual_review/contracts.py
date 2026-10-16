"""Strict AI evidence validation. Status strings alone are never a gate."""
import hashlib
import json
import math
import re
from pathlib import Path
from .pipeline import SCORE_FIELDS, sha, render_blind_panel_pixels


def require(value, reason):
    if not value:raise ValueError(reason)


def _verified_review_snapshot(manifest, inference=None):
    from engine.style.dataset.binding import verify_snapshot_binding, verify_m2_receipt_binding
    binding=manifest.get('snapshot_binding')
    require(isinstance(binding,dict),'canonical review snapshot binding missing')
    canonical=verify_snapshot_binding(binding.get('snapshot_dir'),manifest.get('dataset_snapshot_sha256'))
    require(binding==canonical,'manifest full review snapshot binding drift')
    if inference is not None:
        require(isinstance(inference,dict),'review inference receipt missing')
        verify_m2_receipt_binding(inference,canonical)
        require(inference.get('snapshot_binding')==canonical,'inference full review snapshot binding drift')
    return canonical


def validate_review_source_paths(run_dir, identity_record, manifest, inference):
    """Authorize image locations by role, with one exact training reference.

    Snapshot membership is never a blanket exception for arbitrary evidence.
    The reference must be the exact indexed training image used by inference,
    even when the snapshot itself is located inside the run directory.
    """
    root=Path(run_dir).resolve()
    require(isinstance(identity_record,dict) and isinstance(inference,dict),'review source identity/inference record missing')
    canonical=_verified_review_snapshot(manifest,inference)
    snapshot=Path(canonical['snapshot_dir'])
    sources=identity_record.get('source_paths')
    require(isinstance(sources,dict) and set(sources)=={'M0','M1','M2','reference'},'four role-bound source paths required')
    def absolute(value):
        require(isinstance(value,str) and bool(value.strip()),'review source path missing')
        path=Path(value)
        return (path if path.is_absolute() else root/path).absolute()
    for role in ('M0','M1','M2'):
        path=absolute(sources[role]).resolve()
        require(path.is_relative_to(root),'model review source escapes run: '+role)
        require(not path.is_relative_to(snapshot),'model review source cannot use snapshot sample: '+role)
    reference=absolute(sources['reference'])
    require(reference.resolve().is_relative_to(snapshot),'reference escapes canonical snapshot')
    inputs=inference.get('inputs')
    require(isinstance(inputs,dict),'reference inference inputs missing')
    require(reference==absolute(inputs.get('style_ref_path')),'reference differs from inference style_ref_path')
    index=json.loads((snapshot/'index.json').read_text(encoding='utf-8'))
    rows=[json.loads(line) for line in (snapshot/'samples.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    split=json.loads((snapshot/'split.json').read_text(encoding='utf-8'))['assignments']
    matches=[row for row in rows if snapshot/index['files'][row['sample_id']]['path']==reference]
    require(len(matches)==1,'reference must name exactly one canonical indexed sample')
    row=matches[0];sample_id=row['sample_id'];indexed=index['files'][sample_id]
    require(split.get(sample_id)=='train','reference must be a training-side sample')
    require(reference.resolve().is_relative_to(snapshot),'indexed reference symlink escapes snapshot')
    require(sha(reference)==indexed['sha256']==inputs.get('style_ref_sha256'),'reference current/indexed/inference hash mismatch')
    return {'snapshot_binding':canonical,'sample_id':sample_id,'reference_path':str(reference),'reference_sha256':indexed['sha256']}


def validate_scores(scores, identities):
    require(isinstance(scores,dict) and set(scores)==set(identities),'exactly four anonymous score records required')
    for item in scores.values():
        require(isinstance(item,dict),'score record must be an object')
        for field in SCORE_FIELDS:
            v=item.get(field)
            require(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and 0<=v<=5,'five finite 0..5 scores required')


def validate_visual_receipt(visual, identity_record, thresholds, m2_png_sha256):
    require(visual.get('review_state')=='AI_REVIEW_COMPLETE','AI review incomplete')
    response=visual.get('review_response');require(isinstance(response,dict),'raw pixel-viewing response missing')
    response_sha=hashlib.sha256(json.dumps(response,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    require(response_sha==visual.get('review_response_sha256'),'AI response checksum mismatch')
    reviewer=visual.get('reviewer')
    require(isinstance(reviewer,dict) and reviewer.get('reviewer_kind')=='vision_model','vision reviewer provenance missing')
    require(all(isinstance(reviewer.get(k),str) and reviewer[k].strip() for k in ('model_id','provider')),'model/provider missing')
    require(reviewer.get('provider')!='TEST_ONLY' and 'fixture' not in reviewer.get('model_id','').lower(),'test fixture cannot pass production gate')
    require(all(response.get(k)==reviewer.get(k) for k in ('model_id','provider','reviewer_kind')),'AI reviewer metadata mismatch')
    require(response.get('pixels_inspected') is True and response.get('image_sha256')==visual.get('panel_sha256'),'response does not bind viewed panel')
    mapping=identity_record.get('identities');images=identity_record.get('source_paths')
    require(isinstance(mapping,dict) and set(mapping)==set('ABCD') and set(mapping.values())=={'M0','M1','M2','reference'},'complete blinded identity map required')
    require(isinstance(images,dict) and set(images)=={'M0','M1','M2','reference'},'four source image paths required')
    anonymous=visual.get('anonymous_images')
    require(isinstance(anonymous,dict) and set(anonymous)==set('ABCD'),'four image hash bindings required')
    for name,identity in mapping.items():require(sha(images[identity])==anonymous[name].get('image_sha256'),'source image hash mismatch')
    require(sha(images['M2'])==m2_png_sha256,'review concerns another M2 output')
    require(sha(visual['panel_path'])==visual['panel_sha256'],'panel file hash mismatch')
    from PIL import Image
    expected=render_blind_panel_pixels(images,[mapping[k] for k in 'ABCD'])
    with Image.open(visual['panel_path']) as actual:
        require(actual.size==expected.size and actual.convert('RGB').tobytes()==expected.tobytes(),'panel pixels do not match bound source images/order')
    require(visual.get('identity_randomized') is True,'identity randomization absent')
    validate_scores(visual.get('scores'),mapping)
    require(visual['scores']==response.get('scores'),'displayed scores differ from raw AI response')
    require(isinstance(thresholds,dict) and set(thresholds)==set(SCORE_FIELDS),'pinned five-field quality policy missing')
    for value in thresholds.values():require(isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and 0<=value<=5,'invalid policy threshold')
    require(visual.get('thresholds')==thresholds,'post-hoc/unpinned thresholds')
    m2key=next(key for key,value in mapping.items() if value=='M2')
    require(visual.get('m2_anonymous_id')==m2key,'M2 unblinding mismatch')
    computed='FAIL' if response.get('clear_visual_errors') or any(visual['scores'][m2key][field]<thresholds[field] for field in SCORE_FIELDS) else 'PASS'
    require(visual.get('AI_VISUAL_REVIEW')==computed,'AI quality decision does not recompute')
    return computed


def validate_preprocessing_lineage(*records, requested_profile=None):
    """Every link agrees; absent historical metadata is always legacy."""
    from engine.style.m2_adapter.preprocessing import verify_preprocessing_contract
    for record in records:
        training_code = (record.get('runtime_evidence') or {}).get('code_sha256') or {}
        inference_code = record.get('application_code_sha256') or {}
        modern = (record.get('training_objective') is not None
                  or 'preprocessing.py' in training_code or 'preprocessing.py' in inference_code)
        require(not modern or record.get('preprocessing') is not None,
                'modern receipt is missing preprocessing identity')
    contracts = [verify_preprocessing_contract(record.get('preprocessing')) for record in records]
    require(bool(contracts), 'preprocessing lineage requires an identity record')
    require(all(item == contracts[0] for item in contracts), 'preprocessing lineage mismatch')
    if requested_profile is not None:
        require(contracts[0]['profile'] == requested_profile, 'configured preprocessing profile mismatch')
    return contracts[0]


def validate_evaluation_binding(evaluation, adapter_sha, base_hashes, snapshot_sha, runtime_identity, preprocessing=None, runtime_binding=None, *, model_identity=None, preserved_deployment=None):
    require(bool(re.fullmatch(r"[0-9a-f]{64}",str(runtime_identity))),"full runtime identity missing")
    require(evaluation.get('status')=='OK' and evaluation.get('engine',{}).get('engine_mode')=='M2_ADAPTER','real M2 evaluation required')
    from app.domains.styles.m2_topology import validated_topology, validate_topology_lineage
    from engine.style.m2_adapter.topology_dispatch import BASELINE_TOPOLOGY_ID
    engine=evaluation['engine']
    if model_identity is None:
        require(validated_topology(engine)['id']==BASELINE_TOPOLOGY_ID,
                'geometry evaluation requires the expected artifact model identity')
        model_identity=engine
    validate_topology_lineage(model_identity,engine)
    require(engine.get('runtime_binding')==runtime_binding,'evaluation execution binding mismatch')
    expected_version=f'adapter@{adapter_sha[:16]}'
    require(evaluation['engine'].get('model_version')==expected_version,'evaluation model version belongs to another adapter')
    require(evaluation.get('input',{}).get('snapshot_sha256')==snapshot_sha,'evaluation S03 mismatch')
    rows=evaluation.get('per_char');require(isinstance(rows,list) and len(rows)>=2,'at least two evaluated characters required')
    require(len({r.get('label') for r in rows})==len(rows),'duplicate eval targets')
    passes=0
    for row in rows:
        receipt=row.get('gen_receipt') or {}
        validate_topology_lineage(model_identity,receipt)
        require(row.get('status')=='ok' and receipt.get('source')=='M2' and receipt.get('adapter_applied') is True and receipt.get('quality_eligible') is True,'every eval row requires qualification-eligible actual M2 receipt')
        require(receipt.get('runtime_identity_sha256')==runtime_identity,'evaluation runtime identity mismatch')
        if runtime_binding is not None:
            from engine.style.m2_adapter.runtime_variants import verify_execution_receipt
            verify_execution_receipt(runtime_binding,receipt,'inference')
        if preserved_deployment is not None:
            from engine.style.m2_adapter.preserved_training_deployment import verify_inference_receipt
            verify_inference_receipt(preserved_deployment,receipt)
        validate_preprocessing_lineage({'preprocessing': preprocessing}, receipt)
        require(receipt.get('adapter_sha256')==adapter_sha and receipt.get('model_version')==expected_version,'eval row adapter/version mismatch')
        require(receipt.get('dataset_snapshot_sha256')==snapshot_sha and receipt.get('base_ckpt_sha256')==base_hashes,'eval row base/S03 mismatch')
        require(receipt.get('inputs',{}).get('target_char')==row.get('label'),'eval target binding mismatch')
        require(sha(row['out_png'])==row.get('out_png_sha256')==receipt.get('output',{}).get('png_sha256'),'eval output mismatch')
        passes+=row.get('pass_proxy') is True
    summary=evaluation.get('summary') or {};n=len(rows)
    require(summary.get('n_targets')==n and summary.get('n_generated')==n and summary.get('n_gen_error')==0 and summary.get('n_pass_proxy')==passes and summary.get('n_fail_proxy')==n-passes,'eval summary does not match rows')
    return 'PASS' if passes==n else 'FAIL'


def validate_review_set(root, primary, manifest, thresholds):
    """Validate every preregistered category/panel, never just the first glyph."""
    root=Path(root).resolve()
    def contained(value):
        p=Path(value);p=(p if p.is_absolute() else root/p).resolve()
        require(p.is_relative_to(root),'review evidence escapes run')
        return p
    package_path=root/'review-package.json'
    package=json.loads(package_path.read_text(encoding='utf-8'))
    from app.domains.styles.m2_topology import validate_topology_lineage
    validate_topology_lineage(manifest,package)
    require(primary.get('review_package_sha256')==sha(package_path),'review package hash mismatch')
    require(primary.get('review_nonce')==package.get('nonce'),'review nonce mismatch')
    adapter=next(f['sha256'] for f in manifest['files'] if f['relative_path']=='artifact/m2_adapter.pt')
    require(package.get('adapter_sha256')==adapter and package.get('snapshot_sha256')==manifest['dataset_snapshot_sha256'],'review package artifact mismatch')
    runtime=manifest.get('runtime_identity_sha256')
    require(manifest.get('engine_mode')=='M2_ADAPTER' and bool(re.fullmatch(r'[0-9a-f]{64}',str(runtime))),'M2 manifest and full runtime identity required')
    require(package.get('runtime_identity_sha256')==runtime,'review package runtime identity mismatch')
    require(package.get('runtime_binding')==manifest.get('runtime_binding'),'review package execution binding mismatch')
    validate_preprocessing_lineage(manifest, package)
    panels=package.get('panels');rows=primary.get('review_set');categories=package.get('categories')
    require(isinstance(panels,dict) and len(panels)>=3,'complete review panels missing')
    require(isinstance(categories,dict) and set(categories)==set(panels),'review categories missing')
    require(set(categories.values())=={'seen_train','validation','unseen'},'seen, validation and unseen visual coverage required')
    require(isinstance(rows,list) and len(rows)==len(panels) and {r.get('target_char') for r in rows}==set(panels),'every panel requires a unique reviewed record')
    snapshot=Path(_verified_review_snapshot(manifest)['snapshot_dir'])
    records=[json.loads(l) for l in (snapshot/'samples.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    split=json.loads((snapshot/'split.json').read_text(encoding='utf-8'))['assignments']
    train={r['label'] for r in records if split.get(r['sample_id'])=='train'}
    val={r['label'] for r in records if split.get(r['sample_id'])=='val'}
    outcomes=[]
    for row in rows:
        char=row['target_char'];category=categories[char]
        require((category=='seen_train' and char in train) or (category=='validation' and char in val and char not in train) or (category=='unseen' and char not in train|val),'review category disagrees with canonical split')
        vp=contained(row['visual_path']);ip=contained(row['inference_path'])
        require(sha(vp)==row.get('visual_sha256') and sha(ip)==row.get('inference_sha256'),'review-set receipt hash mismatch')
        require(ip==contained(package['inference_receipts'][char]),'review-set inference not bound to package')
        visual=json.loads(vp.read_text(encoding='utf-8'));inference=json.loads(ip.read_text(encoding='utf-8'))
        validate_topology_lineage(manifest,inference)
        require(visual.get('review_package_sha256')==sha(package_path) and visual.get('review_nonce')==package['nonce'],'individual review belongs to another job')
        require(visual.get('panel_sha256')==panels[char]['panel_sha256'] and contained(visual['panel_path'])==contained(panels[char]['panel_path']),'reviewed panel differs from package')
        require(inference.get('source')=='M2' and inference.get('adapter_applied') is True and inference.get('quality_eligible') is True,'actual qualification-eligible M2 required for every reviewed glyph')
        require(inference.get('adapter_sha256')==adapter and inference.get('model_version')==manifest['engine_version'],'review-set adapter/version mismatch')
        require(inference.get('dataset_snapshot_sha256')==manifest['dataset_snapshot_sha256'] and inference.get('base_ckpt_sha256')==manifest['base_ckpt_sha256'],'review-set S03/base mismatch')
        require(inference.get('runtime_identity_sha256')==package.get('runtime_identity_sha256'),'review-set runtime mismatch')
        if manifest.get('runtime_binding') is not None:
            from engine.style.m2_adapter.runtime_variants import verify_execution_receipt
            verify_execution_receipt(manifest['runtime_binding'],inference,'inference')
        validate_preprocessing_lineage(manifest, inference)
        require(inference.get('inputs',{}).get('target_char')==char,'review-set character mismatch')
        png=contained(inference['output']['png']);require(sha(png)==inference['output']['png_sha256'],'review-set output hash mismatch')
        mapping_path=contained(visual['identity_map']['path'])
        require(sha(mapping_path)==visual['identity_map']['sha256'],'review-set identity hash mismatch')
        mapping=json.loads(mapping_path.read_text(encoding='utf-8'))
        validate_review_source_paths(root,mapping,manifest,inference)
        outcomes.append(validate_visual_receipt(visual,mapping,thresholds,inference['output']['png_sha256']))
    return 'PASS' if all(x=='PASS' for x in outcomes) else 'FAIL'


def validate_training_runtime(manifest, raw, wrapper, config):
    from engine.style.m2_adapter.runtime_variants import validate_manifest_binding,verify_execution_receipt
    from app.domains.styles.m2_topology import validate_topology_lineage
    validate_topology_lineage(config,manifest,raw,wrapper)
    execution=validate_manifest_binding(config,manifest)
    require(wrapper.get('runtime_binding')==execution,'wrapper execution binding mismatch')
    if execution is not None:verify_execution_receipt(execution,raw,'training')
    elif str((raw.get('process') or {}).get('device','cpu')).startswith('cuda'):
        raise ValueError('GPU training requires an explicit verified execution binding')
    runtime=manifest.get('runtime_identity_sha256')
    require(bool(re.fullmatch(r'[0-9a-f]{64}',str(runtime))),'full training runtime identity missing')
    for record in (manifest,raw,wrapper):
        require(record.get('engine_mode')=='M2_ADAPTER','M2 training engine mode required throughout chain')
        require(record.get('runtime_identity_sha256')==runtime,'training runtime identity mismatch')
    validate_preprocessing_lineage(manifest, raw, wrapper, requested_profile=config.get('preprocessing_profile'))
    for name in ('training_recipe','training_budget','recipe_id','objective_profile','checkpoint_selection'):
        if any(record.get(name) is not None for record in (manifest,raw,wrapper,config)):
            require(manifest.get(name)==raw.get(name)==wrapper.get(name)==config.get(name),'training recipe/budget lineage mismatch: '+name)
    if any(record.get('training_objective') is not None for record in (manifest, raw, wrapper)):
        require(manifest.get('training_objective') == raw.get('training_objective') == wrapper.get('training_objective'), 'training objective lineage mismatch')
    if config.get('runtime_identity_sha256') is not None:
        require(config['runtime_identity_sha256']==runtime,'configured runtime identity mismatch')
    return runtime
