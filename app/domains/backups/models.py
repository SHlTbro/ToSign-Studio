"""Backups domain - persistence model (contract v0.2 section 1.7 line 134)."""

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Backup(Base):
    __tablename__ = "backups"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    includes: Mapped[str] = mapped_column(Text, nullable=False)  # JSON list
    checksum: Mapped[str] = mapped_column(Text, nullable=False)  # manifest checksum
    status: Mapped[str] = mapped_column(Text, nullable=False)  # queued|running|succeeded|partial|failed|cancelled
    path_relative: Mapped[str] = mapped_column(Text, nullable=False)  # DATA_ROOT-relative (section 1.7 line 137)
