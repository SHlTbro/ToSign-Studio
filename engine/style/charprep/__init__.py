# -*- coding: utf-8 -*-
"""S02 字符准备模块（charprep）：已确认字块 → 拆字 → label/Unicode 确认 → alpha 规范化 → 训练 ready。

硬规则（卡 S02，违反即缺陷）：
1. ``owner_confirmed`` 只能由显式确认动作写入（CLI ``confirm --confirm-by <name>``），
   本包任何函数**永不**自动把样本定为 confirmed。
2. ``candidate`` / ``uncertain`` 单列，**不可**进入训练导出（manifest.export_training）。
3. 多博主/风格不混池：每条样本带 ``style_id``，导出必须显式指定单一 ``style_id``。
4. 码点必须与 label 严格一致（ord(label) == 声明码点）；组合码点/变体选择子拒绝。

子模块：
- normalize : 字块 alpha → 128×128 画布 / fit 112 / 边距 8px（E03 冻结 glyph_png 规格）
- split     : 连通域 + 投影谷拆字，输出 candidate/uncertain 两级，不自动定案
- manifest  : 训练级样本登记 schema + 导出门（仅 owner_confirmed，单 style_id）
- cli       : inspect / split / normalize / register / confirm / export
"""

PREP_VERSION = "charprep/1.0.0"

__all__ = ["normalize", "split", "manifest", "cli", "PREP_VERSION"]
