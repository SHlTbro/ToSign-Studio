"""Server-side, inference-only source admission for the frozen Linux terminal.

Inference execution requires a private authenticated independent qualification.
Admission requires the original byte-pinned artifact manifest; it cannot
reconstruct one from the terminal receipt. Training validators remain unchanged.
"""
from pathlib import Path
import copy
import json
from .runtime_variants import value_sha
from .runtime import sha
from .user_scr_contract import RECIPE_ID, sealed, require_sealed

ADAPTER_SHA='3621a4ac41587ce10892bfb080a4658457607726b8aab834b9bb527a963e2097'
MANIFEST_SHA='fc53c54198eaee8edab81733cccf6fbca3dabae45f8c3d9a3060bb6d90137dc2'
RECEIPT_SHA='a3cb602c4a12e1f3621a953fc934e4ee247d9e4452512a421d1a520d43492cf4'
TRAINING_BINDING_SHA='fdce4b0aa30c697808c59d9cfe9387765e66221b59f69d9ab627b4a750376c7c'
SNAPSHOT_SHA='b994c14347ba267cb45e5af0b3e6c073ba7154bc345c9f2115b7e13500e347b8'
OLD0178_MANIFEST_SHA='9bbefed5c2c845720a11423e0be241a5b30702a89818c67c1f1c4ffd474e40f2'
OLD0178_ADAPTER_SHA='017881442b7f8b909f7d8d8c4c5e7ad1893d0caa3c3267ae3c793a9a96175f81'
OLD0178_RECEIPT_SHA='cfd449986cf1faebcbdf1f154d8cc5960aaa4c2f888d15d375e63e9316b6da1d'
OLD0178_BINDING_SHA='74605a20ccf7afaa8b7a6c1e50f59d855a3c40dda48b93361de76481f620f31e'
OLD0178_VARIANT='windows-cp312-cu128-scr2n-0178-inference-cpu-v1'
SCHEMA='windows-scr-deployment-source/1'
DEPLOYMENT_SCHEMA='windows-scr-inference-deployment/1'
BINDING_SCHEMA='windows-scr-inference-deployment-binding/1'
IDENTITY_FIELDS=('recipe_id','objective_profile','checkpoint_selection','adapter_topology_id',
    'adapter_topology','adapter_topology_sha256','preprocessing','training_objective','training_recipe','training_budget',
    'scr_objective','scr_assets','scr_data_binding','scr_cache_binding','scr_preparation_binding')


def identity(manifest_sha=MANIFEST_SHA):
    if manifest_sha==OLD0178_MANIFEST_SHA:
        from .scr_recipe import RECIPE_ID as recipe
        return {'artifact_manifest_sha256':manifest_sha,'adapter_sha256':OLD0178_ADAPTER_SHA,
                'training_receipt_sha256':OLD0178_RECEIPT_SHA,'training_binding_sha256':OLD0178_BINDING_SHA,
                'recipe_id':recipe,'variant_id':OLD0178_VARIANT}
    if manifest_sha==MANIFEST_SHA:
        from .windows_scr_execution import CPU
        return {'artifact_manifest_sha256':manifest_sha,'adapter_sha256':ADAPTER_SHA,
                'training_receipt_sha256':RECEIPT_SHA,'training_binding_sha256':TRAINING_BINDING_SHA,
                'recipe_id':RECIPE_ID,'variant_id':CPU}
    raise ValueError('E_DEPLOYMENT_MANIFEST')


def _pinned(path, expected, label):
    path=Path(path).absolute()
    if any(p.is_symlink() or p.is_junction() for p in (path,*path.parents)):raise ValueError('E_DEPLOYMENT_SOURCE_SYMLINK')
    if not path.is_file():raise ValueError('E_DEPLOYMENT_SOURCE_MISSING: '+label+'; required SHA256 '+expected)
    if sha(path)!=expected:raise ValueError('E_DEPLOYMENT_SOURCE_CHANGED: '+label)
    return path


