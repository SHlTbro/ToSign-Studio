"""Experimental, unwired differentiable-x0 plumbing for FP32 epsilon models.

No feature weights, style metric, new recipe, or model-loading path is supplied.
The caller must pass the actual author target used to construct ``x_t``; a
content-font condition or a different style reference is not that target.
Clipping is an explicit auxiliary-path choice and never changes epsilon MSE.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from typing import Callable

import torch
from torch import Tensor, nn
from torch.nn import functional as F


class ObjectiveInputError(ValueError):
    """Malformed or unsupported objective inputs; do not continue training."""


def _fp32_finite(value: Tensor, name: str) -> None:
    if not isinstance(value, Tensor) or value.dtype != torch.float32:
        raise ObjectiveInputError(f'{name} must be a float32 tensor')
    if value.device.type not in ('cpu', 'cuda'):
        raise ObjectiveInputError(f'{name} must have real CPU or CUDA storage')
    if value.layout != torch.strided or not bool(torch.isfinite(value).all()):
        raise ObjectiveInputError(f'{name} must be dense and finite')


def _image(value: Tensor, name: str, like: Tensor | None = None) -> None:
    _fp32_finite(value, name)
    if value.ndim != 4 or value.shape[1] != 3 or min(value.shape) < 1:
        raise ObjectiveInputError(f'{name} must have nonempty Bx3xHxW shape')
    if like is not None and (value.shape != like.shape or value.device != like.device):
        raise ObjectiveInputError(f'{name} shape/device differs from epsilon')


def _scalar(value: Tensor, name: str, like: Tensor) -> None:
    _fp32_finite(value, name)
    if value.ndim != 0 or value.device != like.device:
        raise ObjectiveInputError(f'{name} must be a scalar on the epsilon device')


def _has_path(output: Tensor, source: Tensor) -> bool:
    """Check graph connectivity without executing backward or setting .grad.

    A connected zero derivative (for example clipping) is valid. An unrelated
    leaf with requires_grad=True is not evidence of a prediction-input path.
    """
    if output is source:
        return True
    target = (source.grad_fn, source.output_nr)
    pending = [(output.grad_fn, output.output_nr)]
    visited = set()
    while pending:
        node, output_nr = pending.pop()
        edge = (node, output_nr)
        if node is None or edge in visited:
            continue
        if edge == target or (target[0] is None and getattr(node, 'variable', None) is source):
            return True
        visited.add(edge)
        # Multiple outputs (e.g. split) share a node but have different slots.
        pending.extend(node.next_functions)
    return False


def analytic_x0(epsilon: Tensor, x_t: Tensor, t: Tensor,
                alpha_bar: Tensor, *, clip: bool = False) -> Tensor:
    """Compute x0 without scheduler variance draws or detaching its graph.

    ``alpha_bar`` is a fixed 1-D cumulative-alpha schedule, not beta or SNR.
    All tensors must already be on the same device and use explicit FP32;
    timesteps use int64. No implicit transfer, broadcasting, or casting occurs.
    """
    _image(epsilon, 'epsilon')
    _image(x_t, 'x_t', epsilon)
    if type(clip) is not bool:
        raise ObjectiveInputError('clip must be an explicit bool')
    _fp32_finite(alpha_bar, 'alpha_bar')
    if (alpha_bar.ndim != 1 or alpha_bar.numel() == 0
            or alpha_bar.device != epsilon.device or alpha_bar.requires_grad
            or not bool(((alpha_bar > 0) & (alpha_bar <= 1)).all())
            or bool((alpha_bar[1:] > alpha_bar[:-1]).any())):
        raise ObjectiveInputError('alpha_bar must be a fixed, positive, nonincreasing cumulative schedule <=1')
    if (not isinstance(t, Tensor) or t.dtype != torch.int64
            or t.ndim != 1 or t.shape[0] != epsilon.shape[0]
            or t.device != epsilon.device or t.layout != torch.strided):
        raise ObjectiveInputError('t must be int64[B] on the epsilon device')
    if bool(((t < 0) | (t >= alpha_bar.numel())).any()):
        raise ObjectiveInputError('timestep outside the cumulative-alpha schedule')
    alpha = alpha_bar[t].reshape(-1, 1, 1, 1)
    raw = (x_t - (1 - alpha).sqrt() * epsilon) / alpha.sqrt()
    _image(raw, 'raw_x0', epsilon)
    return raw.clamp(-1, 1) if clip else raw


@dataclass(frozen=True)
class ObjectiveTerms:
    total: Tensor
    epsilon_mse: Tensor
    offset: Tensor
    auxiliary_loss: Tensor | None
    raw_x0: Tensor | None
    objective_x0: Tensor | None
    auxiliary_evaluated: bool


def epsilon_with_x0_auxiliary(
    epsilon: Tensor, noise: Tensor, *, offset_penalty: Tensor,
    weight: float = 0.0, x_t: Tensor | None = None,
    t: Tensor | None = None, alpha_bar: Tensor | None = None,
    author_x0: Tensor | None = None,
    auxiliary: Callable[[Tensor, Tensor], Tensor] | None = None,
    clip: bool = True,
) -> ObjectiveTerms:
    """Add a caller-supplied scalar objective to unchanged epsilon+offset loss.

    Weight zero skips every auxiliary input and callback, including clipping,
    and performs the exact legacy expression ``F.mse_loss(epsilon,noise)+offset``.
    Positive weight detaches only the author target. This helper defines no
    style/content distance and makes no claim that a supplied loss is suitable.
    """
    if isinstance(weight, bool) or not isinstance(weight, Real) or not math.isfinite(weight) or weight < 0:
        raise ObjectiveInputError('weight must be a finite nonnegative real, excluding bool')
    _image(epsilon, 'epsilon')
    _image(noise, 'noise', epsilon)
    _scalar(offset_penalty, 'offset_penalty', epsilon)
    epsilon_loss = F.mse_loss(epsilon, noise)
    base = epsilon_loss + offset_penalty
    _scalar(base, 'base_loss', epsilon)
    if weight == 0:
        return ObjectiveTerms(base, epsilon_loss, offset_penalty, None, None, None, False)
    if not callable(auxiliary):
        raise ObjectiveInputError('positive weight requires a callable auxiliary')
    if type(clip) is not bool:
        raise ObjectiveInputError('clip must be an explicit bool')
    _image(author_x0, 'author_x0', epsilon)
    if bool((author_x0.abs() > 1).any()):
        raise ObjectiveInputError('author_x0 must be the normalized author target in [-1,1]')
    raw = analytic_x0(epsilon, x_t, t, alpha_bar, clip=False)
    objective_x0 = raw.clamp(-1, 1) if clip else raw
    extra = auxiliary(objective_x0, author_x0.detach())
    _scalar(extra, 'auxiliary_loss', epsilon)
    if torch.is_grad_enabled() and objective_x0.requires_grad and not _has_path(extra, objective_x0):
        raise ObjectiveInputError('auxiliary_loss detached the generated-image graph')
    total = base + float(weight) * extra
    _scalar(total, 'total_loss', epsilon)
    return ObjectiveTerms(total, epsilon_loss, offset_penalty, extra, raw, objective_x0, True)


class FrozenFeaturePair(nn.Module):
    """Encode prediction with autograd and target without it, in frozen eval mode.

    The caller supplies the feature module and any explicit image preprocessing.
    This wrapper never loads weights, changes normalization, resizes, or selects
    a style metric. It must not be shared with a training-mode feature consumer.
    """

    def __init__(self, encoder: nn.Module):
        super().__init__()
        if not isinstance(encoder, nn.Module):
            raise ObjectiveInputError('encoder must be a supplied torch module')
        if any(p.grad is not None for p in encoder.parameters()):
            raise ObjectiveInputError('encoder must have no existing parameter gradients')
        self.encoder = encoder.requires_grad_(False)
        self.train(False)

    def train(self, mode: bool = True):
        if type(mode) is not bool:
            raise ObjectiveInputError('mode must be bool')
        # Parent training-mode propagation must never re-enable BN or dropout.
        super().train(False)
        return self

    def _check_frozen(self, device) -> None:
        if any(module.training for module in self.encoder.modules()):
            raise ObjectiveInputError('feature encoder must remain in eval mode')
        for param in self.encoder.parameters():
            _fp32_finite(param, 'feature parameter')
            if param.device != device or param.requires_grad or param.grad is not None:
                raise ObjectiveInputError('feature parameters must remain frozen on the input device')
        for buf in self.encoder.buffers():
            if buf.device != device or buf.requires_grad or not bool(torch.isfinite(buf).all()):
                raise ObjectiveInputError('feature buffers must remain fixed/finite on the input device')
            if buf.is_floating_point() and buf.dtype != torch.float32:
                raise ObjectiveInputError('floating feature buffers must be float32')

    @staticmethod
    def _features(value, image: Tensor, *, need_grad: bool, forbid_grad: bool = False):
        if isinstance(value, Tensor):
            _fp32_finite(value, 'features')
            if value.ndim < 1 or value.shape[0] != image.shape[0] or value.numel() == 0 or value.device != image.device:
                raise ObjectiveInputError('feature batch/device must match the image')
            if need_grad and not _has_path(value, image):
                raise ObjectiveInputError('feature encoder detached the prediction graph')
            if forbid_grad and value.requires_grad:
                raise ObjectiveInputError('target features must have no gradient graph')
            return
        if isinstance(value, (tuple, list)) and value:
            for child in value:
                FrozenFeaturePair._features(child, image, need_grad=need_grad, forbid_grad=forbid_grad)
            return
        raise ObjectiveInputError('features must be tensors or nonempty tensor sequences')

    @staticmethod
    def _matching_features(pred, target) -> bool:
        if isinstance(pred, Tensor) and isinstance(target, Tensor):
            return pred.shape == target.shape
        if type(pred) is not type(target) or len(pred) != len(target):
            return False
        return all(FrozenFeaturePair._matching_features(p, t) for p, t in zip(pred, target))

    def forward(self, prediction: Tensor, target: Tensor):
        _image(prediction, 'prediction')
        _image(target, 'target', prediction)
        self._check_frozen(prediction.device)
        # Strict experimental state guard, including .data mutations that bypass
        # Tensor version counters. Real feature assets require a separate memory
        # qualification: this snapshot temporarily duplicates their state.
        state = {('parameter', n): p for n, p in self.encoder.named_parameters()}
        state.update({('buffer', n): b for n, b in self.encoder.named_buffers()})
        before = {key: (value, value.detach().clone()) for key, value in state.items()}
        pred_features = self.encoder(prediction)
        with torch.no_grad():
            target_features = self.encoder(target.detach())
        after = {('parameter', n): p for n, p in self.encoder.named_parameters()}
        after.update({('buffer', n): b for n, b in self.encoder.named_buffers()})
        if before.keys() != after.keys() or any(old is not after[key] or not torch.equal(value, after[key])
                                               for key, (old, value) in before.items()):
            raise ObjectiveInputError('feature forward changed frozen parameters or buffers')
        self._check_frozen(prediction.device)
        self._features(pred_features, prediction, need_grad=torch.is_grad_enabled() and prediction.requires_grad)
        self._features(target_features, target, need_grad=False, forbid_grad=True)
        if not self._matching_features(pred_features, target_features):
            raise ObjectiveInputError('prediction and target feature structures/shapes differ')
        return pred_features, target_features
