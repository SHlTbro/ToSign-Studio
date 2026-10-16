"""Style domain - persistence models (contract v0.2 section 1.4/1.8 lines 55-80,
146-156; data model lines 204-205; patch-B CC-2/CC-3; patch-C section 9 CC-16).

Schema frozen by dispatch T69 section 2. Two column names are a cross-task
interface with the T64 training_sets delete lock chain, which reads them via
raw SQL: ``style_samples.source_training_image_id`` and
``style_runs.sample_ids`` (JSON integer array). They must not drift.

No real training/inference state lives here (axiom red line, dispatch T69
section 0.3): ``style_runs`` rows are bookkeeping; engine-side transitions
are carried by service functions only. Timestamps are ISO8601 UTC.
"""

from sqlalchemy import Float, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class StyleProfile(Base):
    __tablename__ = "style_profiles"

    style_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # draft|pending_review|approved|rejected
    quality_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    receipt_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # patch-C section 4 (CC-11): optional preset font override; null = system font stack.
    font_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class StyleSample(Base):
    __tablename__ = "style_samples"

    sample_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    style_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Lock-chain interface column (dispatch T64 section 2): points at
    # training_images.image_id; T64 delete refuses while a queued/running
    # style run pins this sample.
    source_training_image_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_at: Mapped[str] = mapped_column(Text, nullable=False)


class StyleRun(Base):
    __tablename__ = "style_runs"

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # created|queued|running|succeeded|failed|cancelled
    # Lock-chain interface column (dispatch T64 section 2): JSON integer array
    # of style_samples.sample_id, locked at creation (patch-B CC-2 line 24).
    sample_ids: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[str] = mapped_column(Text, nullable=False)  # JSON object
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class StyleRunBlindReview(Base):
    __tablename__ = "style_run_blind_reviews"

    review_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)  # pass|fail
    items: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class StyleRunArtifact(Base):
    """S10 artifact registry (B-DATA-STYLES 2026-09-27, additive new table).

    Answers migration requirement M-2 (b-platform/migration-requirements-
    2026-09-27 section 3): publish binding lives in a NEW table, additive/
    create_all compatible; no column is added to the existing style_runs /
    style_profiles tables. One row per published run (publish is idempotent on
    run_id); ``active`` is the rollback pointer (rollback flips it, never
    deletes files).

    Hash chain (every field must recompute-verify before publish accepts):
    - ``sha256``                = sha256 of the artifact manifest file itself
    - ``metrics_receipt_sha256``= sha256 of the engine receipt JSON
    - ``dataset_snapshot_sha256``= sha256 over the canonical locked sample set
    - ``config_sha256``         = sha256 over the canonical run config JSON
    - ``base_ckpt_sha256``      = engine base checkpoint hash set (JSON) or the
      dummy-engine test marker (never a fabricated real hash)

    Data discipline (B10 red line, mirrored from the worker): B10 PRIMARY/
    SEALED material must never enter any training path; artifacts derived from
    such data must not be registered here. quality_status records the Owner
    quality gate state at publish time ("BLOCKED_OWNER" = engineering chain
    verified, real blind evaluation S08/S09 has NOT happened).
    """

    __tablename__ = "style_run_artifacts"

    artifact_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    engine_mode: Mapped[str] = mapped_column(Text, nullable=False)
    engine_id: Mapped[str] = mapped_column(Text, nullable=False)
    engine_version: Mapped[str] = mapped_column(Text, nullable=False)
    base_ckpt_sha256: Mapped[str] = mapped_column(Text, nullable=False)  # JSON object or marker
    dataset_snapshot_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    config_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    metrics_receipt_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)  # manifest inside run dir
    sha256: Mapped[str] = mapped_column(Text, nullable=False)  # manifest file hash
    quality_status: Mapped[str] = mapped_column(Text, nullable=False, default="BLOCKED_OWNER")
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class StyleRunCreateKey(Base):
    """Additive per-user idempotency keys; no existing run schema changes."""
    __tablename__ = "style_run_create_keys"
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)


class StyleRunCheckpoint(Base):
    """Immutable server anchors; never accept checkpoint paths from the client."""
    __tablename__ = "style_run_checkpoints"
    checkpoint_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    manifest_sha256: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    relative_directory: Mapped[str] = mapped_column(Text, nullable=False)
    binding: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class StyleRunExecution(Base):
    """Owned process birth identities, additive and compatible with create_all."""
    __tablename__ = "style_run_executions"
    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ownership: Mapped[str] = mapped_column(Text, nullable=False)


class StyleRunContinuation(Base):
    """One attempt per parent. Further retries continue from that new attempt."""
    __tablename__ = "style_run_continuations"
    parent_run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    request_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    child_run_id: Mapped[int] = mapped_column(Integer, nullable=False)
