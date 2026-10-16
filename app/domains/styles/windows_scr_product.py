"""Owned Windows SCR preparation/config chain; ID-only HTTP, separate cache scope."""
import copy,json,re
from pathlib import Path
from app.core.storage import storage_root,ensure_within_root
from engine.style.m2_adapter.user_scr_contract import sealed,require_sealed,validate_data_binding,validate_cache_binding
from engine.style.m2_adapter.windows_training_recipe import RECIPE_ID,plan_training_recipe,select_training_registration
from engine.style.m2_adapter.windows_user_scr import VARIANTS,cache_identity
from .user_scr_product import UserSCRProductError,_owner_samples,CLIENT_FIELDS
PROJ=Path(__file__).resolve().parents[3]

def cache_root(user,style):
    root=storage_root()
    return ensure_within_root(root/'users'/str(user)/'styles/wsc'/str(style),root)
def _record(user,style,pid):
    if not isinstance(pid,str) or re.fullmatch('[0-9a-f]{64}',pid) is None:raise UserSCRProductError('PREPARATION_REQUIRED','exact preparation ID required')
    root=storage_root();return ensure_within_root(root/'users'/str(user)/'styles/windows-scr-preparations'/str(style)/(pid+'.json'),root)
def plan(snapshot,sha,root,assets,selected):
    from engine.style.m2_adapter.runtime_variants import make_binding
    from engine.style.m2_adapter.user_scr_cache import inspect_user_snapshot
    from .user_scr_preparation import plan_user_scr_preparation
    data=inspect_user_snapshot(snapshot,sha,ledger_path=assets['source_ledger'])['data_binding']
    binding=make_binding(selected);identity=cache_identity(PROJ,binding['variant_id'],binding['environment_identity_sha256'],sha)
    # Full identities remain sealed in the record; a compact 128-bit combined
    # scope keeps consumer paths usable under Windows' legacy MAX_PATH.
    from engine.style.m2_adapter.runtime_variants import value_sha
    scope=value_sha({'runtime_binding':binding,'windows_cache_identity':identity})[:32]
    scoped=Path(root)/scope
    result=plan_user_scr_preparation(snapshot,sha,cache_root=scoped,server_assets=assets)
    if result.get('scr_data_binding')!=data:raise UserSCRProductError('STALE_CACHE','Windows canonical preparation data differs')
    recipe=plan_training_recipe(data['counts']['train'],{'train':{'device':binding['device']}})
    result.update(windows_cache_identity=identity,windows_runtime_binding=binding,
        training_recipe=recipe['training_recipe'],training_budget=recipe['training_budget'])
    return result
def register(session,user,style,snapshot_binding,asset_paths,runtime_binding):
    from engine.style.m2_adapter.windows_training_runtime import resolve
    from .user_scr_jobs import _owned_style
    _owned_style(session,user,style);selected=resolve(PROJ,runtime_binding,purpose='prepare')
    assets={k:asset_paths[k] for k in ('module','checkpoint','state_schema','font_sans','font_serif','source_ledger')}
    result=plan(snapshot_binding['snapshot_dir'],snapshot_binding['snapshot_sha256'],cache_root(user,style),assets,selected)
    if not result.get('cache_reusable') or result['cache_asset_paths']!=asset_paths:raise UserSCRProductError('STALE_CACHE','exact verified Windows reference cache required')
    owners=_owner_samples(session,user,style,result['scr_data_binding'])
    record=sealed({'schema':'windows-user-scr-prepared-record/1','user_id':user,'style_id':style,'recipe_id':RECIPE_ID,
        'snapshot_binding':snapshot_binding,'scr_data_binding':result['scr_data_binding'],'scr_cache_binding':result['scr_cache_binding'],
        'scr_asset_paths':asset_paths,'runtime_binding':runtime_binding,'windows_cache_identity':result['windows_cache_identity'],
        'owner_samples':owners,'m2_launch_authorized':False})
    p=_record(user,style,record['sha256']);p.parent.mkdir(parents=True,exist_ok=True)
    if p.exists():
        if json.loads(p.read_text(encoding='utf-8'))!=record:raise UserSCRProductError('STALE_CACHE','immutable preparation changed')
    else:
        with p.open('x',encoding='utf-8') as f:json.dump(record,f,sort_keys=True,ensure_ascii=False,allow_nan=False)
    return {'preparation_id':record['sha256'],'snapshot_sha256':snapshot_binding['snapshot_sha256'],'style_id':style,'cache_prepared':True,'m2_launch_authorized':False}
