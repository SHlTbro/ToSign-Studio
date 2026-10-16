"""Derive the explicit product recipe from the immutable verified S03 split."""
import json
from pathlib import Path
from engine.style.dataset.binding import verify_snapshot_binding
from engine.style.dataset.schema import DatasetError
from engine.style.m2_adapter.recipe_dispatch import resolve_selected_recipe

PROJ=Path(__file__).resolve().parents[3]

def canonical_control_config(config, *, require_existing=False):
    if config.get('engine')!='m2':return dict(config)
    if not any(config.get(k) is not None for k in ('snapshot_dir','snapshot_sha256','recipe_id','training_recipe','runtime_binding','adapter_topology_id','adapter_topology','adapter_topology_sha256')):
        if require_existing:raise ValueError('Training cannot start until the draft has a verified S03 snapshot and complete explicit product recipe')
        return dict(config)  # Preserve the existing incomplete-draft CRUD contract; not trainable.
    try:binding=verify_snapshot_binding(config.get('snapshot_dir'),config.get('snapshot_sha256'),base_dir=PROJ)
    except DatasetError as exc:raise ValueError(str(exc)) from exc
    root=Path(binding['snapshot_dir']);assignments=json.loads((root/'split.json').read_text(encoding='utf-8'))['assignments']
    count=sum(side=='train' for side in assignments.values())
    from engine.style.m2_adapter.user_scr_contract import RECIPE_ID as USER_SCR_RECIPE_ID
    from engine.style.m2_adapter.windows_training_recipe import RECIPE_ID as WINDOWS_RECIPE
    if config.get('recipe_id') in (USER_SCR_RECIPE_ID,WINDOWS_RECIPE):
        from engine.style.m2_adapter.user_scr_cache import validate_user_reference_cache
        from engine.style.m2_adapter.user_scr_contract import validate_preparation_binding
        cache = validate_user_reference_cache(config.get('scr_asset_paths'), binding, verify_assets=False)
        if (cache['data_binding'] != config.get('scr_data_binding')
                or cache['cache_binding'] != config.get('scr_cache_binding')):
            raise ValueError('STALE_CACHE: immutable run data/cache binding differs from fresh references')
        validate_preparation_binding(config.get('scr_preparation_binding'), cache['data_binding'], cache['cache_binding'])
    resolved=resolve_selected_recipe(count,config)
    for name in ('training_recipe','training_budget'):
        if name in config and config[name]!=resolved[name]:raise ValueError(name+' differs from the verified recipe/split')
    if require_existing and any(config.get(name)!=value for name,value in resolved.items()):
        raise ValueError('Immutable run config does not carry this complete product recipe; create or explicitly update the draft run')
    return {**config,**resolved}
