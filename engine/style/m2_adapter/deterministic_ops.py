"""Deterministic DeformConv2d execution without changing its mathematics.

Torchvision 0.25's CUDA backward scatters with atomic adds and explicitly rejects
strict determinism. Run only this operator on CPU, retaining all autograd copy
edges, and return its output to the caller's device. No vendor file is edited.
"""
from __future__ import annotations

import hashlib
import types
from pathlib import Path


POLICY = "cpu-deform-forward-backward-fp32-v1"


def policy_identity():
    return {"schema": "r9-operator-execution/1", "policy": POLICY,
            "operator": "torchvision.ops.deform_conv2d",
            "execution": "CPU forward and backward; differentiable device copies",
            "precision": "float32 without autocast or detach",
            "scope": "same six frozen-base modules for training and inference",
            "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def _forward_cpu(self, input, offset, mask=None):
    import torch
    from torchvision.ops import deform_conv2d
    values = [input, offset, self.weight]
    if self.bias is not None:
        values.append(self.bias)
    if mask is not None:
        values.append(mask)
    if any(value.dtype != torch.float32 for value in values):
        raise ValueError("deterministic DeformConv2d policy requires float32")
    if any(value.device != input.device for value in values):
        raise ValueError("all DeformConv2d operands must share the caller device")
    self._r9_deform_calls += 1
    self._r9_deform_cross_device_calls += int(input.device.type != "cpu")
    # These are differentiable copies, deliberately never .detach(). Input,
    # offset, mask and even weight/bias gradients are preserved if requested.
    result = deform_conv2d(input.to("cpu"), offset.to("cpu"), self.weight.to("cpu"),
                          self.bias.to("cpu") if self.bias is not None else None,
                          stride=self.stride, padding=self.padding, dilation=self.dilation,
                          mask=mask.to("cpu") if mask is not None else None)
    return result.to(input.device)


def install_cpu_deform_policy(model, *, expected_count=6):
    from torchvision.ops import DeformConv2d
    names = []
    for name, module in model.named_modules():
        if not isinstance(module, DeformConv2d):
            continue
        names.append(name)
        if hasattr(module, "_r9_deform_policy"):
            if module._r9_deform_policy != POLICY:
                raise ValueError("a different DeformConv2d execution policy is installed")
            continue
        module._r9_deform_policy = POLICY
        module._r9_deform_calls = 0
        module._r9_deform_cross_device_calls = 0
        module.forward = types.MethodType(_forward_cpu, module)
    if expected_count is not None and len(names) != expected_count:
        raise ValueError(f"expected {expected_count} DeformConv2d modules, found {len(names)}")
    return {**policy_identity(), "module_names": names}


def execution_counts(model):
    from torchvision.ops import DeformConv2d
    return {name: {"forward_calls": module._r9_deform_calls,
                   "cross_device_calls": module._r9_deform_cross_device_calls}
            for name, module in model.named_modules()
            if isinstance(module, DeformConv2d) and getattr(module, "_r9_deform_policy", None) == POLICY}