def admit_source(run_dir, artifact_manifest_path):
    """Derive source identity from trusted original bytes, never caller assertions."""
    root=Path(run_dir).absolute()
    manifest_path=Path(artifact_manifest_path).absolute()
    pin=identity(OLD0178_MANIFEST_SHA if manifest_path.is_file() and sha(manifest_path)==OLD0178_MANIFEST_SHA else MANIFEST_SHA)
    manifest=_pinned(manifest_path,pin['artifact_manifest_sha256'],'original artifact/artifact.json')
    if manifest!=root.resolve()/'artifact/artifact.json':raise ValueError('E_DEPLOYMENT_MANIFEST_PATH')
    adapter=_pinned(root/'artifact/m2_adapter.pt',pin['adapter_sha256'],'terminal adapter')
    receipt_path=_pinned(root/'m2-receipt.json',pin['training_receipt_sha256'],'original m2 receipt')
    receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
    training=receipt.get('runtime_binding',{})
    if training.get('binding_sha256')!=pin['training_binding_sha256'] or value_sha({k:v for k,v in training.items() if k!='binding_sha256'})!=pin['training_binding_sha256']:
        raise ValueError('E_DEPLOYMENT_TRAINING_BINDING')
    if (receipt.get('recipe_id')!=pin['recipe_id'] or receipt.get('dataset_snapshot_sha256')!=SNAPSHOT_SHA
        or receipt.get('model_version')!='adapter@'+pin['adapter_sha256'][:16]
        or receipt.get('adapter_selection',{}).get('global_step')!=200
        or receipt.get('adapter_selection',{}).get('kind')!='terminal_state'
        or receipt.get('n_hook_points')!=22 or receipt.get('n_spatial_offset_heads')!=6):
        raise ValueError('E_DEPLOYMENT_TERMINAL_IDENTITY')
    from .spatial_topology import validate_spatial_topology_fields
    validate_spatial_topology_fields(receipt)
    artifact=json.loads(manifest.read_text(encoding='utf-8'))
    for key in ('runtime_binding','snapshot_binding','recipe_id','training_recipe','training_budget',
                'preprocessing','adapter_topology_sha256','scr_objective','scr_assets'):
        if artifact.get(key)!=receipt.get(key):raise ValueError('E_DEPLOYMENT_MANIFEST_LINEAGE: '+key)
    if artifact.get('base_ckpt_sha256')!=receipt.get('base_ckpt_sha256'):raise ValueError('E_DEPLOYMENT_MANIFEST_BASE')
    return sealed({'schema':SCHEMA,'adapter_path':str(adapter),'adapter_sha256':pin['adapter_sha256'],
      'artifact_manifest_path':str(manifest),'artifact_manifest_sha256':pin['artifact_manifest_sha256'],
      'training_receipt_path':str(receipt_path),'training_receipt_sha256':pin['training_receipt_sha256'],
      'training_runtime_binding':copy.deepcopy(training),'snapshot_sha256':SNAPSHOT_SHA,
      'base_npz_sha256':copy.deepcopy(receipt['base_ckpt_sha256']),
      'runtime_identity_sha256':receipt['runtime_identity_sha256'],
      'model_version':receipt['model_version'],'terminal_updates':200,
      'immutable_training_identity':{k:copy.deepcopy(receipt[k]) for k in IDENTITY_FIELDS if k in receipt},
      'execution_authorized':False,'may_prepare':False,'may_train':False,'may_resume':False,'may_publish':False})


def verify_source(record, *, purpose='inference'):
    if purpose!='inference':raise ValueError('E_DEPLOYMENT_INFERENCE_ONLY')
    require_sealed(record,SCHEMA)
    if record!=admit_source(Path(record['adapter_path']).parents[1],record['artifact_manifest_path']):
        raise ValueError('E_DEPLOYMENT_SOURCE_IDENTITY')
    return record


