"""Explicit physical-occurrence/revision selection after native AI review.

Selections are delegated-AI decisions separate from glyph acceptance. Every
accepted native revision remains immutable evidence; only an authenticated,
complete selection decides which version may count/export/train. Geometry is
used only to block ambiguity, never to infer labels or silently choose pixels.
"""
from __future__ import annotations

import json
import math
import re
import threading
from functools import wraps

from sqlalchemy import inspect
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.core import storage
from app.core.audit import audit
from app.domains.styles.models import StyleProfile
from app.domains.training_sets.models import TrainingSample
from app.domains.training_sets.intake_models import CandidateReviewJob
from app.domains.training_sets.occurrence_models import OccurrenceSelection, CurrentOccurrenceSelection
from app.domains.training_sets import candidate_review_jobs as jobs
from app.domains.training_sets.source_admission import checked_ledger
from app.domains.training_sets.visual_review import load_confirmation_receipt
from engine.style.dataset.schema import DatasetError, review_digest, canonical_json
from engine.style.visual_review.pipeline import sha
from engine.style.visual_review.policy import candidate_delegation

SCHEMA = "native-occurrence-selection/1"
NATIVE_EXTRACTOR = "p4_native_delegated_ai_noop_review"
LOCK = threading.RLock()
REF_KEYS = {"job_id", "candidate_id"}


def _require(value, code, detail):
    if not value:
        raise DatasetError(code, detail)


def _structured_storage_errors(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise DatasetError("E_OCCURRENCE_STORAGE", "malformed occurrence/native review storage") from exc
    return checked


def _profile(session, user_id, style_id):
    _require(type(user_id) is int and user_id > 0 and type(style_id) is int and style_id > 0,
             "E_OCCURRENCE_IDENTITY", "positive authenticated user/style required")
    profile = session.query(StyleProfile).filter(StyleProfile.style_id == style_id,
                                                StyleProfile.user_id == user_id).first()
    _require(profile is not None and profile.source_type == "HANDWRITING_ASSET",
             "E_OCCURRENCE_IDENTITY", "owned handwriting style required")
    return profile


def _authority(user_id):
    policy = candidate_delegation(user_id)
    _require(policy is not None, "E_OCCURRENCE_AUTHORITY", "current scoped AI delegation required")
    return {"actor_kind": "delegated_ai", "actor": "ai:delegated_occurrence_selection",
            "policy_sha256": policy["_sha256"],
            "authorization": policy["candidate_confirmation"]["authorization"],
            "human_review": False, "changes_review_decisions": False}


def _ref(value):
    _require(isinstance(value, dict) and set(value) == REF_KEYS,
             "E_OCCURRENCE_REQUEST", "revision reference requires only job_id and candidate_id")
    _require(isinstance(value["job_id"], str) and re.fullmatch(r"[0-9a-f]{32}", value["job_id"])
             and isinstance(value["candidate_id"], str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value["candidate_id"]),
             "E_OCCURRENCE_REQUEST", "invalid native revision reference")
    return value["job_id"], value["candidate_id"]


def _ref_dict(key):
    return {"job_id": key[0], "candidate_id": key[1]}


def _box(value):
    keys = ("x_norm", "y_norm", "w_norm", "h_norm")
    _require(isinstance(value, dict) and set(value) == set(keys),
             "E_OCCURRENCE_BBOX", "exact normalized source box required")
    _require(all(type(value[key]) in (int, float) and math.isfinite(value[key]) for key in keys),
             "E_OCCURRENCE_BBOX", "source box must be finite")
    x, y, w, h = (float(value[key]) for key in keys)
    _require(0 <= x <= 1 and 0 <= y <= 1 and w > 0 and h > 0 and x + w <= 1 and y + h <= 1,
             "E_OCCURRENCE_BBOX", "source box is out of bounds")
    return x, y, w, h


def _overlap(a, b):
    ax, ay, aw, ah = _box(a); bx, by, bw, bh = _box(b)
    return min(ax + aw, bx + bw) > max(ax, bx) and min(ay + ah, by + bh) > max(ay, by)


def effective_native_rect(box, width, height):
    """Exact extraction._crop_norm round/clamp/minimum-one raster footprint."""
    _box(box)
    _require(type(width) is int and width > 0 and type(height) is int and height > 0,
             "E_OCCURRENCE_DIMENSIONS", "authoritative positive source dimensions required")
    x0 = int(round(box["x_norm"] * width))
    y0 = int(round(box["y_norm"] * height))
    x1 = min(width, int(round((box["x_norm"] + box["w_norm"]) * width)))
    y1 = min(height, int(round((box["y_norm"] + box["h_norm"]) * height)))
    return [x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)]


