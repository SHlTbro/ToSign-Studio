"""Owned Windows training discovery; readiness requires measured registration."""
import json
from .user_scr_product import UserSCRProductError

def reject_unqualified_launch(session,user_id,config):
    """Explicit boundary for create/update/restart of the new disabled recipe."""
    from engine.style.m2_adapter.windows_training_recipe import RECIPE_ID,INFERENCE_ONLY,select_training_registration
    from engine.style.m2_adapter.windows_user_scr import BLOCKER
    if config.get('recipe_id')!=RECIPE_ID:return
    from .user_scr_jobs import _owned_style
    from .m2_runtime_api import PROJ
    _owned_style(session,user_id,config.get('style_id'))
    try:selected,binding=select_training_registration(PROJ,config.get('runtime_registration_id'))
    except (OSError,ValueError,KeyError,TypeError) as exc:
        code=getattr(exc,'code','RUNTIME_UNQUALIFIED')
        raise UserSCRProductError(code if code in (INFERENCE_ONLY,BLOCKER) else 'RUNTIME_UNQUALIFIED',str(exc)) from exc
    from engine.style.m2_adapter.windows_user_scr import VARIANTS
    if binding['variant_id'] not in VARIANTS:
        raise UserSCRProductError(BLOCKER,'Independent measured Windows device-specific training registration required')

def discover(session,user_id,style_id,runtime_registration_id=None):
    from .user_scr_jobs import _owned_style
    _owned_style(session,user_id,style_id)
    from .m2_runtime_api import discover as source_discovery,PROJ
    from engine.style.m2_adapter import windows_user_scr as W
    from engine.style.m2_adapter.windows_training_recipe import select_training_registration
    from engine.style.m2_adapter.user_scr_contract import asset_contract
    info=source_discovery(session,user_id,style_id)
    blockers=[{'code':W.BLOCKER,'detail':'Independent Windows full SCR prepare/gradient/cache qualification is required'}]
    if runtime_registration_id:
        try:select_training_registration(PROJ,runtime_registration_id)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            blockers.insert(0,{'code':getattr(exc,'code','RUNTIME_UNQUALIFIED'),'detail':str(exc)})
    result={'status':'BLOCKED','ready':False,'recipe_id':W.RECIPE_ID,'config':None,'sample_ids':[],
        'snapshot':info.get('snapshot'),'provider_mode':info.get('provider_mode'),
        'dependencies':{'fresh_windows_cache_required':True,'server_asset_config_present':(PROJ/'config/user-scr-assets.json').is_file(),
            'required_fonts':[{'key':key,'file':name,'bytes':size,'sha256':digest} for key,name,size,digest in zip(
                ('font_sans','font_serif'),('NotoSansCJK-Regular.ttc','NotoSerifCJK-Regular.ttc'),
                (19484784,26411360),asset_contract()['font_sha256'])]},
        'acceptance_policy':info.get('acceptance_policy'),'m2_launch_authorized':False,
        'runtime_options':[{'variant_id':v,'device':W.profile_for_source(PROJ,v)['device'],
            'ready':False,'full_model_ready':False,'code':W.BLOCKER,
            'execution_profile_sha256':W.profile_for_source(PROJ,v)['execution_profile_sha256'],
            'training_recipe_sha256':W.profile_for_source(PROJ,v)['training_recipe']['sha256']} for v in sorted(W.VARIANTS)],
        'capabilities':{'prepare_user_scr':False,'create_m2_run':False,'resume_m2_run':False,'may_publish':False},
        'qualification_plan':W.qualification_plan(PROJ,W.CPU),
        'qualification_plans':{v:W.qualification_plan(PROJ,v) for v in sorted(W.VARIANTS)},'blockers':blockers}
    from engine.style.m2_adapter.windows_training_dependencies import inspect_dependencies
    result['installation'] = inspect_dependencies(PROJ)
    result['blockers'].insert(0, {'code': result['installation']['execution_policy']['code'],
        'detail': 'Signed Personal Runtime bootstrap and fresh verified Windows device-specific qualification are required; Base grants no execution.'})
    from engine.style.m2_adapter.runtime_variants import discover_installed
    ready=[r for r in discover_installed(PROJ) if r.get('ready') and r.get('variant_id') in W.VARIANTS]
    result['runtime_options'].extend(ready)
    result['capabilities']['prepare_user_scr']=bool(ready and info.get('snapshot'))
    if runtime_registration_id and any(r.get('registration_id')==runtime_registration_id for r in ready) and info.get('snapshot'):
        result['blockers']=[{'code':'PREPARATION_REQUIRED','detail':'Prepare this owned S03 with the selected qualified Windows device-specific registration'}]
        from .windows_scr_product import canonical,_record
        snapshot=info['snapshot'];snapshot_sha=snapshot.get('snapshot_sha256')
        directory=_record(user_id,style_id,'0'*64).parent
        for path in sorted(directory.glob('*.json'))[:100]:
            try:
                record=json.loads(path.read_text(encoding='utf-8'))
                request={'engine':'m2','recipe_id':W.RECIPE_ID,'style_id':style_id,
                    'snapshot_sha256':snapshot_sha,'runtime_registration_id':runtime_registration_id,
                    'preparation_id':record['sha256']}
                _,ids=canonical(session,user_id,request,[])
                result.update(status='READY',ready=True,config=request,sample_ids=ids,blockers=[])
                result['capabilities']['create_m2_run']=True
                break
            except (OSError,ValueError,KeyError,TypeError):continue
    return result
