"""Usage query domain (patch-C section 11.7).

Storage layout is ``<YY_STORAGE_ROOT>/<user_id>/<pool>/`` with the six frozen
pools below. Quota accounting and this read-only breakdown share the same
directory walk so the number a user sees is the number the uploader enforces.

D01 (fb-platform 2026-09-27): the six-pool API contract is unchanged, but the
extraction draft pool (``<root>/users/<uid>/styles/drafts``, legacy layout
written by extraction/service.py _store_draft) used to be invisible to both
the breakdown and the quota gate - drafts could grow without bound while
GET /usage reported no change. Drafts are now:
- walked and reported as the top-level ``drafts`` object of GET /usage, and
- included in ``total_bytes`` and in :func:`total_usage_bytes` (the number
  the assets/training quota gates enforce).
The ``pools`` array stays exactly the frozen six entries; the walk primitives
live in ``app.core.storage`` (D01 unified storage module) so the quota gate
and this display can never disagree.
"""
from app.core import storage

# Frozen six-pool API contract (patch-C section 11.7). Consumers (t71 probe,
# P9_UserCenter) assert this exact set - do not add entries here; drafts are
# reported separately via get_usage()["drafts"].
POOLS = storage.USER_POOLS
LIMIT_BYTES = storage.QUOTA_LIMIT_BYTES


def storage_root() -> str:
    """Legacy string-typed root accessor (extraction/assets_upload call
    ``os.path.join(storage_root(), ...)``). Delegates to app.core.storage."""
    return str(storage.storage_root())


def pool_usages(user_id: str) -> list:
    """Return [{pool, bytes, file_count}] for the six frozen pools; missing
    dir counts 0."""
    root = storage.storage_root()
    result = []
    for pool in POOLS:
        total, count = storage.walk_usage(root / str(user_id) / pool)
        result.append({"pool": pool, "bytes": total, "file_count": count})
    return result


def drafts_usage(user_id: str) -> dict:
    """D01: the extraction-draft pool (``users/<uid>/styles/drafts``)."""
    total, count = storage.walk_usage(storage.user_drafts_dir(user_id))
    return {"pool": "drafts", "bytes": total, "file_count": count}


def total_usage_bytes(user_id: str) -> int:
    """The enforced quota number: six pools + drafts (D01)."""
    return storage.user_usage_bytes(user_id)


def get_usage(user_id: str) -> dict:
    pools = pool_usages(user_id)
    drafts = drafts_usage(user_id)
    return {
        "total_bytes": sum(p["bytes"] for p in pools) + drafts["bytes"],
        "limit_bytes": LIMIT_BYTES,
        "pools": pools,
        "drafts": {"bytes": drafts["bytes"], "file_count": drafts["file_count"]},
    }
