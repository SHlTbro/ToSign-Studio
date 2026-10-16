"""CC-14 blogger name preset table (contract: contract-v0.3-patch-C section 7)."""

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class BloggerNamePreset(Base):
    __tablename__ = "blogger_name_presets"

    preset_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
