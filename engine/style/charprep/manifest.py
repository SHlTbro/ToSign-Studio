# -*- coding: utf-8 -*-
"""S02 charprep.manifest — 训练级单字样本登记 schema 与导出门。

每条样本可回溯：parent_source_hash（原图/字块文件 sha256）、source_bbox（原坐标系 +
coord_space）、label/unicode 码点（严格一致）、final_alpha_sha256（normalize 输出 PNG）、
prep_version、review_status ∈ {candidate, uncertain, owner_confirmed}。

硬规则（模块层强制，不靠调用方自觉）：
1. register() 永不接受 owner_confirmed —— 初始状态只能是 candidate/uncertain；
   唯一晋升路径 confirm(sample_id, confirm_by=<非空署名>)（CLI --confirm-by 透传）。
2. export_training(style_id) 只导出该 style_id 的 owner_confirmed；一条没有 → 拒绝
   （E_EMPTY_EXPORT）。style_id 必填 → 多博主/风格结构上不可能混池导出。
3. 码点严格一致：ord(label) == unicode_cp；组合附加符（unicodedata.combining≠0）、
   变体选择子 U+FE00-FE0F / U+E0100-E01EF、代理区、控制符 → 拒绝。
4. bbox 跨坐标系拒绝：必须带 coord_space，且 bbox 必须落在声明 parent 尺寸内。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import unicodedata
from dataclasses import dataclass, asdict

from . import PREP_VERSION

REVIEW_CANDIDATE = "candidate"
REVIEW_UNCERTAIN = "uncertain"
REVIEW_CONFIRMED = "owner_confirmed"
REGISTERABLE_STATUSES = (REVIEW_CANDIDATE, REVIEW_UNCERTAIN)  # register 无 confirmed 通道

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COORD_SPACE_RE = re.compile(r"^pixel:[A-Za-z0-9_.\-]+$")  # 例 pixel:parent_image
# 禁用码点区：变体选择子 + 代理区 + CJK 兼容补充说明性符号不在训练 label 语义内
_FORBIDDEN_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF), (0xD800, 0xDFFF), (0x0000, 0x001F))


class ManifestError(ValueError):
    def __init__(self, code: str, msg: str):
        super().__init__(f"[{code}] {msg}")
        self.code = code


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def validate_label(label: str, unicode_cp: int) -> str:
    """label/码点联合校验 → 返回规范化 label。任何不一致拒绝。"""
    if not isinstance(label, str) or len(label) != 1:
        raise ManifestError("E_LABEL_LEN", f"label must be exactly 1 char, got {label!r}")
    cp = ord(label)
    if not isinstance(unicode_cp, int) or unicode_cp != cp:
        raise ManifestError(
            "E_CODEPOINT_MISMATCH",
            f"declared U+{unicode_cp:04X} != ord(label)=U+{cp:04X}（码点必须与 label 严格一致）")
    if unicodedata.combining(label) != 0:
        raise ManifestError("E_COMBINING_REJECTED", f"combining char U+{cp:04X} not allowed as label")
    for lo, hi in _FORBIDDEN_RANGES:
        if lo <= cp <= hi:
            raise ManifestError("E_CODEPOINT_FORBIDDEN", f"U+{cp:04X} in forbidden range {lo:04X}-{hi:04X}")
    if unicodedata.category(label) in ("Cf", "Cc", "Cs", "Co"):
        raise ManifestError("E_CODEPOINT_FORBIDDEN", f"category {unicodedata.category(label)} not allowed")
    return label


def validate_bbox(bbox: dict, parent_width: int, parent_height: int) -> dict:
    """bbox 必须带 coord_space 且完全落在 parent 尺寸内（跨坐标系/越界拒绝）。"""
    if not isinstance(bbox, dict):
        raise ManifestError("E_BBOX_TYPE", "source_bbox must be dict {x,y,w,h,coord_space}")
    space = bbox.get("coord_space")
    if not space or not COORD_SPACE_RE.match(str(space)):
        raise ManifestError("E_BBOX_COORD_SPACE", f"missing/illegal coord_space: {space!r}")
    try:
        x, y, w, h = (int(bbox[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError) as e:
        raise ManifestError("E_BBOX_SHAPE", f"bbox needs int x/y/w/h: {e!r}") from e
    if w <= 0 or h <= 0:
        raise ManifestError("E_BBOX_SHAPE", f"non-positive bbox size {w}x{h}")
    if x < 0 or y < 0 or x + w > parent_width or y + h > parent_height:
        raise ManifestError(
            "E_BBOX_OUT_OF_PARENT",
            f"bbox ({x},{y},{w},{h}) escapes declared parent {parent_width}x{parent_height}"
            "（跨坐标系/越界拒绝）")
    return {"x": x, "y": y, "w": w, "h": h, "coord_space": str(space)}


@dataclass
class Sample:
    sample_id: str
    style_id: str
    label: str
    unicode_cp: int
    parent_source_hash: str
    source_bbox: dict
    final_alpha_sha256: str
    prep_version: str
    review_status: str
    parent_width: int
    parent_height: int
    created_at: str
    source_ref: str = ""
    confirmed_by: str = ""
    confirmed_at: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["codepoint_u"] = f"U+{self.unicode_cp:04X}"
        return d


class Manifest:
    """内存态 manifest + JSON 文件持久化。JSON 顶层：{"prep_version", "samples": [...]}。"""

    def __init__(self, samples: list[Sample] | None = None):
        self.samples: list[Sample] = list(samples or [])
        self._seq = len(self.samples)

    # ---- 持久化 ----
    def save(self, path) -> None:
        payload = {"prep_version": PREP_VERSION, "samples": [s.to_dict() for s in self.samples]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)

    @classmethod
    def load(cls, path) -> "Manifest":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        out = []
        for d in data.get("samples", []):
            d = dict(d)
            d.pop("codepoint_u", None)
            out.append(Sample(**d))
        return cls(out)

    # ---- 登记（永不自动定案） ----
    def register(self, *, style_id: str, label: str, unicode_cp: int,
                 parent_source_hash: str, source_bbox: dict,
                 parent_width: int, parent_height: int,
                 final_alpha_sha256: str, review_status: str = REVIEW_CANDIDATE,
                 source_ref: str = "", notes: str = "") -> Sample:
        if not style_id or not str(style_id).strip():
            raise ManifestError("E_STYLE_ID", "style_id required（多博主/风格不混池）")
        if review_status not in REGISTERABLE_STATUSES:
            raise ManifestError(
                "E_REGISTER_STATUS",
                f"register only accepts {REGISTERABLE_STATUSES}; "
                f"owner_confirmed 需显式 confirm(confirm_by=...)，模块不自动定案")
        if not _SHA256_RE.match(parent_source_hash or ""):
            raise ManifestError("E_PARENT_HASH", "parent_source_hash must be 64-hex sha256")
        if not _SHA256_RE.match(final_alpha_sha256 or ""):
            raise ManifestError("E_ALPHA_HASH", "final_alpha_sha256 must be 64-hex sha256")
        label = validate_label(label, unicode_cp)
        bbox = validate_bbox(source_bbox, parent_width, parent_height)
        dup = [s for s in self.samples
               if s.style_id == style_id and s.label == label
               and s.final_alpha_sha256 == final_alpha_sha256]
        if dup:
            raise ManifestError("E_DUPLICATE", f"duplicate sample {style_id}/{label}/{final_alpha_sha256[:8]}")
        self._seq += 1
        s = Sample(sample_id=f"ss_{self._seq:06d}", style_id=str(style_id), label=label,
                   unicode_cp=ord(label), parent_source_hash=parent_source_hash,
                   source_bbox=bbox, final_alpha_sha256=final_alpha_sha256,
                   prep_version=PREP_VERSION, review_status=review_status,
                   parent_width=parent_width, parent_height=parent_height,
                   created_at=_now(), source_ref=source_ref, notes=notes)
        self.samples.append(s)
        return s

    # ---- 显式确认（唯一 confirmed 通道） ----
    def confirm(self, sample_id: str, confirm_by: str) -> Sample:
        if not confirm_by or not str(confirm_by).strip():
            raise ManifestError("E_CONFIRM_BY", "confirm_by（确认人署名）必填——owner_confirmed 只能显式写入")
        for s in self.samples:
            if s.sample_id == sample_id:
                if s.review_status == REVIEW_UNCERTAIN:
                    raise ManifestError("E_UNCERTAIN_CONFIRM",
                                        "uncertain 样本须先人工复核重登记为 candidate 才可确认")
                if s.review_status == REVIEW_CONFIRMED:
                    if s.confirmed_by == str(confirm_by).strip():
                        return s  # 幂等
                    raise ManifestError("E_RECONFIRM_CONFLICT",
                                        f"already confirmed by {s.confirmed_by!r}")
                s.review_status = REVIEW_CONFIRMED
                s.confirmed_by = str(confirm_by).strip()
                s.confirmed_at = _now()
                return s
        raise ManifestError("E_SAMPLE_NOT_FOUND", f"no sample {sample_id}")

    # ---- 导出（仅 confirmed，单风格） ----
    def export_training(self, style_id: str) -> list[dict]:
        if not style_id:
            raise ManifestError("E_STYLE_ID", "export 需显式 style_id（不混池）")
        rows = [s.to_dict() for s in self.samples
                if s.style_id == style_id and s.review_status == REVIEW_CONFIRMED]
        if not rows:
            raise ManifestError(
                "E_EMPTY_EXPORT",
                f"no owner_confirmed sample for style_id={style_id!r}（空 manifest/无已确认样本拒导出）")
        return rows

    def counts(self) -> dict:
        out: dict[str, int] = {}
        for s in self.samples:
            out[s.review_status] = out.get(s.review_status, 0) + 1
        return out
