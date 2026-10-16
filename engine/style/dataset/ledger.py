# -*- coding: utf-8 -*-
"""S03 dataset.ledger — R04 数据使用台账 → 机器可执行禁入索引。

台账：.ai-hub/parallel-lines/line-b/b-research/task-results/data-usage-ledger-draft.json
（R04 产出，S03 强制输入）。本模块把它的自然语言规则翻译成数据结构：

1. forbidden_hashes —— 所有 ``training_allowed != true`` 组的 item sha256
   （含 false 与 null：eval_only / reserve_eval_only / sealed_no_touch /
   restricted / unassigned_candidate 一律禁入）+ ``v03r1_training_parent_hashes``
   全部 38 个已训练底图 hash。
2. unknown_hashes —— history_status 以 UNKNOWN 开头的 item（全局规则：未知历史=UNKNOWN，
   禁入；"无 hash 碰撞"≠NEVER_USED，台账明文降级规则照抄）。
3. families —— 近重复家族（B10 近重复 4 张与 Slice A 同场景同签）：由 item 的
   ``near_duplicate_of``（8-hex 文件名前缀）解析出成员对，或显式 ``family_id`` 字段。
   家族禁止跨 train/val split。

只读消费：本模块绝不改写台账文件；台账文件自身 sha256 一并登记进快照 index，
供快照 verify 时检测"台账已变更、旧快照需重审"。
"""
from __future__ import annotations

import json
from pathlib import Path

from .schema import DatasetError, sha256_bytes

DEFAULT_LEDGER_REL_PATH = (
    ".ai-hub/parallel-lines/line-b/b-research/task-results/data-usage-ledger-draft.json")

# R04 v2 promotion record（编排放权：active ledger 由它指认，禁止在本模块再硬编码 v2 路径）
PROMOTION_REL_PATH = (
    ".ai-hub/parallel-lines/line-b/b-research/R04-LEDGER-V2-PROMOTION.json")


def default_ledger_path(project_root) -> Path:
    return Path(project_root) / DEFAULT_LEDGER_REL_PATH


