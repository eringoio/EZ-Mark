import json
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.services.answer_key_service import validate_answer_key
from app.services.demo_service import ensure_uploads_allowed
from app.services.i18n_service import _
from app.services.list_filter_service import (
    filter_context,
    month_fallback_filters,
    parse_list_filters,
)
from app.services.peer_review_builder_service import (
    add_competency,
    add_rater_group,
    delete_competency,
    delete_rater_group,
    get_or_create_form,
    move_competency,
    peer_review_layout_payload,
    peer_review_template_issues,
    update_competency,
    update_form_settings,
    update_rater_group,
)
from app.services.peer_review_builder_service import (
    builder_state as peer_review_builder_state,
)
from app.services.portable_package_service import (
    PackageError,
    export_template_package,
    import_template_package,
    read_package,
    template_preview,
)
from app.services.sheet_generation_service import _coordinate_map_for_questions
from app.services.templates import templates
from app.services.test_template_service import (
    BuilderGroupInput,
    BuilderQuestionInput,
    BuilderSheetInput,
    TemplateCreateInput,
    TemplateUpdateInput,
    archive_template,
    create_blank_template,
    create_run_from_template,
    delete_template,
    duplicate_template,
    ensure_template_editable,
    get_template,
    list_runs_for_template,
    list_templates,
    list_templates_paginated,
    save_template_builder,
    update_template,
)

router = APIRouter()
settings = get_settings()

# Assessment type -> default scoring mode (mirrors the exam create route).
SCORING_MODES = {
    "generic_test": "generic_raw",
    "peer_review": "peer_average",
}
PEER_REVIEW_TEMPLATE_UNAVAILABLE = "Peer Evaluation is configured per session. Create a peer evaluation session to build reusable rating forms."

# Uploaded workbooks are parked here between preview and confirm (relative to data/).
PREVIEW_DIR = "import_previews"
PREVIEW_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def _editable_template_or_404(db: Session, template_id: int):
    """Load a template for a mutating route; 404 if missing, 409 if archived (read-only)."""
    template = get_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404)
    ensure_template_editable(template)
    return template


@router.get("/templates", response_class=HTMLResponse)
def templates_index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    filters = parse_list_filters(request.query_params)
    paginated = list_templates_paginated(db, filters)
    template_list = paginated.items
    # Templates have no test date, so the date filter (and same-month fallback) runs against
    # created_at — label it 作成日/Created on this page only.
    month_fallback_items = (
        list_templates(db, month_fallback_filters(filters))
        if filters.date and not template_list
        else []
    )
    return templates.TemplateResponse(
        request,
        "test_templates/index.html",
        {
            "active_nav": "templates",
            "template_list": template_list,
            "month_fallback_items": month_fallback_items,
            "filters": filter_context(filters),
            "filter_action": "/templates",
            "filter_date_label_key": "filters.created_date",
            "filter_archived_default": "hide",
            "pagination": paginated,
        },
    )


