from datetime import datetime, timedelta
from io import BytesIO
from secrets import token_urlsafe

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ExamStudent, Student, StudentImportBatch
from app.services.demo_service import ensure_uploads_allowed
from app.services.exam_service import get_exam, get_run_or_404
from app.services.i18n_service import _
from app.services.peer_review_builder_service import sync_peer_review_targets
from app.services.student_import_service import (
    build_student_template_xlsx,
    import_preview_to_payload,
    import_students_from_payload,
    parse_student_import,
    validate_student_rows,
)
from app.services.templates import templates

router = APIRouter()


def _sync_peer_review_roster(db: Session, exam) -> None:
    """Keep peer-review session targets aligned with the real roster after roster edits."""
    if exam.assessment_type == "peer_review":
        previous = db.info.get("defer_roster_commit")
        db.info["defer_roster_commit"] = True
        try:
            sync_peer_review_targets(db, exam)
        finally:
            if previous is None:
                db.info.pop("defer_roster_commit", None)
            else:
                db.info["defer_roster_commit"] = previous


@router.get("/students", response_class=HTMLResponse)
def all_students(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    students = list(
        db.scalars(select(Student).order_by(Student.class_name, Student.student_number))
    )
    return templates.TemplateResponse(
        request,
        "students/index.html",
        {"request": request, "active_nav": "sessions", "students": students, "exam": None},
    )


@router.get("/exams/{exam_id}/students", response_class=HTMLResponse)
def exam_students(request: Request, exam_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    exam = get_run_or_404(db, exam_id, for_write=True)
    links = sorted(
        [link for link in exam.students if link.included],
        key=lambda link: (
            link.seat_order is None,
            link.seat_order or 0,
            link.group_name,
            link.student.student_number,
        ),
    )
    return templates.TemplateResponse(
        request,
        "students/index.html",
        {
            "request": request,
            "active_nav": "sessions",
            "students": [link.student for link in links],
            "student_links": links,
            "exam": exam,
        },
    )


@router.get("/students/{student_id}/edit", response_class=HTMLResponse)
def global_student_edit_form(
    request: Request,
    student_id: int,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    student = db.get(Student, student_id)
    if student is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "students/edit.html",
        {
            "request": request,
            "active_nav": "sessions",
            "exam": None,
            "student": student,
            "link": None,
            "errors": [],
            "mode": "edit",
        },
    )


@router.post("/students/{student_id}/edit", response_model=None)
def global_student_edit(
    request: Request,
    student_id: int,
    student_number: str = Form(...),
    name: str = Form(...),
    class_name: str = Form(...),
    active: bool = Form(False),
    db: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    student = db.get(Student, student_id)
    if student is None:
        raise HTTPException(status_code=404)
    errors = _validate_student_update(db, student, student_number, name, class_name)
    if errors:
        return templates.TemplateResponse(
            request,
            "students/edit.html",
            {
                "request": request,
                "active_nav": "sessions",
                "exam": None,
                "student": student,
                "link": None,
                "errors": errors,
                "mode": "edit",
            },
            status_code=400,
        )
    _apply_student_fields(student, student_number, name, class_name, active)
    db.commit()
    return RedirectResponse("/students", status_code=303)


@router.get("/exams/{exam_id}/students/new", response_class=HTMLResponse)
def exam_student_new_form(
    request: Request,
    exam_id: int,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    exam = get_run_or_404(db, exam_id, for_write=True)
    return templates.TemplateResponse(
        request,
        "students/edit.html",
        {
            "request": request,
            "active_nav": "sessions",
            "exam": exam,
            "student": None,
            "link": None,
            "errors": [],
            "mode": "new",
        },
    )


@router.post("/exams/{exam_id}/students/new", response_model=None)
def exam_student_new(
    request: Request,
    exam_id: int,
    student_number: str = Form(...),
    name: str = Form(...),
    class_name: str = Form(...),
    active: bool = Form(False),
    seat_order: int | None = Form(None),
    db: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    exam = get_run_or_404(db, exam_id, for_write=True)
    student_number = student_number.strip()
    name = name.strip()
    class_name = class_name.strip()
    errors = _validate_required_student_fields(student_number, name, class_name)
    if errors:
        return _student_form_response(request, exam, None, None, "new", errors, 400)

    preview = validate_student_rows(
        [
            {
                "student_number": student_number,
                "name": name,
                "class_name": class_name,
                "requested_seat_order": seat_order,
            }
        ],
        db,
        exam,
    )
    if preview.error_rows:
        return _student_form_response(
            request, exam, None, None, "new", [row.message for row in preview.error_rows], 400
        )
    if any(row.status == "conflict" for row in preview.rows):
        return _preview_response(request, db, exam, preview)

    if preview.rows[0].status == "attached":
        return _student_form_response(
            request, exam, None, None, "new", [_("students.conflicts.attached")], 400
        )
    import_students_from_payload(db, exam, import_preview_to_payload(preview), commit=False)
    student = (
        db.get(Student, preview.rows[0].existing_id)
        if preview.rows[0].existing_id
        else db.scalar(select(Student).where(Student.student_number == student_number))
    )
    link = _exam_student_link(db, exam.id, student.id)
    if not preview.rows[0].existing_id:
        student.active = active
    if seat_order is not None:
        link.seat_order = seat_order
    db.flush()
    _sync_peer_review_roster(db, exam)
    db.commit()
    return RedirectResponse(f"/exams/{exam_id}/students", status_code=303)


@router.get("/exams/{exam_id}/students/{student_id}/edit", response_class=HTMLResponse)
def exam_student_edit_form(
    request: Request,
    exam_id: int,
    student_id: int,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    exam = get_run_or_404(db, exam_id, for_write=True)
    student = db.get(Student, student_id)
    link = _exam_student_link(db, exam_id, student_id)
    if student is None or link is None:
        raise HTTPException(status_code=404)
    return _student_form_response(request, exam, student, link, "edit", [], 200)


@router.post("/exams/{exam_id}/students/{student_id}/edit", response_model=None)
def exam_student_edit(
    request: Request,
    exam_id: int,
    student_id: int,
    student_number: str = Form(...),
    name: str = Form(...),
    class_name: str = Form(...),
    active: bool = Form(False),
    seat_order: int | None = Form(None),
    db: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    exam = get_run_or_404(db, exam_id, for_write=True)
    student = db.get(Student, student_id)
    link = _exam_student_link(db, exam_id, student_id)
    if student is None or link is None:
        raise HTTPException(status_code=404)
    errors = _validate_student_update(db, student, student_number, name, class_name)
    if errors:
        return _student_form_response(request, exam, student, link, "edit", errors, 400)
    _apply_student_fields(student, student_number, name, student.class_name, active)
    link.class_name = class_name.strip()
    link.seat_order = seat_order
    db.commit()
    return RedirectResponse(f"/exams/{exam_id}/students", status_code=303)


@router.post("/exams/{exam_id}/students/{student_id}/remove-from-run", response_model=None)
def remove_student_from_exam(
    exam_id: int,
    student_id: int,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    get_run_or_404(db, exam_id, for_write=True)
    link = _exam_student_link(db, exam_id, student_id)
    if link is None:
        raise HTTPException(status_code=404)
    link.included = False
    db.commit()
    return RedirectResponse(f"/exams/{exam_id}/students", status_code=303)


@router.post("/exams/{exam_id}/students/bulk-update", response_model=None)
async def bulk_update_exam_students(
    exam_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    exam = get_run_or_404(db, exam_id, for_write=True)
    form = await request.form()

    # An inline addition can reuse the conflict preview when the existing rows are
    # unchanged. Reject combined conflicting additions/edits rather than dropping edits.
    arrays = [
        form.getlist(key)
        for key in ("student_id", "student_number", "name", "class_name", "seat_order")
    ]
    if len({len(values) for values in arrays}) != 1:
        raise HTTPException(400, _("students.conflicts.invalid_rows"))
    pending_rows = []
    unchanged = not form.getlist("remove_id")
    for sid, number, name, group, order in zip(*arrays, strict=True):
        if not sid.strip():
            if number.strip() or name.strip() or group.strip():
                pending_rows.append({"student_number": number, "name": name, "class_name": group})
                unchanged = unchanged and not order.strip()
        else:
            try:
                link = _exam_student_link(db, exam.id, int(sid))
            except ValueError as exc:
                raise HTTPException(400, _("students.conflicts.invalid")) from exc
            if link is None:
                raise HTTPException(400, _("students.conflicts.invalid"))
            unchanged = unchanged and (
                number.strip(),
                name.strip(),
                group.strip(),
                order.strip(),
            ) == (
                link.student.student_number,
                link.student.name,
                link.group_name,
                str(link.seat_order or ""),
            )
    if pending_rows:
        preview = validate_student_rows(pending_rows, db, exam)
        if preview.error_rows:
            raise HTTPException(400, "; ".join(row.message for row in preview.error_rows))
        if any(row.status == "conflict" for row in preview.rows):
            if unchanged:
                return _preview_response(request, db, exam, preview)
            raise HTTPException(400, _("students.conflicts.use_import"))

    for remove_id_str in form.getlist("remove_id"):
        try:
            sid = int(remove_id_str.strip())
        except (ValueError, AttributeError):
            continue
        link = _exam_student_link(db, exam_id, sid)
        if link:
            link.included = False

    student_ids = form.getlist("student_id")
    student_numbers = form.getlist("student_number")
    names = form.getlist("name")
    class_names = form.getlist("class_name")
    seat_orders = form.getlist("seat_order")

    if (
        len(
            {
                len(values)
                for values in (student_ids, student_numbers, names, class_names, seat_orders)
            }
        )
        != 1
    ):
        raise HTTPException(400, _("students.conflicts.invalid_rows"))
    for sid_str, sn, nm, cn, so in zip(
        student_ids, student_numbers, names, class_names, seat_orders, strict=False
    ):
        sn, nm, cn, so = sn.strip(), nm.strip(), cn.strip(), so.strip()
        try:
            seat_order_int: int | None = int(so) if so else None
        except ValueError:
            seat_order_int = None

        if sid_str.strip():
            try:
                student = db.get(Student, int(sid_str))
            except (ValueError, TypeError):
                continue
            if student is None:
                continue
            link = _exam_student_link(db, exam_id, student.id)
            if link is None:
                raise HTTPException(400, _("students.conflicts.invalid"))
            errors = _validate_student_update(db, student, sn, nm, cn)
            if errors:
                raise HTTPException(400, "; ".join(errors))
            if sn:
                student.student_number = sn
            if nm:
                student.name = nm
            if cn:
                link.class_name = cn
            link = _exam_student_link(db, exam_id, student.id)
            if link:
                link.seat_order = seat_order_int
            db.flush()
        else:
            if not sn or not nm or not cn:
                continue
            candidate = validate_student_rows(
                [{"student_number": sn, "name": nm, "class_name": cn}], db, exam
            )
            if candidate.error_rows:
                raise HTTPException(400, candidate.error_rows[0].message)
            if candidate.rows[0].status == "conflict":
                raise HTTPException(400, _("students.conflicts.use_import"))
            student = (
                db.get(Student, candidate.rows[0].existing_id)
                if candidate.rows[0].existing_id
                else None
            )
            if student is None:
                student = Student(student_number=sn, name=nm, class_name=cn, active=True)
                db.add(student)
                db.flush()
            link = _exam_student_link(db, exam_id, student.id)
            if link is None:
                db.add(
                    ExamStudent(
                        exam_id=exam.id,
                        student_id=student.id,
                        class_name=cn,
                        seat_order=seat_order_int,
                        included=True,
                    )
                )
            else:
                link.included = True
                if seat_order_int is not None:
                    link.seat_order = seat_order_int

    db.flush()
    _sync_peer_review_roster(db, exam)
    db.commit()
    return RedirectResponse(f"/exams/{exam_id}/students", status_code=303)


@router.post("/students/bulk-update", response_model=None)
async def bulk_update_global_students(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = await request.form()
    student_ids = form.getlist("student_id")
    student_numbers = form.getlist("student_number")
    names = form.getlist("name")
    class_names = form.getlist("class_name")

    if len({len(values) for values in (student_ids, student_numbers, names, class_names)}) != 1:
        raise HTTPException(400, _("students.conflicts.invalid_rows"))
    for sid_str, sn, nm, cn in zip(student_ids, student_numbers, names, class_names, strict=False):
        sn, nm, cn = sn.strip(), nm.strip(), cn.strip()
        if not sid_str.strip():
            continue
        try:
            student = db.get(Student, int(sid_str))
        except (ValueError, TypeError):
            continue
        if student is None:
            continue
        errors = _validate_student_update(db, student, sn, nm, cn)
        if errors:
            raise HTTPException(400, "; ".join(errors))
        if sn:
            student.student_number = sn
        if nm:
            student.name = nm
        if cn:
            student.class_name = cn
        db.flush()

    db.commit()
    return RedirectResponse("/students", status_code=303)


@router.post("/exams/{exam_id}/students/add-manual", response_model=None)
async def add_students_manual(
    request: Request,
    exam_id: int,
    db: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    exam = get_run_or_404(db, exam_id, for_write=True)
    form = await request.form()
    student_numbers = form.getlist("student_number")
    names = form.getlist("name")
    class_names = form.getlist("class_name")

    if len({len(values) for values in (student_numbers, names, class_names)}) != 1:
        raise HTTPException(400, _("students.conflicts.invalid_rows"))

    # Student numbers already included in this session — adding them again is a duplicate.
    # Compare on a whitespace-insensitive key so a stray/full-width space cannot sneak the
    # same student in twice.
    existing_keys = {
        _student_number_key(link.student.student_number)
        for link in exam.students
        if link.included and link.student is not None
    }

    rows: list[dict] = []
    errors: list[str] = []
    seen_keys: dict[str, int] = {}
    for sn, nm, cn in zip(student_numbers, names, class_names, strict=False):
        rows.append(
            {
                "student_number": sn.strip(),
                "name": nm.strip(),
                "class_name": cn.strip(),
                "field_errors": {},
            }
        )

    valid_rows: list[dict] = []
    for index, row in enumerate(rows):
        sn, nm, cn = row["student_number"], row["name"], row["class_name"]
        # Fully empty rows are ignored; partially filled rows fail loud.
        if not sn and not nm and not cn:
            continue
        display_row = index + 1
        if not sn:
            row["field_errors"]["student_number"] = True
        if not nm:
            row["field_errors"]["name"] = True
        if not cn:
            row["field_errors"]["class_name"] = True
        if row["field_errors"]:
            errors.append(_("students.import_form.error_missing_fields", row=display_row))
            continue
        key = _student_number_key(sn)
        if key in seen_keys:
            row["field_errors"]["student_number"] = True
            errors.append(
                _("students.import_form.error_duplicate_in_form", row=display_row, number=sn)
            )
            continue
        included = next(
            (
                link
                for link in exam.students
                if link.included and _student_number_key(link.student.student_number) == key
            ),
            None,
        )
        is_correction = (
            included
            and sn == included.student.student_number
            and (nm != included.student.name or cn != included.group_name)
        )
        if key in existing_keys and not is_correction:
            row["field_errors"]["student_number"] = True
            errors.append(
                _("students.import_form.error_duplicate_in_session", row=display_row, number=sn)
            )
            continue
        seen_keys[key] = index
        valid_rows.append(row)

    if errors:
        # Re-render the form with the submitted rows and field-level highlighting; do not
        # commit any partial data.
        return templates.TemplateResponse(
            request,
            "students/import.html",
            {
                "request": request,
                "active_nav": "sessions",
                "exam": exam,
                "submitted_rows": rows,
                "errors": errors,
            },
            status_code=400,
        )

    preview = validate_student_rows(
        [{key: row[key] for key in ("student_number", "name", "class_name")} for row in valid_rows],
        db,
        exam,
    )
    if preview.error_rows:
        return templates.TemplateResponse(
            request,
            "students/import.html",
            {
                "request": request,
                "active_nav": "sessions",
                "exam": exam,
                "submitted_rows": rows,
                "errors": [row.message for row in preview.error_rows],
            },
            status_code=400,
        )
    if any(row.status == "conflict" for row in preview.rows):
        return _preview_response(request, db, exam, preview)
    if preview.rows:
        import_students_from_payload(db, exam, import_preview_to_payload(preview), commit=False)
    db.flush()
    _sync_peer_review_roster(db, exam)
    db.commit()
    return RedirectResponse(f"/exams/{exam_id}/students", status_code=303)


@router.get("/exams/{exam_id}/students/import", response_class=HTMLResponse)
def student_import_form(
    request: Request,
    exam_id: int,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    exam = get_run_or_404(db, exam_id, for_write=True)
    return templates.TemplateResponse(
        request,
        "students/import.html",
        {"request": request, "active_nav": "sessions", "exam": exam},
    )


@router.post("/exams/{exam_id}/students/import/preview", response_class=HTMLResponse)
async def student_import_preview(
    request: Request,
    exam_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    ensure_uploads_allowed()
    exam = get_run_or_404(db, exam_id, for_write=True)
    try:
        preview = parse_student_import(file.filename or "", await file.read(), db, exam)
        payload = _store_preview(db, exam, preview)
        errors = []
    except ValueError as exc:
        preview = None
        payload = ""
        errors = [str(exc)]
    return templates.TemplateResponse(
        request,
        "students/import_preview.html",
        {
            "request": request,
            "active_nav": "sessions",
            "exam": exam,
            "preview": preview,
            "payload": payload,
            "errors": errors,
        },
    )


def _store_preview(db, exam, preview) -> str:
    from sqlalchemy import delete

    db.execute(
        delete(StudentImportBatch).where(
            StudentImportBatch.created_at < datetime.utcnow() - timedelta(hours=1)
        )
    )
    token = token_urlsafe(32)
    db.add(
        StudentImportBatch(token=token, exam_id=exam.id, payload=import_preview_to_payload(preview))
    )
    db.commit()
    return token


def _preview_response(request, db, exam, preview):
    return templates.TemplateResponse(
        request,
        "students/import_preview.html",
        {
            "request": request,
            "active_nav": "sessions",
            "exam": exam,
            "preview": preview,
            "payload": _store_preview(db, exam, preview),
            "errors": [],
        },
    )


@router.post("/exams/{exam_id}/students/import/confirm")
async def student_import_confirm(
    request: Request,
    exam_id: int,
    import_payload: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    ensure_uploads_allowed()
    exam = get_run_or_404(db, exam_id, for_write=True)
    batch = db.get(StudentImportBatch, import_payload)
    if (
        not batch
        or batch.exam_id != exam.id
        or batch.created_at < datetime.utcnow() - timedelta(hours=1)
    ):
        raise HTTPException(400, _("students.conflicts.invalid"))
    if batch.consumed:
        return RedirectResponse(f"/exams/{exam_id}/students", status_code=303)
    form = await request.form()
    choices = {}
    for key, value in form.multi_items():
        if key == "import_payload":
            continue
        parts = key.split("_")
        if len(parts) != 2 or parts[0] not in {"name", "group", "skip"}:
            raise HTTPException(400, _("students.conflicts.invalid"))
        decision = choices.setdefault(parts[1], {})
        if parts[0] in decision:
            raise HTTPException(400, _("students.conflicts.invalid"))
        decision[parts[0]] = value
    try:
        # Claim the preview in the same transaction as all roster changes.
        from sqlalchemy import update

        claimed = db.execute(
            update(StudentImportBatch)
            .where(StudentImportBatch.token == batch.token, StudentImportBatch.consumed.is_(False))
            .values(consumed=True)
        )
        if claimed.rowcount != 1:
            db.rollback()
            return RedirectResponse(f"/exams/{exam_id}/students", status_code=303)
        import_students_from_payload(db, exam, batch.payload, choices, commit=False)
        db.info["defer_roster_commit"] = True
        _sync_peer_review_roster(db, exam)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    finally:
        db.info.pop("defer_roster_commit", None)
    return RedirectResponse(f"/exams/{exam_id}/students", status_code=303)


@router.get("/exams/{exam_id}/students/import-template.xlsx")
def student_import_template(exam_id: int, db: Session = Depends(get_db)) -> StreamingResponse:
    if get_exam(db, exam_id) is None:
        raise HTTPException(status_code=404)
    content = build_student_template_xlsx()
    headers = {"Content-Disposition": 'attachment; filename="student_import_template.xlsx"'}
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


def _student_number_key(value: str) -> str:
    """Whitespace-insensitive comparison key for duplicate detection.

    Removes all surrounding/internal whitespace (including the full-width U+3000 space) so
    "2505 ma04", "2505　ma04" and "2505ma04" are recognised as the same number. Casing is
    left untouched on purpose: student numbers are not known to be case-insensitive, and
    forcing a case could merge genuinely distinct school IDs. This only affects how
    duplicates are *detected* — the number is always stored exactly as typed (stripped).
    """
    return "".join(value.split())


def _exam_student_link(db: Session, exam_id: int, student_id: int) -> ExamStudent | None:
    return db.scalar(
        select(ExamStudent)
        .where(ExamStudent.exam_id == exam_id)
        .where(ExamStudent.student_id == student_id)
    )


def _validate_required_student_fields(
    student_number: str,
    name: str,
    class_name: str,
) -> list[str]:
    errors = []
    if not student_number.strip():
        errors.append(_("students.conflicts.required_number"))
    if not name.strip():
        errors.append(_("students.conflicts.required_name"))
    if not class_name.strip():
        errors.append(_("students.conflicts.required_group"))
    return errors


def _validate_student_update(
    db: Session,
    student: Student,
    student_number: str,
    name: str,
    class_name: str,
) -> list[str]:
    errors = _validate_required_student_fields(student_number, name, class_name)
    duplicate = any(
        other.id != student.id
        and _student_number_key(other.student_number) == _student_number_key(student_number)
        for other in db.scalars(select(Student))
    )
    if duplicate:
        errors.append(_("students.conflicts.number_exists"))
    return errors


def _apply_student_fields(
    student: Student,
    student_number: str,
    name: str,
    class_name: str,
    active: bool,
) -> None:
    student.student_number = student_number.strip()
    student.name = name.strip()
    student.class_name = class_name.strip()
    student.active = active


def _student_form_response(
    request: Request,
    exam,
    student: Student | None,
    link: ExamStudent | None,
    mode: str,
    errors: list[str],
    status_code: int,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "students/edit.html",
        {
            "request": request,
            "active_nav": "sessions",
            "exam": exam,
            "student": student,
            "link": link,
            "errors": errors,
            "mode": mode,
        },
        status_code=status_code,
    )
