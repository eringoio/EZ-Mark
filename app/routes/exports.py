from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.exam_service import get_run_or_404
from app.services.export_service import (
    export_path,
    generate_per_question_export,
    generate_question_analysis_export,
    generate_student_summary_export,
)
from app.services.templates import templates

router = APIRouter()


@router.get("/exams/{exam_id}/exports", response_class=HTMLResponse)
def exports_page(request: Request, exam_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    exam = get_run_or_404(db, exam_id)
    return templates.TemplateResponse(
        request,
        "exports/index.html",
        {"request": request, "active_nav": "insights", "exam": exam},
    )


@router.get("/exams/{exam_id}/exports/student-summary.{file_type}")
def student_summary_export(
    exam_id: int,
    file_type: str,
    db: Session = Depends(get_db),
) -> FileResponse:
    exam = _exam_or_404(db, exam_id)
    path = export_path(generate_student_summary_export(db, exam, file_type))
    return FileResponse(path, filename=path.name)


@router.get("/exams/{exam_id}/exports/per-question.{file_type}")
def per_question_export(
    exam_id: int,
    file_type: str,
    db: Session = Depends(get_db),
) -> FileResponse:
    exam = _exam_or_404(db, exam_id)
    path = export_path(generate_per_question_export(db, exam, file_type))
    return FileResponse(path, filename=path.name)


@router.get("/exams/{exam_id}/exports/question-analysis.{file_type}")
def question_analysis_export(
    exam_id: int,
    file_type: str,
    db: Session = Depends(get_db),
) -> FileResponse:
    exam = _exam_or_404(db, exam_id)
    path = export_path(generate_question_analysis_export(db, exam, file_type))
    return FileResponse(path, filename=path.name)


def _exam_or_404(db: Session, exam_id: int):
    return get_run_or_404(db, exam_id)


@router.get("/exams/{exam_id}/exports/display/{table_key}.xlsx")
def display_results_export(
    request: Request, exam_id: int, table_key: str, db: Session = Depends(get_db)
):
    from io import BytesIO

    from fastapi import HTTPException
    from fastapi.responses import StreamingResponse

    from app.routes.results import results_page
    from app.services.display_export_service import (
        DisplayTable,
        ResultTableParser,
        build_display_workbook,
    )
    from app.services.i18n_service import _

    # Rendering uses the same request locale, ordering and presentation as the UI.
    response = results_page(request, exam_id, db)
    parser = ResultTableParser()
    parser.feed(response.body.decode("utf-8"))
    tables = (
        parser.tables
        if table_key == "all"
        else [table for table in parser.tables if table.key == table_key]
    )
    if not tables and table_key != "all":
        raise HTTPException(404, _("display_export.unknown"))
    if not tables:
        tables = [DisplayTable("empty", _("display_export.empty"))]
    return StreamingResponse(
        BytesIO(build_display_workbook(tables)),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="results_{exam_id}.xlsx"'},
    )
