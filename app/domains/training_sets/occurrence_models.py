"""Additive occurrence selections; never change candidate or review rows."""
from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base


class OccurrenceSelection(Base):
    __tablename__ = "personal_occurrence_selections"
    plan_sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    style_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    receipt_json: Mapped[str] = mapped_column(Text, nullable=False)


class CurrentOccurrenceSelection(Base):
    __tablename__ = "personal_current_occurrence_selections"
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    style_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_sha256: Mapped[str] = mapped_column(Text, nullable=False)
