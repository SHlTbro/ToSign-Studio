"""Portable immutable SCR specification; per-snapshot/cache identity lives elsewhere.

Importing this module neither opens model assets nor imports an ML framework.
There are deliberately no asset path defaults and no historical dataset pins.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

RECIPE_ID = 'm2-scr2n-user-snapshot-v1'
OBJECTIVE_PROFILE = 'epsilon-offset-scr2n-user-v1'
FONT_ORDER = ('noto_sans_cjk_sc_regular', 'noto_serif_cjk_sc_regular')
ASSET_PATH_KEYS = ('module', 'checkpoint', 'state_schema', 'font_sans', 'font_serif',
                   'source_ledger', 'reference_cache_manifest', 'reference_cache_vectors')
SHA256 = re.compile(r'^[0-9a-f]{64}$')


def digest(body):
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def sealed(body):
    return {**body, 'sha256': digest(body)}


def require_sealed(record, schema):
    if (not isinstance(record, dict) or record.get('schema') != schema
            or record.get('sha256') != digest({k: v for k, v in record.items() if k != 'sha256'})):
        raise ValueError('invalid sealed SCR record: ' + schema)
    return record


def objective_contract():
    # Retain the exact reviewed mathematical definition under a distinct identity.
    from .scr_contract import objective_contract as historical_objective
    body = {k: v for k, v in historical_objective().items() if k != 'sha256'}
    body.update(schema='m2-scr2n-user-objective/1', objective_profile=OBJECTIVE_PROFILE)
    return sealed(body)


def asset_contract():
    from .scr_contract import asset_contract as historical_assets
    historical = historical_assets()
    keys = ('official_revision', 'module_sha256', 'checkpoint_sha256', 'checkpoint_size_bytes',
            'checkpoint_sha_origin', 'state_schema_sha256', 'state_keys', 'font_protocol', 'font_sha256')
    return sealed({'schema': 'm2-scr2n-user-assets/1', **{k: historical[k] for k in keys},
                   'negative_font_order': list(FONT_ORDER), 'ttc_face_index': 2,
                   'font_render_size_px': 126, 'font_canvas_size_px': 128,
                   'feature_layers': [0, 1, 2, 3], 'feature_dimensions': 2048,
                   'feature_dtype': 'float32'})


def validate_user_scr_provenance(record):
    if not isinstance(record, dict):
        raise ValueError('SCR provenance must be an object')
    for name, expected in (('scr_objective', objective_contract()), ('scr_assets', asset_contract())):
        if digest(record.get(name)) != digest(expected):
            raise ValueError('SCR immutable provenance mismatch: ' + name)
    return {k: record[k] for k in ('scr_objective', 'scr_assets')}


def validate_user_scr_asset_path_shape(paths):
    if not isinstance(paths, dict) or set(paths) != set(ASSET_PATH_KEYS):
        raise ValueError('exact eight consumer-local SCR asset paths required')
    for name, value in paths.items():
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError('SCR asset path must be absolute on the consumer: ' + name)
    return dict(paths)


def file_sha256(path):
    from .trainer_resume_protocol import native_path
    value = hashlib.sha256()
    with native_path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def validate_user_scr_assets(paths):
    """Runtime-only byte verification. Never called by metadata-only dry runs."""
    paths = validate_user_scr_asset_path_shape(paths)
    specification = asset_contract()
    for key in ('module', 'checkpoint', 'state_schema', 'font_sans', 'font_serif'):
        expected = (specification['font_sha256'][('font_sans', 'font_serif').index(key)]
                    if key.startswith('font_') else specification[key + '_sha256'])
        if not Path(paths[key]).is_file() or file_sha256(paths[key]) != expected:
            raise ValueError('SCR immutable asset bytes differ: ' + key)
    if Path(paths['checkpoint']).stat().st_size != specification['checkpoint_size_bytes']:
        raise ValueError('SCR checkpoint size differs')
    return {key: str(Path(value).resolve()) for key, value in paths.items()}


def validate_data_binding(record):
    require_sealed(record, 'm2-scr2n-user-data/1')
    counts = record.get('counts', {})
    rows = record.get('rows')
    if (record.get('authority') != 'S03_CONTENT_SHA256'
            or not SHA256.fullmatch(str(record.get('snapshot_sha256', '')))
            or not SHA256.fullmatch(str(record.get('ledger_sha256', '')))
            or not isinstance(rows, list) or not rows
            or any(type(counts.get(k)) is not int for k in ('total', 'train', 'val'))
            or counts['train'] < 8 or counts['val'] < 2 or counts['total'] != len(rows)
            or counts['train'] + counts['val'] != counts['total']):
        raise ValueError('invalid SCR data count/authority binding')
    ids = [r.get('sample_id') for r in rows if isinstance(r, dict)]
    if len(ids) != len(rows) or any(not isinstance(s, str) or not s for s in ids) or ids != sorted(set(ids)):
        raise ValueError('SCR rows must be unique and ordered by sample_id')
    for row in rows:
        if (row.get('split') not in ('train', 'val') or not isinstance(row.get('label'), str)
                or len(row['label']) != 1 or row.get('target_origin') != 'canonical_author_alpha'
                or any(not SHA256.fullmatch(str(row.get(k, ''))) for k in
                       ('source_png_sha256', 'parent_source_hash', 'author_target_normalized_fp32_chw_sha256'))):
            raise ValueError('invalid SCR author row identity')
    if any(sum(r['split'] == side for r in rows) != counts[side] for side in ('train', 'val')):
        raise ValueError('SCR row split count differs')
    return record


def validate_cache_binding(record, data_binding):
    require_sealed(record, 'm2-scr2n-user-cache-binding/1')
    validate_data_binding(data_binding)
    if (record.get('data_binding_sha256') != data_binding['sha256']
            or record.get('snapshot_sha256') != data_binding['snapshot_sha256']
            or record.get('counts') != data_binding['counts']
            or record.get('scr_assets_sha256') != asset_contract()['sha256']
            or record.get('scr_objective_sha256') != objective_contract()['sha256']
            or record.get('synthetic_test_only') is not False
            or any(not SHA256.fullmatch(str(record.get(k, ''))) for k in
                   ('manifest_sha256', 'vectors_sha256', 'preprocessing_sha256'))):
        raise ValueError('SCR per-run cache/data binding differs or is synthetic')
    return record


def validate_preparation_binding(record, data_binding, cache_binding):
    require_sealed(record, 'm2-scr2n-user-preparation-binding/1')
    validate_cache_binding(cache_binding, data_binding)
    if (set(record) != {'schema', 'user_id', 'style_id', 'snapshot_sha256', 'data_binding_sha256',
                        'cache_binding_sha256', 'preparation_id', 'sha256'}
            or any(type(record.get(k)) is not int or record[k] < 1 for k in ('user_id', 'style_id'))
            or str(record['style_id']) != str(data_binding.get('style_id'))
            or record['snapshot_sha256'] != data_binding['snapshot_sha256']
            or record['data_binding_sha256'] != data_binding['sha256']
            or record['cache_binding_sha256'] != cache_binding['sha256']
            or not SHA256.fullmatch(str(record.get('preparation_id', '')))):
        raise ValueError('SCR server preparation binding differs from data/cache/user/style')
    return record


def user_scr_identity(record):
    validate_user_scr_provenance(record)
    data = validate_data_binding(record.get('scr_data_binding'))
    cache = validate_cache_binding(record.get('scr_cache_binding'), data)
    validate_preparation_binding(record.get('scr_preparation_binding'), data, cache)
    if record.get('snapshot_sha256', data['snapshot_sha256']) != data['snapshot_sha256']:
        raise ValueError('SCR request snapshot differs from prepared data')
    return json.loads(json.dumps({key: record[key] for key in
        ('scr_objective', 'scr_assets', 'scr_data_binding', 'scr_cache_binding', 'scr_preparation_binding')},
        ensure_ascii=False, allow_nan=False))


def project_user_scr_execution_evidence(evidence):
    if not isinstance(evidence, dict) or evidence.get('schema') != 'm2-scr2n-user-objective-evidence/1':
        raise ValueError('actual dynamic SCR objective evidence required')
    keys = ('scr_feature_state', 'scr_cached_reference_proof', 'scr_rng_preserved')
    if any(key not in evidence for key in keys):
        raise ValueError('dynamic SCR execution evidence incomplete')
    copied = json.loads(json.dumps(evidence, ensure_ascii=False, allow_nan=False))
    return {**{key: copied[key] for key in keys}, 'scr_execution_evidence': copied}
