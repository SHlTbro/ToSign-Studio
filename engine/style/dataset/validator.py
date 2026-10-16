# -*- coding: utf-8 -*-
"""S03 dataset.validator — 唯一 production 校验函数（正例复算与负例注入走同一函数）。

显式 FAIL 码全集（卡 S03 DoD：注入负例必须真实失败）：
- E_EMPTY_SPLIT               train/val 任一侧为空（存储态 split.json 也要过这关）
- E_FORBIDDEN_PARENT          eval-only/SEALED/已训练底图 父 hash 混入（R04 台账硬禁入）
- E_UNKNOWN_PARENT            父 hash 不在台账且无 user_upload_confirmed 来源声明
                              （未知历史=UNKNOWN；"无 hash 碰撞"≠NEVER_USED）
- E_FAMILY_CROSS_SPLIT        同近重复 family 跨 train/val（R04 近重复家族规则）
- E_SAME_PARENT_CROSS_SPLIT   同 parent_source_hash 跨 split（C-57 同源不跨侧）
- E_CROSS_USER                单快照混入多个 style_id（多博主/风格不混池）
- E_HASH_MISSING / E_HASH_MISMATCH / E_ALPHA_MISSING / E_DUPLICATE_ALPHA / E_ROW_*
INSUFFICIENT_DATA：最低数据门不足 → 结构化状态 + reasons，不是异常、不凑数、不产快照。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ledger import LedgerIndex
from .schema import DatasetError, validate_row

PROVENANCE_KEY = "parent_origin"
PROVENANCE_VALUE = "user_upload_confirmed"


@dataclass
class Failure:
    code: str
    detail: str
    sample_ids: list = field(default_factory=list)


@dataclass
class ValidationResult:
    status: str                    # OK | FAIL | INSUFFICIENT_DATA
    failures: list = field(default_factory=list)          # list[Failure]
    insufficient_reasons: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "OK"

    def raise_if_bad(self):
        if self.status == "FAIL":
            raise DatasetError(
                self.failures[0].code,
                "; ".join(f"{f.code}({','.join(map(str, f.sample_ids[:3]))}): {f.detail}"
                          for f in self.failures[:5]) or "validation failed")
        if self.status == "INSUFFICIENT_DATA":
            raise DatasetError("E_INSUFFICIENT_DATA", "; ".join(self.insufficient_reasons))


def _gates_attr(gates, name, default):
    return getattr(gates, name, default) if gates is not None else default


def validate_pool(rows, splits=None, ledger: LedgerIndex | None = None,
                  unknown_parent_policy: str = "allow_with_provenance", gates=None) -> ValidationResult:
    """生产校验：rows=训练级行 list[dict]；splits=sample_id->side（None 则跳过 split 维度）。

    SnapshotBuilder / plan_splits / validate_snapshot 全部经由本函数，保证
    "正例与注入负例过同一 production 验证函数"（卡 S03 DoD）。
    """
    failures: list[Failure] = []
    rows = list(rows)
    if not rows:
        return ValidationResult("FAIL", [Failure("E_EMPTY_POOL", "no rows given")], stats={})

    clean: list[dict] = []
    for r in rows:
        try:
            clean.append(validate_row(r))
        except DatasetError as e:
            failures.append(Failure(e.code, str(e), [str(r.get("sample_id", "?")) if isinstance(r, dict) else "?"]))

    style_ids = sorted({str(r["style_id"]) for r in clean})
    if len(style_ids) > 1:
        failures.append(Failure("E_CROSS_USER",
                                f"multiple style_id in one pool: {style_ids}（多博主/风格不混池）",
                                [r["sample_id"] for r in clean if r["style_id"] != style_ids[0]]))

    seen_alpha, seen_sid = {}, {}
    for r in clean:
        a = r["final_alpha_sha256"]
        if a in seen_alpha:
            failures.append(Failure("E_DUPLICATE_ALPHA",
                                    f"final_alpha_sha256 {a[:12]}… appears twice（重命名逃不过内容 hash）",
                                    [seen_alpha[a], r["sample_id"]]))
        seen_alpha[a] = r["sample_id"]
        if r["sample_id"] in seen_sid:
            failures.append(Failure("E_DUPLICATE_SAMPLE_ID",
                                    f"sample_id {r['sample_id']} duplicated", [r["sample_id"]]))
        seen_sid[r["sample_id"]] = True

    # ---- 台账禁入（R04 规则机器执行）----
    if ledger is not None:
        for r in clean:
            ph = str(r["parent_source_hash"]).lower()
            st = ledger.status_of(ph)
            if st is not None:
                if ph in ledger.forbidden_hashes:
                    failures.append(Failure(
                        "E_FORBIDDEN_PARENT",
                        f"parent {ph[:12]}… in ledger group {st['group_id']} "
                        f"(role={st['role']}, training_allowed={st['training_allowed']}) —— 硬禁入",
                        [r["sample_id"]]))
            else:
                # 台账未列出的父 hash：默认按全局规则视为 UNKNOWN；
                # 仅 allow_with_provenance 模式下，user_upload_confirmed 来源声明可豁免。
                allowed = (unknown_parent_policy == "allow_with_provenance"
                           and r.get(PROVENANCE_KEY) == PROVENANCE_VALUE)
                if not allowed:
                    failures.append(Failure(
                        "E_UNKNOWN_PARENT",
                        f"parent {ph[:12]}… not in R04 ledger and no accepted "
                        f"{PROVENANCE_KEY}（未知历史=UNKNOWN → 禁入；"
                        "无 hash 碰撞≠NEVER_USED）",
                        [r["sample_id"]]))

    stats: dict = {}
    pool_parents = sorted({str(r["parent_source_hash"]).lower() for r in clean})
    pool_labels = sorted({r["label"] for r in clean})
    stats["pool"] = {"n_samples": len(clean), "n_parents": len(pool_parents),
                     "n_distinct_labels": len(pool_labels), "style_ids": style_ids}

    # ---- split 维度 ----
    if splits is not None:
        by_id = {r["sample_id"]: r for r in clean}
        missing = sorted(s for s in splits if s not in by_id)
        if missing:
            failures.append(Failure("E_SPLIT_UNKNOWN_SAMPLE",
                                    f"split references unknown sample_ids: {missing[:5]}", missing[:5]))
        bad_side = sorted(s for s, side in splits.items() if side not in ("train", "val"))
        if bad_side:
            failures.append(Failure("E_SPLIT_BAD_SIDE",
                                    f"illegal side values: {[(s, splits[s]) for s in bad_side[:3]]}",
                                    bad_side[:5]))
        train_ids = sorted(s for s, side in splits.items() if side == "train" and s in by_id)
        val_ids = sorted(s for s, side in splits.items() if side == "val" and s in by_id)
        if not train_ids:
            failures.append(Failure("E_EMPTY_SPLIT", "train split is empty（空 split 拒绝）"))
        elif not val_ids and gates is None:
            # 纯快照校验（无数据门上下文）：空 val 视为结构性失败（如 split 被篡改/截断）。
            # 计划器上下文（gates 提供时）走 INSUFFICIENT_DATA，见下方数据门评估。
            failures.append(Failure("E_EMPTY_SPLIT", "val split is empty（空 split 拒绝）"))
        val_empty_for_gates = (not val_ids)

        sid_map: dict[str, list] = {}
        for r in clean:
            sid_map.setdefault(str(r["parent_source_hash"]).lower(), []).append(r["sample_id"])
        for sha, sids in sorted(sid_map.items()):
            sides = sorted({splits[s] for s in sids if s in splits})
            if len(sides) > 1:
                failures.append(Failure("E_SAME_PARENT_CROSS_SPLIT",
                                        f"parent {sha[:12]}… straddles splits {sides}"
                                        "（C-57 同源不跨 split）", sorted(sids)))
        if ledger is not None:
            for fid in sorted(ledger.families):
                members = sorted(m for m in ledger.families[fid] if m in sid_map)
                if not members:
                    continue
                sides = sorted({splits[s] for m in members for s in sid_map[m] if s in splits})
                if len(sides) > 1:
                    failures.append(Failure(
                        "E_FAMILY_CROSS_SPLIT",
                        f"near-duplicate family {fid} straddles splits {sides}"
                        "（近重复家族跨 split 泄漏）",
                        sorted(s for m in members for s in sid_map[m])))

        stats["train"] = {"n_samples": len(train_ids),
                          "n_parents": len({by_id[s]["parent_source_hash"] for s in train_ids}),
                          "n_labels": len({by_id[s]["label"] for s in train_ids})}
        stats["val"] = {"n_samples": len(val_ids),
                        "n_parents": len({by_id[s]["parent_source_hash"] for s in val_ids}),
                        "n_labels": len({by_id[s]["label"] for s in val_ids})}

    if failures:
        return ValidationResult("FAIL", failures, stats=stats)

    # ---- 最低数据门（无硬失败才评估；不足 → INSUFFICIENT_DATA，不凑数）----
    if splits is not None and gates is not None:
        t, v, p = stats["train"], stats["val"], stats["pool"]
        checks = (
            ("min_train_samples", t["n_samples"], "train samples"),
            ("min_val_samples", v["n_samples"], "val samples"),
            ("min_train_groups", t["n_parents"], "train parent groups"),
            ("min_val_groups", v["n_parents"], "val parent groups"),
            ("min_distinct_labels", p["n_distinct_labels"], "distinct labels（有效字符数门）"),
            ("min_distinct_parents", p["n_parents"], "distinct parents（独立来源数门）"),
        )
        reasons = [f"{human} {actual} < {attr} {need}"
                   for attr, actual, human in checks
                   for need in [_gates_attr(gates, attr, 1)]
                   if actual < need]
        if val_empty_for_gates:
            reasons.append("val split empty（无任何组件落入 val：样本/父组不足以支撑 holdout）")
        if reasons:
            return ValidationResult("INSUFFICIENT_DATA", [], insufficient_reasons=reasons, stats=stats)

    return ValidationResult("OK", [], stats=stats)


def validate_snapshot(snapshot_dir, ledger: LedgerIndex | None = None, gates=None) -> ValidationResult:
    """对已冻结快照目录整体复核：完整性（hash 复算 + FROZEN 标记）+ 同一 production 池校验。

    完整性失败与池级违规**聚合上报**：例如 split.json 被篡改成家族跨侧时，
    E_INDEX_MISMATCH 与 E_FAMILY_CROSS_SPLIT 同时出现（篡改面与数据面都逃不掉）。
    """
    from .snapshot import verify_snapshot  # 局部 import 避免环
    integ = verify_snapshot(snapshot_dir, ledger=ledger)
    rows, index, splits = integ.stats.get("loaded", ([], {}, None))
    pool = validate_pool(rows, splits=splits, ledger=ledger,
                         unknown_parent_policy=str(index.get("unknown_parent_policy",
                                                             "allow_with_provenance")),
                         gates=gates)
    if integ.status == "OK":
        return pool
    combined = ValidationResult(
        "FAIL",
        list(integ.failures) + list(pool.failures),
        stats=pool.stats)
    return combined
