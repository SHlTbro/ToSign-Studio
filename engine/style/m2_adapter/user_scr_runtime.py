"""Explicit qualification gates for the new generic SCR execution path.

Profile support is separate from qualification. This module accepts only the
capability receipt already validated by the formal immutable registration;
historical receipts and caller allow/ready flags cannot confer qualification.
"""
from pathlib import Path

from .user_scr_contract import (RECIPE_ID, OBJECTIVE_PROFILE, asset_contract, digest,
    file_sha256, validate_user_scr_asset_path_shape, validate_user_scr_provenance)
from .user_scr_recipe import validate_user_scr_binding

FEATURE_PATH_KEYS = ('module', 'checkpoint', 'state_schema', 'font_sans', 'font_serif')


def user_scr_source_hashes(app):
    root = Path(app).resolve()
    paths = (list((root / 'engine').rglob('*.py')) + list((root / 'app/domains/styles').rglob('*.py'))
        + list((root / 'app/domains/render').rglob('*.py')))
    return {p.relative_to(root).as_posix(): file_sha256(p) for p in sorted(paths) if '__pycache__' not in p.parts}


def resolve_user_scr_runtime(app, config, *, purpose='train'):
    """Return a fresh registered runtime only after new-profile qualification."""
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    from .runtime_variants import resolve_binding, specification
    if purpose not in ('prepare', 'train', 'inference'):
        raise ValueError('unsupported user SCR runtime purpose')
    if not isinstance(config, dict) or config.get('recipe_id') != RECIPE_ID:
        raise ValueError('explicit generic user SCR recipe required')
    # This gate is first, before registry probes, asset reads, or ML imports.
    binding = validate_user_scr_binding(config.get('runtime_binding'))
    validate_user_scr_provenance(config)
    resolved = resolve_binding(app, binding, user_scr_purpose=purpose)
    _, spec = specification(app, binding['variant_id'])
    if (spec.get('recipe_id') != RECIPE_ID or spec.get('objective_profile') != OBJECTIVE_PROFILE
            or spec.get('full_model_probe_required') is not True):
        raise ValueError('registered profile does not qualify generic SCR')
    registration = resolved.get('registration', {})
    # resolve_binding/_assess validates the complete stored receipt, fresh
    # environment, exact source/profile, base assets, and feature/font bytes.
    from .user_scr_capability import CAPABILITY_SCHEMA, VARIANT_ID
    proof = registration.get('capability_receipt')
    if (binding['variant_id'] != VARIANT_ID or not isinstance(proof, dict)
            or proof.get('schema') != CAPABILITY_SCHEMA
            or proof.get('source_hashes') != user_scr_source_hashes(app)):
        raise ValueError('new exact-source registered SCR component capability required')
    if purpose != 'inference':
        paths = validate_user_scr_asset_path_shape(config.get('scr_asset_paths'))
        registered = proof.get('scr_feature_asset_paths')
        expected = {key: str(Path(paths[key]).resolve()) for key in FEATURE_PATH_KEYS}
        if not isinstance(registered, dict) or registered != expected:
            raise ValueError('SCR feature/font paths differ from registered consumer-local assets')
    if (config.get('train') or {}).get('device', binding['device']) != binding['device']:
        raise ValueError('generic SCR requested device differs from registered runtime')
    if purpose != 'prepare':
        expected = {'runtime_manifest_path': str(resolved['manifest_path']),
                    'fd_repo': str(resolved['fd_repo']),
                    'content_font_path': str(Path(resolved['fd_repo']) / 'ttf/KaiXinSongA.ttf')}
        for key, wanted in expected.items():
            if not isinstance(config.get(key), str) or Path(config[key]).resolve() != Path(wanted).resolve():
                raise ValueError('generic SCR run path differs from registration: ' + key)
        if (config.get('runtime_identity_sha256') != resolved['asset_identity_sha256']
                or config.get('base_npz_sha256') != resolved['manifest']['base_npz_sha256']):
            raise ValueError('generic SCR base identity differs from registered runtime')
    return resolved