@router.post("/templates/bulk/archive", response_model=None)
def bulk_archive_templates(
    selected_ids: list[int] = Form(default=[]),
    return_to: str = Form("/templates"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    for template_id in selected_ids:
        template = get_template(db, template_id)
        if template is None or template.template_status == "archived":
            continue
        archive_template(db, template)
    return RedirectResponse(return_to or "/templates", status_code=303)


@router.post("/templates/bulk/delete", response_model=None)
def bulk_delete_templates(
    selected_ids: list[int] = Form(default=[]),
    return_to: str = Form("/templates"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    for template_id in selected_ids:
        template = get_template(db, template_id)
        if template is None:
            continue
        delete_template(db, template)
    return RedirectResponse(return_to or "/templates", status_code=303)


# --- Portable template packages (.ezmark-template.zip) ------------------------
#
# Separate from the result-export system (export_service): a portable package moves a
# whole template definition between EZ Mark installs. Export is allowed for archived
# templates; import always creates a new active template with freshly remapped ids.


@router.get("/templates/import-package", response_class=HTMLResponse)
def import_package_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "test_templates/package_import.html",
        {"active_nav": "templates", "errors": []},
    )


@router.post("/templates/import-package/preview", response_class=HTMLResponse)
async def import_package_preview(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    ensure_uploads_allowed()
    filename = (file.filename or "").lower()
    if not filename.endswith(".zip"):
        return templates.TemplateResponse(
            request,
            "test_templates/package_import.html",
            {"active_nav": "templates", "errors": [_("packages.invalid_package")]},
            status_code=400,
        )
    preview_dir = settings.data_root / PREVIEW_DIR
    preview_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    temp_path = preview_dir / f"{token}.zip"
    temp_path.write_bytes(await file.read())
    try:
        package = read_package(temp_path)
        if package["package_type"] != "template":
            raise PackageError(_("packages.wrong_type_template"))
        preview = template_preview(package)
    except PackageError as exc:
        temp_path.unlink(missing_ok=True)
        return templates.TemplateResponse(
            request,
            "test_templates/package_import.html",
            {"active_nav": "templates", "errors": [str(exc)]},
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "test_templates/package_preview.html",
        {"active_nav": "templates", "preview": preview, "token": token},
    )


@router.post("/templates/import-package/confirm", response_model=None)
def import_package_confirm(
    request: Request, token: str = Form(...), db: Session = Depends(get_db)
) -> Response:
    ensure_uploads_allowed()
    if not PREVIEW_TOKEN_RE.match(token):
        raise HTTPException(status_code=400, detail=_("packages.invalid_token"))
    temp_path = settings.data_root / PREVIEW_DIR / f"{token}.zip"
    if not temp_path.exists():
        raise HTTPException(status_code=404, detail=_("packages.expired"))
    try:
        package = read_package(temp_path)
        template = import_template_package(db, package)
    except PackageError as exc:
        return templates.TemplateResponse(
            request,
            "test_templates/package_import.html",
            {"active_nav": "templates", "errors": [str(exc)]},
            status_code=400,
        )
    finally:
        temp_path.unlink(missing_ok=True)
    return RedirectResponse(f"/templates/{template.id}", status_code=303)


@router.get("/templates/{template_id}/portable-export", response_model=None)
def portable_export_template(template_id: int, db: Session = Depends(get_db)) -> FileResponse:
    # Allowed for archived templates (export is read-only).
    template = get_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404)
    path = export_template_package(db, template)
    return FileResponse(path, filename=path.name, media_type="application/zip")


@router.get("/templates/new", response_class=HTMLResponse)
def new_template(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "test_templates/new.html",
        {"active_nav": "templates", "errors": []},
    )


@router.post("/templates/new", response_model=None)
def create_template(
    request: Request,
    name: str = Form(...),
    assessment_type: str = Form("generic_test"),
    template_description: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    data = TemplateCreateInput(
        name=name,
        assessment_type=assessment_type,
        scoring_mode=SCORING_MODES.get(assessment_type, "generic_raw"),
        numbering_mode="reset_per_problem",
        template_description=template_description or None,
    )
    try:
        template = create_blank_template(db, data)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "test_templates/new.html",
            {"active_nav": "templates", "errors": [str(exc)], "form": data},
            status_code=400,
        )
    if template.assessment_type == "peer_review":
        return RedirectResponse(f"/templates/{template.id}/peer-review", status_code=303)
    return RedirectResponse(f"/templates/{template.id}/builder", status_code=303)


@router.get("/templates/{template_id}/builder", response_model=None)
def template_builder(request: Request, template_id: int, db: Session = Depends(get_db)) -> Response:
    template = _editable_template_or_404(db, template_id)
    if template.assessment_type == "peer_review":
        # Peer-review templates have their own WYSIWYG builder, not the answer-key one.
        return RedirectResponse(f"/templates/{template_id}/peer-review", status_code=307)
    runs = list_runs_for_template(db, template)
    return templates.TemplateResponse(
        request,
        "test_templates/builder.html",
        {
            "active_nav": "templates",
            "template": template,
            "runs": runs,
            "issues": validate_answer_key(template),
            "errors": [],
            "saved": False,
        },
    )


@router.get("/templates/{template_id}/builder/state")
def builder_state(template_id: int, db: Session = Depends(get_db)) -> JSONResponse:
    template = _editable_template_or_404(db, template_id)
    if template.assessment_type == "peer_review":
        raise HTTPException(status_code=404, detail=PEER_REVIEW_TEMPLATE_UNAVAILABLE)
    return JSONResponse(_serialize_builder_state(template))


@router.post("/templates/{template_id}/builder/state")
async def save_builder_state(
    request: Request, template_id: int, db: Session = Depends(get_db)
) -> JSONResponse:
    template = _editable_template_or_404(db, template_id)
    if template.assessment_type == "peer_review":
        raise HTTPException(status_code=404, detail=PEER_REVIEW_TEMPLATE_UNAVAILABLE)
    payload = await request.json()
    try:
        save_template_builder(db, template, _parse_builder_state(payload))
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "state": _serialize_builder_state(template)})


@router.post("/templates/{template_id}/builder", response_class=HTMLResponse)
async def save_builder(
    request: Request, template_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    template = _editable_template_or_404(db, template_id)
    if template.assessment_type == "peer_review":
        raise HTTPException(status_code=404, detail=PEER_REVIEW_TEMPLATE_UNAVAILABLE)
    form = await request.form()
    try:
        save_template_builder(db, template, _parse_builder_form(form))
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "test_templates/builder.html",
            {
                "active_nav": "templates",
                "template": template,
                "runs": list_runs_for_template(db, template),
                "issues": validate_answer_key(template),
                "errors": [str(exc)],
                "saved": False,
            },
            status_code=400,
        )
    return RedirectResponse(f"/templates/{template.id}/builder?saved=1", status_code=303)


@router.get("/templates/{template_id}/builder/layout")
def builder_layout(
    template_id: int,
    physical_sheet_id: int,
    db: Session = Depends(get_db),
) -> JSONResponse:
    template = _editable_template_or_404(db, template_id)
    if template.assessment_type == "peer_review":
        raise HTTPException(status_code=404, detail=PEER_REVIEW_TEMPLATE_UNAVAILABLE)
    sheet = next(
        (sheet for sheet in template.physical_sheets if sheet.id == physical_sheet_id),
        None,
    )
    if sheet is None:
        raise HTTPException(status_code=404)
    questions = [
        question
        for question in template.questions
        if question.physical_sheet_id == physical_sheet_id
    ]
    coordinate_map = (
        _coordinate_map_for_questions(questions, sheet.sheet_name, sheet.slug) if questions else {}
    )
    selected_answers = {question.id: question.correct_answer for question in questions}
    return JSONResponse({"layout": coordinate_map, "selected_answers": selected_answers})


# --- Peer-review template builder ---------------------------------------------
#
# Peer-review templates are reusable structure only: a score scale, students-per-page,
# competencies (評価項目), and optional rater groups. They never hold real students,
# generated pages, scans, or results — those belong to sessions created from the
# template. The builder below edits that reusable structure with a WYSIWYG paper
# preview driven by the shared peer_review_layout_service.

PEER_BUILDER_PATH = "/templates/{template_id}/peer-review"


def _peer_template(db: Session, template_id: int):
    template = get_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404)
    if template.assessment_type != "peer_review":
        raise HTTPException(status_code=400, detail="This template is not a peer-review template.")
    # Every caller is a peer-review builder/settings route; archived templates are read-only.
    ensure_template_editable(template)
    return template


