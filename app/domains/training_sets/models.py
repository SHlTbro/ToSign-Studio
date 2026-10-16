"""Training-image set domain - persistence model (patch-C section 3, CC-10).

Fully separate pool from P2 ``assets``: own table, own endpoints, and no
"cleanliness" review - training images are expected to contain text. Stroke
analysis is not part of this domain; the blogger triggers it manually in P4
(axioms A2/A3, patch-C section 3).

Schema frozen by dispatch T64 section 2. ``uploaded_at`` is ISO8601 UTC.

TrainingSample (T2, batch B4-01) - character-level sample table per
t44-r1-extraction-spec-v2 §3 (v3 FROZEN) and
``contracts/style-font-pipeline-v1.json`` →
``data_contracts.sample_manifest_row``. C-57 (FROZEN): this schema carries
NO train/val/test partition field of any kind; the full policy clause -
stratification by ``source_asset_id`` inside the training script, one
source image never crossing collections - lives in the ``service`` module
docstring of this package.
"""

from sqlalchemy import Boolean, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class TrainingImage(Base):
    __tablename__ = "training_images"

    image_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_at: Mapped[str] = mapped_column(Text, nullable=False)


class TrainingSample(Base):
    """Character-level glyph sample (t44-r1-extraction-spec-v2 §3, v3).

    Pure new table - no existing table is touched (B4-01 backward
    compatibility rule). ``text_region_bbox_norm`` is a JSON object
    ``{"x_norm": float, "y_norm": float, "w_norm": float, "h_norm": float}``
    stored as a JSON string (repo convention: Text + JSON, styles/models.py).
    ``draft_alpha_path`` / ``confirmed_alpha_path`` are optional C-58
    traceability fields; missing values do not block registration.
    Timestamps are ISO8601 UTC. No train/val/test partition column exists
    (C-57): see the module docstring and the ``service`` module docstring.

    ``user_id`` (M-1, D06/b2-extraction-data 2026-09-27, ACCEPT of external
    audit 2.2): persistent owner of the sample. ADDITIVE column, type
    matching the sibling ``TrainingImage.user_id`` (Integer). Set by
    ``service.register_sample`` from the authenticated caller; never
    client-supplied. It is a persistence-internal column: the frozen
    ``sample_manifest_row`` API shape (``sample_dict``) is unchanged. The
    legacy sentinel ``0`` means "registered before owner persistence"
    (``users.id`` is AUTOINCREMENT starting at 1, so 0 never matches a real
    caller and legacy rows are invisible to per-user queries rather than
    misattributed). Databases that created the pre-M-1 table are evolved by
    the central migration ``app/core/migration.py`` step 1 (ALTER + backfill
    from the frozen glyph_path layout); fresh databases (including the real
    dev-demo DB, which does not have this table yet as of 2026-09-27) get
    the complete model from create_all directly.

    P4/S02 provenance (M-2): a P4-backed row is the canonical character-level
    candidate. It links to its product ``StyleSample`` and edited
    ``TrainingImage`` while retaining the original asset hash, box, extraction
    draft hash and final alpha hash. New rows always begin as ``candidate``;
    only the authenticated batch-review endpoint can set ``owner_confirmed``.
    """

    __tablename__ = "training_samples"

    sample_id: Mapped[str] = mapped_column(Text, primary_key=True)  # ss_XXXXXX, unique by PK
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)  # owner; 0 = legacy sentinel
    style_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    source_asset_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    glyph_path: Mapped[str] = mapped_column(Text, nullable=False)
    text_label: Mapped[str] = mapped_column(Text, nullable=False)
    char_unicode: Mapped[str] = mapped_column(Text, nullable=False)  # U+XXXX
    text_region_bbox_norm: Mapped[str] = mapped_column(Text, nullable=False)  # JSON object
    output_size: Mapped[int] = mapped_column(Integer, nullable=False)  # 128
    extracted_by: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. touchup
    touchup_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    draft_alpha_path: Mapped[str | None] = mapped_column(Text, nullable=True)  # C-58 optional
    confirmed_alpha_path: Mapped[str | None] = mapped_column(Text, nullable=True)  # C-58 optional
    created_at: Mapped[str] = mapped_column(Text, nullable=False)  # ISO8601 UTC
    source_training_image_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    style_sample_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    source_asset_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_asset_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_asset_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_family_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_training_image_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_alpha_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_alpha_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    touchup_edit_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    touchup_confirmed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(Text, nullable=False, default="candidate", server_default="candidate", index=True)
    owner_confirmed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_confirmed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance_version: Mapped[str | None] = mapped_column(Text, nullable=True)

# Register additive snapshot authority tables before create_all.
from . import intake_models as _intake_models  # noqa: F401,E402
from . import occurrence_models as _occurrence_models  # noqa: F401,E402
