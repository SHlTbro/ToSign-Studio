"""Central schema migration entry (D06, b2-extraction-data 2026-09-27).

Single versioned migration mechanism for the real database. Domain writers
submit steps here; they must never ALTER the shared database on their own
(01-platform card D06 write-face clause, migration-requirements §3 M-5).

Properties (D06 DoD):

- **Version table**: ``_schema_versions(version INTEGER PRIMARY KEY,
  description, applied_at, applied_by)`` - one row per applied step, written
  in the SAME transaction as the step itself, so a step and its version
  mark commit or roll back together.
- **Ordered + skip-applied**: steps run strictly by ascending ``version``;
  applied versions are skipped (re-running is a no-op - idempotent by
  construction, not by convention).
- **Per-step transaction, no half-commit**: each step runs inside one
  explicit ``BEGIN IMMEDIATE ... COMMIT`` transaction on a dedicated raw
  sqlite connection (``isolation_level=None`` so DDL is transactional too).
  A failure - including an injected one simulating a mid-step kill - rolls
  the step back completely (ALTER/CREATE included) and leaves no version
  row; the next run replays exactly the missing steps. The same
  ``BEGIN IMMEDIATE`` doubles as the cross-process write lock (a second
  migrating process blocks, then re-checks the ledger and skips).
- **Additive-first, no drop/recreate**: steps may create tables/indexes or
  ADD COLUMN with defaults; dropping or recreating user tables is
  forbidden (D06 red line). Rollback story is therefore forward-only:
  restore the pre-migration backup taken before the first pending step.
- **Pre-migration backup**: for file-backed sqlite databases, the db file
  (plus -wal/-shm if present) is copied to
  ``<db_dir>/pre-migration-backups/<name>.<utcstamp>.bak`` once before the
  first pending step that requires one. A failed backup aborts the run
  before anything is written.
- **Concurrency**: a process-local lock serializes in-process callers; the
  version re-check inside each step's write transaction plus sqlite's
  single-writer locking make a racing second process either skip (step
  already committed) or fail its own transaction cleanly and converge on
  the next run. It does NOT rely on wall-clock scheduling.
- **Dry run**: ``YY_MIGRATION_DRYRUN=1`` (or ``dry_run=True``) reports the
  pending steps without writing anything.

Startup wiring: since DF-P0-01 (b-migration-api 2026-09-29), ``create_app``
calls ``ensure_schema(get_engine())`` BEFORE ``Base.metadata.create_all`` -
the single choke point every launch path (repo uvicorn, installer/package
launchers, tests) goes through. ``scripts/migrate.py status / run`` remains
the operator entry point. Both orders ("create_all then migrate" /
"migrate then create_all") converge because step 1 is a no-op when the
table does not exist yet and create_all only builds missing tables; the
wired order (migrate first) additionally guarantees the ORM never touches a
pre-v2 legacy table.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

VERSION_TABLE = "_schema_versions"

# Process-local lock: in-process callers (scripts, tests, future startup
# hook) never race each other; cross-process safety comes from the sqlite
# single-writer lock plus the in-transaction version re-check.
_MIGRATION_LOCK = threading.Lock()


@dataclass(frozen=True)
class Migration:
    """One ordered schema step. ``apply(conn)`` receives a
    :class:`MigrationConn` (explicit transaction already open, DDL
    transactional) and must be idempotent enough to run inside that
    transaction and return a short human-readable outcome note; it must
    never drop or recreate user tables."""

    version: int
    description: str
    apply: Callable[[Any], str]
    requires_backup: bool = True


def _table_exists(conn: Connection, name: str) -> bool:
    row = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _index_names(conn: Connection, table: str) -> set[str]:
    rows = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
    ).fetchall()
    return {row[0] for row in rows}


def _table_columns(conn: Connection, table: str) -> set[str]:
    # PRAGMA cannot be parameterized; ``table`` is an internal constant, not
    # user input.
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _applied_versions(conn: Connection) -> set[int]:
    if not _table_exists(conn, VERSION_TABLE):
        return set()
    rows = conn.execute(text(f"SELECT version FROM {VERSION_TABLE}")).fetchall()
    return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _m1_training_samples_user_id(conn) -> str:
    """M-1 (P09/E10 closure, external audit 2.2): TrainingSample owner column.

    - table absent (the real dev-demo DB state as of 2026-09-27): no-op -
      the next create_all builds the complete model including ``user_id``.
    - table present without the column (a database created by pre-M-1
      code): ADD COLUMN ``user_id INTEGER NOT NULL DEFAULT 0``. The
      sentinel ``0`` = "legacy, unattributed" (users.id AUTOINCREMENT
      starts at 1, so 0 can never match a real caller). Rows whose
      ``glyph_path`` matches the frozen caller-owned layout
      ``users/{uid}/styles/{style_id}/glyphs/{sample_id}.png`` are backfilled
      with the parsed owner; everything else stays 0.
    - creates ``ix_training_samples_user_id`` if missing.
    """
    if not _table_exists(conn, "training_samples"):
        return "training_samples absent; nothing to migrate (create_all builds the complete model)"
    notes: list[str] = []
    if "user_id" not in _table_columns(conn, "training_samples"):
        conn.exec_driver_sql(
            "ALTER TABLE training_samples ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0"
        )
        # Backfill attributable legacy rows: substr(glyph_path, 7) skips
        # "users/"; the uid segment is everything before the next "/". A
        # non-numeric segment CASTs to 0 and stays the legacy sentinel.
        conn.exec_driver_sql(
            "UPDATE training_samples SET user_id = CAST("
            "substr(substr(glyph_path, 7), 1, instr(substr(glyph_path, 7), '/') - 1)"
            " AS INTEGER) "
            "WHERE user_id = 0 AND glyph_path LIKE 'users/%/styles/%/glyphs/%.png'"
        )
        notes.append("user_id column added (legacy sentinel 0) + attributable rows backfilled from glyph_path")
    else:
        notes.append("user_id column already present")
    if "ix_training_samples_user_id" not in _index_names(conn, "training_samples"):
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_training_samples_user_id "
            "ON training_samples (user_id)"
        )
        notes.append("index ix_training_samples_user_id created")
    return "; ".join(notes)


def _m2_training_sample_provenance(conn) -> str:
    """M-2 (personal-style data flywheel): additive provenance and review fields.

    Existing rows receive ``review_status='candidate'``. This migration never
    infers Owner approval from the legacy ``touchup_confirmed`` flag.
    """
    if not _table_exists(conn, "training_samples"):
        return "training_samples absent; create_all will build the complete M-2 model"

    additions = (
        ("source_training_image_id", "INTEGER NULL"),
        ("style_sample_id", "INTEGER NULL"),
        ("source_asset_sha256", "TEXT NULL"),
        ("source_asset_width", "INTEGER NULL"),
        ("source_asset_height", "INTEGER NULL"),
        ("source_family_id", "TEXT NULL"),
        ("source_training_image_sha256", "TEXT NULL"),
        ("draft_alpha_sha256", "TEXT NULL"),
        ("final_alpha_sha256", "TEXT NULL"),
        ("touchup_edit_count", "INTEGER NULL"),
        ("touchup_confirmed_at", "TEXT NULL"),
        ("review_status", "TEXT NOT NULL DEFAULT 'candidate'"),
        ("owner_confirmed_by", "TEXT NULL"),
        ("owner_confirmed_at", "TEXT NULL"),
        ("reviewed_by", "TEXT NULL"),
        ("reviewed_at", "TEXT NULL"),
        ("review_reason", "TEXT NULL"),
        ("provenance_version", "TEXT NULL"),
    )
    columns = _table_columns(conn, "training_samples")
    added = []
    for name, declaration in additions:
        if name not in columns:
            conn.exec_driver_sql(
                f"ALTER TABLE training_samples ADD COLUMN {name} {declaration}"
            )
            added.append(name)
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_training_samples_source_training_image_id "
        "ON training_samples (source_training_image_id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_training_samples_review_status "
        "ON training_samples (review_status)"
    )
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_training_samples_style_sample_id "
        "ON training_samples (style_sample_id)"
    )
    return "added=" + (",".join(added) if added else "none") + "; legacy rows remain candidate/unattributed"


MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        description="M-1 training_samples.user_id ownership column (P09/E10, additive)",
        apply=_m1_training_samples_user_id,
        requires_backup=True,
    ),
    Migration(
        version=2,
        description="M-2 TrainingSample P4 provenance and explicit Owner review (additive)",
        apply=_m2_training_sample_provenance,
        requires_backup=True,
    ),
]


# ---------------------------------------------------------------------------
# Mechanism
# ---------------------------------------------------------------------------

# sqlite3 connect timeout for BEGIN IMMEDIATE lock acquisition (matches
# app/core/db.py's engine timeout).
_WRITE_LOCK_TIMEOUT_S = 30


class MigrationConn:
    """Connection facade handed to :attr:`Migration.apply`.

    Wraps a raw sqlite3 connection running in explicit-transaction mode
    (``isolation_level=None``), exposing SQLAlchemy-style
    ``exec_driver_sql``. Every statement the step makes - DDL included -
    joins the caller's ``BEGIN IMMEDIATE`` transaction, and SQLite has
    transactional DDL, so a mid-step failure (injected or a real crash
    before COMMIT) rolls back the ALTER/CREATE too: zero half-commit.
    """

    __slots__ = ("_raw",)

    def __init__(self, raw):
        self._raw = raw

    def exec_driver_sql(self, statement: str, parameters: tuple = ()):
        return self._raw.execute(statement, parameters)


@contextmanager
def _write_connection(engine: Engine):
    """A dedicated raw sqlite3 connection in explicit-transaction mode.

    Deliberately NOT pooled through the engine: mutating
    ``isolation_level`` on a pooled pysqlite connection would silently
    change the commit semantics of every other domain. File-backed sqlite
    is required (the CLI/app database always is)."""
    database = engine.url.database
    if not database or database == ":memory:":
        raise RuntimeError(
            "central migration requires a file-backed sqlite database "
            f"(got engine url {str(engine.url)!r})"
        )
    raw = sqlite3.connect(database, timeout=_WRITE_LOCK_TIMEOUT_S)
    try:
        raw.isolation_level = None  # explicit BEGIN/COMMIT (transactional DDL)
        yield raw
    finally:
        try:
            raw.rollback()  # no-op when no transaction is open
        except sqlite3.Error:
            pass
        raw.close()


def _applied_versions_raw(raw) -> set[int]:
    cur = raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (VERSION_TABLE,)
    )
    if cur.fetchone() is None:
        return set()
    return {row[0] for row in raw.execute(f"SELECT version FROM {VERSION_TABLE}")}


def backup_database(engine: Engine) -> str | None:
    """File-level backup of a file-backed sqlite DB (+ wal/shm sidecars).

    Returns the backup path, or None for non-file URLs (memory / non-sqlite).
    Raises on failure - callers must treat that as "do not migrate"."""
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return None
    raw = url.split("sqlite:///", 1)[1] if "sqlite:///" in url else ""
    if not raw or raw == ":memory:":
        return None
    db_path = Path(raw)
    if not db_path.is_file():
        return None
    backup_dir = db_path.parent / "pre-migration-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"{db_path.name}.{stamp}.bak"
    shutil.copy2(db_path, target)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.is_file():
            shutil.copy2(sidecar, str(target) + suffix)
    return str(target)


def _version_table_sql() -> str:
    return (
        f"CREATE TABLE IF NOT EXISTS {VERSION_TABLE} ("
        "version INTEGER PRIMARY KEY, "
        "description TEXT NOT NULL, "
        "applied_at TEXT NOT NULL, "
        "applied_by TEXT NOT NULL)"
    )


def status(engine: Engine) -> list[dict]:
    """Per-step status: version, description, applied flag and audit stamps."""
    with engine.connect() as conn:
        meta: dict[int, dict] = {}
        if _table_exists(conn, VERSION_TABLE):
            for version, desc, at, by in conn.execute(
                text(
                    f"SELECT version, description, applied_at, applied_by "
                    f"FROM {VERSION_TABLE} ORDER BY version"
                )
            ):
                meta[version] = {
                    "applied_at": at,
                    "applied_by": by,
                    "recorded_description": desc,
                }
        return [
            {
                "version": m.version,
                "description": m.description,
                "applied": m.version in meta,
                "applied_at": meta.get(m.version, {}).get("applied_at"),
                "applied_by": meta.get(m.version, {}).get("applied_by"),
            }
            for m in MIGRATIONS
        ]


def pending_migrations(engine: Engine) -> list[Migration]:
    """Steps not yet applied, in execution order (read-only)."""
    with _MIGRATION_LOCK:
        with engine.connect() as conn:
            done = _applied_versions(conn)
        return [m for m in MIGRATIONS if m.version not in done]


def run_migrations(
    engine: Engine,
    *,
    applied_by: str = "scripts/migrate.py",
    dry_run: bool | None = None,
    backup: bool = True,
) -> list[dict]:
    """Apply every pending step in order; returns one record per step.

    ``dry_run=None`` defers to ``YY_MIGRATION_DRYRUN=1``. Each step runs in
    its own ``BEGIN IMMEDIATE`` transaction together with its version row
    (transactional DDL included); any exception rolls the step back fully
    and aborts the remaining ones - re-run replays exactly the missing
    steps. ``BEGIN IMMEDIATE`` doubles as the cross-process write lock: a
    second migrating process blocks up to 30s, then re-checks the ledger
    inside its own transaction and skips what the first already committed."""
    if dry_run is None:
        dry_run = os.environ.get("YY_MIGRATION_DRYRUN") == "1"
    with _MIGRATION_LOCK:
        with engine.connect() as conn:
            done = _applied_versions(conn)
        todo = [m for m in MIGRATIONS if m.version not in done]
        if dry_run:
            return [
                {
                    "version": m.version,
                    "description": m.description,
                    "note": "dry-run: pending, not applied",
                    "dry_run": True,
                }
                for m in todo
            ]
        if not todo:
            return []
        backup_path: str | None = None
        if backup and any(m.requires_backup for m in todo):
            backup_path = backup_database(engine)  # raises -> nothing written
        applied: list[dict] = []
        with _write_connection(engine) as raw:
            raw.execute("BEGIN IMMEDIATE")
            raw.execute(_version_table_sql())
            raw.execute("COMMIT")
            for m in todo:
                raw.execute("BEGIN IMMEDIATE")
                try:
                    # Re-check under the write lock: a concurrent migrator may
                    # have committed this step after our listing above.
                    if m.version in _applied_versions_raw(raw):
                        raw.execute("ROLLBACK")
                        continue
                    note = m.apply(MigrationConn(raw))
                    raw.execute(
                        f"INSERT INTO {VERSION_TABLE} "
                        "(version, description, applied_at, applied_by) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            m.version,
                            m.description,
                            datetime.now(timezone.utc).isoformat(),
                            applied_by,
                        ),
                    )
                    raw.execute("COMMIT")
                except BaseException:
                    raw.execute("ROLLBACK")
                    raise
                applied.append(
                    {
                        "version": m.version,
                        "description": m.description,
                        "note": note,
                        "backup": backup_path,
                        "dry_run": False,
                    }
                )
        return applied


def ensure_schema(engine: Engine, **kwargs) -> list[dict]:
    """Startup-facing alias of :func:`run_migrations` (wired into
    ``create_app`` before ``create_all`` since DF-P0-01, 2026-09-29)."""
    kwargs.setdefault("applied_by", "app-startup")
    return run_migrations(engine, **kwargs)
