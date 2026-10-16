# -*- coding: utf-8 -*-
"""S03 dataset.snapshot — 训练集不可变快照（写后内容 hash 冻结，拒绝覆盖/篡改）。

布局（snapshot_dir/）：
  samples.jsonl   训练级行（canonical JSON Lines；剥离 alpha_path，补 snapshot_file）
  split.json      split 指派 + 计划元数据（无 split_config 时省略）
  samples/<sid>.png  规范化 alpha PNG 副本（内容级 sha256 复核）
  index.json      元数据 + content_sha256（覆盖 samples.jsonl/split.json/每个 PNG 的 hash）
  FROZEN          冻结标记（snapshot_id + content_sha256）

不可变语义：
- 目标目录非空 → 拒绝构建（E_SNAPSHOT_EXISTS），永不原地改写；
- 写入后立刻 chmod 只读（尽力而为），verify_snapshot 对任何篡改复算 hash 必失败；
- index 不含自身 hash，content_sha256 只覆盖数据内容（含 created_at 无关），同一输入
  重建 content_sha256 相同 → "正例可复算 index"。
- 训练启动只读本目录，不读变化中的 P4 活动目录（卡 S03）。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .ledger import LedgerIndex
from .schema import DatasetError, canonical_json, sha256_bytes, validate_row
from .split import SplitConfig, SplitPlan, plan_splits
from .validator import ValidationResult, validate_pool

DATASET_TOOL_VERSION = "engine/style/dataset/0.1.0"

SAMPLES_JSONL = "samples.jsonl"
SPLIT_JSON = "split.json"
SAMPLES_DIR = "samples"
INDEX_JSON = "index.json"
FROZEN_MARKER = "FROZEN"
PROVENANCE_JSON = "provenance.json"


@dataclass
class BuildResult:
    status: str                                # OK | INSUFFICIENT_DATA
    snapshot_dir: str = ""
    snapshot_id: str = ""
    snapshot_sha256: str = ""                  # = content_sha256
    index: dict = field(default_factory=dict)
    plan: SplitPlan | None = None
    validation: ValidationResult | None = None
    insufficient_reasons: list = field(default_factory=list)


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _content_hash(samples_bytes: bytes, split_bytes: bytes | None, file_hashes: dict,
                  provenance_bytes: bytes | None = None) -> str:
    payload = {
        "samples_sha256": sha256_bytes(samples_bytes),
        "split_sha256": sha256_bytes(split_bytes) if split_bytes is not None else None,
        "files": dict(sorted(file_hashes.items())),
        "format": 2 if provenance_bytes is not None else 1,
    }
    if provenance_bytes is not None:
        payload["provenance_sha256"] = sha256_bytes(provenance_bytes)
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


def _make_readonly(path: Path):
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass  # Windows 只读属性尽力而为；篡改检测由 hash 复算兜底


def unlock_snapshot(snapshot_dir):
    """恢复写权限（测试清理/受控迁移用；正常流程无人调用）。"""
    root = Path(snapshot_dir)
    if not root.exists():
        return
    for p in [root, *root.rglob("*")]:
        if p.is_file():
            try:
                os.chmod(p, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
            except OSError:
                pass


def build_snapshot(rows: list[dict], out_dir, *, ledger: LedgerIndex,
                   split_config: SplitConfig | None = None,
                   created_at: str | None = None,
                   provenance_metadata: dict | None = None) -> BuildResult:
    """构建不可变快照。

    流程：池级 production 校验（含 R04 台账禁入）→ alpha PNG 复制+hash 复核 →
    split 计划（组原子）→ 分配后整体校验 → 写盘冻结 → 自校验。
    硬失败 → DatasetError；数据门不足 → status=INSUFFICIENT_DATA（不写任何文件）。
    """
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise DatasetError("E_SNAPSHOT_EXISTS",
                           f"refuse to build into non-empty dir: {out_dir}（快照不可覆盖）")

    # A source ledger can only enforce forbidden inputs when its declared
    # metadata sources still match the current files. Binary/sealed materials
    # are not opened by this verification.
    ledger.assert_sources_valid()

    # 1) 行规范化 + 池级硬校验（跨用户/重复 alpha/台账禁入/UNKNOWN 策略）
    policy = split_config.unknown_parent_policy if split_config else "allow_with_provenance"
    clean = [validate_row(r) for r in rows]          # 单行结构错误 → 明确异常
    pre = validate_pool(clean, splits=None, ledger=ledger, unknown_parent_policy=policy)
    pre.raise_if_bad()

    # 2) alpha PNG 内容级复核 + 复制
    png_hashes: dict[str, str] = {}
    png_bytes_map: dict[str, bytes] = {}
    for r in clean:
        ap = r.get("alpha_path")
        if not ap:
            raise DatasetError("E_ALPHA_MISSING",
                               f"sample {r['sample_id']} has no alpha_path（无法做内容级断言）")
        p = Path(ap)
        if not p.is_file():
            raise DatasetError("E_ALPHA_MISSING",
                               f"sample {r['sample_id']} alpha PNG missing: {p}")
        data = p.read_bytes()
        actual = sha256_bytes(data)
        if actual != r["final_alpha_sha256"].lower():
            raise DatasetError("E_ALPHA_HASH_MISMATCH",
                               f"sample {r['sample_id']} alpha content sha {actual[:12]}… != declared "
                               f"{str(r['final_alpha_sha256'])[:12]}…（hash 不匹配/文件被换）")
        png_hashes[r["sample_id"]] = actual
        png_bytes_map[r["sample_id"]] = data

    # 3) split 计划（组原子；INSUFFICIENT_DATA 在写盘前返回）
    plan: SplitPlan | None = None
    splits: dict[str, str] | None = None
    if split_config is not None:
        plan = plan_splits(clean, ledger, split_config)
        if plan.status == "FAIL":
            raise DatasetError("E_SPLIT_PLAN_FAILED",
                               "; ".join(plan.reasons[:5]) or "split plan failed")
        if plan.status == "INSUFFICIENT_DATA":
            return BuildResult(status="INSUFFICIENT_DATA", plan=plan,
                               insufficient_reasons=list(plan.reasons))
        splits = dict(plan.assignments)

    # 4) 写盘
    stored_rows = []
    for r in sorted(clean, key=lambda x: x["sample_id"]):
        row = {k: v for k, v in r.items() if k != "alpha_path"}
        row["snapshot_file"] = f"{SAMPLES_DIR}/{r['sample_id']}.png"
        stored_rows.append(row)
    samples_bytes = ("\n".join(canonical_json(r) for r in stored_rows) + "\n").encode("utf-8")

    split_bytes = None
    if splits is not None:
        split_payload = {
            "assignments": splits,
            "plan": {"status": plan.status, "seed": split_config.seed,
                     "train_ratio": split_config.train_ratio,
                     "unknown_parent_policy": policy,
                     "components": plan.components, "stats": plan.stats},
        }
        split_bytes = (canonical_json(split_payload) + "\n").encode("utf-8")

    provenance_bytes = None
    if provenance_metadata is not None:
        if not isinstance(provenance_metadata, dict):
            raise DatasetError("E_PROVENANCE_METADATA", "provenance_metadata must be an object")
        logical_id = provenance_metadata.get("snapshot_id")
        if not isinstance(logical_id, str) or not logical_id.strip():
            raise DatasetError("E_PROVENANCE_METADATA", "snapshot_id is required in provenance_metadata")
        provenance_bytes = (canonical_json(provenance_metadata) + "\n").encode("utf-8")

    out_dir.mkdir(parents=True)
    (out_dir / SAMPLES_DIR).mkdir()
    (out_dir / SAMPLES_JSONL).write_bytes(samples_bytes)
    for sid, data in png_bytes_map.items():
        (out_dir / SAMPLES_DIR / f"{sid}.png").write_bytes(data)
    if split_bytes is not None:
        (out_dir / SPLIT_JSON).write_bytes(split_bytes)

    content_sha = _content_hash(samples_bytes, split_bytes, png_hashes, provenance_bytes)
    logical_id = (provenance_metadata or {}).get("snapshot_id", content_sha[:16])
    index = {
        "tool_version": DATASET_TOOL_VERSION,
        "snapshot_id": logical_id,
        "snapshot_sha256": content_sha,
        "created_at": created_at or _now(),
        "style_id": sorted({r["style_id"] for r in clean})[0],
        "n_samples": len(clean),
        "ledger_path": str(ledger.path),
        "ledger_sha256": ledger.sha256,
        "unknown_parent_policy": policy,
        "has_split": split_bytes is not None,
        "counts": plan.stats if plan is not None else pre.stats,
        "files": {sid: {"path": f"{SAMPLES_DIR}/{sid}.png", "sha256": h}
                  for sid, h in sorted(png_hashes.items())},
        "content_sha256": content_sha,
    }
    if provenance_bytes is not None:
        index["provenance_file"] = PROVENANCE_JSON
        index["provenance_sha256"] = sha256_bytes(provenance_bytes)
    (out_dir / INDEX_JSON).write_bytes((canonical_json(index) + "\n").encode("utf-8"))
    if provenance_bytes is not None:
        (out_dir / PROVENANCE_JSON).write_bytes(provenance_bytes)
    (out_dir / FROZEN_MARKER).write_bytes(
        f"FROZEN {index['snapshot_id']} {content_sha}\n".encode("utf-8"))

    # 5) 冻结（只读尽力而为 + 逐文件）
    _make_readonly(out_dir / SAMPLES_JSONL)
    if split_bytes is not None:
        _make_readonly(out_dir / SPLIT_JSON)
    for sid in png_bytes_map:
        _make_readonly(out_dir / SAMPLES_DIR / f"{sid}.png")
    _make_readonly(out_dir / INDEX_JSON)
    if provenance_bytes is not None:
        _make_readonly(out_dir / PROVENANCE_JSON)
    _make_readonly(out_dir / FROZEN_MARKER)

    # 6) 自校验（正例路径：同一 production verify 必须立即通过）
    res = verify_snapshot(out_dir, ledger=ledger)
    if res.status != "OK":
        raise DatasetError("E_SNAPSHOT_SELF_CHECK", f"fresh snapshot failed self-check: "
                                                    f"{[f.code for f in res.failures]}")
    return BuildResult(status="OK", snapshot_dir=str(out_dir),
                       snapshot_id=index["snapshot_id"], snapshot_sha256=content_sha,
                       index=index, plan=plan, validation=plan.validation if plan else pre)


def load_snapshot(snapshot_dir):
    """读取快照 → (rows, index, splits|None)。只读；不校验（校验走 verify/validate）。"""
    root = Path(snapshot_dir)
    idx_path = root / INDEX_JSON
    if not idx_path.is_file():
        raise DatasetError("E_INDEX_MISSING", f"no {INDEX_JSON} in {root}")
    index = json.loads(idx_path.read_bytes().decode("utf-8"))
    rows = []
    sj = root / SAMPLES_JSONL
    if not sj.is_file():
        raise DatasetError("E_SNAPSHOT_FILE_MISSING", f"no {SAMPLES_JSONL} in {root}")
    for line in sj.read_bytes().decode("utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    splits = None
    sp = root / SPLIT_JSON
    if sp.is_file():
        splits = json.loads(sp.read_bytes().decode("utf-8"))["assignments"]
    return rows, index, splits


def verify_snapshot(snapshot_dir, ledger: LedgerIndex | None = None) -> ValidationResult:
    """完整性复核：FROZEN 标记、逐文件 hash、content_sha256 复算、台账变更检测。

    成功 → ValidationResult(OK) 且 stats["loaded"] = (rows, index, splits)。
    任何篡改 → ValidationResult(FAIL, failures=[...])，不抛异常（供 validator 聚合）。
    """
    from .validator import Failure  # 局部 import 避免环
    root = Path(snapshot_dir)
    failures: list[Failure] = []
    if not root.is_dir():
        return ValidationResult("FAIL", [Failure("E_SNAPSHOT_MISSING", f"not a dir: {root}")])
    if not (root / FROZEN_MARKER).is_file():
        failures.append(Failure("E_NOT_FROZEN", f"missing {FROZEN_MARKER} marker"))
    try:
        rows, index, splits = load_snapshot(root)
    except DatasetError as e:
        return ValidationResult("FAIL", [Failure(e.code, str(e))])

    # 逐文件 hash
    for sid, meta in sorted((index.get("files") or {}).items()):
        f = root / meta["path"]
        if not f.is_file():
            failures.append(Failure("E_SNAPSHOT_FILE_MISSING",
                                    f"{meta['path']} missing", [sid]))
            continue
        actual = sha256_bytes(f.read_bytes())
        if actual != meta["sha256"]:
            failures.append(Failure("E_FILE_HASH_MISMATCH",
                                    f"{meta['path']} content {actual[:12]}… != index "
                                    f"{meta['sha256'][:12]}…（快照后被篡改/替换）", [sid]))

    # content hash 复算（覆盖 samples.jsonl / split.json / files 表）
    sj = root / SAMPLES_JSONL
    sp = root / SPLIT_JSON
    provenance_bytes = None
    provenance_name = index.get("provenance_file")
    if provenance_name:
        if provenance_name != PROVENANCE_JSON:
            failures.append(Failure("E_PROVENANCE_PATH", f"unexpected provenance path: {provenance_name}"))
        provenance_path = root / PROVENANCE_JSON
        if not provenance_path.is_file():
            failures.append(Failure("E_SNAPSHOT_FILE_MISSING", f"{PROVENANCE_JSON} missing"))
        else:
            provenance_bytes = provenance_path.read_bytes()
            actual_provenance_sha = sha256_bytes(provenance_bytes)
            if actual_provenance_sha != index.get("provenance_sha256"):
                failures.append(Failure("E_PROVENANCE_HASH_MISMATCH", "provenance.json hash differs from index"))
            try:
                provenance_obj = json.loads(provenance_bytes.decode("utf-8"))
                if provenance_obj.get("snapshot_id") != index.get("snapshot_id"):
                    failures.append(Failure("E_PROVENANCE_SNAPSHOT_ID", "provenance snapshot_id differs from index"))
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                failures.append(Failure("E_PROVENANCE_INVALID", "provenance.json is not an object JSON document"))
    recomputed = _content_hash(
        sj.read_bytes() if sj.is_file() else b"",
        sp.read_bytes() if sp.is_file() else None,
        {sid: m["sha256"] for sid, m in (index.get("files") or {}).items()},
        provenance_bytes)
    if recomputed != index.get("content_sha256"):
        failures.append(Failure("E_INDEX_MISMATCH",
                                f"content_sha256 recompute {recomputed[:12]}… != index "
                                f"{str(index.get('content_sha256'))[:12]}…"
                                "（index/清单被篡改）"))

    # 台账变更检测：台账换了 → 旧快照禁入结论可能翻转，必须重审
    if ledger is not None and index.get("ledger_sha256") not in (None, ledger.sha256):
        failures.append(Failure("E_LEDGER_CHANGED",
                                f"snapshot frozen against ledger {str(index.get('ledger_sha256'))[:12]}… "
                                f"but current ledger is {ledger.sha256[:12]}…（台账已变更，需重建快照）"))

    res = ValidationResult("FAIL", failures) if failures else ValidationResult("OK", [])
    res.stats = {"loaded": (rows, index, splits), "snapshot_id": index.get("snapshot_id")}
    return res
