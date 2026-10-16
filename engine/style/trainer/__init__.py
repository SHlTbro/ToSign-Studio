# -*- coding: utf-8 -*-
"""S05 trainer 骨架（ENGINEERING_SKELETON）—— M2 适配器训练的工程骨架与 CPU 单测。

ENGINEERING_SKELETON 声明（防伪报边界）：
- 本模块是 S01 ENGINE_DECISION 裁定的 FontDiffuser 唯一路线下的 **训练工程骨架**：
  训练循环/checkpoint/resume/收据机制为真实实现（真梯度、真状态、真 hash），
  但本波**未加载 FontDiffuser 权重、未开始任何真实训练、未产出任何真实 checkpoint**
  （GPU 波次、S03 数据快照达标 + Owner 预算/停止规则预注册后才允许）。
- M2_ADAPTER 为冻结标识（contracts/style-font-pipeline-v1.json）；实现引擎名
  fontdiffuser-frozen-v1（INTERFACE_CONTRACT_DRAFT §1）。
- 实证约束（S01 probe_m2_backward，2026-09-27）：基座 100.49M 参数默认全可训 ——
  本模块 config 层**强制**表达"冻结基座 + 仅训 adapter"，结构上不可能全量微调。

子模块：
- config  : TrainConfig/FreezePolicy（红线在 validate() 里强制，不靠调用方自觉）
- trainer : 训练循环（有限 loss 断言/非零梯度断言/checkpoint best+last/resume/失败样本归档）
- receipt : run 收据（data snapshot hash/config hash/基座 ckpt hash/代码版本/env/逐 epoch 指标）
"""
from .config import TrainConfig, FreezePolicy, EarlyStop, ConfigError, hash_config, hash_files
from .trainer import Trainer, TrainerError, param_fingerprint
from .receipt import RunReceipt, TRAINER_VERSION

__all__ = [
    "TrainConfig", "FreezePolicy", "EarlyStop", "ConfigError", "hash_config", "hash_files",
    "Trainer", "TrainerError", "param_fingerprint",
    "RunReceipt", "TRAINER_VERSION",
]
