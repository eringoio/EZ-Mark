import logging
import os
import traceback

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import (
    MockExam,
    Question,
    Response,
    ScanBatch,
    SheetInstance,
    SheetTemplate,
)
from app.models.scan import ScannedPage
from app.services.coordinate_question_service import (
    BROKEN_RESPONSE_MESSAGE,
    coordinate_entries_matching_question,
    coordinate_map_check,
    orphan_review_problem_rows,
    repair_orphan_responses_for_exam,
)
from app.services.exam_service import get_run_or_404
from app.services.review_service import (
    correct_response,
    count_remaining_page_responses,
    list_review_responses,
    mark_response_invalid,
    next_response_id_after,
    next_review_response,
    resolve_remaining_page_responses,
)
from app.services.scoring_service import (
    maybe_finalize_exam,
    problem_group_titles,
    question_group_label,
    question_short_label,
)
from app.services.templates import templates

router = APIRouter()
logger = logging.getLogger(__name__)
REVIEWABLE_RESPONSE_STATUSES = {"blank", "multiple_mark", "unclear", "invalid"}


def _after_review_save(db: Session, exam_id: int, next_response_id: int | None) -> RedirectResponse:
    exam = get_run_or_404(db, exam_id)
    maybe_finalize_exam(db, exam)
    if next_response_id is not None:
        return RedirectResponse(
            f"/exams/{exam_id}/review?response_id={next_response_id}", status_code=303
        )
    return RedirectResponse(f"/exams/{exam_id}/review", status_code=303)


