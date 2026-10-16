# -*- coding: utf-8 -*-
"""S03 dataset.schema — 训练级样本行校验（输入 = S02 charprep 导出的 manifest 行）。

对齐点（重要，S02 charprep 由并行 Agent 开发中）：
- 本模块**不 import** engine/style/charprep（避免耦合在飞模块），只按其已落盘的
  ``engine/style/charprep/manifest.py``（2026-09-27 版，PREP_VERSION=charprep/1.0.0）
  导出行 schema 消费：sample_id/style_id/label/unicode_cp/parent_source_hash/
  source_bbox/final_alpha_sha256/prep_version/review_status/parent_width/
  parent_height/created_at(/source_ref/confirmed_by/confirmed_at/notes/codepoint_u)。
- charprep 导出行**不含** alpha PNG 的文件路径与来源声明。dataset 侧扩展两个可选键：
  * ``alpha_path``     —— 规范化 PNG 的路径（快照复制 + 内容 hash 复核用；存储时剥离）；
  * ``parent_origin``  —— 父图来源声明。台账未列出的新父 hash 默认按全局规则
    "未知历史=UNKNOWN" 禁入；仅当声明为 ``user_upload_confirmed``（S02 确认链可回溯）
    才允许进入训练池。该键语义需 G03/Owner 复核（对齐点，见 TaskResult）。
- 若 charprep schema 后续变更，以 charprep.manifest 为准，本模块相应字段集需同步。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata

REVIEW_CONFIRMED = "owner_confirmed"
REVIEW_AI_CONFIRMED = "ai_confirmed_authorized"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Human owner confirmation remains valid; explicit AI confirmation additionally
# requires the full self-contained review_provenance receipt below.
REQUIRED_FIELDS = (
    "sample_id", "style_id", "label", "unicode_cp",
    "parent_source_hash", "final_alpha_sha256",
    "review_status", "prep_version",
)
# dataset 侧扩展键（不属 charprep schema，语义见模块 docstring 对齐点）
EXTENSION_KEYS = ("alpha_path", "parent_origin", "review_provenance")
KNOWN_KEYS = frozenset({
    "sample_id", "style_id", "label", "unicode_cp",
    "parent_source_hash", "source_bbox", "final_alpha_sha256",
    "prep_version", "review_status", "parent_width", "parent_height",
    "created_at", "source_ref", "confirmed_by", "confirmed_at", "notes",
    "codepoint_u", "snapshot_file",
}) | set(EXTENSION_KEYS)

# 禁用码点区（与 charprep.manifest 对齐：变体选择子/代理区/控制符）
_FORBIDDEN_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF), (0xD800, 0xDFFF), (0x0000, 0x001F))


class DatasetError(Exception):
    """S03 数据集模块统一错误；code 为机器可读失败码。"""

    def __init__(self, code: str, msg: str):
        super().__init__(f"[{code}] {msg}")
        self.code = code


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj) -> str:
    """规范化 JSON（排序键、紧凑分隔符）——内容 hash 的唯一序列化口径。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def validate_row(row: dict) -> dict:
    """校验一条训练级行 → 返回规范化 dict（保留已知字段 + 扩展键，剔除未知键）。

    任何不合规 → DatasetError（code 见下）。与 validator 的聚合校验（同 family 跨
    split 等）分工：本函数只做**单行**结构/内容校验。
    """
    if not isinstance(row, dict):
        raise DatasetError("E_ROW_TYPE", f"row must be dict, got {type(row).__name__}")

    missing = [k for k in REQUIRED_FIELDS if row.get(k) in (None, "")]
    if missing:
        raise DatasetError("E_ROW_MISSING_FIELD", f"missing required fields {missing}: keys={sorted(row)}")

    unknown = sorted(set(row) - KNOWN_KEYS)
    if unknown:
        raise DatasetError("E_ROW_UNKNOWN_FIELD", f"unknown fields {unknown}（防拼写错误混入）")

    if row["review_status"] not in (REVIEW_CONFIRMED, REVIEW_AI_CONFIRMED):
        raise DatasetError(
            "E_ROW_NOT_CONFIRMED",
            f"review_status={row['review_status']!r} 不是训练级（candidate/uncertain 不可进入训练导出）")
    if row["review_status"] == REVIEW_AI_CONFIRMED:
        validate_ai_confirmation(row)

    for key in ("parent_source_hash", "final_alpha_sha256"):
        if not _SHA256_RE.match(str(row[key])):
            raise DatasetError("E_ROW_BAD_HASH", f"{key} must be 64-hex sha256, got {row[key]!r}")

    label = row["label"]
    if not isinstance(label, str) or len(label) != 1:
        raise DatasetError("E_ROW_LABEL_LEN", f"label must be exactly 1 char, got {label!r}")
    try:
        cp = ord(label)
    except TypeError as e:  # pragma: no cover
        raise DatasetError("E_ROW_LABEL_LEN", f"label not a char: {label!r}") from e
    if int(row["unicode_cp"]) != cp:
        raise DatasetError(
            "E_ROW_CODEPOINT_MISMATCH",
            f"declared U+{int(row['unicode_cp']):04X} != ord(label)=U+{cp:04X}")
    if unicodedata.combining(label) != 0:
        raise DatasetError("E_ROW_COMBINING", f"combining char U+{cp:04X} not allowed as label")
    for lo, hi in _FORBIDDEN_RANGES:
        if lo <= cp <= hi:
            raise DatasetError("E_ROW_CODEPOINT_FORBIDDEN", f"U+{cp:04X} in forbidden range {lo:04X}-{hi:04X}")

    if not str(row["sample_id"]).strip():
        raise DatasetError("E_ROW_SAMPLE_ID", "sample_id must be non-empty")
    if not str(row["style_id"]).strip():
        raise DatasetError("E_ROW_STYLE_ID", "style_id must be non-empty（多博主/风格不混池）")

    out = {k: row[k] for k in row if k in KNOWN_KEYS}
    origin = out.get("parent_origin")
    if origin is not None and origin not in ("user_upload_confirmed",):
        raise DatasetError(
            "E_ROW_BAD_ORIGIN",
            f"parent_origin={origin!r} not recognized（仅 user_upload_confirmed 可豁免 UNKNOWN 禁入）")
    return out


