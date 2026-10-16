"""Server-authoritative discovery of verified personal-style runtime + S03."""
import json
import os
from pathlib import Path
from app.core.errors import ApiError
from app.core.storage import storage_root
from .models import StyleProfile
from app.domains.training_sets.models import TrainingSample
from engine.style.dataset.binding import verify_snapshot_binding
from engine.style.dataset.schema import DatasetError
from engine.style.m2_adapter.runtime import verify_runtime,runtime_python
from engine.style.visual_review.policy import load_policy

PROJ=Path(__file__).resolve().parents[3]


def discover(session,user_id,style_id,runtime_registration_id=None,*,recipe_id=None):
    from engine.style.m2_adapter.user_scr_contract import RECIPE_ID as USER_SCR_RECIPE_ID
    from engine.style.m2_adapter.windows_training_recipe import RECIPE_ID as WINDOWS_TRAINING_RECIPE_ID
    if recipe_id == WINDOWS_TRAINING_RECIPE_ID:
        from .windows_scr_training_api import discover as discover_windows
        from .user_scr_product import UserSCRProductError
        try:return discover_windows(session,user_id,style_id,runtime_registration_id)
        except UserSCRProductError as exc:raise ApiError(404 if exc.code=='STYLE_NOT_FOUND' else 409,exc.code,str(exc)) from exc
    if recipe_id == USER_SCR_RECIPE_ID:
        from .user_scr_product import discover_user_scr
        return discover_user_scr(session,user_id,style_id,runtime_registration_id)
    if recipe_id not in (None, 'm2-control-corrected-v1'):
        raise ApiError(400,'INVALID_REQUEST','Unsupported explicit recipe discovery request')
    profile=session.query(StyleProfile).filter(StyleProfile.user_id==user_id,StyleProfile.style_id==style_id).first()
    if profile is None:raise ApiError(404,'STYLE_NOT_FOUND','Style not found')
    blockers=[];runtime=None;snapshots=[];selected_binding=None;selected_device='cpu'
    from engine.style.m2_adapter.runtime_variants import inspect_registration,make_binding,discover_installed
    runtime_options=discover_installed(PROJ)
    if profile.source_type!='HANDWRITING_ASSET' or profile.status!='approved':
        blockers.append({'code':'STYLE_NOT_APPROVED','detail':'M2 requires an approved, owned handwriting profile'})
    try:
        from engine.style.m2_adapter.release_runtime_policy import reject_execution
        from engine.style.m2_adapter.runtime_variants import CPU
        reject_execution(PROJ,CPU)
        if runtime_registration_id:
            selected=inspect_registration(PROJ,runtime_registration_id)
            if not selected.get('ready'):raise ValueError(selected.get('reason','selected runtime is unavailable'))
            selected_binding=make_binding(selected);selected_device=selected['device']
            runtime={'manifest_path':selected['manifest_path'],'identity_sha256':selected['asset_identity_sha256'],'fd_repo':selected['fd_repo'],'manifest':selected['manifest'],'license_scope':'private noncommercial research only; no redistribution clearance'}
        else:
            runtime=verify_runtime()
            if not runtime_python().is_file():raise ValueError('ML interpreter missing from runtime addon')
    except (OSError,ValueError,KeyError) as exc:blockers.append({'code':getattr(exc,'code','RUNTIME_UNAVAILABLE'),'detail':str(exc)})
    roots=[storage_root()/'users'/str(user_id)/'styles/snapshots',PROJ/'artifacts/style-datasets']
    if os.environ.get('YY_M2_SNAPSHOT_ROOT'):roots.insert(0,Path(os.environ['YY_M2_SNAPSHOT_ROOT']))
    for root in roots:
        if not root.is_dir():continue
        for candidate in sorted(root.iterdir())[:200]:
            if not candidate.is_dir() or not (candidate/'index.json').is_file():continue
            try:
                idx=json.loads((candidate/'index.json').read_text(encoding='utf-8'))
                if str(idx.get('style_id'))!=str(style_id):continue
                binding=verify_snapshot_binding(candidate,idx.get('snapshot_sha256'))
                rows=[json.loads(l) for l in (candidate/'samples.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
                assignments=json.loads((candidate/'split.json').read_text(encoding='utf-8'))['assignments']
                if len({r['label'] for r in rows})<20 or sum(v=='train' for v in assignments.values())<8 or sum(v=='val' for v in assignments.values())<2:continue
                snapshots.append((str(idx.get('created_at','')),candidate,binding,rows))
            except (OSError,ValueError,KeyError,TypeError,AttributeError,DatasetError):continue
    if not snapshots:blockers.append({'code':'SNAPSHOT_UNAVAILABLE','detail':'No fully verified same-style S03 meeting unchanged data minima; prepare a reviewed snapshot first'})
    from engine.style.dataset.ledger_diagnostics import source_dependency_status
    dependencies=source_dependency_status(PROJ)
    policy=load_policy(user_id)
    acceptance={'mode':'AI_WITH_EXPLICIT_USER_DELEGATION' if policy else 'OWNER_REQUIRED',
                'human_blind_review':policy['human_blind_review']['mode'] if policy else 'REQUIRED',
                'candidate_confirmation':policy['candidate_confirmation']['mode'] if policy else 'OWNER_REQUIRED'}
    if policy:acceptance['policy_sha256']=policy['_sha256']
    from engine.style.visual_review import pipeline
    general_ai=pipeline._REVIEWER is not None and getattr(pipeline._REVIEWER,'provider_mode','')!='BOUND_OFFLINE_SESSION_EVIDENCE'
    ready=not blockers;config=None;sample_ids=[];snapshot_info=None
    if snapshots:
        _,folder,binding,rows=max(snapshots,key=lambda x:x[0]);ids=[r['sample_id'] for r in rows]
        linked=session.query(TrainingSample).filter(TrainingSample.user_id==user_id,TrainingSample.style_id==str(style_id),TrainingSample.sample_id.in_(ids)).all()
        sample_ids=sorted({r.style_sample_id for r in linked if r.style_sample_id is not None})
        snapshot_info={'snapshot_id':binding['snapshot_id'],'snapshot_sha256':binding['snapshot_sha256'],
                       'sample_count':len(rows),'distinct_labels':len({r['label'] for r in rows})}
        if runtime:
            config={'engine':'m2','style_id':style_id,'snapshot_dir':str(folder.resolve()),'snapshot_sha256':binding['snapshot_sha256'],
                    'runtime_manifest_path':runtime['manifest_path'],'runtime_identity_sha256':runtime['identity_sha256'],
                    'fd_repo':runtime['fd_repo'],'content_font_path':str(Path(runtime['fd_repo'])/'ttf/KaiXinSongA.ttf'),
                    'base_npz_sha256':runtime['manifest']['base_npz_sha256'],
                    'train':{'device':selected_device}}
            if selected_binding:
                config['runtime_binding']=selected_binding
                from engine.style.m2_adapter.runtime_variants import OFFSET_CPU, SPATIAL_CPU, SCR_CPU
                if selected_binding['variant_id']==OFFSET_CPU:
                    from engine.style.m2_adapter.offset_recipe import RECIPE_ID as OFFSET_RECIPE_ID
                    from engine.style.m2_adapter.adapter_topology import topology_fields,OFFSET_TOPOLOGY_ID
                    config.update(recipe_id=OFFSET_RECIPE_ID,**topology_fields(OFFSET_TOPOLOGY_ID))
                elif selected_binding['variant_id']==SPATIAL_CPU:
                    from engine.style.m2_adapter.spatial_recipe import RECIPE_ID as SPATIAL_RECIPE_ID
                    from engine.style.m2_adapter.spatial_topology import topology_fields
                    config.update(recipe_id=SPATIAL_RECIPE_ID,**topology_fields())
                elif selected_binding['variant_id']==SCR_CPU:
                    from engine.style.m2_adapter.scr_recipe import RECIPE_ID as SCR_RECIPE_ID
                    from engine.style.m2_adapter.scr_contract import objective_contract,asset_contract,default_scr_asset_paths
                    from engine.style.m2_adapter.spatial_topology import topology_fields
                    config.update(recipe_id=SCR_RECIPE_ID,**topology_fields(),scr_objective=objective_contract(),
                                  scr_assets=asset_contract(),scr_asset_paths=default_scr_asset_paths())
            from .m2_product_config import canonical_control_config
            try:config=canonical_control_config(config)
            except (OSError,ValueError,KeyError,TypeError) as exc:
                blockers.append({'code':'CONTROL_RECIPE_UNAVAILABLE','detail':str(exc)});ready=False;config=None
            if config is not None:
                if policy:config['visual_thresholds']=policy['visual_thresholds']
    return {'status':'READY' if ready else 'BLOCKED','ready':ready,'config':config,'sample_ids':sample_ids,
            'runtime_options':runtime_options,'selected_runtime_binding':selected_binding,'snapshot':snapshot_info,'snapshot_preparation_dependencies':dependencies,'blockers':blockers,'acceptance_policy':acceptance,
            'capabilities':{'create_m2_run':ready,'run_evaluation':runtime is not None,'ai_visual_review':general_ai,
                            'ai_candidate_confirmation':bool(policy and general_ai and dependencies['status']=='READY'),'offline_ai_review_import':True,'native_candidate_review_jobs':bool(policy and dependencies['status']=='READY'),'native_candidate_evidence_import':'TRUSTED_LOCAL_HASH_PINNED_ONLY','native_candidate_visible_UI':True},
            'provider_mode':'CONFIGURED' if general_ai else 'BLOCKED_PROVIDER',
            'license_scope':runtime['license_scope'] if runtime else 'Runtime addon unavailable'}