@router.get("/exams/{exam_id}/review", response_class=HTMLResponse)
def review_queue(
    request: Request,
    exam_id: int,
    response_id: int | None = None,
    scanned_page_id: int | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    logger.info(
        "Review route started",
        extra={
            "review_route": {
                "exam_id": exam_id,
                "response_id": response_id,
                "scanned_page_id": scanned_page_id,
            }
        },
    )
    try:
        exam = get_run_or_404(db, exam_id, for_write=True)
        if exam.assessment_type == "peer_review":
            return RedirectResponse(f"/exams/{exam.id}/peer-review/review", status_code=303)

        if scanned_page_id is not None:
            return _render_review_page_view(request, db, exam, scanned_page_id)

        repair_payload = repair_orphan_responses_for_exam(db, exam_id)
        if repair_payload["repaired"]:
            db.commit()
        responses = list_review_responses(db, exam_id)
        current = next((r for r in responses if r.id == response_id), None) if response_id else None
        if current is None:
            current = responses[0] if responses else None
        page_response_map, _overlay_debug = _build_page_response_map_with_debug(
            db, responses, current
        )
        queue_groups = _build_queue_groups(exam, responses)
        prev_response_id, next_response_id = _prev_next_response_ids(responses, current)
        stale_pages = (
            _needs_review_pages_without_reviewable_responses(db, exam_id) if not responses else []
        )
        orphan_review_items = orphan_review_problem_rows(db, exam_id) if not responses else []
        display_page = current.scanned_page if current and current.scanned_page else None
        bulk_page_remaining_count = (
            count_remaining_page_responses(db, display_page.id) if display_page else 0
        )
        logger.info(
            "Review route data selected",
            extra={
                "review_route": {
                    "exam_id": exam_id,
                    "reviewable_response_count": len(responses),
                    "selected_response_id": current.id if current else None,
                    "selected_page_id": display_page.id if display_page else None,
                    "image_path": display_page.image_path if display_page else None,
                    "aligned_image_path": display_page.aligned_image_path if display_page else None,
                    "overlay_count": len(page_response_map),
                }
            },
        )
        return templates.TemplateResponse(
            request,
            "review/index.html",
            _review_template_context(
                request,
                exam,
                responses=responses,
                current=current,
                page_response_map=page_response_map,
                queue_groups=queue_groups,
                prev_response_id=prev_response_id,
                next_response_id=next_response_id,
                stale_needs_review_pages=stale_pages,
                orphan_review_items=orphan_review_items,
                bulk_page_remaining_count=bulk_page_remaining_count,
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "Review route failed",
            extra={"review_route": {"exam_id": exam_id, "scanned_page_id": scanned_page_id}},
        )
        exam = db.get(MockExam, exam_id)
        if exam is None:
            raise
        return templates.TemplateResponse(
            request,
            "review/index.html",
            _review_template_context(
                request,
                exam,
                error=f"Review page could not load: {exc}",
            ),
            status_code=500,
        )


@router.get("/debug/review/exam/{exam_id}.json")
def review_exam_debug(exam_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    _assert_scan_debug_allowed(request)
    exam = db.get(MockExam, exam_id)
    if exam is None:
        raise HTTPException(status_code=404)
    pages = list(
        db.scalars(select(ScannedPage).join(ScanBatch).where(ScanBatch.exam_id == exam_id)).unique()
    )
    pages_by_review_status: dict[str, int] = {}
    aggregate_counts = _empty_response_status_counts()
    needs_review_pages = []
    orphan_rows = _orphan_response_debug_rows(db, exam_id)
    for page in pages:
        pages_by_review_status[page.review_status] = (
            pages_by_review_status.get(page.review_status, 0) + 1
        )
        counts = _response_status_counts(page.responses)
        for status, count in counts.items():
            aggregate_counts[status] = aggregate_counts.get(status, 0) + count
        if page.review_status == "needs_review":
            needs_review_pages.append(_review_page_debug_row(page, counts))
    return {
        "exam_id": exam_id,
        "total_scanned_pages": len(pages),
        "pages_by_review_status": pages_by_review_status,
        "needs_review_page_count": len(needs_review_pages),
        "response_counts_by_status": aggregate_counts,
        "reviewable_response_count": _reviewable_response_count(aggregate_counts),
        "orphan_response_count": len(orphan_rows),
        "orphan_reviewable_response_count": sum(
            1 for row in orphan_rows if row["status"] in REVIEWABLE_RESPONSE_STATUSES
        ),
        "orphan_response_ids": [row["response_id"] for row in orphan_rows],
        "orphan_responses": orphan_rows,
        "pages_marked_needs_review": needs_review_pages,
    }


@router.get("/debug/review/exam/{exam_id}/route-check.json")
def review_route_check(exam_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    _assert_scan_debug_allowed(request)
    payload = {
        "route_path": f"/exams/{exam_id}/review",
        "exam_exists": False,
        "reviewable_responses_query_succeeds": False,
        "reviewable_response_count": 0,
        "first_reviewable_response_id": None,
        "overlay_data_can_be_built": False,
        "image_paths_exist": None,
        "template_coordinate_map_exists": None,
        "orphan_response_count": 0,
        "orphan_reviewable_response_count": 0,
        "orphan_response_ids": [],
        "orphan_responses": [],
        "exception": None,
        "traceback": None,
    }
    try:
        exam = db.get(MockExam, exam_id)
        payload["exam_exists"] = exam is not None
        if exam is None:
            return payload
        responses = list_review_responses(db, exam_id)
        payload["reviewable_responses_query_succeeds"] = True
        payload["reviewable_response_count"] = len(responses)
        orphan_rows = _orphan_response_debug_rows(db, exam_id)
        payload["orphan_response_count"] = len(orphan_rows)
        payload["orphan_reviewable_response_count"] = sum(
            1 for row in orphan_rows if row["status"] in REVIEWABLE_RESPONSE_STATUSES
        )
        payload["orphan_response_ids"] = [row["response_id"] for row in orphan_rows]
        payload["orphan_responses"] = orphan_rows
        first = responses[0] if responses else None
        payload["first_reviewable_response_id"] = first.id if first else None
        response_map, overlay_debug = _build_page_response_map_with_debug(db, responses, first)
        payload["overlay_data_can_be_built"] = bool(response_map)
        payload.update(overlay_debug)
        page = first.scanned_page if first and first.scanned_page else None
        if page:
            data_root = get_settings().data_root
            image_path = data_root / page.image_path
            aligned_path = data_root / page.aligned_image_path if page.aligned_image_path else None
            payload["image_paths_exist"] = {
                "original": image_path.exists(),
                "aligned": aligned_path.exists() if aligned_path else None,
            }
            template = page.sheet_instance.sheet_template if page.sheet_instance else None
            payload["template_coordinate_map_exists"] = bool(
                template and template.coordinate_map_json
            )
    except Exception as exc:
        payload["exception"] = str(exc)
        payload["traceback"] = traceback.format_exc(limit=8)
    return payload


@router.post("/debug/review/exam/{exam_id}/repair-orphan-responses")
def repair_orphan_responses_debug(
    exam_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    _assert_scan_debug_allowed(request)
    get_run_or_404(db, exam_id, for_write=True)
    before = _orphan_response_debug_rows(db, exam_id)
    result = repair_orphan_responses_for_exam(db, exam_id)
    db.commit()
    after = _orphan_response_debug_rows(db, exam_id)
    return {"exam_id": exam_id, "before": before, "repair": result, "after": after}


@router.get("/debug/templates/{template_id}/coordinate-map-check.json")
def template_coordinate_map_check_debug(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    _assert_scan_debug_allowed(request)
    template = db.get(SheetTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404)
    return coordinate_map_check(db, template)


@router.get("/exams/{exam_id}/review/next", response_class=HTMLResponse)
def review_next(request: Request, exam_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    exam = get_run_or_404(db, exam_id, for_write=True)
    if exam.assessment_type == "peer_review":
        return RedirectResponse(f"/exams/{exam.id}/peer-review/review", status_code=303)
    return templates.TemplateResponse(
        request,
        "review/current.html",
        {
            "request": request,
            "exam": exam,
            "current": next_review_response(db, exam_id),
            "error": None,
        },
    )


def _render_review_page_view(
    request: Request,
    db: Session,
    exam: MockExam,
    scanned_page_id: int,
) -> HTMLResponse:
    view_page = db.get(ScannedPage, scanned_page_id)
    if view_page is None:
        raise HTTPException(status_code=404)
    page_response_map, _debug = _build_page_response_map_with_debug(
        db,
        list(view_page.responses),
        None,
        view_page=view_page,
    )
    logger.info(
        "Review route page view selected",
        extra={
            "review_route": {
                "exam_id": exam.id,
                "selected_page_id": view_page.id,
                "image_path": view_page.image_path,
                "aligned_image_path": view_page.aligned_image_path,
                "overlay_count": len(page_response_map),
            }
        },
    )
    return templates.TemplateResponse(
        request,
        "review/index.html",
        _review_template_context(
            request,
            exam,
            page_response_map=page_response_map,
            view_page=view_page,
        ),
    )


def _review_template_context(
    request: Request,
    exam: MockExam,
    responses: list | None = None,
    current=None,
    page_response_map: dict | None = None,
    queue_groups: list[dict] | None = None,
    prev_response_id: int | None = None,
    next_response_id: int | None = None,
    view_page: ScannedPage | None = None,
    stale_needs_review_pages: list[ScannedPage] | None = None,
    orphan_review_items: list[dict] | None = None,
    error: str | None = None,
    bulk_page_remaining_count: int = 0,
) -> dict:
    return {
        "request": request,
        "active_nav": "scan_review",
        "exam": exam,
        "responses": responses or [],
        "current": current,
        "page_response_map": page_response_map or {},
        "queue_groups": queue_groups or [],
        "prev_response_id": prev_response_id,
        "next_response_id": next_response_id,
        "view_page": view_page,
        "stale_needs_review_pages": stale_needs_review_pages or [],
        "orphan_review_items": orphan_review_items or [],
        "broken_response_message": BROKEN_RESPONSE_MESSAGE,
        "error": error,
        "bulk_page_remaining_count": bulk_page_remaining_count,
    }


def _build_page_response_map(responses: list, current) -> dict:
    page_response_map, _debug = _build_page_response_map_with_debug(None, responses, current)
    return page_response_map


def _build_page_response_map_with_debug(
    db: Session | None,
    responses: list,
    current,
    view_page: ScannedPage | None = None,
) -> tuple[dict, dict]:
    page_response_map = {}
    debug = _empty_overlay_debug(current)
    display_page = view_page or (current.scanned_page if current and current.scanned_page else None)
    if display_page is None:
        return page_response_map, debug
    page_id = display_page.id
    sheet_instance = display_page.sheet_instance
    template = sheet_instance.sheet_template if sheet_instance else None
    coordinate_map = template.coordinate_map_json if template else {}
    check = coordinate_map_check(db, template) if db is not None and template is not None else None
    if check:
        debug["duplicate_coordinate_question_ids"] = check["duplicate_question_ids"]
        debug["duplicate_coordinate_keys"] = check["duplicate_coordinate_keys"]
    for response in responses:
        if (
            getattr(response, "scanned_page", None) is not None
            and response.scanned_page.id == page_id
            and getattr(response, "question", None) is not None
        ):
            matches = (
                coordinate_entries_matching_question(
                    db, sheet_instance, response.question, coordinate_map or {}
                )
                if db is not None and sheet_instance is not None
                else []
            )
            if response.id == getattr(current, "id", None):
                debug["coordinate_entries_matching_current_response"] = [
                    _coordinate_debug_entry(entry) for entry in matches
                ]
            if not matches:
                logger.warning(
                    "Review overlay response has no coordinate entry match",
                    extra={
                        "review_overlay": {
                            "response_id": response.id,
                            "question_id": response.question.id,
                            "scanned_page_id": page_id,
                        }
                    },
                )
                continue
            if len(matches) > 1:
                logger.warning(
                    "Review overlay response has ambiguous coordinate entry matches",
                    extra={
                        "review_overlay": {
                            "response_id": response.id,
                            "question_id": response.question.id,
                            "scanned_page_id": page_id,
                            "matching_coordinate_keys": [
                                entry["coordinate_key"] for entry in matches
                            ],
                        }
                    },
                )
            selected = matches[0]
            if response.id == getattr(current, "id", None):
                debug["selected_coordinate_entry_key"] = selected["coordinate_key"]
            page_response_map[selected["coordinate_key"]] = {
                "response_id": response.id,
                "detected": response.detected_answer or "",
                "correct": response.question.correct_answer or "",
                "is_current": response.id == getattr(current, "id", None),
                "coordinate_key": selected["coordinate_key"],
            }
    return page_response_map, debug


def _empty_overlay_debug(current) -> dict:
    question = current.question if current and getattr(current, "question", None) else None
    return {
        "current_response_id": current.id if current else None,
        "current_question_id": question.id if question else None,
        "current_question_label": question.display_label if question else None,
        "current_problem_no": question.problem_no if question else None,
        "coordinate_entries_matching_current_response": [],
        "duplicate_coordinate_question_ids": [],
        "duplicate_coordinate_keys": [],
        "selected_coordinate_entry_key": None,
    }


def _coordinate_debug_entry(entry: dict) -> dict:
    return {
        "index": entry["coordinate_index"],
        "question_id": entry.get("question_id"),
        "display_label": entry.get("display_label"),
        "group_label": entry.get("group_label"),
        "choice_count": len(entry.get("choices") or []),
        "coordinate_key": entry["coordinate_key"],
    }


def _build_queue_groups(exam: MockExam, responses: list) -> list[dict]:
    queue_groups: list[dict] = []
    group_titles = problem_group_titles(exam)
    for response in responses:
        sheet_instance = response.scanned_page.sheet_instance if response.scanned_page else None
        header_parts = [
            sheet_instance.printed_student_number if sheet_instance else "—",
            sheet_instance.physical_sheet.sheet_name
            if sheet_instance and sheet_instance.physical_sheet
            else "—",
        ]
        group_label = (
            question_group_label(response.question, group_titles) if response.question else ""
        )
        if group_label:
            header_parts.append(group_label)
        header = " · ".join(header_parts)
        if not queue_groups or queue_groups[-1]["header"] != header:
            queue_groups.append({"header": header, "items": []})
        queue_groups[-1]["items"].append(
            {
                "response": response,
                "q_label": question_short_label(response.question) if response.question else "—",
            }
        )
    return queue_groups


def _prev_next_response_ids(responses: list, current) -> tuple[int | None, int | None]:
    if current is None or len(responses) <= 1:
        return None, None
    ids = [response.id for response in responses]
    index = ids.index(current.id)
    return ids[index - 1], ids[(index + 1) % len(ids)]


def _needs_review_pages_without_reviewable_responses(
    db: Session, exam_id: int
) -> list[ScannedPage]:
    pages = list(
        db.scalars(
            select(ScannedPage)
            .join(ScanBatch)
            .where(ScanBatch.exam_id == exam_id)
            .where(ScannedPage.review_status == "needs_review")
        ).unique()
    )
    return [
        page
        for page in pages
        if not any(
            response.status in REVIEWABLE_RESPONSE_STATUSES and response.question is not None
            for response in page.responses
        )
    ]


def _assert_scan_debug_allowed(request: Request) -> None:
    if not _scan_debug_enabled():
        raise HTTPException(status_code=404)
    client_host = request.client.host if request.client else ""
    host_header = request.headers.get("host", "").split(":", 1)[0]
    allowed_hosts = {"127.0.0.1", "::1", "localhost"}
    if client_host not in allowed_hosts and host_header not in allowed_hosts:
        raise HTTPException(status_code=404)


def _scan_debug_enabled() -> bool:
    env_value = os.getenv("ENABLE_SCAN_DEBUG")
    if env_value is not None:
        return env_value == "1"
    return get_settings().enable_scan_debug


def _empty_response_status_counts() -> dict:
    return {
        status: 0 for status in ["ok", "blank", "multiple_mark", "unclear", "invalid", "reviewed"]
    }


def _response_status_counts(responses) -> dict:
    counts = _empty_response_status_counts()
    for response in responses:
        counts[response.status] = counts.get(response.status, 0) + 1
    return counts


def _reviewable_response_count(status_counts: dict) -> int:
    return sum(status_counts.get(status, 0) for status in REVIEWABLE_RESPONSE_STATUSES)


def _review_page_debug_row(page: ScannedPage, status_counts: dict) -> dict:
    sheet_instance = page.sheet_instance
    student = sheet_instance.student if sheet_instance else None
    physical_sheet = sheet_instance.physical_sheet if sheet_instance else None
    template = sheet_instance.sheet_template if sheet_instance else None
    data_root = get_settings().data_root
    image_path = data_root / page.image_path
    aligned_path = data_root / page.aligned_image_path if page.aligned_image_path else None
    reviewable_count = _reviewable_response_count(status_counts)
    diagnosis = "Page has responses that require review."
    if page.review_status == "needs_review" and len(page.responses) > 0 and reviewable_count == 0:
        diagnosis = "Page is marked needs_review, but no individual responses require review."
    elif page.review_status == "needs_review" and len(page.responses) == 0:
        diagnosis = "Page is marked needs_review because no responses were created."
    if aligned_path and not aligned_path.exists():
        diagnosis = "Aligned image is missing."
    if page.qr_status == "manual":
        diagnosis = "Page was manually mapped because QR did not match automatically."
    return {
        "scanned_page_id": page.id,
        "student_number": student.student_number if student else None,
        "student_name": student.name if student else None,
        "physical_sheet_name": physical_sheet.sheet_name if physical_sheet else None,
        "sheet_instance_id": page.sheet_instance_id,
        "template_id": template.id if template else None,
        "response_count": len(page.responses),
        "response_status_counts": status_counts,
        "reviewable_response_count": reviewable_count,
        "alignment_status": page.alignment_status,
        "qr_status": page.qr_status,
        "image_exists": image_path.exists(),
        "aligned_image_exists": aligned_path.exists() if aligned_path else None,
        "diagnosis": diagnosis,
    }


def _orphan_response_debug_rows(db: Session, exam_id: int) -> list[dict]:
    rows = db.execute(
        select(
            Response.id,
            Response.scanned_page_id,
            Response.question_id,
            Response.status,
            ScannedPage.sheet_instance_id,
            Question.id,
            SheetInstance.id,
        )
        .join(ScannedPage, Response.scanned_page_id == ScannedPage.id)
        .join(ScanBatch, ScannedPage.scan_batch_id == ScanBatch.id)
        .outerjoin(Question, Response.question_id == Question.id)
        .outerjoin(SheetInstance, ScannedPage.sheet_instance_id == SheetInstance.id)
        .where(ScanBatch.exam_id == exam_id)
        .where(
            (ScannedPage.sheet_instance_id.is_(None))
            | (SheetInstance.id.is_(None))
            | (Question.id.is_(None))
        )
        .order_by(Response.id)
    ).all()
    return [
        {
            "response_id": response_id,
            "scanned_page_id": scanned_page_id,
            "question_id": question_id,
            "question_exists": question_exists is not None,
            "sheet_instance_id": sheet_instance_id,
            "sheet_instance_exists": sheet_instance_exists is not None,
            "status": status,
        }
        for (
            response_id,
            scanned_page_id,
            question_id,
            status,
            sheet_instance_id,
            question_exists,
            sheet_instance_exists,
        ) in rows
    ]


@router.post("/responses/{response_id}/correct")
def correct_response_route(
    response_id: int,
    final_answer: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    existing = db.get(Response, response_id)
    if existing is None or existing.scanned_page is None:
        raise HTTPException(status_code=404)
    get_run_or_404(db, existing.scanned_page.scan_batch.exam_id, for_write=True)
    try:
        response = correct_response(db, response_id, final_answer)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    exam_id = response.scanned_page.scan_batch.exam_id
    next_id = next_response_id_after(db, exam_id, response)
    return _after_review_save(db, exam_id, next_id)


@router.post("/responses/{response_id}/mark-blank")
def mark_blank_route(response_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    existing = db.get(Response, response_id)
    if existing is None or existing.scanned_page is None:
        raise HTTPException(status_code=404)
    get_run_or_404(db, existing.scanned_page.scan_batch.exam_id, for_write=True)
    try:
        response = correct_response(db, response_id, None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    exam_id = response.scanned_page.scan_batch.exam_id
    next_id = next_response_id_after(db, exam_id, response)
    return _after_review_save(db, exam_id, next_id)


@router.post("/responses/{response_id}/mark-invalid")
def mark_invalid_route(response_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    existing = db.get(Response, response_id)
    if existing is None or existing.scanned_page is None:
        raise HTTPException(status_code=404)
    get_run_or_404(db, existing.scanned_page.scan_batch.exam_id, for_write=True)
    try:
        response = mark_response_invalid(db, response_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    exam_id = response.scanned_page.scan_batch.exam_id
    next_id = next_response_id_after(db, exam_id, response)
    return _after_review_save(db, exam_id, next_id)


@router.post("/scanned-pages/{scanned_page_id}/mark-remaining-blank")
def mark_remaining_page_blank_route(
    scanned_page_id: int, db: Session = Depends(get_db)
) -> RedirectResponse:
    page = db.get(ScannedPage, scanned_page_id)
    if page is None or page.scan_batch is None:
        raise HTTPException(status_code=404)
    exam = get_run_or_404(db, page.scan_batch.exam_id, for_write=True)
    if exam.assessment_type == "peer_review":
        raise HTTPException(status_code=400)
    resolve_remaining_page_responses(db, scanned_page_id, "blank")
    maybe_finalize_exam(db, exam)
    return RedirectResponse(f"/exams/{exam.id}/review", status_code=303)


@router.post("/scanned-pages/{scanned_page_id}/mark-remaining-invalid")
def mark_remaining_page_invalid_route(
    scanned_page_id: int, db: Session = Depends(get_db)
) -> RedirectResponse:
    page = db.get(ScannedPage, scanned_page_id)
    if page is None or page.scan_batch is None:
        raise HTTPException(status_code=404)
    exam = get_run_or_404(db, page.scan_batch.exam_id, for_write=True)
    if exam.assessment_type == "peer_review":
        raise HTTPException(status_code=400)
    resolve_remaining_page_responses(db, scanned_page_id, "invalid")
    maybe_finalize_exam(db, exam)
    return RedirectResponse(f"/exams/{exam.id}/review", status_code=303)
