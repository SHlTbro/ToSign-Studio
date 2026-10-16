"""Read-only, owner-scoped transport of immutable native review request bytes.

An export is neither a review nor trusted evidence import. No credentials, score
submission, provider connection, identity renewal, or sample write happens here.
"""
from __future__ import annotations

import hashlib
import re
import tempfile
import zipfile
from dataclasses import dataclass
from typing import BinaryIO

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core import storage
from app.core.errors import ApiError
from app.domains.assets_upload.models import Asset
from engine.style.dataset.schema import canonical_json, review_digest
from . import candidate_review_jobs as jobs
from .source_admission import assert_source_permitted, checked_ledger, require_training_source, source_path

_CANDIDATE = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_WINDOWS_DEVICES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_PACKAGE_FIELDS = {"schema", "job_id", "nonce", "user_id", "style_id", "created_at",
                   "policy_sha256", "authorization", "ledger_sha256", "application_version",
                   "items", "status", "human_confirmation", "canonical_samples_created"}


def capabilities() -> dict:
    # The native-job transport has no online provider path. Reviewer allowlisting
    # in the importer is not a connection or proof that a real review occurred.
    return {"request_export": True, "http_raw_review_import": False,
            "trusted_import": "operator_authenticated_hash_pinned_cli",
            "online_provider_status": "NOT_CONFIGURED", "export_is_review_acceptance": False}


@dataclass
class ReviewRequestArchive:
    stream: BinaryIO
    job_id: str
    package_sha256: str
    archive_sha256: str
    code_version_matches: bool
    size_bytes: int

    def close(self):
        self.stream.close()

    def chunks(self):
        try:
            while chunk := self.stream.read(1024 * 1024):
                yield chunk
        finally:
            self.close()


def _read_exact(path, expected: str, *, root) -> bytes:
    jobs.require(isinstance(expected, str) and jobs.HASH_RE.fullmatch(expected),
                 "REVIEW_JOB_CHANGED", "review request contains an invalid byte checksum")
    jobs.require(root.resolve() == root, "REVIEW_JOB_CHANGED", "owned request directory cannot alias another location")
    safe = storage.ensure_within_root(path, root)
    jobs.require(safe.is_file(), "REVIEW_JOB_CHANGED", "review request bytes are unavailable")
    data = safe.read_bytes()
    jobs.require(jobs.review_digest_bytes(data) == expected,
                 "REVIEW_JOB_CHANGED", "review request byte checksum changed")
    return data


def export_request(session, user_id: int, job_id: str) -> ReviewRequestArchive:
    """Build one access-consistent archive without changing caller or database state.

    A fresh Session avoids reusing cached job/source/admission authority. SQLite's
    writer reservation precedes every authority read and lasts through the final
    spool hash, so another transaction cannot revoke/transfer sources midway.
    Rollback releases it before HTTP streaming and changes no persistent values.
    """
    bind = session.get_bind()
    if bind.dialect.name != "sqlite":
        raise ApiError(409, "REVIEW_EXPORT_ATOMICITY_UNAVAILABLE", "Atomic review export requires SQLite")
    with Session(bind=bind, autoflush=False, expire_on_commit=False) as export_session:
        try:
            try:
                export_session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            except OperationalError as exc:
                raise ApiError(409, "REVIEW_EXPORT_BUSY", "Source access is changing; retry the review export", retryable=True) from exc
            return _export_reserved_request(export_session, user_id, job_id)
        finally:
            export_session.rollback()


