"""Own-code, unwired two-negative tensor objective. No model or asset loader.

The experiment fixes four layers, T=0.07, and coefficient 0.01. Positives and
the two same-character font negatives are caller-bound cached constants.
This simplified loss is not the original 16-negative augmented SCR method.
"""
from dataclasses import dataclass
import math
from numbers import Real
from typing import Callable

import torch
from torch import Tensor
from torch.nn import functional as F

TEMPERATURE = 0.07
COEFFICIENT = 0.01
LAYERS = 4
NEGATIVES = 2


class PairedObjectiveError(ValueError):
    pass


def _tensor(x, name):
    if (not isinstance(x, Tensor) or x.dtype != torch.float32 or
            x.layout != torch.strided or x.device.type not in ('cpu', 'cuda') or
            not bool(torch.isfinite(x).all())):
        raise PairedObjectiveError(name + ' must be a finite dense FP32 tensor')


def _normalize(x, name):
    norms = torch.linalg.vector_norm(x, dim=-1, keepdim=True)
    if not bool(torch.isfinite(norms).all()) or bool((norms <= 1e-12).any()):
        raise PairedObjectiveError(name + ' has a zero/underflow/overflow vector norm')
    return x / norms


@dataclass(frozen=True)
class PairedTerms:
    mean: Tensor
    per_sample_layer: Tensor
    logits: Tensor


def paired_two_negative_info_nce(query: Tensor, positive: Tensor,
                                 negatives: Tensor) -> PairedTerms:
    """Mean of Bx4 paired cross-entropies, positive at logit index zero.

    Shapes: query/positive Bx4xD, negatives Bx4x2xD. The query graph is
    preserved. Positive and negative values are detached before normalization.
    No random operation, hidden cross-batch negative or supplied temperature.
    """
    for x, name in ((query, 'query'), (positive, 'positive'), (negatives, 'negatives')):
        _tensor(x, name)
    if query.ndim != 3 or query.shape[0] < 1 or query.shape[1] != LAYERS or query.shape[2] < 2:
        raise PairedObjectiveError('query must have Bx4xD shape with B>=1,D>=2')
    if positive.shape != query.shape or negatives.shape != (*query.shape[:2], NEGATIVES, query.shape[-1]):
        raise PairedObjectiveError('positive/negatives must have matching Bx4xD/Bx4x2xD shapes')
    if positive.device != query.device or negatives.device != query.device:
        raise PairedObjectiveError('all features must share a device')
    q = _normalize(query, 'query')
    p = _normalize(positive.detach(), 'positive')
    n = _normalize(negatives.detach(), 'negatives')
    positive_cos = (q * p).sum(dim=-1, keepdim=True)
    negative_cos = (q.unsqueeze(-2) * n).sum(dim=-1)
    logits = torch.cat((positive_cos, negative_cos), dim=-1) / TEMPERATURE
    per_sample_layer = torch.logsumexp(logits, dim=-1) - logits[..., 0]
    mean = per_sample_layer.mean()
    _tensor(mean, 'loss')
    return PairedTerms(mean, per_sample_layer, logits)


@dataclass(frozen=True)
class CombinedTerms:
    total: Tensor
    base: Tensor
    auxiliary: PairedTerms | None
    evaluated: bool


def add_paired_auxiliary(base_loss: Tensor, *, coefficient: float = 0.0,
                        feature_triplet: Callable | None = None) -> CombinedTerms:
    """Zero weight returns the original base tensor and never calls the supplier.

    Base loss must already be the unmodified epsilon MSE plus offset penalty.
    Positive experiment weight is fixed to 0.01; no tuning surface is exposed.
    """
    _tensor(base_loss, 'base_loss')
    if base_loss.ndim != 0:
        raise PairedObjectiveError('base_loss must be a scalar')
    if (isinstance(coefficient, bool) or not isinstance(coefficient, Real) or
            not math.isfinite(coefficient) or coefficient not in (0.0, COEFFICIENT)):
        raise PairedObjectiveError('coefficient must be exactly 0 or the frozen 0.01')
    if coefficient == 0:
        return CombinedTerms(base_loss, base_loss, None, False)
    if not callable(feature_triplet):
        raise PairedObjectiveError('positive coefficient requires a feature supplier')
    triplet = feature_triplet()
    if not isinstance(triplet, (tuple, list)) or len(triplet) != 3:
        raise PairedObjectiveError('feature supplier must return query, positive, negatives')
    terms = paired_two_negative_info_nce(*triplet)
    if terms.mean.device != base_loss.device:
        raise PairedObjectiveError('base loss and auxiliary device differ')
    total = base_loss + COEFFICIENT * terms.mean
    _tensor(total, 'total_loss')
    return CombinedTerms(total, base_loss, terms, True)
