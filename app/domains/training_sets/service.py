"""Training-image set service (patch-C section 3 CC-10 + section 11 gates).

Upload gates are shared with the P2 asset pipeline by reusing
``assets_upload.service`` (limits, ``_prepare``, ``_QUOTA_LOCK``) so the
50MB/20-per-batch/24MP/1:3..3:1 limits, the HEIC->PNG-only transcode, the
lossless red line, and the cross-pool 5GB quota check can never drift
between the two pools (patch-C 11.1/11.2/11.3/11.4, section 16.3).

Uploads only validate, hash and store bytes. The separate, explicit
``ai-confirm`` action invokes a configured pixel reviewer under the bounded
personal-use delegation; it never treats an upload as character confirmation.

Lock chain (frozen by dispatch T64 section 2; binding on T69): a delete is
refused with 409 SAMPLE_LOCKED when the image feeds a style sample whose
sample_id appears in a queued/running style run. During MVP bootstrap the
T69 tables (``style_samples`` / ``style_runs``) may not exist yet; if either
table is absent nothing can reference the image, so the delete proceeds.

TrainingSample registration (T2, batch B4-01; frozen sources:
t44-r1-extraction-spec-v2 §3 v3 + contracts/style-font-pipeline-v1.json
``data_contracts.sample_manifest_row``): a row is only persisted with
``touchup_confirmed=True`` (human confirmation passed); missing or non-true
→ 400 TOUCHUP_REQUIRED (RFC 9457 problem+json via the ApiError handler).

Split policy (C-57, FROZEN): ``split`` is NOT a column of TrainingSample
and is never accepted or persisted here. Train/val/test stratification is
done by the training script, stratifying on ``source_asset_id`` so that all
character instances from the same source image land in the same collection
(cross-collection splits of one source image are forbidden).

Caller binding (P09-BE 2026-09-27, E10 closure): ``register_sample`` takes
the authenticated ``user_id`` and enforces the frozen §3 glyph_path pattern
``users/{uid}/styles/{style_id}/glyphs/{sample_id}.png`` - the sample's
pixels must live in the CALLER's storage tree and exist on disk. A sample
therefore cannot bind another user's data even though ``style_id`` /
``source_asset_id`` remain opaque strings (sample_manifest_row contract;
no FK exists in the frozen schema - referential integrity is a D06
migration question, not silently inventable here).

Data discipline (R04 ledger, binding): B10 PRIMARY 20 + reserve 7 slices
(``materials/b10-realgt/slice_roster.json`` / ``artifacts/b10-realgt/
gt_manifest.json``) are EVALUATION-ONLY and must never enter this pool or
any training candidate set; B7/B9 SEALED sets are likewise excluded. This
domain has no batch import: training input arrives only through per-user
authenticated uploads (``store_batch``) and confirmed registrations
(``register_sample``). UNKNOWN-provenance material must not be registered.

Field hard validation (P09-BE, E10 closure) - all failures answer 400
INVALID_REQUEST: text_label is exactly one CJK ideograph (spec §3 单汉字);
``char_unicode`` U+XXXX must denote the same codepoint as ``text_label``;
``output_size`` is the frozen 128; every bbox component must be finite,
x/y in [0,1], w/h in (0,1], and x+w / y+h within [0,1] (negative, NaN,
Infinity, zero-size and out-of-bounds boxes are refused - pre-fix NaN/Inf
even crashed the 201 render after committing a poisoned row);
``glyph_path`` must equal the frozen pattern above AND exist on disk;
optional C-58 alpha pointers must be caller-owned storage-relative paths
when present (existence not yet enforced: no confirmed-alpha writer is
wired; backfill stays possible). A concurrent duplicate ``sample_id`` that
loses the PK race answers 400 instead of leaking a 500 IntegrityError.
Legacy rows registered before this tightening still serialize unchanged
(non-destructive read guarantee).
"""

import hashlib
import json
import math
import os
import re
import threading
import uuid
from datetime import datetime, timezone

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core import storage
from app.core.errors import ApiError
from app.domains.assets_upload import service as asset_gates
from app.domains.assets_upload.models import Asset
from app.domains.styles.models import StyleProfile, StyleSample
from app.domains.training_sets.models import TrainingImage, TrainingSample
from app.domains.usage.service import LIMIT_BYTES, storage_root, total_usage_bytes

ACTIVE_RUN_STATUSES = ("queued", "running")
MAX_PAGE_SIZE = 100
POOL = "training_images"
_SNAPSHOT_PREPARE_LOCK = threading.RLock()

# t44-r1-extraction-spec-v2 §3: char_unicode is "U+XXXX"; text_label is one
# CJK ideograph (a single Chinese character).
_CHAR_UNICODE_RE = re.compile(r"^U\+[0-9A-Fa-f]{4,6}$")
_BBOX_KEYS = ("x_norm", "y_norm", "w_norm", "h_norm")
# sample_id lands inside the glyph filename: ss_ namespace + filename-safe
# charset only (frozen example shape "ss_XXXXXX"; length capped at 64).
_SAMPLE_ID_RE = re.compile(r"^ss_[A-Za-z0-9_-]{1,58}$")
# style_id becomes a literal path segment of the required glyph_path, so it
# must be a single path-safe segment (no separators/dots-only).
_STYLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# Frozen output_size (spec §3 / Q1: 128x128 RGBA glyph PNG).
_FROZEN_OUTPUT_SIZE = 128
_CTRL_CHARS_RE = re.compile(r"[\x00-\x1f]")


def _is_cjk_ideograph(codepoint: int) -> bool:
    """Single Han ideograph per spec §3 (单汉字): URO + Extension A-F +
    compatibility ideographs."""
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2EBEF
    )



