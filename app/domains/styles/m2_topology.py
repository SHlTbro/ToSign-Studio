"""Explicit model topology at API, artifact and cached-inference boundaries."""
from engine.style.m2_adapter.topology_dispatch import BASELINE_TOPOLOGY_ID, TOPOLOGY_FIELDS
from engine.style.m2_adapter.recipe_dispatch import validate_recipe_topology_binding

RECIPE_FIELDS = ('recipe_id', 'objective_profile', 'checkpoint_selection')
SCR_FIELDS = ('scr_objective', 'scr_assets', 'scr_data_binding', 'scr_cache_binding', 'scr_preparation_binding', 'windows_cache_identity')
IDENTITY_FIELDS = (*RECIPE_FIELDS, *TOPOLOGY_FIELDS, *SCR_FIELDS)


def validated_topology(record):
    topology=validate_recipe_topology_binding(record)
    from engine.style.m2_adapter.scr_recipe import RECIPE_ID,OBJECTIVE_PROFILE,CHECKPOINT_SELECTION
    if record.get('recipe_id')==RECIPE_ID:
        if record.get('objective_profile')!=OBJECTIVE_PROFILE or record.get('checkpoint_selection')!=CHECKPOINT_SELECTION:
            raise ValueError('Complete SCR objective and checkpoint-selection identity required')
    from engine.style.m2_adapter.user_scr_contract import RECIPE_ID as USER_SCR_RECIPE_ID, OBJECTIVE_PROFILE as USER_SCR_OBJECTIVE
    from engine.style.m2_adapter.windows_training_recipe import RECIPE_ID as WINDOWS_RECIPE
    if record.get('recipe_id') in (USER_SCR_RECIPE_ID,WINDOWS_RECIPE):
        if record.get('objective_profile') != USER_SCR_OBJECTIVE or record.get('checkpoint_selection') != CHECKPOINT_SELECTION:
            raise ValueError('Complete user SCR objective and checkpoint-selection identity required')
    if record.get('recipe_id')==WINDOWS_RECIPE:
        from engine.style.m2_adapter.runtime_variants import value_sha
        cache=record.get('windows_cache_identity')
        if not isinstance(cache,dict) or cache.get('schema')!='windows-user-scr-cache-identity/1' or cache.get('sha256')!=value_sha({k:v for k,v in cache.items() if k!='sha256'}):
            raise ValueError('Complete Windows cache identity required')
    return topology


def validate_topology_lineage(expected, *observed):
    wanted = validated_topology(expected)
    for record in observed:
        if validated_topology(record) != wanted:
            raise ValueError('M2 topology differs across configuration/artifact/inference')
        if wanted['id']!=BASELINE_TOPOLOGY_ID and record.get('runtime_binding')!=expected.get('runtime_binding'):
            raise ValueError('M2 geometry runtime binding differs across provenance records')
        for key in (*RECIPE_FIELDS,*SCR_FIELDS):
            if key in expected and expected[key] is not None and record.get(key) != expected[key]:
                raise ValueError('M2 ' + key + ' differs across provenance records')
    return wanted


def inference_identity(record):
    validated_topology(record)
    return {key: record[key] for key in IDENTITY_FIELDS
            if key in record and record[key] is not None}
