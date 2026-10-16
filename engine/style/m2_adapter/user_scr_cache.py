"""On-demand reference features for one freshly verified canonical S03 snapshot.

No encoder is imported or instantiated here. A future, independently qualified
runtime supplies a frozen producer with identity(), state_sha256(), and
encode(FP32 BCHW)->FP32 Bx4x2048 methods. Preparation is explicit and executing;
inspection/validation never calls that producer. Synthetic preparation has a
separate private entry point and cannot pass the production cache gate.
"""
from __future__ import annotations

import copy
from contextlib import nullcontext
import hashlib
import io
import json
import os
from pathlib import Path
import random
import shutil
import sys
import tempfile
import zipfile
from .trainer_resume_protocol import native_path

from .user_scr_contract import (FONT_ORDER, SHA256, asset_contract, digest, file_sha256,
    objective_contract, sealed, validate_data_binding, validate_user_scr_asset_path_shape,
    validate_user_scr_assets)


class UserSCRCacheError(ValueError):
    def __init__(self, code, message):
        super().__init__(code + ': ' + message)
        self.code = code


def inspect_font_coverage(rows, asset_paths):
    """Read cmap/hash only. Does not rasterize, import Torch, or open weights."""
    from fontTools.ttLib import TTFont
    paths = validate_user_scr_asset_path_shape(asset_paths)
    specification = asset_contract()
    labels = sorted({row['label'] for row in rows})
    result = []
    for fid, key, expected in zip(FONT_ORDER, ('font_sans', 'font_serif'), specification['font_sha256']):
        if file_sha256(paths[key]) != expected:
            raise UserSCRCacheError('E_SCR_FONT_BYTES', 'negative font hash differs: ' + fid)
        with TTFont(paths[key], fontNumber=2, lazy=True) as font:
            cmap = font.getBestCmap() or {}
            missing = [label for label in labels if not cmap.get(ord(label)) or cmap[ord(label)] == '.notdef']
        if missing:
            raise UserSCRCacheError('E_SCR_FONT_COVERAGE', fid + ' lacks ' + ','.join(missing))
        result.append({'font_id': fid, 'font_sha256': expected, 'ttc_face_index': 2,
                       'covered_labels': labels, 'unicode_covered': True})
    return result


def _read_json(path):
    def invalid(value):
        raise ValueError('nonfinite JSON constant: ' + value)
    return json.loads(native_path(path).read_text(encoding='utf-8'), parse_constant=invalid)


def preprocessing_contract():
    from .preprocessing import UPSTREAM_PROFILE, preprocessing_contract as author_contract
    return sealed({'schema': 'm2-scr2n-user-preprocessing/1',
        'author': author_contract(UPSTREAM_PROFILE),
        'author_source': 'canonical S03 alpha PNG only; font RGB is never an author target',
        'negative_protocol': asset_contract()['font_protocol'],
        'negative_font_order': list(FONT_ORDER), 'ttc_face_index': 2,
        'font_render_size_px': 126, 'font_canvas_size_px': 128,
        'image_shape': [3, 96, 96], 'tensor': 'contiguous little-endian float32 CHW, RGB / 127.5 - 1',
        'resize': 'Pillow bilinear', 'oversize': 'reject; no crop, substitution or size retune'})


def _image_tensor(image):
    import numpy as np
    if image.mode != 'RGB' or image.size != (96, 96):
        raise ValueError('SCR canonical input must be RGB96')
    pixels = np.asarray(image, dtype=np.float32)
    return np.ascontiguousarray((pixels / np.float32(127.5) - np.float32(1)).transpose(2, 0, 1), dtype='<f4')


def _array_sha(value):
    import hashlib
    return hashlib.sha256(value.tobytes(order='C')).hexdigest()


def _family_components(rows, ledger):
    # Compute closure over ALL ledger parents, including absent connecting members.
    parents = {row['parent_source_hash'] for row in rows}
    links = {p: {p} for p in parents}
    for members in ledger.families.values():
        for p in members:
            links.setdefault(p, {p}).update(members)
    components, seen = [], set()
    for parent in sorted(parents):
        if parent in seen:
            continue
        closure, todo = set(), [parent]
        while todo:
            current = todo.pop()
            if current in closure:
                continue
            closure.add(current)
            todo.extend(links.get(current, {current}) - closure)
        seen.update(closure)
        members = sorted(parents & closure)
        component_rows = [r for r in rows if r['parent_source_hash'] in members]
        if len({r['split'] for r in component_rows}) != 1:
            raise ValueError('SCR parent/family crosses train and val')
        components.append({'parent_hashes': members, 'family_closure_hashes': sorted(closure),
                           'sample_ids': sorted(r['sample_id'] for r in component_rows),
                           'split': component_rows[0]['split']})
    return components


