from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    MockExam,
    PeerReviewRating,
    Response,
    ScannedPage,
    SheetInstance,
    StudentResult,
)
from app.services.review_service import REVIEWABLE_RESPONSE_STATUSES, count_reviewable


def problem_group_titles(exam: MockExam) -> dict[tuple[int | None, int], str]:
    """Lookup of (physical_sheet_id, problem_no) -> human title, e.g. もんだい1."""
    return {
        (group.physical_sheet_id, group.problem_no): group.problem_title
        or f"もんだい{group.problem_no}"
        for group in exam.problem_groups
    }


def question_group_label(question, group_titles: dict | None = None) -> str:
    if question.problem_no is not None:
        if group_titles:
            title = group_titles.get((question.physical_sheet_id, question.problem_no))
            if title:
                return title
        return f"もんだい{question.problem_no}"
    if question.test_section:
        return question.test_section
    return ""


def question_short_label(question) -> str:
    if getattr(question, "local_question_label", None):
        return f"問{question.local_question_label}"
    number = (
        question.subquestion_no
        if question.subquestion_no is not None
        else question.local_question_no
    )
    if number is not None:
        return f"問{number}"
    return str(question.display_label or question.question_no or question.id)


def format_score(value: float | int | None) -> str:
    """Display formatting for point totals: "7" when whole, "7.5" when fractional.

    Never alters stored values; purely for presentation.
    """
    if value is None:
        return ""
    return f"{value:g}" if isinstance(value, float) else str(value)


@dataclass(frozen=True)
class ClassResultSummary:
    student_count: int
    pass_count: int
    fail_count: int
    incomplete_count: int
    not_scored_count: int
    average_total: float | None


def maybe_finalize_exam(db: Session, exam: MockExam) -> bool:
    """Advance the exam status after scans or review changes.

    When at least one scanned page is matched to a sheet and no review
    problems remain, results are recalculated automatically and the exam
    becomes ``results_ready``. Otherwise the exam is marked ``needs_review``.
    Returns True when results were recalculated.
    """
    has_matched_page = any(
        page.sheet_instance_id is not None
        for batch in exam.scan_batches
        for page in batch.scanned_pages
    )
    if exam.assessment_type == "peer_review" and not has_matched_page:
        has_matched_page = bool(
            db.scalar(select(PeerReviewRating.id).where(PeerReviewRating.exam_id == exam.id))
        )
    if not has_matched_page:
        return False

    if count_reviewable(db, exam.id) == 0:
        recalculate_exam_results(db, exam)
        exam.status = "results_ready"
        db.commit()
        return True

    exam.status = "needs_review"
    db.commit()
    return False


def recalculate_exam_results(db: Session, exam: MockExam) -> list:
    if exam.assessment_type == "peer_review":
        from app.services.peer_review_builder_service import get_or_create_form
        from app.services.peer_review_scoring_service import (
            recalculate_anonymous_peer_review_results,
        )

        form = get_or_create_form(db, exam)
        return recalculate_anonymous_peer_review_results(db, form)

    results = []
    for link in exam.students:
        if link.included:
            results.append(_calculate_student_result(db, exam, link.student_id))
    db.commit()
    return results


def summarize_results(results: list[StudentResult]) -> ClassResultSummary:
    scored_totals = [result.total_score for result in results if result.total_score is not None]
    return ClassResultSummary(
        student_count=len(results),
        pass_count=_count_status(results, "pass"),
        fail_count=_count_status(results, "fail"),
        incomplete_count=_count_status(results, "incomplete"),
        not_scored_count=_count_status(results, "not_scored"),
        average_total=sum(scored_totals) / len(scored_totals) if scored_totals else None,
    )


def question_analysis(db: Session, exam: MockExam) -> list[dict]:
    group_titles = problem_group_titles(exam)
    rows = []
    for question in sorted(exam.questions, key=_question_analysis_sort_key):
        responses = list(
            db.scalars(
                select(Response)
                .join(ScannedPage)
                .where(Response.question_id == question.id)
                .where(ScannedPage.review_status.in_(("ok", "needs_review")))
            )
        )
        answered = [response for response in responses if response.final_answer]
        correct = [
            response for response in responses if response.final_answer == question.correct_answer
        ]
        blanks = [response for response in responses if response.status == "blank"]
        multiples = [response for response in responses if response.status == "multiple_mark"]
        student_count = len(responses)
        incorrect_count = student_count - len(correct)
        correct_rate = len(correct) / student_count if student_count else None
        blank_rate = len(blanks) / student_count if student_count else 0
        multiple_rate = len(multiples) / student_count if student_count else 0
        priority_score = (
            incorrect_count * (incorrect_count / student_count)
            + len(blanks) * 0.5
            + len(multiples) * 0.75
            if student_count
            else None
        )
        rows.append(
            {
                "question": question,
                "sheet": question.physical_sheet,
                "group_label": question_group_label(question, group_titles),
                "q_label": question_short_label(question),
                "student_count": student_count,
                "answered_count": len(answered),
                "correct_count": len(correct),
                "correct_rate": correct_rate,
                "incorrect_count": incorrect_count,
                "incorrect_rate": incorrect_count / student_count if student_count else None,
                "blank_count": len(blanks),
                "blank_rate": blank_rate,
                "multiple_mark_count": len(multiples),
                "multiple_mark_rate": multiple_rate,
                "priority_score": priority_score,
                "priority_band": _priority_band(correct_rate),
                "priority_reason_key": _priority_reason_key(
                    correct_rate, blank_rate, multiple_rate
                ),
            }
        )
    return rows