def review_digest(value: dict) -> str:
    """Hash a JSON evidence object; reject non-finite/non-JSON values."""
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return sha256_bytes(raw.encode("utf-8"))


def validate_candidate_response(response: dict, binding: dict) -> None:
    """Evidence completeness only. Acceptance additionally checks every rubric."""
    def require(ok, message):
        if not ok:
            raise ValueError(message)
    require(isinstance(response, dict), "pixel-viewing response missing")
    for key in ("image_sha256", "source_image_sha256", "source_bbox_norm", "label"):
        require(response.get(key) == binding.get(key), f"AI {key} does not bind the candidate/source")
    require(response.get("pixels_inspected") is True and response.get("source_pixels_inspected") is True,
            "both final glyph and source context must be visually inspected")
    require(response.get("reviewer_kind") == "vision_model", "vision reviewer provenance required")
    for key in ("provider", "model_id", "reason", "label_rationale", "crop_rationale", "contamination_rationale"):
        require(isinstance(response.get(key), str) and bool(response[key].strip()), f"{key} required")
    require(response["provider"] != "TEST_ONLY" and "fixture" not in response["model_id"].lower(),
            "test fixture evidence is not production review")
    for key in ("label_correct", "crop_complete", "contamination_free", "uncertain"):
        require(type(response.get(key)) is bool, f"explicit boolean {key} required")
    for key in ("image_quality", "glyph_presence", "background_contamination", "split_quality"):
        require(response.get(key) in ("PASS", "FAIL", "UNCERTAIN"), f"explicit {key} rubric required")
    confidence = response.get("confidence")
    require(isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            and math.isfinite(confidence) and 0 <= confidence <= 1, "finite confidence in 0..1 required")
    require(response.get("recommendation") in ("AI_ACCEPT", "AI_REJECT", "AI_NEEDS_HUMAN"), "invalid recommendation")
    resolved = response.get("resolved_structural_suspicions")
    require(isinstance(resolved, list) and all(isinstance(item, str) for item in resolved),
            "explicit structural-suspicion disposition required")


def candidate_acceptance_reasons(response: dict, checks: dict, minimum_confidence: float) -> list[str]:
    """Recompute the decision; a claimed AI_ACCEPT cannot waive uncertainty."""
    if (isinstance(minimum_confidence, bool) or not isinstance(minimum_confidence, (int, float))
            or not math.isfinite(minimum_confidence) or not 0.9 <= minimum_confidence <= 1):
        raise ValueError("authorization requires minimum confidence in 0.9..1")
    reasons = list(checks.get("clear_errors", []))
    if response["recommendation"] != "AI_ACCEPT":
        reasons.append(response["recommendation"])
    if response["confidence"] < minimum_confidence:
        reasons.append("LOW_CONFIDENCE")
    for key in ("label_correct", "crop_complete", "contamination_free"):
        if response[key] is not True:
            reasons.append(key.upper() + "_NOT_ESTABLISHED")
    if response["uncertain"]:
        reasons.append("AI_UNCERTAIN")
    for key in ("image_quality", "glyph_presence", "background_contamination", "split_quality"):
        if response[key] != "PASS":
            reasons.append(key.upper() + "_" + response[key])
    unresolved = set(checks.get("suspicious_reasons", [])) - set(response["resolved_structural_suspicions"])
    reasons.extend(sorted(unresolved))
    return reasons


