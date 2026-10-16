# -*- coding: utf-8 -*-
"""S05 trainer.receipt — run 收据 schema（ENGINEERING_SKELETON）。

卡 S05：保存 best/last/环境/日志及失败样本；收据锚定 run 的全部输入身份——
data snapshot hash / config hash / 基座 ckpt hash / 代码版本 / env / 逐 epoch 指标。
S10 发布绑定、S07 进度读取面都以本 schema 为雏形（INTERFACE_CONTRACT_DRAFT §7）。
注意：收据存在且 final_status=COMPLETED **不代表质量达标**（质量门在 S08/S09）。
"""
from __future__ import annotations

import datetime as _dt
import json
import platform
from dataclasses import dataclass, field
from pathlib import Path
from engine.style.m2_adapter.trainer_resume_protocol import native_path

from .config import ENGINE_ID, M2_ADAPTER_IDENTIFIER, TRAINER_VERSION, hash_config

RECEIPT_NAME = "receipt.json"


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class RunReceipt:
    run_id: str
    run_dir: str
    seed: int
    device: str
    # ---- 输入身份链（hash 锚定，缺一不可）----
    data_snapshot_sha256: str = ""
    data_snapshot_dir: str = ""
    config_hash: str = ""
    code_version: str = ""                       # trainer 模块文件集内容 hash
    base_ckpt_sha256: dict = field(default_factory=dict)
    engine_id: str = ENGINE_ID
    source_type: str = M2_ADAPTER_IDENTIFIER
    preprocessing: dict = field(default_factory=dict)
    training_objective: dict = field(default_factory=dict)
    # ---- 环境 ----
    env: dict = field(default_factory=dict)
    # ---- 过程 ----
    epochs: list = field(default_factory=list)   # [{epoch,train_loss,val_loss,lr,global_step,seconds}]
    best_epoch: int = -1
    best_val: float | None = None
    final_status: str = "RUNNING"                # RUNNING | COMPLETED | FAILED
    failure_code: str = ""
    n_failure_samples: int = 0
    # ---- 阶段区分（防伪报，写死语义）----
    engineering_skeleton: bool = True
    loaded_real_base_weights: bool = False       # 本骨架永不加载 FontDiffuser 权重
    claims_quality_pass: bool = False            # 收据永远不得宣称质量通过
    started_at: str = field(default_factory=_now)
    finished_at: str = ""
    notes: str = ""

    def finalize(self, status: str, failure_code: str = ""):
        self.final_status = status
        self.failure_code = failure_code
        self.finished_at = _now()

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    def save(self, path=None) -> Path:
        path = Path(path) if path else Path(self.run_dir) / RECEIPT_NAME
        native_path(path.parent).mkdir(parents=True, exist_ok=True)
        native_path(path).write_bytes((json.dumps(self.to_dict(), ensure_ascii=False, indent=1) + "\n")
                         .encode("utf-8"))
        return path

    @classmethod
    def load(cls, path) -> "RunReceipt":
        data = json.loads(native_path(path).read_bytes().decode("utf-8"))
        return cls(**data)

    @classmethod
    def create(cls, config, code_version: str, run_id: str) -> "RunReceipt":
        import torch  # 局部 import：receipt 可在无 torch 环境被 load
        import numpy
        env = {
            "python": platform.python_version(),
            "torch": torch.__version__,
            "numpy": numpy.__version__,
            "platform": platform.platform(),
            "cuda_available": bool(torch.cuda.is_available()),
            "torch_num_threads": torch.get_num_threads(),
        }
        return cls(run_id=run_id, run_dir=str(config.run_dir), seed=config.seed,
                   device=config.device, engine_id=config.engine_id,
                   preprocessing=dict(config.preprocessing), training_objective=dict(config.training_objective),
                   data_snapshot_sha256=config.data_snapshot_sha256,
                   data_snapshot_dir=str(config.data_snapshot_dir),
                   config_hash=hash_config(config), code_version=code_version,
                   base_ckpt_sha256=dict(config.base_ckpt_sha256), env=env,
                   engineering_skeleton=True, loaded_real_base_weights=False)
