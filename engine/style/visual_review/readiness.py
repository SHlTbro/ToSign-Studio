"""Derive readiness from current, contained, hash-bound evidence, never claims alone."""
from __future__ import annotations
import json
import re
from pathlib import Path
from .pipeline import publish_readiness, sha


def check_run_readiness(run_dir, manifest, run_status, *, run_config=None, user_id=None):
    root=Path(run_dir).resolve();gates={};reasons=[];preserved=None
    from .contracts import validate_visual_receipt, validate_evaluation_binding, validate_preprocessing_lineage, validate_review_source_paths
    def contained(value):
        p=Path(value);p=p if p.is_absolute() else root/p;p=p.resolve()
        if not p.is_relative_to(root):raise ValueError('evidence path escapes run directory')
        return p
    def read(rel):return json.loads(contained(rel).read_text(encoding='utf-8'))
    try:
        from engine.style.m2_adapter import preserved_training_deployment as D
        preserved=D.lookup(Path(__file__).resolve().parents[3],root,user_id=user_id,config=run_config)
        if preserved is None and manifest.get('runtime_binding') is not None:
            from engine.style.m2_adapter.runtime_variants import resolve_binding
            resolve_binding(Path(__file__).resolve().parents[3],manifest['runtime_binding'],user_scr_purpose='inference')
        for item in manifest['files']:
            if sha(contained(item['relative_path']))!=item['sha256']:raise ValueError('artifact file hash mismatch')
        if sha(contained(manifest['metrics_receipt_relative_path']))!=manifest['metrics_receipt_sha256']:
            raise ValueError('training receipt hash mismatch')
        train=read(manifest['metrics_receipt_relative_path'])
        if not isinstance(run_config,dict):raise ValueError('immutable run configuration is required')
        from .policy import validate_threshold_policy
        validate_threshold_policy(run_config,user_id)
        import hashlib
        config_sha=hashlib.sha256(json.dumps(run_config,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        if config_sha!=manifest.get('config_sha256'):raise ValueError('run configuration hash mismatch')
        binding=manifest.get('snapshot_binding') or {}
        from engine.style.dataset.binding import verify_snapshot_binding
        canonical=verify_snapshot_binding(binding.get('snapshot_dir'),manifest['dataset_snapshot_sha256'])
        from engine.style.dataset.binding import verify_m2_receipt_binding
        verify_m2_receipt_binding(train,canonical)
        keys=('authority','snapshot_id','style_id','snapshot_sha256','index_sha256','provenance_sha256')
        if any(binding.get(k)!=canonical.get(k) for k in keys):raise ValueError('artifact snapshot binding drift')
        raw_meta=train.get('training_receipt') or {}
        raw_path=contained(raw_meta['relative_path'])
        if raw_meta.get('relative_path')!='m2-receipt.json' or sha(raw_path)!=raw_meta.get('sha256'):
            raise ValueError('raw M2 training receipt missing or changed')
        raw=json.loads(raw_path.read_text(encoding='utf-8'))
        verify_m2_receipt_binding(raw,canonical)
        if (root/'receipts/admission-supplement.json').is_file():
            raw=D.normalized_training_receipt(root,run_config,manifest)
        from .contracts import validate_training_runtime
        validate_training_runtime(manifest,raw,train,run_config)
        if raw.get('ok') is not True or raw.get('stats',{}).get('final_status')!='COMPLETED':
            raise ValueError('actual training did not complete')
        if raw.get('engine_id')!=manifest.get('engine_id') or train.get('engine_id')!=manifest.get('engine_id'):
            raise ValueError('training engine identity mismatch')
        if raw.get('model_version')!=manifest.get('engine_version') or train.get('engine_version')!=manifest.get('engine_version'):
            raise ValueError('training artifact version mismatch')
        measured=raw.get('runtime_evidence') or {}
        if measured!=train.get('runtime_evidence') or measured.get('loaded_real_base_weights') is not True:
            raise ValueError('verified real base loading evidence missing')
        if measured.get('base_format')!='npz_allow_pickle_false' or measured.get('hook_count')!=22:
            raise ValueError('real adapter architecture evidence missing')
        if not all(isinstance(measured.get(k),int) and measured[k]>0 for k in ('base_parameter_count','adapter_parameter_count')):
            raise ValueError('measured parameter counts missing')
        adapter=next(item for item in manifest['files'] if item['relative_path']=='artifact/m2_adapter.pt')
        raw_adapter=next(item for item in raw['output']['files'] if item['relative_path']=='artifact/m2_adapter.pt')
        if raw_adapter['sha256']!=adapter['sha256'] or manifest['engine_version']!=f"adapter@{adapter['sha256'][:16]}":
            raise ValueError('selected adapter does not match measured training output')
        if raw.get('base_ckpt_sha256')!=manifest.get('base_ckpt_sha256'):
            raise ValueError('training/base full hash mismatch')
        bases=manifest.get('base_ckpt_sha256')
        if not isinstance(bases,dict) or len(bases)!=3 or any(not re.fullmatch(r'[0-9a-f]{64}',str(v)) for v in bases.values()):
            raise ValueError('full base checkpoint hashes missing')
        gates['hash_verification']='PASS'
        gates['training']='PASS' if run_status=='succeeded' and train.get('ok') is True and train.get('engine_mode')=='M2_ADAPTER' else 'BLOCKED'
    except Exception as exc:
        gates['hash_verification']='BLOCKED';gates['training']='BLOCKED';reasons.append(str(exc))
    def require(condition, reason):
        if not condition:raise ValueError(reason)
    if preserved is not None:manifest=D.execution_view(manifest,preserved)
    inference=None
    try:
        inference=read('receipts/m2_inference.json');inputs=inference.get('inputs') or {}
        from app.domains.styles.m2_topology import validate_topology_lineage
        validate_topology_lineage(manifest,inference)
        out=inference['output'];png=contained(out['png'])
        adapter=next(item for item in manifest['files'] if item['relative_path']=='artifact/m2_adapter.pt')
        require(inference.get('ok') is True and inference.get('source')=='M2' and inference.get('adapter_applied') is True and inference.get('quality_eligible') is True, 'qualification-eligible real M2 inference missing')
        require(manifest.get('engine_mode')=='M2_ADAPTER' and inference.get('runtime_identity_sha256')==manifest.get('runtime_identity_sha256') and bool(re.fullmatch(r'[0-9a-f]{64}',str(manifest.get('runtime_identity_sha256')))), 'inference runtime/mode mismatch')
        require(inference.get('adapter_sha256')==adapter['sha256'], 'inference adapter mismatch')
        require(inference.get('dataset_snapshot_sha256')==manifest['dataset_snapshot_sha256'], 'inference S03 mismatch')
        require(inference.get('base_ckpt_sha256')==manifest['base_ckpt_sha256'], 'inference base mismatch')
        validate_preprocessing_lineage(manifest, inference)
        if manifest.get("runtime_binding") is not None:
            from engine.style.m2_adapter.runtime_variants import verify_execution_receipt
            verify_execution_receipt(manifest["runtime_binding"],inference,"inference")
        if preserved is not None:D.verify_inference_receipt(preserved,inference)
        require(inputs.get('unseen_in_training') is True and sha(png)==out['png_sha256'], 'unseen output/hash missing')
        gates['inference']='PASS'
    except Exception as exc:gates['inference']='BLOCKED';reasons.append('inference: '+str(exc))
    try:
        require(gates.get('hash_verification')=='PASS','canonical artifact hash gate required')
        visual=read('receipts/m2_visual_review.json')
        identity_meta=visual.get('identity_map') or {}
        identity_path=contained(identity_meta['path'])
        require(sha(identity_path)==identity_meta.get('sha256'),'identity map checksum mismatch')
        identities=json.loads(identity_path.read_text(encoding='utf-8'))
        contained(visual['panel_path'])
        require(gates['inference']=='PASS','inference gate did not pass')
        validate_review_source_paths(root,identities,manifest,inference)
        decision=validate_visual_receipt(visual,identities,(run_config or {}).get('visual_thresholds'),inference['output']['png_sha256'])
        from .contracts import validate_review_set
        aggregate=validate_review_set(root,visual,manifest,(run_config or {}).get('visual_thresholds'))
        gates['ai_visual']='PASS' if decision==aggregate=='PASS' else 'FAIL'
    except Exception as exc:gates['ai_visual']='BLOCKED';reasons.append('visual: '+str(exc))
    try:
        require(gates.get('hash_verification')=='PASS','canonical artifact hash gate required')
        evaluation=read('receipts/eval_receipt.json')
        for item in evaluation.get('files',{}).values():contained(item['path'])
        from engine.style.evaluator.receipt import verify_receipt
        valid,errors=verify_receipt(evaluation)
        require(valid, str(errors))
        for row in evaluation.get('per_char',[]):
            contained(row['out_png'])
            gen_path=contained(evaluation['files'][f"gen/gen_{row['label_cp']}.json"]['path'])
            require(json.loads(gen_path.read_text(encoding='utf-8'))==row.get('gen_receipt'),'evaluation row/receipt file mismatch')
        adapter=next(item for item in manifest['files'] if item['relative_path']=='artifact/m2_adapter.pt')
        gates['evaluation']=validate_evaluation_binding(evaluation,adapter['sha256'],manifest['base_ckpt_sha256'],manifest['dataset_snapshot_sha256'],manifest.get('runtime_identity_sha256'),manifest.get('preprocessing'),manifest.get('runtime_binding'),model_identity=manifest,preserved_deployment=preserved)
    except Exception as exc:gates['evaluation']='BLOCKED';reasons.append('evaluation: '+str(exc))
    report=publish_readiness(gates);report['details']=reasons
    report['AI_BLIND_REVIEW']=gates.get('ai_visual','BLOCKED')
    report['HUMAN_BLIND_REVIEW']='PENDING'
    from .policy import human_waiver
    policy=human_waiver(user_id) if user_id is not None else None
    if policy:
        report['OWNER_BLIND_REVIEW']=report['HUMAN_BLIND_REVIEW']='WAIVED_BY_USER'
        report['acceptance_policy']={'schema':policy['schema'],'scope':policy['scope'],
                                     'policy_sha256':policy['_sha256'],
                                     'authorization':policy['human_blind_review']['authorization']}
        if report['status']=='PUBLISH_READY_PENDING_OWNER':
            report['status']='PUBLISH_READY_AI_AUTHORIZED';report['may_publish']=True
    from .owner_direct import assess,MODE
    owner=assess(root,manifest,run_config,user_id,gates)
    report['OWNER_DIRECT_VISUAL_ACCEPTANCE']=owner['status']
    report['owner_direct_acceptance']=owner
    if owner['status']=='ACCEPTED':
        report['visual_acceptance_mode']=MODE
        # Preserve actual AI and objective verdicts. Direct appearance acceptance
        # is a distinct legal mode, and only replaces the AI appearance gate.
        report['blockers']={k:v for k,v in report['blockers'].items() if k!='ai_visual'}
        if owner['may_publish']:
            report['status']='PUBLISH_READY_OWNER_DIRECT';report['may_publish']=True
    return report

