from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ExamStudent,
    MockExam,
    ScannedPage,
    SheetInstance,
    Student,
    StudentResult,
)
from app.services.i18n_service import _
from app.services.list_filter_service import (
    ListFilters,
    PaginatedResult,
    apply_filters,
    apply_filters_and_paginate,
)

# Shown when staff try to run an operational action (students, sheets, scans, review,
# results, exports) on a reusable template instead of a test run.
TEMPLATE_OPERATION_ERROR = (
    "Create a test run from this template before adding students, generating sheets, "
    "scanning, or exporting results."
)

# Shown when staff try to mutate an archived session/run. Archived sessions are read-only:
# their scan files are purged, but result/insight data is kept for viewing and export.
ARCHIVED_SESSION_ERROR = (
    "This session is archived and read-only. Archived sessions can only be viewed, "
    "exported, or deleted."
)


@dataclass(frozen=True)
class ExamCreateInput:
    name: str
    assessment_type: str
    exam_date: str | None
    scoring_mode: str
    numbering_mode: str


def list_exams(db: Session, filters: ListFilters | None = None) -> list[MockExam]:
    # Exclude reusable templates; they have their own /templates listing. By default
    # (filters=None) archived sessions are hidden and the newest come first, so every
    # existing caller keeps its prior behaviour minus archived rows.
    exams = list(db.scalars(select(MockExam).where(MockExam.is_template.is_(False))))
    return apply_filters(exams, filters or ListFilters())


def list_exams_paginated(db: Session, filters: ListFilters) -> PaginatedResult:
    exams = list(db.scalars(select(MockExam).where(MockExam.is_template.is_(False))))
    return apply_filters_and_paginate(exams, filters)


def get_exam(db: Session, exam_id: int) -> MockExam | None:
    return db.get(MockExam, exam_id)


def ensure_not_template(exam: MockExam) -> None:
    """Service-level guard: refuse operational actions on a reusable template."""
    if exam.is_template:
        raise ValueError(TEMPLATE_OPERATION_ERROR)


def is_archived(exam: MockExam) -> bool:
    """True for an archived template (template_status) or archived session/run (status)."""
    if exam.is_template:
        return exam.template_status == "archived"
    return exam.status == "archived"


def ensure_not_archived(exam: MockExam) -> None:
    """Raise 409 if the exam/run is archived (read-only)."""
    if is_archived(exam):
        raise HTTPException(status_code=409, detail=ARCHIVED_SESSION_ERROR)


def ensure_run_editable(exam: MockExam) -> None:
    """Route guard for mutating session/run actions.

    Templates are rejected upstream by ``get_run_or_404``; this additionally blocks
    archived sessions so every modifying route stays read-only after archive.
    """
    ensure_not_archived(exam)


@dataclass(frozen=True)
class ExamUpdateInput:
    name: str
    exam_date: str | None = None


