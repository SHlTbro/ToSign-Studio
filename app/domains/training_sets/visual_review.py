"""Additive candidate review sidecars, kept separate from Owner review rows."""
import json
import logging
import os
import threading
import re
from datetime import datetime, timezone
from pathlib import Path
from app.core import storage
from engine.style.visual_review.pipeline import candidate_review, sha, write_new
from engine.style.visual_review import pipeline
from engine.style.visual_review.policy import candidate_delegation
from engine.style.dataset.schema import (
    REVIEW_AI_CONFIRMED, candidate_acceptance_reasons, review_digest,
    validate_candidate_response, validate_ai_confirmation,
)


_CONFIRMATION_WRITE_LOCK = threading.Lock()

def candidate_review_path(user_id, candidate_id, image_sha256):
    if not re.fullmatch(r'[A-Za-z0-9_-]+',str(candidate_id)) or not re.fullmatch(r'[0-9a-f]{64}',image_sha256):
        raise ValueError('invalid candidate review identity')
    root=storage.storage_root()
    return storage.ensure_within_root(root/'users'/str(int(user_id))/'styles'/'visual-reviews'/'candidates'/str(candidate_id)/image_sha256/'candidate_visual_review.json',root)


def ensure_candidate_review(user_id, candidate_id, image_path, label):
    image_sha=sha(image_path);path=candidate_review_path(user_id,candidate_id,image_sha)
    if path.is_file():
        result=json.loads(path.read_text(encoding='utf-8'))
        if result.get('candidate_id')!=str(candidate_id) or result.get('image_sha256')!=image_sha:
            raise ValueError('candidate visual review binding mismatch')
        return result
    result=candidate_review(candidate_id,image_path,label=label)
    try:write_new(path,result)
    except FileExistsError:
        return ensure_candidate_review(user_id,candidate_id,image_path,label)
    return result


def schedule_candidate_review(session,user_id,candidate_id,image_path,label):
    """Only successful P4 DB commits create sidecars; rollbacks create none.

    A read path retries a failed sidecar write, so post-commit IO cannot falsely
    roll back a saved candidate or auto-promote Owner confirmation.
    """
    from sqlalchemy import event
    args=(user_id,candidate_id,str(image_path),label)
    def after_commit(_session):
        try:ensure_candidate_review(*args)
        except Exception:logging.getLogger(__name__).exception('candidate AI visual review unavailable')
    event.listen(session,'after_commit',after_commit,once=True)


def review_candidate_for_confirmation(*, user_id, style_id, sample_id, image_path,
                                      source_image_path, source_bbox_norm, label,
                                      reviewer=None, review_context=None, reviewed_at=None):
    """Run the configured, authorized vision adapter; never accept client scores.

    An adapter must view BOTH the saved alpha and the original source context.
    Existing crop-only advisory sidecars cannot be upgraded into confirmations.
    No network provider is selected, installed, or paid for by this function.
    """
    policy = candidate_delegation(user_id)
    if policy is None:
        raise ValueError("AI candidate confirmation is not authorized for this user/project")
    checks = pipeline.inspect_glyph(image_path, (128, 128))
    binding = {"image_sha256": sha(image_path), "source_image_sha256": sha(source_image_path),
               "source_bbox_norm": source_bbox_norm, "label": label}
    receipt = {
        "schema": "ai-candidate-confirmation/1", "project": "dyYY",
        "personal_noncommercial": True, "user_id": user_id, "style_id": str(style_id),
        "sample_id": sample_id, "candidate_id": sample_id, "binding": binding,
        "image_sha256": binding["image_sha256"], "label": label,
        "actor_kind": "ai", "actor": "ai:unavailable", "reviewer": None,
        "reviewed_at": reviewed_at or datetime.now(timezone.utc).isoformat(),
        "authorization": policy["candidate_confirmation"]["authorization"],
        "policy_sha256": policy["_sha256"],
        "minimum_confidence": policy["candidate_confirmation"]["minimum_confidence"],
        "structural_checks": checks, "review_response": None, "review_response_sha256": None,
        "review_state": "BLOCKED_AI_PROVIDER", "review_status": "candidate",
        "recommendation": "AI_NEEDS_HUMAN", "confidence": None,
        "reason": "No configured pixel-viewing AI reviewer; candidate remains isolated",
        "isolation_reasons": ["BLOCKED_AI_PROVIDER"], "owner_confirmation": "UNCHANGED",
    }
    if review_context is not None:
        receipt["offline_review_context"] = review_context
    selected_reviewer = reviewer if reviewer is not None else pipeline._REVIEWER
    if selected_reviewer is not None:
        try:
            response = selected_reviewer({
                "task": "candidate_confirmation_review", "candidate_id": sample_id,
                "image_path": str(Path(image_path).resolve()),
                "source_image_path": str(Path(source_image_path).resolve()), **binding,
                "rubric": list(pipeline.QUALITY_FIELDS) + ["label_correct", "crop_complete", "contamination_free"],
                "required_context": "View final alpha AND original source around the exact bbox; uncertain label/crop/contamination must be isolated",
                "required_response_fields": ["pixels_inspected", "source_pixels_inspected", "source_image_sha256",
                    "source_bbox_norm", "label", "label_correct", "crop_complete", "contamination_free", "uncertain",
                    "label_rationale", "crop_rationale", "contamination_rationale", "resolved_structural_suspicions"],
                "permitted_recommendations": list(pipeline.RECOMMENDATIONS),
            })
            # Hash after the adapter returns too: it may have been slow while
            # another process changed a source. Never certify stale pixels.
            if sha(image_path) != binding["image_sha256"] or sha(source_image_path) != binding["source_image_sha256"]:
                raise ValueError("candidate/source pixels changed during visual review")
            validate_candidate_response(response, binding)
            reasons = candidate_acceptance_reasons(response, checks, receipt["minimum_confidence"])
            reviewer = {k: response[k] for k in ("provider", "model_id", "reviewer_kind")}
            receipt.update({"review_response": response, "review_response_sha256": review_digest(response),
                "reviewer": reviewer, "actor": f'ai:{response["provider"]}:{response["model_id"]}',
                "review_state": "AI_REVIEW_COMPLETE", "review_status": "candidate" if reasons else REVIEW_AI_CONFIRMED,
                "recommendation": response["recommendation"] if not reasons or response["recommendation"] == "AI_REJECT" else "AI_NEEDS_HUMAN",
                "confidence": response["confidence"], "reason": response["reason"],
                "isolation_reasons": reasons,
                **{k: response[k] for k in pipeline.QUALITY_FIELDS}})
        except Exception as exc:
            receipt.update({"review_state": "BLOCKED_AI_EVIDENCE", "reason": str(exc),
                            "isolation_reasons": ["BLOCKED_AI_EVIDENCE", str(exc)]})
    if checks["clear_errors"]:
        receipt["recommendation"] = "AI_REJECT"
        receipt["isolation_reasons"] = sorted(set(receipt["isolation_reasons"] + checks["clear_errors"]))
    receipt["receipt_sha256"] = review_digest(receipt)
    return receipt