def inspect_user_snapshot(snapshot_dir, expected_sha256, *, ledger_path):
    """Verify data/authority and pixels, without opening any weights or executing a model."""
    from engine.style.dataset.binding import verify_snapshot_binding
    from engine.style.dataset.ledger import LedgerIndex
    from engine.style.dataset.snapshot import load_snapshot
    from engine.style.dataset.split import SplitConfig
    from engine.style.dataset.validator import validate_snapshot
    from .preprocessing import full_range_glyph_image
    if not Path(ledger_path).is_absolute():
        raise ValueError('explicit absolute consumer-local source ledger required')
    verified = verify_snapshot_binding(snapshot_dir, expected_sha256)
    root = Path(verified['snapshot_dir'])
    source_rows, index, split = load_snapshot(root)
    ledger = LedgerIndex.load(ledger_path)
    ledger.assert_sources_valid()
    if index.get('ledger_sha256') != ledger.sha256:
        raise ValueError('SCR source ledger differs from frozen snapshot')
    validate_snapshot(root, ledger=ledger, gates=SplitConfig()).raise_if_bad()
    rows = []
    for source in sorted(source_rows, key=lambda r: r['sample_id']):
        sid = source['sample_id']
        # Explicit target origin prevents font controls entering this author channel.
        if source.get('parent_origin') != 'user_upload_confirmed':
            raise ValueError('SCR author targets require confirmed user-upload provenance')
        tensor = _image_tensor(full_range_glyph_image(root / source['snapshot_file']))
        rows.append({'sample_id': sid, 'label': source['label'], 'split': split[sid],
            'parent_source_hash': source['parent_source_hash'], 'snapshot_file': source['snapshot_file'],
            'source_png_sha256': index['files'][sid]['sha256'],
            'source_row_sha256': digest(source), 'target_origin': 'canonical_author_alpha',
            'author_target_normalized_fp32_chw_sha256': _array_sha(tensor),
            'positive_input_id': 'author_' + sid,
            'negative_input_ids': [f'control_{font}_U+{ord(source["label"]):04X}' for font in FONT_ORDER]})
    body = {'schema': 'm2-scr2n-user-data/1', 'authority': verified['authority'],
        'snapshot_id': verified['snapshot_id'], 'style_id': verified['style_id'],
        'snapshot_sha256': verified['snapshot_sha256'], 'index_sha256': verified['index_sha256'],
        'provenance_sha256': verified['provenance_sha256'], 'ledger_sha256': ledger.sha256,
        'counts': {'total': len(rows), 'train': sum(r['split'] == 'train' for r in rows),
                   'val': sum(r['split'] == 'val' for r in rows)},
        'preprocessing_sha256': preprocessing_contract()['sha256'],
        'family_components': _family_components(rows, ledger), 'rows': rows}
    binding = sealed(body)
    validate_data_binding(binding)
    # A mutation while inspecting pixels must not become a new trusted identity.
    if verify_snapshot_binding(root, expected_sha256) != verified:
        raise ValueError('SCR snapshot changed during inspection')
    return {'snapshot_binding': verified, 'data_binding': binding, 'rows': copy.deepcopy(rows)}


def _fresh_snapshot(binding, paths):
    if not isinstance(binding, dict):
        raise ValueError('fresh canonical snapshot binding required')
    result = inspect_user_snapshot(binding.get('snapshot_dir'), binding.get('snapshot_sha256'),
                                   ledger_path=paths['source_ledger'])
    if result['snapshot_binding'] != binding:
        raise ValueError('SCR binding differs from fresh canonical verification')
    return result