def validate_ai_confirmation(row: dict) -> dict:
    """Validate self-contained immutable AI provenance without relabeling history.

    Active policy and canonical DB authority are checked by the exporter. This
    shape check does not read mutable policy when verifying old snapshots.
    """
    try:
        proof = row.get("review_provenance")
        if not isinstance(proof, dict) or proof.get("schema") != "ai-candidate-confirmation/1":
            raise ValueError("AI confirmation provenance missing")
        digest = proof.get("receipt_sha256")
        if digest != review_digest({k: v for k, v in proof.items() if k != "receipt_sha256"}):
            raise ValueError("AI confirmation checksum mismatch")
        if proof.get("review_status") != REVIEW_AI_CONFIRMED or proof.get("actor_kind") != "ai":
            raise ValueError("AI actor/status missing")
        if proof.get("project") != "dyYY" or proof.get("personal_noncommercial") is not True:
            raise ValueError("AI confirmation exceeds project scope")
        if type(proof.get("user_id")) is not int or proof["user_id"] < 1:
            raise ValueError("AI confirmation lacks user scope")
        for key in ("policy_sha256", "review_response_sha256"):
            if not isinstance(proof.get(key), str) or not _SHA256_RE.fullmatch(proof[key]):
                raise ValueError(f"invalid {key}")
        authority = proof.get("authorization")
        if not isinstance(authority, dict) or not all(isinstance(authority.get(k), str) and authority[k].strip()
                for k in ("message_id", "created_at", "user_statement")):
            raise ValueError("explicit user authorization missing")
        if proof.get("sample_id") != row["sample_id"] or str(proof.get("style_id")) != str(row["style_id"]):
            raise ValueError("AI confirmation belongs to another sample/style")
        binding = proof["binding"]
        if (binding.get("image_sha256") != row["final_alpha_sha256"]
                or binding.get("source_image_sha256") != row["parent_source_hash"]
                or binding.get("label") != row["label"]):
            raise ValueError("AI confirmation image/source/label mismatch")
        box = binding.get("source_bbox_norm")
        if not isinstance(box, dict) or set(box) != {"x_norm", "y_norm", "w_norm", "h_norm"}:
            raise ValueError("AI source crop binding missing")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in box.values()):
            raise ValueError("AI source crop must be finite")
        x,y,w,h = (box[k] for k in ("x_norm", "y_norm", "w_norm", "h_norm"))
        if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1 and x+w <= 1 and y+h <= 1):
            raise ValueError("AI source crop outside image")
        if "source_bbox" in row:
            pixel_box = row["source_bbox"]
            width, height = row.get("parent_width"), row.get("parent_height")
            if (not isinstance(pixel_box, dict) or pixel_box.get("coord_space") != "pixel:parent_image"
                    or type(width) is not int or type(height) is not int or min(width, height) <= 0):
                raise ValueError("AI source crop lacks parent dimensions/coordinate space")
            expected_box = {"x": x*width, "y": y*height, "w": w*width, "h": h*height}
            if any(isinstance(pixel_box.get(k), bool) or not isinstance(pixel_box.get(k), (int, float))
                   or not math.isclose(pixel_box[k], value, rel_tol=0, abs_tol=1e-8) for k, value in expected_box.items()):
                raise ValueError("AI reviewed crop differs from manifest source crop")
        response = proof["review_response"]
        validate_candidate_response(response, binding)
        if review_digest(response) != proof["review_response_sha256"]:
            raise ValueError("raw AI response checksum mismatch")
        reviewer = {k: response[k] for k in ("provider", "model_id", "reviewer_kind")}
        if proof.get("reviewer") != reviewer or proof.get("actor") != f'ai:{response["provider"]}:{response["model_id"]}':
            raise ValueError("AI reviewer/actor mismatch")
        if row.get("confirmed_by") != proof["actor"] or row.get("confirmed_at") != proof.get("reviewed_at") or not proof.get("reviewed_at"):
            raise ValueError("AI confirmation stamp mismatch")
        checks = proof["structural_checks"]
        if not isinstance(checks, dict) or checks.get("image_sha256") != binding["image_sha256"]:
            raise ValueError("structural check image binding missing")
        if not isinstance(checks.get("clear_errors"), list) or not isinstance(checks.get("suspicious_reasons"), list):
            raise ValueError("structural check findings missing")
        reasons = candidate_acceptance_reasons(response, checks, proof["minimum_confidence"])
        if reasons or proof.get("isolation_reasons") != []:
            raise ValueError("AI confirmation has unresolved visual problems: " + ", ".join(reasons))
        return proof
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DatasetError("E_ROW_AI_CONFIRMATION", str(exc)) from exc