def confirmation_receipt_path(user_id, sample_id, image_sha256, receipt_sha256):
    if not isinstance(receipt_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", receipt_sha256):
        raise ValueError("invalid AI receipt identity")
    root = candidate_review_path(user_id, sample_id, image_sha256).parent
    # Validate containment using the normal path before adapting Windows IO.
    # Keep all historical folder names and the full content hash unchanged.
    path = storage.ensure_within_root(root / "decisions" / (receipt_sha256 + ".json"))
    if os.name == "nt":
        value = str(path)
        if not value.startswith("\\\\?\\"):
            value = ("\\\\?\\UNC\\" + value[2:]) if value.startswith("\\\\") else "\\\\?\\" + value
        path = Path(value)
    return path


def save_confirmation_receipt(receipt):
    """Append-only review evidence, not authority until a DB transaction binds it."""
    path = confirmation_receipt_path(receipt["user_id"], receipt["sample_id"],
                                     receipt["binding"]["image_sha256"], receipt["receipt_sha256"])
    # The desktop worker is single-process. An identical concurrent writer
    # cannot observe another thread's incomplete exclusive file write.
    with _CONFIRMATION_WRITE_LOCK:
        try:write_new(path, receipt)
        except FileExistsError:
            expected=json.dumps(receipt,ensure_ascii=False,indent=2,allow_nan=False).encode("utf-8")
            if path.read_bytes() not in (expected, expected.replace(b"\n", b"\r\n")):raise ValueError("immutable confirmation receipt conflict")
    return json.dumps({"schema": "ai-review-reference/1", "receipt_sha256": receipt["receipt_sha256"],
                       "reason": receipt["reason"]}, ensure_ascii=False, allow_nan=False)


def load_confirmation_receipt(row, *, require_confirmed=False):
    """Bind the immutable receipt to canonical DB fields and current delegation."""
    try:
        ref = json.loads(row.review_reason or "null")
    except (ValueError, TypeError):
        ref = None
    if not isinstance(ref, dict) or ref.get("schema") != "ai-review-reference/1":
        raise ValueError("canonical sample has no AI review reference")
    path = confirmation_receipt_path(row.user_id, row.sample_id, row.final_alpha_sha256, ref.get("receipt_sha256"))
    receipt = json.loads(path.read_text(encoding="utf-8"))
    digest = review_digest({k: v for k, v in receipt.items() if k != "receipt_sha256"})
    if receipt.get("receipt_sha256") != ref["receipt_sha256"] or digest != ref["receipt_sha256"]:
        raise ValueError("AI review receipt checksum mismatch")
    expected_binding = {"image_sha256": row.final_alpha_sha256, "source_image_sha256": row.source_asset_sha256,
                        "source_bbox_norm": json.loads(row.text_region_bbox_norm), "label": row.text_label}
    if (receipt.get("user_id") != row.user_id or str(receipt.get("style_id")) != str(row.style_id)
            or receipt.get("sample_id") != row.sample_id or receipt.get("binding") != expected_binding
            or receipt.get("actor") != row.reviewed_by or receipt.get("reviewed_at") != row.reviewed_at
            or receipt.get("review_status") != row.review_status):
        raise ValueError("AI review receipt does not match the canonical sample")
    if require_confirmed:
        if row.owner_confirmed_by is not None or row.owner_confirmed_at is not None:
            raise ValueError("AI confirmation cannot claim human confirmation")
        policy = candidate_delegation(row.user_id)
        if (policy is None or receipt.get("policy_sha256") != policy["_sha256"]
                or receipt.get("authorization") != policy["candidate_confirmation"]["authorization"]
                or receipt.get("minimum_confidence") != policy["candidate_confirmation"]["minimum_confidence"]):
            raise ValueError("AI confirmation lacks the current scoped delegation")
        validate_ai_confirmation({"sample_id": row.sample_id, "style_id": row.style_id,
            "label": row.text_label, "final_alpha_sha256": row.final_alpha_sha256,
            "parent_source_hash": row.source_asset_sha256, "confirmed_by": row.reviewed_by,
            "confirmed_at": row.reviewed_at, "review_provenance": receipt})
    return receipt