def verify_metadata(record, meta):
    """The original training binding and recipe are never relabelled Windows."""
    if 'source' in record:record=record['source']
    verify_source(record)
    if meta.get('runtime_binding')!=record['training_runtime_binding']:
        raise ValueError('E_DEPLOYMENT_TRAINING_BINDING')
    for key,value in record['immutable_training_identity'].items():
        if meta.get(key)!=value:raise ValueError('E_DEPLOYMENT_METADATA: '+key)
    if meta.get('snapshot_sha256',meta.get('dataset_snapshot_sha256'))!=record['snapshot_sha256']:
        raise ValueError('E_DEPLOYMENT_METADATA: snapshot')
    if record.get('artifact_manifest_sha256')==OLD0178_MANIFEST_SHA:
        receipt=json.loads(Path(record['training_receipt_path']).read_text(encoding='utf-8'))
        if (meta.get('selection')!=receipt.get('adapter_selection')
            or meta.get('training_environment')!=receipt.get('training_environment')
            or meta.get('base_npz_sha256')!=record['base_npz_sha256']
            or meta.get('content_font_sha256')!='4e11c8d15dcef64e5b55548e5764442d1b1f3be6fc52346f1338af9b48cf19bd'):
            raise ValueError('E_DEPLOYMENT_METADATA: original terminal/base/environment')
        if any(type(meta.get(k)) is not int or meta[k]!=v for k,v in
               (('n_hook_points',22),('n_offset_projection_heads',0),('n_spatial_offset_heads',6))):
            raise ValueError('E_DEPLOYMENT_METADATA: original 56-tensor topology')
    return True


def execution_plan(app, record, variant):
    """Bind a future independent execution proof; no boolean grants admission."""
    verify_source(record)
    if record['artifact_manifest_sha256']==OLD0178_MANIFEST_SHA:
        if variant!=OLD0178_VARIANT:raise ValueError('E_DEPLOYMENT_EXECUTION_MODEL_VARIANT')
        from .windows_scr_execution import profile as inference_profile
        profile=inference_profile(app,variant)
        source_files=profile['source_files']
    else:
        from .windows_user_scr import profile_for_source
        profile=profile_for_source(app,variant)
        source_files=profile['required_source_files']
    return sealed({'schema':'windows-scr-inference-deployment-plan/1','source_sha256':record['sha256'],
      'source_model_version':record['model_version'],'training_runtime_binding_sha256':record['training_runtime_binding']['binding_sha256'],
      'execution_variant_id':variant,'execution_device':profile['device'],
      'execution_profile_sha256':profile['execution_profile_sha256'],
      'dependency_lock_sha256':profile['lock_sha256'],'source_inventory_sha256':value_sha(source_files),
      'purpose':'inference','status':'QUALIFICATION_REQUIRED',
      'execution_authorized':False,'may_prepare':False,'may_train':False,'may_resume':False,'may_publish':False,
      'required_evidence':['server-owned independent interpreter/environment admission',
        'final-source full base + original terminal56 native inference receipt',
        'exact source/execution compatibility receipt and output hashes',
        'deployment-bound request/cache/queued-job integration']})


def is_windows(binding):
    return isinstance(binding,dict) and binding.get('schema')==BINDING_SCHEMA


def _root(app):return Path(app).resolve()/'runtime/windows-scr-inference'