class NotoFontRasterizer:
    """Deferred exact Noto face2/126px rasterizer; no import until rasterize()."""
    def rasterize(self, label, font_id, font_path):
        import numpy as np
        from PIL import Image
        from fontTools.ttLib import TTFont
        import pygame.freetype
        import pygame.surfarray
        font_index = FONT_ORDER.index(font_id)
        specification = asset_contract()
        if file_sha256(font_path) != specification['font_sha256'][font_index]:
            raise ValueError('negative font bytes changed')
        with TTFont(font_path, fontNumber=2, lazy=True) as tt:
            glyph = (tt.getBestCmap() or {}).get(ord(label))
            if not glyph or glyph == '.notdef':
                raise UserSCRCacheError('E_SCR_FONT_COVERAGE', 'missing negative font glyph coverage: ' + label)
        pygame.freetype.init()
        font = pygame.freetype.Font(str(font_path), size=126, font_index=2)
        if font.style != 0 or font.resolution != 72:
            raise ValueError('negative font rendering configuration differs')
        metrics = font.get_metrics(label)
        if len(metrics) != 1 or metrics[0] is None:
            raise UserSCRCacheError('E_SCR_FONT_COVERAGE', 'missing negative font glyph metrics: ' + label)
        surface, _ = font.render(label)
        alpha = pygame.surfarray.pixels_alpha(surface).transpose(1, 0).copy()
        h, w = alpha.shape
        if not (0 < h <= 128 and 0 < w <= 128) or not bool((alpha > 0).any()):
            raise ValueError('negative font glyph is empty or exceeds128px: ' + label)
        canvas = np.full((128, 128), 255, dtype=np.uint8)
        x, y = round((128 - w) / 2), round((128 - h) / 2)
        canvas[y:y+h, x:x+w] = 255 - alpha
        image = Image.fromarray(canvas).convert('RGB').resize((96, 96), Image.Resampling.BILINEAR)
        return {'image': image, 'unicode_covered': True, 'nonblank': True,
                'font_sha256': specification['font_sha256'][font_index],
                'protocol': specification['font_protocol']}


def _synthetic_assets(paths):
    """Explicitly labelled test bytes; can never equal the immutable production pins."""
    original = asset_contract()
    return sealed({**{k: v for k, v in original.items() if k != 'sha256'},
        'synthetic_test_only': True,
        **{key + '_sha256': file_sha256(paths[key]) for key in ('module', 'checkpoint', 'state_schema')},
        'checkpoint_size_bytes': Path(paths['checkpoint']).stat().st_size,
        'font_sha256': [file_sha256(paths[k]) for k in ('font_sans', 'font_serif')]})


def _reference_batch(producer, synthetic):
    # Keep inspection Torch-free and do not duck-type a trust declaration.
    # Only the already-loaded exact official factory type gets boundary hashes;
    # subclasses, custom producers and all synthetic preparations stay strict.
    module = sys.modules.get(__package__ + '.user_scr_objective')
    official = getattr(module, '_OfficialUserSCRReferenceProducer', None)
    if not synthetic and official is not None and type(producer) is official:
        return producer._reference_batch()
    return nullcontext()


def _producer_state(producer, assets, synthetic):
    expected = {'scr_assets_sha256': assets['sha256'], 'dtype': 'float32', 'eval': True,
                'all_parameters_frozen': True, 'synthetic_test_only': synthetic}
    if digest(producer.identity()) != digest(expected):
        raise ValueError('producer must attest exact frozen FP32 feature assets and mode')
    state = producer.state_sha256()
    if not isinstance(state, str) or not SHA256.fullmatch(state):
        raise ValueError('producer requires complete frozen state SHA256')
    return state


def _valid_features(value, shape, name):
    import numpy as np
    if not isinstance(value, np.ndarray) or value.dtype != np.dtype('float32') or value.shape != shape:
        raise ValueError(name + ' feature dtype/shape differs')
    if not np.isfinite(value).all() or (np.linalg.norm(value.astype(np.float64), axis=-1) <= 1e-12).any():
        raise ValueError(name + ' features must be finite nonzero vectors')
    return np.ascontiguousarray(value, dtype='<f4').copy()


def _rng_state():
    import numpy as np
    # Observe an already initialized producer framework, never import it here.
    torch = sys.modules.get('torch')
    return (random.getstate(), np.random.get_state(), torch,
            torch.random.get_rng_state().clone() if torch is not None else None,
            tuple(s.clone() for s in torch.cuda.get_rng_state_all())
            if torch is not None and torch.cuda.is_initialized() else ())


