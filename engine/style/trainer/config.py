# -*- coding: utf-8 -*-
"""S05 trainer.config — run 配置（ENGINEERING_SKELETON）。

红线强制（卡 S05 + ENGINE_DECISION §7）：
1. ``FreezePolicy.freeze_base`` 恒为 True；``trainable_prefixes`` 不得触及任何基座模块名
   （unet/style_encoder/content_encoder）。想全量微调 100.49M 基座 → ConfigError，
   结构上不可能"把全量训练误当轻量适配"。
2. seed / 预算（max_epochs+max_steps）/ 停止规则（early_stop）必须显式预注册，
   缺任何一项 → ConfigError（不允许"先跑起来再说"）。
3. GPU 默认拒绝（``allow_gpu=False``）：本波 GPU 纪律（另一 Agent 占 GPU 槽）+
   S06 worker 未接 GPU 互斥锁前，cuda 设备不可用；显式 allow_gpu=True 才放行。
4. run_dir 必须指向不存在的新目录（输出不可覆盖旧 run）；resume 除外。
5. data_snapshot_sha256 必填：S05 只吃 S03 不可变快照，禁止直读 P4 活动目录。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from engine.style.m2_adapter.trainer_resume_protocol import native_path

ENGINE_ID = "fontdiffuser-frozen-v1"          # S01 裁定的唯一引擎实现
M2_ADAPTER_IDENTIFIER = "M2_ADAPTER"          # 冻结标识（contracts 冻结枚举）
TRAINER_VERSION = "engine/style/trainer/0.4.0+BOUND_COMMITTED_RESUME"

# S01 实证：FontDiffuser 三件套基座（这些模块名在任何情况下不得可训）
BASE_FROZEN_MODULES = ("unet", "style_encoder", "content_encoder")
BASE_CKPT_SHA256 = {  # INTERFACE_CONTRACT_DRAFT §1（只读基座，训练不得写回）
    "unet": "6bde1920ac8d843e",
    "style_encoder": "82eb56abc37ebf7e",
    "content_encoder": "e5b5258247357903",
}


class ConfigError(ValueError):
    def __init__(self, code: str, msg: str):
        super().__init__(f"[{code}] {msg}")
        self.code = code


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_files(paths) -> str:
    """对一组文件内容做确定性 hash（代码版本锚点：trainer 模块文件集）。"""
    h = hashlib.sha256()
    for p in sorted(Path(x) for x in paths):
        h.update(str(p.name).encode("utf-8"))
        h.update(sha256_bytes(native_path(p).read_bytes()).encode("ascii"))
    return h.hexdigest()


def hash_config(cfg: "TrainConfig") -> str:
    """训练身份 hash —— resume 一致性锚点。

    排除两类字段：易变字段（run_dir/created_at）与**调度旋钮**（max_epochs/max_steps/
    max_seconds/stop）。理由：预注册预算的合法延长不改变"在训什么"
    （数据/架构/冻结/优化器/seed 才是身份）；换数据/换架构/换冻结 → hash 变 → resume 拒绝。
    """
    d = dataclasses.asdict(cfg)
    for k in ("run_dir", "run_name", "created_at", "max_epochs", "max_steps", "max_seconds",
              "stop", "resume"):
        d.pop(k, None)
    return sha256_bytes(json.dumps(d, sort_keys=True, ensure_ascii=False,
                                   separators=(",", ":")).encode("utf-8"))


@dataclass
class FreezePolicy:
    """冻结挂 adapter 策略（S01：基座全可训是默认陷阱，这里反转成默认冻结）。"""
    freeze_base: bool = True                      # 恒 True；False → ConfigError
    frozen_modules: tuple = BASE_FROZEN_MODULES   # 基座三件套（架构级冻结）
    trainable_prefixes: tuple = ("adapter.",)     # 仅 adapter 参数可训
    source_type: str = M2_ADAPTER_IDENTIFIER      # 冻结标识，不可改

    def __post_init__(self):
        if self.source_type != M2_ADAPTER_IDENTIFIER:
            raise ConfigError("E_SOURCE_TYPE_FROZEN",
                              f"source_type is a frozen identifier, got {self.source_type!r}")
        if not self.freeze_base:
            raise ConfigError("E_FREEZE_REQUIRED",
                              "freeze_base=False 被禁止（基座 100.49M 全可训 ≠ 轻量适配，S01 实证）")
        if not self.trainable_prefixes:
            raise ConfigError("E_NO_TRAINABLE_SCOPE",
                              "trainable_prefixes 为空 → 零可训练参数，拒绝此类配置")
        for prefix in self.trainable_prefixes:
            for base_mod in self.frozen_modules:
                if prefix.startswith(base_mod + ".") or prefix == base_mod:
                    raise ConfigError(
                        "E_TRAINABLE_HITS_BASE",
                        f"trainable prefix {prefix!r} touches frozen base module {base_mod!r}")

    def apply(self, model):
        """把冻结策略施加到模型：返回 (trainable_names, frozen_names)。"""
        trainable, frozen = [], []
        for name, p in model.named_parameters():
            hit = any(name.startswith(pref) for pref in self.trainable_prefixes)
            p.requires_grad = bool(hit)
            (trainable if hit else frozen).append(name)
        if not trainable:
            raise ConfigError("E_ZERO_TRAINABLE",
                              "after apply(): zero trainable params（零可训参数防伪报）")
        return trainable, frozen


@dataclass
class EarlyStop:
    """预注册停止规则（卡 S05：停止规则不允许训练中途发明）。"""
    metric: str = "val_loss"
    mode: str = "min"                 # min | max
    patience: int = 3
    min_delta: float = 0.0

    def __post_init__(self):
        if self.mode not in ("min", "max"):
            raise ConfigError("E_STOP_MODE", f"mode must be min/max: {self.mode!r}")
        if self.patience < 1:
            raise ConfigError("E_STOP_PATIENCE", "patience must be >= 1")

    def improved(self, old: float, new: float) -> bool:
        if old is None:
            return True
        return (new < old - self.min_delta) if self.mode == "min" else (new > old + self.min_delta)


@dataclass
class TrainConfig:
    """一次 M2 适配器训练 run 的完整配置（收据与 checkpoint 都锚定它的 hash）。"""
    run_name: str = ""
    run_dir: str = ""
    seed: int = -1                                # 必须显式设置（-1 → ConfigError）
    engine_id: str = ENGINE_ID
    base_ckpt_sha256: dict = field(default_factory=lambda: dict(BASE_CKPT_SHA256))
    data_snapshot_dir: str = ""                   # S03 快照目录（只读）
    data_snapshot_sha256: str = ""                # 快照 content hash（resume 一致性锚点）
    preprocessing: dict = field(default_factory=dict)
    training_objective: dict = field(default_factory=dict)
    freeze: FreezePolicy = field(default_factory=FreezePolicy)
    stop: EarlyStop = field(default_factory=EarlyStop)
    max_epochs: int = 0                           # 预算：必须 >0
    max_steps: int = 0                            # 预算：必须 >0（两轴取先到）
    max_seconds: float = 0.0                      # 可选软预算（0=不限）
    optimizer: dict = field(default_factory=lambda: {"name": "adamw", "lr": 1e-4,
                                                     "weight_decay": 0.01})
    device: str = "cpu"
    allow_gpu: bool = False                       # GPU 纪律默认关（S06 worker 显式开）
    resume: bool = False                          # resume 构建：允许 run_dir 已有 checkpoint
    gradient_clip_norm: float | None = None
    log_every: int = 1
    created_at: str = ""                          # 不参与 config hash

    def __post_init__(self):
        if isinstance(self.freeze, dict):
            self.freeze = FreezePolicy(**self.freeze)
        if isinstance(self.stop, dict):
            self.stop = EarlyStop(**self.stop)
        self.validate()

    def validate(self):
        if self.gradient_clip_norm is not None and self.gradient_clip_norm <= 0:
            raise ConfigError("E_GRAD_CLIP", "gradient_clip_norm must be positive")
        if self.seed is None or int(self.seed) < 0:
            raise ConfigError("E_SEED_REQUIRED", "seed 必须显式预注册（固定 seed 是卡 S05 硬要求）")
        if self.engine_id not in (ENGINE_ID, "fontdiffuser-hf7bd89f3-npz-v1"):
            raise ConfigError("E_ENGINE_ID", f"engine_id must be {ENGINE_ID!r}, got {self.engine_id!r}")
        if self.max_epochs <= 0 or self.max_steps <= 0:
            raise ConfigError("E_BUDGET_REQUIRED",
                              "max_epochs 与 max_steps 都必须 >0（预算/停止规则先于训练预注册）")
        if not self.data_snapshot_dir or not self.data_snapshot_sha256:
            raise ConfigError("E_SNAPSHOT_REQUIRED",
                              "必须绑定 S03 不可变快照（目录+content hash）；禁止直读 P4 活动目录")
        if str(self.device).startswith("cuda") and not self.allow_gpu:
            raise ConfigError("E_GPU_NOT_ALLOWED",
                              "device=cuda 需显式 allow_gpu=True（GPU 纪律/S06 互斥锁未接线）")
        if not self.resume and self.run_dir and native_path(self.run_dir).exists() \
                and any(native_path(self.run_dir).iterdir()):
            raise ConfigError("E_RUN_EXISTS",
                              f"run_dir 已非空: {self.run_dir}（输出不可覆盖旧 run；resume=True 除外）")
