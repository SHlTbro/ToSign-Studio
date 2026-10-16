"""Authenticated, append-only family selection, independent of model code.

Only the authenticated service may issue a receipt from a fresh exporter report.
A request supplies parent assignments, never identity, intake, source provenance,
review results, paths, gates, or authorization claims. Loading a historical split
is not an issuance path and a split receipt never constitutes a visual review.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.core import storage
from app.domains.training_sets.source_admission import assert_source_permitted
from engine.style.dataset import DatasetError, SplitConfig, SplitPlan, validate_pool
from engine.style.dataset.schema import canonical_json, review_digest, sha256_bytes, validate_row
from engine.style.dataset.split import _union_find_components
from engine.style.dataset import snapshot as snapshot_format
from engine.style.visual_review.policy import candidate_delegation

_SCHEMA = "authenticated-family-split/1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RECEIPT_FIELDS = {"schema", "user_id", "style_id", "base_intake_sha256", "r04_ledger_sha256",
                   "parent_assignments", "source_provenance", "authorization", "plan_sha256"}


def _identity(value, name):
    if type(value) is not int or value < 1:
        raise DatasetError("E_SPLIT_IDENTITY", f"{name} must be a positive authenticated integer")
    return value


def _hash(value, name):
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise DatasetError("E_SPLIT_IDENTITY", f"{name} must be a lowercase SHA256")
    return value


def _sources(value):
    if not isinstance(value, list) or not value:
        raise DatasetError("E_SPLIT_PROVENANCE", "verified current source provenance is required")
    seen = set()
    result = []
    for source in value:
        if not isinstance(source, dict):
            raise DatasetError("E_SPLIT_PROVENANCE", "source provenance must contain objects")
        _hash(source.get("source_asset_sha256"), "source_asset_sha256")
        aid = source.get("source_asset_id")
        if not isinstance(aid, str) or not aid or aid in seen:
            raise DatasetError("E_SPLIT_PROVENANCE", "source asset identities must be unique and nonempty")
        receipt = source.get("source_admission")
        if (not isinstance(receipt, dict) or receipt.get("asset_id") != aid
                or receipt.get("source_sha256") != source["source_asset_sha256"]):
            raise DatasetError("E_SPLIT_PROVENANCE", "parent lacks verified canonical source admission")
        _hash(receipt.get("receipt_sha256"), "source admission receipt_sha256")
        seen.add(aid)
        # These records come from the canonical exporter after file/admission checks.
        # Round-trip excludes mutable references to a caller's report.
        result.append(json.loads(canonical_json(source)))
    return sorted(result, key=lambda s: (s["source_asset_sha256"], s["source_asset_id"]))


def _assignments(value, sources):
    if not isinstance(value, dict) or not value:
        raise DatasetError("E_SPLIT_ASSIGNMENTS", "explicit parent SHA256 to train/val assignments are required")
    for parent, side in value.items():
        _hash(parent, "assigned parent")
        if side not in ("train", "val"):
            raise DatasetError("E_SPLIT_ASSIGNMENTS", "only train and val assignments are permitted")
    expected = {source["source_asset_sha256"] for source in sources}
    if set(value) != expected:
        raise DatasetError("E_SPLIT_COVERAGE", "assignments must exactly cover every current eligible parent; missing or unlisted parents are forbidden")
    if set(value.values()) != {"train", "val"}:
        raise DatasetError("E_EMPTY_SPLIT", "explicit plan must include both train and val")
    return dict(sorted(value.items()))


def family_split_plan_path(user_id, style_id, plan_sha256):
    uid = _identity(user_id, "user_id")
    sid = _identity(style_id, "style_id")
    digest = _hash(plan_sha256, "plan_sha256")
    root = storage.storage_root()
    return storage.ensure_within_root(root / "users" / str(uid) / "styles" / "split-plans" /
                                      str(sid) / (digest + ".json"), root)


def _authorization(user_id, actor_kind):
    if actor_kind == "authenticated_user":
        return {"actor_kind": actor_kind, "actor": f"user:{user_id}",
                "method": "explicit_current_intake_family_selection", "review_evidence": False}
    if actor_kind != "delegated_ai":
        raise DatasetError("E_SPLIT_UNAUTHORIZED", "split selection actor is not supported")
    policy = candidate_delegation(user_id)
    if policy is None:
        raise DatasetError("E_SPLIT_UNAUTHORIZED", "current user has no scoped AI intake delegation")
    return {"actor_kind": "delegated_ai", "actor": "ai:delegated_intake",
            "method": "explicit_current_intake_family_selection", "review_evidence": False,
            "policy_sha256": policy["_sha256"],
            "authority": policy["candidate_confirmation"]["authorization"]}


def _source_ownership(sources, user_id, style_id):
    if any(source["source_admission"].get("user_id") != user_id
           or str(source["source_admission"].get("style_id")) != str(style_id) for source in sources):
        raise DatasetError("E_SPLIT_IDENTITY", "source admissions are not bound to current user and style")


def authorize_family_split_plan(*, user_id: int, style_id: int, intake_report: dict,
                                parent_assignments: dict, actor_kind: str = "delegated_ai") -> dict:
    """Record explicit authenticated selection from a SERVER-generated preflight.

    Do not expose this callable as an endpoint accepting an intake_report. The
    service must run export itself using its authenticated user and owned style.
    Stable receipts make retries idempotent; they attest selection, never review.
    """
    uid, sid = _identity(user_id, "user_id"), _identity(style_id, "style_id")
    if (not isinstance(intake_report, dict) or intake_report.get("target_style_id") != sid
            or intake_report.get("target_user_id") != uid):
        raise DatasetError("E_SPLIT_IDENTITY", "preflight is not bound to the authenticated user and style")
    sources = _sources(intake_report.get("eligible_parent_sources"))
    _source_ownership(sources, uid, sid)
    payload = {"schema": _SCHEMA, "user_id": uid, "style_id": sid,
               "base_intake_sha256": _hash(intake_report.get("base_intake_sha256"), "base_intake_sha256"),
               "r04_ledger_sha256": _hash(intake_report.get("r04_ledger_sha256"), "r04_ledger_sha256"),
               "parent_assignments": _assignments(parent_assignments, sources),
               "source_provenance": sources,
               "authorization": _authorization(uid, actor_kind)}
    payload["plan_sha256"] = review_digest(payload)
    path = family_split_plan_path(uid, sid, payload["plan_sha256"])
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (canonical_json(payload) + "\n").encode("utf-8")
    try:
        with path.open("xb") as stream:
            stream.write(raw)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise DatasetError("E_SPLIT_TAMPERED", "existing split authorization differs from its immutable identity")
    return payload


def load_family_split_plan(value, *, user_id: int, style_id: int) -> dict:
    """Accept a receipt id or exact stored receipt, never a self-signed client plan."""
    if isinstance(value, dict):
        if set(value) != _RECEIPT_FIELDS:
            raise DatasetError("E_SPLIT_PLAN", "split authorization has missing or unknown fields")
        digest = value.get("plan_sha256")
    else:
        digest = value
    path = family_split_plan_path(user_id, style_id, digest)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetError("E_SPLIT_UNAUTHORIZED", "no recorded selection for this authenticated user and style") from exc
    if not isinstance(stored, dict) or set(stored) != _RECEIPT_FIELDS:
        raise DatasetError("E_SPLIT_TAMPERED", "malformed recorded family selection")
    actual = review_digest({key: val for key, val in stored.items() if key != "plan_sha256"})
    if (stored.get("schema") != _SCHEMA or stored.get("plan_sha256") != digest or actual != digest
            or stored.get("user_id") != user_id or stored.get("style_id") != style_id
            or (isinstance(value, dict) and value != stored)):
        raise DatasetError("E_SPLIT_TAMPERED", "split authorization identity/content mismatch")
    auth = stored.get("authorization")
    expected_auth = _authorization(user_id, auth.get("actor_kind") if isinstance(auth, dict) else None)
    if stored.get("authorization") != expected_auth:
        raise DatasetError("E_SPLIT_UNAUTHORIZED", "split selection lacks authenticated authorization")
    return stored


def _complete_family_components(rows, ledger):
    """Extend production groups through absent intermediate ledger relatives.

    A--B and B--C imply A/C share a family even when B has no current sample.
    All ledger edges participate; only current sample parents are assigned.
    """
    initial = _union_find_components(rows, ledger)
    merged = {}
    for component in initial:
        closure = set(component["parent_hashes"])
        while True:
            before = len(closure)
            for members in ledger.families.values():
                if closure.intersection(members):
                    closure.update(members)
            if len(closure) == before:
                break
        key = tuple(sorted(closure))
        target = merged.setdefault(key, {"parent_hashes": set(), "sample_ids": set(),
                                        "family_ids": set(), "family_closure_hashes": list(key)})
        target["parent_hashes"].update(component["parent_hashes"])
        target["sample_ids"].update(component["sample_ids"])
        target["family_ids"].update(fid for fid, members in ledger.families.items() if closure.intersection(members))
    return [{"component_id": f"explicit-{index:04d}",
             **{name: sorted(value) for name, value in component.items()}}
            for index, (_, component) in enumerate(sorted(merged.items()))]


def validate_family_split_plan(value, *, user_id: int, style_id: int, base_intake_sha256: str,
                               source_provenance: list[dict], rows: list[dict], ledger) -> tuple[SplitPlan, dict]:
    """Bind receipt to current provenance and run unchanged production S03 gates."""
    receipt = load_family_split_plan(value, user_id=user_id, style_id=style_id)
    sources = _sources(source_provenance)
    _source_ownership(sources, user_id, style_id)
    if (receipt["base_intake_sha256"] != base_intake_sha256
            or receipt["r04_ledger_sha256"] != ledger.sha256 or receipt["source_provenance"] != sources):
        raise DatasetError("E_SPLIT_STALE", "intake, ledger or source authorization changed; select current families again")
    parent_assignments = _assignments(receipt["parent_assignments"], sources)
    for parent in parent_assignments:
        assert_source_permitted(ledger, parent)
    cfg = SplitConfig()  # No client/custom gates: original minima and UNKNOWN rules.
    pre = validate_pool(rows, ledger=ledger, unknown_parent_policy=cfg.unknown_parent_policy)
    pre.raise_if_bad()
    if any(str(row["style_id"]) != str(style_id) for row in rows):
        raise DatasetError("E_SPLIT_IDENTITY", "sample style differs from authenticated plan")
    if {row["parent_source_hash"] for row in rows} != set(parent_assignments):
        raise DatasetError("E_SPLIT_COVERAGE", "plan does not exactly cover current sample parents")
    components = _complete_family_components(rows, ledger)
    for component in components:
        if len({parent_assignments[parent] for parent in component["parent_hashes"]}) != 1:
            raise DatasetError("E_FAMILY_CROSS_SPLIT", "an R04 source family cannot cross train and validation")
    assignments = {row["sample_id"]: parent_assignments[row["parent_source_hash"]] for row in rows}
    result = validate_pool(rows, splits=assignments, ledger=ledger,
                           unknown_parent_policy=cfg.unknown_parent_policy, gates=cfg)
    reasons = ([f"{failure.code}: {failure.detail}" for failure in result.failures]
               if result.status == "FAIL" else result.insufficient_reasons)
    return SplitPlan(result.status, assignments, components, result.stats, reasons, result), receipt


def bound_intake_sha256(base_intake_sha256: str, plan_sha256: str) -> str:
    return review_digest({"schema": "family-bound-intake/1",
                          "base_intake_sha256": _hash(base_intake_sha256, "base_intake_sha256"),
                          "family_split_plan_sha256": _hash(plan_sha256, "plan_sha256")})


def build_authorized_snapshot(rows: list[dict], out_dir, *, ledger, plan: SplitPlan,
                              created_at: str, provenance_metadata: dict):
    """Write a NEW explicit-split S03 snapshot using the frozen engine's format.

    The engine's builder can only choose a random plan. This additive adapter
    avoids modifying engine code or freezing and then rewriting any snapshot.
    Validation, family components, content hashing, read-only marking and the
    final verifier are the existing production implementations.
    """
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise DatasetError("E_SNAPSHOT_EXISTS", "snapshot cannot be overwritten")
    if ledger.is_v2():
        ledger.assert_r04_integrity()
    else:
        ledger.assert_sources_valid()
    clean = [validate_row(row) for row in rows]
    cfg = SplitConfig()
    validated, receipt = validate_family_split_plan(provenance_metadata.get("family_split_plan"),
        user_id=provenance_metadata.get("target_user_id"), style_id=provenance_metadata.get("target_style_id"),
        base_intake_sha256=provenance_metadata.get("base_intake_sha256"),
        source_provenance=provenance_metadata.get("eligible_parent_sources"), rows=clean, ledger=ledger)
    if validated.assignments != plan.assignments:
        raise DatasetError("E_SPLIT_TAMPERED", "build assignments differ from authorized selection")
    if validated.status == "FAIL":
        validated.validation.raise_if_bad()
    if validated.status != "OK":
        return snapshot_format.BuildResult(status=validated.status, plan=validated,
                                            insufficient_reasons=list(validated.reasons))
    audit = provenance_metadata.get("dataset_visual_audit")
    if (not isinstance(audit, dict) or audit.get("schema") != "dataset-visual-audit/2"
            or audit.get("status") != "PASS" or audit.get("may_freeze_new_snapshot") is not True):
        raise DatasetError("E_SNAPSHOT_VISUAL_REVIEW", "explicit split authorization is not a pre-freeze visual review")
    reviewed = audit.get("reviews")
    if (not isinstance(reviewed, list) or len(reviewed) != len(clean)
            or {review.get("candidate_id") for review in reviewed if isinstance(review, dict)} !=
               {row["sample_id"] for row in clean}):
        raise DatasetError("E_SNAPSHOT_VISUAL_REVIEW", "pre-freeze audit must cover every current sample exactly once")
    by_id = {row["sample_id"]: row for row in clean}
    for review in reviewed:
        human = review.get('schema') == 'human-input-visual-review/1'
        row = by_id[review['candidate_id']]
        if human:
            checks = review.get('checks') or {}
            binding = review.get('binding') or {}
            if (review.get('decision') != 'accept' or review.get('review_status') != 'human_visual_accepted'
                    or review.get('actor_kind') != 'authenticated_user'
                    or review.get('actor') != 'user:' + str(provenance_metadata['target_user_id'])
                    or review.get('owner_confirmation') != 'UNCHANGED' or review.get('model_quality_approved') is not False
                    or set(checks) != {'source_label_matches','strokes_complete','alpha_usable','suspicions_checked'}
                    or any(value is not True for value in checks.values())
                    or review_digest(binding) != review.get('binding_sha256')
                    or review_digest({k:v for k,v in review.items() if k != 'receipt_sha256'}) != review.get('receipt_sha256')
                    or binding.get('user_id') != provenance_metadata['target_user_id']
                    or binding.get('style_id') != provenance_metadata['target_style_id']
                    or binding.get('sample_id') != row['sample_id'] or binding.get('label') != row['label']
                    or binding.get('image_sha256') != row['final_alpha_sha256']
                    or binding.get('source_sha256') != row['parent_source_hash']
                    or binding.get('source_bbox') != row['source_bbox']
                    or binding.get('structural_checks', {}).get('clear_errors') != []
                    or row.get('review_status') != 'owner_confirmed'):
                raise DatasetError('E_SNAPSHOT_VISUAL_REVIEW', 'human audit lacks complete current input binding')
        if ((not human and review.get("review_status") != "ai_confirmed_authorized")
                or review.get("image_sha256") != by_id[review["candidate_id"]]["final_alpha_sha256"]):
            raise DatasetError("E_SNAPSHOT_VISUAL_REVIEW", "pre-freeze audit does not bind accepted current pixels")
    expected_intake = bound_intake_sha256(provenance_metadata["base_intake_sha256"], receipt["plan_sha256"])
    if provenance_metadata.get("intake_sha256") != expected_intake:
        raise DatasetError("E_INTAKE_CHANGED", "snapshot intake does not include the authorized split identity")
    logical_id = provenance_metadata.get("snapshot_id")
    if not isinstance(logical_id, str) or not logical_id.startswith("ps-" + receipt["plan_sha256"] + "-"):
        raise DatasetError("E_SNAPSHOT_PROVENANCE", "snapshot identity must include its family selection")
    files, file_hashes, stored_rows = {}, {}, []
    for row in sorted(clean, key=lambda row: row["sample_id"]):
        sid = row["sample_id"]
        storage.check_segment(sid, "sample_id")
        path = Path(row.get("alpha_path") or "")
        if not row.get("alpha_path") or not path.is_file():
            raise DatasetError("E_ALPHA_MISSING", f"sample {sid} has no alpha file")
        data = path.read_bytes()
        if sha256_bytes(data) != row["final_alpha_sha256"]:
            raise DatasetError("E_ALPHA_HASH_MISMATCH", f"sample {sid} bytes changed before freezing")
        files[sid], file_hashes[sid] = data, sha256_bytes(data)
        stored_rows.append({**{key: val for key, val in row.items() if key != "alpha_path"},
                            "snapshot_file": f"samples/{sid}.png"})
    samples_bytes = ("\n".join(canonical_json(row) for row in stored_rows) + "\n").encode("utf-8")
    split_payload = {"assignments": validated.assignments, "plan": {
        "status": "OK", "strategy": "authenticated_explicit_family_selection",
        "family_split_plan_sha256": receipt["plan_sha256"],
        "unknown_parent_policy": cfg.unknown_parent_policy,
        "components": validated.components, "stats": validated.stats}}
    split_bytes = (canonical_json(split_payload) + "\n").encode("utf-8")
    provenance_bytes = (canonical_json(provenance_metadata) + "\n").encode("utf-8")
    content_sha = snapshot_format._content_hash(samples_bytes, split_bytes, file_hashes, provenance_bytes)
    index = {"tool_version": snapshot_format.DATASET_TOOL_VERSION, "snapshot_id": logical_id,
             "snapshot_sha256": content_sha, "created_at": created_at,
             "style_id": str(provenance_metadata["target_style_id"]), "n_samples": len(clean),
             "ledger_path": str(ledger.path), "ledger_sha256": ledger.sha256,
             "unknown_parent_policy": cfg.unknown_parent_policy, "has_split": True,
             "counts": validated.stats, "content_sha256": content_sha,
             "files": {sid: {"path": f"samples/{sid}.png", "sha256": digest}
                       for sid, digest in sorted(file_hashes.items())},
             "provenance_file": "provenance.json", "provenance_sha256": sha256_bytes(provenance_bytes)}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "samples").mkdir()
    output_files = {"samples.jsonl": samples_bytes, "split.json": split_bytes,
                    "provenance.json": provenance_bytes,
                    "index.json": (canonical_json(index) + "\n").encode("utf-8"),
                    **{f"samples/{sid}.png": data for sid, data in files.items()},
                    "FROZEN": f"FROZEN {logical_id} {content_sha}\n".encode("utf-8")}
    for name, data in output_files.items():
        with (out_dir / name).open("xb") as stream:
            stream.write(data)
        snapshot_format._make_readonly(out_dir / name)
    verification = snapshot_format.verify_snapshot(out_dir, ledger=ledger)
    verification.raise_if_bad()
    return snapshot_format.BuildResult(status="OK", snapshot_dir=str(out_dir), snapshot_id=logical_id,
        snapshot_sha256=content_sha, index=index, plan=validated, validation=validated.validation)
