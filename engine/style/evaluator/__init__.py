# -*- coding: utf-8 -*-
"""S08 engine.style.evaluator — 个人风格评估器（自动 proxy 指标 + 失败样本归档）。

边界（写死，防伪报）：
- 本模块只产出**自动结构 proxy 指标**，不产出质量 verdict——真人视觉判定属 S09 盲评域；
  自动评估允许 NEGATIVE / INCONCLUSIVE，不要求必通过（卡 S08 DoD）。
- 只读消费 dataset/（快照 schema）与 m1_adapter/（生成句柄）；不 import app/**、render/**。
- 指标全部是**结构代理**（ink 比例/质心/连通域/透明层健全/统计风格距离），
  **不是 OCR、不是字形正确性的人判**；"wrong_char_proxy" 字段名中的 proxy 即此意。
"""
EVALUATOR_VERSION = "engine/style/evaluator/0.1.0"

# run_eval 不在包 __init__ 预加载（-m 方式运行会双 import；CLI 直接用子模块路径）
from . import metrics, receipt, gpu_lock  # noqa: E402,F401
