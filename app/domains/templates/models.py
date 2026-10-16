"""Template domain - persistence models (contract v0.2 section 4 lines 201-202).

LayoutTemplate physically lives here since B2-03 (D-T12-1): the model moved
out of app.domains.render.models, which now re-imports it from this module
(dependency direction render -> templates). The table name, column names and
constraints are unchanged, so existing sqlite databases need no migration.
All timestamps ISO8601 UTC; all paths are DATA_ROOT-relative.
"""

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class LayoutTemplate(Base):
    __tablename__ = "layout_templates"

    template_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # draft|pending_review|approved|rejected
    slots: Mapped[str] = mapped_column(Text, nullable=False)  # JSON [{key, text_template, region{x_norm,y_norm,w_norm,h_norm}}]
    base_asset_id: Mapped[str] = mapped_column(Text, nullable=False)
    style_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    canvas_width: Mapped[int] = mapped_column(Integer, nullable=False)
    canvas_height: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
