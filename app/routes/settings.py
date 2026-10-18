from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.omr_settings_service import (
    PRESETS,
    get_effective_omr_settings,
    reset_global_omr_settings,
    save_global_omr_settings,
)
from app.services.templates import templates

router = APIRouter()


@router.get("/settings/omr", response_class=HTMLResponse)
def omr_settings(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    settings = get_effective_omr_settings(db)
    return templates.TemplateResponse(
        request,
        "settings/omr.html",
        {
            "request": request,
            "active_nav": "settings",
            "settings": settings,
            "presets": PRESETS,
        },
    )


@router.post("/settings/omr", response_model=None)
def save_omr_settings(
    preset: str = Form("custom"),
    mark_threshold: float = Form(0.35),
    weak_threshold: float = Form(0.25),
    min_gap_between_top_two: float = Form(0.12),
    dark_pixel_threshold: int = Form(140),
    local_drift_search_mm: float = Form(1.5),
    bubble_outline_gray: float = Form(0.65),
    grid_line_gray: float = Form(0.78),
    choice_label_gray: float = Form(0.68),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    save_global_omr_settings(
        db,
        preset=preset,
        mark_threshold=mark_threshold,
        weak_threshold=weak_threshold,
        min_gap_between_top_two=min_gap_between_top_two,
        dark_pixel_threshold=dark_pixel_threshold,
        local_drift_search_mm=local_drift_search_mm,
        bubble_outline_gray=bubble_outline_gray,
        grid_line_gray=grid_line_gray,
        choice_label_gray=choice_label_gray,
    )
    return RedirectResponse("/settings/omr?saved=1", status_code=303)


@router.post("/settings/omr/reset", response_model=None)
def reset_omr_settings(db: Session = Depends(get_db)) -> RedirectResponse:
    reset_global_omr_settings(db)
    return RedirectResponse("/settings/omr?reset=1", status_code=303)
