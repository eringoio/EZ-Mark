from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    MockExam,
    PeerReviewRating,
    Question,
    Response,
    ScanBatch,
    ScannedPage,
    SheetInstance,
)
from app.services.peer_review_review_service import PROBLEM_STATUSES

# Problem statuses for normal OMR ``Response`` rows. Anonymous peer-review ratings use
# the canonical ``PROBLEM_STATUSES`` instead, so the unresolved definition stays in one
# place and cannot drift apart again.
REVIEWABLE_RESPONSE_STATUSES = {"blank", "multiple_mark", "unclear", "invalid"}


def count_reviewable(db: Session, exam_id: int) -> int:
    exam = db.get(MockExam, exam_id)
    if exam and exam.assessment_type == "peer_review":
        return db.scalar(
            select(func.count(PeerReviewRating.id))
            .where(PeerReviewRating.exam_id == exam_id)
            .where(PeerReviewRating.status.in_(PROBLEM_STATUSES))
        ) or 0
    return db.scalar(
        select(func.count(Response.id))
        .join(ScannedPage, Response.scanned_page_id == ScannedPage.id)
        .join(ScanBatch, ScannedPage.scan_batch_id == ScanBatch.id)
        .join(SheetInstance, ScannedPage.sheet_instance_id == SheetInstance.id)
        .join(Question, Response.question_id == Question.id)
        .where(ScanBatch.exam_id == exam_id)
        .where(ScannedPage.review_status.in_(("ok", "needs_review")))
        .where(Response.status.in_(REVIEWABLE_RESPONSE_STATUSES))
    ) or 0


def count_orphan_reviewable(db: Session, exam_id: int) -> int:
    return db.scalar(
        select(func.count(Response.id))
        .join(ScannedPage, Response.scanned_page_id == ScannedPage.id)
        .join(ScanBatch, ScannedPage.scan_batch_id == ScanBatch.id)
        .outerjoin(Question, Response.question_id == Question.id)
        .where(ScanBatch.exam_id == exam_id)
        .where(ScannedPage.review_status.in_(("ok", "needs_review")))
        .where(Response.status.in_(REVIEWABLE_RESPONSE_STATUSES))
        .where(Question.id.is_(None))
    ) or 0


def list_review_responses(db: Session, exam_id: int) -> list:
    exam = db.get(MockExam, exam_id)
    if exam and exam.assessment_type == "peer_review":
        return []
    responses = list(
        db.scalars(
            select(Response)
            .join(ScannedPage)
            .join(ScanBatch)
            .join(SheetInstance, ScannedPage.sheet_instance_id == SheetInstance.id)
            .join(Question, Response.question_id == Question.id)
            .where(ScanBatch.exam_id == exam_id)
            .where(ScannedPage.review_status.in_(("ok", "needs_review")))
            .where(Response.status.in_(REVIEWABLE_RESPONSE_STATUSES))
            .order_by(ScannedPage.page_no, Response.id)
        )
    )
    return sorted(responses, key=queue_sort_key)


def queue_sort_key(response: Response) -> tuple:
    """Queue order: student -> physical sheet -> problem group -> question."""
    page = response.scanned_page
    sheet_instance = page.sheet_instance if page else None
    student_number = sheet_instance.printed_student_number if sheet_instance else ""
    physical_sheet = sheet_instance.physical_sheet if sheet_instance else None
    sheet_order = (physical_sheet.display_order, physical_sheet.sheet_no) if physical_sheet else (9999, 9999)
    question = response.question
    problem_no = question.problem_no if question and question.problem_no is not None else 9999
    sub_no = 0
    if question:
        sub_no = (
            question.subquestion_no
            if question.subquestion_no is not None
            else question.local_question_no or question.question_no or question.display_order or 0
        )
    return (student_number, *sheet_order, problem_no, sub_no, response.id)


def next_review_response(db: Session, exam_id: int) -> any:
    responses = list_review_responses(db, exam_id)
    return responses[0] if responses else None


def next_response_id_after(db: Session, exam_id: int, resolved) -> int | None:
    """Queue item to select after ``resolved`` was saved (already excluded)."""
    responses = list_review_responses(db, exam_id)
    if not responses:
        return None
    resolved_key = queue_sort_key(resolved)
    for response in responses:
        if queue_sort_key(response) > resolved_key:
            return response.id
    return responses[0].id


def correct_response(db: Session, response_id: int, final_answer: str | None) -> Response:
    response = db.get(Response, response_id)
    if response is None:
        raise ValueError("Response not found.")
    if response.question is None:
        raise ValueError("Response is not linked to question metadata.")
    if final_answer is not None:
        valid_answers = {str(number) for number in range(1, response.question.choice_count + 1)}
        if final_answer not in valid_answers:
            raise ValueError("Final answer must be within the choice range.")
    response.final_answer = final_answer
    response.status = "reviewed"
    response.reviewed_by = "staff"
    response.reviewed_at = datetime.utcnow()
    response.is_correct = (
        final_answer == response.question.correct_answer
        if final_answer and response.question.correct_answer
        else False
    )
    db.commit()
    db.refresh(response)
    return response


def mark_response_invalid(db: Session, response_id: int) -> Response:
    response = db.get(Response, response_id)
    if response is None:
        raise ValueError("Response not found.")
    response.final_answer = None
    response.status = "reviewed"
    response.reviewed_by = "staff"
    response.reviewed_at = datetime.utcnow()
    response.is_correct = False
    db.commit()
    db.refresh(response)
    return response


def count_remaining_page_responses(db: Session, scanned_page_id: int) -> int:
    return db.scalar(
        select(func.count(Response.id))
        .where(Response.scanned_page_id == scanned_page_id)
        .where(Response.status.in_(REVIEWABLE_RESPONSE_STATUSES))
    ) or 0


def resolve_remaining_page_responses(
    db: Session,
    scanned_page_id: int,
    action: Literal["blank", "invalid"],
    reviewed_by: str = "staff",
) -> int:
    if action not in {"blank", "invalid"}:
        raise ValueError("Unsupported bulk review action.")
    page = db.get(ScannedPage, scanned_page_id)
    if page is None:
        raise ValueError("Scanned page not found.")

    responses = list(
        db.scalars(
            select(Response)
            .where(Response.scanned_page_id == scanned_page_id)
            .where(Response.status.in_(REVIEWABLE_RESPONSE_STATUSES))
        )
    )
    reviewed_at = datetime.utcnow()
    for response in responses:
        response.final_answer = None
        response.status = "reviewed"
        response.reviewed_by = reviewed_by
        response.reviewed_at = reviewed_at
        response.is_correct = False

    db.flush()
    remaining = count_remaining_page_responses(db, scanned_page_id)
    page.review_status = "needs_review" if remaining else "ok"
    if page.scan_batch is not None:
        batch = page.scan_batch
        batch.problem_pages = sum(
            1
            for batch_page in batch.scanned_pages
            if batch_page.review_status == "needs_review"
        )
        batch.status = "needs_review" if batch.problem_pages else "processed"
    db.commit()
    return len(responses)