def _peer_redirect(template_id: int) -> RedirectResponse:
    return RedirectResponse(f"/templates/{template_id}/peer-review", status_code=303)


def _peer_mutate(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _parse_ruby_json(raw: str) -> list:
    """Parse the per-segment ruby payload (a JSON array string) into a list (``[]`` if bad)."""
    if not raw or not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


@router.get("/templates/{template_id}/peer-review", response_class=HTMLResponse)
def peer_review_template_builder(
    request: Request, template_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    template = _peer_template(db, template_id)
    state = peer_review_builder_state(db, template)
    return templates.TemplateResponse(
        request,
        "test_templates/peer_review_builder.html",
        {
            "active_nav": "templates",
            "template": template,
            "state": state,
            "layout": peer_review_layout_payload(db, template),
            "issues": peer_review_template_issues(db, template),
        },
    )


@router.get("/templates/{template_id}/peer-review/layout")
def peer_review_template_layout(template_id: int, db: Session = Depends(get_db)) -> JSONResponse:
    template = _peer_template(db, template_id)
    return JSONResponse(peer_review_layout_payload(db, template))


@router.get("/templates/{template_id}/peer-review/builder/state")
def peer_review_template_builder_state(
    template_id: int, db: Session = Depends(get_db)
) -> JSONResponse:
    template = _peer_template(db, template_id)
    return JSONResponse(peer_review_builder_state(db, template))


@router.post("/templates/{template_id}/peer-review/builder/state")
async def save_peer_review_template_builder_state(
    request: Request, template_id: int, db: Session = Depends(get_db)
) -> JSONResponse:
    template = _peer_template(db, template_id)
    form = get_or_create_form(db, template)
    _peer_mutate(update_form_settings, db, form, await request.json())
    return JSONResponse(peer_review_builder_state(db, template))


@router.post("/templates/{template_id}/peer-review/settings", response_model=None)
def save_peer_review_template_settings(
    template_id: int,
    title: str = Form(...),
    score_min: int = Form(...),
    score_max: int = Form(...),
    students_per_page: int = Form(...),
    page_orientation_mode: str = Form("auto"),
    db: Session = Depends(get_db),
) -> Response:
    template = _peer_template(db, template_id)
    form = get_or_create_form(db, template)
    _peer_mutate(
        update_form_settings,
        db,
        form,
        {
            "title": title.strip() or template.name,
            "score_min": score_min,
            "score_max": score_max,
            "students_per_page": students_per_page,
            "page_orientation_mode": page_orientation_mode,
        },
    )
    return _peer_redirect(template_id)


@router.post("/templates/{template_id}/peer-review/competencies", response_model=None)
def add_peer_review_template_competency(
    template_id: int,
    name: str = Form(...),
    name_furigana: str = Form(""),
    short_label: str = Form(""),
    name_ruby_json: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    form = get_or_create_form(db, _peer_template(db, template_id))
    _peer_mutate(
        add_competency,
        db,
        form,
        name,
        description or None,
        name_furigana or None,
        short_label or None,
        _parse_ruby_json(name_ruby_json),
    )
    return _peer_redirect(template_id)


@router.post(
    "/templates/{template_id}/peer-review/competencies/{competency_id}", response_model=None
)
def edit_peer_review_template_competency(
    template_id: int,
    competency_id: int,
    name: str = Form(...),
    name_furigana: str = Form(""),
    short_label: str = Form(""),
    name_ruby_json: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    form = get_or_create_form(db, _peer_template(db, template_id))
    _peer_mutate(
        update_competency,
        db,
        form,
        competency_id,
        {
            "name": name,
            "name_furigana": name_furigana,
            "short_label": short_label,
            "name_ruby": _parse_ruby_json(name_ruby_json),
            "description": description or None,
        },
    )
    return _peer_redirect(template_id)


@router.post(
    "/templates/{template_id}/peer-review/competencies/{competency_id}/move",
    response_model=None,
)
def move_peer_review_template_competency(
    template_id: int,
    competency_id: int,
    direction: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    form = get_or_create_form(db, _peer_template(db, template_id))
    _peer_mutate(move_competency, db, form, competency_id, direction)
    return _peer_redirect(template_id)


@router.post(
    "/templates/{template_id}/peer-review/competencies/{competency_id}/delete",
    response_model=None,
)
def delete_peer_review_template_competency(
    template_id: int, competency_id: int, db: Session = Depends(get_db)
) -> Response:
    form = get_or_create_form(db, _peer_template(db, template_id))
    _peer_mutate(delete_competency, db, form, competency_id)
    return _peer_redirect(template_id)


@router.post("/templates/{template_id}/peer-review/groups", response_model=None)
def add_peer_review_template_group(
    template_id: int,
    name: str = Form(...),
    weight_percent: float = Form(100),
    description: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    form = get_or_create_form(db, _peer_template(db, template_id))
    _peer_mutate(add_rater_group, db, form, name, weight_percent, description or None)
    return _peer_redirect(template_id)


@router.post("/templates/{template_id}/peer-review/groups/{group_id}", response_model=None)
def edit_peer_review_template_group(
    template_id: int,
    group_id: int,
    name: str = Form(...),
    weight_percent: float = Form(100),
    description: str = Form(""),
    active: bool = Form(True),
    db: Session = Depends(get_db),
) -> Response:
    form = get_or_create_form(db, _peer_template(db, template_id))
    _peer_mutate(
        update_rater_group,
        db,
        form,
        group_id,
        {
            "name": name,
            "weight_percent": weight_percent,
            "description": description or None,
            "active": active,
        },
    )
    return _peer_redirect(template_id)


@router.post("/templates/{template_id}/peer-review/groups/{group_id}/delete", response_model=None)
def delete_peer_review_template_group(
    template_id: int, group_id: int, db: Session = Depends(get_db)
) -> Response:
    form = get_or_create_form(db, _peer_template(db, template_id))
    _peer_mutate(delete_rater_group, db, form, group_id)
    return _peer_redirect(template_id)


def _parse_builder_form(form: Any) -> list[BuilderSheetInput]:
    sheets: list[BuilderSheetInput] = []
    for sheet_index, sheet_id in enumerate(form.getlist("sheet_id")):
        groups: list[BuilderGroupInput] = []
        group_prefix = f"sheet_{sheet_index}_group_id"
        for group_index, raw_group_id in enumerate(form.getlist(group_prefix)):
            questions: list[BuilderQuestionInput] = []
            question_prefix = f"sheet_{sheet_index}_group_{group_index}_question_id"
            for question_index, raw_question_id in enumerate(form.getlist(question_prefix)):
                field_prefix = f"sheet_{sheet_index}_group_{group_index}_question_{question_index}"
                questions.append(
                    BuilderQuestionInput(
                        id=_optional_int(raw_question_id),
                        number_label=str(form.get(f"{field_prefix}_number") or question_index + 1),
                        correct_answer=(form.get(f"{field_prefix}_answer") or None),
                        choice_count=int(form.get(f"{field_prefix}_choice_count") or 4),
                        points=float(form.get(f"{field_prefix}_points") or 1),
                        scoring_section_id=_optional_int(
                            form.get(f"{field_prefix}_scoring_section_id")
                        ),
                    )
                )
            groups.append(
                BuilderGroupInput(
                    id=_optional_int(raw_group_id),
                    problem_title=str(
                        form.get(f"sheet_{sheet_index}_group_{group_index}_title") or ""
                    ),
                    questions=questions,
                )
            )
        sheets.append(
            BuilderSheetInput(
                id=int(sheet_id),
                sheet_name=str(form.get(f"sheet_{sheet_index}_name") or ""),
                groups=groups,
            )
        )
    return sheets


def _serialize_builder_state(template: Any) -> dict[str, Any]:
    questions_by_sheet_and_problem: dict[tuple[int, int], list[Any]] = {}
    for question in template.questions:
        key = (question.physical_sheet_id, question.problem_no)
        questions_by_sheet_and_problem.setdefault(key, []).append(question)

    sheets = []
    for sheet in template.physical_sheets:
        groups = [group for group in template.problem_groups if group.physical_sheet_id == sheet.id]
        groups.sort(key=lambda group: (group.display_order, group.problem_no, group.id))
        serialized_groups = []
        covered_question_ids: set[int] = set()
        for group in groups:
            questions = questions_by_sheet_and_problem.get((sheet.id, group.problem_no), [])
            questions.sort(key=_builder_question_sort_key)
            covered_question_ids.update(question.id for question in questions)
            serialized_groups.append(
                _serialize_builder_group(
                    template,
                    group.id,
                    group.problem_no,
                    group.problem_title,
                    questions,
                )
            )
        # Workbook-imported templates have questions but no ProblemGroup rows
        # (and no problem_no), so without a fallback the builder looks empty.
        # Synthesize unsaved groups; saving the builder persists them for real.
        leftovers = [
            question
            for question in template.questions
            if question.physical_sheet_id == sheet.id and question.id not in covered_question_ids
        ]
        if leftovers:
            serialized_groups.extend(
                _synthesize_builder_groups(template, leftovers, start_no=len(serialized_groups) + 1)
            )
        sheets.append(
            {
                "id": sheet.id,
                "name": sheet.sheet_name,
                "sheet_no": sheet.sheet_no,
                "groups": serialized_groups,
            }
        )

    return {
        "template": {
            "id": template.id,
            "name": template.name,
            "assessment_type": template.assessment_type,
            "has_runs": bool(template.template_runs),
        },
        "scoring_sections": [
            {"id": section.id, "name": section.name} for section in template.scoring_sections
        ],
        "sheets": sheets,
    }


def _builder_question_sort_key(question: Any) -> tuple:
    return (
        question.display_order if question.display_order is not None else 0,
        question.subquestion_no if question.subquestion_no is not None else 0,
        question.id,
    )


def _serialize_builder_group(
    template: Any,
    group_id: int | None,
    problem_no: int,
    problem_title: str | None,
    questions: list[Any],
) -> dict[str, Any]:
    return {
        "id": group_id,
        "problem_no": problem_no,
        "problem_title": problem_title,
        "default_points": questions[0].points if questions else 1,
        "default_scoring_section_id": (
            questions[0].scoring_section_id
            if questions
            else (template.scoring_sections[0].id if template.scoring_sections else None)
        ),
        "default_choice_count": questions[0].choice_count if questions else 4,
        "questions": [
            {
                "id": question.id,
                "number": (
                    question.local_question_label
                    if question.local_question_label
                    else question.subquestion_no
                    if question.subquestion_no is not None
                    else question.local_question_no
                    if question.local_question_no is not None
                    else index
                ),
                "correct_answer": question.correct_answer,
                "choice_count": question.choice_count,
                "points": question.points,
                "scoring_section_id": question.scoring_section_id,
            }
            for index, question in enumerate(questions, start=1)
        ],
    }


def _synthesize_builder_groups(
    template: Any, questions: list[Any], start_no: int
) -> list[dict[str, Any]]:
    """Split groupless questions into builder groups (id=None until saved).

    Contiguous runs of the same problem_no (when set) or item_type (the question
    category, e.g. 漢字読み) become one group each.
    """
    questions = sorted(questions, key=_builder_question_sort_key)
    runs: list[tuple[Any, list[Any]]] = []
    for question in questions:
        run_key = question.problem_no if question.problem_no is not None else question.item_type
        if not runs or runs[-1][0] != run_key:
            runs.append((run_key, []))
        runs[-1][1].append(question)

    groups = []
    for offset, (_run_key, run_questions) in enumerate(runs):
        problem_no = start_no + offset
        if run_questions[0].problem_no is None and run_questions[0].item_type:
            title = run_questions[0].item_type
        else:
            title = f"もんだい{problem_no}"
        groups.append(_serialize_builder_group(template, None, problem_no, title, run_questions))
    return groups


def _parse_builder_state(payload: dict[str, Any]) -> list[BuilderSheetInput]:
    sheets: list[BuilderSheetInput] = []
    for raw_sheet in payload.get("sheets", []):
        groups: list[BuilderGroupInput] = []
        for raw_group in raw_sheet.get("groups", []):
            questions = [
                BuilderQuestionInput(
                    id=_optional_int(raw_question.get("id")),
                    number_label=str(raw_question.get("number") or "").strip() or None,
                    correct_answer=raw_question.get("correct_answer") or None,
                    choice_count=int(raw_question.get("choice_count") or 4),
                    points=float(raw_question.get("points") or 0),
                    scoring_section_id=_optional_int(raw_question.get("scoring_section_id")),
                )
                for raw_question in raw_group.get("questions", [])
            ]
            groups.append(
                BuilderGroupInput(
                    id=_optional_int(raw_group.get("id")),
                    problem_title=str(raw_group.get("problem_title") or ""),
                    questions=questions,
                )
            )
        sheets.append(
            BuilderSheetInput(
                id=int(raw_sheet["id"]),
                sheet_name=str(raw_sheet.get("name") or ""),
                groups=groups,
            )
        )
    return sheets


def _optional_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(value)


@router.get("/templates/{template_id}", response_class=HTMLResponse)
def template_detail(
    request: Request, template_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    template = get_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404)
    runs = list_runs_for_template(db, template)
    peer = (
        _peer_template_context(db, template) if template.assessment_type == "peer_review" else None
    )
    return templates.TemplateResponse(
        request,
        "test_templates/detail.html",
        {"active_nav": "templates", "template": template, "runs": runs, "peer": peer},
    )


def _peer_template_context(db: Session, template) -> dict:
    """Readiness + reusable-structure stats shown on a peer-review template detail page."""
    form = get_or_create_form(db, template)
    issues = peer_review_template_issues(db, template)
    return {
        "ready": not issues,
        "issues": issues,
        "score_min": form.score_min,
        "score_max": form.score_max,
        "students_per_page": form.students_per_page,
        "competencies_count": len(template.competencies),
        "rater_groups_count": len([g for g in form.rater_groups if g.active]),
    }


@router.get("/templates/{template_id}/runs/new", response_class=HTMLResponse)
def new_run_form(request: Request, template_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    template = _editable_template_or_404(db, template_id)
    peer = (
        _peer_template_context(db, template) if template.assessment_type == "peer_review" else None
    )
    return templates.TemplateResponse(
        request,
        "test_templates/run_new.html",
        {"active_nav": "templates", "template": template, "peer": peer, "errors": []},
    )


@router.post("/templates/{template_id}/runs/new", response_model=None)
def create_run(
    request: Request,
    template_id: int,
    run_name: str = Form(...),
    exam_date: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    template = _editable_template_or_404(db, template_id)
    try:
        run = create_run_from_template(db, template, run_name, exam_date or None)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "test_templates/run_new.html",
            {
                "active_nav": "templates",
                "template": template,
                "errors": [str(exc)],
                "run_name": run_name,
                "exam_date": exam_date or "",
            },
            status_code=400,
        )
    return RedirectResponse(f"/exams/{run.id}/students/import", status_code=303)


@router.get("/templates/{template_id}/edit", response_class=HTMLResponse)
def edit_template_form(
    request: Request, template_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    template = _editable_template_or_404(db, template_id)
    return templates.TemplateResponse(
        request,
        "test_templates/edit.html",
        {"active_nav": "templates", "template": template, "errors": []},
    )


@router.post("/templates/{template_id}/edit", response_model=None)
async def edit_template(
    request: Request, template_id: int, db: Session = Depends(get_db)
) -> Response:
    template = _editable_template_or_404(db, template_id)

    form = await request.form()
    data = TemplateUpdateInput(
        name=str(form.get("name") or ""),
        template_description=str(form.get("template_description") or "") or None,
        pass_mark_value=_pass_mark_value(form),
        pass_mark_type=str(form.get("pass_mark_type") or "") or None,
    )
    try:
        update_template(db, template, data)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "test_templates/edit.html",
            {"active_nav": "templates", "template": template, "errors": [str(exc)]},
            status_code=400,
        )
    return RedirectResponse(f"/templates/{template_id}", status_code=303)


@router.post("/templates/{template_id}/delete", response_model=None)
def delete_template_route(template_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    template = get_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404)
    delete_template(db, template)
    return RedirectResponse("/templates", status_code=303)


def _pass_mark_value(form: Any) -> float | None:
    value = form.get("pass_mark_value")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@router.post("/templates/{template_id}/duplicate", response_model=None)
def duplicate_template_route(template_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    template = get_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404)
    copy = duplicate_template(db, template)
    return RedirectResponse(f"/templates/{copy.id}/edit", status_code=303)


@router.post("/templates/{template_id}/archive", response_model=None)
def archive_template_route(template_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    template = get_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404)
    archive_template(db, template)
    return RedirectResponse("/templates", status_code=303)
