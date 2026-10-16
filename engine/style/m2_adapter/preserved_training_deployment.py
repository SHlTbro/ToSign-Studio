"""Narrow admission of the retained GPU2 terminal into independent GPU inference.

Training history is immutable. This descriptor grants inference compatibility only;
quality, owner approval and publication remain the existing product gates.
"""
from pathlib import Path
import copy
import hmac
import hashlib
import json
import math
from datetime import datetime
import re
from .runtime_variants import sha, value_sha, verify_execution_receipt, resolve_binding
from .trainer_resume_protocol import native_path
from .user_scr_contract import sealed, require_sealed
from . import windows_training_runtime as W

MODEL_SHA='8f473ea1a8d9063971ca4603048276f85d49785f0cf42967c034c27fa257ab60'
MANIFEST_SHA='85853e6a4ef418a83704be9141a289d24b09ffdfdcb7fec21ccbf82d6c28a2ee'
RAW_SHA='1dad4d1f7a558235409a53f0d02a044c562996375d509f10953c3b84ebb470b1'
CATALOG_SHA='8aaebf846a2d1c661d4fb6daa67d1125da9d7992f2744bb72bda5dce0928c98e'
SNAPSHOT_SHA='e32b9ab16123172eef1b67349e7cd420374e3c1fa726c0811ea6966cef8d2ad9'
REGISTRATION='ecece45bd43504c0596c95f99daadcf6'
# Signed, immutable public evidence of the original GPU2 qualification. These
# anchors preserve provenance only; current execution still requires its own key.
REGISTRATION_FILE_SHA='bac4b08bfc749cdd24b47710adb4854921f54fdf65b12a67ae6bb7145a6589a2'
CAPABILITY_FILE_SHA='57eba5ba8c9cc02c36c7b7966cea18478c8b2617ddc7eba06fe67ca77aaafc68'
SCHEMA='preserved-gpu2-training-source/1'
DEPLOYMENT_SCHEMA='preserved-gpu2-inference-deployment/1'
BINDING_SCHEMA='preserved-gpu2-inference-binding/1'
IDENTITY_FIELDS=('recipe_id','objective_profile','checkpoint_selection','adapter_topology_id','adapter_topology','adapter_topology_sha256','preprocessing','training_recipe','training_budget','scr_objective','scr_assets','scr_data_binding','scr_cache_binding','scr_preparation_binding','windows_cache_identity')

def require(ok,detail):
    if not ok:raise ValueError('E_PRESERVED_TRAINING: '+detail)

def read(path,*,expected_sha256=None,detail='evidence bytes changed'):
    p=Path(path).absolute()
    require(not any(x.is_symlink() or x.is_junction() for x in (p,*p.parents)),'unsafe evidence path')
    raw=native_path(p).read_bytes()
    if expected_sha256 is not None:
        require(hashlib.sha256(raw).hexdigest()==expected_sha256,detail)
    return json.loads(raw.decode('utf-8'))

def normalize_training_receipt(run_dir,raw):
    root=Path(run_dir)
    selection=raw.get('adapter_selection',{});budget=raw.get('training_budget',{})
    require(raw.get('ok') is True and raw.get('stats',{}).get('final_status')=='COMPLETED','incomplete engine')
    require(selection.get('kind')=='terminal_state' and selection.get('global_step')==budget.get('expected_optimizer_steps')==192 and selection.get('last_completed_epoch')==raw['stats'].get('epochs')==8 and selection.get('validation_matches_exported_state') is True and math.isfinite(float(selection['validation_loss'])),'incomplete terminal')
    for item in raw['output']['files']:
        rel=Path(item['relative_path']);require(not rel.is_absolute() and not rel.drive and '..' not in rel.parts,'output escape')
        p=root/rel;require(sha(native_path(p))==item['sha256'] and item.get('bytes') in (None,native_path(p).stat().st_size),'output changed')
    require(any(i['relative_path']=='control-training-diagnostics.json' for i in raw['output']['files']),'unbound diagnostics')
    diag=read(root/'control-training-diagnostics.json');require(diag['base_frozen'] is True,'base changed')
    normalized=copy.deepcopy(raw);counts=raw['runtime_evidence']['operator_execution_counts']
    require('operator_execution_counts' not in raw or raw['operator_execution_counts']==counts,'conflicting operators')
    normalized['operator_execution_counts']=copy.deepcopy(counts)
    for k in ('cuda_peak_allocated','cuda_peak_reserved'):
        require(k not in raw['process'] or raw['process'][k]==diag[k],'conflicting memory')
        normalized['process'][k]=diag[k]
    verify_execution_receipt(raw['runtime_binding'],normalized,'training')
    return normalized