def _rect_overlap(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _job_evidence(session, user_id, style_id, job_id, cache):
    if job_id in cache:
        return cache[job_id]
    job, package = jobs.get_job(session, user_id, job_id)
    _require(job.style_id == style_id and package.get("user_id") == user_id
             and package.get("style_id") == style_id and package.get("job_id") == job_id,
             "E_OCCURRENCE_IDENTITY", "native job belongs to another user/style")
    # Read-only original validators retain the exact R2 code/pixel authority.
    jobs._validate_current(session, job, package)
    cache[job_id] = (job, package)
    return job, package


@_structured_storage_errors
def current_native_intake(session, user_id, style_id, *, confirmed=None, ledger=None):
    """Recompute all accepted native versions from jobs, canonical rows and bytes."""
    profile = _profile(session, user_id, style_id)
    if confirmed is None:
        confirmed = session.query(TrainingSample).filter(TrainingSample.user_id == user_id,
            TrainingSample.style_id == str(style_id),
            TrainingSample.review_status.in_(("owner_confirmed", "ai_confirmed_authorized"))).all()
    receipts, native_marked = {}, set()
    for row in confirmed:
        if row.extracted_by == NATIVE_EXTRACTOR:
            native_marked.add(row.sample_id)
        if row.review_status == "ai_confirmed_authorized":
            proof = load_confirmation_receipt(row, require_confirmed=True)
            receipts[row.sample_id] = proof
            if "offline_review_context" in proof:
                native_marked.add(row.sample_id)
    has_jobs = inspect(session.connection()).has_table(CandidateReviewJob.__tablename__)
    applied = (session.query(CandidateReviewJob).filter(CandidateReviewJob.user_id == user_id,
               CandidateReviewJob.style_id == style_id, CandidateReviewJob.status == "applied").all()
               if has_jobs else [])
    if not applied and not native_marked:
        return None
    authority = _authority(user_id)
    ledger = ledger or checked_ledger()
    by_id = {row.sample_id: row for row in confirmed}
    versions, job_bindings, cache, covered = [], [], {}, set()
    from scripts.export_personal_style_dataset import _row_for_confirmed_sample
    for listed_job in sorted(applied, key=lambda job: job.job_id):
        job, package = _job_evidence(session, user_id, style_id, listed_job.job_id, cache)
        result = json.loads(job.result_json or "null")
        _require(isinstance(result, dict) and result.get("job_id") == job.job_id
                 and result.get("status") == "APPLIED" and isinstance(result.get("items"), list)
                 and result.get("bundle_sha256") == job.imported_bundle_sha256,
                 "E_OCCURRENCE_JOB", "applied native job lacks its canonical result/evidence")
        path = jobs.job_root(user_id, job.job_id) / "trusted-evidence" / f"{job.imported_bundle_sha256}.json"
        _require(path.is_file() and sha(path) == job.imported_bundle_sha256,
                 "E_OCCURRENCE_REVIEW_CHANGED", "original imported AI review bytes changed")
        bundle = jobs.strict_json(path.read_text(encoding="utf-8"))
        jobs._validate_bundle(bundle, job, package)
        items = {item["candidate_id"]: item for item in package["items"]}
        decisions = result["items"]
        _require(all(isinstance(decision, dict) for decision in decisions),
                 "E_OCCURRENCE_JOB", "native result dispositions must be objects")
        _require(len(decisions) == len(items) and {d.get("candidate_id") for d in decisions} == set(items),
                 "E_OCCURRENCE_JOB", "native result must cover the exact job candidates")
        job_bindings.append({"job_id": job.job_id, "package_sha256": job.package_sha256,
                             "bundle_sha256": job.imported_bundle_sha256, "result_sha256": review_digest(result)})
        for decision in decisions:
            sid = decision.get("canonical_sample_id")
            if sid is None:
                _require(decision.get("review_status") != "ai_confirmed_authorized",
                         "E_OCCURRENCE_JOB", "accepted native version has no canonical sample")
                continue
            item = items[decision["candidate_id"]]
            expected_sid = "ss_ai_" + review_digest({"job_id": job.job_id, "candidate_id": item["candidate_id"]})[:32]
            _require(sid == expected_sid and sid in by_id and sid not in covered,
                     "E_OCCURRENCE_JOB", "accepted native version is missing, duplicated or unlisted")
            row = by_id[sid]; proof = receipts.get(sid)
            _require(row.review_status == "ai_confirmed_authorized" and isinstance(proof, dict),
                     "E_OCCURRENCE_REVIEW_CHANGED", "accepted native review state changed")
            context = proof.get("offline_review_context")
            expected = {"schema": "candidate-offline-evidence/1", "job_id": job.job_id,
                        "package_sha256": job.package_sha256, "bundle_sha256": job.imported_bundle_sha256,
                        "candidate_id": item["candidate_id"], "candidate_version_sha256": item["candidate_version_sha256"]}
            _require(isinstance(context, dict) and all(context.get(k) == v for k, v in expected.items())
                     and decision.get("receipt_sha256") == proof["receipt_sha256"]
                     and decision.get("candidate_version_sha256") == item["candidate_version_sha256"]
                     and row.source_asset_id == item["source_asset_id"]
                     and row.source_asset_sha256 == item["source_image_sha256"]
                     and row.final_alpha_sha256 == item["image_sha256"]
                     and json.loads(row.text_region_bbox_norm) == item["source_bbox_norm"],
                     "E_OCCURRENCE_REVIEW_CHANGED", "canonical version differs from original native review lineage")
            _, provenance = _row_for_confirmed_sample(session, row, profile, ledger, storage.storage_root())
            versions.append({"ref": {"job_id": job.job_id, "candidate_id": item["candidate_id"]},
                "sample_id": sid, "source_asset_id": row.source_asset_id,
                "parent_source_hash": row.source_asset_sha256, "source_bbox_norm": item["source_bbox_norm"],
                "source_width": row.source_asset_width, "source_height": row.source_asset_height,
                "effective_source_rect": effective_native_rect(item["source_bbox_norm"], row.source_asset_width, row.source_asset_height),
                "label": row.text_label, "image_sha256": row.final_alpha_sha256,
                "candidate_version_sha256": item["candidate_version_sha256"],
                "review_receipt_sha256": proof["receipt_sha256"],
                "canonical_provenance_sha256": provenance["provenance_sha256"],
                "source_admission_sha256": provenance["source_admission"]["receipt_sha256"]})
            covered.add(sid)
    _require(covered == native_marked, "E_OCCURRENCE_JOB", "every accepted native row must resolve to exactly one applied job")
    if not versions:
        return None
    body = {"schema": "current-native-occurrence-intake/1", "user_id": user_id, "style_id": style_id,
            "ledger_sha256": ledger.sha256, "authority": authority, "jobs": job_bindings,
            "versions": sorted(versions, key=lambda version: _ref(version["ref"]))}
    body["native_intake_sha256"] = review_digest(body)
    return body


def _resolve_groups(session, user_id, style_id, groups, intake):
    _require(isinstance(groups, list) and 1 <= len(groups) <= 10000,
             "E_OCCURRENCE_REQUEST", "explicit nonempty occurrence groups are required")
    versions = {_ref(version["ref"]): version for version in intake["versions"]}
    covered, occurrences, resolved, cache = set(), set(), [], {}
    for group in groups:
        _require(isinstance(group, dict) and set(group) == {"origin", "revisions", "selected"},
                 "E_OCCURRENCE_REQUEST", "group needs origin, revisions and exactly one selected reference")
        origin_key, selected = _ref(group["origin"]), _ref(group["selected"])
        revisions = group["revisions"]
        _require(isinstance(revisions, list) and revisions, "E_OCCURRENCE_REQUEST", "accepted revisions are required")
        refs = [_ref(ref) for ref in revisions]
        _require(len(set(refs)) == len(refs) and selected in refs and not covered.intersection(refs)
                 and set(refs).issubset(versions), "E_OCCURRENCE_COVERAGE", "revisions must be listed exactly once; select one listed accepted version")
        _, package = _job_evidence(session, user_id, style_id, origin_key[0], cache)
        anchors = [item for item in package["items"] if item["candidate_id"] == origin_key[1]]
        _require(len(anchors) == 1, "E_OCCURRENCE_ORIGIN", "origin is not a native candidate in this job")
        anchor = anchors[0]
        origin = {"ref": _ref_dict(origin_key), "source_asset_id": anchor["source_asset_id"],
                  "parent_source_hash": anchor["source_image_sha256"], "source_bbox_norm": anchor["source_bbox_norm"],
                  "source_width": anchor["source_width"], "source_height": anchor["source_height"],
                  "effective_source_rect": effective_native_rect(anchor["source_bbox_norm"], anchor["source_width"], anchor["source_height"]),
                  "candidate_version_sha256": anchor["candidate_version_sha256"],
                  "package_sha256": review_digest(package)}
        oid = review_digest({"user_id": user_id, "style_id": style_id,
                             "parent_source_hash": origin["parent_source_hash"],
                             "original_source_bbox_norm": origin["source_bbox_norm"]})
        _require(oid not in occurrences, "E_OCCURRENCE_DUPLICATE", "same physical origin cannot be declared twice")
        for ref in refs:
            version = versions[ref]
            _require(version["parent_source_hash"] == origin["parent_source_hash"]
                     and version["source_asset_id"] == origin["source_asset_id"]
                     and (version["source_width"], version["source_height"]) == (origin["source_width"], origin["source_height"])
                     and _rect_overlap(version["effective_source_rect"], origin["effective_source_rect"]),
                     "E_OCCURRENCE_ORIGIN", "revision must share its explicitly linked original source occurrence")
        covered.update(refs); occurrences.add(oid)
        resolved.append({"occurrence_id": oid, "origin": origin,
                         "revisions": [versions[ref] for ref in sorted(refs)],
                         "selected": versions[selected]})
    _require(covered == set(versions), "E_OCCURRENCE_COVERAGE", "selection must cover every current accepted native revision exactly once")
    return sorted(resolved, key=lambda group: group["occurrence_id"])


def assert_selected_boxes_disjoint(rows):
    """Conservative ambiguity gate: same-parent positive area overlap is blocked."""
    by_parent = {}
    for row in rows:
        bbox = json.loads(row.text_region_bbox_norm)
        dimensions = (row.source_asset_width, row.source_asset_height)
        rect = effective_native_rect(bbox, *dimensions)
        for other, other_box, other_rect, other_dimensions in by_parent.setdefault(row.source_asset_sha256, []):
            _require(dimensions == other_dimensions, "E_OCCURRENCE_DIMENSIONS", "same parent has conflicting dimensions")
            _require(not (_overlap(bbox, other_box) or _rect_overlap(rect, other_rect)), "E_OCCURRENCE_OVERLAP",
                     f"selected samples {other} and {row.sample_id} overlap on the same original parent or native raster footprint; explicit revision resolution required")
        by_parent[row.source_asset_sha256].append((row.sample_id, bbox, rect, dimensions))


@_structured_storage_errors
def select_occurrences(session, user_id, style_id, payload):
    _profile(session, user_id, style_id)
    _require(isinstance(payload, dict) and set(payload) == {"native_intake_sha256", "groups"},
             "E_OCCURRENCE_REQUEST", "accepts only current native_intake_sha256 and explicit groups")
    with LOCK:
        intake = current_native_intake(session, user_id, style_id)
        _require(intake is not None and payload["native_intake_sha256"] == intake["native_intake_sha256"],
                 "E_OCCURRENCE_STALE", "native intake changed; retrieve current occurrence inventory")
        groups = _resolve_groups(session, user_id, style_id, payload["groups"], intake)
        selected = {group["selected"]["sample_id"] for group in groups}
        native_ids = {version["sample_id"] for version in intake["versions"]}
        rows = session.query(TrainingSample).filter(TrainingSample.user_id == user_id,
            TrainingSample.style_id == str(style_id),
            TrainingSample.review_status.in_(("owner_confirmed", "ai_confirmed_authorized"))).all()
        assert_selected_boxes_disjoint([row for row in rows if row.sample_id not in native_ids or row.sample_id in selected])
        receipt = {"schema": SCHEMA, "user_id": user_id, "style_id": style_id,
                   "native_intake_sha256": intake["native_intake_sha256"], "native_intake": intake,
                   "authority": _authority(user_id), "groups": groups,
                   "selected_sample_ids": sorted(selected), "excluded_sample_ids": sorted(native_ids - selected),
                   "preserves_all_review_rows": True}
        receipt["plan_sha256"] = review_digest(receipt)
        # The request dependency commits AFTER this function returns. A Python
        # lock cannot protect that interval; let SQLite serialize inserts against
        # uncommitted competing requests, then verify the immutable exact value.
        _require(session.get_bind().dialect.name == "sqlite", "E_OCCURRENCE_STORAGE",
                 "occurrence selection currently requires SQLite transaction-safe upserts")
        session.execute(sqlite_insert(OccurrenceSelection).values(
            plan_sha256=receipt["plan_sha256"], user_id=user_id, style_id=style_id,
            receipt_json=canonical_json(receipt)).on_conflict_do_nothing(index_elements=["plan_sha256"]))
        existing = session.query(OccurrenceSelection).populate_existing().filter(
            OccurrenceSelection.plan_sha256 == receipt["plan_sha256"]).one()
        _require(existing.user_id == user_id and existing.style_id == style_id
                 and json.loads(existing.receipt_json) == receipt,
                 "E_OCCURRENCE_TAMPERED", "immutable occurrence selection changed")
        session.execute(sqlite_insert(CurrentOccurrenceSelection).values(
            user_id=user_id, style_id=style_id, plan_sha256=receipt["plan_sha256"]
        ).on_conflict_do_update(index_elements=["user_id", "style_id"],
                               set_={"plan_sha256": receipt["plan_sha256"]}))
        session.query(CurrentOccurrenceSelection).populate_existing().filter(
            CurrentOccurrenceSelection.user_id == user_id,
            CurrentOccurrenceSelection.style_id == style_id).one()
        audit(session, action="native_occurrence_revision_select", actor=receipt["authority"]["actor"],
              object_ref=f"styles/{style_id}", result=receipt["plan_sha256"])
        session.flush()
        return receipt


@_structured_storage_errors
def resolve_export_selection(session, user_id, style_id, confirmed, *, ledger=None):
    intake = current_native_intake(session, user_id, style_id, confirmed=confirmed, ledger=ledger)
    if intake is None:
        return list(confirmed), None
    _require(inspect(session.connection()).has_table(CurrentOccurrenceSelection.__tablename__),
             "E_OCCURRENCE_SELECTION_REQUIRED", "native accepted versions need an explicit occurrence/revision selection")
    pointer = session.get(CurrentOccurrenceSelection, (user_id, style_id))
    if pointer is not None:
        _require(isinstance(pointer.plan_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", pointer.plan_sha256),
                 "E_OCCURRENCE_TAMPERED", "malformed current occurrence pointer")
    record = session.get(OccurrenceSelection, pointer.plan_sha256) if pointer is not None else None
    _require(record is not None, "E_OCCURRENCE_SELECTION_REQUIRED", "select one current revision for every original occurrence")
    receipt = json.loads(record.receipt_json)
    _require(isinstance(receipt, dict) and receipt.get("schema") == SCHEMA
             and record.user_id == user_id and record.style_id == style_id
             and receipt.get("user_id") == user_id and receipt.get("style_id") == style_id
             and receipt.get("plan_sha256") == record.plan_sha256
             and review_digest({key: value for key, value in receipt.items() if key != "plan_sha256"}) == record.plan_sha256,
             "E_OCCURRENCE_TAMPERED", "stored occurrence selection identity/content mismatch")
    _require(receipt.get("native_intake") == intake
             and receipt.get("native_intake_sha256") == intake["native_intake_sha256"]
             and receipt.get("authority") == _authority(user_id),
             "E_OCCURRENCE_STALE", "source, accepted revisions, pixels, review or delegation changed; select current occurrences again")
    groups = receipt.get("groups")
    _require(isinstance(groups, list) and groups and all(isinstance(group, dict) for group in groups),
             "E_OCCURRENCE_TAMPERED", "stored occurrence groups must be nonempty objects")
    for group in groups:
        _require(set(group) == {"occurrence_id", "origin", "revisions", "selected"}
                 and isinstance(group["origin"], dict) and isinstance(group["selected"], dict)
                 and isinstance(group["revisions"], list) and group["revisions"]
                 and all(isinstance(version, dict) and "ref" in version for version in group["revisions"])
                 and "ref" in group["origin"] and "ref" in group["selected"],
                 "E_OCCURRENCE_TAMPERED", "malformed stored occurrence revision lineage")
    compact = [{"origin": group["origin"]["ref"], "revisions": [version["ref"] for version in group["revisions"]],
                "selected": group["selected"]["ref"]} for group in receipt["groups"]]
    resolved = _resolve_groups(session, user_id, style_id, compact, intake)
    selected = {group["selected"]["sample_id"] for group in resolved}
    native_ids = {version["sample_id"] for version in intake["versions"]}
    _require(resolved == receipt["groups"] and receipt.get("selected_sample_ids") == sorted(selected)
             and receipt.get("excluded_sample_ids") == sorted(native_ids - selected)
             and receipt.get("preserves_all_review_rows") is True,
             "E_OCCURRENCE_TAMPERED", "selection lineage or revision membership changed")
    retained = [row for row in confirmed if row.sample_id not in native_ids or row.sample_id in selected]
    assert_selected_boxes_disjoint(retained)
    return retained, receipt


@_structured_storage_errors
def occurrence_inventory(session, user_id, style_id):
    intake = current_native_intake(session, user_id, style_id)
    return {"native_intake_sha256": intake["native_intake_sha256"] if intake else None,
            "versions": intake["versions"] if intake else [],
            "selection_required": intake is not None,
            "selection_endpoint": f"/api/v1/styles/{style_id}/occurrence-selection"}