def _compatibility(source,execution,snapshot_binding):
    report=execution['capability']['report']
    return sealed({'schema':'windows-scr-inference-compatibility/1','source_sha256':source['sha256'],
      'training_runtime_binding_sha256':source['training_runtime_binding']['binding_sha256'],
      'execution_binding_sha256':execution['runtime_binding']['binding_sha256'],
      'capability_receipt_sha256':execution['runtime_binding']['capability_receipt_sha256'],
      'adapter_sha256':source['adapter_sha256'],'artifact_manifest_sha256':source['artifact_manifest_sha256'],'snapshot_binding':snapshot_binding,
      'base_npz_sha256':source['base_npz_sha256'],'content_font_sha256':report['content_font_sha256'],
      'topology_sha256':source['immutable_training_identity']['adapter_topology_sha256'],
      'preprocessing_sha256':value_sha(source['immutable_training_identity']['preprocessing']),
      'original_metadata_sha256':report['original_metadata_sha256'],
      'execution_environment_identity_sha256':report['environment']['identity_sha256'],
      'source_inventory_sha256':value_sha(report['profile']['source_files']),
      'native_raw_rgb_sha256':report['output']['raw_rgb_sha256'],'native_alpha_sha256':report['output']['alpha_sha256'],
      'scope':'inference','may_prepare':False,'may_train':False,'may_resume':False,'may_publish':False,
      'quality_evaluation_ported':False,'claims_quality_pass':False})


def register_deployment(app,run_dir,artifact_manifest_path,snapshot_dir,execution_binding):
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    from . import windows_scr_execution as E
    from engine.style.dataset.binding import verify_snapshot_binding
    source=admit_source(run_dir,artifact_manifest_path)
    execution=E.resolve(app,execution_binding)
    if execution_binding['variant_id']!=identity(source['artifact_manifest_sha256'])['variant_id']:raise ValueError('E_DEPLOYMENT_EXECUTION_MODEL')
    snapshot=verify_snapshot_binding(snapshot_dir,SNAPSHOT_SHA)
    if snapshot!=execution['capability']['report']['snapshot_binding']:raise ValueError('E_DEPLOYMENT_EXECUTION_SNAPSHOT')
    receipt=json.loads(Path(source['training_receipt_path']).read_text(encoding='utf-8'))
    descriptor=sealed({'schema':DEPLOYMENT_SCHEMA,'source':source,'runtime_binding':execution_binding,
      'training_runtime_binding':source['training_runtime_binding'],'training_snapshot_binding':receipt['snapshot_binding'],
      'snapshot_binding':snapshot,'adapter_sha256':source['adapter_sha256'],'manifest_sha256':source['artifact_manifest_sha256'],
      'run_dir':str(Path(run_dir).resolve()),'source_inventory_sha256':value_sha(E.inventory(app)),
      'compatibility_receipt':_compatibility(source,execution,snapshot),'scope':'inference',
      'may_prepare':False,'may_train':False,'may_resume':False,'may_publish':False,'claims_quality_pass':False})
    E._write(_root(app)/'deployments'/descriptor['sha256']/'descriptor.json',descriptor)
    pointer=_root(app)/'active'/(source['artifact_manifest_sha256']+'.json');selection={'deployment_sha256':descriptor['sha256']}
    if pointer.exists() and E._read(pointer)!=selection:
        import os,uuid
        old=E._read(pointer)
        E._write(_root(app)/'active-history'/(old['deployment_sha256']+'.json'),old)
        temporary=pointer.parent/('selection-'+uuid.uuid4().hex+'.json');E._write(temporary,selection)
        os.replace(temporary,pointer)
    else:E._write(pointer,selection)
    return resolve_deployment(app,reference(descriptor))


def reference(descriptor):
    return {'schema':BINDING_SCHEMA,'manifest_sha256':descriptor['manifest_sha256'],'deployment_sha256':descriptor['sha256'],
      'source_sha256':descriptor['source']['sha256'],'execution_binding_sha256':descriptor['runtime_binding']['binding_sha256'],
      'compatibility_receipt_sha256':descriptor['compatibility_receipt']['sha256'],'scope':'inference'}