def admit_source(run_dir,config,*,user_id,producer_app):
    require(type(user_id) is int and user_id==1,'wrong owner')
    root=Path(run_dir).absolute();producer=Path(producer_app).absolute()
    require(root.parts[-5:]==('users','1','styles','runs','2'),'wrong owned run')
    catalog_path=producer.parent/'release/SOURCE-MANIFEST-V3-LOCAL.json'
    require(native_path(catalog_path).is_file(),'historical source catalog missing')
    require(sha(native_path(catalog_path))==CATALOG_SHA,'historical source catalog changed')
    catalog=read(catalog_path)
    for rel,digest in catalog['files'].items():require(sha(native_path(producer/rel))==digest,'historical source changed: '+rel)
    for rel,digest in (('artifact/artifact.json',MANIFEST_SHA),('artifact/m2_adapter.pt',MODEL_SHA),('m2-receipt.json',RAW_SHA)):
        require(sha(native_path(root/rel))==digest,'original evidence changed: '+rel)
    manifest=read(root/'artifact/artifact.json');raw=read(root/'m2-receipt.json');supplement=read(root/'receipts/admission-supplement.json')
    require(hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()==manifest['config_sha256'],'immutable config changed')
    require(config['runtime_binding']==manifest['runtime_binding']==raw['runtime_binding'] and raw['runtime_binding']['registration_id']==REGISTRATION,'training binding changed')
    require(raw['dataset_snapshot_sha256']==config['snapshot_sha256']==SNAPSHOT_SHA,'wrong S03')
    for item in manifest['files']:require(sha(native_path(root/item['relative_path']))==item['sha256'],'formal artifact changed')
    require(any(i['relative_path']=='receipts/admission-supplement.json' and i['sha256']==sha(native_path(root/i['relative_path'])) for i in manifest['files']),'supplement not admitted')
    normalized=normalize_training_receipt(root,raw)
    provenance=supplement.get('admission_supplement',{})
    require(provenance.get('original_sha256')==RAW_SHA and provenance.get('original_receipt_unchanged') is True and provenance.get('diagnostics_sha256')==sha(native_path(root/'control-training-diagnostics.json')),'supplement provenance changed')
    require({k:v for k,v in supplement.items() if k!='admission_supplement'}==normalized,'supplement is not actual measured projection')
    from engine.style.dataset.binding import verify_snapshot_binding,verify_m2_receipt_binding
    snapshot=verify_snapshot_binding(config['snapshot_dir'],SNAPSHOT_SHA);verify_m2_receipt_binding(raw,snapshot)
    for k in IDENTITY_FIELDS:
        require(config.get(k) is None or config[k]==normalized.get(k),'training identity changed: '+k)
    verify_historical_qualification(producer,raw['runtime_binding'],root)
    return sealed({'schema':SCHEMA,'user_id':1,'run_id':2,'run_dir':str(root),'producer_app':str(producer),'producer_catalog_sha256':CATALOG_SHA,'manifest_sha256':MANIFEST_SHA,'adapter_sha256':MODEL_SHA,'raw_sha256':RAW_SHA,'supplement_sha256':sha(native_path(root/'receipts/admission-supplement.json')),'training_runtime_binding':raw['runtime_binding'],'snapshot_binding':snapshot,'base_ckpt_sha256':raw['base_ckpt_sha256'],'runtime_identity_sha256':raw['runtime_identity_sha256'],'config_sha256':manifest['config_sha256'],'immutable_training_identity':{k:copy.deepcopy(normalized[k]) for k in IDENTITY_FIELDS if k in normalized},**{k:copy.deepcopy(normalized[k]) for k in IDENTITY_FIELDS if k in normalized},'adapter_path':str(root/'artifact/m2_adapter.pt'),'artifact_manifest_path':str(root/'artifact/artifact.json'),'snapshot_sha256':SNAPSHOT_SHA,'model_version':raw['model_version'],'config':copy.deepcopy(config),'snapshot_dir':config['snapshot_dir'],'runtime_binding':raw['runtime_binding'],'terminal_updates':192,'may_publish':False,'execution_authorized':False})

