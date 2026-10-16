"""Server-owned, exact-author palette qualification. No caller paths or colors.

The distribution catalog is disabled. A separately audited QA catalog derivative
may enable these two measured train palettes for the qualified natural/M2 path.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from sqlalchemy.exc import SQLAlchemyError
from app.core.errors import ApiError
from engine.style.dataset.schema import DatasetError

from . import natural_authorization
from .ink_color_v1 import ALPHA_POLICY, COMPOSITE_POLICY, COMPOSITOR_VERSION, SOURCE_MODE, TRANSFER_POLICY, validate_rgb
from .natural_layout_v1 import digest

ROOT = Path(__file__).resolve().parent
CATALOG = ROOT / 'ink_color_catalog.json'
ASSETS = ROOT / 'ink_color_assets'
MEASUREMENT_SHA256 = '2511cf55b5fa8bf3cbbc2a38636352c677bb1c030965567e3242b25ec0ae2bc0'
CONTRACT_SHA256 = '99c832fd5ba366438156a38c75c15d31bf499e563ba5d97ca0648075ac8e6001'
PALETTES = {
    'train-magenta-v1': ('NIKA-001', (196, 26, 180), 'dc689dc1f0304f9b023bfbd14e28cc1a5c3067943cb4062e2539852d92289e40'),
    'train-violet-v1': ('NIKA-010', (47, 12, 221), 'a66bf2014aa6b785eed0f4d5c5037a33175b43d87b274819caa136c1c9cf03ca'),
}


def _read_bytes(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('ink authority file is missing or indirect')
    raw = path.read_bytes()
    return raw, hashlib.sha256(raw).hexdigest()


def _read_json(path):
    raw, sha = _read_bytes(path)
    return json.loads(raw), sha


def _asset(name):
    if not isinstance(name, str) or not name or Path(name).name != name or '\\' in name:
        raise ValueError('ink authority path escapes catalog')
    if ASSETS.is_symlink():
        raise ValueError('indirect ink authority directory')
    return ASSETS / name


def _canonical_train_membership(snapshot, measurement, layout_binding):
    """Verify actual canonical parent/candidate membership, not claimed hashes."""
    from engine.style.dataset.snapshot import _content_hash
    root = Path(snapshot['snapshot_dir'])
    raw = {name: _read_bytes(root / name)[0] for name in ('index.json', 'samples.jsonl', 'split.json', 'provenance.json')}
    index = json.loads(raw['index.json'])
    if hashlib.sha256(raw['index.json']).hexdigest() != snapshot['index_sha256']:
        raise ValueError('ink-bound author snapshot index changed')
    if _content_hash(raw['samples.jsonl'], raw['split.json'], {sid: meta['sha256'] for sid, meta in index['files'].items()}, raw['provenance.json']) != snapshot['snapshot_sha256']:
        raise ValueError('ink-bound author snapshot metadata changed')
    rows = [json.loads(line) for line in raw['samples.jsonl'].decode('utf8').splitlines() if line.strip()]
    assignments = json.loads(raw['split.json'])['assignments']
    ids = [row['sample_id'] for row in rows]
    if len(set(ids)) != len(ids) or set(ids) != set(assignments) or set(ids) != set(index['files']):
        raise ValueError('ink-bound sample membership is incomplete or duplicated')
    intake = str(index.get('style_id', ''))
    if not intake.isdecimal() or int(intake) < 1 or str(snapshot.get('style_id')) != intake or any(str(row.get('style_id')) != intake for row in rows):
        raise ValueError('canonical intake style identity is missing or inconsistent')
    source = measurement['source_sha256']
    parents = [row for row in rows if row.get('parent_source_hash') == source]
    if not parents or any(assignments[row['sample_id']] != 'train' for row in parents):
        raise ValueError('palette original is not an exclusively authorized train parent')
    candidates = []
    for row in parents:
        candidate = ((row.get('review_provenance') or {}).get('offline_review_context') or {}).get('candidate_id')
        if not isinstance(candidate, str) or not candidate:
            raise ValueError('palette parent lacks canonical reviewed candidate identity')
        candidates.append(candidate)
    expected = measurement['source_candidate_ids']
    if len(set(candidates)) != len(candidates) or len(set(expected)) != len(expected) or sorted(candidates) != sorted(expected):
        raise ValueError('palette candidates differ from actual authorized train-parent samples')
    if len(parents) != measurement['n_accepted_occurrences'] or len(set(row['label'] for row in parents)) != measurement['n_distinct_labels']:
        raise ValueError('palette occurrence/label counts differ from canonical training evidence')
    row_sha = hashlib.sha256(raw['samples.jsonl']).hexdigest()
    split_sha = hashlib.sha256(raw['split.json']).hexdigest()
    layout_train = layout_binding['train_trace_binding']
    if row_sha != layout_train['samples_jsonl_sha256'] or split_sha != layout_train['split_json_sha256']:
        raise ValueError('palette and natural layout saw different canonical author evidence')
    return {'source_image_sha256': source, 'intake_style_id': int(intake), 'sample_ids': sorted(row['sample_id'] for row in parents),
            'candidate_ids': sorted(candidates), 'train_sample_count': len(parents),
            'all_actual_parent_samples_train': True, 'samples_jsonl_sha256': row_sha,
            'split_json_sha256': split_sha, 'source_refs': sorted(set(row['source_ref'] for row in parents))}


def _current_owned_source(user_id, style_id, measurement, membership, session):
    """Fresh committed ownership, admission and source bytes on each resolution."""
    from sqlalchemy.orm import Session
    from app.core.db import get_engine
    from app.core import storage
    from app.domains.assets_upload.models import Asset
    from app.domains.styles.models import StyleProfile
    from app.domains.training_sets.source_admission import require_training_source, source_path
    records = []
    with Session(bind=session.get_bind() if session is not None else get_engine(), autoflush=False) as reader:
        profile = reader.query(StyleProfile).filter(StyleProfile.style_id == style_id, StyleProfile.user_id == user_id,
                                                    StyleProfile.enabled == 1, StyleProfile.status == 'approved').one_or_none()
        if profile is None or profile.source_type != 'M2_ADAPTER':
            raise ValueError('current owned output adapter profile is unavailable or unapproved')
        intake_style_id = membership['intake_style_id']
        if type(intake_style_id) is not int or intake_style_id < 1:
            raise ValueError('canonical intake profile identity is invalid')
        intake = reader.query(StyleProfile).filter(StyleProfile.style_id == intake_style_id, StyleProfile.user_id == user_id,
                                                  StyleProfile.enabled == 1, StyleProfile.status == 'approved').one_or_none()
        if intake is None or intake.source_type != 'HANDWRITING_ASSET':
            raise ValueError('current owned handwriting intake profile is unavailable or unapproved')
        for ref in membership['source_refs']:
            if not isinstance(ref, str) or not ref.startswith('assets/') or len(ref.split('/')) != 2 or not ref.split('/')[1]:
                raise ValueError('canonical train parent lacks exact owned asset identity')
            asset = reader.query(Asset).filter(Asset.asset_id == ref.split('/')[1], Asset.user_id == str(user_id),
                                               Asset.enabled == 1, Asset.status == 'approved').one_or_none()
            if asset is None or asset.sha256 != measurement['source_sha256']:
                raise ValueError('current owned original source is missing, revoked or changed')
            path = source_path(asset, user_id)
            # source_path checks lexical owner identity and global containment;
            # additionally keep the resolved target in this owner's asset pool.
            storage.ensure_within_root(path, storage.user_pool_dir(user_id, 'assets'))
            admission = require_training_source(reader, asset, user_id, membership['intake_style_id'])
            raw, actual = _read_bytes(path)
            if actual != measurement['source_sha256'] or len(raw) != asset.bytes or [asset.width, asset.height] != measurement['source_size']:
                raise ValueError('current owned original source bytes or dimensions changed')
            records.append({'asset_id': asset.asset_id, 'source_ref': ref, 'owner_user_id': user_id,
                            'source_image_sha256': actual, 'source_size': [asset.width, asset.height],
                            'source_bytes': len(raw), 'intake_style_id': membership['intake_style_id'], 'source_admission_sha256': digest(admission)})
    if not records:
        raise ValueError('palette has no current owned original source')
    return records


def _resolve_author_palette(user_id, resolved, ink_palette_id, *, layout_profile, render_source_policy, frozen, session, verified_layout=None):
    if layout_profile != 'natural-layout-v1' or render_source_policy != 'm2_required':
        raise ValueError('source-solid ink requires explicit natural-layout-v1 and m2_required')
    if type(user_id) is not int or user_id < 1 or ink_palette_id not in PALETTES:
        raise ValueError('unknown owned train palette')
    if not isinstance(resolved, dict) or resolved.get('state') != 'bound' or resolved.get('engine_mode') != 'M2_ADAPTER':
        raise ValueError('bound trained adapter required for source-solid ink')
    if any(type(resolved.get(k)) is not int or resolved[k] < 1 for k in ('style_id', 'artifact_id')):
        raise ValueError('exact positive author/profile artifact identities required')
    catalog, catalog_sha = _read_json(CATALOG)
    if not isinstance(catalog, dict) or catalog.get('schema') != 'author-ink-catalog/1':
        raise ValueError('unknown author ink catalog')
    entries = catalog.get('entries')
    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        raise ValueError('malformed author ink catalog entries')
    matching = [e for e in entries if e.get('id') == ink_palette_id and e.get('owner_user_id') == user_id and e.get('profile_id') == resolved.get('style_id')]
    if len(matching) != 1:
        raise ValueError('this owned author profile has no qualified ink palette')
    entry = matching[0]
    if any(type(entry.get(k)) is not int or entry[k] < 1 for k in ('owner_user_id', 'profile_id', 'artifact_id')):
        raise ValueError('malformed ink catalog identity')
    if entry.get('enabled') is not True:
        raise ValueError('author ink acceptance remains incomplete')
    if entry.get('layout_profile') != layout_profile or entry.get('render_source_policy') != render_source_policy or entry.get('mode') != SOURCE_MODE:
        raise ValueError('ink catalog combination is not supported')
    # A caller-local layout result is accepted only by private discovery after its fresh check.
    # Direct job resolution below supplies none and therefore performs its own full check.
    natural = verified_layout if verified_layout is not None else natural_authorization.resolve_author_profile(user_id, resolved)
    if not isinstance(natural, dict) or not isinstance(natural.get('binding'), dict):
        raise ValueError('current natural author layout is unavailable')
    layout_binding = natural['binding']
    for field in ('owner_user_id', 'profile_id', 'artifact_id', 'artifact_manifest_sha256', 'adapter_sha256', 'snapshot_sha256', 'runtime_binding_sha256'):
        if entry.get(field) is None or entry[field] != layout_binding.get(field):
            raise ValueError('current author/model/source/runtime differs from ink qualification')
    if entry.get('natural_profile_sha256') != layout_binding['profile_sha256']:
        raise ValueError('current natural profile differs from ink qualification')
    code = entry.get('code_sha256')
    if not isinstance(code, dict) or set(code) != {'ink_color_v1.py', 'ink_authorization.py'}:
        raise ValueError('ink implementation authority is incomplete')
    for name, expected in code.items():
        if _read_bytes(ROOT / name)[1] != expected:
            raise ValueError('ink implementation is not the reviewed revision')
    files = entry.get('files')
    source_name, expected_rgb, expected_source = PALETTES[ink_palette_id]
    expected_files = {'palette_candidates.json': MEASUREMENT_SHA256, 'R6-COLOR-INTEGRATION-CONTRACT.md': CONTRACT_SHA256}
    if files != expected_files:
        raise ValueError('ink evidence identities differ from bounded reviewed source')
    loaded = {}
    for name, expected in files.items():
        raw, actual = _read_bytes(_asset(name))
        if actual != expected:
            raise ValueError('ink source/measurement authority bytes changed: ' + name)
        loaded[name] = raw
    measurements = json.loads(loaded['palette_candidates.json'])
    if measurements.get('schema') != 'dyyy-apparent-ink-palette/1':
        raise ValueError('unknown palette measurements')
    measured = [row for row in measurements['sources'] if row.get('source') == source_name]
    if len(measured) != 1:
        raise ValueError('missing or ambiguous measured original source')
    measured = measured[0]
    if measured.get('split') != 'train' or measured.get('candidate_role') != 'train_supported_candidate' or measured.get('source_sha256') != expected_source or validate_rgb(measured.get('candidate_rgb_srgb_assumed')) != expected_rgb:
        raise ValueError('palette is not the exact train-supported apparent color')
    profile = {'schema': 'source-solid-ink-profile/1', 'id': ink_palette_id, 'mode': SOURCE_MODE,
               'rgb': list(expected_rgb), 'hex': '#%02X%02X%02X' % expected_rgb, 'source_id': source_name,
               'source_image_sha256': expected_source, 'measurement_sha256': MEASUREMENT_SHA256,
               'role': 'train_supported_candidate', 'split': 'train', 'alpha_policy': ALPHA_POLICY,
               'transfer_policy': TRANSFER_POLICY, 'opacity_multiplier': 1,
               'compositor_version': COMPOSITOR_VERSION, 'composite_policy': COMPOSITE_POLICY}
    if entry.get('profile') != profile or digest(entry.get('profile')) != digest(profile) or entry.get('profile_sha256') != digest(profile):
        raise ValueError('ink profile differs from measured source authority')
    membership = _canonical_train_membership(resolved['snapshot_binding'], measured, layout_binding)
    owned_sources = _current_owned_source(user_id, resolved['style_id'], measured, membership, session)
    binding = {'schema': 'author-ink-color-binding/1', 'id': ink_palette_id, 'palette_id': ink_palette_id, 'mode': SOURCE_MODE,
               'rgb': list(expected_rgb), 'source_image_sha256': expected_source, 'measurement_sha256': MEASUREMENT_SHA256,
               'implementation_sha256': code['ink_color_v1.py'], 'current_owned_source_bindings': owned_sources,
               'catalog_sha256': catalog_sha, 'entry_sha256': digest(entry),
               'profile': profile, 'profile_sha256': digest(profile),
               'owner_user_id': user_id, 'profile_id': resolved['style_id'], 'intake_style_id': membership['intake_style_id'],
               'artifact_id': resolved['artifact_id'], 'artifact_manifest_sha256': resolved['manifest_sha256'],
               'adapter_sha256': entry['adapter_sha256'], 'snapshot_sha256': entry['snapshot_sha256'],
               'runtime_binding_sha256': entry['runtime_binding_sha256'],
               'layout_profile': layout_profile, 'render_source_policy': render_source_policy,
               'natural_layout_binding_sha256': digest(layout_binding),
               'code_sha256': code, 'authority_file_hashes': files,
               'train_parent_binding': membership, 'qualification': entry['qualification']}
    if frozen is not None and (not isinstance(frozen, dict) or frozen != binding or digest(frozen) != digest(binding)):
        raise ValueError('frozen ink qualification changed; create a new render job')
    return {'rgb': list(expected_rgb), 'binding': binding}


def _resolve_author_palette_checked(user_id, resolved, ink_palette_id, *, layout_profile, render_source_policy, frozen, session, verified_layout=None):
    try:
        return _resolve_author_palette(user_id, resolved, ink_palette_id, layout_profile=layout_profile,
                                       render_source_policy=render_source_policy, frozen=frozen, session=session,
                                       verified_layout=verified_layout)
    except (OSError, AttributeError, KeyError, TypeError, IndexError, RuntimeError, SQLAlchemyError, ApiError, DatasetError) as exc:
        raise ValueError('malformed or unavailable author ink authority') from exc


def resolve_author_palette(user_id, resolved, ink_palette_id, *, layout_profile='natural-layout-v1', render_source_policy='m2_required', frozen=None, session=None):
    """Resolve fresh authority; exact frozen equality is required on replay."""
    return _resolve_author_palette_checked(user_id, resolved, ink_palette_id, layout_profile=layout_profile,
                                           render_source_policy=render_source_policy, frozen=frozen, session=session)


def _discover_author_palettes(user_id, resolved, *, layout_profile, render_source_policy, session, verified_layout):
    if not isinstance(verified_layout, dict) or not isinstance(verified_layout.get('binding'), dict):
        raise ValueError('fresh author layout verification is required')
    choices, reasons = [], []
    for palette_id in PALETTES:
        try:
            value = _resolve_author_palette_checked(user_id, resolved, palette_id, layout_profile=layout_profile,
                                                    render_source_policy=render_source_policy, frozen=None,
                                                    session=session, verified_layout=verified_layout)
        except ValueError as exc:
            reasons.append(str(exc))
            continue
        profile = value['binding']['profile']
        choices.append({'id': palette_id, 'label': {'train-magenta-v1': 'Train source magenta', 'train-violet-v1': 'Train source violet-blue'}[palette_id],
                        'mode': SOURCE_MODE, 'rgb': value['rgb'], 'hex': profile['hex'],
                        'source_id': profile['source_id'], 'role': 'train', 'split': 'train',
                        'profile_sha256': value['binding']['profile_sha256'],
                        'binding': value['binding'], 'binding_sha256': digest(value['binding']),
                        'warning': 'Apparent source-photo sRGB; inspect local photo visibility.'})
    return {'available': bool(choices), 'reason': None if choices else '; '.join(dict.fromkeys(reasons)), 'palettes': choices}


def discover_author_palettes(user_id, resolved, *, layout_profile='natural-layout-v1', render_source_policy='m2_required', session=None):
    """Advertise choices only after a fresh layout check in this call."""
    try:
        verified_layout = natural_authorization.resolve_author_profile(user_id, resolved)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'available': False, 'reason': str(exc), 'palettes': []}
    try:
        return _discover_author_palettes(user_id, resolved, layout_profile=layout_profile,
                                         render_source_policy=render_source_policy, session=session,
                                         verified_layout=verified_layout)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, SQLAlchemyError, ApiError, DatasetError) as exc:
        return {'available': False, 'reason': str(exc), 'palettes': []}