def resolve_deployment(app,binding,*,expected_run_dir=None,expected_manifest_sha256=None,purpose='inference',observe=True):
    if isinstance(binding,dict) and binding.get('manifest_sha256')==OLD0178_MANIFEST_SHA:
        from . import personal_runtime_policy as Personal
        policy=Personal.bootstrap(app)
        with Personal._verified_scope(app,policy,'verification'):
            return _resolve_deployment(app,binding,expected_run_dir=expected_run_dir,
                                       expected_manifest_sha256=expected_manifest_sha256,purpose=purpose,observe=observe)
    return _resolve_deployment(app,binding,expected_run_dir=expected_run_dir,
                               expected_manifest_sha256=expected_manifest_sha256,purpose=purpose,observe=observe)


def _resolve_deployment(app,binding,*,expected_run_dir=None,expected_manifest_sha256=None,purpose='inference',observe=True):
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    from . import windows_scr_execution as E
    from engine.style.dataset.binding import verify_snapshot_binding
    if purpose!='inference' or not is_windows(binding) or binding.get('scope')!='inference':raise ValueError('E_DEPLOYMENT_INFERENCE_ONLY')
    import re
    if not re.fullmatch('[0-9a-f]{64}',str(binding.get('deployment_sha256'))):raise ValueError('E_DEPLOYMENT_DIGEST')
    descriptor=E._read(_root(app)/'deployments'/binding['deployment_sha256']/'descriptor.json')
    require_sealed(descriptor,DEPLOYMENT_SCHEMA)
    if reference(descriptor)!=binding:raise ValueError('E_DEPLOYMENT_BINDING_CHANGED')
    source=verify_source(descriptor['source'])
    if descriptor.get('manifest_sha256')!=source['artifact_manifest_sha256'] or descriptor.get('adapter_sha256')!=source['adapter_sha256']:raise ValueError('E_DEPLOYMENT_MANIFEST')
    if expected_manifest_sha256 is not None and expected_manifest_sha256!=source['artifact_manifest_sha256']:raise ValueError('E_DEPLOYMENT_MANIFEST')
    if expected_run_dir is not None and Path(expected_run_dir).resolve()!=Path(descriptor['run_dir']):raise ValueError('E_DEPLOYMENT_RUN_DIR')
    if any(descriptor.get(k) is not False for k in ('may_prepare','may_train','may_resume','may_publish','claims_quality_pass')):raise ValueError('E_DEPLOYMENT_SCOPE')
    execution=E.resolve(app,descriptor['runtime_binding'],observe=observe,purpose=purpose)
    if descriptor['runtime_binding']['variant_id']!=identity(source['artifact_manifest_sha256'])['variant_id']:raise ValueError('E_DEPLOYMENT_EXECUTION_MODEL')
    snapshot=verify_snapshot_binding(descriptor['snapshot_binding']['snapshot_dir'],SNAPSHOT_SHA)
    if descriptor['snapshot_binding']!=snapshot or descriptor['source_inventory_sha256']!=value_sha(E.inventory(app)):raise ValueError('E_DEPLOYMENT_SOURCE_CHANGED')
    if descriptor['compatibility_receipt']!=_compatibility(source,execution,snapshot):raise ValueError('E_DEPLOYMENT_COMPATIBILITY_CHANGED')
    if descriptor['training_runtime_binding']!=source['training_runtime_binding']:raise ValueError('E_DEPLOYMENT_TRAINING_BINDING')
    training=json.loads(Path(source['training_receipt_path']).read_text(encoding='utf-8'))
    return {'descriptor':descriptor,'source':source,'training':training,'run_dir':descriptor['run_dir'],
      'snapshot_binding':snapshot,'runtime_binding':descriptor['runtime_binding'],'deployment_binding':binding,
      'execution':execution,'deployment_version':'deployment@'+descriptor['sha256'][:16],
      'may_train':False,'may_publish':False}


