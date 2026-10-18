from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.answer_key_service import validate_answer_key
from app.services.exam_service import get_run_or_404
from app.services.sheet_generation_service import (
    generate_blank_sheets,
    generate_exam_sheets,
    get_generated_file,
    latest_exam_download_path,
    latest_exam_zip,
    latest_sheet_generated_at,
)
from app.services.templates import templates

router = APIRouter()


def _peer_review_redirect(exam) -> RedirectResponse | None:
    """Peer-review exams must never use the normal answer-sheet flow.

    Returns a redirect to the peer-review builder when ``exam`` is a peer-review
    assessment, otherwise ``None`` so the normal flow proceeds.
    """
    if exam.assessment_type == "peer_review":
        return RedirectResponse(f"/exams/{exam.id}/peer-review/builder", status_code=303)
    return None


@router.get("/exams/{exam_id}/sheets", response_class=HTMLResponse, response_model=None)
def sheets_page(request: Request, exam_id: int, db: Session = Depends(get_db)) -> Response:
    exam = get_run_or_404(db, exam_id)
    redirect = _peer_review_redirect(exam)
    if redirect is not None:
        return redirect
    included_student_count = len([link for link in exam.students if link.included])
    active_sheet_count = len(
        [
            sheet
            for sheet in exam.physical_sheets
            if any(question.physical_sheet_id == sheet.id for question in exam.questions)
        ]
    )
    return templates.TemplateResponse(
        request,
        "sheets/index.html",
        {
            "request": request,
            "active_nav": "sessions",
            "exam": exam,
            "download_path": latest_exam_download_path(db, exam),
            "zip_path": latest_exam_zip(db, exam),
            "last_generated_at": latest_sheet_generated_at(db, exam),
            "included_student_count": included_student_count,
            "active_sheet_count": active_sheet_count,
            "errors": [],
        },
    )


@router.post(
    "/exams/{exam_id}/sheets/generate",
    response_class=HTMLResponse,
    response_model=None,
)
def generate_sheets(
    request: Request,
    exam_id: int,
    db: Session = Depends(get_db),
) -> Response:
    exam = get_run_or_404(db, exam_id, for_write=True)
    redirect = _peer_review_redirect(exam)
    if redirect is not None:
        return redirect
    readiness_issues = validate_answer_key(exam)
    if readiness_issues:
        return _sheets_error_response(
            request,
            db,
            exam,
            [
                "This template is not ready yet. Please complete the answer key in the template builder first."
            ],
            status_code=400,
        )
    try:
        summary = generate_exam_sheets(db, exam)
    except ValueError as exc:
        return _sheets_error_response(request, db, exam, [str(exc)], status_code=400)
    return _download_response(summary.download_path)


@router.post(
    "/exams/{exam_id}/sheets/blank",
    response_class=HTMLResponse,
    response_model=None,
)
def download_blank_sheets(
    request: Request,
    exam_id: int,
    db: Session = Depends(get_db),
) -> Response:
    exam = get_run_or_404(db, exam_id, for_write=True)
    redirect = _peer_review_redirect(exam)
    if redirect is not None:
        return redirect
    try:
        download_path = generate_blank_sheets(db, exam)
    except ValueError as exc:
        return _sheets_error_response(request, db, exam, [str(exc)], status_code=400)
    return _download_response(download_path)


@router.get("/exams/{exam_id}/sheets/download/latest", response_model=None)
@router.get("/exams/{exam_id}/sheets/download/all.zip", response_model=None)
def download_all_sheets(exam_id: int, db: Session = Depends(get_db)) -> Response:
    exam = get_run_or_404(db, exam_id)
    redirect = _peer_review_redirect(exam)
    if redirect is not None:
        return redirect
    download_path = latest_exam_download_path(db, exam)
    if download_path is None:
        raise HTTPException(status_code=404)
    return _download_response(download_path)


def _download_response(relative_path: str) -> FileResponse:
    try:
        path = get_generated_file(relative_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    media_type = "application/zip" if path.suffix.lower() == ".zip" else "application/pdf"
    return FileResponse(path, media_type=media_type, filename=path.name)


def _sheets_error_response(
    request: Request,
    db: Session,
    exam,
    errors: list[str],
    status_code: int,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "sheets/index.html",
        {
            "request": request,
            "active_nav": "sessions",
            "exam": exam,
            "download_path": latest_exam_download_path(db, exam),
            "zip_path": latest_exam_zip(db, exam),
            "last_generated_at": latest_sheet_generated_at(db, exam),
            "included_student_count": len([link for link in exam.students if link.included]),
            "active_sheet_count": len(
                [
                    sheet
                    for sheet in exam.physical_sheets
                    if any(question.physical_sheet_id == sheet.id for question in exam.questions)
                ]
            ),
            "errors": errors,
        },
        status_code=status_code,
    )