def verify_metadata(source,meta):
    require_sealed(source,SCHEMA)
    require(meta.get('runtime_binding')==source['training_runtime_binding'],'metadata training binding changed')
    for k,v in source['immutable_training_identity'].items():require(meta.get(k)==v,'metadata changed: '+k)
    require(meta.get('dataset_snapshot_sha256',meta.get('snapshot_sha256'))==SNAPSHOT_SHA,'metadata snapshot changed')
    return True

def _root(app):return Path(app)/'runtime/preserved-training-deployments'

def reference(descriptor):
    return {'schema':BINDING_SCHEMA,'deployment_sha256':value_sha(descriptor),'source_sha256':descriptor['source']['sha256'],'training_binding_sha256':descriptor['source']['training_runtime_binding']['binding_sha256'],'execution_binding_sha256':descriptor['runtime_binding']['binding_sha256'],'scope':'inference'}

def verify_execution_compatibility(app,source,binding,current):
    """Consume authenticated target proof, never caller snapshot readiness."""
    training=source['training_runtime_binding']
    from .windows_user_scr import GPU
    require(binding.get('variant_id')==training.get('variant_id')==GPU,'inference variant differs from GPU2 training contract')
    require(binding.get('device')==training.get('device')=='cuda','GPU device contract differs')
    require(binding.get('asset_identity_sha256')==training.get('asset_identity_sha256'),'base assets differ')
    digest=current['registration']['capability_receipt_sha256']
    require(digest==binding['capability_receipt_sha256'] and re.fullmatch('[0-9a-f]{64}',digest),'target capability binding changed')
    envelope=read(W._root(app)/'capabilities'/f'{digest}.json')
    require(value_sha(envelope)==digest and hmac.compare_digest(envelope['hmac_sha256'],W._mac(app,envelope['body'])),'target capability authentication changed')
    body=envelope['body'];measured=body['proof']['component']['snapshot_binding']
    require(measured==source['snapshot_binding']==body['input_binding']['snapshot_binding'],'target qualification S03 differs from trained model')
    from engine.style.dataset.binding import verify_snapshot_binding
    actual=verify_snapshot_binding(body['snapshot'],SNAPSHOT_SHA)
    require(actual==measured and actual['snapshot_sha256']==SNAPSHOT_SHA,'actual qualification snapshot changed')
    return True

def register(app,source,inference_binding,*,config,user_id):
    require_sealed(source,SCHEMA)
    require(source==admit_source(source['run_dir'],config,user_id=user_id,producer_app=source['producer_app']),'source changed')
    current=resolve_binding(app,inference_binding,user_scr_purpose='inference')
    require(current.get('ready') is True and current.get('may_infer') is True and current.get('device')=='cuda' and inference_binding['device']=='cuda','independent GPU inference required')
    require(inference_binding!=source['training_runtime_binding'],'independent execution binding required')
    require(inference_binding['asset_identity_sha256']==source['training_runtime_binding']['asset_identity_sha256'],'base assets differ')
    verify_execution_compatibility(app,source,inference_binding,current)
    # Exact recipe/topology/preprocessing are preserved by consumer request verification.
    descriptor={'schema':DEPLOYMENT_SCHEMA,'source':source,'runtime_binding':copy.deepcopy(inference_binding),'source_inventory_sha256':value_sha(W.profile_for_source(app,inference_binding['variant_id'])['required_source_files']),'scope':'inference','may_publish':False}
    envelope={'descriptor':descriptor,'hmac_sha256':W._mac(app,descriptor)}
    binding=reference(descriptor);W._write(_root(app)/binding['deployment_sha256']/'descriptor.json',envelope)
    return binding

