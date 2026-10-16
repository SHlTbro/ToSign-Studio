"""Canonical, dependency-light S03 binding for M2 requests and receipts.

Never substitutes a StyleSample-list digest for the frozen S03 content digest.
This module reads snapshots only and cannot repair historical artifacts.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .schema import DatasetError
from .snapshot import verify_snapshot

AUTHORITY = "S03_CONTENT_SHA256"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def verify_snapshot_binding(snapshot_dir, expected_sha256, *, base_dir=None) -> dict:
    if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
        raise DatasetError("E_SNAPSHOT_MISMATCH", "expected S03 SHA256 must be 64 lowercase hex characters")
    if not isinstance(snapshot_dir, (str, Path)) or not str(snapshot_dir).strip():
        raise DatasetError("E_SNAPSHOT_MISSING", "S03 snapshot directory is required")
    root = Path(snapshot_dir)
    if not root.is_absolute() and base_dir is not None:
        root = Path(base_dir) / root
    root = root.resolve()
    try:
        raw_index = (root / "index.json").read_bytes()
        index = json.loads(raw_index.decode("utf-8"))
        if not isinstance(index, dict) or not isinstance(index.get("files"), dict):
            raise ValueError("index/files must be objects")
        # Inspect paths before the existing verifier opens any indexed file.
        for meta in index["files"].values():
            rel = meta.get("path") if isinstance(meta, dict) else None
            if not isinstance(rel, str) or "\\" in rel or ":" in rel:
                raise ValueError("unsafe indexed sample path")
            parts = rel.split("/")
            if any(p in ("", ".", "..") for p in parts) or not (root / rel).resolve().is_relative_to(root):
                raise ValueError("indexed sample path escapes snapshot")
        rows = [json.loads(line) for line in (root / "samples.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        if not rows or any(not isinstance(row, dict) for row in rows):
            raise ValueError("snapshot samples must be nonempty objects")
        ids = [row.get("sample_id") for row in rows]
        if any(not isinstance(sid, str) for sid in ids) or len(set(ids)) != len(ids) or set(ids) != set(index["files"]):
            raise ValueError("sample IDs must uniquely and exactly cover index.files")
        if index.get("style_id") in (None, ""):
            raise ValueError("snapshot style identity is required")
        for row in rows:
            meta = index["files"][row["sample_id"]]
            if row.get("snapshot_file") != meta["path"]:
                raise ValueError("sample row path differs from its hash-verified indexed path")
            if str(row.get("style_id")) != str(index["style_id"]):
                raise ValueError("sample row style differs from snapshot style")
            if row.get("final_alpha_sha256") is not None and row["final_alpha_sha256"] != meta.get("sha256"):
                raise ValueError("sample alpha hash differs from indexed hash")
        split_file = root / "split.json"
        if not split_file.is_file():
            raise ValueError("M2 requires a frozen train/val split")
        split = json.loads(split_file.read_text(encoding="utf-8")).get("assignments")
        if not isinstance(split, dict) or set(split) != set(ids) or any(side not in ("train", "val") for side in split.values()):
            raise ValueError("split must exactly cover sample IDs with train/val assignments")
        if index.get("n_samples") is not None and index["n_samples"] != len(rows):
            raise ValueError("sample count differs from index")
        result = verify_snapshot(root)
        if result.status != "OK":
            raise DatasetError("E_SNAPSHOT_INVALID", "; ".join(f.code for f in result.failures))
        if index.get("content_sha256") != expected_sha256 or index.get("snapshot_sha256") != expected_sha256:
            raise DatasetError("E_SNAPSHOT_MISMATCH", "verified S03 content/index digest differs from requested authority")
        expected_marker = f"FROZEN {index['snapshot_id']} {expected_sha256}"
        if (root / "FROZEN").read_text(encoding="utf-8").strip() != expected_marker:
            raise DatasetError("E_SNAPSHOT_MISMATCH", "FROZEN marker differs from S03 identity")
    except DatasetError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DatasetError("E_SNAPSHOT_INVALID", f"cannot verify S03 snapshot: {exc}") from exc
    return {"authority": AUTHORITY, "snapshot_id": index["snapshot_id"], "style_id": index.get("style_id"),
            "snapshot_sha256": expected_sha256, "snapshot_dir": str(root),
            "index_sha256": hashlib.sha256(raw_index).hexdigest(),
            "provenance_sha256": index.get("provenance_sha256")}


def verify_m2_receipt_binding(receipt: dict, binding: dict) -> None:
    """New M2 runs must return the exact authority verified before launch."""
    received = receipt.get("snapshot_binding")
    if not isinstance(received, dict):
        raise DatasetError("E_SNAPSHOT_RECEIPT_MISMATCH", "M2 receipt lacks canonical snapshot_binding")
    keys = ("authority", "snapshot_id", "style_id", "snapshot_sha256", "index_sha256", "provenance_sha256")
    if receipt.get("dataset_snapshot_sha256") != binding["snapshot_sha256"] or any(received.get(k) != binding.get(k) for k in keys):
        raise DatasetError("E_SNAPSHOT_RECEIPT_MISMATCH", "M2 receipt does not bind the requested verified S03 snapshot")
