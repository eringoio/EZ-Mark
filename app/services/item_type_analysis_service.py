"""Item-type / question-category breakdown.

These breakdowns come from ``Question.item_type`` and are distinct from scoring
sections. Scoring uses ``Response.final_answer`` only; blank, invalid, unclear, and
multiple-mark responses count as incorrect unless a reviewer set a matching
``final_answer``. Questions with no ``item_type`` are grouped under ``unknown`` so
they are surfaced rather than silently dropped.

Categories are fully generic: whatever labels staff type in become the breakdown
keys (no hardcoded language-test categories). Questions are grouped in first-appearance
order, then ``unknown`` last.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MockExam, Response, ScannedPage, SheetInstance

UNKNOWN_ITEM_TYPE = "unknown"


def calculate_student_item_type_breakdown(db: Session, exam: MockExam, student_id: int) -> dict:
    """Return one entry per item type present in the exam for a single student.

    Each entry: ``{code, label, correct, total, accuracy, percentage}``. ``accuracy`` is a
    0-1 ratio (or ``None`` when ``total`` is 0); ``percentage`` is ``accuracy * 100`` rounded
    to one decimal. Categories appear in first-appearance order, then ``unknown``.
    """
    response_by_question = {
        response.question_id: response for response in _student_responses(db, exam.id, student_id)
    }

    buckets: dict[str, dict] = {}
    for question in exam.questions:
        code = question.item_type or UNKNOWN_ITEM_TYPE
        bucket = buckets.setdefault(
            code,
            {"code": code, "label": code, "correct": 0, "total": 0},
        )
        bucket["total"] += 1
        response = response_by_question.get(question.id)
        final_answer = response.final_answer if response else None
        if final_answer is not None and final_answer == question.correct_answer:
            bucket["correct"] += 1

    ordered: dict[str, dict] = {}
    for code in buckets:
        ordered[code] = _finalize(buckets[code])
    # Surface questions without a category last instead of mixed into the rest.
    if UNKNOWN_ITEM_TYPE in ordered:
        ordered[UNKNOWN_ITEM_TYPE] = ordered.pop(UNKNOWN_ITEM_TYPE)
    return ordered


def calculate_exam_item_type_breakdowns(db: Session, exam: MockExam) -> dict:
    """Return ``{student_id: breakdown}`` for every included student in the exam."""
    return {
        link.student_id: calculate_student_item_type_breakdown(db, exam, link.student_id)
        for link in exam.students
        if link.included
    }


def _finalize(bucket: dict) -> dict:
    total = bucket["total"]
    accuracy = bucket["correct"] / total if total else None
    bucket["accuracy"] = accuracy
    bucket["percentage"] = round(accuracy * 100, 1) if accuracy is not None else None
    return bucket


def _student_responses(db: Session, exam_id: int, student_id: int) -> list[Response]:
    return list(
        db.scalars(
            select(Response)
            .join(ScannedPage)
            .join(SheetInstance)
            .where(SheetInstance.exam_id == exam_id)
            .where(SheetInstance.student_id == student_id)
        )
    )
