"""Style domain service (contract v0.2 section 1.4/1.8, patch-B CC-2/CC-3,
patch-C section 9 CC-16).

Semantics pinned by dispatch T69:
- ``style_profile.status`` flow: ``draft`` -> (approve|reject); publish from a
  style run produces ``pending_review`` -> (approve|reject) (patch-C 9.1/9.2).
- Reference gate (v0.2 line 77 + patch-C 9.4): only ``approved`` may be
  referenced by render-jobs; ``assert_style_approved`` is the carrier
  function. The render-jobs binding itself belongs to T66.
- Run mutability (patch-B CC-2 line 22): config writable only in ``created``;
  sample removal allowed in ``created``/``queued``; sample set locked at run
  creation (line 24).
- Publish gate (patch-B CC-3 line 34): requires a ``verdict=pass`` blind
  review, else 409 RUN_BLIND_REVIEW_REQUIRED (0-3 item protocol unchanged,
  S09 will extend it - never silently).
- Publish status precondition (S10 minimal contract revision, registered -
  NOT a silent overturn): publishing is now restricted to ``succeeded`` runs
  with a real, hash-verified artifact binding. The previous reading
  (``created``/``queued``/``running`` allowed) predates any engine able to
  produce artifacts; queued/running publishes now answer 400
  RUN_NOT_PUBLISHABLE (S10 task card: "queued/running run 发布=400"). Legacy
  t69 probe fixtures were adapted in the same registered revision.
- S10 publish binds a real artifact (manifest + per-file sha256 recomputed)
  into ``style_run_artifacts`` (additive table, migration requirement M-2);
  quality gate is marked ``BLOCKED_OWNER`` on the produced profile until a
  real blind evaluation (S08/S09) exists - engineering chain verified, the
  human quality gate is NOT auto-passed.
- Anti-enumeration (patch-B section 3 + v0.2.1 section 12 spirit): rows owned
  by other users answer the same 404 as missing rows.
- No real training/inference here: engine-side run transitions are carried
  by ``set_run_status`` so probes can drive the worker role directly.
"""

import hashlib
import json
from pathlib import Path
import math
import os
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import ApiError
from app.core import storage
from app.domains.assets_upload.models import Asset
from app.domains.styles import runstore
from app.domains.styles.models import (
    StyleProfile,
    StyleRun,
    StyleRunArtifact,
    StyleRunBlindReview,
    StyleSample,
)
from app.domains.training_sets.models import TrainingImage, TrainingSample

PROFILE_STATUSES = ("draft", "pending_review", "approved", "rejected")
RUN_STATUSES = ("created", "queued", "running", "succeeded", "failed", "cancelled")
APPROVABLE_FROM = ("draft", "pending_review")
CANCELLABLE_FROM = ("created", "queued", "running")
SAMPLE_DELETABLE_FROM = ("created", "queued")
PUBLISHABLE_FROM = ("succeeded",)  # S10 revision: succeeded only (see docstring)
M2_SOURCE_TYPE = "M2_ADAPTER"
MAX_PAGE_SIZE = 100
ARTIFACT_RECEIPT_PREFIX = "style_run_artifacts/"
EVAL_RECEIPT_FILENAME = "eval_receipt.json"  # S08 future producer contract
_P4_DRAFT_PATH_RE = re.compile(r"^users/(?P<uid>\d+)/styles/drafts/(?P<name>[0-9a-f]{16}\.png)$")
_P4_BBOX_KEYS = ("x_norm", "y_norm", "w_norm", "h_norm")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def profile_dict(row: StyleProfile) -> dict:
    """Serialize one profile row to the contract v0.2 line 204 field shape."""
    return {
        "style_id": row.style_id,
        "name": row.name,
        "source_type": row.source_type,
        "status": row.status,
        "quality_status": row.quality_status,
        "coverage": row.coverage,
        "model_version": row.model_version,
        "receipt_id": row.receipt_id,
        "font_url": row.font_url,
        "enabled": bool(row.enabled),
        "reject_reason": row.reject_reason,
        "created_at": row.created_at,
    }


def sample_dict(row: StyleSample) -> dict:
    """Serialize one sample row (contract v0.2 line 205 + T64 lock-chain link)."""
    return {
        "sample_id": row.sample_id,
        "style_id": row.style_id,
        "source_training_image_id": row.source_training_image_id,
        "text_label": row.text_label,
        "relative_path": row.relative_path,
        "sha256": row.sha256,
        "uploaded_at": row.uploaded_at,
        "s02_candidate_id": getattr(row, "s02_candidate_id", None),
        "review_status": "candidate" if getattr(row, "s02_candidate_id", None) else None,
    }


