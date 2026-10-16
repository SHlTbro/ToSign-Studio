"""Render domain - persistence models (contract v0.2 section 4 lines 197-212,
section 1.6 lines 87-122, state machine section 5, snapshots section 6).

Since B2-03 (D-T12-1) LayoutTemplate physically lives in
app.domains.templates.models; it is re-imported here so the dependency
direction is render -> templates and the registration order of
app.main imports keeps layout_tables create_all idempotent. All timestamps
ISO8601 UTC; all paths are DATA_ROOT-relative.
"""

from sqlalchemy import Boolean, Float, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.domains.templates.models import LayoutTemplate  # noqa: F401  (re-export; single definition source)


class RenderJob(Base):
    __tablename__ = "render_jobs"

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, index=True)  # queued|running|succeeded|partial|failed|cancelled
    draft: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True, index=True)
    retry_of_job_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ttl_expires_at: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)  # draft only
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Snapshots (section 6): history replays from these, never live references.
    template_snapshot: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    style_snapshot: Mapped[str] = mapped_column(Text, nullable=False)  # JSON (incl. font_url)
    base_asset_id: Mapped[str] = mapped_column(Text, nullable=False)
    base_checksum: Mapped[str] = mapped_column(Text, nullable=False)
    strict_glyph: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sign_blogger: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blogger_name: Mapped[str | None] = mapped_column(Text, nullable=True)  # snapshot (CC-12)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class RenderItem(Base):
    __tablename__ = "render_items"

    item_id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, index=True)  # pending|running|completed|completed_with_warning|failed|cancelled
    cn: Mapped[str] = mapped_column(Text, nullable=False)
    blessing: Mapped[str | None] = mapped_column(Text, nullable=True)
    region_override: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON {slot_key: region}
    template_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # per-item override ref
    style_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    render_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    output_relative_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fallback_map: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON {char: fallback_font}
    warnings: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reviewed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_batch_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class ExportArtifact(Base):
    __tablename__ = "export_artifacts"

    artifact_id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