def _rng_equal(left, right):
    import numpy as np
    return (left[0] == right[0] and left[1][0] == right[1][0]
            and np.array_equal(left[1][1], right[1][1]) and left[1][2:] == right[1][2:]
            and left[2] is right[2]
            and (left[2] is None or left[2].equal(left[3], right[3]))
            and len(left[4]) == len(right[4])
            and all(left[2].equal(a, b) for a, b in zip(left[4], right[4])))


def _restore_rng(state):
    import numpy as np
    random.setstate(state[0])
    np.random.set_state(state[1])
    if state[2] is not None:
        state[2].random.set_rng_state(state[3])
        if state[4]:
            state[2].cuda.set_rng_state_all(list(state[4]))


def _encode(producer, image, assets, synthetic, state):
    batch = image[None].copy()
    before = _array_sha(batch)
    if _producer_state(producer, assets, synthetic) != state:
        raise ValueError('frozen producer state changed before feature execution')
    rng = _rng_state()
    try:
        result = producer.encode(batch)
        if not _rng_equal(rng, _rng_state()):
            raise ValueError('SCR producer consumed RNG or initialized a framework during feature execution')
    finally:
        _restore_rng(rng)
    if _array_sha(batch) != before or _producer_state(producer, assets, synthetic) != state:
        raise ValueError('frozen producer state or input changed during feature execution')
    return _valid_features(result, (1, 4, 2048), 'reference')[0]


