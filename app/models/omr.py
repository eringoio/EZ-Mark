from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OmrSettings(Base):
    __tablename__ = "omr_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String, default="global", unique=True, index=True)
    preset: Mapped[str] = mapped_column(String, default="standard")
    mark_threshold: Mapped[float] = mapped_column(Float, default=0.35)
    weak_threshold: Mapped[float] = mapped_column(Float, default=0.25)
    min_gap_between_top_two: Mapped[float] = mapped_column(Float, default=0.12)
    dark_pixel_threshold: Mapped[int] = mapped_column(Integer, default=140)
    local_drift_search_mm: Mapped[float] = mapped_column(Float, default=1.5)
    bubble_outline_gray: Mapped[float] = mapped_column(Float, default=0.65)
    grid_line_gray: Mapped[float] = mapped_column(Float, default=0.78)
    choice_label_gray: Mapped[float] = mapped_column(Float, default=0.68)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