def _export_reserved_request(session, user_id: int, job_id: str) -> ReviewRequestArchive:
    """Package historical identities verbatim while enforcing current data access.

    Stale code does not rewrite or renew a package. It is exposed in the manifest
    and response header; the existing trusted import/apply current-code gate is
    unchanged. Current source permission and R04 restrictions still apply.
    """
    job, package = jobs.get_job(session, user_id, job_id)
    jobs.profile_for(session, user_id, job.style_id)
    jobs.require(set(package) == _PACKAGE_FIELDS and isinstance(package.get("nonce"), str)
                 and jobs.JOB_RE.fullmatch(package["nonce"]),
                 "REVIEW_JOB_CHANGED", "review request identity fields are malformed")
    root = jobs.job_root(user_id, job_id)
    package_path = storage.ensure_within_root(root / "package.json", root)
    raw_package = package_path.read_bytes()
    jobs.require(jobs.strict_json(raw_package.decode("utf-8")) == package,
                 "REVIEW_JOB_CHANGED", "review package changed during export")
    version_matches = package.get("application_version") == jobs.code_version()
    ledger = checked_ledger()
    files = {}
    # Build fully before exposing the response, but spill the ZIP off the heap.
    # Only one source/crop is buffered at a time, not the entire owned source pool.
    stream = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    archive = zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED)

    def add_file(name, raw):
        identity = {"sha256": jobs.review_digest_bytes(raw), "size_bytes": len(raw)}
        if name in files:
            jobs.require(files[name] == identity, "REVIEW_JOB_CHANGED", "conflicting archive file identity")
            return
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100600 << 16
        archive.writestr(info, raw)
        files[name] = identity

    try:
        add_file("package.json", raw_package)
        assets = {}
        item_files = []
        seen = set()
        for item in package["items"]:
            cid = item.get("candidate_id")
            jobs.require(isinstance(cid, str) and _CANDIDATE.fullmatch(cid)
                         and cid.casefold() not in seen and cid.casefold() not in _WINDOWS_DEVICES,
                         "REVIEW_JOB_CHANGED", "unsafe or duplicate candidate archive identity")
            seen.add(cid.casefold())
            jobs.require(item.get("candidate_version_sha256") == review_digest(
                {key: value for key, value in item.items() if key != "candidate_version_sha256"}),
                "REVIEW_JOB_CHANGED", "candidate version identity changed")
            asset_id = item.get("source_asset_id")
            if asset_id not in assets:
                asset = session.query(Asset).filter(Asset.asset_id == asset_id, Asset.user_id == str(user_id),
                                                    Asset.enabled == 1).first()
                jobs.require(asset is not None, "REVIEW_JOB_CHANGED", "owned source is unavailable")
                # Validate the access boundary before any source or derived pixel read.
                assert_source_permitted(ledger, asset.sha256)
                owned_root = storage.storage_root().resolve() / str(user_id) / "assets"
                jobs.require(owned_root.resolve() == owned_root, "REVIEW_JOB_CHANGED",
                             "owned source directory cannot alias another location")
                owned_path = storage.ensure_within_root(source_path(asset, user_id), owned_root)
                admission = require_training_source(session, asset, user_id, job.style_id, ledger)
                jobs.require(asset.sha256 == item.get("source_image_sha256")
                             and admission["receipt_sha256"] == item.get("source_admission_sha256"),
                             "REVIEW_JOB_CHANGED", "source admission or version changed")
                raw_source = _read_exact(owned_path, asset.sha256, root=owned_root)
                name = f"sources/{asset.sha256}.bin"
                add_file(name, raw_source)
                del raw_source
                assets[asset_id] = (asset.sha256, admission["receipt_sha256"], name)
            digest, admission_digest, source_name = assets[asset_id]
            jobs.require(digest == item.get("source_image_sha256")
                         and admission_digest == item.get("source_admission_sha256"),
                         "REVIEW_JOB_CHANGED", "candidate source binding changed")
            folder = storage.ensure_within_root(root / cid, root)
            paths = {}
            for kind, hash_key in (("alpha", "image_sha256"), ("source-context", "source_context_sha256")):
                name = f"candidates/{cid}/{kind}.png"
                add_file(name, _read_exact(folder / (kind + ".png"), item.get(hash_key), root=folder))
                paths[kind.replace("-", "_") + "_path"] = name
            item_files.append({"candidate_id": cid, "candidate_version_sha256": item["candidate_version_sha256"],
                               **paths, "source_path": source_name})
        from .candidate_review_ingress import export_binding
        ingress = export_binding(session, user_id, job_id)
        if ingress is not None:
            add_file('review-ingress-request.json', (canonical_json(ingress) + '\n').encode('utf-8'))
        manifest = {"schema": "native-candidate-review-request-export/1", "job_id": job_id,
            "user_id": user_id, "style_id": job.style_id, "nonce": package["nonce"],
            "package_sha256": job.package_sha256, "package_file_sha256": jobs.review_digest_bytes(raw_package),
            "package_digest_kind": "canonical_json_sha256", "policy_sha256": package["policy_sha256"],
            "ledger_sha256": package["ledger_sha256"], "application_version": package["application_version"],
            "code_version_matches": version_matches,
            "status": "REQUEST_ONLY" if version_matches else "HISTORICAL_STALE_CODE_REQUEST_ONLY",
            "job_status": job.status, "capabilities": capabilities(), "items": item_files,
            "files": dict(sorted(files.items())),
            "review_ingress": ingress, "canonical_samples_created": False, "review_accepted": False, "human_confirmation": False}
        add_file("request-manifest.json", (canonical_json(manifest) + "\n").encode("utf-8"))
        archive.close()
        # R04 and admission policy are file-backed authority, outside the SQLite
        # reservation. Recheck them after copying bytes; drift invalidates the
        # whole spool instead of mixing permissions from different versions.
        current_ledger = checked_ledger()
        jobs.require(current_ledger.sha256 == ledger.sha256, "REVIEW_EXPORT_CHANGED",
                     "source ledger changed while preparing the review export")
        for asset_id, (digest, admission_digest, _) in assets.items():
            asset = session.query(Asset).filter(Asset.asset_id == asset_id, Asset.user_id == str(user_id),
                                                Asset.enabled == 1).first()
            admission = require_training_source(session, asset, user_id, job.style_id, current_ledger)
            jobs.require(asset.sha256 == digest and admission["receipt_sha256"] == admission_digest,
                         "REVIEW_EXPORT_CHANGED", "source authority changed while preparing the review export")
        size = stream.tell()
        stream.seek(0)
        digest = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        stream.seek(0)
        return ReviewRequestArchive(stream, job_id, job.package_sha256, digest.hexdigest(), version_matches, size)
    except BaseException:
        archive.close()
        stream.close()
        raise