def lookup_deployment(app,run_dir,manifest_sha256):
    from .windows_scr_execution import _read
    if manifest_sha256 not in (MANIFEST_SHA,OLD0178_MANIFEST_SHA):return None
    pointer=_root(app)/'active'/(manifest_sha256+'.json')
    if not pointer.exists():return None
    ref=_read(pointer);descriptor=_read(_root(app)/'deployments'/ref['deployment_sha256']/'descriptor.json')
    return resolve_deployment(app,reference(descriptor),expected_run_dir=run_dir,expected_manifest_sha256=manifest_sha256)


def verify_request(resolved,*,adapter_path,adapter_sha256,snapshot_dir,snapshot_sha256,runtime_binding):
    source=resolved['source']
    if (Path(adapter_path).resolve()!=Path(source['adapter_path']).resolve() or adapter_sha256!=source.get('adapter_sha256',ADAPTER_SHA)
        or Path(snapshot_dir).resolve()!=Path(resolved['snapshot_binding']['snapshot_dir']).resolve()
        or snapshot_sha256!=SNAPSHOT_SHA or runtime_binding!=resolved['runtime_binding']):raise ValueError('E_DEPLOYMENT_REQUEST')


def receipt_fields(resolved):
    d=resolved['descriptor']
    return {'deployment_binding':resolved['deployment_binding'],'deployment_version':resolved['deployment_version'],
      'training_runtime_binding':d['training_runtime_binding'],'training_snapshot_binding':d['training_snapshot_binding'],
      'deployment_source_sha256':d['source']['sha256'],'compatibility_receipt_sha256':d['compatibility_receipt']['sha256'],
      'execution_source_inventory_sha256':d['source_inventory_sha256'],'execution_purpose':'inference',
      'deployment_may_publish':False,'deployment_claims_quality_pass':False}


def verify_execution_receipt(resolved,receipt):
    from .windows_scr_execution import measure
    if receipt.get('runtime_binding')!=resolved['runtime_binding']:raise ValueError('E_DEPLOYMENT_EXECUTION_BINDING')
    if receipt.get('runtime_environment')!=resolved['execution']['environment']:raise ValueError('E_DEPLOYMENT_EXECUTION_ENVIRONMENT')
    if receipt.get('inference_environment')!=resolved['execution']['environment']['numerical_environment']:raise ValueError('E_DEPLOYMENT_EXECUTION_ENVIRONMENT')
    if receipt.get('training_environment')!=resolved['training']['training_environment']:raise ValueError('E_DEPLOYMENT_TRAINING_ENVIRONMENT')
    adapter_sha=resolved['source']['adapter_sha256']
    if receipt.get('model_version')!='adapter@'+adapter_sha[:16] or receipt.get('adapter_sha256')!=adapter_sha:raise ValueError('E_DEPLOYMENT_MODEL_VERSION')
    for k,v in resolved['source']['immutable_training_identity'].items():
        if receipt.get(k)!=v:raise ValueError('E_DEPLOYMENT_ORIGINAL_RECIPE: '+k)
    from .topology_dispatch import validate_inference_topology_execution
    validate_inference_topology_execution(receipt)
    return True


def verify_receipt(resolved,receipt):
    if any(receipt.get(k)!=v for k,v in receipt_fields(resolved).items()):raise ValueError('E_DEPLOYMENT_RECEIPT_FIELDS')
    if receipt.get('snapshot_binding')!=resolved['snapshot_binding']:raise ValueError('E_DEPLOYMENT_SNAPSHOT')
    return verify_execution_receipt(resolved,receipt)


def validate_lineage(app,record):
    resolved=resolve_deployment(app,record.get('deployment_binding'),observe=False)
    if record.get('runtime_binding')!=resolved['runtime_binding']:raise ValueError('E_DEPLOYMENT_EXECUTION_BINDING')
    for k,value in resolved['source']['immutable_training_identity'].items():
        if k in ('preprocessing','training_objective','training_recipe','training_budget') and k not in record:continue
        if record.get(k)!=value:raise ValueError('E_DEPLOYMENT_RECIPE_LINEAGE: '+k)
    return resolved
