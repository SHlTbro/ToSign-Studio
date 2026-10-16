# -*- coding: utf-8 -*-
"""S08 evaluator.receipt — 评估收据 schema、落盘与防篡改复核。

schema 版本：eval-receipt/1.0（字段语义变更必须升版本，防跨版本混比）。

【边界声明（写进每张收据的 boundaries 字段）】
- 本收据只含**自动结构 proxy 指标**，不产出质量 verdict；
  真人视觉判定属 S09 盲评域（hidden model、协议版本、reviewer 署名）。
- 自动评估允许 NEGATIVE / INCONCLUSIVE；`summary.n_pass_proxy` 不是质量通过数。

防篡改（卡 S08 DoD"原始预测冻结可独立重算"）：
- receipt.receipt_sha256 = sha256(canonical_json(receipt 去掉该字段))；
  任何字段（含逐字指标/汇总）被改 → verify_receipt 复算失败；
- receipt.files 覆盖输出 PNG / 生成收据 / 可视化包，逐文件 sha256；
  verify_receipt 同时复算文件内容 hash——预测被替换也必失败。
canonical_json 复用 dataset.schema 同一口径（排序键、紧凑分隔符）。
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from engine.style.dataset.schema import canonical_json, sha256_bytes  # 只读消费（同一序列化口径）
from engine.style.m1_adapter.receipts import sha256_file  # 只读消费

EVAL_RECEIPT_VERSION = "eval-receipt/1.0"

# 自动状态（不是质量 verdict；S08 DoD 允许 NEGATIVE/INCONCLUSIVE）
STATUS_OK = "OK"                        # 评估链路完成（指标可为负）
STATUS_INSUFFICIENT = "INSUFFICIENT_EVAL_SET"   # holdout 不足/为空——显式，不凑数
STATUS_FAILED = "EVAL_FAILED"           # 评估器内部错误（逐字明细里注明）

_BOUNDARIES = (
    "自动 proxy ≠ 真人视觉判定；本模块不产出质量 verdict（真人判定属 S09 盲评域）；"
    "summary.n_pass_proxy 不是质量通过数；自动评估允许 NEGATIVE/INCONCLUSIVE；"
    "原始预测（PNG+sha256）已冻结，可凭本收据独立重算指标"
)


def generation_completeness(receipt: dict, status: str | None = None) -> tuple[bool, list[str]]:
    """Return whether every requested item was generated successfully.

    This is a generation-integrity gate only; metric and human quality reviews
    remain separate. Callers may pass the evaluator's returned status to ensure
    it agrees with the immutable receipt.
    """
    reasons: list[str] = []
    if not isinstance(receipt, dict):
        return False, ["evaluation receipt is not an object"]

    declared_status = receipt.get("status")
    if declared_status != STATUS_OK:
        reasons.append(f"receipt status is {declared_status!r}, expected {STATUS_OK}")
    if status is not None and status != declared_status:
        reasons.append("returned evaluator status differs from receipt status")
    if status is not None and status != STATUS_OK:
        reasons.append(f"returned evaluator status is {status!r}, expected {STATUS_OK}")

    summary = receipt.get("summary")
    if not isinstance(summary, dict):
        return False, reasons + ["evaluation summary is missing"]
    targets = summary.get("n_targets")
    generated = summary.get("n_generated")
    errors = summary.get("n_gen_error")
    if type(targets) is not int or targets <= 0:
        reasons.append("target count must be a positive integer")
    if type(generated) is not int or generated < 0:
        reasons.append("generated count must be a nonnegative integer")
    elif type(targets) is int and generated != targets:
        reasons.append("generated count does not equal target count")
    if type(errors) is not int or errors != 0:
        reasons.append("generation error count must be zero")

    rows = receipt.get("per_char")
    if not isinstance(rows, list):
        reasons.append("per-character rows are missing")
    else:
        if type(targets) is int and len(rows) != targets:
            reasons.append("per-character row count does not equal target count")
        labels = [row.get("label") for row in rows if isinstance(row, dict)]
        if len(labels) != len(rows) or any(not isinstance(label, str) or not label for label in labels):
            reasons.append("every per-character row must identify its target")
        elif len(set(labels)) != len(labels):
            reasons.append("per-character target rows are not unique")
        if any(not isinstance(row, dict) or row.get("status") != "ok" for row in rows):
            reasons.append("every target must have a successful generation row")
    return not reasons, reasons


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def new_eval_id(stamp: str | None = None) -> str:
    return f"eval_{stamp or _dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"


def build_receipt(*, eval_id: str, status: str, evaluator_version: str,
                  metrics_version: str, config: dict, config_sha256: str,
                  inp: dict, engine: dict, per_char: list, summary: dict,
                  files: dict, notes: list | None = None) -> dict:
    """组装收据（不含 self-hash；落盘前走 finalize_receipt）。"""
    return {
        "schema_version": EVAL_RECEIPT_VERSION,
        "eval_id": eval_id,
        "status": status,                     # OK | INSUFFICIENT_EVAL_SET | EVAL_FAILED
        "created_at": _now(),
        "evaluator_version": evaluator_version,
        "metrics_version": metrics_version,
        "verdict": None,                      # 恒 None：质量 verdict 属 S09，本模块不产出
        "input": inp,                         # kind/snapshot hash/引用文件 hash/防泄漏检查
        "engine": engine,                     # engine_id/model_version/ckpt（来自逐字生成收据）
        "config": config,                     # 全部阈值显式（无隐藏参数）
        "config_sha256": config_sha256,
        "per_char": per_char,                 # 逐字明细：指标+失败分类账+生成收据引用
        "summary": summary,
        "files": files,                       # 相对路径 → sha256（预测/可视化/生成收据冻结）
        "notes": notes or [],
        "boundaries": _BOUNDARIES,
    }


def receipt_payload_sha256(receipt: dict) -> str:
    """canonical（去 self-hash）→ sha256。篡改任何字段复算必变。"""
    body = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
    return sha256_bytes(canonical_json(body).encode("utf-8"))


def finalize_receipt(receipt: dict) -> dict:
    """补 self-hash 字段（保存前的最后一步）。"""
    receipt["receipt_sha256"] = receipt_payload_sha256(receipt)
    return receipt


def save_receipt(receipt: dict, path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
    return receipt["receipt_sha256"]


def load_receipt(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def verify_receipt(receipt: dict) -> tuple[bool, list[str]]:
    """复算 self-hash + 逐文件 hash → (ok, 失败原因列表)。任何篡改 → False。

    files 表条目格式：{"path": 相对或绝对路径, "sha256": 期望值}。
    文件缺失/内容被换/hash 被改 → 失败；self-hash 字段本身被改 → 失败。
    """
    reasons: list[str] = []
    expected = receipt.get("receipt_sha256")
    if not expected:
        return False, ["missing receipt_sha256（未 finalize 或字段被删）"]
    actual = receipt_payload_sha256(receipt)
    if actual != expected:
        reasons.append(f"receipt_sha256 recompute {actual[:12]}… != declared "
                       f"{str(expected)[:12]}…（收据内容被篡改）")
    for name, meta in sorted((receipt.get("files") or {}).items()):
        p = Path(meta["path"])
        if not p.is_file():
            reasons.append(f"{name}: file missing: {meta['path']}")
            continue
        h = sha256_file(p)
        if h != meta["sha256"]:
            reasons.append(f"{name}: content sha {h[:12]}… != declared "
                           f"{meta['sha256'][:12]}…（预测/可视化文件被替换）")
    return (not reasons), reasons