def update_exam(db: Session, exam: MockExam, data: ExamUpdateInput) -> MockExam:
    """Edit a run/exam's name and test date."""
    if not data.name.strip():
        raise ValueError("Assessment name is required.")
    if data.exam_date:
        try:
            datetime.strptime(data.exam_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("Date must use YYYY-MM-DD format.") from exc
    exam.name = data.name.strip()
    exam.exam_date = data.exam_date or None
    db.commit()
    db.refresh(exam)
    return exam


def delete_exam(db: Session, exam: MockExam) -> None:
    """Delete a run/exam and any students orphaned by deleting that whole run."""
    candidate_student_ids = {link.student_id for link in exam.students}
    db.delete(exam)
    db.flush()
    delete_orphan_students(db, candidate_student_ids)
    db.commit()


def delete_orphan_students(db: Session, candidate_student_ids: set[int]) -> None:
    for student_id in candidate_student_ids:
        if _student_is_orphaned(db, student_id):
            student = db.get(Student, student_id)
            if student is not None:
                db.delete(student)


def _student_is_orphaned(db: Session, student_id: int) -> bool:
    return all(
        count == 0
        for count in (
            _count_student_refs(db, ExamStudent, ExamStudent.student_id == student_id),
            _count_student_refs(db, SheetInstance, SheetInstance.student_id == student_id),
            _count_student_refs(db, StudentResult, StudentResult.student_id == student_id),
            _count_student_refs(db, ScannedPage, ScannedPage.student_id == student_id),
        )
    )


def _count_student_refs(db: Session, model: type, where_clause) -> int:
    return db.scalar(select(func.count()).select_from(model).where(where_clause)) or 0


def get_run_or_404(db: Session, exam_id: int, *, for_write: bool = False) -> MockExam:
    """Load a runnable exam/run for operational routes.

    Raises 404 if missing and 400 (with a clear message) if the record is a template,
    so templates cannot reach student/sheet/scan/review/results/export actions. When
    ``for_write`` is set (mutating routes), archived sessions are additionally rejected
    with 409 so archived runs stay read-only.
    """
    exam = db.get(MockExam, exam_id)
    if exam is None:
        raise HTTPException(status_code=404)
    if exam.is_template:
        raise HTTPException(status_code=400, detail=TEMPLATE_OPERATION_ERROR)
    if for_write:
        ensure_not_archived(exam)
    return exam


def create_exam(db: Session, data: ExamCreateInput) -> MockExam:
    validate_exam_input(data)
    exam = MockExam(
        name=data.name.strip(),
        assessment_type=data.assessment_type,
        exam_date=data.exam_date or None,
        scoring_mode=data.scoring_mode,
        numbering_mode=data.numbering_mode,
        status="draft",
    )
    db.add(exam)
    db.commit()
    db.refresh(exam)
    return exam


def validate_exam_input(data: ExamCreateInput) -> None:
    if not data.name.strip():
        raise ValueError("Assessment name is required.")
    if data.assessment_type not in {"generic_test", "peer_review"}:
        raise ValueError("Choose a valid assessment type.")
    if data.exam_date:
        try:
            datetime.strptime(data.exam_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("Date must use YYYY-MM-DD format.") from exc
    if data.numbering_mode not in {"continuous", "reset_per_problem"}:
        raise ValueError("Choose a valid question numbering mode.")


def next_exam_action(exam: MockExam) -> tuple[str, str]:
    if exam.assessment_type == "peer_review":
        if exam.status == "draft":
            if not exam.students:
                return (_("exams.action.add_students"), f"/exams/{exam.id}/students")
            return (
                _("exams.action.configure_competencies"),
                f"/exams/{exam.id}/peer-review/builder",
            )
        actions = {
            "ready_to_print": (
                _("exams.action.generate_sheets"),
                f"/exams/{exam.id}/peer-review/builder",
            ),
            "printed": (
                _("exams.action.upload_scans"),
                f"/exams/{exam.id}/peer-review/scan-batches",
            ),
            "scanning": (
                _("exams.action.view_scan_progress"),
                f"/exams/{exam.id}/peer-review/scan-batches",
            ),
            "ready_to_score": (_("exams.action.calculate_results"), f"/exams/{exam.id}/results"),
            "needs_review": (
                _("exams.action.review_problems"),
                f"/exams/{exam.id}/peer-review/review",
            ),
            "results_ready": (_("exams.action.view_results"), f"/exams/{exam.id}/results"),
            "locked": (_("exams.action.export"), f"/exams/{exam.id}/exports"),
        }
        return actions.get(
            exam.status, (_("exams.action.open"), f"/exams/{exam.id}/peer-review/builder")
        )
    if exam.status == "draft":
        if not exam.students:
            return (_("exams.action.add_students"), f"/exams/{exam.id}/students")
        return (_("exams.action.generate_sheets"), f"/exams/{exam.id}/sheets")
    actions = {
        "ready_to_print": (_("exams.action.generate_sheets"), f"/exams/{exam.id}/sheets"),
        "printed": (_("exams.action.upload_scans"), f"/exams/{exam.id}/scan-batches"),
        "scanning": (_("exams.action.view_scan_progress"), f"/exams/{exam.id}/scan-batches"),
        "needs_review": (_("exams.action.review_problems"), f"/exams/{exam.id}/review"),
        "results_ready": (_("exams.action.view_results"), f"/exams/{exam.id}/results"),
        "locked": (_("exams.action.export"), f"/exams/{exam.id}/exports"),
    }
    return actions.get(exam.status, (_("exams.action.open"), f"/exams/{exam.id}"))
