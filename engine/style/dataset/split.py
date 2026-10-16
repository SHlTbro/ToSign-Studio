# -*- coding: utf-8 -*-
"""S03 dataset.split — 同源分组 split 计划（确定性、组原子、INSUFFICIENT_DATA 不凑数）。

规则（卡 S03 / R04 台账）：
1. C-57：同 ``parent_source_hash`` 的样本**整组**入同一侧（同图不同裁剪/重命名逃不脱
   内容级 hash 分组）。
2. 近重复 family（R04 ``near_duplicate_of`` / ``family_id``）跨 split 禁入：家族成员
   的父 hash 经并查集并入同一"组件"，组件是分配的最小单元——结构性不可能跨侧。
3. 确定性：``seed`` 控制组件洗牌；同输入同 seed → 同 split（可复算）。
4. 分配后走 validator 同一 production 校验；最低数据门不足 → INSUFFICIENT_DATA
   结构化结果（带 reasons），绝不复制样本凑数、也绝不产出快照。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .ledger import LedgerIndex
from .schema import DatasetError
from .validator import ValidationResult, validate_pool


@dataclass
class SplitConfig:
    """split 比例与最低数据门（门槛不足 → INSUFFICIENT_DATA，不降门）。"""
    train_ratio: float = 0.8
    seed: int = 20260927
    # 台账未列父 hash 的处理：allow_with_provenance（默认，需 user_upload_confirmed 声明）
    # 或 reject（仅认台账，任何未列父 hash 一律禁入）
    unknown_parent_policy: str = "allow_with_provenance"
    # ---- 最低数据门（E03/E28 口径由调用方按真实数据预注册；这里给保守默认）----
    min_train_samples: int = 8
    min_val_samples: int = 2
    min_train_groups: int = 2      # train 侧最少独立父图（来源）数
    min_val_groups: int = 1
    min_distinct_labels: int = 2   # 全池最少有效字符数（去重 label）
    min_distinct_parents: int = 3  # 全池最少独立来源数

    def __post_init__(self):
        if not 0.0 < self.train_ratio < 1.0:
            raise DatasetError("E_SPLIT_RATIO", f"train_ratio must be in (0,1): {self.train_ratio}")
        if self.unknown_parent_policy not in ("reject", "allow_with_provenance"):
            raise DatasetError("E_SPLIT_POLICY", f"bad unknown_parent_policy: {self.unknown_parent_policy!r}")


@dataclass
class SplitPlan:
    status: str                      # OK | INSUFFICIENT_DATA
    assignments: dict[str, str]      # sample_id -> "train" | "val"
    components: list[dict]           # [{component_id, parent_hashes, family_ids, sample_ids}]
    stats: dict
    reasons: list[str] = field(default_factory=list)
    validation: ValidationResult | None = None


def _union_find_components(rows, ledger: LedgerIndex | None) -> list[dict]:
    """并查集：同 parent 一组；台账 family 成员并组。返回组件列表（确定序）。"""
    parent_of = {r["sample_id"]: r["parent_source_hash"].lower() for r in rows}
    parents = sorted(set(parent_of.values()))
    index = {p: i for i, p in enumerate(parents)}
    parent = list(range(len(parents)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # family 边：只并"池内都出现"的成员（台账家族是跨数据集的全局事实）
    if ledger is not None:
        for fid in sorted(ledger.families):
            members = sorted(m for m in ledger.families[fid] if m in index)
            for m in members[1:]:
                union(index[members[0]], index[m])

    comp_members: dict[int, list[str]] = {}
    for p in parents:
        comp_members.setdefault(find(index[p]), []).append(p)
    components = []
    for root in sorted(comp_members):
        ph = sorted(comp_members[root])
        fids = sorted({ledger.family_of(p) for p in ph} - {None}) if ledger else []
        sids = sorted(s for s, pp in parent_of.items() if pp in set(ph))
        components.append({
            "component_id": f"c{root:04d}",
            "parent_hashes": ph, "family_ids": fids, "sample_ids": sids,
        })
    return components


def plan_splits(rows: list[dict], ledger: LedgerIndex | None,
                cfg: SplitConfig) -> SplitPlan:
    """确定性 split 计划。先池级校验（含台账禁入），再整组分配，再整体验证。"""
    # 1) 池级硬校验（无 split 维度）——与 validator 同一 production 函数
    pre = validate_pool(rows, splits=None, ledger=ledger,
                        unknown_parent_policy=cfg.unknown_parent_policy)
    if pre.status == "FAIL":
        return SplitPlan(status="FAIL", assignments={}, components=[],
                         stats=pre.stats, reasons=[f"{f.code}: {f.detail}" for f in pre.failures],
                         validation=pre)

    # 2) 同源组件 + 确定性分配
    components = _union_find_components(rows, ledger)
    target_train = int(round(len(rows) * cfg.train_ratio))
    rng = random.Random(cfg.seed)
    order = sorted(components, key=lambda c: (-len(c["sample_ids"]), c["component_id"]))
    rng.shuffle(order)  # 洗牌后仍确定性（seed 固定）

    assignments: dict[str, str] = {}
    train_n = 0
    for comp in order:
        side = "train" if train_n < target_train else "val"
        for sid in comp["sample_ids"]:
            assignments[sid] = side
        if side == "train":
            train_n += len(comp["sample_ids"])

    # holdout 保底：贪心装箱可能把全部组件吞进 train（大组件后到）。
    # 若 val 为空且 train ≥2 个组件，把 train 中最小的组件整体移到 val
    # （组件原子移动，不破坏同源/家族不跨侧；比例让位于 holdout 存在性）。
    if train_n == len(rows):
        train_comps = [c for c in order if assignments[c["sample_ids"][0]] == "train"]
        if len(train_comps) >= 2:
            smallest = min(train_comps, key=lambda c: (len(c["sample_ids"]), c["component_id"]))
            for sid in smallest["sample_ids"]:
                assignments[sid] = "val"

    # 3) 分配后整体校验（空 split/组跨侧/家族跨侧/数据门）——同一 production 函数
    result = validate_pool(rows, splits=assignments, ledger=ledger,
                           unknown_parent_policy=cfg.unknown_parent_policy,
                           gates=cfg)

    stats = result.stats
    if result.status == "FAIL":
        return SplitPlan(status="FAIL", assignments=assignments, components=components,
                         stats=stats, reasons=[f"{f.code}: {f.detail}" for f in result.failures],
                         validation=result)
    return SplitPlan(status=result.status, assignments=assignments, components=components,
                     stats=stats, reasons=result.insufficient_reasons, validation=result)
