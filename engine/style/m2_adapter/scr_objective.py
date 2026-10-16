"""Pinned, frozen SCR query bridge for the isolated two-negative experiment.

Only the production constructor can load official assets. The separate private
synthetic factory exists for model-free tests and never imports official code.
This is deliberately not an nn.Module: attaching it to a training wrapper does
not register SCR parameters or add them to that wrapper's optimizer/checkpoint.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import random
from types import MappingProxyType

import numpy as np
import torch
from torch import Tensor, nn

from .scr_contract import asset_contract, digest, validate_scr_asset_paths
from .scr_paired_objective import PairedTerms, paired_two_negative_info_nce
from .x0_objectives import ObjectiveInputError, _fp32_finite, _has_path


class SCRObjectiveError(ObjectiveInputError):
    """The pinned input, cache, graph or frozen-state contract was violated."""


def _read_json(path):
    def invalid(value):
        raise SCRObjectiveError('non-finite JSON constant: ' + value)
    return json.loads(Path(path).read_text(encoding='utf-8'), parse_constant=invalid)


def _tensor_sha(value):
    return hashlib.sha256(value.detach().cpu().contiguous().numpy().tobytes()).hexdigest()


def _rng_state():
    # Do not initialize CUDA merely to inspect its RNG.
    return (random.getstate(), np.random.get_state(), torch.random.get_rng_state().clone(),
            tuple(s.clone() for s in torch.cuda.get_rng_state_all()) if torch.cuda.is_initialized() else ())


def _rng_equal(left, right):
    return (left[0] == right[0] and left[1][0] == right[1][0]
            and np.array_equal(left[1][1], right[1][1]) and left[1][2:] == right[1][2:]
            and torch.equal(left[2], right[2]) and len(left[3]) == len(right[3])
            and all(torch.equal(a, b) for a, b in zip(left[3], right[3])))


@contextmanager
def _preserve_rng(*, require_unchanged=False):
    state = _rng_state()
    try:
        yield
        if require_unchanged and not _rng_equal(state, _rng_state()):
            raise SCRObjectiveError('SCR feature/cache path consumed RNG')
    finally:
        random.setstate(state[0])
        np.random.set_state(state[1])
        torch.random.set_rng_state(state[2])
        if state[3]:
            torch.cuda.set_rng_state_all(list(state[3]))


def _validate_checkpoint(state, schema):
    expected = schema.get('state') if isinstance(schema, dict) else None
    if (not isinstance(expected, dict) or len(expected) != 195
            or schema.get('state_key_count') != 195 or not isinstance(state, dict)
            or set(state) != set(expected)):
        raise SCRObjectiveError('checkpoint must have the exact pinned 195 state keys')
    observed = {}
    for name, want in expected.items():
        value = state[name]
        if (not isinstance(value, Tensor) or value.device.type != 'cpu'
                or value.layout != torch.strided or value.requires_grad
                or list(value.shape) != want['shape']
                or str(value.dtype).removeprefix('torch.') != want['dtype']
                or value.numel() != want['numel'] or not bool(torch.isfinite(value).all())):
            raise SCRObjectiveError('checkpoint tensor differs from pinned schema: ' + name)
        observed[name] = {'shape': list(value.shape), 'dtype': want['dtype'],
                          'numel': value.numel(), 'finite': True, 'layout': str(value.layout)}
    return observed


def _make_style_extractor(module, encoder):
    class StyleExtractor(module.StyleExtractor):
        def forward(self, input, index):
            # Keep the pinned forward/state; dim-max selects the first tied
            # spatial value and has a strict deterministic CUDA backward.
            feats = self.encode_with_intermediate(input)
            codes = []
            for x in index.split(','):
                code = feats[int(x)].clone()
                gap = torch.nn.functional.adaptive_avg_pool2d(code, (1, 1))
                gmp = code.flatten(2).max(dim=2, keepdim=True).values.unsqueeze(-1)
                conv1x1 = getattr(self, 'conv1x1_{:d}'.format(int(x)))
                code = torch.cat([gap, gmp], 1)
                codes.append(self.relu(conv1x1(code)))
            return codes

    return StyleExtractor(encoder)


def _load_official_encoder(paths, schema):
    # Explicit CPU/weights-only loading, with no key rewriting or partial load.
    state = torch.load(paths['checkpoint'], map_location='cpu', weights_only=True)
    observed = _validate_checkpoint(state, schema)
    loaded_hashes = {name: _tensor_sha(value) for name, value in state.items()}
    # The pinned source has global vgg initialization. Its import and all module
    # construction run inside the constructor's RNG-preserving boundary.
    spec = importlib.util.spec_from_file_location('_m2_pinned_scr_modules', paths['module'])
    if spec is None or spec.loader is None:
        raise SCRObjectiveError('cannot load the pinned SCR module')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Features(nn.Module):
        def __init__(self):
            super().__init__()
            encoder = nn.Sequential(*list(module.vgg.children()))
            self.StyleFeatExtractor = _make_style_extractor(module, encoder)
            self.StyleFeatProjector = module.Projector()

        def forward(self, x):
            return self.StyleFeatProjector(self.StyleFeatExtractor(x, '0,1,2,3'), '0,1,2,3')

    encoder = Features()
    encoder.load_state_dict(state, strict=True)
    encoder.requires_grad_(False).eval()
    if {name: _tensor_sha(value) for name, value in encoder.state_dict().items()} != loaded_hashes:
        raise SCRObjectiveError('loaded SCR state differs from the complete checkpoint')
    return encoder, observed


def _validate_reference_manifest(manifest, binding):
    assets = asset_contract()
    required = {'schema': 'm2-scr2n-reference-cache/1',
                'status': 'COPIED_SAVED_VECTORS_NO_FEATURE_EXECUTION',
                'snapshot_sha256': assets['snapshot_sha256'], 'layers': [0, 1, 2, 3],
                'dtype': 'float32', 'dimensions': 2048, 'positive_shape': [72, 4, 2048],
                'negative_shape': [72, 4, 2, 2048],
                'negative_font_order': ['noto_sans_cjk_sc_regular', 'noto_serif_cjk_sc_regular']}
    if (not isinstance(manifest, dict) or any(manifest.get(k) != v for k, v in required.items())
            or not isinstance(binding, dict) or binding.get('snapshot_sha256') != assets['snapshot_sha256']):
        raise SCRObjectiveError('reference cache/snapshot identity differs from the pinned experiment')
    for key, want in (('preprocessing_identity', assets['preprocessing_identity_sha256']),
                      ('source_vector_archive', assets['original_vector_archive_sha256']),
                      ('source_input_manifest', assets['input_manifest_sha256']),
                      ('vectors', assets['reference_cache_vectors_sha256'])):
        if not isinstance(manifest.get(key), dict) or manifest[key].get('sha256') != want:
            raise SCRObjectiveError('reference cache lineage mismatch: ' + key)
    from engine.style.dataset.binding import verify_snapshot_binding
    verified = verify_snapshot_binding(binding.get('snapshot_dir'), assets['snapshot_sha256'])
    if verified != binding:
        raise SCRObjectiveError('snapshot binding differs from fresh verification')
    root = Path(verified['snapshot_dir'])
    samples = {row['sample_id']: row for row in
               (_read_json_line(line) for line in (root / 'samples.jsonl').read_text().splitlines() if line.strip())}
    split = _read_json(root / 'split.json')['assignments']
    files = _read_json(root / 'index.json')['files']
    rows = manifest.get('rows')
    if (not isinstance(rows, list) or len(rows) != 72
            or len({row['sample_id'] for row in rows}) != 72
            or set(row['sample_id'] for row in rows) != set(samples)
            or sum(row['split'] == 'train' for row in rows) != 51):
        raise SCRObjectiveError('reference rows do not cover the exact 72-row snapshot')
    for row in rows:
        sid = row['sample_id']
        label = samples[sid]['label']
        if (row['label'] != label or row['split'] != split[sid]
                or row['source_png_sha256'] != files[sid]['sha256']
                or row['positive_input_id'] != 'author_' + sid
                or row['negative_input_ids'] != [f'control_{font}_U+{ord(label):04X}' for font in required['negative_font_order']]):
            raise SCRObjectiveError('reference row target/font/split mismatch: ' + sid)
    return rows


def _read_json_line(line):
    return json.loads(line)


class SCRTwoNegativeObjective:
    def __init__(self, paths, snapshot_binding):
        with _preserve_rng():
            self._paths = validate_scr_asset_paths(paths)
            schema = _read_json(self._paths['state_schema'])
            assets = asset_contract()
            if (schema.get('source_sha256') != assets['module_sha256']
                    or schema.get('source_commit') != assets['official_revision']):
                raise SCRObjectiveError('static state schema does not bind the official source')
            manifest = _read_json(self._paths['reference_cache_manifest'])
            rows = _validate_reference_manifest(manifest, snapshot_binding)
            # Never open the original archive, which also contains generated
            # features: only the pinned positive/negative reference cache.
            with np.load(self._paths['reference_cache_vectors'], allow_pickle=False) as archive:
                if set(archive.files) != {'positive', 'negative'}:
                    raise SCRObjectiveError('reference archive must contain only positive and negative')
                positive = torch.from_numpy(archive['positive'].copy())
                negative = torch.from_numpy(archive['negative'].copy())
            self._prepare_cache(rows, positive, negative, expected_rows=72)
            encoder, observed = _load_official_encoder(self._paths, schema)
            self._finish_initialization(encoder, observed, snapshot_binding, synthetic=False)
            if validate_scr_asset_paths(self._paths) != self._paths:
                raise SCRObjectiveError('SCR asset paths changed during construction')

    def _prepare_cache(self, rows, positive, negative, *, expected_rows):
        for value, name, shape in ((positive, 'positive cache', (expected_rows, 4, 2048)),
                                   (negative, 'negative cache', (expected_rows, 4, 2, 2048))):
            _fp32_finite(value, name)
            if tuple(value.shape) != shape or value.device.type != 'cpu':
                raise SCRObjectiveError(name + ' has an unexpected shape/device')
            norms = torch.linalg.vector_norm(value, dim=-1)
            if not bool(torch.isfinite(norms).all()) or bool((norms <= 1e-12).any()):
                raise SCRObjectiveError(name + ' contains invalid vector norms')
        if len(rows) != expected_rows:
            raise SCRObjectiveError('reference row count differs from cache')
        self._rows = {}
        for index, row in enumerate(rows):
            sid, target_hash = row.get('sample_id'), row.get('author_target_normalized_fp32_chw_sha256')
            if (not isinstance(sid, str) or not sid or sid in self._rows
                    or row.get('split') not in ('train', 'val') or not isinstance(row.get('label'), str)
                    or not isinstance(target_hash, str) or len(target_hash) != 64
                    or any(ch not in '0123456789abcdef' for ch in target_hash)):
                raise SCRObjectiveError('malformed/duplicate reference row identity')
            self._rows[sid] = MappingProxyType({'index': index, 'sample_id': sid, 'label': row['label'],
                'split': row['split'], 'author_target_normalized_fp32_chw_sha256': target_hash})
        self._rows = MappingProxyType(self._rows)
        self._positive = positive.detach().clone()
        self._negative = negative.detach().clone()
        self._cache_hashes = (_tensor_sha(self._positive), _tensor_sha(self._negative))

    def _finish_initialization(self, encoder, observed, snapshot_binding, *, synthetic):
        if not isinstance(encoder, nn.Module) or any(p.grad is not None for p in encoder.parameters()):
            raise SCRObjectiveError('encoder must be a module without existing parameter gradients')
        self._encoder = encoder.requires_grad_(False).eval()
        self._observed_state = copy.deepcopy(observed)
        self._snapshot_binding = copy.deepcopy(snapshot_binding)
        self._synthetic = synthetic
        self._device = torch.device('cpu')
        self._calls = {'train': 0, 'val': 0}
        self._lookups = {'train': 0, 'val': 0}
        self._prediction_feature_forwards = 0
        self._prediction_images = 0
        self._target_hash_checks = 0
        self._state_before = self._state_hashes()
        self._state_objects = self._objects()
        self._check_frozen()

    def _objects(self):
        return {('parameter', name): id(value) for name, value in self._encoder.named_parameters()} | {
                ('buffer', name): id(value) for name, value in self._encoder.named_buffers()}

    def _state_hashes(self):
        values = dict(self._encoder.named_parameters()) | dict(self._encoder.named_buffers())
        return {name: {'shape': list(value.shape), 'dtype': str(value.dtype), 'sha256': _tensor_sha(value)}
                for name, value in sorted(values.items())}

    def _check_frozen(self):
        if any(module.training for module in self._encoder.modules()):
            raise SCRObjectiveError('SCR must remain in frozen eval mode')
        for value in list(self._encoder.parameters()) + list(self._encoder.buffers()):
            if (value.device != self._device or value.layout != torch.strided
                    or value.requires_grad or value.grad is not None or not bool(torch.isfinite(value).all())
                    or (value.is_floating_point() and value.dtype != torch.float32)):
                raise SCRObjectiveError('SCR parameters/buffers must remain frozen finite FP32')
        current = self._state_hashes()
        if current != self._state_before or self._objects() != self._state_objects:
            raise SCRObjectiveError('frozen SCR full state changed')
        if (self._positive.requires_grad or self._negative.requires_grad
                or (_tensor_sha(self._positive), _tensor_sha(self._negative)) != self._cache_hashes):
            raise SCRObjectiveError('frozen reference feature cache changed')
        return current

    def to(self, device):
        device = torch.device(device)
        if device.type not in ('cpu', 'cuda'):
            raise SCRObjectiveError('SCR device must be CPU or CUDA')
        self._check_frozen()
        with _preserve_rng():
            self._encoder.to(device=device)
        self._device = next(iter(self._encoder.parameters()), torch.empty(0, device=device)).device
        self._state_objects = self._objects()
        self._check_frozen()
        return self

    @staticmethod
    def _image(value, name):
        _fp32_finite(value, name)
        if value.ndim != 4 or tuple(value.shape[1:]) != (3, 96, 96) or value.shape[0] < 1:
            raise SCRObjectiveError(name + ' must have Bx3x96x96 shape')
        if bool((value.abs() > 1).any()):
            raise SCRObjectiveError(name + ' must already be normalized/clipped to [-1,1]')

    def loss(self, prediction_x0, batch_inputs, phase) -> PairedTerms:
        if phase not in ('train', 'val'):
            raise SCRObjectiveError('phase must be train or val')
        self._image(prediction_x0, 'prediction_x0')
        if prediction_x0.device != self._device:
            raise SCRObjectiveError('prediction and SCR device differ; call objective.to(device) explicitly')
        if not isinstance(batch_inputs, dict):
            raise SCRObjectiveError('batch_inputs must be an identity-bearing dictionary')
        target = batch_inputs.get('author_target_x0')
        self._image(target, 'author_target_x0')
        if target.shape != prediction_x0.shape or target.device != prediction_x0.device:
            raise SCRObjectiveError('author target and prediction shape/device differ')
        ids = batch_inputs.get('sample_id')
        if not isinstance(ids, (list, tuple)) or len(ids) != prediction_x0.shape[0]:
            raise SCRObjectiveError('sample_id must contain one exact ID per batch row')
        if phase == 'train' and (not torch.is_grad_enabled() or not prediction_x0.requires_grad):
            raise SCRObjectiveError('training SCR prediction requires an enabled gradient graph')
        indices = []
        for index, sid in enumerate(ids):
            if not isinstance(sid, str) or sid not in self._rows:
                raise SCRObjectiveError('unknown author sample_id')
            row = self._rows[sid]
            if row['split'] != phase:
                raise SCRObjectiveError('reference cache lookup crosses train/val split: ' + sid)
            if _tensor_sha(target[index]) != row['author_target_normalized_fp32_chw_sha256']:
                raise SCRObjectiveError('author_target_x0 bytes differ from the exact cached occurrence: ' + sid)
            self._target_hash_checks += 1
            indices.append(row['index'])
        self._check_frozen()
        with _preserve_rng(require_unchanged=True):
            try:
                self._prediction_feature_forwards += 1
                self._prediction_images += prediction_x0.shape[0]
                outputs = self._encoder(prediction_x0)
            finally:
                self._check_frozen()
            if not isinstance(outputs, (list, tuple)) or len(outputs) != 4:
                raise SCRObjectiveError('SCR must return the four projected layer tensors')
            for output in outputs:
                _fp32_finite(output, 'projected query')
                if output.shape != (prediction_x0.shape[0], 2048) or output.device != prediction_x0.device:
                    raise SCRObjectiveError('projected query must have Bx2048 shape on the prediction device')
                if torch.is_grad_enabled() and prediction_x0.requires_grad and not _has_path(output, prediction_x0):
                    raise SCRObjectiveError('SCR query detached the prediction gradient graph')
            query = torch.stack(outputs, dim=1)
            positive = self._positive[indices].to(device=query.device).detach()
            negative = self._negative[indices].to(device=query.device).detach()
            result = paired_two_negative_info_nce(query, positive, negative)
            if torch.is_grad_enabled() and prediction_x0.requires_grad and not _has_path(result.mean, prediction_x0):
                raise SCRObjectiveError('SCR objective detached the prediction gradient graph')
        self._calls[phase] += 1
        self._lookups[phase] += len(indices)
        return result

    def evidence(self):
        current = self._check_frozen()
        state_proof = {'state_keys': len(current), 'weights_only': not self._synthetic,
                       'strict_load': not self._synthetic, 'all_finite': True,
                       'eval': not any(m.training for m in self._encoder.modules()),
                       'all_parameters_frozen': all(not p.requires_grad for p in self._encoder.parameters()),
                       'parameter_gradients_absent': all(p.grad is None for p in self._encoder.parameters()),
                       'before_sha256': digest(self._state_before), 'after_sha256': digest(current),
                       'equal': current == self._state_before}
        cache_proof = {'rows': len(self._rows),
                       'train_rows': sum(row['split'] == 'train' for row in self._rows.values()),
                       'val_rows': sum(row['split'] == 'val' for row in self._rows.values()),
                       'negative_count': 2, 'positive_negative_forward_calls': 0,
                       'pixel_hashes_checked': self._target_hash_checks > 0,
                       'pixel_hash_check_count': self._target_hash_checks}
        return {'schema': 'm2-scr2n-objective-evidence/1', 'synthetic_test_only': self._synthetic,
                'scr_feature_state': state_proof, 'scr_cached_reference_proof': cache_proof,
                'scr_rng_preserved': True,
                'snapshot_binding': copy.deepcopy(self._snapshot_binding),
                'checkpoint_observed_state': copy.deepcopy(self._observed_state),
                'weights_only': not self._synthetic, 'map_location': 'cpu',
                'partial_load': False, 'prefix_stripping': False,
                'model_state_before': copy.deepcopy(self._state_before), 'model_state_after': current,
                'full_state_sha256': digest(current), 'frozen_eval': True,
                'trainable_parameter_count': 0, 'reference_feature_forwards': 0,
                'reference_tensors_gradient_absent': all(not value.requires_grad and value.grad is None
                                                         for value in (self._positive, self._negative)),
                'generated_feature_cache_lookups': 0,
                'prediction_feature_forwards': self._prediction_feature_forwards,
                'prediction_feature_images': self._prediction_images,
                'completed_loss_calls': dict(self._calls), 'reference_row_lookups': dict(self._lookups),
                'positive_vector_lookups': sum(self._lookups.values()) * 4,
                'negative_vector_lookups': sum(self._lookups.values()) * 8,
                'cache_tensor_sha256': list(self._cache_hashes),
                'rng_preserved_during_construction': True, 'feature_rng_unchanged': True,
                'registered_in_wrapper_parameters_or_state': False}


def _make_synthetic_objective_for_tests(encoder_factory, rows, positive, negative):
    """Internal toy-only test fixture; never used by the production constructor.

    No paths, checkpoint or source substitutions are accepted by this factory.
    Its evidence is explicitly synthetic and carries no production asset claims.
    """
    with _preserve_rng():
        objective = object.__new__(SCRTwoNegativeObjective)
        objective._paths = None
        objective._prepare_cache(rows, positive, negative, expected_rows=len(rows))
        objective._finish_initialization(encoder_factory(), {}, {'synthetic_test_only': True}, synthetic=True)
    return objective