def resolve_active_ledger_path(project_root) -> Path:
    """从 R04 promotion record 解析当前 active ledger（fail-closed）。

    promotion record 缺失/非法/active ledger 缺失/hash 漂移一律 DatasetError——
    绝不回退到 draft，也绝不在这里硬编码 v2 文件名（v3 晋升时无需改代码）。
    """
    root = Path(project_root)
    promo_path = root / PROMOTION_REL_PATH
    if not promo_path.is_file():
        raise DatasetError("E_PROMOTION_MISSING",
                           f"R04 promotion record not found: {promo_path}")
    try:
        promo = json.loads(promo_path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise DatasetError("E_PROMOTION_INVALID", f"promotion record not valid JSON: {e}") from e
    active = promo.get("active_ledger") or {}
    rel, expected = active.get("file"), active.get("sha256")
    if not rel or not expected:
        raise DatasetError("E_PROMOTION_INVALID",
                           "promotion record missing active_ledger.file/sha256")
    ledger_path = root / rel
    if not ledger_path.is_file():
        raise DatasetError("E_PROMOTION_INVALID",
                           f"active ledger missing on disk: {ledger_path}")
    actual = sha256_bytes(ledger_path.read_bytes())
    if actual != str(expected).lower():
        raise DatasetError("E_PROMOTION_HASH_MISMATCH",
                           f"active ledger sha256 drift: {actual} != {expected}")
    return ledger_path


def _iter_group_items(group: dict):
    """产出 (sub_group_id|None, item)。兼容组内有/无 sub_groups 两种布局。"""
    for item in group.get("items", []) or []:
        yield None, item
    for sub in group.get("sub_groups", []) or []:
        for item in sub.get("items", []) or []:
            yield sub.get("sub_group_id"), item


class LedgerIndex:
    """台账的机器可执行视图（只读）。"""

    def __init__(self, *, path, sha256, data,
                 forbidden_hashes, unknown_hashes, families, item_index):
        self.path = Path(path)
        self.sha256 = sha256
        self.data = data
        self.forbidden_hashes = frozenset(forbidden_hashes)
        self.unknown_hashes = frozenset(unknown_hashes)
        self.families = {fid: frozenset(members) for fid, members in families.items()}
        self._item_index = item_index  # sha256 -> {group_id, role, training_allowed, history_status}

    # ---- 构造 ----
    @classmethod
    def load(cls, path) -> "LedgerIndex":
        path = Path(path)
        if not path.is_file():
            raise DatasetError("E_LEDGER_MISSING", f"R04 ledger not found: {path}")
        raw = path.read_bytes()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise DatasetError("E_LEDGER_PARSE", f"ledger not valid JSON: {e}") from e
        if not isinstance(data.get("groups"), list):
            raise DatasetError("E_LEDGER_PARSE", "ledger has no groups[]")

        forbidden: set[str] = set()
        unknown: set[str] = set()
        families: dict[str, set[str]] = {}
        item_index: dict[str, dict] = {}
        filename_to_sha: dict[str, str] = {}
        near_dup_refs: list[tuple[str, str, str]] = []  # (family_id, item_sha, prefix)

        for group in data["groups"]:
            gid = group.get("group_id", "?")
            allowed = group.get("training_allowed")
            history = str(group.get("history_status", ""))
            for _sub, item in _iter_group_items(group):
                sha = item.get("sha256")
                if not sha:
                    continue  # 无 hash 条目无法做内容级断言，跳过（不改写台账）
                sha = str(sha).lower()
                item_index[sha] = {
                    "group_id": gid, "role": group.get("role"),
                    "training_allowed": allowed, "history_status": history,
                    "filename": item.get("filename"),
                }
                if item.get("filename"):
                    filename_to_sha[str(item["filename"])] = sha
                if allowed is not True:  # false 与 null（未裁定）一律禁入
                    forbidden.add(sha)
                if history.startswith("UNKNOWN"):
                    unknown.add(sha)
                    forbidden.add(sha)  # 未知历史=UNKNOWN → 硬禁入
                fid = item.get("family_id")
                if fid:
                    families.setdefault(str(fid), set()).add(sha)
                if item.get("near_duplicate_of"):
                    near_dup_refs.append((f"{gid}:{item['near_duplicate_of']}", sha,
                                          str(item["near_duplicate_of"])))

        # 解析 near_duplicate_of（8-hex 文件名前缀）→ 把被指目标拉进同一家族
        for fid, sha, prefix in near_dup_refs:
            matches = [s for fn, s in filename_to_sha.items() if fn.startswith(prefix)]
            if not matches:
                raise DatasetError(
                    "E_LEDGER_FAMILY_DANGLING",
                    f"near_duplicate_of={prefix!r} resolves to no ledger item（前缀悬空）")
            members = families.setdefault(fid, set())
            members.add(sha)
            members.update(matches)

        # v03r1 已训练底图：新训练禁止直接复用（台账明文）
        v03 = data.get("v03r1_training_parent_hashes") or {}
        for sha in v03.get("sha256", []) or []:
            sha = str(sha).lower()
            forbidden.add(sha)
            item_index.setdefault(sha, {
                "group_id": "v03r1_training_parent_hashes", "role": "prior_train_base",
                "training_allowed": False, "history_status": "PRIOR_TRAIN"})

        return cls(path=path, sha256=sha256_bytes(raw), data=data,
                   forbidden_hashes=forbidden, unknown_hashes=unknown,
                   families=families, item_index=item_index)

    # ---- 查询 ----
    def status_of(self, parent_sha: str) -> dict | None:
        """父 hash 在台账中的角色记录；不在台账 → None（由 unknown 策略决定去留）。"""
        return self._item_index.get(str(parent_sha).lower())

    def family_of(self, parent_sha: str) -> str | None:
        parent_sha = str(parent_sha).lower()
        for fid, members in self.families.items():
            if parent_sha in members:
                return fid
        return None

    def verify_sources(self, project_root) -> list[dict]:
        """只读复核台账 sources 清单：JSON/JSONL 元数据做 sha256 比对；其余只查存在性
        （绝不读任何图像/SEALED 内容——本模块只消费台账与其元数据）。"""
        root = Path(project_root)
        out = []
        for s in self.data.get("sources", []) or []:
            p = root / str(s.get("path", ""))
            entry = {"path": s.get("path"), "exists": p.is_file(), "sha_match": None}
            if p.is_file() and str(s.get("path", "")).lower().endswith((".json", ".jsonl")):
                entry["sha_match"] = (sha256_bytes(p.read_bytes()) == str(s.get("sha256", "")).lower())
            out.append(entry)
        return out

    def assert_sources_valid(self, project_root=None) -> list[dict]:
        """Fail closed when a declared JSON/JSONL source is missing or stale.

        Binary/image source contents are intentionally not opened here; R04's
        metadata source list is verified without touching sealed image data.
        """
        if not self.data.get("sources"):
            return []
        root = Path(project_root) if project_root is not None else self._project_root()
        results = self.verify_sources(root)
        invalid = [r for r in results if not r["exists"] or r["sha_match"] is False]
        if invalid:
            details = "; ".join(
                f"{row['path']}: " + ("missing" if not row["exists"] else "sha256 mismatch")
                for row in invalid
            )
            raise DatasetError("E_LEDGER_SOURCE_MISMATCH", details)
        return results

    def _project_root(self) -> Path:
        """Find the repository root without assuming a fixed ledger directory depth."""
        for candidate in (self.path.resolve(), *self.path.resolve().parents):
            if (candidate / "materials").is_dir() and (candidate / "artifacts").is_dir():
                return candidate
        raise DatasetError("E_PROJECT_ROOT_UNKNOWN", f"cannot infer project root from ledger {self.path}")

    # ---- R04 v2 semantic integrity（promotion 语义；legacy ledger 不走此路径） ----

    def is_v2(self) -> bool:
        """v2 判定：显式 version=2 + semantic_digest pin + STALE 裁决。
        旧 draft/legacy ledger 永远走 raw-SHA fail-closed（不拿 v2 规则重释旧 snapshot）。"""
        pins = self.data.get("pins") or {}
        return (self.data.get("version") == 2
                and bool(pins.get("semantic_digest"))
                and (self.data.get("r04_adjudication") or {}).get("verdict")
                == "LEDGER_STALE_CONFIRMED")

    @staticmethod
    def _canon(obj) -> str:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def _entry_digest(cls, benchmark_id, sha256, dimensions, role) -> str:
        import hashlib
        payload = {"benchmark_id": benchmark_id, "sha256": sha256,
                   "dimensions": dimensions, "role": role}
        return hashlib.sha256(cls._canon(payload).encode("utf-8")).hexdigest()

    def _recompute_v2_semantic_digests(self, root: Path) -> dict:
        """按 v2 ledger 冻结的 semantic_digest_scheme 复算 sidecar 语义 digest。

        输入仅稳定语义字段（benchmark_id/sha256/dimensions/role）；generated_at/by、
        序列化顺序等 metadata 一概不入域——sidecar 合法重生成不应再造成误判。
        B7 13 条来自 benchmark sidecar（dimensions="WxH"、role 沿 sidecar 原词）；
        B9 55 条来自 fixed_benchmark_v2（dimensions=None、sealed_holdout 归一 sealed）。
        """
        import hashlib
        pins = self.data["pins"]
        sidecar = json.loads((root / pins["benchmark_sidecar"]).read_bytes().decode("utf-8"))
        b9_manifest = json.loads(
            (root / "materials/b9-domain-expansion/fixed_benchmark_v2.json")
            .read_bytes().decode("utf-8"))

        b7_entries = [
            {"benchmark_id": i["benchmark_id"], "sha256": str(i["sha256"]).lower(),
             "dimensions": f"{i['width']}x{i['height']}", "role": i["role"]}
            for i in sidecar["images"]]
        b9_entries = [
            {"benchmark_id": i["benchmark_id"], "sha256": str(i["sha256"]).lower(),
             "dimensions": None,
             "role": "sealed" if i.get("role") == "sealed_holdout" else i.get("role")}
            for i in b9_manifest["images"]]
        combined = sorted(b7_entries + b9_entries, key=lambda e: e["benchmark_id"])

        def dig(entries):
            return hashlib.sha256(
                self._canon(entries).encode("utf-8")).hexdigest()

        return {
            "b7_benchmark_sidecar_13": dig(b7_entries),
            "b9_benchmark_55": dig(b9_entries),
            "combined_68": dig(combined),
            "n_entries": len(combined),
        }

    def assert_r04_integrity(self, project_root=None) -> dict:
        """版本分派的数据源完整性断言（fail-closed）。

        - legacy/draft ledger：维持既有行为——sources[].sha256 逐个 raw 校验，
          sidecar 不豁免（E_LEDGER_SOURCE_MISMATCH）。
        - v2 ledger：source manifest sha + semantic digest 为硬 pin；
          benchmark sidecar 整文件 sha 按 promotion 语义降为 informational
          （记录漂移但不阻塞）；其余 metadata sources 维持 raw 硬校验。

        返回 integrity 报告 dict；任何硬校验失败 raise DatasetError。
        """
        root = Path(project_root) if project_root is not None else self._project_root()
        if not self.is_v2():
            self.assert_sources_valid(root)
            return {"strategy": "legacy_raw_sha", "ok": True}

        pins = self.data["pins"]
        report: dict = {"strategy": "v2_semantic", "ok": True,
                        "sidecar_informational": None, "hard_sources": []}

        # 3.1 source manifest：硬 pin
        sm_rel = str(pins.get("source_manifest", ""))
        sm_path = root / sm_rel
        if not sm_path.is_file():
            raise DatasetError("E_R04_SOURCE_MANIFEST_MISMATCH",
                               f"source manifest missing: {sm_rel}")
        sm_sha = sha256_bytes(sm_path.read_bytes())
        if sm_sha != str(pins["source_manifest_sha256"]).lower():
            raise DatasetError(
                "E_R04_SOURCE_MANIFEST_MISMATCH",
                f"fixed_benchmark source manifest drift: {sm_sha} != {pins['source_manifest_sha256']}")
        report["source_manifest"] = {"path": sm_rel, "sha256": sm_sha, "match": True}

        # 3.3 semantic digest：硬 pin（B7/B9 分域 + combined 全须一致）
        actual = self._recompute_v2_semantic_digests(root)
        expected = pins.get("semantic_digests") or {}
        mismatch = {k: {"expected": expected.get(k), "actual": actual.get(k)}
                    for k in expected
                    if str(actual.get(k)).lower() != str(expected.get(k)).lower()}
        if mismatch or actual["n_entries"] != 68:
            raise DatasetError(
                "E_R04_SEMANTIC_MISMATCH",
                "protected semantic set changed: " + json.dumps(mismatch or {"n_entries": actual["n_entries"]},
                                                                ensure_ascii=False))
        report["semantic_digest"] = {"combined_68": actual["combined_68"],
                                     "n_entries": actual["n_entries"], "match": True}

        # 3.2 benchmark sidecar：必须存在；raw sha = informational（不阻塞）
        sc_rel = str(pins.get("benchmark_sidecar", ""))
        sc_path = root / sc_rel
        if not sc_path.is_file():
            raise DatasetError("E_R04_SIDECAR_MISSING", f"benchmark sidecar missing: {sc_rel}")
        sc_sha = sha256_bytes(sc_path.read_bytes())
        sc_expected = str(pins.get("benchmark_sidecar_sha256_current", "")).lower()
        report["sidecar_informational"] = {
            "path": sc_rel, "sha256": sc_sha,
            "matches_last_recorded": sc_sha == sc_expected,
            "informational_only": True}

        # 其余 metadata sources：raw 硬校验（不含 sidecar 与 source manifest——已按 v2 语义处理）
        skip = {pins.get("benchmark_sidecar"), pins.get("source_manifest")}
        for s in self.data.get("sources", []) or []:
            rel = str(s.get("path", ""))
            if rel in skip:
                continue
            p = root / rel
            exists = p.is_file()
            sha_match = (sha256_bytes(p.read_bytes()) == str(s.get("sha256", "")).lower()
                         ) if exists and rel.lower().endswith((".json", ".jsonl")) else None
            entry = {"path": rel, "exists": exists, "sha_match": sha_match}
            report["hard_sources"].append(entry)
            if not exists or sha_match is False:
                raise DatasetError("E_LEDGER_SOURCE_MISMATCH",
                                   f"{rel}: " + ("missing" if not exists else "sha256 mismatch"))
        return report

    def describe(self) -> dict:
        return {
            "path": str(self.path), "sha256": self.sha256,
            "n_groups": len(self.data.get("groups", [])),
            "n_forbidden_hashes": len(self.forbidden_hashes),
            "n_unknown_hashes": len(self.unknown_hashes),
            "n_families": len(self.families),
            "family_sizes": {fid: len(m) for fid, m in sorted(self.families.items())},
        }