def _write_deterministic_npz(path, positive, negative):
    import numpy as np
    # np.savez includes zip timestamps; fixed ZipInfo makes byte identity reproducible.
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_STORED) as archive:
        for key, value in (('positive', positive), ('negative', negative)):
            stream = io.BytesIO()
            np.lib.format.write_array(stream, value, allow_pickle=False)
            info = zipfile.ZipInfo(key + '.npy', date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o600 << 16
            archive.writestr(info, stream.getvalue())


def _cache_binding(manifest, manifest_sha256):
    return sealed({'schema': 'm2-scr2n-user-cache-binding/1',
        'snapshot_sha256': manifest['data_binding']['snapshot_sha256'],
        'data_binding_sha256': manifest['data_binding']['sha256'],
        'counts': manifest['data_binding']['counts'], 'scr_assets_sha256': manifest['scr_assets']['sha256'],
        'scr_objective_sha256': manifest['scr_objective']['sha256'],
        'preprocessing_sha256': manifest['preprocessing']['sha256'],
        'manifest_sha256': manifest_sha256, 'vectors_sha256': manifest['vectors']['sha256'],
        'synthetic_test_only': manifest['synthetic_test_only']})


def prepare_user_reference_cache(output_dir, snapshot_binding, asset_paths, *, feature_producer,
                                 font_rasterizer=None):
    if font_rasterizer is not None:
        raise ValueError('production cache uses the fixed Noto rasterizer; injection is synthetic-only')
    return _prepare(output_dir, snapshot_binding, asset_paths, feature_producer,
                    NotoFontRasterizer(), synthetic=False)


def _prepare_synthetic_user_reference_cache_for_tests(output_dir, snapshot_binding, asset_paths, *,
                                                      feature_producer, font_rasterizer):
    return _prepare(output_dir, snapshot_binding, asset_paths, feature_producer, font_rasterizer,
                    synthetic=True)


def _preserve_cleanup_error(primary, cleanup, *, operation, path):
    """Keep the first exception object/traceback, attach cleanup diagnostics.

    This changes no tempfile, permission or process behavior. Failed owned
    temporary files stay where they are when the OS refuses cleanup.
    """
    row = {'operation':operation,'path':str(path),'error_type':type(cleanup).__name__,
        'errno':getattr(cleanup,'errno',None),'winerror':getattr(cleanup,'winerror',None),'detail':str(cleanup)}
    try:
        primary.cache_cleanup_errors = [*getattr(primary,'cache_cleanup_errors',[]),row]
        primary.add_note('Owned cache cleanup failed: '+json.dumps(row,ensure_ascii=False,sort_keys=True))
    except Exception:
        # Diagnostic attachment must never replace the original failure.
        pass

def _prepare(output_dir, snapshot_binding, asset_paths, producer, rasterizer, *, synthetic):
    import numpy as np
    from .preprocessing import full_range_glyph_image
    paths = validate_user_scr_asset_path_shape(asset_paths)
    output = Path(output_dir)
    if not output.is_absolute() or native_path(output).exists():
        raise ValueError('cache output must be a new absolute directory; no overwrite')
    resolved_output = output.resolve()
    if any(native_path(parent / 'FROZEN').exists() for parent in (resolved_output, *resolved_output.parents)):
        raise ValueError('cache output cannot be inside a frozen snapshot')
    if (Path(paths['reference_cache_manifest']) != output / 'manifest.json'
            or Path(paths['reference_cache_vectors']) != output / 'reference-vectors.npz'):
        raise ValueError('cache output paths must name the new directory')
    inspected = _fresh_snapshot(snapshot_binding, paths)
    if not synthetic:
        validate_user_scr_assets(paths)
    assets = _synthetic_assets(paths) if synthetic else asset_contract()
    temporary = None
    created_output = False
    published = []
    primary_error = None
    try:
        # The successful boundary exit is required before atomic publication.
        with _reference_batch(producer, synthetic):
            state = _producer_state(producer, assets, synthetic)
            rows = copy.deepcopy(inspected['rows'])
            unique_negatives, negative_evidence = {}, {}
            for label in sorted({row['label'] for row in rows}):
                values, evidence = [], []
                for font_id, key, font_hash in zip(FONT_ORDER, ('font_sans', 'font_serif'), assets['font_sha256']):
                    rendered = rasterizer.rasterize(label, font_id, paths[key])
                    if (not isinstance(rendered, dict) or rendered.get('unicode_covered') is not True
                            or rendered.get('nonblank') is not True or rendered.get('font_sha256') != font_hash
                            or rendered.get('protocol') != assets['font_protocol']):
                        raise UserSCRCacheError('E_SCR_FONT_COVERAGE', 'missing glyph coverage or negative font provenance differs')
                    image = _image_tensor(rendered['image'])
                    if not bool((image < 1).any()):
                        raise ValueError('negative font raster is blank')
                    values.append(_encode(producer, image, assets, synthetic, state))
                    evidence.append({'font_id': font_id, 'font_sha256': font_hash, 'label': label,
                        'unicode_covered': True, 'nonblank': True,
                        'normalized_fp32_chw_sha256': _array_sha(image)})
                unique_negatives[label] = np.stack(values, axis=1)
                negative_evidence[label] = evidence
            positive, negative = [], []
            for row in rows:
                target = _image_tensor(full_range_glyph_image(Path(snapshot_binding['snapshot_dir']) / row['snapshot_file']))
                if _array_sha(target) != row['author_target_normalized_fp32_chw_sha256']:
                    raise ValueError('author target changed before feature execution')
                p = _encode(producer, target, assets, synthetic, state)
                n = unique_negatives[row['label']].copy()
                positive.append(p)
                negative.append(n)
                row.update(negative_inputs=copy.deepcopy(negative_evidence[row['label']]),
                           positive_vector_sha256=_array_sha(p), negative_vector_sha256=_array_sha(n))
            positive, negative = np.stack(positive), np.stack(negative)
            if _fresh_snapshot(snapshot_binding, paths)['data_binding'] != inspected['data_binding']:
                raise ValueError('snapshot changed during cache construction')
            if synthetic:
                if _synthetic_assets(paths) != assets:
                    raise ValueError('synthetic assets changed during cache construction')
            else:
                validate_user_scr_assets(paths)
            native_path(output.parent).mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix='.' + output.name + '-', dir=native_path(output.parent)))
            vectors = temporary / 'reference-vectors.npz'
            _write_deterministic_npz(vectors, positive, negative)
            final_state = _producer_state(producer, assets, synthetic)
            if final_state != state:
                raise ValueError('frozen producer state changed before cache publication')
            manifest = sealed({'schema': 'm2-scr2n-user-reference-cache/1',
                'status': 'SYNTHETIC_TEST_ONLY' if synthetic else 'COMPUTED_FRESH_REFERENCE_FEATURES',
                'synthetic_test_only': synthetic, 'production_qualified': False,
                'data_binding': inspected['data_binding'], 'scr_assets': assets,
                'scr_objective': objective_contract(), 'preprocessing': preprocessing_contract(),
                'layers': [0, 1, 2, 3], 'dimensions': 2048, 'dtype': 'float32',
                'positive_shape': list(positive.shape), 'negative_shape': list(negative.shape),
                'negative_font_order': list(FONT_ORDER), 'rows': rows,
                'vectors': {'filename': 'reference-vectors.npz', 'sha256': file_sha256(vectors),
                    'positive_sha256': _array_sha(positive), 'negative_sha256': _array_sha(negative)},
                'feature_execution': {'mode': 'frozen_eval_fp32', 'producer_state_before_sha256': state,
                    'producer_state_after_sha256': final_state,
                    'author_images': len(rows), 'negative_images': 2 * len(unique_negatives),
                    'reference_feature_forwards': len(rows) + 2 * len(unique_negatives),
                    'reused_historical_vectors': False, 'rng_preserved': True,
                    'synthetic_test_only': synthetic}})
            manifest_path = temporary / 'manifest.json'
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                separators=(',', ':'), allow_nan=False) + '\n', encoding='utf-8')
        # Exclusive directory creation forbids overwriting even an empty target.
        # Publish the complete manifest LAST as the atomic commit point. Readers
        # cannot accept the new cache before both immutable files are present.
        native_path(output).mkdir()
        created_output = True
        for filename in ('reference-vectors.npz', 'manifest.json'):
            os.link(native_path(temporary / filename), native_path(output / filename))
            published.append(filename)
        return {'paths': paths, 'manifest': manifest, 'data_binding': inspected['data_binding'],
                'cache_binding': _cache_binding(manifest, file_sha256(output / 'manifest.json'))}
    except BaseException as exc:
        primary_error = exc
        if created_output:
            for filename in reversed(published):
                try:
                    native_path(output / filename).unlink(missing_ok=True)
                except OSError as cleanup:
                    _preserve_cleanup_error(exc,cleanup,operation='rollback-published-file',path=output/filename)
            # If another process inserted a file, preserve it and never replace
            # or recursively remove a directory we no longer exclusively own.
            try:
                if not any(native_path(output).iterdir()):
                    native_path(output).rmdir()
            except OSError as cleanup:
                _preserve_cleanup_error(exc,cleanup,operation='rollback-output-directory',path=output)
        raise
    finally:
        if temporary is not None:
            try:
                shutil.rmtree(native_path(temporary))
            except OSError as cleanup:
                if primary_error is None:
                    # A cleanup failure alone remains a failure; qualification
                    # cannot quietly succeed after an incomplete cleanup.
                    raise
                _preserve_cleanup_error(primary_error,cleanup,operation='remove-owned-temporary-directory',path=temporary)


