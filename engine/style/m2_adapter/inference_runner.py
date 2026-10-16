"""One-request subprocess CLI for M2 inference; explicit blocked receipts."""
from __future__ import annotations
import json
import sys
from pathlib import Path
from .trainer_resume_protocol import native_path
from .inference import M2Inference, InferenceError
from engine.style.dataset.schema import DatasetError
from .inference_diagnostics import stage, traced_stage, trace_entry


from .personal_runtime_policy import bound_action, operation_entry

@bound_action('inference')
@traced_stage('runner.run')
def run(req):
    from .release_runtime_policy import assert_m2_execution_allowed
    from .runtime_variants import RuntimeUnavailable
    try:assert_m2_execution_allowed(Path(__file__).resolve().parents[3])
    except (RuntimeUnavailable,OSError) as exc:
        return 2, {"ok":False,"status":"BLOCKED","source":None,"failure":{"code":getattr(exc,'code','BLOCKED_ENV'),"detail":str(exc)},"AI_VISUAL_REVIEW":"BLOCKED","OWNER_BLIND_REVIEW":"PENDING"}
    try:
        required=('adapter_path','adapter_sha256','snapshot_dir','snapshot_sha256','fd_repo','base_npz_sha256','target_char','seed','out_png')
        missing=[k for k in required if k not in req or req[k] is None]
        if missing:raise InferenceError('E_INPUT_MISSING',f'missing fields: {missing}')
        with stage('runner.ctor'):
            engine=M2Inference(**{k:req[k] for k in required[:6]},
                           content_ttf=req.get('content_ttf'),device=req.get('device','cpu'),
                           steps=req.get('steps',20),use_adapter=req.get('use_adapter',True),
                           runtime_manifest_path=req.get('runtime_manifest_path'),
                           allow_legacy_compatibility=req.get('allow_legacy_compatibility',False),
                           sampler=req.get('sampler','dpm'),guidance_scale=req.get('guidance_scale',7.5),
                           cpu_threads=req.get('cpu_threads',2),
                           preprocessing_profile=req.get('preprocessing_profile'),runtime_binding=req.get('runtime_binding'), deployment_binding=req.get('deployment_binding'),deployment_user_id=req.get('deployment_user_id'),recipe_id=req.get('recipe_id'),objective_profile=req.get('objective_profile'),checkpoint_selection=req.get('checkpoint_selection'),
                           **{k:req[k] for k in ('adapter_topology_id','adapter_topology','adapter_topology_sha256','scr_objective','scr_assets','scr_data_binding','scr_cache_binding','scr_preparation_binding','windows_cache_identity') if k in req})
        with stage('runner.generate'):
            _,receipt=engine.generate(req.get('style_ref'),req['target_char'],req['seed'],req['out_png'])
        return 0,receipt
    except (InferenceError,DatasetError) as exc:
        return 2,{'ok':False,'status':'BLOCKED','source':None,'failure':{'code':exc.code,'detail':str(exc)},
                  'AI_VISUAL_REVIEW':'BLOCKED','OWNER_BLIND_REVIEW':'PENDING'}
    except Exception as exc:
        return 2,{'ok':False,'status':'FAIL','source':None,'failure':{'code':'E_INFERENCE','detail':repr(exc)},
                  'AI_VISUAL_REVIEW':'BLOCKED','OWNER_BLIND_REVIEW':'PENDING'}


@trace_entry
@operation_entry
def main(argv=None):
    from .release_runtime_policy import assert_m2_execution_allowed
    from .runtime_variants import RuntimeUnavailable
    from .personal_runtime_policy import present,bootstrap
    try:
        with stage('main.bootstrap'):
            if present(Path(__file__).resolve().parents[3]):bootstrap(Path(__file__).resolve().parents[3])
            else:assert_m2_execution_allowed(Path(__file__).resolve().parents[3])
    except (RuntimeUnavailable,OSError) as exc:
        receipt={"ok":False,"status":"BLOCKED","source":None,"failure":{"code":getattr(exc,'code','BLOCKED_ENV'),"detail":str(exc)},"AI_VISUAL_REVIEW":"BLOCKED","OWNER_BLIND_REVIEW":"PENDING"}
        print(json.dumps(receipt,ensure_ascii=False));return 3
    argv=sys.argv[1:] if argv is None else argv
    if len(argv)!=1:return 3
    try:
        with stage('main.request'):
            req=json.loads(native_path(argv[0]).read_text(encoding='utf-8'))
    except (OSError,ValueError):return 3
    try:
        with stage('main.binding'):
            assert_m2_execution_allowed(Path(__file__).resolve().parents[3],binding=req.get('runtime_binding') if isinstance(req,dict) else None,purpose='inference')
    except (OSError,ValueError) as exc:
        print(json.dumps({'ok':False,'failure':{'code':getattr(exc,'code','E_PERSONAL_RUNTIME_BINDING'),'detail':str(exc)}}));return 3
    out=Path(req.get('out_receipt',str(req.get('out_png','inference'))+'.receipt.json'))
    if any(native_path(parent/'FROZEN').exists() for parent in out.resolve().parents):
        print(json.dumps({'ok':False,'failure':{'code':'E_FROZEN_OUTPUT','detail':'frozen snapshots are read-only'}}));return 3
    if native_path(out).exists():
        print(json.dumps({'ok':False,'failure':{'code':'E_OUTPUT_EXISTS','detail':'receipt already exists'}}));return 3
    with stage('main.run'):
        code,receipt=run(req)
    with stage('main.receipt'):
        native_path(out.parent).mkdir(parents=True,exist_ok=True)
        with native_path(out).open('x',encoding='utf-8') as f:json.dump(receipt,f,ensure_ascii=False,indent=2)
    print(json.dumps(receipt,ensure_ascii=False));return code


if __name__=='__main__':sys.exit(main())
