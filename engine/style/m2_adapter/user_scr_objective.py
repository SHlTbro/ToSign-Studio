"""S03-bound SCR objective behind explicit generic-runtime qualification.

Official feature loading is confined to deferred runtime constructors.
The private synthetic constructor cannot claim a production cache or runtime.
This is a plain object, so its frozen encoder never enters wrapper optimizers.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager

import torch

from .scr_objective import (
    SCRObjectiveError, SCRTwoNegativeObjective, _load_official_encoder,
    _preserve_rng, _read_json, _tensor_sha,
)
from .user_scr_cache import validate_user_reference_cache
from .user_scr_contract import (
    OBJECTIVE_PROFILE, RECIPE_ID, asset_contract, digest,
    validate_cache_binding, validate_data_binding, validate_user_scr_assets,
)
from .x0_objectives import _fp32_finite, epsilon_with_x0_auxiliary


class UserSCRTwoNegativeObjective(SCRTwoNegativeObjective):
    """Same four-layer two-negative loss, bound to the actual canonical S03 N.

    Constructing this class is a future runtime operation, not preparation
    planning. Validation completes before importing official feature source or
    loading weights. The runner imports this class only after admission checks.
    """

    def __init__(self, asset_paths, snapshot_binding, *, expected_data_binding, expected_cache_binding):
        with _preserve_rng():
            validate_data_binding(expected_data_binding)
            validate_cache_binding(expected_cache_binding, expected_data_binding)
            cache = validate_user_reference_cache(asset_paths, snapshot_binding, verify_assets=False)
            if (cache['data_binding'] != expected_data_binding
                    or cache['cache_binding'] != expected_cache_binding):
                raise SCRObjectiveError('SCR cache differs from the immutable training request')
            self._paths = validate_user_scr_assets(asset_paths)
            self._bind_cache(cache, snapshot_binding, synthetic=False)
            schema = _read_json(self._paths['state_schema'])
            assets = asset_contract()
            if (schema.get('source_sha256') != assets['module_sha256']
                    or schema.get('source_commit') != assets['official_revision']):
                raise SCRObjectiveError('SCR state schema does not bind the exact official source')
            encoder, observed = _load_official_encoder(self._paths, schema)
            self._finish_initialization(encoder, observed, snapshot_binding, synthetic=False)
            # Close source/cache/snapshot mutation during construction before
            # the caller can obtain an objective capable of feature execution.
            fresh = validate_user_reference_cache(self._paths, snapshot_binding)
            if fresh['cache_binding'] != self._cache_binding or fresh['data_binding'] != self._data_binding:
                raise SCRObjectiveError('SCR cache identity changed during construction')

    def _bind_cache(self, cache, snapshot_binding, *, synthetic):
        data = cache['data_binding']
        binding = cache['cache_binding']
        if (binding.get('synthetic_test_only') is not synthetic
                or data['snapshot_sha256'] != snapshot_binding.get('snapshot_sha256')):
            raise SCRObjectiveError('SCR constructor cache kind/snapshot identity differs')
        self._data_binding = copy.deepcopy(data)
        self._cache_binding = copy.deepcopy(binding)
        self._identity_hash = digest([self._data_binding, self._cache_binding])
        self._prepare_cache(cache['rows'], torch.from_numpy(cache['positive'].copy()),
                            torch.from_numpy(cache['negative'].copy()),
                            expected_rows=data['counts']['total'])

    def _check_frozen(self):
        if digest([self._data_binding, self._cache_binding]) != self._identity_hash:
            raise SCRObjectiveError('SCR bound data/cache identity changed')
        return super()._check_frozen()

    def _validate_batch_identity(self, batch_inputs, phase):
        if phase not in ('train', 'val'):
            raise SCRObjectiveError('phase must be train or val')
        if phase == 'val' and torch.is_grad_enabled():
            raise SCRObjectiveError('validation must run under no_grad; validation rows cannot enter optimizer loss')
        if not isinstance(batch_inputs, dict):
            raise SCRObjectiveError('identity-bearing batch dictionary required')
        ids = batch_inputs.get('sample_id')
        labels, splits = batch_inputs.get('label'), batch_inputs.get('split')
        if (not isinstance(ids, (tuple, list)) or not ids
                or not isinstance(labels, (tuple, list)) or len(labels) != len(ids)
                or not isinstance(splits, (tuple, list)) or len(splits) != len(ids)):
            raise SCRObjectiveError('sample_id, label and split require one value per occurrence')
        for sid, label, split in zip(ids, labels, splits):
            if not isinstance(sid, str) or sid not in self._rows:
                raise SCRObjectiveError('unknown author sample_id')
            row = self._rows[sid]
            if label != row['label'] or split != row['split'] or split != phase:
                raise SCRObjectiveError('batch label/split differs from canonical S03 occurrence: ' + sid)

    def loss(self, prediction_x0, batch_inputs, phase):
        self._validate_batch_identity(batch_inputs, phase)
        # The unchanged bridge checks each target's FP32 CHW bytes, frozen
        # state/BN/RNG/cache, and every layer's graph path before InfoNCE.
        return super().loss(prediction_x0, batch_inputs, phase)

    def epsilon_loss(self, epsilon, noise, *, offset_sum, x_t, t, alpha_bar,
                     batch_inputs, phase):
        """Unchanged epsilon + 0.1 offset-square + 0.01 paired SCR math."""
        self._validate_batch_identity(batch_inputs, phase)
        _fp32_finite(offset_sum, 'offset_sum')
        if offset_sum.numel() == 0 or offset_sum.device != epsilon.device:
            raise SCRObjectiveError('offset_sum must be nonempty on the epsilon device')
        return epsilon_with_x0_auxiliary(
            epsilon, noise, offset_penalty=.1 * (offset_sum ** 2).mean(),
            weight=.01, x_t=x_t, t=t, alpha_bar=alpha_bar,
            author_x0=batch_inputs.get('author_target_x0'), clip=True,
            auxiliary=lambda prediction, target: self.loss(
                prediction, {**batch_inputs, 'author_target_x0': target}, phase).mean)

    def evidence(self):
        result = super().evidence()
        result.update(schema='m2-scr2n-user-objective-evidence/1',
                      recipe_id=RECIPE_ID, objective_profile=OBJECTIVE_PROFILE,
                      scr_data_binding=copy.deepcopy(self._data_binding),
                      scr_cache_binding=copy.deepcopy(self._cache_binding),
                      runtime_qualified=False, production_ready=False,
                      m2_launch_authorized=False, validation_optimizer_rows=0)
        return result


def _make_synthetic_user_objective_for_tests(asset_paths, snapshot_binding, encoder_factory,
                                            *, expected_data_binding=None, expected_cache_binding=None):
    """Explicit toy injection only; reject invalid bindings before the factory.

    This accepts only visibly synthetic caches produced by the separate private
    test preparation path. It never imports official source or opens official
    weights; the synthetic cache validator rehashes explicit inert fixture bytes.
    """
    with _preserve_rng():
        cache = validate_user_reference_cache(asset_paths, snapshot_binding,
                                             allow_synthetic=True, verify_assets=False)
        if cache['cache_binding'].get('synthetic_test_only') is not True:
            raise SCRObjectiveError('synthetic factory requires an explicitly synthetic cache')
        if ((expected_data_binding is not None and cache['data_binding'] != expected_data_binding)
                or (expected_cache_binding is not None and cache['cache_binding'] != expected_cache_binding)):
            raise SCRObjectiveError('SCR synthetic cache differs from the expected test binding')
        objective = object.__new__(UserSCRTwoNegativeObjective)
        objective._paths = None
        objective._bind_cache(cache, snapshot_binding, synthetic=True)
        objective._finish_initialization(encoder_factory(), {}, snapshot_binding, synthetic=True)
        return objective


class _OfficialUserSCRReferenceProducer:
    """Deferred official CPU feature producer; no public fixture injection."""

    def __init__(self, asset_paths):
        with _preserve_rng():
            self._paths = validate_user_scr_assets(asset_paths)
            self._assets = asset_contract()
            schema = _read_json(self._paths['state_schema'])
            if (schema.get('source_sha256') != self._assets['module_sha256']
                    or schema.get('source_commit') != self._assets['official_revision']):
                raise SCRObjectiveError('SCR reference producer source schema differs')
            self._encoder, self._observed = _load_official_encoder(self._paths, schema)
            self._initial_state = self._state()
            self._initial_objects = self._objects()
            self._batch_active = False
            self._unusable = False
            self._check_light()
            validate_user_scr_assets(self._paths)

    def _objects(self):
        # Metadata/version checks read no model tensor bytes. Version counters
        # also detect ordinary in-place mutation followed by value restoration.
        return {('module', k): (id(v), type(v)) for k, v in self._encoder.named_modules()} | {
            (kind, k): (id(v), v.data_ptr(), v._version, tuple(v.shape), tuple(v.stride()),
                        v.dtype, v.device, v.layout)
            for kind, values in (('parameter', self._encoder.named_parameters()),
                                 ('buffer', self._encoder.named_buffers())) for k, v in values}

    def _state(self):
        return {k: {'shape': list(v.shape), 'dtype': str(v.dtype), 'sha256': _tensor_sha(v)}
                for k, v in self._encoder.state_dict().items()}

    def _check_light(self, *, require_no_grad=False):
        if self._unusable:
            raise SCRObjectiveError('SCR reference producer failed a previous frozen batch')
        if require_no_grad and torch.is_grad_enabled():
            raise SCRObjectiveError('SCR reference execution requires no_grad')
        if any(module.training for module in self._encoder.modules()):
            raise SCRObjectiveError('SCR reference encoder must remain eval')
        for value in [*self._encoder.parameters(), *self._encoder.buffers()]:
            if (value.device.type != 'cpu' or value.layout != torch.strided
                    or value.requires_grad or value.grad is not None
                    or (value.is_floating_point() and value.dtype != torch.float32)):
                raise SCRObjectiveError('SCR reference encoder must remain frozen CPU FP32')
        if self._objects() != self._initial_objects:
            raise SCRObjectiveError('SCR reference encoder object/version state changed')

    def _check_full(self):
        self._check_light()
        # Strict loading checked all 195 tensors for finiteness. Equality to
        # those complete bytes includes buffers and rejects nonfinite changes,
        # including .data writes that bypass ordinary version counters.
        if self._state() != self._initial_state:
            raise SCRObjectiveError('SCR reference encoder full state changed')

    def _check(self):
        if self._batch_active:
            self._check_light()
        else:
            self._check_full()

    @contextmanager
    def _reference_batch(self):
        """Trusted official forward only; success is withheld until final hash.

        Cache selects this only for this exact factory-owned type. Custom and
        synthetic producers keep their original per-forward full-state policy.
        No partially computed vectors can be published on a failed boundary.
        """
        if self._batch_active:
            raise SCRObjectiveError('SCR reference batches cannot overlap')
        try:
            self._check_full()
            self._batch_active = True
            try:
                yield
            finally:
                # Run even on a feature/rasterizer failure. No callback or
                # source/cache publication occurs after this successful exit.
                self._check_full()
        except BaseException:
            self._unusable = True
            raise
        finally:
            self._batch_active = False

    def identity(self):
        self._check()
        return {'scr_assets_sha256': self._assets['sha256'], 'dtype': 'float32',
                'eval': True, 'all_parameters_frozen': True, 'synthetic_test_only': False}

    def state_sha256(self):
        self._check()
        return digest(self._initial_state)

    def encode(self, images):
        import numpy as np
        if not isinstance(images, np.ndarray) or images.dtype != np.dtype('float32'):
            raise SCRObjectiveError('SCR reference input must be FP32 NumPy BCHW')
        value = torch.from_numpy(np.ascontiguousarray(images).copy())
        SCRTwoNegativeObjective._image(value, 'reference images')
        input_sha256 = _tensor_sha(value)
        self._check()
        with _preserve_rng(require_unchanged=True), torch.no_grad():
            self._check_light(require_no_grad=True)
            try:
                output = self._encoder(value)
            finally:
                if _tensor_sha(value) != input_sha256:
                    raise SCRObjectiveError('SCR reference encoder changed its input')
                self._check_light(require_no_grad=True)
                self._check()
            if not isinstance(output, (tuple, list)) or len(output) != 4:
                raise SCRObjectiveError('SCR reference encoder requires four projected layers')
            for feature in output:
                _fp32_finite(feature, 'reference features')
                if feature.shape != (len(images), 2048) or feature.device.type != 'cpu' or feature.requires_grad:
                    raise SCRObjectiveError('SCR reference feature shape/device/gradient differs')
            return torch.stack(output, dim=1).contiguous().numpy().copy()


def make_user_scr_reference_producer(asset_paths):
    """Runtime-only strict195/weights_only factory; never called by the backend."""
    return _OfficialUserSCRReferenceProducer(asset_paths)
