# -*- coding: utf-8 -*-
"""S03 数据集导出/同源分组/不可变快照模块（dataset）。

任务卡：docs/plans/2026-09-27-full-feature-taskpack/02-personal-style-task-cards.md §S03。
数据红线：B10 PRIMARY 20 + RESERVE 7 = eval_only、B7/B9 SEALED 禁触、未知历史=UNKNOWN；
本模块机器执行 R04 台账（.ai-hub/parallel-lines/line-b/b-research/task-results/
data-usage-ledger-draft.json）的禁入规则——"无 hash 碰撞"≠NEVER_USED。

子模块：
- schema    : 训练级行校验（与 S02 charprep.manifest 导出行对齐，只按 schema 消费不 import）
- ledger    : R04 数据台账 → 机器可执行禁入索引（forbidden/unknown/family）
- split     : 同 parent_source_hash 分组整组入同一侧（C-57）+ 近重复 family 不跨 split
- snapshot  : 不可变快照（写后内容 hash 冻结 + index.json + FROZEN 标记，拒绝覆盖）
- validator : 唯一 production 校验函数（正例复算与负例注入走同一函数）

硬规则（卡 S03，违反即缺陷）：
1. eval-only/SEALED/UNKNOWN 父 hash 硬禁入（读 R04 台账作配置，不靠人工记忆）。
2. 同 parent / 同 family 的样本整组进同一 split，结构性不可能跨侧。
3. 空 split、最低数据门不足 → INSUFFICIENT_DATA 结构化结果，不复制样本凑数。
4. 快照不可变：不可覆盖、可复算、篡改必被 verify 发现。
"""

from .schema import DatasetError, validate_row, canonical_json, sha256_bytes
from .ledger import LedgerIndex, DEFAULT_LEDGER_REL_PATH, default_ledger_path
from .split import SplitConfig, SplitPlan, plan_splits
from .validator import (
    Failure, ValidationResult, validate_pool, validate_snapshot)
from .snapshot import (
    BuildResult, build_snapshot, load_snapshot, verify_snapshot, unlock_snapshot)

__all__ = [
    "DatasetError", "validate_row", "canonical_json", "sha256_bytes",
    "LedgerIndex", "DEFAULT_LEDGER_REL_PATH", "default_ledger_path",
    "SplitConfig", "SplitPlan", "plan_splits",
    "Failure", "ValidationResult", "validate_pool", "validate_snapshot",
    "BuildResult", "build_snapshot", "load_snapshot", "verify_snapshot", "unlock_snapshot",
]