def _validate_owned_relative_path(value, user_id: int, field: str) -> str:
    """Shape/ownership check for optional storage-relative pointer fields.

    Must be a forward-slash relative path inside the caller's own
    ``users/{uid}/`` tree: no absolute/drive/UNC forms, no ``.``/``..``
    segments, no backslashes or control characters. Existence on disk is
    deliberately not required here (traceability pointers may be backfilled).
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "INVALID_REQUEST", f"{field} must be a non-empty string when present")
    if "\\" in value or _CTRL_CHARS_RE.search(value):
        raise ApiError(400, "INVALID_REQUEST", f"{field} must use forward slashes")
    segments = value.split("/")
    if (
        value.startswith("/")
        or value.endswith("/")
        or len(segments) < 3
        or any(seg in ("", ".", "..") for seg in segments)
        or segments[0] != "users"
        or segments[1] != str(user_id)
    ):
        raise ApiError(
            400,
            "INVALID_REQUEST",
            f"{field} must be a storage-relative path inside users/{user_id}/",
        )
    return value



def register_sample(session: Session, payload: dict, user_id: int) -> dict:
    """Register one confirmed character-level sample (T2, minimal MVP).

    Contract (frozen): touchup_confirmed must be present and True, else
    400 TOUCHUP_REQUIRED. Shape errors answer 400 INVALID_REQUEST - both
    codes are members of the frozen ``error_codes_full_table``. The optional
    C-58 traceability fields (``draft_alpha_path`` / ``confirmed_alpha_path``)
    may be absent; absence does not block registration. ``created_at`` is
    server-generated ISO8601 UTC. No ``split`` field exists (C-57): see the
    module docstring - stratification is a training-script concern.

    Caller binding (P09-BE, E10 closure): ``user_id`` is the authenticated
    caller from the router; the glyph must live at
    ``users/{user_id}/styles/{style_id}/glyphs/{sample_id}.png`` inside
    ``storage_root()`` and exist on disk, so one user can never register a
    sample over another user's pixels. Validation is non-destructive for
    legacy rows: only writes are tightened, reads are unchanged.

    Owner persistence (M-1, b2-extraction-data 2026-09-27): ``user_id`` is
    persisted on the row (models.TrainingSample docstring). The serialized
    manifest-row shape (``sample_dict``) is unchanged - the frozen
    ``sample_manifest_row`` contract carries the owner via ``glyph_path``
    and the column is persistence-internal.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "INVALID_REQUEST", "request body must be a JSON object")

    if payload.get("touchup_confirmed") is not True:
        raise ApiError(
            400,
            "TOUCHUP_REQUIRED",
            "Touchup confirmation required",
            "sample registration requires touchup_confirmed=true "
            "(human confirmation passed); t44-r1-extraction-spec-v2 §3",
        )

    sample_id = payload.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        raise ApiError(400, "INVALID_REQUEST", "sample_id must be a non-empty string")
    if not _SAMPLE_ID_RE.match(sample_id):
        raise ApiError(
            400,
            "INVALID_REQUEST",
            "sample_id must match ss_XXXXXX (ss_ namespace, filename-safe)",
        )
    if session.get(TrainingSample, sample_id) is not None:
        raise ApiError(
            400,
            "INVALID_REQUEST",
            f"sample_id already registered: {sample_id}",
        )

    missing = [
        field
        for field in (
            "style_id",
            "source_asset_id",
            "glyph_path",
            "text_label",
            "char_unicode",
            "extracted_by",
        )
        if not isinstance(payload.get(field), str) or not payload.get(field).strip()
    ]
    if missing:
        raise ApiError(
            400,
            "INVALID_REQUEST",
            f"missing or empty required string fields: {', '.join(missing)}",
        )

    style_id = payload["style_id"].strip()
    if not _STYLE_ID_RE.match(style_id):
        raise ApiError(
            400,
            "INVALID_REQUEST",
            "style_id must be one path-safe segment (letters/digits/_/-)",
        )
    if _CTRL_CHARS_RE.search(payload["source_asset_id"]):
        raise ApiError(400, "INVALID_REQUEST", "source_asset_id contains control characters")

    char_unicode = payload["char_unicode"]
    if not _CHAR_UNICODE_RE.match(char_unicode):
        raise ApiError(
            400,
            "INVALID_REQUEST",
            "char_unicode must match U+XXXX (e.g. U+9526)",
        )

    text_label = payload["text_label"]
    if len(text_label) != 1:
        raise ApiError(400, "INVALID_REQUEST", "text_label must be exactly one character")
    codepoint = ord(text_label)
    if not _is_cjk_ideograph(codepoint):
        raise ApiError(
            400,
            "INVALID_REQUEST",
            "text_label must be one CJK ideograph (t44 spec §3 单汉字)",
        )
    if int(char_unicode[2:], 16) != codepoint:
        raise ApiError(
            400,
            "INVALID_REQUEST",
            f"char_unicode {char_unicode} does not match text_label "
            f"{text_label!r} (U+{codepoint:04X})",
        )

    output_size = payload.get("output_size")
    if not isinstance(output_size, int) or isinstance(output_size, bool) or output_size <= 0:
        raise ApiError(400, "INVALID_REQUEST", "output_size must be a positive integer")
    if output_size != _FROZEN_OUTPUT_SIZE:
        raise ApiError(
            400,
            "INVALID_REQUEST",
            f"output_size must be the frozen {_FROZEN_OUTPUT_SIZE} "
            "(t44 spec §3: 128x128 RGBA glyph)",
        )

    bbox = payload.get("text_region_bbox_norm")
    if not isinstance(bbox, dict) or set(bbox) != set(_BBOX_KEYS):
        raise ApiError(
            400,
            "INVALID_REQUEST",
            "text_region_bbox_norm must be an object with exactly "
            "x_norm/y_norm/w_norm/h_norm",
        )
    for key in _BBOX_KEYS:
        value = bbox[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ApiError(
                400,
                "INVALID_REQUEST",
                f"text_region_bbox_norm.{key} must be a number",
            )
        if not math.isfinite(value):
            raise ApiError(
                400,
                "INVALID_REQUEST",
                f"text_region_bbox_norm.{key} must be finite (no NaN/Infinity)",
            )
        if key in ("x_norm", "y_norm") and not 0 <= value <= 1:
            raise ApiError(
                400,
                "INVALID_REQUEST",
                f"text_region_bbox_norm.{key} must be within [0, 1]",
            )
        if key in ("w_norm", "h_norm") and not 0 < value <= 1:
            raise ApiError(
                400,
                "INVALID_REQUEST",
                f"text_region_bbox_norm.{key} must be within (0, 1] (nonzero box)",
            )
    if bbox["x_norm"] + bbox["w_norm"] > 1 or bbox["y_norm"] + bbox["h_norm"] > 1:
        raise ApiError(
            400,
            "INVALID_REQUEST",
            "text_region_bbox_norm must stay inside the image (x+w <= 1, y+h <= 1)",
        )

    # Frozen §3 pattern binds the glyph to the CALLER's storage tree; a
    # foreign or fabricated path cannot pass the on-disk check either.
    expected_glyph_rel = f"users/{user_id}/styles/{style_id}/glyphs/{sample_id}.png"
    if payload["glyph_path"] != expected_glyph_rel:
        raise ApiError(
            400,
            "INVALID_REQUEST",
            f"glyph_path must be users/{user_id}/styles/{style_id}/glyphs/"
            f"{sample_id}.png (t44 spec §3, caller-owned)",
        )
    glyph_abs = os.path.join(storage_root(), *expected_glyph_rel.split("/"))
    if not os.path.isfile(glyph_abs):
        raise ApiError(
            400,
            "INVALID_REQUEST",
            "glyph_path does not exist in caller storage; register only "
            "confirmed extracted glyphs",
        )

    draft_alpha_path = _validate_owned_relative_path(
        payload.get("draft_alpha_path"), user_id, "draft_alpha_path"
    )
    confirmed_alpha_path = _validate_owned_relative_path(
        payload.get("confirmed_alpha_path"), user_id, "confirmed_alpha_path"
    )

    row = TrainingSample(
        sample_id=sample_id,
        user_id=user_id,  # M-1 owner persistence (P09/E10 closure); never client-supplied
        style_id=style_id,
        source_asset_id=payload["source_asset_id"],
        glyph_path=expected_glyph_rel,
        text_label=text_label,
        char_unicode=char_unicode,
        text_region_bbox_norm=json.dumps(
            {key: bbox[key] for key in _BBOX_KEYS}, ensure_ascii=False, allow_nan=False
        ),
        output_size=output_size,
        extracted_by=payload["extracted_by"],
        touchup_confirmed=True,
        draft_alpha_path=draft_alpha_path,
        confirmed_alpha_path=confirmed_alpha_path,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # Concurrent duplicate sample_id lost the PK race: answer the frozen
        # 400 shape instead of leaking a 500 IntegrityError.
        session.rollback()
        raise ApiError(
            400,
            "INVALID_REQUEST",
            f"sample_id already registered (concurrent): {sample_id}",
        )
    return sample_dict(row)



def sample_dict(row: TrainingSample) -> dict:
    """Serialize one TrainingSample to the manifest-row data shape (spec §3)."""
    return {
        "sample_id": row.sample_id,
        "style_id": row.style_id,
        "source_asset_id": row.source_asset_id,
        "glyph_path": row.glyph_path,
        "text_label": row.text_label,
        "char_unicode": row.char_unicode,
        "text_region_bbox_norm": json.loads(row.text_region_bbox_norm),
        "output_size": row.output_size,
        "extracted_by": row.extracted_by,
        "touchup_confirmed": row.touchup_confirmed,
        "draft_alpha_path": row.draft_alpha_path,
        "confirmed_alpha_path": row.confirmed_alpha_path,
        "created_at": row.created_at,
    }



def list_personal_style_samples(
    session: Session,
    user_id: int,
    style_id: int,
    *,
    review_status: str | None = None,
    page: int = 1,
    page_size: int = 100,
) -> dict:
    """Read the one canonical character table for a caller-owned handwriting style."""
    profile = session.query(StyleProfile).filter(
        StyleProfile.style_id == style_id, StyleProfile.user_id == user_id
    ).first()
    if profile is None:
        raise ApiError(404, "STYLE_NOT_FOUND", "Style not found")
    if profile.source_type != "HANDWRITING_ASSET":
        raise ApiError(409, "STYLE_IDENTITY_UNKNOWN", "Character review requires a HANDWRITING_ASSET style")
    if page < 1 or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ApiError(400, "INVALID_REQUEST", f"page must be >= 1 and page_size within 1..{MAX_PAGE_SIZE}")
    if review_status is not None and review_status not in ("candidate", "owner_confirmed", "ai_confirmed_authorized", "rejected"):
        raise ApiError(400, "INVALID_REQUEST", "review_status must be candidate, owner_confirmed, ai_confirmed_authorized or rejected")

    query = session.query(TrainingSample).filter(
        TrainingSample.user_id == user_id,
        TrainingSample.style_id == str(style_id),
    )
    if review_status is not None:
        query = query.filter(TrainingSample.review_status == review_status)
    total = query.count()
    rows = query.order_by(TrainingSample.created_at.asc(), TrainingSample.sample_id.asc()).offset(
        (page - 1) * page_size
    ).limit(page_size).all()
    return {
        "items": [_personal_style_sample_dict(session, row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "style_id": style_id,
        "target_style": {"name": profile.name, "source_type": profile.source_type, "status": profile.status},
    }


def _personal_style_sample_dict(session: Session, row: TrainingSample) -> dict:
    """Safe review payload: image ids are served through existing owner-scoped file routes."""
    try:
        bbox = json.loads(row.text_region_bbox_norm)
    except (TypeError, json.JSONDecodeError):
        bbox = None
    provenance_complete = bool(
        row.source_training_image_id is not None and row.style_sample_id is not None
        and row.source_asset_sha256 and row.source_family_id and row.draft_alpha_path
        and row.source_training_image_sha256 and row.draft_alpha_sha256
        and row.final_alpha_sha256 and bbox is not None
        and row.touchup_confirmed is True and row.touchup_confirmed_at
        and isinstance(row.touchup_edit_count, int) and row.touchup_edit_count >= 0
        and row.provenance_version == "p4-s02/v1"
    )
    ai_review = {"review_state": "BLOCKED_AI_PROVIDER", "recommendation": "AI_NEEDS_HUMAN"}
    review_provenance = None
    display_reason = row.review_reason
    if row.source_training_image_id is not None and row.final_alpha_sha256:
        try:
            from .visual_review import ensure_candidate_review, load_confirmation_receipt
            image_path = _owned_pool_path(row.user_id, "training_images", row.glyph_path)
            if (row.reviewed_by or "").startswith("ai:") or row.review_status == "ai_confirmed_authorized":
                ai_review = load_confirmation_receipt(row, require_confirmed=row.review_status == "ai_confirmed_authorized")
                display_reason = ai_review["reason"]
                review_provenance = ai_review if row.review_status == "ai_confirmed_authorized" else None
                if _sha256_file(image_path) != row.final_alpha_sha256:
                    raise ValueError("confirmed candidate pixels changed")
            else:
                ai_review = {"review_state": "NOT_REQUESTED", "recommendation": "AI_NEEDS_HUMAN", "reason": "AI review requires a separate explicit action"}
        except Exception as exc:
            ai_review = {"review_state": "BLOCKED_AI_EVIDENCE", "recommendation": "AI_NEEDS_HUMAN",
                         "review_error": str(exc), "reason": str(exc)}
            review_provenance = None
    return {
        "candidate_visual_review": ai_review,
        "sample_id": row.sample_id,
        "style_id": int(row.style_id) if str(row.style_id).isdigit() else row.style_id,
        "text_label": row.text_label,
        "char_unicode": row.char_unicode,
        "source_asset_id": row.source_asset_id,
        "source_asset_sha256": row.source_asset_sha256,
        "source_asset_width": row.source_asset_width,
        "source_asset_height": row.source_asset_height,
        "source_family_id": row.source_family_id,
        "source_training_image_id": row.source_training_image_id,
        "source_training_image_sha256": row.source_training_image_sha256,
        "draft_alpha_sha256": row.draft_alpha_sha256,
        "final_alpha_sha256": row.final_alpha_sha256,
        "text_region_bbox_norm": bbox,
        "touchup_edit_count": row.touchup_edit_count,
        "touchup_confirmed_at": row.touchup_confirmed_at,
        "style_sample_id": row.style_sample_id,
        "review_status": row.review_status,
        "owner_confirmed_by": row.owner_confirmed_by,
        "owner_confirmed_at": row.owner_confirmed_at,
        "review_reason": display_reason,
        "reviewed_by": row.reviewed_by,
        "reviewed_at": row.reviewed_at,
        "confirmation_actor_type": ("ai" if row.review_status == "ai_confirmed_authorized"
                                    else "human" if row.review_status == "owner_confirmed" else None),
        "review_provenance": review_provenance,
        "provenance_version": row.provenance_version,
        "provenance_complete": provenance_complete,
        "created_at": row.created_at,
    }


def _snapshot_profile(session: Session, user_id: int, style_id: int):
    if type(style_id) is not int or style_id < 1:
        raise ApiError(400, "INVALID_REQUEST", "style_id must be a positive integer")
    profile = session.query(StyleProfile).filter(
        StyleProfile.style_id == style_id, StyleProfile.user_id == user_id
    ).first()
    if profile is None:
        raise ApiError(404, "STYLE_NOT_FOUND", "Style not found")
    if profile.source_type != "HANDWRITING_ASSET" or profile.status != "approved":
        raise ApiError(409, "STYLE_NOT_APPROVED", "Snapshot preparation requires an approved handwriting style")
    return profile



def _snapshot_blockers(report: dict) -> list[str]:
    dependency = report.get("source_dependencies") or {}
    if dependency.get("status") == "BLOCKED_DEPENDENCY":
        return [f"Missing metadata: {item['path']} SHA256 {item['expected_sha256']}"
                for item in dependency.get("missing_metadata", [])] + [
            dependency.get("detail") or "R04 integrity prerequisite failed",
            dependency.get("remediation") or "Restore the required R04 metadata"]
    reasons = list(report.get("s03_reasons") or [])
    if report.get("m2_missing_unique_char_count", 0):
        reasons.append(f"Need {report['m2_missing_unique_char_count']} more distinct eligible characters for M2")
    if not report.get("structural_dataset_ready") and not reasons:
        reasons.append(report.get("reason") or report.get("dataset_snapshot_ready") or "Intake is unavailable")
    return reasons



def _snapshot_family_components(report: dict) -> list[dict]:
    from .source_admission import checked_ledger
    from .split_authorization import _complete_family_components
    from engine.style.dataset.schema import DatasetError
    sources = report.get("eligible_parent_sources") or []
    if not sources:
        return []
    ledger = checked_ledger()
    if ledger.sha256 != report.get("r04_ledger_sha256"):
        raise DatasetError("E_INTAKE_CHANGED", "ledger changed during preflight; reload current families")
    # Match production transitive grouping, including absent intermediate relatives.
    groups = _complete_family_components([
        {"sample_id": source["source_asset_id"], "parent_source_hash": source["source_asset_sha256"]}
        for source in sources], ledger)
    return [{"component_id": group["component_id"], "parent_hashes": group["parent_hashes"],
             "source_asset_ids": group["sample_ids"], "family_ids": group["family_ids"]}
            for group in groups]



def snapshot_preflight(session: Session, user_id: int, style_id: int) -> dict:
    """Read current canonical intake without a receipt, audit, review, or snapshot write."""
    from scripts.export_personal_style_dataset import export
    from engine.style.dataset.schema import DatasetError

    _snapshot_profile(session, user_id, style_id)
    result = {"schema": "personal-snapshot-preflight/1", "target_user_id": user_id,
        "target_style_id": style_id, "base_intake_sha256": None,
        "eligible_parent_sources": [], "explicit_family_split_required": False,
        "family_components": [], "blockers": [], "status": "BLOCKED",
        "structural_dataset_ready": False, "snapshot_ready": False,
        "snapshot_created": False, "authorization_created": False,
        "selection_requires_expected_base_intake_sha256": True}
    root = storage.ensure_within_root(storage.storage_root() / "users" / str(user_id) / "styles" / "snapshots")
    try:
        report = export(style_id, root / "preflight", create_snapshot=False, expected_user_id=user_id)
        # Only server-owned allowlisted report fields cross this read endpoint.
        for key in ("base_intake_sha256", "eligible_parent_sources", "explicit_family_split_required",
                    "structural_dataset_ready", "eligible_confirmed_sample_count", "eligible_confirmed_characters",
                    "m2_missing_unique_char_count", "candidate_count", "reviewed_version_count",
                    "occurrence_selection_sha256", "occurrence_selection_required", "dataset_snapshot_ready"):
            if key in report:
                result[key] = report[key]
        result["family_components"] = _snapshot_family_components(report)
        result["blockers"] = _snapshot_blockers(report)
        result["status"] = "PREFLIGHT_READY" if not result["blockers"] else "BLOCKED"
    except (DatasetError, OSError, ValueError, KeyError, RuntimeError, TypeError) as exc:
        result.update(base_intake_sha256=None, eligible_parent_sources=[], family_components=[],
                      structural_dataset_ready=False, blockers=[str(exc)], status="BLOCKED")
    return result



def _reserve_explicit_snapshot_intake(session: Session) -> None:
    """Hold SQLite's writer reservation until the caller commits/rolls back.

    The exporter uses independent read-only connections. A write reservation
    keeps their canonical database input stable through compare, authorization,
    and freeze without changing any row. Files are independently hash checked.
    """
    from sqlalchemy.exc import OperationalError
    if session.get_bind().dialect.name != "sqlite":
        raise ApiError(409, "INTAKE_ATOMICITY_UNAVAILABLE", "Atomic intake selection requires SQLite")
    try:
        connection = session.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            # Upgrade an existing read transaction; affects zero rows.
            connection.exec_driver_sql("UPDATE style_profiles SET style_id=style_id WHERE 0")
    except OperationalError as exc:
        raise ApiError(409, "INTAKE_BUSY", "Intake changed concurrently; reload preflight and retry", retryable=True) from exc



def prepare_personal_style_snapshot(session: Session, user_id: int, payload: dict) -> dict:
    """Create or reuse a verified NEW intake snapshot; paths/hashes are server-owned."""
    from scripts.export_personal_style_dataset import export
    from engine.style.dataset.schema import DatasetError
    from engine.style.dataset.binding import verify_snapshot_binding

    legacy_fields = {"style_id"}
    explicit_fields = {"style_id", "parent_assignments", "expected_base_intake_sha256"}
    if not isinstance(payload, dict) or set(payload) not in (legacy_fields, explicit_fields):
        raise ApiError(400, "INVALID_REQUEST", "snapshot preparation requires style_id; explicit assignments also require expected_base_intake_sha256")
    if "parent_assignments" in payload and (not isinstance(payload["expected_base_intake_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", payload["expected_base_intake_sha256"])):
        raise ApiError(400, "INVALID_REQUEST", "expected_base_intake_sha256 must be the lowercase SHA256 from current preflight")
    style_id = payload["style_id"]
    _snapshot_profile(session, user_id, style_id)
    root = storage.ensure_within_root(storage.storage_root() / "users" / str(user_id) / "styles" / "snapshots")
    # This desktop product is single-process. Serialize before fingerprinting
    # so retry/concurrent clicks reuse one freeze instead of duplicating it.
    with _SNAPSHOT_PREPARE_LOCK:
        try:
            if "parent_assignments" in payload:
                _reserve_explicit_snapshot_intake(session)
            report = export(style_id, root / "preflight", create_snapshot=False, expected_user_id=user_id)
            family_plan=None
            if "parent_assignments" in payload:
                if report.get("base_intake_sha256") != payload["expected_base_intake_sha256"]:
                    raise ApiError(409, "INTAKE_PREFLIGHT_STALE", "Intake changed; reload preflight and select the current source families")
                from .split_authorization import authorize_family_split_plan, _assignments, _sources
                assignments = _assignments(payload["parent_assignments"], _sources(report.get("eligible_parent_sources")))
                for component in _snapshot_family_components(report):
                    if len({assignments[parent] for parent in component["parent_hashes"]}) != 1:
                        raise DatasetError("E_FAMILY_CROSS_SPLIT", "an R04 source family cannot cross train and validation")
                family_plan=authorize_family_split_plan(user_id=user_id,style_id=style_id,intake_report=report,parent_assignments=payload["parent_assignments"],actor_kind="authenticated_user")
                report=export(style_id,root/"preflight",create_snapshot=False,expected_user_id=user_id,family_split_plan=family_plan)
                audit(session,action="personal_family_split_select",actor=family_plan["authorization"]["actor"],object_ref=f"styles/{style_id}",result=family_plan["plan_sha256"])
            blocked = {**report, "status": "BLOCKED", "snapshot_ready": False,
                       "snapshot_created": False, "snapshot_reused": False}
            if (report.get("source_dependencies") or {}).get("status")=="BLOCKED_DEPENDENCY":
                dependency=report["source_dependencies"]
                blocked["status"]="BLOCKED_DEPENDENCY"
                blocked["blockers"]=[f"Missing metadata: {item['path']} SHA256 {item['expected_sha256']}" for item in dependency.get("missing_metadata",[])]
                blocked["blockers"] += [dependency.get("detail") or "R04 integrity prerequisite failed",dependency["remediation"]]
                return blocked
            if not report.get("structural_dataset_ready"):
                blocked["blockers"] = list(report.get("s03_reasons") or [])
                if report.get("m2_missing_unique_char_count", 0):
                    blocked["blockers"].append(f"Need {report['m2_missing_unique_char_count']} more distinct eligible characters for M2")
                if not blocked["blockers"]:
                    blocked["blockers"] = [report.get("reason") or report["dataset_snapshot_ready"]]
                return blocked
            # Full identity stays in immutable metadata; a bounded directory
            # name keeps Windows snapshot sample paths below legacy MAX_PATH.
            # A rare prefix collision fails the full intake check, never overwrites.
            target = storage.ensure_within_root(root / ("ps-" + report["intake_sha256"][:32]))
            if (target / "FROZEN").is_file():
                index = json.loads((target / "index.json").read_text(encoding="utf-8"))
                binding = verify_snapshot_binding(target, index.get("snapshot_sha256"))
                metadata = json.loads((target / "provenance.json").read_text(encoding="utf-8"))
                from .human_review import assert_frozen_reviews_current
                profile = _snapshot_profile(session, user_id, style_id)
                linked = session.query(TrainingSample).filter(TrainingSample.user_id == user_id, TrainingSample.style_id == str(style_id),
                    TrainingSample.sample_id.in_(metadata.get('confirmed_sample_ids', []))).all()
                assert_frozen_reviews_current(session, profile, {row.sample_id: row for row in linked}, metadata)
                if (metadata.get("intake_sha256") != report["intake_sha256"]
                        or metadata.get("target_style_id") != style_id
                        or metadata.get("dataset_visual_audit", {}).get("may_freeze_new_snapshot") is not True):
                    raise DatasetError("E_SNAPSHOT_PROVENANCE", "existing intake snapshot lacks verified visual provenance")
                return {**report, "status": "READY", "snapshot_ready": True, "snapshot_created": False,
                    "snapshot_reused": True, "blockers": [], "snapshot_id": binding["snapshot_id"],
                    "dataset_snapshot_dir": str(target), "dataset_snapshot_sha256": binding["snapshot_sha256"],
                    "snapshot_binding": binding, "dataset_snapshot_ready": "OK", "DATASET_VISUAL_REVIEW": "PASS",
                    "AI_VISUAL_REVIEW": metadata['dataset_visual_audit'].get('AI_VISUAL_REVIEW', 'PASS')}
            report = export(style_id, target, expected_user_id=user_id, expected_intake_sha256=report["intake_sha256"],family_split_plan=family_plan)
            if not report.get("snapshot_created"):
                return {**report, "status": "BLOCKED", "snapshot_ready": False, "snapshot_reused": False,
                    "blockers": report.get("s03_reasons") or [report.get("reason") or report["dataset_snapshot_ready"]]}
            binding = verify_snapshot_binding(target, report["snapshot_sha256"])
            return {**report, "status": "READY", "snapshot_ready": True, "snapshot_reused": False,
                "blockers": [], "dataset_snapshot_dir": str(target),
                "dataset_snapshot_sha256": binding["snapshot_sha256"], "snapshot_binding": binding}
        except (DatasetError, OSError, ValueError, KeyError, RuntimeError) as exc:
            return {"status": "BLOCKED", "snapshot_ready": False, "snapshot_created": False,
                "snapshot_reused": False, "blockers": [str(exc)], "target_style_id": style_id}



def ai_confirm_personal_style_samples(session: Session, user_id: int, payload: dict) -> dict:
    """Review candidates under the exact scoped user delegation, with no fake human stamp.

    The server, never the request body, chooses the configured reviewer and
    supplies canonical pixels/context. Uncertain/unavailable reviews remain
    candidates, so they cannot enter any training export.
    """
    from engine.style.visual_review.policy import candidate_delegation
    from engine.style.dataset.ledger import LedgerIndex, resolve_active_ledger_path
    from engine.style.dataset.schema import DatasetError
    from pathlib import Path
    from .visual_review import review_candidate_for_confirmation, save_confirmation_receipt

    if not isinstance(payload, dict) or set(payload) != {"style_id", "sample_ids"}:
        raise ApiError(400, "INVALID_REQUEST", "AI confirmation accepts only style_id and sample_ids")
    style_id, ids = payload.get("style_id"), payload.get("sample_ids")
    if type(style_id) is not int or style_id < 1:
        raise ApiError(400, "INVALID_REQUEST", "style_id must be a positive integer")
    if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_PAGE_SIZE
            or any(not isinstance(sid, str) or not sid.strip() for sid in ids) or len(ids) != len(set(ids))):
        raise ApiError(400, "INVALID_REQUEST", f"sample_ids must contain 1..{MAX_PAGE_SIZE} unique sample ids")
    profile = session.query(StyleProfile).filter(StyleProfile.style_id == style_id, StyleProfile.user_id == user_id).first()
    if profile is None:
        raise ApiError(404, "STYLE_NOT_FOUND", "Style not found")
    if profile.source_type != "HANDWRITING_ASSET" or profile.status != "approved":
        raise ApiError(409, "STYLE_NOT_APPROVED", "AI confirmation requires an approved handwriting style")
    if candidate_delegation(user_id) is None:
        raise ApiError(403, "AI_CONFIRMATION_NOT_AUTHORIZED", "No AI confirmation delegation for this user/project")
    rows = session.query(TrainingSample).filter(TrainingSample.sample_id.in_(ids),
        TrainingSample.user_id == user_id, TrainingSample.style_id == str(style_id)).all()
    by_id = {row.sample_id: row for row in rows}
    if len(by_id) != len(ids):
        raise ApiError(404, "TRAINING_SAMPLE_NOT_FOUND", "One or more samples were not found")
    # Consult metadata before opening source pixels. Even an accidentally
    # inserted canonical candidate must not make AI inspect sealed/eval data.
    try:
        project_root = Path(__file__).resolve().parents[3]
        ledger = LedgerIndex.load(resolve_active_ledger_path(project_root))
        ledger.assert_r04_integrity(project_root)
    except DatasetError as exc:
        raise ApiError(409, "R04_DATA_BLOCKED", "Source ledger integrity blocks AI confirmation", str(exc)) from exc
    prepared = []
    # Complete preflight before invoking any visual adapter or writing evidence.
    for sid in ids:
        row = by_id[sid]
        if row.review_status != "candidate":
            raise ApiError(409, "SAMPLE_ALREADY_REVIEWED", f"sample {sid} is {row.review_status}")
        if row.owner_confirmed_by is not None or row.owner_confirmed_at is not None:
            raise ApiError(409, "SAMPLE_PROVENANCE_INVALID", "Candidate already carries human confirmation")
        source_sha = (row.source_asset_sha256 or "").lower()
        family = ledger.family_of(source_sha)
        if (source_sha in ledger.forbidden_hashes or source_sha in ledger.unknown_hashes
                or family and ledger.families[family] & (ledger.forbidden_hashes | ledger.unknown_hashes)):
            raise ApiError(409, "R04_DATA_BLOCKED", f"sample {sid} belongs to a forbidden/unknown source family")
        if not isinstance(row.text_label, str) or len(row.text_label) != 1 or not _is_cjk_ideograph(ord(row.text_label)):
            raise ApiError(409, "SAMPLE_PROVENANCE_INVALID", f"sample {sid} requires a valid single CJK label")
        style_sample = session.get(StyleSample, row.style_sample_id) if row.style_sample_id is not None else None
        _verify_personal_sample_provenance(session, user_id, style_id, row, style_sample, row.text_label)
        if style_sample.text_label != row.text_label or row.char_unicode != f"U+{ord(row.text_label):04X}":
            raise ApiError(409, "SAMPLE_PROVENANCE_INVALID", "AI review cannot silently repair a drifted label")
        asset = session.query(Asset).filter(Asset.asset_id == row.source_asset_id, Asset.user_id == str(user_id)).one()
        prepared.append((row, row.reviewed_at, _owned_pool_path(user_id, "assets", asset.relative_path)))
    results = []
    for row, previous_reviewed_at, source_path in prepared:
        receipt = review_candidate_for_confirmation(user_id=user_id, style_id=style_id, sample_id=row.sample_id,
            image_path=_owned_pool_path(user_id, POOL, row.glyph_path), source_image_path=source_path,
            source_bbox_norm=json.loads(row.text_region_bbox_norm), label=row.text_label)
        # Persist immutable evidence first. An orphan after rollback is not a
        # confirmation: consumers require the DB-bound checksum/stamps too.
        reference = save_confirmation_receipt(receipt)
        claimed = session.query(TrainingSample).filter(TrainingSample.sample_id == row.sample_id,
            TrainingSample.user_id == user_id, TrainingSample.style_id == str(style_id),
            TrainingSample.review_status == "candidate", TrainingSample.reviewed_at == previous_reviewed_at,
            TrainingSample.final_alpha_sha256 == receipt["binding"]["image_sha256"],
            TrainingSample.source_asset_sha256 == receipt["binding"]["source_image_sha256"],
            TrainingSample.text_label == receipt["binding"]["label"],
        ).update({TrainingSample.review_status: receipt["review_status"],
            TrainingSample.reviewed_by: receipt["actor"], TrainingSample.reviewed_at: receipt["reviewed_at"],
            TrainingSample.review_reason: reference}, synchronize_session=False)
        if claimed != 1:
            raise ApiError(409, "SAMPLE_ALREADY_REVIEWED", f"sample {row.sample_id} changed during AI review")
        row.review_status, row.reviewed_by = receipt["review_status"], receipt["actor"]
        row.reviewed_at, row.review_reason = receipt["reviewed_at"], reference
        audit(session, action="personal_style_sample_ai_confirm" if row.review_status == "ai_confirmed_authorized"
              else "personal_style_sample_ai_isolate", actor=receipt["actor"],
              object_ref=f"training_samples/{row.sample_id}", result=row.review_status)
        results.append({"sample_id": row.sample_id, "review_status": row.review_status,
                        "reason": receipt["reason"], "candidate_visual_review": receipt})
    session.flush()
    confirmed = sum(item["review_status"] == "ai_confirmed_authorized" for item in results)
    return {"style_id": style_id, "confirmed": confirmed, "isolated": len(results) - confirmed, "items": results}


def review_personal_style_samples(session: Session, user_id: int, payload: dict) -> dict:
    """Apply a single explicit Owner batch; every decision gets its own audit and timestamp."""
    if not isinstance(payload, dict):
        raise ApiError(400, "INVALID_REQUEST", "request body must be a JSON object")
    style_id = payload.get("style_id")
    items = payload.get("items")
    if not isinstance(style_id, int) or isinstance(style_id, bool) or style_id < 1:
        raise ApiError(400, "INVALID_REQUEST", "style_id must be a positive integer")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_PAGE_SIZE:
        raise ApiError(400, "INVALID_REQUEST", f"items must contain 1..{MAX_PAGE_SIZE} decisions")
    profile = session.query(StyleProfile).filter(
        StyleProfile.style_id == style_id, StyleProfile.user_id == user_id
    ).first()
    if profile is None:
        raise ApiError(404, "STYLE_NOT_FOUND", "Style not found")
    if profile.source_type != "HANDWRITING_ASSET":
        raise ApiError(409, "STYLE_IDENTITY_UNKNOWN", "Character review requires a HANDWRITING_ASSET style")

    by_id: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ApiError(400, "INVALID_REQUEST", "each review item must be an object")
        sample_id = item.get("sample_id")
        decision = item.get("decision")
        if not isinstance(sample_id, str) or not sample_id.strip() or decision not in ("confirm", "reject"):
            raise ApiError(400, "INVALID_REQUEST", "each item needs sample_id and confirm/reject decision")
        if sample_id in by_id:
            raise ApiError(400, "INVALID_REQUEST", f"duplicate sample_id in batch: {sample_id}")
        label = item.get("text_label")
        if label is not None and (not isinstance(label, str) or len(label) != 1 or not _is_cjk_ideograph(ord(label))):
            raise ApiError(400, "INVALID_REQUEST", "text_label must be one CJK ideograph")
        reason = item.get("reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
            raise ApiError(400, "INVALID_REQUEST", "reason must be text up to 500 characters")
        by_id[sample_id] = {**item, "decision": decision, "text_label": label,
                            "reason": reason.strip() if isinstance(reason, str) and reason.strip() else None}

    rows = session.query(TrainingSample).filter(
        TrainingSample.sample_id.in_(list(by_id)),
        TrainingSample.user_id == user_id,
        TrainingSample.style_id == str(style_id),
    ).all()
    rows_by_id = {row.sample_id: row for row in rows}
    if len(rows_by_id) != len(by_id):
        raise ApiError(404, "TRAINING_SAMPLE_NOT_FOUND", "One or more samples were not found")

    now = datetime.now(timezone.utc).isoformat()
    # Validate the entire batch first. No partial review is applied if one row is stale.
    prepared: list[tuple[TrainingSample, StyleSample | None, dict, str | None]] = []
    for sample_id, item in by_id.items():
        row = rows_by_id[sample_id]
        if row.review_status != "candidate":
            raise ApiError(409, "SAMPLE_ALREADY_REVIEWED", f"sample {sample_id} is {row.review_status}")
        label = item["text_label"] or row.text_label
        style_sample = session.get(StyleSample, row.style_sample_id) if row.style_sample_id is not None else None
        if item["decision"] == "confirm":
            if profile.status != "approved":
                raise ApiError(409, "STYLE_NOT_APPROVED", "Only an approved handwriting style can be confirmed for training")
            if not isinstance(label, str) or len(label) != 1 or not _is_cjk_ideograph(ord(label)):
                raise ApiError(400, "INVALID_REQUEST", f"sample {sample_id} needs one confirmed CJK label")
            _verify_personal_sample_provenance(session, user_id, style_id, row, style_sample, label)
        else:
            if style_sample is not None and (style_sample.style_id != style_id or style_sample.source_training_image_id != row.source_training_image_id):
                raise ApiError(409, "SAMPLE_PROVENANCE_INVALID", f"sample {sample_id} StyleSample link does not match")
        prepared.append((row, style_sample, item, label))

    results = []
    for row, style_sample, item, label in prepared:
        decision = item["decision"]
        new_status = "owner_confirmed" if decision == "confirm" else "rejected"
        claimed = session.query(TrainingSample).filter(
            TrainingSample.sample_id == row.sample_id,
            TrainingSample.user_id == user_id,
            TrainingSample.style_id == str(style_id),
            TrainingSample.review_status == "candidate",
        ).update({
            TrainingSample.review_status: new_status,
            TrainingSample.reviewed_by: str(user_id),
            TrainingSample.reviewed_at: now,
        }, synchronize_session=False)
        if claimed != 1:
            raise ApiError(409, "SAMPLE_ALREADY_REVIEWED", f"sample {row.sample_id} was reviewed concurrently")
        row.review_status = new_status
        row.reviewed_by = str(user_id)
        row.reviewed_at = now
        row.review_reason = item["reason"]
        if decision == "confirm":
            row.text_label = label
            row.char_unicode = f"U+{ord(label):04X}"
            row.owner_confirmed_by = str(user_id)
            row.owner_confirmed_at = now
            style_sample.text_label = label
            action = "personal_style_sample_confirm"
            result = "owner_confirmed"
        else:
            action = "personal_style_sample_reject"
            result = "rejected"
        audit(session, action=action, actor=str(user_id),
              object_ref=f"training_samples/{row.sample_id}", result=result)
        results.append({"sample_id": row.sample_id, "decision": decision,
                        "review_status": row.review_status, "owner_confirmed_at": row.owner_confirmed_at})
    session.flush()
    return {"style_id": style_id, "items": results, "confirmed": sum(r["decision"] == "confirm" for r in results),
            "rejected": sum(r["decision"] == "reject" for r in results)}



def _verify_personal_sample_provenance(
    session: Session, user_id: int, style_id: int, row: TrainingSample,
    style_sample: StyleSample | None, label: str,
) -> None:
    if (row.source_training_image_id is None or row.style_sample_id is None or style_sample is None
            or row.provenance_version != "p4-s02/v1" or row.touchup_confirmed is not True
            or row.touchup_confirmed_at is None or not isinstance(row.touchup_edit_count, int)
            or isinstance(row.touchup_edit_count, bool) or row.touchup_edit_count < 0
            or not row.source_asset_sha256 or row.source_family_id != f"sha256:{row.source_asset_sha256}"):
        raise ApiError(409, "SAMPLE_PROVENANCE_INCOMPLETE", f"sample {row.sample_id} has incomplete P4 provenance")
    if style_sample.style_id != style_id or style_sample.source_training_image_id != row.source_training_image_id:
        raise ApiError(409, "SAMPLE_PROVENANCE_INVALID", f"sample {row.sample_id} StyleSample link does not match")
    if style_sample.text_label != row.text_label and row.text_label != label:
        raise ApiError(409, "SAMPLE_PROVENANCE_INVALID", f"sample {row.sample_id} character label drifted")
    image = session.query(TrainingImage).filter(
        TrainingImage.image_id == row.source_training_image_id, TrainingImage.user_id == user_id
    ).first()
    asset = session.query(Asset).filter(
        Asset.asset_id == row.source_asset_id, Asset.user_id == str(user_id), Asset.enabled == 1
    ).first()
    if image is None or asset is None or asset.kind not in ("cos", "sample_source") or asset.status != "approved":
        raise ApiError(409, "SAMPLE_PROVENANCE_INVALID", f"sample {row.sample_id} source image is unavailable or unapproved")
    if (asset.sha256 != row.source_asset_sha256 or asset.width != row.source_asset_width
            or asset.height != row.source_asset_height):
        raise ApiError(409, "SAMPLE_PROVENANCE_INVALID", f"sample {row.sample_id} source asset metadata drifted")
    if (image.sha256 != row.source_training_image_sha256 or image.width != 128 or image.height != 128
            or row.output_size != 128 or row.glyph_path != image.relative_path
            or row.confirmed_alpha_path != image.relative_path
            or style_sample.sha256 != image.sha256 or style_sample.relative_path != image.relative_path):
        raise ApiError(409, "SAMPLE_PROVENANCE_INVALID", f"sample {row.sample_id} saved glyph metadata drifted")
    root = storage_root()
    asset_path = _owned_pool_path(user_id, "assets", asset.relative_path)
    image_path = _owned_pool_path(user_id, POOL, image.relative_path)
    if not os.path.isfile(asset_path) or _sha256_file(asset_path) != row.source_asset_sha256:
        raise ApiError(409, "SOURCE_HASH_MISMATCH", f"sample {row.sample_id} source asset file changed")
    if not os.path.isfile(image_path) or _sha256_file(image_path) != row.final_alpha_sha256 or row.final_alpha_sha256 != row.source_training_image_sha256:
        raise ApiError(409, "SOURCE_HASH_MISMATCH", f"sample {row.sample_id} saved glyph file changed")
    draft_match = re.fullmatch(r"users/(\d+)/styles/drafts/([0-9a-f]{16}\.png)", row.draft_alpha_path or "")
    if draft_match is None or draft_match.group(1) != str(user_id):
        raise ApiError(409, "SAMPLE_PROVENANCE_INVALID", f"sample {row.sample_id} draft path is not caller-owned")
    draft_path = storage.draft_path(user_id, draft_match.group(2))
    if not draft_path.is_file() or _sha256_file(str(draft_path)) != row.draft_alpha_sha256:
        raise ApiError(409, "SOURCE_HASH_MISMATCH", f"sample {row.sample_id} extraction draft changed")
    try:
        bbox = json.loads(row.text_region_bbox_norm)
        keys = {"x_norm", "y_norm", "w_norm", "h_norm"}
        valid_bbox = isinstance(bbox, dict) and set(bbox) == keys and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            for v in bbox.values()
        ) and (0 <= bbox["x_norm"] <= 1 and 0 <= bbox["y_norm"] <= 1
               and 0 < bbox["w_norm"] <= 1 and 0 < bbox["h_norm"] <= 1
               and bbox["x_norm"] + bbox["w_norm"] <= 1
               and bbox["y_norm"] + bbox["h_norm"] <= 1)
    except (TypeError, ValueError, json.JSONDecodeError):
        valid_bbox = False
    if not valid_bbox:
        raise ApiError(409, "SAMPLE_PROVENANCE_INVALID", f"sample {row.sample_id} has an invalid source box")



def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()



def _owned_pool_path(user_id: int, pool: str, relative_path: str) -> str:
    normalized = str(relative_path).replace("\\", "/")
    parts = normalized.split("/")
    if len(parts) < 3 or parts[:2] != [str(user_id), pool] or any(
        part in ("", ".", "..") for part in parts
    ):
        raise ApiError(409, "SAMPLE_PROVENANCE_INVALID", "Stored source path is not in the authenticated user's pool")
    root = storage.storage_root()
    path = storage.ensure_within_root(root.joinpath(*parts), root)
    return os.fspath(path)



def image_dict(row: TrainingImage) -> dict:
    """Serialize one row to the frozen seven-field data shape (patch-C section 3)."""
    return {
        "image_id": row.image_id,
        "file_name": row.file_name,
        "width": row.width,
        "height": row.height,
        "size_bytes": row.size_bytes,
        "status": row.status,
        "uploaded_at": row.uploaded_at,
    }



def list_images(session: Session, user_id: int, page: int, page_size: int) -> dict:
    """Paginated listing scoped to the caller; newest first."""
    if page < 1 or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ApiError(
            400,
            "INVALID_REQUEST",
            f"page must be >= 1 and page_size within 1..{MAX_PAGE_SIZE}",
        )
    query = session.query(TrainingImage).filter(TrainingImage.user_id == user_id)
    total = query.count()
    rows = (
        query.order_by(TrainingImage.image_id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [image_dict(row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }



def store_batch(session: Session, user_id: int, files: list) -> list:
    """Validate, quota-gate, then atomically store one batch of uploads.

    ``files`` is a list of (filename, content_bytes). The batch is
    all-or-nothing: every gate for every file passes before any file hits
    disk, so a rejected batch leaves zero residue (same discipline as the
    P2 asset pipeline).
    """
    if not files:
        raise ApiError(400, "INVALID_REQUEST", "empty upload batch")
    if len(files) > asset_gates.MAX_BATCH_COUNT:
        raise ApiError(
            400,
            "INVALID_REQUEST",
            f"batch has {len(files)} files, max is {asset_gates.MAX_BATCH_COUNT}",
        )
    for filename, data in files:
        if len(data) > asset_gates.MAX_FILE_BYTES:
            raise ApiError(
                413,
                "FILE_TOO_LARGE",
                f"{filename}: {len(data)} bytes exceeds {asset_gates.MAX_FILE_BYTES}",
            )

    prepared = [asset_gates._prepare(filename, data) for filename, data in files]

    # Held across quota-check -> write -> commit so concurrent uploaders
    # serialize against the single shared 5GB pool (asset_gates docstring).
    with asset_gates._QUOTA_LOCK:
        new_bytes = sum(len(item["stored_bytes"]) for item in prepared)
        if new_bytes and total_usage_bytes(str(user_id)) + new_bytes > LIMIT_BYTES:
            raise ApiError(
                413,
                "QUOTA_EXCEEDED",
                "upload would exceed the 5 GB per-user quota; free up space first",
            )

        pool_dir = os.path.join(storage_root(), str(user_id), POOL)
        os.makedirs(pool_dir, exist_ok=True)
        written: list = []
        rows: list = []
        try:
            for item in prepared:
                stored_name = uuid.uuid4().hex + item["ext"]
                relative_path = os.path.join(str(user_id), POOL, stored_name)
                abs_path = os.path.join(storage_root(), relative_path)
                tmp_path = abs_path + ".part"
                try:
                    with open(tmp_path, "wb") as fh:
                        fh.write(item["stored_bytes"])
                    os.replace(tmp_path, abs_path)
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                written.append(abs_path)
                row = TrainingImage(
                    user_id=user_id,
                    file_name=item["filename"],
                    width=item["width"],
                    height=item["height"],
                    size_bytes=len(item["stored_bytes"]),
                    status="uploaded",
                    relative_path=relative_path,
                    sha256=item["sha256"],
                    uploaded_at=datetime.now(timezone.utc).isoformat(),
                )
                session.add(row)
                rows.append(row)
            session.commit()
        except Exception:
            # Zero half-finished state: roll back the DB rows and remove
            # every file this batch already landed on disk.
            session.rollback()
            for path in written:
                if os.path.exists(path):
                    os.remove(path)
            raise
    return rows



def _lock_chain_hit(session: Session, image_id: int) -> bool:
    """True when the image feeds a sample pinned by a queued/running style run.

    Bootstrap rule (dispatch T64 section 2): if either T69 table is absent
    the chain cannot exist, so the delete is allowed. T69 must create the
    tables with these exact names and columns.
    """
    inspector = inspect(session.get_bind())
    if not (
        inspector.has_table("style_samples") and inspector.has_table("style_runs")
    ):
        return False
    sample_ids = {
        row[0]
        for row in session.execute(
            text(
                "SELECT sample_id FROM style_samples "
                "WHERE source_training_image_id = :image_id"
            ),
            {"image_id": image_id},
        )
    }
    if not sample_ids:
        return False
    placeholders = ", ".join(f"'{status}'" for status in ACTIVE_RUN_STATUSES)
    runs = session.execute(
        text(f"SELECT sample_ids FROM style_runs WHERE status IN ({placeholders})")
    )
    for (raw,) in runs:
        try:
            pinned = set(json.loads(raw or "[]"))
        except (TypeError, ValueError):
            continue
        if pinned & sample_ids:
            return True
    return False



def delete_image(session: Session, user_id: int, image_id: int) -> None:
    """Hard-delete one training image (row + file) with audit.

    Anti-enumeration: rows owned by other users answer 404
    TRAINING_IMAGE_NOT_FOUND, identical to a missing row (patch-C section
    12). Hard deletes always write an audit row (contract v0.2 section 8,
    line 245). The DB row commits before the file is removed, so a crash
    can only leave a harmless orphan file, never a row pointing at a
    missing file.
    """
    row = (
        session.query(TrainingImage)
        .filter(
            TrainingImage.image_id == image_id,
            TrainingImage.user_id == user_id,
        )
        .first()
    )
    if row is None:
        raise ApiError(404, "TRAINING_IMAGE_NOT_FOUND", "Training image not found")
    if _lock_chain_hit(session, image_id):
        audit(
            session,
            action="training_image_delete",
            actor=str(user_id),
            object_ref=f"training_images/{image_id}",
            result="fail",
            error_code="SAMPLE_LOCKED",
        )
        session.commit()
        raise ApiError(
            409,
            "SAMPLE_LOCKED",
            "Sample locked",
            "image is referenced by a queued or running style run",
        )
    relative_path = row.relative_path
    session.delete(row)
    audit(
        session,
        action="training_image_delete",
        actor=str(user_id),
        object_ref=f"training_images/{image_id}",
        result="ok",
    )
    session.commit()
    abs_path = os.path.join(storage_root(), relative_path)
    if os.path.exists(abs_path):
        os.remove(abs_path)

