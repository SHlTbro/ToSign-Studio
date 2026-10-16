"""Unified storage-root utilities (D01, batch fb-platform 2026-09-27).

Single source of truth for the on-disk layout under ``YY_STORAGE_ROOT``:

==============================  ============================================
layout                          producer / consumer
==============================  ==============================  ============
``<root>/<uid>/<pool>/...``     per-user pools walked by GET /usage
``<root>/users/<uid>/styles/    extraction alpha drafts (legacy layout,
drafts/<hex16>.png``            kept for compatibility - counted by usage)
``<root>/_backups/<id>/``       admin backups (root-level, outside the
                                per-user walk by design)
==============================  ============================================

What this module guarantees (D01 DoD):

- **Containment**: every path this module derives from external strings is
  validated segment-by-segment (no separators, no ``..``, no drive/root
  parts) AND re-checked with ``os.path.realpath`` so an existing junction or
  symlink ancestor that escapes the storage root is rejected. Negative or
  positive callers cannot escape the root even with crafted ids/filenames.
- **Atomic writes**: bytes and PIL images land via ``.part-<rand>`` temp file
  in the SAME directory + ``os.replace`` (atomic on same volume), temp always
  removed on failure - a disk-full or crashed write never leaves a half PNG
  at the final path.
- **Quota accounting**: the walk that GET /usage reports and the gate the
  writers enforce share :func:`user_usage_bytes` (six pools + drafts), so the
  displayed number and the enforced number can never disagree. The process
  local :data:`QUOTA_LOCK` serializes check->write->commit for all NEW
  writers; legacy writers (assets_upload/training_sets) keep their own
  ``_QUOTA_LOCK`` until their domain owner migrates them (see
  task-results/d01-d09-2026-09-27.md wiring sheet).
"""

import os
import threading
import uuid
from pathlib import Path, PureWindowsPath
from typing import Callable

from app.core.errors import ApiError

# Single quota constant; app.domains.usage.service re-exports it as
# LIMIT_BYTES for the legacy importers (assets_upload, training_sets).
QUOTA_LIMIT_BYTES = 5_368_709_120  # 5 GiB per user

# Process-local lock serializing quota-critical sections of NEW writers
# (single-process deployment per dispatch G72-7). Held around the short
# usage-walk + check + write + DB-commit section only, never around decode.
QUOTA_LOCK = threading.Lock()

# Frozen six-pool API contract (patch-C section 11.7); drafts are accounted
# separately - see app/domains/usage/service.py.
USER_POOLS = ("assets", "training_images", "style_samples", "outputs", "backups", "models")

# Legacy extraction draft layout (extraction/service.py _store_draft). Kept
# verbatim so existing draft files/URLs stay readable; new writes go through
# this module so usage sees them.
_DRAFTS_PARTS = ("users", "{user_id}", "styles", "drafts")


class StoragePathError(ApiError):
    """Raised when a derived path would leave the storage root (D01 canary)."""

    def __init__(self, detail: str):
        super().__init__(400, "INVALID_REQUEST", "Invalid storage path", detail)


def storage_root() -> Path:
    """Absolute storage root from YY_STORAGE_ROOT (RuntimeError if unset).

    Matches the historical contract of app.domains.usage.service.storage_root;
    the only difference is the return type (absolute Path)."""
    raw = os.environ.get("YY_STORAGE_ROOT")
    if not raw:
        raise RuntimeError("YY_STORAGE_ROOT is not set")
    return Path(os.path.abspath(raw))


def check_segment(value: object, kind: str) -> str:
    """Validate one path segment; raise StoragePathError on anything that
    could move the derived path (separators, dot segments, drives, NUL,
    Windows trailing dot/space quirks)."""
    if not isinstance(value, str) or not value or not value.strip():
        raise StoragePathError(f"{kind} must be a non-empty string")
    if value in (".", ".."):
        raise StoragePathError(f"{kind} must not be a dot segment")
    if "/" in value or "\\" in value or "\x00" in value:
        raise StoragePathError(f"{kind} must not contain path separators or NUL")
    pure = PureWindowsPath(value)
    if pure.is_absolute() or pure.drive or pure.root:
        raise StoragePathError(f"{kind} must not be absolute or carry a drive/root part")
    if value != value.strip() or value.endswith("."):
        raise StoragePathError(f"{kind} must not have trailing spaces/dots (Windows quirk)")
    return value


def ensure_within_root(candidate: os.PathLike | str, root: os.PathLike | str | None = None) -> Path:
    """Realpath containment check: candidate must resolve INSIDE root.

    Catches both lexical attacks (blocked earlier by check_segment) and
    filesystem attacks where an existing ancestor is a junction/symlink
    pointing outside the root. Returns the realpath of the candidate."""
    root_real = os.path.realpath(os.fspath(root) if root is not None else storage_root())
    cand_real = os.path.realpath(os.fspath(candidate))
    cand_norm = os.path.normcase(cand_real)
    root_norm = os.path.normcase(root_real)
    if cand_norm != root_norm and not cand_norm.startswith(root_norm.rstrip(os.sep) + os.sep):
        raise StoragePathError(f"path escapes the storage root: {cand_real}")
    return Path(cand_real)