def resolve(app,binding,*,user_id,config=None,purpose='inference'):
    require(purpose=='inference' and isinstance(binding,dict) and binding.get('schema')==BINDING_SCHEMA and re.fullmatch('[0-9a-f]{64}',str(binding.get('deployment_sha256'))),'invalid deployment reference')
    envelope=read(_root(app)/binding['deployment_sha256']/'descriptor.json');descriptor=envelope['descriptor']
    require(hmac.compare_digest(envelope['hmac_sha256'],W._mac(app,descriptor)) and binding==reference(descriptor),'descriptor authentication changed')
    source=descriptor['source']
    if config is None:config=source['config']
    require(source==admit_source(source['run_dir'],config,user_id=user_id,producer_app=source['producer_app']),'source changed')
    execution=resolve_binding(app,descriptor['runtime_binding'],user_scr_purpose='inference')
    require(execution.get('may_infer') is True and execution.get('device')=='cuda','execution not available')
    verify_execution_compatibility(app,source,descriptor['runtime_binding'],execution)
    require(descriptor['source_inventory_sha256']==value_sha(W.profile_for_source(app,descriptor['runtime_binding']['variant_id'])['required_source_files']),'execution source changed')
    return {'descriptor':descriptor,'source':source,'training':read(Path(source['run_dir'])/'receipts/admission-supplement.json'),'manifest':read(Path(source['run_dir'])/'artifact/artifact.json'),'runtime_binding':descriptor['runtime_binding'],'training_runtime_binding':source['training_runtime_binding'],'snapshot_binding':source['snapshot_binding'],'deployment_binding':binding,'execution':execution}

def inference_identity(resolved):
    return {'deployment_binding':resolved['deployment_binding'],'training_runtime_binding':resolved['training_runtime_binding'],'training_snapshot_binding':resolved['source']['snapshot_binding'],'training_manifest_sha256':MANIFEST_SHA,'training_receipt_sha256':RAW_SHA,'training_admission_supplement_sha256':resolved['source']['supplement_sha256'],'runtime_binding':resolved['runtime_binding']}


def verify_request(resolved,*,adapter_path,adapter_sha256,snapshot_dir,snapshot_sha256,runtime_binding):
    source=resolved['source']
    require(Path(adapter_path).resolve()==(Path(source['run_dir'])/'artifact/m2_adapter.pt').resolve() and adapter_sha256==MODEL_SHA,'wrong model request')
    require(Path(snapshot_dir).resolve()==Path(source['snapshot_binding']['snapshot_dir']).resolve() and snapshot_sha256==SNAPSHOT_SHA,'wrong snapshot request')
    require(runtime_binding==resolved['runtime_binding'],'wrong inference binding request')
    return True

def verify_inference_receipt(resolved,receipt):
    require(receipt.get('adapter_sha256')==MODEL_SHA and receipt.get('dataset_snapshot_sha256')==SNAPSHOT_SHA,'inference model/snapshot changed')
    require(receipt.get('base_ckpt_sha256')==resolved['source']['base_ckpt_sha256'],'inference base changed')
    for key,value in inference_identity(resolved).items():require(receipt.get(key)==value,'inference provenance changed: '+key)
    for key in ('recipe_id','objective_profile','checkpoint_selection','adapter_topology_id','adapter_topology','adapter_topology_sha256','preprocessing','scr_objective','scr_assets'):
        require(receipt.get(key)==resolved['source']['immutable_training_identity'].get(key),'inference identity changed: '+key)
    verify_execution_receipt(resolved['runtime_binding'],receipt,'inference')
    return True


