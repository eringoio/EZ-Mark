from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OmrSettings


@dataclass(frozen=True)
class EffectiveOmrSettings:
    preset: str = "standard"
    mark_threshold: float = 0.35
    weak_threshold: float = 0.25
    min_gap_between_top_two: float = 0.12
    dark_pixel_threshold: int = 140
    local_drift_search_mm: float = 1.5
    bubble_outline_gray: float = 0.65
    grid_line_gray: float = 0.78
    choice_label_gray: float = 0.68


PRESETS: dict[str, EffectiveOmrSettings] = {
    "standard": EffectiveOmrSettings(),
    "light_pencil": EffectiveOmrSettings(
        preset="light_pencil",
        mark_threshold=0.30,
        weak_threshold=0.20,
        min_gap_between_top_two=0.10,
        dark_pixel_threshold=155,
    ),
    "strict": EffectiveOmrSettings(
        preset="strict",
        mark_threshold=0.42,
        weak_threshold=0.28,
        min_gap_between_top_two=0.16,
        dark_pixel_threshold=130,
    ),
}


def get_effective_omr_settings(db: Session | None = None) -> EffectiveOmrSettings:
    if db is None:
        return EffectiveOmrSettings()
    row = db.scalar(select(OmrSettings).where(OmrSettings.scope == "global"))
    if row is None:
        return EffectiveOmrSettings()
    return EffectiveOmrSettings(
        preset=row.preset,
        mark_threshold=row.mark_threshold,
        weak_threshold=row.weak_threshold,
        min_gap_between_top_two=row.min_gap_between_top_two,
        dark_pixel_threshold=row.dark_pixel_threshold,
        local_drift_search_mm=row.local_drift_search_mm,
        bubble_outline_gray=row.bubble_outline_gray,
        grid_line_gray=row.grid_line_gray,
        choice_label_gray=row.choice_label_gray,
    )


def save_global_omr_settings(
    db: Session,
    *,
    preset: str,
    mark_threshold: float,
    weak_threshold: float,
    min_gap_between_top_two: float,
    dark_pixel_threshold: int,
    local_drift_search_mm: float,
    bubble_outline_gray: float,
    grid_line_gray: float,
    choice_label_gray: float,
) -> OmrSettings:
    if preset in PRESETS and preset != "custom":
        base = PRESETS[preset]
        mark_threshold = base.mark_threshold
        weak_threshold = base.weak_threshold
        min_gap_between_top_two = base.min_gap_between_top_two
        dark_pixel_threshold = base.dark_pixel_threshold
    row = db.scalar(select(OmrSettings).where(OmrSettings.scope == "global"))
    if row is None:
        row = OmrSettings(scope="global")
        db.add(row)
    row.preset = preset
    row.mark_threshold = _bounded(mark_threshold, 0.01, 0.95)
    row.weak_threshold = _bounded(weak_threshold, 0.0, row.mark_threshold)
    row.min_gap_between_top_two = _bounded(min_gap_between_top_two, 0.0, 0.8)
    row.dark_pixel_threshold = int(_bounded(dark_pixel_threshold, 1, 254))
    row.local_drift_search_mm = _bounded(local_drift_search_mm, 0.0, 4.0)
    row.bubble_outline_gray = _bounded(bubble_outline_gray, 0.25, 0.9)
    row.grid_line_gray = _bounded(grid_line_gray, 0.3, 0.95)
    row.choice_label_gray = _bounded(choice_label_gray, 0.3, 0.9)
    db.commit()
    db.refresh(row)
    return row


def reset_global_omr_settings(db: Session) -> None:
    """Return all OMR settings to their defaults by removing the saved override row.

    With no global row present, ``get_effective_omr_settings`` falls back to the
    built-in ``EffectiveOmrSettings()`` defaults.
    """
    row = db.scalar(select(OmrSettings).where(OmrSettings.scope == "global"))
    if row is not None:
        db.delete(row)
        db.commit()


def _bounded(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, float(value)))
