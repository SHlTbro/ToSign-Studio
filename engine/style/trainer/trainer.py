# -*- coding: utf-8 -*-
"""S05 trainer.trainer — 训练循环骨架（ENGINEERING_SKELETON）。

真实实现的部分（本波 CPU 单测覆盖）：
- 真梯度/真参数更新：每 epoch 前后参数指纹必须不同（E_NO_PARAM_CHANGE 防伪报）；
  每步梯度必须存在、有限、整体范数 > 0（E_NO_GRADIENT / E_GRAD_INVALID）。
- 有限 loss 断言：NaN/Inf loss → E_LOSS_NONFINITE，样本归档，run 收据标 FAILED。
- checkpoint best/last：model/optimizer/scaler/RNG(python·numpy·torch)/data index/
  config hash/快照 hash 全量落盘；写 checkpoints.json 登记文件 sha256（resume 前验签，
  拒绝不可信外来文件被当作自家 checkpoint 加载——卡 S05 风险项）。
- resume：验签 + config-hash/快照-hash 一致性检查（换数据/换架构 → E_RESUME_*_MISMATCH）；
  恢复 RNG 与数据指针（mid-epoch batch 指针续训），续训状态可解释。
- 失败样本归档钩子：on_batch_failure()（默认写 run_dir/failed_samples/，可覆写）。

ENGINEERING_SKELETON 边界（本波为真，不得越界宣称）：
- 不加载 FontDiffuser 权重（loaded_real_base_weights=False 写死在收据）；
- 不产出真实训练 checkpoint（单测只用 tmp 目录微型假模型）；
- 训练"能跑"≠质量达标（质量门在 S08/S09）；budget 耗尽正常收尾 ≠ 效果 PASS。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import random
import time
from pathlib import Path

import numpy as np
import torch

from .config import TRAINER_VERSION, TrainConfig, hash_config
from .receipt import RunReceipt
from engine.style.m2_adapter.trainer_resume_protocol import native_path

CHECKPOINT_SCHEMA = "m2-trainer-checkpoint/3"
RNG_SCHEMA = "global-rng-primitives/1"
CKPT_LAST = "ckpt-last.pt"
CKPT_BEST = "ckpt-best.pt"
CKPT_INDEX = "checkpoints.json"
RECEIPT_NAME = "receipt.json"
FAILED_DIR = "failed_samples"


class TrainerError(Exception):
    def __init__(self, code: str, msg: str):
        super().__init__(f"[{code}] {msg}")
        self.code = code


def param_fingerprint(model) -> str:
    """参数集内容指纹（sha256）——"参数确有变化"的机器判据。"""
    h = hashlib.sha256()
    for name, t in sorted(model.state_dict().items()):
        ct = t.detach().cpu().contiguous()
        h.update(name.encode("utf-8"))
        h.update(str(tuple(ct.shape)).encode("ascii"))
        h.update(ct.numpy().tobytes())
    return h.hexdigest()


def _summarize_batch(batch, depth: int = 0):
    """batch 摘要（只记 shape/dtype/标量，不落张量本体）。"""
    if torch.is_tensor(batch):
        return {"type": "tensor", "shape": list(batch.shape), "dtype": str(batch.dtype)}
    if isinstance(batch, (list, tuple)):
        return {"type": type(batch).__name__,
                "items": [_summarize_batch(b, depth + 1) for b in batch][:8]}
    if isinstance(batch, dict):
        return {"type": "dict",
                "items": {k: _summarize_batch(v, depth + 1) for k, v in list(batch.items())[:8]}}
    if isinstance(batch, (int, float, str, bool)) or batch is None:
        return batch
    return str(type(batch).__name__)


class Trainer:
    """M2 适配器训练骨架。GPU 波次接入 FontDiffuser 时复用本循环，不重写。"""

    def __init__(self, config: TrainConfig, model, optimizer,
                 train_loader, val_loader=None, loss_fn=None, scaler=None,
                 run_id: str = "", resume_from: str | None = None,
                 resume_binding=None, resume_manifest_sha256=None,
                 loader_state_getter=None, loader_state_restorer=None, cancel_requested=None):
        # run_dir 纪律：新 run 拒绝非空目录；resume 例外（目录本就有 checkpoint）
        if resume_from is None:
            config.validate()
        else:
            self._validate_for_resume(config)
        if resume_binding is not None and resume_from is not None:
            target = Path(config.run_dir).absolute()
            previous = Path(resume_from).absolute()
            if previous.is_relative_to(target) or (native_path(target).exists() and any(native_path(target).iterdir())):
                raise TrainerError("E_RESUME_OUTPUT", "bound continuation requires a fresh run directory; historical run is immutable")
        self.config = config
        self.model = model
        self.optimizer = optimizer
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.loss_fn = loss_fn or self._default_loss
        self.scaler = scaler
        self.resume_binding = resume_binding
        self.resume_manifest_sha256 = resume_manifest_sha256
        self.loader_state_getter = loader_state_getter
        self.loader_state_restorer = loader_state_restorer
        self.cancel_requested = cancel_requested or (lambda: False)
        self.elapsed_seconds = 0.0
        self._segment_started = None
        self._epoch_losses = []
        self._checkpoint_healthy = True
        if resume_binding is not None:
            from engine.style.m2_adapter.trainer_resume_protocol import validate_binding
            validate_binding(resume_binding)
            if not callable(loader_state_getter) or not callable(loader_state_restorer):
                raise TrainerError("E_RESUME_LOADER", "bound trainer requires private loader state callbacks")
            if not getattr(getattr(train_loader, "batch_sampler", None), "_yy_stateful_batches", False) or train_loader.num_workers != 0:
                raise TrainerError("E_RESUME_LOADER", "bound trainer requires committed sampler and zero workers")

        # 冻结策略强制施加（基座结构性不可训）
        self.trainable_names, self.frozen_names = config.freeze.apply(model)

        # 确定性基座：seed 全家桶（resume 时随后被 ckpt 内 RNG 状态覆盖）
        self._seed_everything(config.seed)

        self.run_dir = Path(config.run_dir)
        native_path(self.run_dir).mkdir(parents=True, exist_ok=True)
        self.failed_dir = self.run_dir / FAILED_DIR
        native_path(self.failed_dir).mkdir(exist_ok=True)
        self.run_id = run_id or f"{config.run_name or 'm2run'}-{_now_stamp()}"
        self.code_version = _code_version()
        if self.resume_binding is not None:
            if (self.resume_binding["run_id"] != self.run_id
                or self.resume_binding["snapshot_sha256"] != config.data_snapshot_sha256
                or self.resume_binding["trainer_version"] != TRAINER_VERSION
                or self.resume_binding["trainer_code_sha256"] != self.code_version):
                raise TrainerError("E_RESUME_IDENTITY_MISMATCH", "server binding differs from actual trainer identity")

        # 可变训练状态
        self.epoch = 0                  # 已完成 epoch 数
        self.global_step = 0
        self.batch_ptr = {"epoch": 0, "batch_idx": 0}
        self.best_val: float | None = None
        self.gradient_diagnostics = []
        self._resume_pending = False
        self._code_note = ""

        self.receipt = RunReceipt.create(config, code_version=self.code_version,
                                         run_id=self.run_id)
        self.receipt.notes = "ENGINEERING_SKELETON run (CPU unit test); no real FontDiffuser weights"
        if resume_from is not None:
            self._resume(resume_from)

    # -------------------------------------------------- public API
    def train(self) -> RunReceipt:
        """主循环。预算（epoch/step/可选秒）耗尽或早停 → 正常收尾；异常 → FAILED 收据后重抛。"""
        cfg = self.config
        t0 = time.time()
        self._segment_started = time.monotonic()
        remaining_seconds = max(0.0, cfg.max_seconds - self.elapsed_seconds) if cfg.max_seconds else None
        self._training_deadline = self._segment_started + remaining_seconds if remaining_seconds is not None else None
        try:
            while (self.epoch < cfg.max_epochs and self.global_step < cfg.max_steps
                   and not (cfg.max_seconds and self._elapsed_now() >= cfg.max_seconds)):
                if self.cancel_requested():
                    raise TrainerError("CANCELLED", "cancel observed before epoch")
                current = self.epoch + 1
                start_batch = self.batch_ptr["batch_idx"] if self._resume_pending else 0
                callback = getattr(self, "on_epoch_start", None)
                if callback is not None:
                    callback(current)
                if not self._resume_pending:
                    self._epoch_losses = []
                if self.resume_binding is not None and not self._resume_pending:
                    self.batch_ptr = {"epoch": current, "batch_idx": 0}
                    self._save_ckpt(CKPT_LAST)
                completed = self._run_epoch(current, start_batch=start_batch)
                self._resume_pending = False
                if not completed:
                    # 预算中途耗尽：epoch 未完，不记 epoch 指标；
                    # 指针换算为"下一个未执行的 batch"后落盘（resume 指针语义）
                    self.batch_ptr = {"epoch": current,
                                      "batch_idx": self.batch_ptr["batch_idx"]}
                    self.receipt.notes += f" | budget exhausted mid-epoch @epoch{current}"
                    break
                self.epoch = current
                self.batch_ptr = {"epoch": current, "batch_idx": 0}
                val = self._validate() if self.val_loader is not None else None
                if val is not None:
                    if not torch.isfinite(torch.tensor(float(val))):
                        raise TrainerError("E_VAL_NONFINITE", f"val metric not finite: {val}")
                improved = (val is not None and cfg.stop.improved(self.best_val, val))
                if improved:
                    self.best_val = val
                    self.receipt.best_epoch = current
                    self.receipt.best_val = val
                    callback = getattr(self, "on_best", None)
                    if callback is not None:
                        callback(current, self.global_step, val)
                self.receipt.epochs.append({
                    "epoch": current,
                    "train_loss": _mean(self._epoch_losses),
                    "val_loss": val,
                    "global_step": self.global_step,
                    "n_steps": len(self._epoch_losses),
                })
                self._save_ckpt(CKPT_LAST)
                if improved:
                    self._save_ckpt(CKPT_BEST)
                # Durable progress is explicitly RUNNING, never a terminal pass.
                self.receipt.save()
                callback = getattr(self, "on_epoch_complete", None)
                if callback is not None:
                    callback(current, self.global_step, val)
                # 早停（预注册规则）：自 best 起连续 patience 个 epoch 无改善（仅当有 val 指标）
                if (val is not None
                        and (self.receipt.epochs[-1]["epoch"] - self.receipt.best_epoch)
                        >= cfg.stop.patience):
                    self.receipt.notes += " | early_stop: patience exhausted"
                    break
            self.receipt.finalize("COMPLETED")
        except TrainerError as e:
            self.receipt.finalize("CANCELLED" if e.code == "CANCELLED" else "FAILED", failure_code=e.code)
            if self.resume_binding is None:
                self._save_ckpt(CKPT_LAST)  # 现场保全（含中断点指针）
            self.receipt.save()
            raise
        except Exception as e:  # 非预期异常同样落 FAILED 收据，不伪报
            self.receipt.finalize("FAILED", failure_code="E_UNEXPECTED")
            self.receipt.notes += f" | {type(e).__name__}: {e}"
            self.receipt.save()
            raise
        self.elapsed_seconds = self._elapsed_now()
        self._segment_started = None
        self._save_ckpt(CKPT_LAST)
        self.receipt.n_failure_samples = self._count_failures()
        self.receipt.save()
        return self.receipt

    def on_batch_failure(self, batch, code: str, detail: str):
        """失败样本归档钩子（可覆写）。默认：写 run_dir/failed_samples/*.json。"""
        payload = {
            "run_id": self.run_id,
            "global_step": self.global_step,
            "batch_ptr": dict(self.batch_ptr),
            "code": code,
            "detail": detail,
            "batch_summary": _summarize_batch(batch),
            "saved_at": _now_stamp(),
        }
        path = self.failed_dir / f"step{self.global_step:08d}_{code}.json"
        native_path(path).write_bytes(json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"))
        return path

    # -------------------------------------------------- epochs/steps
    def _run_epoch(self, epoch_no: int, start_batch: int = 0) -> bool:
        """Commit the next-batch pointer only after a successful optimizer update."""
        before = param_fingerprint(self.model)
        self.model.train()
        executed = 0
        stateful = getattr(getattr(self.train_loader, "batch_sampler", None), "_yy_stateful_batches", False)
        if stateful and self._resume_pending and start_batch > 0:
            # A cursor-zero checkpoint precedes the first iterator allocation;
            # it must consume that first base seed normally. Only a committed
            # nonzero cursor has already consumed the epoch's iterator draw.
            # DataLoader allocates an iterator base seed even with zero workers.
            # A resumed iterator must not consume another global/private draw.
            global_rng = torch.get_rng_state().clone()
            generator = getattr(self.train_loader, "generator", None)
            loader_rng = generator.get_state().clone() if generator is not None else None
            try:
                iterator = iter(self.train_loader)
            finally:
                torch.set_rng_state(global_rng)
                if generator is not None:
                    generator.set_state(loader_rng)
        else:
            iterator = iter(self.train_loader)
        batch_idx = start_batch if stateful else 0
        while True:
            if self.cancel_requested():
                raise TrainerError("CANCELLED", "cancel observed at committed optimizer boundary")
            if getattr(self, "_training_deadline", None) is not None and time.monotonic() >= self._training_deadline:
                self.receipt.notes += " | cumulative wall-time budget reached before next batch"
                return False
            try:
                batch = next(iterator)
            except StopIteration:
                break
            if not stateful and batch_idx < start_batch:
                batch_idx += 1
                continue
            self.batch_ptr = {"epoch": epoch_no, "batch_idx": batch_idx}
            try:
                loss = self._step(batch)
            except TrainerError as e:
                self.on_batch_failure(batch, e.code, str(e))
                raise
            self._epoch_losses.append(float(loss.detach()))
            executed += 1
            self.global_step += 1
            batch_idx += 1
            self.batch_ptr = {"epoch": epoch_no, "batch_idx": batch_idx}
            callback = getattr(self, "on_step_complete", None)
            if callback is not None:
                callback(self.global_step, dict(self.batch_ptr))
            if stateful and self.train_loader.batch_sampler.next_batch != batch_idx:
                raise TrainerError("E_RESUME_LOADER", "sampler cursor was not committed with optimizer step")
            if self.resume_binding is not None:
                self._save_ckpt(CKPT_LAST)
            if self.global_step >= self.config.max_steps:
                break
        if executed and param_fingerprint(self.model) == before:
            raise TrainerError("E_NO_PARAM_CHANGE", f"epoch {epoch_no}: optimizer updates did not change parameters")
        return batch_idx >= len(self.train_loader)

    def _step(self, batch):
        x, y = self._unpack(batch)
        self.optimizer.zero_grad()
        out = self.model(x)
        loss = self.loss_fn(out, y)
        if not torch.isfinite(loss):
            raise TrainerError("E_LOSS_NONFINITE",
                               f"loss={float(loss) if loss.numel() == 1 else loss} not finite")
        try:
            if self.scaler is not None:
                self.scaler.scale(loss).backward()
            else:
                loss.backward()
        except RuntimeError as e:
            # 典型：无任何 requires_grad 参数位于前向路径（梯度断流/零可训网络）
            raise TrainerError("E_NO_GRADIENT",
                               f"loss has no grad path（loss 不依赖任何可训参数）: {e}") from e
        if self.scaler is not None:
            self.scaler.unscale_(self.optimizer)
        self._check_gradients()
        if self.config.gradient_clip_norm is not None:
            norm = torch.nn.utils.clip_grad_norm_([p for p in self.model.parameters() if p.requires_grad],
                                                 self.config.gradient_clip_norm, error_if_nonfinite=True)
            self.gradient_diagnostics.append({"step":self.global_step+1,"unclipped_norm":float(norm),
                                              "clipped":float(norm)>self.config.gradient_clip_norm})
        if self.scaler is not None:
            self.scaler.step(self.optimizer)
            self.scaler.update()
        else:
            self.optimizer.step()
        return loss.detach()

    def _check_gradients(self):
        grads = [p.grad for p in self.model.parameters()
                 if p.requires_grad and p.grad is not None]
        if not grads:
            raise TrainerError(
                "E_NO_GRADIENT",
                "no gradient on any trainable param（零可训参数/梯度断流却报训练 → 拒绝）")
        for g in grads:
            if not torch.isfinite(g).all():
                raise TrainerError("E_GRAD_INVALID", "NaN/Inf gradient（梯度非法）")
        total = torch.norm(torch.stack([g.norm() for g in grads]))
        if float(total) <= 0.0:
            raise TrainerError("E_NO_GRADIENT", f"total grad norm == 0（{len(grads)} grads）")

    def _validate(self) -> float:
        self.model.eval()
        losses = []
        with torch.no_grad():
            for index, batch in enumerate(self.val_loader):
                x, y = self._unpack(batch)
                value = float(self.loss_fn(self.model(x), y))
                if not math.isfinite(value):
                    raise TrainerError('E_VAL_NONFINITE', f'validation batch{index} produced nonfinite loss: {value}')
                losses.append(value)
        if not losses:
            raise TrainerError('E_VAL_EMPTY', 'validation loader produced no scored batches')
        return sum(losses) / len(losses)

    # -------------------------------------------------- checkpoint/resume
    def _model_structure(self, state):
        return {name: {"shape": list(value.shape), "dtype": str(value.dtype), "layout": str(value.layout),
                       "numel": value.numel()} for name, value in sorted(state.items())}

    def _frozen_model_fingerprint(self, state):
        h = hashlib.sha256()
        for name, value in sorted(state.items()):
            if name in self.trainable_names:
                continue
            metadata = {"name": name, "dtype": str(value.dtype), "shape": list(value.shape), "layout": str(value.layout)}
            h.update(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            raw = value.detach().cpu().contiguous().reshape(-1).view(torch.uint8).numpy()
            h.update(memoryview(raw))
        return h.hexdigest()

    def _ckpt_payload(self) -> dict:
        full_state = self.model.state_dict()
        bound = self.resume_binding is not None
        model_state = {name: value for name, value in full_state.items() if name in self.trainable_names} if bound else full_state
        return {
            "trainer_version": TRAINER_VERSION,
            "code_version": self.code_version,
            "checkpoint_schema": CHECKPOINT_SCHEMA,
            "resume_binding": self.resume_binding,
            "loader_state": self.loader_state_getter() if self.loader_state_getter else None,
            "elapsed_seconds": self._elapsed_now(),
            "epoch_losses": list(self._epoch_losses),
            "best_epoch": self.receipt.best_epoch,
            "gradient_diagnostics": list(self.gradient_diagnostics),
            "run_id": self.run_id,
            "config_hash": hash_config(self.config),
            "data_snapshot_sha256": self.config.data_snapshot_sha256,
            "epoch": self.epoch,
            "global_step": self.global_step,
            "batch_ptr": dict(self.batch_ptr),
            "best_val": self.best_val,
            "model_state": model_state,
            "model_state_scope": "trainable-only" if bound else "complete",
            "model_structure": self._model_structure(full_state),
            "frozen_model_sha256": self._frozen_model_fingerprint(full_state) if bound else None,
            "optimizer_state": self.optimizer.state_dict(),
            "scaler_state": self.scaler.state_dict() if self.scaler is not None else None,
            "rng": self._rng_state(),
            "data_index": {"epoch": self.epoch, "batch_idx": self.batch_ptr["batch_idx"],
                           "global_step": self.global_step,
                           "train_loader_len": len(self.train_loader),
                           "note": "deterministic loader required (shuffle=False or seeded generator)"},
            "epochs_log": list(self.receipt.epochs),
            "saved_at": _now_stamp(),
        }

    def _save_ckpt(self, name: str):
        payload = self._ckpt_payload()
        # torch.save 直写含非 ASCII（中文）路径在 Windows 上会失败
        # （PyTorchFileWriter 不支持）——与 worker.save_ckpt 同法：BytesIO 中转。
        import io
        buf = io.BytesIO()
        torch.save(payload, buf)
        if self.resume_binding is not None:
            import shutil
            trainable_bytes = sum(value.numel() * value.element_size() for value in payload["model_state"].values())
            estimate = buf.tell() + 2 * trainable_bytes
            maximum = self.config.max_steps + 2 * self.config.max_epochs + 3
            generations = self.run_dir / "resume_generations"
            existing = len(list(native_path(generations).glob("*/checkpoint.pt"))) if native_path(generations).exists() else 0
            remaining = maximum - existing
            if (os.name == "nt" and self.run_dir.absolute().drive.upper() != "E:") or remaining <= 0:
                raise TrainerError("E_RESUME_DISK", "E-only bounded checkpoint generation count exceeded")
            # Preserve every immutable generation. Reserve future generations and
            # mutable last/best scratch writes; no deletion or budget extension.
            required = (remaining + 3) * estimate
            if shutil.disk_usage(native_path(self.run_dir)).free < required:
                raise TrainerError("E_RESUME_DISK", "insufficient disk for complete immutable checkpoint budget")
        final = self.run_dir / name
        tmp = self.run_dir / (name + ".tmp")
        native_path(tmp).write_bytes(buf.getvalue())
        os.replace(native_path(tmp), native_path(final))
        self._register_ckpt(name, final)
        if self.resume_binding is not None and name == CKPT_LAST:
            from engine.style.m2_adapter.trainer_resume_protocol import write_manifest
            import uuid
            generation = self.run_dir / "resume_generations" / f"step-{self.global_step:08d}-{uuid.uuid4().hex}"
            native_path(generation).mkdir(parents=True, exist_ok=False)
            checkpoint = generation / "checkpoint.pt"
            with native_path(checkpoint).open("xb") as stream:
                stream.write(buf.getvalue())
                stream.flush()
                os.fsync(stream.fileno())
            boundary = "completed_epoch" if self.batch_ptr == {"epoch": self.epoch, "batch_idx": 0} else "committed_batch"
            record = write_manifest(generation, checkpoint, self.resume_binding,
                {"epoch": self.epoch, "global_step": self.global_step, "batch_ptr": dict(self.batch_ptr),
                 "elapsed_seconds": payload["elapsed_seconds"], "train_loader_len": len(self.train_loader), "boundary": boundary})
            callback = getattr(self, "on_checkpoint_committed", None)
            if callback is not None:
                callback(record)

    def _register_ckpt(self, name: str, path: Path):
        """checkpoint 登记簿：文件 sha256 入账。resume 前验签，拒收被调包的 .pt。"""
        from .config import sha256_bytes
        idx_path = self.run_dir / CKPT_INDEX
        reg = {}
        if native_path(idx_path).is_file():
            reg = json.loads(native_path(idx_path).read_bytes().decode("utf-8"))
        reg[name] = sha256_bytes(native_path(path).read_bytes())
        temporary = idx_path.with_suffix(".json.tmp")
        native_path(temporary).write_bytes(json.dumps(reg, indent=1, sort_keys=True).encode("utf-8"))
        os.replace(native_path(temporary), native_path(idx_path))

    def _resume(self, ckpt_path: str):
        from .config import sha256_bytes
        path = Path(ckpt_path)
        if not native_path(path).is_file():
            raise TrainerError("E_CKPT_MISSING", f"checkpoint not found: {path}")
        verified_manifest = None
        if self.resume_binding is not None:
            from engine.style.m2_adapter.trainer_resume_protocol import verify_manifest
            verified_manifest = verify_manifest(path.parent, self.resume_binding, self.resume_manifest_sha256)
            if Path(verified_manifest["checkpoint_path"]).absolute() != path.absolute():
                raise TrainerError("E_RESUME_PATH", "request differs from server anchored checkpoint")
        reg_path = path.parent / CKPT_INDEX
        if verified_manifest is None and not native_path(reg_path).is_file():
            raise TrainerError("E_CKPT_UNREGISTERED", "checkpoint registry is missing")
        reg = json.loads(native_path(reg_path).read_bytes().decode("utf-8")) if verified_manifest is None else {path.name: verified_manifest["manifest"]["checkpoint"]["sha256"]}
        recorded = reg.get(path.name)
        if not isinstance(recorded, str) or len(recorded) != 64:
            raise TrainerError("E_CKPT_UNREGISTERED", "checkpoint has no full registered hash")
        if recorded != sha256_bytes(native_path(path).read_bytes()):
            raise TrainerError("E_CKPT_TAMPERED", f"{path.name} sha mismatch vs {CKPT_INDEX}")
        try:
            ckpt = torch.load(native_path(path), map_location="cpu", weights_only=True)
        except Exception as exc:
            raise TrainerError("E_CKPT_SAFE_LOAD",
                               "restricted checkpoint load failed; legacy NumPy-pickle checkpoints are unsupported") from exc
        if not isinstance(ckpt, dict) or ckpt.get("checkpoint_schema") != CHECKPOINT_SCHEMA:
            raise TrainerError("E_CKPT_UNSUPPORTED_FORMAT",
                               "checkpoint schema is unsupported; no unsafe legacy fallback is permitted")

        if ckpt.get("trainer_version") != TRAINER_VERSION or ckpt.get("code_version") != self.code_version:
            raise TrainerError("E_RESUME_VERSION", "trainer version or source changed")
        if ckpt.get("run_id") != self.run_id:
            raise TrainerError("E_RESUME_RUN", "checkpoint belongs to another logical run")
        if ckpt.get("data_snapshot_sha256") != self.config.data_snapshot_sha256:
            raise TrainerError("E_RESUME_IDENTITY_MISMATCH", "checkpoint snapshot changed")
        if verified_manifest is not None:
            from engine.style.m2_adapter.trainer_resume_protocol import validate_payload
            validate_payload(ckpt, verified_manifest["manifest"], checkpoint_schema=CHECKPOINT_SCHEMA,
                trainer_version=TRAINER_VERSION, code_version=self.code_version, config_hash=hash_config(self.config),
                run_id=self.run_id, snapshot_sha256=self.config.data_snapshot_sha256, train_loader_len=len(self.train_loader))
        if ckpt.get("config_hash") != hash_config(self.config):
            raise TrainerError(
                "E_RESUME_IDENTITY_MISMATCH",
                "resume onto different data/architecture/freeze/optimizer/seed"
                "（换数据或换架构恢复 → 拒绝；调度旋钮（预算/停止）除外）")
        for key in ("epoch", "global_step"):
            if type(ckpt.get(key)) is not int or ckpt[key] < 0:
                raise TrainerError("E_RESUME_POSITION", "nonnegative integer position required")
        if ckpt["epoch"] > self.config.max_epochs or ckpt["global_step"] > self.config.max_steps:
            raise TrainerError("E_RESUME_BUDGET", "checkpoint exceeds bound budget")
        index = ckpt.get("data_index", {})
        if index.get("train_loader_len") != len(self.train_loader) or index.get("epoch") != ckpt["epoch"] or index.get("global_step") != ckpt["global_step"] or index.get("batch_idx") != ckpt.get("batch_ptr", {}).get("batch_idx"):
            raise TrainerError("E_RESUME_LOADER", "train loader length changed")
        pointer = ckpt.get("batch_ptr", {})
        if set(pointer) != {"epoch", "batch_idx"} or any(type(pointer[k]) is not int for k in pointer) or not 0 <= pointer["batch_idx"] <= len(self.train_loader):
            raise TrainerError("E_RESUME_POSITION", "invalid next-batch pointer")
        elapsed = ckpt.get("elapsed_seconds")
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
            raise TrainerError("E_RESUME_BUDGET", "invalid cumulative elapsed time")
        if self.config.max_seconds and elapsed > self.config.max_seconds:
            raise TrainerError("E_RESUME_BUDGET", "checkpoint exhausted bound cumulative time")
        if bool(ckpt.get("scaler_state") is not None) != bool(self.scaler is not None):
            raise TrainerError("E_RESUME_SCALER", "scaler presence changed")
        self._validate_resume_states(ckpt)
        if self.resume_binding is not None:
            merged = dict(self.model.state_dict())
            merged.update(ckpt["model_state"])
            self.model.load_state_dict(merged, strict=True)
        else:
            self.model.load_state_dict(ckpt["model_state"], strict=True)
        self.optimizer.load_state_dict(ckpt["optimizer_state"])
        if self.scaler is not None and ckpt.get("scaler_state") is not None:
            self.scaler.load_state_dict(ckpt["scaler_state"])
        if self.loader_state_restorer is not None:
            self.loader_state_restorer(ckpt["loader_state"])
        self._restore_rng(ckpt["rng"])
        self.elapsed_seconds = float(ckpt["elapsed_seconds"])
        self._epoch_losses = list(ckpt.get("epoch_losses", []))
        self.gradient_diagnostics = list(ckpt["gradient_diagnostics"])
        self.receipt.best_epoch = ckpt.get("best_epoch", -1)
        self.receipt.best_val = ckpt.get("best_val")
        self.epoch = int(ckpt["epoch"])           # 已完成 epoch 数
        self.global_step = int(ckpt["global_step"])
        self.batch_ptr = dict(ckpt["batch_ptr"])
        self.best_val = ckpt.get("best_val")
        self.receipt.epochs = list(ckpt.get("epochs_log", []))
        self._resume_pending = self.batch_ptr != {"epoch": self.epoch, "batch_idx": 0}               # 下一轮从 batch_ptr 续跑同一 epoch
        self.receipt.notes += f" | resumed from {path.name} @epoch{self.epoch} batch{self.batch_ptr['batch_idx']}"

    # -------------------------------------------------- helpers
    @staticmethod
    def _unpack(batch):
        if isinstance(batch, (tuple, list)) and len(batch) >= 2:
            return batch[0], batch[1]
        raise TrainerError("E_BATCH_SHAPE", "batch must be (input, target) pair")

    @staticmethod
    def _default_loss(out, y):
        return torch.nn.functional.mse_loss(out, y)

    def _validate_resume_states(self, checkpoint):
        """Validate tensor shape/finite state and AdamW groups before any load."""
        state = checkpoint.get("model_state")
        expected = self.model.state_dict()
        if getattr(self, "resume_binding", None) is not None:
            from engine.style.m2_adapter.trainer_resume_protocol import validate_trainable_model_metadata
            validate_trainable_model_metadata(checkpoint, self._model_structure(expected), self.trainable_names,
                                               self._frozen_model_fingerprint(expected))
        elif not isinstance(state, dict) or set(state) != set(expected) or checkpoint.get("model_state_scope", "complete") != "complete":
            raise TrainerError("E_RESUME_MODEL", "exact complete model state keys required")
        for name, value in state.items():
            wanted = expected[name]
            if not torch.is_tensor(value) or value.dtype != wanted.dtype or value.shape != wanted.shape or value.layout != wanted.layout:
                raise TrainerError("E_RESUME_MODEL", "model tensor identity changed: " + name)
            if not bool(torch.isfinite(value).all()):
                raise TrainerError("E_RESUME_MODEL", "nonfinite checkpoint tensor: " + name)
        optimizer = checkpoint.get("optimizer_state")
        fresh = self.optimizer.state_dict()
        if not isinstance(optimizer, dict) or set(optimizer) != {"state", "param_groups"} or not isinstance(optimizer["state"], dict):
            raise TrainerError("E_RESUME_OPTIMIZER", "exact optimizer state required")
        groups = optimizer["param_groups"]
        if not isinstance(groups, list) or len(groups) != len(fresh["param_groups"]):
            raise TrainerError("E_RESUME_OPTIMIZER", "optimizer groups changed")
        params = {}
        for saved, original, actual in zip(groups, fresh["param_groups"], self.optimizer.param_groups):
            if not isinstance(saved, dict) or set(saved) != set(original) or any(saved[key] != original[key] for key in original if key != "params"):
                raise TrainerError("E_RESUME_OPTIMIZER", "optimizer hyperparameters changed")
            if saved["params"] != original["params"] or len(saved["params"]) != len(actual["params"]):
                raise TrainerError("E_RESUME_OPTIMIZER", "optimizer parameter order changed")
            params.update(zip(saved["params"], actual["params"]))
        if not set(optimizer["state"]).issubset(params):
            raise TrainerError("E_RESUME_OPTIMIZER", "unknown optimizer parameter state")
        for identifier, row in optimizer["state"].items():
            if not isinstance(row, dict) or not {"step", "exp_avg", "exp_avg_sq"}.issubset(row) or not set(row).issubset({"step", "exp_avg", "exp_avg_sq", "max_exp_avg_sq"}):
                raise TrainerError("E_RESUME_OPTIMIZER", "complete AdamW state required")
            for key, value in row.items():
                if not torch.is_tensor(value) or not bool(torch.isfinite(value).all()):
                    raise TrainerError("E_RESUME_OPTIMIZER", "finite optimizer tensors required")
                if key == "step":
                    if value.numel() != 1 or float(value) < 0 or not float(value).is_integer() or float(value) > checkpoint["global_step"]:
                        raise TrainerError("E_RESUME_OPTIMIZER", "optimizer step differs from committed progress")
                elif value.shape != params[identifier].shape or value.dtype != params[identifier].dtype:
                    raise TrainerError("E_RESUME_OPTIMIZER", "optimizer moments differ from parameter layout")
        diagnostics = checkpoint.get("gradient_diagnostics")
        expected_count = checkpoint["global_step"] if self.config.gradient_clip_norm is not None else 0
        if not isinstance(diagnostics, list) or len(diagnostics) != expected_count:
            raise TrainerError("E_RESUME_HISTORY", "gradient diagnostics do not cover committed optimizer steps")
        for index, row in enumerate(diagnostics, 1):
            if (not isinstance(row, dict) or set(row) != {"step", "unclipped_norm", "clipped"}
                or type(row["step"]) is not int or row["step"] != index
                or type(row["unclipped_norm"]) not in (int, float) or not math.isfinite(row["unclipped_norm"])
                or row["unclipped_norm"] < 0 or type(row["clipped"]) is not bool):
                raise TrainerError("E_RESUME_HISTORY", "ordered finite gradient diagnostics required")
        logs = checkpoint.get("epochs_log")
        if not isinstance(logs, list) or len(logs) != checkpoint["epoch"]:
            raise TrainerError("E_RESUME_HISTORY", "complete epoch history required")
        previous_step = 0
        for index, row in enumerate(logs, 1):
            if not isinstance(row, dict) or row.get("epoch") != index or type(row.get("global_step")) is not int or not previous_step < row["global_step"] <= checkpoint["global_step"]:
                raise TrainerError("E_RESUME_HISTORY", "epoch progress history changed")
            previous_step = row["global_step"]
        losses = checkpoint.get("epoch_losses")
        if not isinstance(losses, list) or any(type(value) not in (int, float) or not math.isfinite(value) for value in losses):
            raise TrainerError("E_RESUME_HISTORY", "finite partial epoch losses required")
        if checkpoint["batch_ptr"]["batch_idx"] and len(losses) != checkpoint["batch_ptr"]["batch_idx"]:
            raise TrainerError("E_RESUME_HISTORY", "partial epoch losses differ from committed cursor")
        if checkpoint.get("best_val") is not None and not math.isfinite(checkpoint["best_val"]):
            raise TrainerError("E_RESUME_HISTORY", "best validation metric is not finite")
        best_epoch = checkpoint.get("best_epoch")
        if type(best_epoch) is not int or not -1 <= best_epoch <= checkpoint["epoch"]:
            raise TrainerError("E_RESUME_HISTORY", "best epoch is invalid")

    def _elapsed_now(self):
        return self.elapsed_seconds + (time.monotonic() - self._segment_started if self._segment_started is not None else 0.0)

    def _rng_state(self) -> dict:
        numpy_state = np.random.get_state()
        # Store NumPy RNG as primitive lists, never pickle a NumPy class.
        # The pinned runtime requires restricted weights_only checkpoint loads.
        return {"schema": RNG_SCHEMA, "python": random.getstate(),
                "numpy": [numpy_state[0], numpy_state[1].tolist(),
                          int(numpy_state[2]), int(numpy_state[3]), float(numpy_state[4])],
                "torch": torch.get_rng_state()}

    def _restore_rng(self, rng: dict):
        if rng.get("schema") != RNG_SCHEMA:
            raise TrainerError("E_RNG_SCHEMA", "global RNG state is not the supported primitive format")
        random.setstate(rng["python"])
        numpy_state = rng["numpy"]
        np.random.set_state((numpy_state[0], np.asarray(numpy_state[1], dtype=np.uint32),
                             int(numpy_state[2]), int(numpy_state[3]), float(numpy_state[4])))
        torch.set_rng_state(rng["torch"].cpu() if torch.is_tensor(rng["torch"]) else rng["torch"])

    def _seed_everything(self, seed: int):
        random.seed(seed)
        np.random.seed(seed % (2 ** 32))
        torch.manual_seed(seed)

    def _validate_for_resume(self, config: TrainConfig):
        """resume 允许 run_dir 已存在（checkpoint 就在里面），其余红线照常。"""
        from .config import ConfigError
        if config.seed is None or int(config.seed) < 0:
            raise ConfigError("E_SEED_REQUIRED", "seed 必须显式预注册")
        if str(config.device).startswith("cuda") and not config.allow_gpu:
            raise ConfigError("E_GPU_NOT_ALLOWED", "device=cuda 需显式 allow_gpu=True")

    def _count_failures(self) -> int:
        return len(list(native_path(self.failed_dir).glob("*.json"))) if native_path(self.failed_dir).is_dir() else 0


def _mean(xs):
    xs = [x for x in xs if x is not None and x == x]
    return sum(xs) / len(xs) if xs else float("nan")


def _now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _code_version() -> str:
    """trainer 模块文件集内容 hash（代码版本锚点；无 git 依赖）。"""
    from engine.style.m2_adapter.trainer_resume_protocol import trainer_code_sha256
    return trainer_code_sha256(Path(__file__).resolve().parents[3])