def verify_historical_qualification(producer,binding,run_dir):
    record=read(W._root(producer)/'registrations'/REGISTRATION/'registration.json',expected_sha256=REGISTRATION_FILE_SHA,detail='historical registration bytes changed')
    require(record['registration_id']==REGISTRATION==value_sha({k:v for k,v in record.items() if k!='registration_id'})[:32],'historical registration changed')
    envelope=read(W._root(producer)/'capabilities'/f"{record['capability_receipt_sha256']}.json",expected_sha256=CAPABILITY_FILE_SHA,detail='historical capability bytes changed')
    require(value_sha(envelope)==record['capability_receipt_sha256'],'historical capability authentication')
    for k in ('variant_id','device','asset_identity_sha256','dependency_lock_sha256','execution_profile_sha256','environment_identity_sha256','capability_receipt_sha256'):
        require(record[k]==binding[k],'historical binding changed: '+k)
    require(record['device']=='cuda' and record['synthetic_test_only'] is False and record['may_publish'] is False,'historical qualification scope changed')
    require(record['execution_profile_sha256']==W.profile_for_source(producer,record['variant_id'])['execution_profile_sha256'],'historical qualified source changed')
    wrapper=read(Path(run_dir)/'receipts/engine_receipt.json')
    completed=datetime.fromisoformat(wrapper['finished_at']).timestamp()
    require(record['issued_at']<=completed<=record['expires_at'],'training was outside historical qualification validity')
    return record

def normalized_training_receipt(run_dir,config,manifest):
    """Read-only original/supplement integrity; never grants current execution."""
    root=Path(run_dir)
    require(sha(native_path(root/'artifact/artifact.json'))==MANIFEST_SHA and read(root/'artifact/artifact.json')=={k:v for k,v in manifest.items() if not k.startswith('_')},'formal manifest changed')
    require(sha(native_path(root/'m2-receipt.json'))==RAW_SHA,'raw changed')
    require(hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()==manifest['config_sha256'],'immutable config changed')
    raw=read(root/'m2-receipt.json');supplement=read(root/'receipts/admission-supplement.json')
    entry=next(i for i in manifest['files'] if i['relative_path']=='receipts/admission-supplement.json')
    require(sha(native_path(root/entry['relative_path']))==entry['sha256'],'supplement changed')
    require(raw['runtime_binding']==config['runtime_binding']==manifest['runtime_binding'] and raw['runtime_binding']['registration_id']==REGISTRATION,'original binding changed')
    require(raw['dataset_snapshot_sha256']==config['snapshot_sha256']==SNAPSHOT_SHA,'original snapshot changed')
    normalized=normalize_training_receipt(root,raw)
    provenance=supplement.get('admission_supplement',{})
    require(provenance.get('original_sha256')==RAW_SHA and provenance.get('original_receipt_unchanged') is True and provenance.get('diagnostics_sha256')==sha(native_path(root/'control-training-diagnostics.json')),'supplement provenance changed')
    require({k:v for k,v in supplement.items() if k!='admission_supplement'}==normalized,'supplement projection changed')
    for k in IDENTITY_FIELDS:
        require(config.get(k) is None or config[k]==normalized.get(k),'immutable recipe changed: '+k)
    return supplement

def is_preserved(binding):
    return isinstance(binding,dict) and binding.get('schema')==BINDING_SCHEMA

def pointer_path(app,run_dir):
    """Append-only registration per source namespace; historical pointers stay intact.

    A different signed application must register its own qualified execution.
    An older namespace's global pointer never grants the new source rights.
    """
    namespace=value_sha(str(Path(app).resolve()).casefold())
    return Path(run_dir)/'receipts/preserved-inference-deployments'/(namespace+'.json')

def lookup(app,run_dir,*,user_id,config=None):
    pointer=pointer_path(app,run_dir)
    if not native_path(pointer).is_file():return None
    envelope=read(pointer);body=envelope['body']
    require(hmac.compare_digest(envelope['hmac_sha256'],W._mac(app,body)),'pointer authentication changed')
    require(body['user_id']==user_id and Path(body['run_dir']).resolve()==Path(run_dir).resolve(),'pointer owner/path changed')
    return resolve(app,body['deployment_binding'],user_id=user_id,config=config)

resolve_run_inference=lookup

def execution_view(record,resolved):
    """Ephemeral consumer view; never overwrite a training manifest/config."""
    result=copy.deepcopy(record)
    result.update(inference_identity(resolved))
    return result