def _priority_band(correct_rate: float | None) -> str:
    if correct_rate is None:
        return "none"
    if correct_rate < 0.40:
        return "high"
    if correct_rate < 0.60:
        return "medium"
    return "low"


def _priority_reason_key(
    correct_rate: float | None,
    blank_rate: float,
    multiple_rate: float,
) -> str:
    if correct_rate is None:
        return "no_data"
    if blank_rate >= 0.20:
        return "many_blanks"
    if multiple_rate >= 0.10:
        return "many_multiple_marks"
    if correct_rate < 0.40:
        return "low_correct_rate"
    if correct_rate < 0.60:
        return "worth_reviewing"
    return "mostly_ok"


def _question_analysis_sort_key(question) -> tuple:
    sheet = question.physical_sheet
    return (
        sheet.display_order if sheet else 9999,
        sheet.sheet_no if sheet else 9999,
        question.problem_no if question.problem_no is not None else 9999,
        question.subquestion_no
        if question.subquestion_no is not None
        else question.local_question_no or question.question_no or question.display_order or 0,
    )


def _calculate_student_result(db: Session, exam: MockExam, student_id: int) -> StudentResult:
    existing = db.scalar(
        select(StudentResult).where(
            StudentResult.exam_id == exam.id,
            StudentResult.student_id == student_id,
        )
    )
    result = existing or StudentResult(exam_id=exam.id, student_id=student_id)
    question_sheet_ids = {
        question.physical_sheet_id
        for question in exam.questions
        if question.physical_sheet_id is not None
    }
    required_sheet_ids = {
        sheet.id
        for sheet in exam.physical_sheets
        if sheet.required and sheet.id in question_sheet_ids
    }
    scanned_sheet_ids = set(
        db.scalars(
            select(SheetInstance.physical_sheet_id)
            .join(ScannedPage)
            .where(SheetInstance.exam_id == exam.id)
            .where(SheetInstance.student_id == student_id)
        )
    )
    missing_required_sheet = not required_sheet_ids.issubset(scanned_sheet_ids)
    responses = _student_responses(db, exam.id, student_id)
    has_unreviewed = any(response.status in REVIEWABLE_RESPONSE_STATUSES for response in responses)

    # Generic raw scoring: raw points and max points.
    response_by_question = {response.question_id: response for response in responses}
    total_correct_points = 0.0
    total_max_points = 0.0

    # Section-based generic scoring if sections exist
    raw = {}
    if exam.scoring_sections:
        raw = {
            section.code: {"correct": 0.0, "total": 0.0, "accuracy": None}
            for section in exam.scoring_sections
        }
        for question in exam.questions:
            response = response_by_question.get(question.id)
            final_answer = response.final_answer if response else None
            is_correct = final_answer == question.correct_answer if final_answer else False
            if response is not None:
                response.is_correct = is_correct

            total_max_points += question.points
            if is_correct:
                total_correct_points += question.points

            if question.scoring_section:
                sec_raw = raw[question.scoring_section.code]
                sec_raw["total"] += question.points
                if is_correct:
                    sec_raw["correct"] += question.points

        for sec_raw in raw.values():
            if sec_raw["total"]:
                sec_raw["accuracy"] = sec_raw["correct"] / sec_raw["total"]
    else:
        for question in exam.questions:
            response = response_by_question.get(question.id)
            final_answer = response.final_answer if response else None
            is_correct = final_answer == question.correct_answer if final_answer else False
            if response is not None:
                response.is_correct = is_correct

            total_max_points += question.points
            if is_correct:
                total_correct_points += question.points

        raw["total"] = {
            "correct": total_correct_points,
            "total": total_max_points,
            "accuracy": total_correct_points / total_max_points if total_max_points else 0.0,
        }

    total_score = total_correct_points
    percentage = total_correct_points / total_max_points if total_max_points else 0.0

    score_details: dict = {}
    if exam.scoring_sections:
        for section in exam.scoring_sections:
            sec_raw = raw.get(section.code, {})
            sec_correct = sec_raw.get("correct", 0.0)
            sec_total = sec_raw.get("total", 0.0)
            sec_accuracy = sec_raw.get("accuracy")
            score_details[section.code] = {
                "earned": sec_correct,
                "possible": sec_total,
                "percentage": round(sec_accuracy * 100, 1) if sec_accuracy is not None else 0.0,
            }

    score_details["total"] = total_score
    score_details["percentage"] = round(percentage * 100, 1)
    score_details["max_points"] = total_max_points
    score_details["correct_points"] = total_correct_points

    if missing_required_sheet:
        status = "incomplete"
    elif has_unreviewed:
        status = "not_scored"
    elif exam.pass_mark_type and exam.pass_mark_value is not None:
        if exam.pass_mark_type == "points":
            is_passed = total_correct_points >= exam.pass_mark_value
        else:  # "percentage"
            is_passed = (percentage * 100) >= exam.pass_mark_value
        status = "pass" if is_passed else "fail"
    else:
        # No pass mark: neutral scored status instead of pass/fail.
        status = "scored"

    result.raw_json = raw
    result.score_details_json = score_details
    result.total_score = total_score
    result.pass_fail_status = status
    if existing is None:
        db.add(result)
    return result


def _student_responses(db: Session, exam_id: int, student_id: int) -> list[Response]:
    return list(
        db.scalars(
            select(Response)
            .join(ScannedPage)
            .join(SheetInstance)
            .where(SheetInstance.exam_id == exam_id)
            .where(SheetInstance.student_id == student_id)
            .where(ScannedPage.review_status.in_(("ok", "needs_review")))
        )
    )


def _count_status(results: list[StudentResult], status: str) -> int:
    return len([result for result in results if result.pass_fail_status == status])
