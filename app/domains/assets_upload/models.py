"""Assets upload domain - persistence model.

Table ``assets`` rows are written only after every upload gate passes and the
file bytes are durably stored (temp file + rename), so a DB row always points
at an existing on-disk file.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from app.core.db import Base


class Asset(Base):
    __tablename__ = "assets"

    asset_id = Column(String(36), primary_key=True)
    user_id = Column(String(64), nullable=False, index=True)
    relative_path = Column(String(512), nullable=False)
    sha256 = Column(String(64), nullable=False, index=True)
    width = Column(Integer, nullable=False)
    height = Column(Integer, nullable=False)
    bytes = Column(Integer, nullable=False)
    uploaded_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    # v0.2 section 1.1/section 4 additions (T58 Phase 2 asset lifecycle):
    kind = Column(String(32), nullable=False, default='cos', server_default='cos')
    status = Column(String(32), nullable=False, default='pending', server_default='pending', index=True)
    approved_at = Column(String(64), nullable=True)
    approved_by = Column(String(64), nullable=True)
    thumbnail_path = Column(String(512), nullable=True)
    enabled = Column(Integer, nullable=False, default=1, server_default='1')