def run_dict(row: StyleRun) -> dict:
    """Serialize one run row; sample_ids/config are decoded from their JSON storage."""
    return {
        "run_id": row.run_id,
        "status": row.status,
        "sample_ids": json.loads(row.sample_ids or "[]"),
        "config": json.loads(row.config or "{}"),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _get_owned_style(session: Session, user_id: int, style_id: int) -> StyleProfile:
    """Fetch one profile scoped to the caller; 404 for missing or foreign rows."""
    row = (
        session.query(StyleProfile)
        .filter(StyleProfile.style_id == style_id, StyleProfile.user_id == user_id)
        .first()
    )
    if row is None:
        raise ApiError(404, "STYLE_NOT_FOUND", "Style not found")
    return row


def _get_owned_run(session: Session, user_id: int, run_id: int) -> StyleRun:
    """Fetch one run scoped to the caller; 404 RUN_NOT_FOUND for missing or
    foreign rows (patch-B section 3: anti-enumeration)."""
    row = (
        session.query(StyleRun)
        .filter(StyleRun.run_id == run_id, StyleRun.user_id == user_id)
        .first()
    )
    if row is None:
        raise ApiError(404, "RUN_NOT_FOUND", "Style run not found")
    return row


def create_style(session: Session, user_id: int, source_type, name=None, font_url=None) -> StyleProfile:
    """Create a profile in ``draft``; ``source_type`` is required (v0.2 line 65)."""
    if not isinstance(source_type, str) or not source_type.strip():
        raise ApiError(400, "INVALID_REQUEST", "source_type is required")
    row = StyleProfile(
        user_id=user_id,
        name=(name.strip() if isinstance(name, str) and name.strip() else "untitled-style"),
        source_type=source_type.strip(),
        font_url=(font_url.strip() if isinstance(font_url, str) and font_url.strip() else None),
        status="draft",
        enabled=1,
        created_at=_now(),
    )
    session.add(row)
    session.flush()
    return row


def list_styles(
    session: Session,
    user_id: int,
    status=None,
    source_type=None,
    enabled=None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Caller-scoped listing with the frozen filters (v0.2 line 66).

    ``meta.total`` is the notification carrier for pending reviews
    (patch-C 9.5), so the total must be filter-accurate, not page-accurate.
    """
    if page < 1 or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ApiError(
            400,
            "INVALID_REQUEST",
            f"page must be >= 1 and page_size within 1..{MAX_PAGE_SIZE}",
        )
    query = session.query(StyleProfile).filter(StyleProfile.user_id == user_id)
    if status is not None:
        query = query.filter(StyleProfile.status == status)
    if source_type is not None:
        query = query.filter(StyleProfile.source_type == source_type)
    if enabled is not None:
        if enabled not in (0, 1):
            raise ApiError(400, "INVALID_REQUEST", "enabled filter must be 0 or 1")
        query = query.filter(StyleProfile.enabled == enabled)
    total = query.count()
    rows = (
        query.order_by(StyleProfile.style_id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [profile_dict(row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def get_style(session: Session, user_id: int, style_id: int) -> StyleProfile:
    """Detail view (v0.2 line 67); 404 STYLE_NOT_FOUND for foreign rows."""
    return _get_owned_style(session, user_id, style_id)


def approve_style(session: Session, user_id: int, style_id: int) -> StyleProfile:
    """``draft``/``pending_review`` -> ``approved`` (patch-C 9.2), audited as
    ``style_approve`` (patch-C 9.6). Already-approved is an idempotent no-op."""
    row = _get_owned_style(session, user_id, style_id)
    if row.status == "approved":
        return row
    if row.status not in APPROVABLE_FROM:
        raise ApiError(
            409,
            "INVALID_STATE",
            f"style cannot be approved from status {row.status}",
        )
    row.status = "approved"
    row.reject_reason = None
    audit(
        session,
        action="style_approve",
        actor=str(user_id),
        object_ref=f"style_profiles/{style_id}",
        result="ok",
    )
    session.flush()
    return row


def reject_style(session: Session, user_id: int, style_id: int, reason) -> StyleProfile:
    """-> ``rejected`` with a mandatory reason (patch-C 9.2), audited as
    ``style_reject``. Already-rejected is an idempotent no-op once a reason
    was supplied."""
    if not isinstance(reason, str) or not reason.strip():
        raise ApiError(400, "INVALID_REQUEST", "reason is required")
    row = _get_owned_style(session, user_id, style_id)
    if row.status == "rejected":
        return row
    row.status = "rejected"
    row.reject_reason = reason.strip()
    audit(
        session,
        action="style_reject",
        actor=str(user_id),
        object_ref=f"style_profiles/{style_id}",
        result="ok",
    )
    session.flush()
    return row


def register_sample(
    session: Session,
    user_id: int,
    style_id: int,
    *,
    source_training_image_id=None,
    text_label=None,
    relative_path=None,
    sha256=None,
    touchup_confirmed=None,
    touchup_edit_count=None,
    source_asset_id=None,
    source_bbox_norm=None,
    draft_alpha_path=None,
) -> StyleSample:
    """Register sample metadata only; the file body is the P4 extraction
    chain's job (dispatch T69 section 2). When linked to a training image,
    path/hash default to that image's stored values, and the link is what the
    T64 delete lock chain reads.

    ``touchup_confirmed`` is enforced here as well as at the route boundary,
    so trusted internal callers cannot create a sample without confirmation.

    No-op confirmation audit (patch-D §4-4, batch B5-01): when the blogger
    confirms touchup completion with an edit count of 0, the registration is
    still allowed (default放行留痕 [暂定]; blocking is an Owner ruling) but an
    audit row ``action=touchup_confirm, result=noop`` carrying the
    style/sample/asset reference is written. The audit trail lands in the
    same transaction as the sample row.
    """
    if touchup_confirmed is not True:
        raise ApiError(
            400,
            "TOUCHUP_REQUIRED",
            "sample registration requires touchup_confirmed=true",
        )
    _get_owned_style(session, user_id, style_id)
    if source_training_image_id is not None:
        image = (
            session.query(TrainingImage)
            .filter(
                TrainingImage.image_id == source_training_image_id,
                TrainingImage.user_id == user_id,
            )
            .first()
        )
        if image is None:
            raise ApiError(404, "TRAINING_IMAGE_NOT_FOUND", "Training image not found")
        # The owned TrainingImage row is the authority for both the file path
        # and hash; caller-supplied metadata must not override server values.
        relative_path = image.relative_path
        sha256 = image.sha256
    if not relative_path or not sha256:
        raise ApiError(
            400,
            "INVALID_REQUEST",
            "relative_path and sha256 are required (or link a training image)",
        )

    # P4/S02 bridge: when provenance is supplied, persist the character
    # candidate in the existing TrainingSample fact table in the same DB
    # transaction as its StyleSample. P4 touchup confirmation is never Owner
    # character confirmation.
    provenance_values = (source_asset_id, source_bbox_norm, draft_alpha_path)
    has_p4_provenance = any(value is not None for value in provenance_values)
    candidate_values = None
    if has_p4_provenance:
        if any(value is None for value in provenance_values) or source_training_image_id is None:
            raise ApiError(400, "INVALID_REQUEST", "P4 candidate requires source asset, bbox, draft path and saved training image")
        profile = _get_owned_style(session, user_id, style_id)
        if profile.source_type != "HANDWRITING_ASSET" or profile.status != "approved":
            raise ApiError(409, "STYLE_IDENTITY_UNKNOWN", "P4 training candidates require an approved HANDWRITING_ASSET profile")
        if not isinstance(text_label, str) or len(text_label) != 1 or not _is_training_ideograph(ord(text_label)):
            raise ApiError(400, "INVALID_REQUEST", "P4 training candidate label must be one CJK ideograph")
        bbox = _validate_p4_bbox(source_bbox_norm)

        source_asset = (
            session.query(Asset)
            .filter(Asset.asset_id == source_asset_id, Asset.user_id == str(user_id), Asset.enabled == 1)
            .first()
        )
        if source_asset is None or source_asset.kind not in ("cos", "sample_source") or source_asset.status != "approved":
            raise ApiError(404, "ASSET_NOT_FOUND", "Source image not found")
        source_path = _p4_owned_pool_file(user_id, "assets", source_asset.relative_path)
        if not source_path.is_file() or _sha256_path(os.fspath(source_path)) != source_asset.sha256:
            raise ApiError(409, "SOURCE_HASH_MISMATCH", "Source image is missing or no longer matches its stored SHA256")

        match = _P4_DRAFT_PATH_RE.fullmatch(str(draft_alpha_path))
        if match is None or match.group("uid") != str(user_id):
            raise ApiError(400, "INVALID_REQUEST", "draft_alpha_path must reference this user's extraction draft")
        draft_path = storage.draft_path(user_id, match.group("name"))
        if not draft_path.is_file():
            raise ApiError(400, "INVALID_REQUEST", "P4 extraction draft no longer exists")
        draft_sha = _sha256_path(os.fspath(draft_path))

        image = (
            session.query(TrainingImage)
            .filter(TrainingImage.image_id == source_training_image_id, TrainingImage.user_id == user_id)
            .first()
        )
        if image is None:
            raise ApiError(404, "TRAINING_IMAGE_NOT_FOUND", "Training image not found")
        image_path = _p4_owned_pool_file(user_id, "training_images", image.relative_path)
        if not image_path.is_file():
            raise ApiError(409, "TRAINING_IMAGE_NOT_FOUND", "Saved P4 image is missing")
        final_sha = _sha256_path(os.fspath(image_path))
        if final_sha != image.sha256 or final_sha != sha256:
            raise ApiError(409, "SOURCE_HASH_MISMATCH", "Saved P4 image hash does not match the image and StyleSample metadata")
        if (image.width, image.height) != (128, 128):
            raise ApiError(400, "INVALID_REQUEST", "P4 candidate image must be the frozen 128x128 glyph")
        if not isinstance(touchup_edit_count, int) or isinstance(touchup_edit_count, bool) or touchup_edit_count < 0:
            raise ApiError(400, "INVALID_REQUEST", "touchup_edit_count must be a non-negative integer")
        candidate_values = {
            "source_asset": source_asset,
            "image": image,
            "bbox": bbox,
            "draft_alpha_path": str(draft_alpha_path),
            "draft_alpha_sha256": draft_sha,
            "final_alpha_sha256": final_sha,
            "touchup_edit_count": touchup_edit_count,
        }

    row = StyleSample(
        style_id=style_id,
        source_training_image_id=source_training_image_id,
        text_label=text_label if isinstance(text_label, str) else None,
        relative_path=relative_path,
        sha256=sha256,
        uploaded_at=_now(),
    )
    session.add(row)
    session.flush()
    if candidate_values is not None:
        now = _now()
        source = candidate_values["source_asset"]
        image = candidate_values["image"]
        candidate = TrainingSample(
            sample_id=f"ss_p4_{uuid.uuid4().hex}",
            user_id=user_id,
            style_id=str(style_id),
            source_asset_id=source.asset_id,
            glyph_path=image.relative_path,
            text_label=text_label,
            char_unicode=f"U+{ord(text_label):04X}",
            text_region_bbox_norm=json.dumps(candidate_values["bbox"], ensure_ascii=False, allow_nan=False),
            output_size=128,
            extracted_by="p4_touchup",
            touchup_confirmed=True,
            draft_alpha_path=candidate_values["draft_alpha_path"],
            confirmed_alpha_path=image.relative_path,
            created_at=now,
            source_training_image_id=image.image_id,
            style_sample_id=row.sample_id,
            source_asset_sha256=source.sha256,
            source_asset_width=source.width,
            source_asset_height=source.height,
            source_family_id=f"sha256:{source.sha256}",
            source_training_image_sha256=image.sha256,
            draft_alpha_sha256=candidate_values["draft_alpha_sha256"],
            final_alpha_sha256=candidate_values["final_alpha_sha256"],
            touchup_edit_count=candidate_values["touchup_edit_count"],
            touchup_confirmed_at=now,
            review_status="candidate",
            provenance_version="p4-s02/v1",
        )
        session.add(candidate)
        session.flush()
        row.s02_candidate_id = candidate.sample_id
    if isinstance(touchup_edit_count, int) and not isinstance(touchup_edit_count, bool) and touchup_edit_count == 0:
        object_ref = f"styles/{style_id}/samples/{row.sample_id}"
        if source_training_image_id is not None:
            object_ref += f" asset=training_images/{source_training_image_id}"
        audit(
            session,
            action="touchup_confirm",
            actor=str(user_id),
            object_ref=object_ref,
            result="noop",
        )
        # patch-D §4-4 also asks for a server-side warning next to the audit
        # row. Surfacing it in the response envelope needs a styles-router
        # passthrough that is OUTSIDE this task's write face (review FAIL
        # 2026-09-24, rolled back byte-identical to the pre-write backup), so
        # the warning rides on the row instance (plain attribute, not a
        # column) and stays dormant until the orchestrator authorizes the
        # router line. The audit row below is the enforced part within this
        # write face.
        row.touchup_noop = True
    session.flush()
    return row


def _sha256_path(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_p4_bbox(value) -> dict:
    if not isinstance(value, dict) or set(value) != set(_P4_BBOX_KEYS):
        raise ApiError(400, "INVALID_REQUEST", "source_bbox_norm needs x_norm/y_norm/w_norm/h_norm")
    out = {}
    for key in _P4_BBOX_KEYS:
        item = value[key]
        if not isinstance(item, (int, float)) or isinstance(item, bool) or not math.isfinite(item):
            raise ApiError(400, "INVALID_REQUEST", f"source_bbox_norm.{key} must be a finite number")
        out[key] = float(item)
    if not (0 <= out["x_norm"] <= 1 and 0 <= out["y_norm"] <= 1
            and 0 < out["w_norm"] <= 1 and 0 < out["h_norm"] <= 1
            and out["x_norm"] + out["w_norm"] <= 1
            and out["y_norm"] + out["h_norm"] <= 1):
        raise ApiError(400, "INVALID_REQUEST", "source_bbox_norm must stay within [0,1]")
    return out


def _p4_owned_pool_file(user_id: int, pool: str, relative_path: str):
    """Resolve a stored P4 file only inside the caller's expected storage pool."""
    normalized = str(relative_path).replace("\\", "/")
    parts = normalized.split("/")
    if len(parts) < 3 or parts[:2] != [str(user_id), pool] or any(
        part in ("", ".", "..") or "\x00" in part for part in parts
    ):
        raise ApiError(409, "SAMPLE_PROVENANCE_INVALID", "Stored P4 path is outside the caller's pool")
    root = storage.storage_root()
    return storage.ensure_within_root(root.joinpath(*parts), root)


def _is_training_ideograph(cp: int) -> bool:
    return (0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF
            or 0xF900 <= cp <= 0xFAFF or 0x20000 <= cp <= 0x2EBEF)


def list_samples(session: Session, user_id: int, style_id: int) -> list:
    """Samples of one owned style; 404 STYLE_NOT_FOUND for foreign styles."""
    _get_owned_style(session, user_id, style_id)
    rows = (
        session.query(StyleSample)
        .filter(StyleSample.style_id == style_id)
        .order_by(StyleSample.sample_id.asc())
        .all()
    )
    return [sample_dict(row) for row in rows]


def create_run(session: Session, user_id: int, sample_ids, config=None, idempotency_key=None) -> StyleRun:
    if isinstance(config, dict) and '_server_continuation' in config:
        raise ApiError(400, 'INVALID_REQUEST', 'Continuation metadata is server owned')
    if idempotency_key is None:
        return _create_run_unkeyed(session,user_id,sample_ids,config)
    if not isinstance(idempotency_key,str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}",idempotency_key):
        raise ApiError(400,"INVALID_REQUEST","Invalid Idempotency-Key")
    from .models import StyleRunCreateKey
    from sqlalchemy.exc import IntegrityError
    fingerprint=runstore.canonical_sha256({"sample_ids":sample_ids,"config":config or {}})
    def existing():
        key=session.get(StyleRunCreateKey,(user_id,idempotency_key))
        if key is None:return None
        if key.request_sha256!=fingerprint:
            raise ApiError(409,"IDEMPOTENCY_CONFLICT","Same key cannot be reused with a different request")
        return _get_owned_run(session,user_id,key.run_id)
    previous=existing()
    if previous is not None:return previous
    try:
        with session.begin_nested():
            run=_create_run_unkeyed(session,user_id,sample_ids,config)
            session.add(StyleRunCreateKey(user_id=user_id,request_key=idempotency_key,
                                         request_sha256=fingerprint,run_id=run.run_id))
            session.flush()
        return run
    except IntegrityError:
        previous=existing()
        if previous is not None:return previous
        raise ApiError(409,"IDEMPOTENCY_PENDING","Another creation is in progress; retry the same request and key")


def _assert_m2_product_runtime(session: Session, user_id: int, config: dict) -> None:
    """Read-only asset/identity checks; never import ML or launch a probe."""
    from engine.style.m2_adapter.runtime import runtime_python, verify_runtime
    from engine.style.m2_adapter.runtime_variants import binding_for_config
    from engine.style.dataset.binding import verify_snapshot_binding
    from engine.style.dataset.schema import DatasetError
    profile = _get_owned_style(session, user_id, config.get("style_id"))
    if profile.source_type != "HANDWRITING_ASSET" or profile.status != "approved":
        raise ApiError(409, "STYLE_NOT_APPROVED", "M2 requires an approved, owned handwriting profile")
    try:
        snapshot = verify_snapshot_binding(config.get("snapshot_dir"), config.get("snapshot_sha256"), base_dir=Path(__file__).resolve().parents[3])
        if str(snapshot["style_id"]) != str(profile.style_id):
            raise ValueError("S03 snapshot style differs from the authenticated owned profile")
        selected = binding_for_config(Path(__file__).resolve().parents[3], config)
        runtime = verify_runtime(config.get("runtime_manifest_path"), config.get("fd_repo"))
        python = Path(selected["python"]) if selected else runtime_python(config.get("runtime_manifest_path"))
        if not python.is_file():
            raise ValueError("M2 ML interpreter is unavailable")
        if (config.get("runtime_identity_sha256") != runtime["identity_sha256"]
                or config.get("base_npz_sha256") != runtime["manifest"]["base_npz_sha256"]):
            raise ValueError("M2 run must bind the verified runtime identity and base model hashes")
    except (OSError, ValueError, KeyError, TypeError, DatasetError) as exc:
        raise ApiError(409, "RUNTIME_UNAVAILABLE", str(exc)) from exc


def _guard_windows_training(session,user_id,config):
    from .windows_scr_training_api import reject_unqualified_launch
    from .user_scr_product import UserSCRProductError
    try:reject_unqualified_launch(session,user_id,config)
    except UserSCRProductError as exc:raise ApiError(404 if exc.code=='STYLE_NOT_FOUND' else 409,exc.code,str(exc)) from exc

def _canonical_windows_training(session,user_id,config,ids,*,existing=False):
    from .windows_scr_product import canonical
    from .user_scr_product import UserSCRProductError
    try:return canonical(session,user_id,config,ids,require_existing=existing)
    except (UserSCRProductError,ValueError,OSError,KeyError,TypeError) as exc:
        code=getattr(exc,'code','STALE_CACHE')
        raise ApiError(404 if code=='STYLE_NOT_FOUND' else 409,code,str(exc)) from exc


def _create_run_unkeyed(session: Session, user_id: int, sample_ids, config=None) -> StyleRun:
    """Create a run in ``created`` with the sample set locked at creation
    (patch-B CC-2 line 24: no additions afterwards, removal only)."""
    if not isinstance(sample_ids, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in sample_ids
    ):
        raise ApiError(400, "INVALID_REQUEST", "sample_ids must be an array of integers")
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ApiError(400, "INVALID_REQUEST", "config must be an object")
    _guard_windows_training(session,user_id,config)
    from engine.style.m2_adapter.windows_training_recipe import RECIPE_ID as WINDOWS_RECIPE
    if config.get('recipe_id')==WINDOWS_RECIPE:
        config,sample_ids=_canonical_windows_training(session,user_id,config,sample_ids)
    from engine.style.m2_adapter.user_scr_contract import RECIPE_ID as USER_SCR_RECIPE_ID
    if config.get('recipe_id') == USER_SCR_RECIPE_ID:
        from .user_scr_product import canonical_user_scr_config, UserSCRProductError
        try:config,sample_ids=canonical_user_scr_config(session,user_id,config,sample_ids)
        except UserSCRProductError as exc:raise ApiError(404 if exc.code=='STYLE_NOT_FOUND' else 409,exc.code,str(exc)) from exc
    if config.get('engine')=='m2':
        from engine.style.m2_adapter.runtime_variants import binding_for_config
        try:binding_for_config(Path(__file__).resolve().parents[3],config)
        except (OSError,ValueError,KeyError) as exc:raise ApiError(409,'RUNTIME_UNAVAILABLE',str(exc))
    from .m2_product_config import canonical_control_config
    try:config=canonical_control_config(config)
    except (OSError,ValueError,KeyError,TypeError) as exc:raise ApiError(409,'CONTROL_RECIPE_UNAVAILABLE',str(exc))
    if config.get('engine') == 'm2' and config.get('snapshot_dir') and config.get('recipe_id') not in (USER_SCR_RECIPE_ID,WINDOWS_RECIPE):
        _assert_m2_product_runtime(session, user_id, config)
    from engine.style.visual_review.policy import bind_threshold_policy
    try:config=bind_threshold_policy(config,user_id)
    except ValueError as exc:raise ApiError(400,"POLICY_CONFIG_MISMATCH",str(exc))
    now = _now()
    row = StyleRun(
        user_id=user_id,
        status="created",
        sample_ids=json.dumps(sample_ids),
        config=json.dumps(config),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def get_run(session: Session, user_id: int, run_id: int) -> StyleRun:
    """Run status view; 404 RUN_NOT_FOUND covers foreign rows."""
    return _get_owned_run(session, user_id, run_id)


def start_run(session: Session, user_id: int, run_id: int) -> StyleRun:
    """``created`` -> ``queued`` (v0.2 line 148). Other states are not mutable."""
    row = _get_owned_run(session, user_id, run_id)
    if row.status in ('queued', 'running'):
        return row
    if row.status != "created":
        raise ApiError(
            409, "RUN_NOT_MUTABLE", f"run in status {row.status} cannot be started"
        )
    config=json.loads(row.config or '{}')
    if config.get('_server_continuation', {}).get('mode') == 'resume':
        from .run_recovery import assert_resume_qualified
        assert_resume_qualified(config)
    _guard_windows_training(session,user_id,config)
    from engine.style.m2_adapter.windows_training_recipe import RECIPE_ID as WINDOWS_RECIPE
    if config.get('recipe_id')==WINDOWS_RECIPE:
        _canonical_windows_training(session,user_id,config,json.loads(row.sample_ids or '[]'),existing=True)
    if config.get("engine") == "m2" and os.environ.get("YY_STYLE_WORKER", "1").lower() in ("0", "false", "off"):
        raise ApiError(409, "EXECUTION_DISABLED", "M2 execution is disabled in this environment; no training was queued")
    from engine.style.m2_adapter.user_scr_contract import RECIPE_ID as USER_SCR_RECIPE_ID
    if config.get('recipe_id') == USER_SCR_RECIPE_ID:
        from .user_scr_product import canonical_user_scr_config, UserSCRProductError
        try:canonical_user_scr_config(session,user_id,config,json.loads(row.sample_ids or '[]'),require_existing=True)
        except UserSCRProductError as exc:raise ApiError(404 if exc.code=='STYLE_NOT_FOUND' else 409,exc.code,str(exc)) from exc
    from .m2_product_config import canonical_control_config
    try:canonical_control_config(config,require_existing=True)
    except (OSError,ValueError,KeyError,TypeError) as exc:raise ApiError(409,'CONTROL_RECIPE_UNAVAILABLE',str(exc))
    if config.get('engine')=='m2':
        from engine.style.m2_adapter.runtime_variants import binding_for_config
        try:binding_for_config(Path(__file__).resolve().parents[3],config)
        except (OSError,ValueError,KeyError) as exc:raise ApiError(409,'RUNTIME_UNAVAILABLE',str(exc))
        if config.get('recipe_id') not in (USER_SCR_RECIPE_ID,WINDOWS_RECIPE):
            _assert_m2_product_runtime(session, user_id, config)
    changed = session.query(StyleRun).filter_by(run_id=run_id, user_id=user_id, status='created').update(
        {'status': 'queued', 'updated_at': _now()}, synchronize_session=False)
    session.flush()
    session.refresh(row)
    if not changed and row.status not in ('queued', 'running'):
        raise ApiError(409, 'RUN_NOT_MUTABLE', 'Run changed while it was being started')
    return row


def cancel_run(session: Session, user_id: int, run_id: int) -> StyleRun:
    """``queued``/``running`` -> ``cancelled`` (v0.2 line 149)."""
    row = _get_owned_run(session, user_id, run_id)
    if row.status == 'cancelled':
        return row
    previous_status = row.status
    m2_draft = row.status == 'created' and json.loads(row.config or '{}').get('engine') == 'm2'
    if row.status not in CANCELLABLE_FROM and not m2_draft:
        raise ApiError(
            409, "RUN_NOT_MUTABLE", f"run in status {row.status} cannot be cancelled"
        )
    changed = session.query(StyleRun).filter(StyleRun.run_id==run_id, StyleRun.user_id==user_id,
        StyleRun.status.in_(CANCELLABLE_FROM)).update({'status':'cancelled', 'updated_at':_now()}, synchronize_session=False)
    session.flush()
    session.refresh(row)
    if not changed and row.status != 'cancelled':
        raise ApiError(409, 'RUN_NOT_MUTABLE', 'Run already completed before cancellation')
    if changed and previous_status in ('created', 'queued'):
        from .models import StyleRunExecution
        if session.get(StyleRunExecution, run_id) is None:
            from .run_recovery import record_execution
            record_execution(session, run_id, phase='ended')
    return row


def update_run_config(session: Session, user_id: int, run_id: int, config) -> StyleRun:
    """Config writable only in ``created`` (patch-B CC-2 line 22)."""
    row = _get_owned_run(session, user_id, run_id)
    if '_server_continuation' in json.loads(row.config or '{}') or isinstance(config, dict) and '_server_continuation' in config:
        raise ApiError(409, 'RUN_NOT_MUTABLE', 'Resume attempts retain their checkpoint config')
    if row.status != "created":
        raise ApiError(
            409, "RUN_NOT_MUTABLE", f"config is writable only in created, not {row.status}"
        )
    if not isinstance(config, dict):
        raise ApiError(400, "INVALID_REQUEST", "config must be an object")
    _guard_windows_training(session,user_id,config)
    from engine.style.m2_adapter.windows_training_recipe import RECIPE_ID as WINDOWS_RECIPE
    if config.get('recipe_id')==WINDOWS_RECIPE:
        config,locked_ids=_canonical_windows_training(session,user_id,config,json.loads(row.sample_ids or '[]'))
        if locked_ids!=json.loads(row.sample_ids or '[]'):
            raise ApiError(409,'STALE_CACHE','Created run cannot change its canonical sample set')
    from engine.style.m2_adapter.user_scr_contract import RECIPE_ID as USER_SCR_RECIPE_ID
    if config.get('recipe_id') == USER_SCR_RECIPE_ID:
        from .user_scr_product import canonical_user_scr_config, UserSCRProductError
        try:config,locked_ids=canonical_user_scr_config(session,user_id,config,json.loads(row.sample_ids or '[]'))
        except UserSCRProductError as exc:raise ApiError(404 if exc.code=='STYLE_NOT_FOUND' else 409,exc.code,str(exc)) from exc
        if locked_ids != json.loads(row.sample_ids or '[]'):
            raise ApiError(409,'STALE_CACHE','A created run cannot change its locked canonical snapshot sample set')
    if config.get('engine')=='m2':
        from engine.style.m2_adapter.runtime_variants import binding_for_config
        try:binding_for_config(Path(__file__).resolve().parents[3],config)
        except (OSError,ValueError,KeyError) as exc:raise ApiError(409,'RUNTIME_UNAVAILABLE',str(exc))
    from .m2_product_config import canonical_control_config
    try:config=canonical_control_config(config)
    except (OSError,ValueError,KeyError,TypeError) as exc:raise ApiError(409,'CONTROL_RECIPE_UNAVAILABLE',str(exc))
    from engine.style.visual_review.policy import bind_threshold_policy
    try:config=bind_threshold_policy(config,user_id)
    except ValueError as exc:raise ApiError(400,"POLICY_CONFIG_MISMATCH",str(exc))
    row.config = json.dumps(config)
    row.updated_at = _now()
    session.flush()
    return row


def delete_run_sample(session: Session, user_id: int, run_id: int, sample_id: int) -> None:
    """Remove one sample from the locked set; allowed in ``created``/``queued``
    (patch-B CC-2 lines 19/22), refused with RUN_NOT_MUTABLE afterwards."""
    row = _get_owned_run(session, user_id, run_id)
    if '_server_continuation' in json.loads(row.config or '{}'):
        raise ApiError(409, 'RUN_NOT_MUTABLE', 'Resume attempts retain their checkpoint sample set')
    if row.status not in SAMPLE_DELETABLE_FROM:
        raise ApiError(
            409,
            "RUN_NOT_MUTABLE",
            f"samples are removable only in created/queued, not {row.status}",
        )
    ids = json.loads(row.sample_ids or "[]")
    if sample_id not in ids:
        raise ApiError(404, "SAMPLE_NOT_FOUND", "sample is not part of this run")
    ids.remove(sample_id)
    row.sample_ids = json.dumps(ids)
    row.updated_at = _now()
    session.flush()


def submit_blind_review(session: Session, user_id: int, run_id: int, items, verdict) -> StyleRunBlindReview:
    """Record one blind review (patch-B CC-3 line 32). Multiple reviews may
    accumulate; publish only asks whether any ``pass`` exists (line 34)."""
    _get_owned_run(session, user_id, run_id)
    if verdict not in ("pass", "fail"):
        raise ApiError(400, "INVALID_REQUEST", "verdict must be pass or fail")
    if not isinstance(items, list) or not all(
        isinstance(item, dict)
        and "sample_key" in item
        and isinstance(item.get("score"), int)
        and not isinstance(item.get("score"), bool)
        and 0 <= item["score"] <= 3
        for item in items
    ):
        raise ApiError(
            400,
            "INVALID_REQUEST",
            "items must be an array of {sample_key, score 0-3}",
        )
    row = StyleRunBlindReview(
        run_id=run_id,
        verdict=verdict,
        items=json.dumps(items),
        created_at=_now(),
    )
    session.add(row)
    session.flush()
    return row


def publish_run(session: Session, user_id: int, run_id: int) -> tuple[StyleRunArtifact, StyleProfile]:
    """S10 publish: bind a real artifact and create the ``pending_review``
    profile (patch-C 9.1) in one transaction.

    Gates, in order (all negative paths answer explicit problem codes - never
    a fabricated profile):
    1. ownership 404 RUN_NOT_FOUND;
    2. run status must be ``succeeded`` -> else 400 RUN_NOT_PUBLISHABLE
       (S10 revision: queued/running/created/failed/cancelled all refuse);
    3. an authenticated owner-direct acceptance with current objective gates,
       or at least one ``verdict=pass`` blind review -> else 409
       RUN_BLIND_REVIEW_REQUIRED (patch-B CC-3 line 34, 0-3 protocol intact);
    4. the run's artifact manifest must exist and every bound file must
       recompute to its recorded sha256 -> else 400 ARTIFACT_MISSING /
       ARTIFACT_INVALID.

    Idempotent: a second publish of the same run returns the SAME artifact +
    profile (audit result ``idempotent_hit``), never a second row/profile.
    Rollback only flips ``active`` pointers; files are never deleted.

    The produced profile carries ``quality_status=BLOCKED_OWNER`` (engineering
    chain verified; the real blind-evaluation quality gate S08/S09 has NOT
    been passed - publish does NOT auto-pass it) and ``receipt_id`` pointing
    at the artifact (stable ID/version locator for the S11 render consumer).
    """
    row = _get_owned_run(session, user_id, run_id)
    if row.status not in PUBLISHABLE_FROM:
        raise ApiError(
            400,
            "RUN_NOT_PUBLISHABLE",
            f"run in status {row.status} cannot be published (only succeeded runs with a "
            "verified artifact can)",
        )
    passed = (
        session.query(StyleRunBlindReview)
        .filter(
            StyleRunBlindReview.run_id == run_id,
            StyleRunBlindReview.verdict == "pass",
        )
        .count()
    )
    from engine.style.visual_review.policy import human_waiver
    waiver = human_waiver(user_id)
    request_config = json.loads(row.config or "{}")
    ai_waived = bool(waiver and request_config.get("engine") == "m2")
    owner_direct = False
    owner_readiness = None
    owner_path = runstore.run_dir(user_id,run_id)/'receipts/owner-direct-visual.json'
    if request_config.get('engine')=='m2' and owner_path.is_file():
        owner_manifest=_load_verified_manifest(row)
        from engine.style.visual_review.readiness import check_run_readiness
        owner_readiness=check_run_readiness(runstore.run_dir(user_id,run_id),owner_manifest,row.status,run_config=request_config,user_id=user_id)
        owner_direct=owner_readiness.get('status')=='PUBLISH_READY_OWNER_DIRECT'
        if owner_readiness.get('OWNER_DIRECT_VISUAL_ACCEPTANCE')=='ACCEPTED' and not owner_direct:
            raise ApiError(409,'RUN_ENGINEERING_GATES_REQUIRED','Direct owner appearance accepted; current training/inference/hash/objective evaluation gates remain blocked')
    if ai_waived:
        waiver_manifest = _load_verified_manifest(row)
        ai_waived = waiver_manifest.get("engine_mode") == "M2_ADAPTER"
    if passed == 0 and not ai_waived and not owner_direct:
        raise ApiError(
            409,
            "RUN_BLIND_REVIEW_REQUIRED",
            "a blind review with verdict=pass is required before publish",
        )

    existing = (
        session.query(StyleRunArtifact)
        .filter(StyleRunArtifact.run_id == run_id, StyleRunArtifact.user_id == user_id)
        .first()
    )
    if existing is not None:
        profile = session.get(StyleProfile, existing.profile_id) if existing.profile_id else None
        if profile is None:
            raise ApiError(500, "ARTIFACT_BROKEN", "published artifact lost its profile link")
        audit(session, action="style_publish", actor=str(user_id),
              object_ref=f"style_runs/{run_id}", result="idempotent_hit")
        session.flush()
        return existing, profile

    manifest = _load_verified_manifest(row)
    if manifest.get("engine_mode") == "M2_ADAPTER":
        from engine.style.visual_review.readiness import check_run_readiness
        readiness = check_run_readiness(runstore.run_dir(user_id, run_id), manifest, row.status, run_config=request_config, user_id=user_id)
        if readiness["status"] not in ("PUBLISH_READY_PENDING_OWNER", "PUBLISH_READY_AI_AUTHORIZED", "PUBLISH_READY_OWNER_DIRECT"):
            raise ApiError(409, "RUN_ENGINEERING_GATES_REQUIRED",
                           "M2 training/inference/AI visual/hash/evaluation gates must pass before publish")
    config = json.loads(row.config or "{}")

    # single active pointer per user: activating this artifact deactivates others
    session.query(StyleRunArtifact).filter(
        StyleRunArtifact.user_id == user_id, StyleRunArtifact.active == 1
    ).update({"active": 0}, synchronize_session=False)

    artifact = StyleRunArtifact(
        run_id=run_id,
        user_id=user_id,
        profile_id=None,
        engine_mode=manifest["engine_mode"],
        engine_id=manifest["engine_id"],
        engine_version=manifest["engine_version"],
        base_ckpt_sha256=json.dumps(manifest["base_ckpt_sha256"], ensure_ascii=False)
        if isinstance(manifest["base_ckpt_sha256"], dict) else str(manifest["base_ckpt_sha256"]),
        dataset_snapshot_sha256=manifest["dataset_snapshot_sha256"],
        config_sha256=manifest["config_sha256"],
        metrics_receipt_sha256=manifest["metrics_receipt_sha256"],
        relative_path=_manifest_relative_path(row),
        sha256=manifest["_manifest_sha256"],
        quality_status="OWNER_DIRECT_VISUAL_ACCEPTED" if owner_direct else "AI_ACCEPTED_USER_AUTHORIZED" if ai_waived else "BLOCKED_OWNER",
        active=1,
        created_at=_now(),
    )
    session.add(artifact)
    session.flush()

    name = config.get("name")
    profile = StyleProfile(
        user_id=user_id,
        name=name.strip() if isinstance(name, str) and name.strip() else f"style-run-{run_id}",
        source_type=M2_SOURCE_TYPE,
        status="approved" if owner_direct or ai_waived else "pending_review",
        quality_status="OWNER_DIRECT_VISUAL_ACCEPTED" if owner_direct else "AI_ACCEPTED_USER_AUTHORIZED" if ai_waived else "BLOCKED_OWNER",
        model_version=artifact.engine_version,
        receipt_id=f"{ARTIFACT_RECEIPT_PREFIX}{artifact.artifact_id}",
        enabled=1,
        created_at=_now(),
    )
    session.add(profile)
    session.flush()
    artifact.profile_id = profile.style_id
    audit(
        session,
        action="style_publish",
        actor=str(user_id),
        object_ref=f"style_runs/{run_id}",
        result=f"artifact={artifact.artifact_id} profile={profile.style_id} quality={artifact.quality_status}"
               + (f" review_mode=owner_direct_visual_acceptance receipt={owner_readiness['owner_direct_acceptance']['receipt_sha256']} actor=authenticated_owner blind_review=false" if owner_direct else f" human_waiver={waiver['human_blind_review']['authorization']['message_id']} actor=ai_user_authorized" if ai_waived else ""),
    )
    session.flush()
    return artifact, profile


def _manifest_relative_path(run: StyleRun) -> str:
    return runstore.artifact_manifest_path(run.user_id, run.run_id).relative_to(
        runstore.run_dir(run.user_id, run.run_id)
    ).as_posix()


def _load_verified_manifest(run: StyleRun) -> dict:
    """Read + fully recompute-verify the run's artifact manifest. 400 on any
    mismatch (publish must bind real, intact artifacts only)."""
    base_dir = runstore.run_dir(run.user_id, run.run_id)
    manifest_path = runstore.artifact_manifest_path(run.user_id, run.run_id)
    if not manifest_path.is_file():
        raise ApiError(400, "ARTIFACT_MISSING",
                       "run produced no artifact manifest (publish binds real artifacts only)")
    manifest = runstore.read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ApiError(400, "ARTIFACT_INVALID", "artifact manifest is not readable JSON")
    for key in ("engine_mode", "engine_id", "engine_version", "base_ckpt_sha256",
                "dataset_snapshot_sha256", "config_sha256",
                "metrics_receipt_relative_path", "metrics_receipt_sha256", "files"):
        if key not in manifest:
            raise ApiError(400, "ARTIFACT_INVALID", f"artifact manifest missing field: {key}")
    for entry in manifest["files"]:
        path = base_dir / str(entry.get("relative_path", ""))
        if not path.is_file():
            raise ApiError(400, "ARTIFACT_INVALID",
                           f"artifact file missing: {entry.get('relative_path')}")
        if runstore.sha256_file(path) != entry.get("sha256"):
            raise ApiError(400, "ARTIFACT_INVALID",
                           f"artifact hash mismatch: {entry.get('relative_path')}")
    receipt_file = base_dir / str(manifest["metrics_receipt_relative_path"])
    if not receipt_file.is_file() or runstore.sha256_file(receipt_file) != manifest["metrics_receipt_sha256"]:
        raise ApiError(400, "ARTIFACT_INVALID", "metrics receipt missing or hash mismatch")
    manifest["_manifest_sha256"] = runstore.sha256_file(manifest_path)
    return manifest


def artifact_dict(row: StyleRunArtifact) -> dict:
    return {
        "artifact_id": row.artifact_id,
        "run_id": row.run_id,
        "profile_id": row.profile_id,
        "engine_mode": row.engine_mode,
        "engine_id": row.engine_id,
        "engine_version": row.engine_version,
        "base_ckpt_sha256": row.base_ckpt_sha256,
        "dataset_snapshot_sha256": row.dataset_snapshot_sha256,
        "config_sha256": row.config_sha256,
        "metrics_receipt_sha256": row.metrics_receipt_sha256,
        "relative_path": row.relative_path,
        "sha256": row.sha256,
        "quality_status": row.quality_status,
        "active": bool(row.active),
        "created_at": row.created_at,
    }


def rollback_run(session: Session, user_id: int, run_id: int) -> StyleRunArtifact:
    """S10 rollback: flip the per-user activation pointer onto THIS run's
    artifact. Files are never deleted; nothing else changes (the previously
    active artifact stays on disk and in the registry). The target profile
    must be ``approved`` - activation only matters for render consumption and
    only approved profiles may be consumed (v0.2 line 77)."""
    row = _get_owned_run(session, user_id, run_id)
    artifact = (
        session.query(StyleRunArtifact)
        .filter(StyleRunArtifact.run_id == run_id, StyleRunArtifact.user_id == user_id)
        .first()
    )
    if artifact is None:
        raise ApiError(404, "ARTIFACT_NOT_FOUND", "run has no published artifact")
    profile = session.get(StyleProfile, artifact.profile_id) if artifact.profile_id else None
    if profile is None:
        raise ApiError(409, "RUN_NOT_PUBLISHED", "artifact has no live published profile")
    if profile.status != "approved":
        raise ApiError(
            409,
            "STYLE_NOT_APPROVED",
            f"rollback target profile status is {profile.status}; only approved "
            "profiles can become the active render-consumed version",
        )
    manifest=_load_verified_manifest(row)
    if manifest.get('_manifest_sha256')!=artifact.sha256:raise ApiError(409,'ARTIFACT_INVALID','rollback target manifest differs from registered version')
    if artifact.quality_status == 'OWNER_DIRECT_VISUAL_ACCEPTED':
        from engine.style.visual_review.readiness import check_run_readiness
        readiness = check_run_readiness(runstore.run_dir(user_id,run_id),manifest,row.status,
                                       run_config=json.loads(row.config or '{}'),user_id=user_id)
        if readiness.get('status') != 'PUBLISH_READY_OWNER_DIRECT':
            raise ApiError(409,'OWNER_ACCEPTANCE_STALE','Activation requires unchanged owner acceptance and current objective engineering gates')
    if manifest.get('runtime_binding') is not None:
        from engine.style.m2_adapter.runtime_variants import resolve_binding
        try:
            from engine.style.m2_adapter import preserved_training_deployment as D
            deployment=D.lookup(Path(__file__).resolve().parents[3],runstore.run_dir(user_id,run_id),user_id=user_id,config=json.loads(row.config or '{}'))
            if deployment is None:resolve_binding(Path(__file__).resolve().parents[3],manifest['runtime_binding'])
        except (OSError,ValueError,KeyError) as exc:raise ApiError(409,'RUNTIME_UNAVAILABLE',str(exc))
    if not artifact.active:
        session.query(StyleRunArtifact).filter(
            StyleRunArtifact.user_id == user_id, StyleRunArtifact.active == 1
        ).update({"active": 0}, synchronize_session=False)
        artifact.active = 1
    audit(session, action="style_rollback", actor=str(user_id),
          object_ref=f"style_runs/{run_id}",
          result=f"active artifact={artifact.artifact_id} (files untouched)")
    session.flush()
    return artifact


def resolve_style_artifact(session: Session, user_id: int, style_id: int) -> tuple[StyleProfile, StyleRunArtifact, dict]:
    """S11 handover point: resolve an APPROVED profile to its bound artifact
    and re-verify the artifact chain on disk (files exist, manifest hash
    matches). ``pending_review``/anything else -> 409 STYLE_NOT_APPROVED;
    missing binding -> 404 ARTIFACT_NOT_BOUND; broken chain -> 409
    ARTIFACT_INVALID (render must never consume an unverifiable artifact)."""
    profile = _get_owned_style(session, user_id, style_id)
    if profile.status != "approved":
        raise ApiError(
            409,
            "STYLE_NOT_APPROVED",
            f"style status is {profile.status}; only approved profiles resolve to an artifact",
        )
    if not isinstance(profile.receipt_id, str) or not profile.receipt_id.startswith(ARTIFACT_RECEIPT_PREFIX):
        raise ApiError(404, "ARTIFACT_NOT_BOUND", "profile carries no artifact binding")
    try:
        artifact_id = int(profile.receipt_id[len(ARTIFACT_RECEIPT_PREFIX):])
    except ValueError:
        raise ApiError(404, "ARTIFACT_NOT_BOUND", "profile artifact binding is malformed") from None
    artifact = session.get(StyleRunArtifact, artifact_id)
    if artifact is None or artifact.user_id != user_id:
        raise ApiError(404, "ARTIFACT_NOT_BOUND", "artifact binding does not resolve")
    if artifact.profile_id != profile.style_id:
        raise ApiError(409, "ARTIFACT_INVALID", "artifact registry profile binding differs")
    run = session.get(StyleRun, artifact.run_id)
    if run is None or run.user_id != user_id:
        raise ApiError(409, "ARTIFACT_INVALID", "artifact registry run ownership differs")
    if artifact.quality_status=='OWNER_DIRECT_VISUAL_ACCEPTED':
        owner_manifest=_load_verified_manifest(run)
        from engine.style.visual_review.readiness import check_run_readiness
        owner_readiness=check_run_readiness(runstore.run_dir(user_id,run.run_id),owner_manifest,run.status,run_config=json.loads(run.config or '{}'),user_id=user_id)
        if owner_readiness.get('status')!='PUBLISH_READY_OWNER_DIRECT':
            raise ApiError(409,'OWNER_ACCEPTANCE_STALE','Render consumption requires unchanged owner acceptance and all current objective engineering gates')
    # Explicit old-terminal inference can omit optimizer/best-checkpoint exports.
    # Publication still uses the unchanged complete _load_verified_manifest path.
    from engine.style.m2_adapter.windows_scr_deployment import lookup_deployment as lookup_windows
    try:
        deployment=lookup_windows(Path(__file__).resolve().parents[3],runstore.run_dir(user_id,run.run_id),artifact.sha256)
    except (OSError,ValueError,KeyError,TypeError) as exc:raise ApiError(409,'ARTIFACT_INVALID',str(exc)) from exc
    if deployment is not None:
        manifest_path=runstore.artifact_manifest_path(user_id,run.run_id)
        if not manifest_path.is_file() or runstore.sha256_file(manifest_path)!=artifact.sha256:
            raise ApiError(409,'ARTIFACT_INVALID','Original inference source manifest differs')
        manifest=runstore.read_json(manifest_path);manifest['_manifest_sha256']=artifact.sha256
    else:
        manifest = _load_verified_manifest(run)  # 400 ARTIFACT_MISSING/INVALID on breakage
    if manifest.get("_manifest_sha256") != artifact.sha256:
        raise ApiError(409, "ARTIFACT_INVALID", "artifact manifest differs from registered version")
    verification = {
        "manifest_sha256": manifest["_manifest_sha256"],
        "files": manifest["files"],
        "verified": True,
    }
    return profile, artifact, verification


# ---------------------------------------------------------------------------
# S07 read faces (samples / progress / metrics / evaluate) - no fabricated data
# ---------------------------------------------------------------------------

def run_samples(session: Session, user_id: int, run_id: int,
                page: int = 1, page_size: int = 20) -> dict:
    """The locked sample set of one owned run, resolved back to sample rows in
    sample_id order (fixed snapshot; patch-B CC-2 line 24). Owner-scoped 404;
    pagination totals are single-source (same reading as list_styles)."""
    if page < 1 or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ApiError(
            400,
            "INVALID_REQUEST",
            f"page must be >= 1 and page_size within 1..{MAX_PAGE_SIZE}",
        )
    run = _get_owned_run(session, user_id, run_id)
    ids = json.loads(run.sample_ids or "[]")
    query = (
        session.query(StyleSample)
        .filter(StyleSample.sample_id.in_(ids))
        .order_by(StyleSample.sample_id.asc())
    )
    total = query.count()
    rows = query.offset((page - 1) * page_size).limit(page_size).all()
    items = [
        {
            "sample_id": row.sample_id,
            "text_label": row.text_label,
            "source_training_image_id": row.source_training_image_id,
            "uploaded_at": row.uploaded_at,
        }
        for row in rows
    ]
    return {"items": items, "page": page, "page_size": page_size, "total": total}


def _read_state_body(run: StyleRun) -> dict:
    """The honest empty structure shared by all read faces when the worker has
    produced no data yet: DB status + all-None numerics. NEVER synthesized
    epochs/percent/eta (S07 red line)."""
    return {
        "run_id": run.run_id,
        "status": run.status,
        "stage": run.status if run.status in ("created", "queued") else None,
        "engine_mode": None,
        "epoch": None,
        "total_epochs": None,
        "loss": None,
        "eta_s": None,
        "percent": None,
        "samples_seen": None,
        "units_done": None,
        "units_total": None,
        "unit": None,
    }


def run_progress(session: Session, user_id: int, run_id: int) -> dict:
    """Lightweight polling payload. Real values come only from the worker
    heartbeat file; a terminal run is frozen (worker stops writing). The
    engine log tail is bounded (never an unbounded read)."""
    run = _get_owned_run(session, user_id, run_id)
    prog = runstore.read_progress(run.user_id, run.run_id)
    if prog is None:
        body = _read_state_body(run)
    else:
        body = {
            "run_id": run.run_id,
            "status": run.status,  # the DB row is the state authority
            "stage": prog.get("stage"),
            "engine_mode": prog.get("engine_mode"),
            "epoch": prog.get("epoch"),
            "total_epochs": prog.get("total_epochs"),
            "loss": prog.get("loss"),
            "eta_s": prog.get("eta_s"),
            "percent": prog.get("percent"),
            "samples_seen": prog.get("samples_seen"),
            "units_done": prog.get("units_done"),
            "units_total": prog.get("units_total"),
            "unit": prog.get("unit"),
        }
        if prog.get("failure") is not None:
            body["failure"] = prog["failure"]
    if run.status=='succeeded' and prog and prog.get('failure') is not None:
        try:
            from engine.style.m2_adapter.preserved_training_deployment import normalized_training_receipt
            manifest=_load_verified_manifest(run)
            receipt=normalized_training_receipt(runstore.run_dir(user_id,run_id),json.loads(run.config or '{}'),manifest)
            body['historical_execution']={'stage':prog.get('stage'),'failure':prog.get('failure')}
            body.update(stage='admitted',failure=None,admission={'status':'PASSED','core_training_completed':True,'global_step':receipt['adapter_selection']['global_step'],'quality_approved':False})
        except (ValueError,OSError,KeyError,TypeError):pass
    body["log_tail"] = runstore.engine_log_tail(run.user_id, run.run_id)
    body["updated_at"] = run.updated_at
    return body


def register_inference_deployment(session,user_id,run_id,runtime_registration_id):
    """Owned explicit compatibility registration; no training or publication."""
    import os
    from engine.style.m2_adapter import preserved_training_deployment as D
    from engine.style.m2_adapter import windows_training_runtime as W
    from engine.style.m2_adapter.runtime_variants import make_binding
    run=_get_owned_run(session,user_id,run_id)
    if run.status!='succeeded':raise ApiError(409,'RUN_NOT_EVALUABLE','Formal admitted terminal training required')
    if not isinstance(runtime_registration_id,str) or not re.fullmatch('[0-9a-f]{32}',runtime_registration_id):raise ApiError(400,'INVALID_REQUEST','Exact current registration required')
    root=runstore.run_dir(user_id,run_id);config=json.loads(run.config or '{}')
    producer=os.environ.get('YY_PRESERVED_TRAINING_SOURCE_ROOT')
    if not producer:raise ApiError(409,'RUNTIME_UNAVAILABLE','Server historical producer source is not configured')
    app=Path(__file__).resolve().parents[3]
    try:
        _load_verified_manifest(run)
        source=D.admit_source(root,config,user_id=user_id,producer_app=producer)
        selected=W.inspect(app,runtime_registration_id,purpose='inference')
        binding=D.register(app,source,make_binding(selected),config=config,user_id=user_id)
        body={'schema':'preserved-gpu2-inference-pointer/1','user_id':user_id,'run_id':run_id,'run_dir':str(root),'deployment_binding':binding}
        envelope={'body':body,'hmac_sha256':W._mac(app,body)}
        pointer=D.pointer_path(app,root)
        W._write(pointer,envelope)
        resolved=D.lookup(app,root,user_id=user_id,config=config)
    except (ValueError,OSError,KeyError,TypeError) as exc:raise ApiError(409,'RUNTIME_UNAVAILABLE',str(exc)) from exc
    audit(session,action='style_run_inference_deployment_registered',actor=str(user_id),object_ref=f'style_runs/{run_id}',result='Independent inference compatibility '+binding['deployment_sha256']+'; training/quality/publication unchanged')
    session.flush()
    return {'run_id':run_id,'deployment_binding':binding,'training_runtime_binding':source['training_runtime_binding'],'runtime_binding':resolved['runtime_binding'],'quality_approved':False,'published':False}


def inference_deployment_status(session,user_id,run_id):
    """Owned P7 reader; no supplied paths, registrations or ready flags."""
    import os
    from engine.style.m2_adapter import preserved_training_deployment as D
    from engine.style.m2_adapter.runtime_variants import discover_installed
    from engine.style.m2_adapter.windows_user_scr import GPU
    run=_get_owned_run(session,user_id,run_id)
    result={'run_id':run_id,'state':'not_required','supports_preserved_training':False,'available':[]}
    if (user_id,run_id)!=(1,2):return result
    result['supports_preserved_training']=True
    root=runstore.run_dir(user_id,run_id);app=Path(__file__).resolve().parents[3];config=json.loads(run.config or '{}')
    try:
        if run.status!='succeeded':raise ValueError('formal admission pending')
        manifest=_load_verified_manifest(run)
        D.normalized_training_receipt(root,config,manifest)
        producer=os.environ.get('YY_PRESERVED_TRAINING_SOURCE_ROOT')
        if not producer:raise ValueError('historical source not configured')
        D.admit_source(root,config,user_id=user_id,producer_app=producer)
        resolved=D.lookup(app,root,user_id=user_id,config=config)
        result['available']=[{k:row[k] for k in ('registration_id','variant_id','device','ready')} for row in discover_installed(app) if row.get('ready') is True and row.get('variant_id')==GPU and row.get('device')=='cuda' and row.get('registration_id')!=D.REGISTRATION]
        if resolved:
            result.update(state='registered',deployment_binding=resolved['deployment_binding'],runtime_binding=resolved['runtime_binding'],training_runtime_binding=resolved['training_runtime_binding'])
        else:result['state']='not_registered'
    except (ValueError,OSError,KeyError,TypeError,ApiError):
        result.update(state='blocked',reason='Preserved training evidence or current execution qualification is unavailable')
    return result


def run_metrics(session: Session, user_id: int, run_id: int) -> dict:
    """Training/inference metrics. No result -> explicit pending/unavailable
    structure (never an all-zeros "evaluated" payload). With a receipt, the
    receipt file hash is included (single binding source for S10/S11)."""
    run = _get_owned_run(session, user_id, run_id)
    body = {
        "run_id": run.run_id,
        "state": None,
        "reason": None,
        "metrics": None,
        "receipt": None,
    }
    if run.status in ("created", "queued", "running"):
        body["state"], body["reason"] = "pending", f"RUN_{run.status.upper()}"
        return body
    if run.status in ("failed", "cancelled"):
        body["state"], body["reason"] = "unavailable", f"RUN_{run.status.upper()}"
        prog = runstore.read_progress(run.user_id, run.run_id)
        if prog is not None and isinstance(prog.get("failure"), dict):
            body["detail"] = prog["failure"]
        return body
    # succeeded
    receipt_file = runstore.receipt_path(run.user_id, run.run_id)
    receipt = runstore.read_json(receipt_file)
    prog = runstore.read_progress(run.user_id, run.run_id)
    if receipt is None or receipt.get("ok") is not True:
        body["state"], body["reason"] = "unavailable", "RECEIPT_ABSENT"
        return body
    steps = receipt.get("steps") or []
    samples_seen = None
    if prog is not None and prog.get("samples_seen") is not None:
        samples_seen = prog.get("samples_seen")
    elif steps:
        samples_seen = len(steps)
    body["state"] = "available"
    body["metrics"] = {
        "run_id": run.run_id,
        "samples_seen": samples_seen,
        "epoch": prog.get("epoch") if prog else None,
        "total_epochs": prog.get("total_epochs") if prog else None,
        "loss": prog.get("loss") if prog else None,
        "eta_s": None,  # terminal: no eta survives completion
        "engine_mode": receipt.get("engine_mode"),
        "engine_id": receipt.get("engine_id"),
    }
    body["receipt"] = {
        "relative_path": runstore.receipt_path(run.user_id, run.run_id).relative_to(
            runstore.run_dir(run.user_id, run.run_id)).as_posix(),
        "sha256": runstore.sha256_file(receipt_file),
        "engine_mode": receipt.get("engine_mode"),
        "engine_id": receipt.get("engine_id"),
        "engine_version": receipt.get("engine_version"),
    }
    return body


def run_evaluate(session: Session, user_id: int, run_id: int) -> dict:
    """Evaluation report reader (GET; triggering evaluation is S08's job). A
    run without an evaluation receipt answers an explicit
    pending/unavailable structure - never a fabricated report, never
    all-zero "already evaluated" numbers. ``available`` requires a real
    eval receipt file (S08 contract), whose hash is returned."""
    run = _get_owned_run(session, user_id, run_id)
    body = {
        "run_id": run.run_id,
        "state": None,
        "reason": None,
        "report": None,
        "receipt": None,
    }
    if run.status in ("created", "queued", "running"):
        body["state"], body["reason"] = "pending", f"RUN_{run.status.upper()}"
        return body
    if run.status in ("failed", "cancelled"):
        body["state"], body["reason"] = "unavailable", f"RUN_{run.status.upper()}"
        return body
    eval_path = runstore.run_dir(run.user_id, run.run_id) / "receipts" / EVAL_RECEIPT_FILENAME
    report = runstore.read_json(eval_path)
    if report is None:
        # honest answer: this run has NO evaluation - S08 has not produced one
        body["state"], body["reason"] = "unavailable", "EVAL_NOT_PRODUCED"
        return body
    required = ("report_id", "glyph_accuracy", "style_similarity", "coverage",
                "per_char_sample", "generated_samples")
    if not all(k in report for k in required):
        body["state"], body["reason"] = "unavailable", "EVAL_RECEIPT_INVALID"
        return body
    body["state"] = "available"
    body["report"] = {k: report[k] for k in required}
    body["receipt"] = {
        "relative_path": f"receipts/{EVAL_RECEIPT_FILENAME}",
        "sha256": runstore.sha256_file(eval_path),
    }
    return body


def set_run_status(session: Session, run_id: int, status: str) -> StyleRun:
    """Engine-side status transition carrier (dispatch T69 section 0.3).

    Worker behavior is simulated by probes calling this directly; no HTTP
    endpoint exposes it, and ownership is intentionally not checked here
    because the worker role is not a user.
    """
    if status not in RUN_STATUSES:
        raise ApiError(400, "INVALID_REQUEST", f"unknown run status {status}")
    row = session.query(StyleRun).filter(StyleRun.run_id == run_id).first()
    if row is None:
        raise ApiError(404, "RUN_NOT_FOUND", "Style run not found")
    row.status = status
    row.updated_at = _now()
    session.flush()
    return row


def enable_style(session: Session, user_id: int, style_id: int) -> StyleProfile:
    """``enabled`` -> True (v0.2 line 69; D-T69-2 closure, batch B1-03).

    Minimal semantics (contract gap registered in b1-styles-toggle-discrepancy):
    the frozen contract constrains no status precondition for enable/disable —
    the toggle is independent of ``status`` (approval is the render-reference
    gate, ``enabled`` is the listing/selection switch). Any owned style in any
    status may be toggled; already-enabled is an idempotent no-op (same reading
    as ``approve_style``), and only a real state change is audited.
    Foreign/missing rows answer 404 STYLE_NOT_FOUND (anti-enumeration).
    """
    return _set_enabled(session, user_id, style_id, True)


def disable_style(session: Session, user_id: int, style_id: int) -> StyleProfile:
    """``enabled`` -> False (v0.2 line 68: no hard delete, only enabled=false;
    D-T69-2 closure, batch B1-03). See ``enable_style`` for the semantics."""
    return _set_enabled(session, user_id, style_id, False)


def _set_enabled(session: Session, user_id: int, style_id: int, value: bool) -> StyleProfile:
    row = _get_owned_style(session, user_id, style_id)
    if bool(row.enabled) == value:
        return row
    row.enabled = 1 if value else 0
    audit(
        session,
        action="style_enable" if value else "style_disable",
        actor=str(user_id),
        object_ref=f"style_profiles/{style_id}",
        result="ok",
    )
    session.flush()
    return row


def assert_style_approved(session: Session, style_id: int, user_id: int) -> StyleProfile:
    """Render-jobs reference gate (v0.2 line 77, patch-C 9.4).

    ``pending_review`` counts as not approved; missing or foreign rows answer
    404 STYLE_NOT_FOUND (anti-enumeration). The render-jobs domain call site
    is bound by T66.
    """
    row = _get_owned_style(session, user_id, style_id)
    if row.status != "approved":
        raise ApiError(
            409,
            "STYLE_NOT_APPROVED",
            f"style status is {row.status}, only approved styles may be referenced",
        )
    return row


def run_readiness(session: Session, user_id: int, run_id: int) -> dict:
    """Read-only engineering state. Owner pending does not stop other work."""
    run = _get_owned_run(session, user_id, run_id)
    from engine.style.visual_review.readiness import check_run_readiness
    manifest = runstore.read_json(runstore.artifact_manifest_path(user_id, run_id)) or {}
    result = check_run_readiness(runstore.run_dir(user_id, run_id), manifest, run.status, run_config=json.loads(run.config or "{}"), user_id=user_id)
    result["run_id"] = run_id
    result["OWNER_BLIND_REVIEW"] = ("PASS" if session.query(StyleRunBlindReview).filter(
        StyleRunBlindReview.run_id == run_id, StyleRunBlindReview.verdict == "pass").count() else result["OWNER_BLIND_REVIEW"])
    from . import m2_jobs
    evaluation_state=m2_jobs.state(user_id,run_id,"evaluation")
    ai_state=m2_jobs.state(user_id,run_id,"ai_review")
    result["evaluation_state"]=evaluation_state
    result["ai_review_state"]=ai_state
    result["allowed_actions"]={"evaluate":run.status=="succeeded" and evaluation_state not in ("running","complete"),
                                "ai_review":run.status=="succeeded" and evaluation_state=="complete" and m2_jobs.ai_available(user_id,run_id),
                                "publish":result.get("may_publish") is True or (result["status"]=="PUBLISH_READY_PENDING_OWNER" and result["OWNER_BLIND_REVIEW"]=="PASS")}
    artifact=session.query(StyleRunArtifact).filter(StyleRunArtifact.user_id==user_id,StyleRunArtifact.run_id==run_id).first()
    profile=session.get(StyleProfile,artifact.profile_id) if artifact and artifact.profile_id else None
    result["publication"]={**profile_dict(profile),"artifact":artifact_dict(artifact)} if profile is not None else None
    # This endpoint never inserts a blind review, approves a profile, or publishes.
    return result