def read(session,user,style,pid,snapshot_sha):
    from .user_scr_jobs import _owned_style
    from engine.style.m2_adapter.windows_training_runtime import resolve
    _owned_style(session,user,style)
    try:r=json.loads(_record(user,style,pid).read_text(encoding='utf-8'));require_sealed(r,'windows-user-scr-prepared-record/1')
    except FileNotFoundError as exc:raise UserSCRProductError('PREPARATION_REQUIRED','owned Windows preparation required') from exc
    if r['sha256']!=pid or r['user_id']!=user or r['style_id']!=style or r['snapshot_binding']['snapshot_sha256']!=snapshot_sha or r.get('m2_launch_authorized') is not False:
        raise UserSCRProductError('STALE_CACHE','Windows preparation belongs to another owner/snapshot')
    selected=resolve(PROJ,r['runtime_binding'],purpose='prepare');assets={k:r['scr_asset_paths'][k] for k in ('module','checkpoint','state_schema','font_sans','font_serif','source_ledger')}
    result=plan(r['snapshot_binding']['snapshot_dir'],snapshot_sha,cache_root(user,style),assets,selected)
    for k in ('scr_data_binding','scr_cache_binding','windows_cache_identity'):
        if result.get(k)!=r[k]:raise UserSCRProductError('STALE_CACHE','Windows prepared '+k+' changed')
    if not result.get('cache_reusable') or result['cache_asset_paths']!=r['scr_asset_paths'] or _owner_samples(session,user,style,r['scr_data_binding'])!=r['owner_samples']:
        raise UserSCRProductError('STALE_CACHE','Windows cache bytes/current owner review changed')
    return r
def canonical(session,user,config,sample_ids,*,require_existing=False):
    from .user_scr_jobs import _owned_style
    from engine.style.m2_adapter.runtime_variants import make_binding
    request=config.get('user_scr_request') if require_existing else config
    if not isinstance(request,dict) or set(request)-CLIENT_FIELDS or request.get('recipe_id')!=RECIPE_ID or request.get('engine')!='m2' or type(request.get('style_id')) is not int:
        raise UserSCRProductError('PREPARATION_REQUIRED','ID-only Windows SCR selection required')
    _owned_style(session,user,request['style_id']);selected,binding=select_training_registration(PROJ,request.get('runtime_registration_id'))
    if binding['variant_id'] not in VARIANTS:raise UserSCRProductError('RUNTIME_UNQUALIFIED','Windows CPU training registration required')
    r=read(session,user,request['style_id'],request.get('preparation_id'),request.get('snapshot_sha256'))
    if r['runtime_binding']!=binding:raise UserSCRProductError('STALE_CACHE','preparation training registration changed')
    ids=sorted(row['style_sample_id'] for row in r['owner_samples'])
    if (require_existing and sample_ids!=ids) or (not require_existing and sample_ids and sorted(sample_ids)!=ids):raise UserSCRProductError('STALE_CACHE','locked Windows sample selection changed')
    prep=sealed({'schema':'m2-scr2n-user-preparation-binding/1','user_id':user,'style_id':request['style_id'],
        'snapshot_sha256':request['snapshot_sha256'],'data_binding_sha256':r['scr_data_binding']['sha256'],
        'cache_binding_sha256':r['scr_cache_binding']['sha256'],'preparation_id':r['sha256']})
    from engine.style.m2_adapter.spatial_topology import topology_fields
    from engine.style.m2_adapter.user_scr_contract import asset_contract,objective_contract
    result={**copy.deepcopy(request),'user_scr_request':copy.deepcopy(request),'snapshot_dir':r['snapshot_binding']['snapshot_dir'],
        'runtime_binding':binding,'runtime_manifest_path':selected['manifest_path'],'runtime_identity_sha256':selected['asset_identity_sha256'],
        'fd_repo':selected['fd_repo'],'content_font_path':str(Path(selected['fd_repo'])/'ttf/KaiXinSongA.ttf'),'base_npz_sha256':selected['manifest']['base_npz_sha256'],
        'scr_objective':objective_contract(),'scr_assets':asset_contract(),'scr_data_binding':r['scr_data_binding'],
        'scr_cache_binding':r['scr_cache_binding'],'scr_preparation_binding':prep,'scr_asset_paths':r['scr_asset_paths'],
        'windows_cache_identity':r['windows_cache_identity'],'train':{'device':binding['device'],**request.get('train',{})},**topology_fields()}
    from .m2_product_config import canonical_control_config
    result=canonical_control_config(result)
    if require_existing and any(config.get(k)!=v for k,v in result.items()):raise UserSCRProductError('STALE_CACHE','immutable Windows run config changed')
    return result,ids