def user_pool_dir(user_id: str | int, pool: str, *, makedirs: bool = False) -> Path:
    """``<root>/<uid>/<pool>/`` for one of the frozen USER_POOLS."""
    if pool not in USER_POOLS:
        raise StoragePathError(f"unknown pool {pool!r}; expected one of {USER_POOLS}")
    uid = check_segment(str(user_id), "user_id")
    path = storage_root() / uid / pool
    if makedirs:
        path.mkdir(parents=True, exist_ok=True)
    return ensure_within_root(path)


def user_drafts_dir(user_id: str | int, *, makedirs: bool = False) -> Path:
    """``<root>/users/<uid>/styles/drafts/`` (extraction draft layout)."""
    uid = check_segment(str(user_id), "user_id")
    parts = [p.format(user_id=uid) for p in _DRAFTS_PARTS]
    path = storage_root().joinpath(*parts)
    if makedirs:
        path.mkdir(parents=True, exist_ok=True)
    return ensure_within_root(path)


def draft_path(user_id: str | int, filename: str) -> Path:
    """Validated full path for one draft file (caller decides existence)."""
    check_segment(filename, "draft filename")
    return ensure_within_root(user_drafts_dir(user_id) / filename)


def walk_usage(path: os.PathLike | str) -> tuple[int, int]:
    """(bytes, file_count) of a directory tree; missing dir counts (0, 0)."""
    total = 0
    count = 0
    if os.path.isdir(path):
        for dirpath, _dirnames, filenames in os.walk(path):
            for name in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass  # file vanished mid-walk (concurrent cleanup) - skip
                count += 1
    return total, count


def user_usage_bytes(user_id: str | int) -> int:
    """The quota number: six pools + drafts for one user.

    This is the number GET /usage displays AND the gates enforce, so they can
    never disagree (assets_upload module contract, now drafts-aware)."""
    root = storage_root()
    uid = check_segment(str(user_id), "user_id")
    total = 0
    for pool in USER_POOLS:
        total += walk_usage(ensure_within_root(root / uid / pool))[0]
    total += walk_usage(user_drafts_dir(uid))[0]
    return total


def quota_exceeded(used_bytes: int, new_bytes: int, limit: int = QUOTA_LIMIT_BYTES) -> bool:
    """Pure gate arithmetic (1-byte boundary exact: used+new > limit)."""
    return bool(new_bytes) and used_bytes + new_bytes > limit


def enforce_quota(
    user_id: str | int,
    new_bytes: int,
    limit: int = QUOTA_LIMIT_BYTES,
    *,
    used_bytes: int | None = None,
) -> None:
    """Raise 413 QUOTA_EXCEEDED when user_id + new_bytes would pass ``limit``.

    Caller must hold :data:`QUOTA_LOCK` around check->write->commit (same
    convention as assets_upload._store_locked) so concurrent writers
    serialize and the total can never overshoot. ``used_bytes`` lets a caller
    that already walked usage reuse it (one walk per batch)."""
    used = user_usage_bytes(user_id) if used_bytes is None else used_bytes
    if quota_exceeded(used, new_bytes, limit):
        raise ApiError(
            413,
            "QUOTA_EXCEEDED",
            "Quota exceeded",
            f"write of {new_bytes} bytes would exceed the "
            f"{limit} bytes per-user quota (used {used}); free up space first",
        )


def atomic_write_bytes(path: os.PathLike | str, data: bytes, *, fsync: bool = True) -> Path:
    """Write ``data`` atomically: same-dir ``.part-<rand>`` + ``os.replace``.

    On any failure the temp file is removed and the target is untouched, so a
    disk-full or killed write never leaves a half file at the final path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    ensure_within_root(target)
    tmp = target.parent / f".{target.name}.part-{uuid.uuid4().hex[:12]}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            if fsync and data:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def atomic_write_image(
    path: os.PathLike | str,
    image,
    fmt: str = "PNG",
    **save_kwargs,
) -> Path:
    """PIL variant of :func:`atomic_write_bytes` (image.save to tmp + replace).

    The extraction draft writer (extraction/service.py _store_draft) currently
    saves directly to the final path - wiring it through here is the domain
    owner's change, see the D01 wiring sheet."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    ensure_within_root(target)
    tmp = target.parent / f".{target.name}.part-{uuid.uuid4().hex[:12]}"
    try:
        image.save(tmp, format=fmt, **save_kwargs)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def write_under_quota(
    user_id: str | int,
    abs_path: os.PathLike | str,
    writer: Callable[[str], None],
    new_bytes: int,
    limit: int = QUOTA_LIMIT_BYTES,
) -> Path:
    """Quota-gate then atomic-write one file under QUOTA_LOCK.

    ``writer(abs_path)`` receives the TEMP path and must create the file
    (e.g. ``lambda tmp: pil_image.save(tmp, format="PNG")``); the replace is
    ours. Either everything lands or nothing does - the gate, the temp and
    the final path follow the D01 invariants."""
    with QUOTA_LOCK:
        enforce_quota(user_id, new_bytes, limit)
        target = Path(abs_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        ensure_within_root(target)
        tmp = target.parent / f".{target.name}.part-{uuid.uuid4().hex[:12]}"
        try:
            writer(str(tmp))
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return target