def verify_user_scr_training_evidence(proof,*,core=False,require_preparation=True):
    """Dynamic cache membership with the same exhaustive frozen-state checks."""
    import math,re
    from .user_scr_contract import user_scr_identity
    from .runtime_variants import require,_offset_assertion,_strict_equal,value_sha,SCR_STATE_LAYOUT_SHA256
    if require_preparation:
        identity=user_scr_identity(proof)
    else:
        # A model-core capability predates per-user preparation authority.
        # It still requires the same strict production data/cache provenance.
        from .user_scr_contract import validate_data_binding, validate_cache_binding
        validate_user_scr_provenance(proof)
        data=validate_data_binding(proof.get('scr_data_binding'))
        identity={'scr_data_binding':data,'scr_cache_binding':validate_cache_binding(proof.get('scr_cache_binding'),data)}
    data=identity['scr_data_binding'];cache=identity['scr_cache_binding'];counts=data['counts']
    evidence=proof.get('scr_execution_evidence')
    require(isinstance(evidence,dict) and evidence.get('scr_data_binding')==data and evidence.get('scr_cache_binding')==cache,'E_SCR_PROVENANCE','actual generic SCR execution data/cache differs')
    require(proof.get('dataset_snapshot_sha256')==data['snapshot_sha256'],'E_SCR_PROVENANCE','generic SCR receipt snapshot differs')
    for path in ('scr_feature_state.weights_only','scr_feature_state.strict_load','scr_feature_state.all_finite',
                 'scr_feature_state.eval','scr_feature_state.all_parameters_frozen','scr_feature_state.parameter_gradients_absent',
                 'scr_feature_state.equal','scr_cached_reference_proof.pixel_hashes_checked','scr_rng_preserved'):
        _offset_assertion(proof,path,True)
    for path,wanted in {'scr_feature_state.state_keys':195,'scr_cached_reference_proof.rows':counts['total'],
                        'scr_cached_reference_proof.train_rows':counts['train'],'scr_cached_reference_proof.val_rows':counts['val'],
                        'scr_cached_reference_proof.negative_count':2,
                        'scr_cached_reference_proof.positive_negative_forward_calls':0}.items():
        _offset_assertion(proof,path,wanted)
    state=proof.get('scr_feature_state')
    require(isinstance(state,dict),'E_SCR_FEATURE_STATE','frozen SCR feature state evidence missing')
    before=state.get('before_sha256');after=state.get('after_sha256')
    require(isinstance(before,str) and re.fullmatch('[0-9a-f]{64}',before) is not None and before==after,'E_SCR_FEATURE_STATE','SCR feature state must remain byte-identical')
    evidence=proof.get('scr_execution_evidence')
    require(isinstance(evidence,dict) and evidence.get('schema')=='m2-scr2n-user-objective-evidence/1' and evidence.get('synthetic_test_only') is False,'E_SCR_FEATURE_STATE','complete actual SCR execution evidence required')
    require(all(_strict_equal(evidence.get(key),proof.get(key)) for key in ('scr_feature_state','scr_cached_reference_proof','scr_rng_preserved')),'E_SCR_FEATURE_STATE','projected SCR evidence disagrees with its source record')
    states=[evidence.get('model_state_before'),evidence.get('model_state_after')]
    require(all(isinstance(record,dict) and len(record)==195 for record in states) and _strict_equal(*states),'E_SCR_FEATURE_STATE','all 195 SCR state records must remain identical')
    require(all(value_sha(record)==before for record in states),'E_SCR_FEATURE_STATE','SCR state digests do not recompute from full state records')
    require(evidence.get('full_state_sha256')==before,'E_SCR_FEATURE_STATE','full SCR state digest differs')
    observed=evidence.get('checkpoint_observed_state')
    require(isinstance(observed,dict) and set(observed)==set(states[0]),'E_SCR_FEATURE_STATE','exact observed checkpoint state keys required')
    layout={}
    for name,row in states[0].items():
        require(isinstance(name,str) and bool(name) and isinstance(row,dict) and set(row)=={'shape','dtype','sha256'},'E_SCR_FEATURE_STATE','typed named SCR state records required')
        shape=row['shape']
        require(isinstance(shape,list) and all(type(size) is int and size>=0 for size in shape) and row['dtype'] in ('torch.float32','torch.int64'),'E_SCR_FEATURE_STATE','SCR tensor dimensions/dtype differ')
        require(isinstance(row['sha256'],str) and re.fullmatch('[0-9a-f]{64}',row['sha256']) is not None,'E_SCR_FEATURE_STATE','SCR tensor digest is malformed')
        checkpoint=observed[name]
        require(isinstance(checkpoint,dict) and set(checkpoint)=={'shape','dtype','numel','finite','layout'},'E_SCR_FEATURE_STATE','complete observed SCR checkpoint tensor records required')
        require(_strict_equal(checkpoint['shape'],shape) and checkpoint['dtype']==row['dtype'].removeprefix('torch.') and type(checkpoint['numel']) is int and checkpoint['numel']==math.prod(shape) and checkpoint['finite'] is True and checkpoint['layout']=='torch.strided','E_SCR_FEATURE_STATE','observed SCR checkpoint tensor differs from unchanged state')
        layout[name]={key:checkpoint[key] for key in ('shape','dtype','numel')}
    require(value_sha(layout)==SCR_STATE_LAYOUT_SHA256,'E_SCR_FEATURE_STATE','SCR tensor names/shapes/dtypes differ from the pinned 195-key static layout')
    for key,wanted in {'weights_only':True,'map_location':'cpu','partial_load':False,'prefix_stripping':False,
                       'frozen_eval':True,'trainable_parameter_count':0,'reference_feature_forwards':0,
                       'reference_tensors_gradient_absent':True,'generated_feature_cache_lookups':0,
                       'rng_preserved_during_construction':True,'feature_rng_unchanged':True,
                       'registered_in_wrapper_parameters_or_state':False}.items():
        require(_strict_equal(evidence.get(key),wanted),'E_SCR_FEATURE_STATE','SCR loading/freezing invariant differs: '+key)
    snapshot=evidence.get('snapshot_binding')
    require(isinstance(snapshot,dict) and snapshot.get('snapshot_sha256')==data['snapshot_sha256'],'E_SCR_PROVENANCE','nested SCR snapshot differs from canonical reference cache')
    for key in ('snapshot','snapshot_binding'):
        if key in proof:require(_strict_equal(proof[key],snapshot),'E_SCR_PROVENANCE','nested SCR snapshot differs from its parent receipt')
    cache_hashes=evidence.get('cache_tensor_sha256')
    require(isinstance(cache_hashes,list) and len(cache_hashes)==2 and all(isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None for value in cache_hashes),'E_SCR_FEATURE_STATE','both detached cache tensor digests required')
    calls=evidence.get('completed_loss_calls');rows=evidence.get('reference_row_lookups')
    require(all(isinstance(record,dict) and set(record)=={'train','val'} and all(type(value) is int and value>=0 for value in record.values()) for record in (calls,rows)),'E_SCR_EXECUTION_COUNTS','typed nonnegative train/val execution totals required')
    require(calls['train']>0 and rows['train']==2*calls['train'] and (calls['val']==0 and rows['val']==0 or calls['val']>0 and calls['val']<=rows['val']<=2*calls['val']),'E_SCR_EXECUTION_COUNTS','SCR phase rows must agree with actual batch calls')
    totals={'prediction_feature_forwards':sum(calls.values()),'prediction_feature_images':sum(rows.values()),
            'positive_vector_lookups':4*sum(rows.values()),'negative_vector_lookups':8*sum(rows.values())}
    for key,wanted in totals.items():
        require(type(evidence.get(key)) is int and evidence[key]==wanted,'E_SCR_EXECUTION_COUNTS','SCR prediction/reference totals differ: '+key)
    checks=proof['scr_cached_reference_proof'].get('pixel_hash_check_count')
    require(type(checks) is int and checks>0 and checks==totals['prediction_feature_images'],'E_SCR_EXECUTION_COUNTS','positive pixel checks must equal all actual prediction images')
    if core:
        require(_strict_equal(calls,{'train':2,'val':0}) and _strict_equal(rows,{'train':4,'val':0}),'E_SCR_EXECUTION_COUNTS','core proof requires exactly two train calls and four prediction images')

