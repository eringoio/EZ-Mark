from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.answer_key_service import (
    apply_problem_group_counts_to_sheet,
    generate_continuous_questions,
    generate_grouped_questions,
    save_answer_key,
    validate_answer_key,
)
from app.services.exam_service import get_exam
from app.services.sheet_generation_service import _group_questions_for_layout
from app.services.templates import templates

router = APIRouter()


def _active_nav(exam) -> str:
    return "templates" if getattr(exam, "is_template", False) else "sessions"


@router.get("/exams/{exam_id}/question-structure", response_class=HTMLResponse)
def question_structure(
    request: Request, exam_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    exam = get_exam(db, exam_id)
    if exam is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "answer_keys/question_structure.html",
        {"request": request, "active_nav": _active_nav(exam), "exam": exam, "errors": []},
    )


@router.post(
    "/exams/{exam_id}/question-structure/generate-questions",
    response_model=None,
)
def generate_questions(
    request: Request,
    exam_id: int,
    numbering_mode: str = Form("continuous"),
    total_questions: int = Form(30),
    group_counts: str = Form("5,5,5"),
    choice_count: int = Form(4),
    physical_sheet_id: int = Form(...),
    scoring_section_id: int | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    exam = get_exam(db, exam_id)
    if exam is None:
        raise HTTPException(status_code=404)
    try:
        if numbering_mode == "continuous":
            generate_continuous_questions(
                db,
                exam,
                total_questions,
                choice_count,
                physical_sheet_id,
                scoring_section_id,
            )
        else:
            counts = [int(value.strip()) for value in group_counts.split(",") if value.strip()]
            generate_grouped_questions(
                db,
                exam,
                counts,
                choice_count,
                physical_sheet_id,
                scoring_section_id,
            )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "answer_keys/question_structure.html",
            {"request": request, "active_nav": _active_nav(exam), "exam": exam, "errors": [str(exc)]},
            status_code=400,
        )
    return RedirectResponse(f"/exams/{exam_id}/answer-key", status_code=303)


@router.get("/exams/{exam_id}/answer-key", response_class=HTMLResponse)
def answer_key(request: Request, exam_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    exam = get_exam(db, exam_id)
    if exam is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "answer_keys/edit.html",
        {
            "request": request,
            "active_nav": _active_nav(exam),
            "exam": exam,
            "issues": validate_answer_key(exam),
            "saved": False,
            "problem_group_counts": _problem_group_counts_by_sheet(exam),
            "sheet_question_counts": _question_counts_by_sheet(exam),
            "sheet_groups": _sheet_groups(exam),
        },
    )


@router.post("/exams/{exam_id}/answer-key/problem-groups", response_class=HTMLResponse)
def update_problem_groups(
    request: Request,
    exam_id: int,
    physical_sheet_id: int = Form(...),
    group_counts: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    exam = get_exam(db, exam_id)
    if exam is None:
        raise HTTPException(status_code=404)
    try:
        counts = _parse_group_counts(group_counts)
        apply_problem_group_counts_to_sheet(db, exam, physical_sheet_id, counts)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "answer_keys/edit.html",
            {
                "request": request,
                "active_nav": _active_nav(exam),
                "exam": exam,
                "issues": validate_answer_key(exam),
                "saved": False,
                "problem_group_counts": _problem_group_counts_by_sheet(exam),
                "sheet_question_counts": _question_counts_by_sheet(exam),
                "sheet_groups": _sheet_groups(exam),
                "group_errors": [str(exc)],
            },
            status_code=400,
        )
    return RedirectResponse(f"/exams/{exam_id}/answer-key", status_code=303)


@router.post("/exams/{exam_id}/answer-key/save", response_class=HTMLResponse)
async def save_answers(
    request: Request,
    exam_id: int,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    exam = get_exam(db, exam_id)
    if exam is None:
        raise HTTPException(status_code=404)
    if not exam.is_template:
        return RedirectResponse(f"/exams/{exam_id}/answer-key", status_code=303)
    form = await request.form()
    answers = {
        int(key.removeprefix("answer_")): str(value)
        for key, value in form.items()
        if key.startswith("answer_")
    }
    issues = save_answer_key(db, exam, answers)
    return templates.TemplateResponse(
        request,
        "answer_keys/edit.html",
        {
            "request": request,
            "active_nav": _active_nav(exam),
            "exam": exam,
            "issues": issues,
            "saved": not issues,
            "problem_group_counts": _problem_group_counts_by_sheet(exam),
            "sheet_question_counts": _question_counts_by_sheet(exam),
            "sheet_groups": _sheet_groups(exam),
        },
    )


def _parse_group_counts(raw_counts: str) -> list[int]:
    try:
        return [int(value.strip()) for value in raw_counts.split(",") if value.strip()]
    except ValueError as exc:
        raise ValueError("Problem counts must be comma-separated whole numbers.") from exc


def _problem_group_counts_by_sheet(exam) -> dict[int, str]:
    result: dict[int, str] = {}
    for sheet in exam.physical_sheets:
        groups = sorted(
            [
                group
                for group in exam.problem_groups
                if group.physical_sheet_id == sheet.id
            ],
            key=lambda group: group.display_order,
        )
        if groups:
            result[sheet.id] = ",".join(str(group.subquestion_count) for group in groups)
    return result


def _question_counts_by_sheet(exam) -> dict[int, int]:
    return {
        sheet.id: len(
            [question for question in exam.questions if question.physical_sheet_id == sheet.id]
        )
        for sheet in exam.physical_sheets
    }


def _sheet_groups(exam) -> dict[int, list[dict]]:
    """Build {sheet_id: [{label, questions, max_choices}]} using the same grouping as the PDF."""
    result: dict[int, list[dict]] = {}
    for sheet in exam.physical_sheets:
        sheet_qs = sorted(
            [q for q in exam.questions if q.physical_sheet_id == sheet.id],
            key=lambda q: q.display_order or 0,
        )
        if not sheet_qs:
            continue
        raw_groups = _group_questions_for_layout(sheet_qs, sheet.sheet_name)
        result[sheet.id] = [
            {
                "label": label,
                "questions": group_questions,
                "max_choices": max(q.choice_count for q in group_questions),
            }
            for label, group_questions in raw_groups
        ]
    return result