def validate_user_reference_cache(asset_paths, snapshot_binding, *, allow_synthetic=False, verify_assets=True):
    """Read/verify existing references; never calls an encoder or font rasterizer.

    verify_assets=False is a metadata/cache dry-run, never production readiness.
    It deliberately does not open checkpoint or module bytes.
    """
    import numpy as np
    paths = validate_user_scr_asset_path_shape(asset_paths)
    inspected = _fresh_snapshot(snapshot_binding, paths)
    manifest_bytes = native_path(paths['reference_cache_manifest']).read_bytes()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    def invalid(value):
        raise ValueError('nonfinite JSON constant: ' + value)
    manifest = json.loads(manifest_bytes.decode('utf-8'), parse_constant=invalid)
    from .user_scr_contract import require_sealed
    require_sealed(manifest, 'm2-scr2n-user-reference-cache/1')
    synthetic = manifest.get('synthetic_test_only')
    if type(synthetic) is not bool or (synthetic and not allow_synthetic):
        raise ValueError('synthetic SCR cache is forbidden for production')
    assets = _synthetic_assets(paths) if synthetic else asset_contract()
    if verify_assets and not synthetic:
        validate_user_scr_assets(paths)
    count = inspected['data_binding']['counts']['total']
    expected = {'status': 'SYNTHETIC_TEST_ONLY' if synthetic else 'COMPUTED_FRESH_REFERENCE_FEATURES',
        'production_qualified': False, 'data_binding': inspected['data_binding'],
        'scr_assets': assets, 'scr_objective': objective_contract(), 'preprocessing': preprocessing_contract(),
        'layers': [0, 1, 2, 3], 'dimensions': 2048, 'dtype': 'float32',
        'positive_shape': [count, 4, 2048], 'negative_shape': [count, 4, 2, 2048],
        'negative_font_order': list(FONT_ORDER)}
    if any(digest(manifest.get(key)) != digest(wanted) for key, wanted in expected.items()):
        raise ValueError('SCR cache metadata/data/immutable identity differs')
    vectors = manifest.get('vectors', {})
    if vectors.get('filename') != 'reference-vectors.npz' or file_sha256(paths['reference_cache_vectors']) != vectors.get('sha256'):
        raise ValueError('SCR cache archive bytes differ')
    with np.load(native_path(paths['reference_cache_vectors']), allow_pickle=False) as archive:
        if archive.files != ['positive', 'negative']:
            raise ValueError('SCR archive must contain ordered positive and negative arrays only')
        positive = _valid_features(archive['positive'], (count, 4, 2048), 'positive')
        negative = _valid_features(archive['negative'], (count, 4, 2, 2048), 'negative')
    if _array_sha(positive) != vectors.get('positive_sha256') or _array_sha(negative) != vectors.get('negative_sha256'):
        raise ValueError('SCR cache tensor bytes differ')
    rows = manifest.get('rows')
    if not isinstance(rows, list) or len(rows) != count:
        raise ValueError('SCR cache rows must exactly cover the current snapshot')
    seen_negative = {}
    for index, (row, source) in enumerate(zip(rows, inspected['rows'])):
        if not isinstance(row, dict) or any(digest(row.get(k)) != digest(v) for k, v in source.items()):
            raise ValueError('SCR cache ordered occurrence/target/split row differs')
        if row.get('positive_vector_sha256') != _array_sha(positive[index]) or row.get('negative_vector_sha256') != _array_sha(negative[index]):
            raise ValueError('SCR cache row vector bytes differ')
        fonts = row.get('negative_inputs')
        if not isinstance(fonts, list) or len(fonts) != 2:
            raise ValueError('two same-character negative font records required')
        for font, fid, sha in zip(fonts, FONT_ORDER, assets['font_sha256']):
            if (not isinstance(font, dict) or font.get('font_id') != fid or font.get('font_sha256') != sha
                    or font.get('label') != row['label'] or font.get('unicode_covered') is not True
                    or font.get('nonblank') is not True
                    or not SHA256.fullmatch(str(font.get('normalized_fp32_chw_sha256', '')))):
                raise ValueError('negative font/label/input hash differs')
        entry = (fonts, row['negative_vector_sha256'])
        if row['label'] in seen_negative and seen_negative[row['label']] != entry:
            raise ValueError('same-character negative features differ across occurrences')
        seen_negative[row['label']] = entry
    execution = manifest.get('feature_execution', {})
    before, after = execution.get('producer_state_before_sha256'), execution.get('producer_state_after_sha256')
    if (not isinstance(before, str) or not SHA256.fullmatch(before) or before != after
            or execution.get('mode') != 'frozen_eval_fp32'
            or execution.get('author_images') != count
            or execution.get('negative_images') != 2 * len(seen_negative)
            or execution.get('reference_feature_forwards') != count + 2 * len(seen_negative)
            or execution.get('reused_historical_vectors') is not False
            or execution.get('rng_preserved') is not True
            or execution.get('synthetic_test_only') is not synthetic):
        raise ValueError('SCR fresh feature execution evidence differs')
    if _fresh_snapshot(snapshot_binding, paths)['data_binding'] != inspected['data_binding']:
        raise ValueError('SCR snapshot changed during cache verification')
    if (file_sha256(paths['reference_cache_manifest']) != manifest_sha
            or file_sha256(paths['reference_cache_vectors']) != vectors['sha256']):
        raise ValueError('SCR cache files changed during verification')
    positive.setflags(write=False)
    negative.setflags(write=False)
    return {'manifest': manifest, 'rows': copy.deepcopy(rows), 'positive': positive, 'negative': negative,
            'data_binding': inspected['data_binding'],
            'cache_binding': _cache_binding(manifest, manifest_sha)}
