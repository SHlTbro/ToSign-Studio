"""Stdlib-only fixed experiment identity. No feature import or model loading."""
import hashlib
import json
from pathlib import Path


def digest(body):
    return hashlib.sha256(json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _sealed(body):
    return {**body,'sha256':digest(body)}


def objective_contract():
    return _sealed({'schema':'m2-scr-two-negative-objective/1','objective_profile':'epsilon-offset-scr2n-v1',
       'epsilon_loss':'unchanged mean epsilon MSE','offset_loss':'unchanged 0.1 * mean(offset_sum ** 2)',
       'coefficient':0.01,'temperature':0.07,'layers':[0,1,2,3],'dimensions':2048,
       'reduction':'mean of per-sample per-layer paired InfoNCE losses',
       'query':'L2-normalized projected frozen SCR features of analytic x0 clipped to [-1,1]',
       'positive':'exact same-occurrence author target used for forward noising; cached and detached',
       'negative_count':2,'negative_fonts':['noto_sans_cjk_sc_regular','noto_serif_cjk_sc_regular'],
       'negatives':'same character in each fixed font; cached and detached; no cross-batch negatives',
       'positive_augmentation':False,'feature_mode':'frozen eval FP32; no parameter or BatchNorm updates',
       'apply_to_condition_dropped_samples':True,'base_loss_unchanged_by_clipping':True,
       'metric_qualified_for_personal_style':False,'original_16_style_SCR_replication':False})


def asset_contract():
    return _sealed({'schema':'m2-scr-two-negative-assets/1',
       'official_revision':'7b28ce9c3b357f4fb23296622f458cf169803539',
       'module_sha256':'8d0fce2e9a2cb6b626251c1d0d2edb88d0ac268061ed7c20a57eefcfb8809694',
       'checkpoint_sha256':'86270e736e93a4a4a1294d46f2176ceb9554c4a24e80eafb35187a9757ab5cff',
       'checkpoint_size_bytes':284277322,'checkpoint_sha_origin':'locally observed official-download bytes; not author-published digest',
       'state_schema_sha256':'9bcf1201886c0e5c86804873016ab7a183d4034ce69fc688df7a710446969cbc',
       'state_keys':195,'reference_cache_manifest_sha256':'48ae9b5c5a40c9366160db97467f680f364382c05b30996998dbfbd6d63d981e',
       'reference_cache_vectors_sha256':'a3a3bdee66c4a7f759c7ca897329b1a72fba862bccf6dad01d33a41604893837',
       'original_vector_archive_sha256':'0d1a7de7b4da25aa8cc978147d47a57a7874261fab294717e5a9a98bfe004ba7',
       'input_manifest_sha256':'19d5718b37fcb8250ae5ee9e7b4511a4f2bbfee538d0623dcca24b84a419d2d0',
       'preprocessing_identity_sha256':'802eb26e6f27c941dd3768d2f9bb4477e206a58bab23106cb2c121d4d542da06',
       'snapshot_sha256':'b994c14347ba267cb45e5af0b3e6c073ba7154bc345c9f2115b7e13500e347b8',
       'font_protocol':'noto_sc_face2_freetype126_center128_bilinear96-v2',
       'font_sha256':['b76b0433203017ca80401b2ee0dd69350349871c4b19d504c34dbdd80541690a','5d9c31a059600193c9d7968a998bde886ccdc77e934006ad243b41794c496a7d']})


def validate_scr_provenance(record):
    if not isinstance(record,dict):raise ValueError('SCR provenance must be an object')
    for key,expected in (('scr_objective',objective_contract()),('scr_assets',asset_contract())):
        actual=record.get(key)
        if (not isinstance(actual,dict) or
            digest({k:v for k,v in actual.items() if k!='sha256'})!=actual.get('sha256') or
            digest(actual)!=digest(expected)):
            raise ValueError('SCR canonical provenance mismatch: '+key)
    return {'scr_objective':objective_contract(),'scr_assets':asset_contract()}


def project_scr_execution_evidence(evidence):
    """Keep the caller schema/snapshot intact while carrying actual SCR proof."""
    if not isinstance(evidence,dict) or evidence.get('schema')!='m2-scr2n-objective-evidence/1':
        raise ValueError('actual SCR objective evidence required')
    keys=('scr_feature_state','scr_cached_reference_proof','scr_rng_preserved')
    if any(key not in evidence for key in keys):raise ValueError('SCR execution evidence is incomplete')
    # JSON copy rejects nonfinite/nonserializable proof and prevents shared edits.
    cloned=json.loads(json.dumps(evidence,ensure_ascii=False,allow_nan=False))
    return {**{key:cloned[key] for key in keys},'scr_execution_evidence':cloned}


def default_scr_asset_paths():
    # This bounded cloud experiment reuses its verified asset directory even
    # when the exact source is copied into a registered staging directory.
    # Relocatable/Windows asset discovery is not claimed by this helper.
    root=Path('/workspace/scratch/64fc1a466651/dyyy_additional_author_photos_20261003')
    experiment=root/'scr-two-negative-objective-experiment-20261004'
    return {'module':str(root/'scr-asset-font-inventory-20261003/official-source-text/src/modules/scr_modules.py'),
            'checkpoint':str(root/'scr-asset-acquisition-20261004T0033Z/scr_210000.pth'),
            'state_schema':str(root/'scr-asset-font-inventory-20261003/scr-expected-state-schema.json'),
            'reference_cache_manifest':str(experiment/'prepared-reference-cache-v1/manifest.json'),
            'reference_cache_vectors':str(experiment/'prepared-reference-cache-v1/reference-vectors.npz')}


def validate_scr_asset_path_shape(paths):
    keys=('module','checkpoint','state_schema','reference_cache_manifest','reference_cache_vectors')
    if not isinstance(paths,dict) or set(paths)!=set(keys):raise ValueError('exact five SCR asset paths required')
    for key,value in paths.items():
        if not isinstance(value,str) or not Path(value).is_absolute():raise ValueError('SCR asset path must be absolute: '+key)
    return dict(paths)


def validate_scr_asset_paths(paths):
    paths=validate_scr_asset_path_shape(paths)
    assets=asset_contract();result={}
    for key in paths:
        value=paths[key]
        if not isinstance(value,str) or not Path(value).is_absolute():raise ValueError('SCR asset path must be absolute: '+key)
        p=Path(value)
        if not p.is_file():raise ValueError('SCR asset missing: '+key)
        actual=hashlib.sha256(p.read_bytes()).hexdigest()
        if actual!=assets[key+'_sha256']:raise ValueError('SCR asset hash mismatch: '+key)
        result[key]=str(p.resolve())
    if Path(result['checkpoint']).stat().st_size!=assets['checkpoint_size_bytes']:raise ValueError('SCR checkpoint size mismatch')
    return result
