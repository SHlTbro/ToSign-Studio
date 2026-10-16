"""S06/S07 run store: on-disk layout of one style run's working directory.

Layout (under YY_STORAGE_ROOT, per-user tree, OUTSIDE the six quota pools -
same treatment as the legacy styles/drafts layout; usage impact registered in
the S06/S07/S10 task result):

    users/{uid}/styles/runs/{run_id}/
        progress.json            worker heartbeat (S06 write, S07 read)
        engine.log               bounded engine stdout/stderr copy
        request.json             engine request (audit/reproducibility)
        receipts/engine_receipt.json   final engine receipt (exit-path truth)
        artifact/artifact.json   artifact manifest (S10 publish verifies it)
        artifact/<payload files> engine outputs listed by the manifest

Anti-fabrication contract (S07): every numeric field the read faces return
comes from this directory (worker/engine output) or is None. The read side
NEVER synthesizes epochs, percentages, eta seconds or "all zeros evaluated"
structures. Log reads are bounded (tail cap) - never unbounded.

The S05 trainer (B4, in parallel development) is expected to speak the same
file protocol: NDJSON progress lines on stdout consumed by the worker, a final
receipt JSON, and exit codes 0/2/3 (see engine/style/INTERFACE_CONTRACT_DRAFT
section 3/5). This module is the alignment point.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.core.storage import ensure_within_root, storage_root
from engine.style.m2_adapter.trainer_resume_protocol import native_path

# Bounded log discipline (S07 risk list: "日志无限读").
ENGINE_LOG_MAX_BYTES = 256 * 1024
LOG_TAIL_MAX_LINES = 50
LOG_TAIL_LINE_MAX_CHARS = 512
LOG_TAIL_MAX_CHARS = 16 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: os.PathLike | str) -> str:
    """Streamed file hash (same recipe as engine/style/m1_adapter/receipts)."""
    h = hashlib.sha256()
    with native_path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_sha256(obj) -> str:
    """Hash of a canonical JSON serialization (sorted keys, no NaN)."""
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_dir(user_id: int, run_id: int, *, makedirs: bool = False) -> Path:
    """Contained per-run directory; containment is re-checked against the
    storage root (D01) so crafted ids can never escape the tree."""
    base = storage_root() / "users" / str(int(user_id)) / "styles" / "runs" / str(int(run_id))
    resolved = ensure_within_root(base, storage_root())
    if makedirs:
        native_path(resolved).mkdir(parents=True, exist_ok=True)
    return resolved


def progress_path(user_id: int, run_id: int) -> Path:
    return run_dir(user_id, run_id) / "progress.json"


def engine_log_path(user_id: int, run_id: int) -> Path:
    return run_dir(user_id, run_id) / "engine.log"


def receipt_path(user_id: int, run_id: int) -> Path:
    return run_dir(user_id, run_id) / "receipts" / "engine_receipt.json"


def artifact_manifest_path(user_id: int, run_id: int) -> Path:
    return run_dir(user_id, run_id) / "artifact" / "artifact.json"


def write_json_atomic(path: Path, payload: dict) -> None:
    """Temp-file + os.replace so readers never observe a half-written JSON."""
    native_path(path.parent).mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".part-{os.getpid()}-{path.name}")
    native_path(tmp).write_text(json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False),
                   encoding="utf-8")
    os.replace(native_path(tmp), native_path(path))


def read_json(path: Path) -> dict | None:
    """Best-effort JSON read; missing/corrupt file -> None (never a guess)."""
    try:
        return json.loads(native_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_progress(user_id: int, run_id: int, payload: dict) -> None:
    """Worker heartbeat write; stamps heartbeat time + worker pid server-side."""
    body = dict(payload)
    body.setdefault("run_id", int(run_id))
    body["worker_pid"] = os.getpid()
    body["heartbeat_at"] = _now()
    write_json_atomic(progress_path(user_id, run_id), body)


def read_progress(user_id: int, run_id: int) -> dict | None:
    return read_json(progress_path(user_id, run_id))


def append_engine_log(user_id: int, run_id: int, text: str) -> None:
    """Append engine output, hard-capped at ENGINE_LOG_MAX_BYTES (write side of
    the bounded-log rule). Beyond the cap a marker is written once."""
    path = engine_log_path(user_id, run_id)
    native_path(path.parent).mkdir(parents=True, exist_ok=True)
    current = native_path(path).stat().st_size if native_path(path).exists() else 0
    if current >= ENGINE_LOG_MAX_BYTES:
        return
    room = ENGINE_LOG_MAX_BYTES - current
    chunk = text.encode("utf-8", errors="replace")
    if len(chunk) > room:
        chunk = chunk[:room] + b"\n[engine.log truncated at cap]\n"
    with native_path(path).open("ab") as f:
        f.write(chunk)


def engine_log_tail(user_id: int, run_id: int) -> list[str]:
    """Bounded tail: last LOG_TAIL_MAX_LINES lines, each line clipped to
    LOG_TAIL_LINE_MAX_CHARS and the whole payload clipped to
    LOG_TAIL_MAX_CHARS. Missing log -> empty list (never fabricated lines)."""
    path = engine_log_path(user_id, run_id)
    try:
        data = native_path(path).read_bytes()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    lines = [line[:LOG_TAIL_LINE_MAX_CHARS] for line in lines[-LOG_TAIL_MAX_LINES:]]
    out: list[str] = []
    used = 0
    for line in reversed(lines):  # newest lines survive the char cap first
        if used + len(line) > LOG_TAIL_MAX_CHARS:
            break
        out.append(line)
        used += len(line) + 1
    out.reverse()
    return out
