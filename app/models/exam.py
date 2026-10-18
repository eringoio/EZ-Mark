from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, event
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.database import Base


class MockExam(Base):
    __tablename__ = "mock_exams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, index=True)
    assessment_type: Mapped[str] = mapped_column(String, default="generic_test", index=True)
    exam_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft", index=True)
    scoring_mode: Mapped[str] = mapped_column(String, default="generic_raw")
    numbering_mode: Mapped[str | None] = mapped_column(String, nullable=True)
    # Explicit pass mark. ``pass_mark_value`` is the threshold, ``pass_mark_type``
    # says how to read it: "points" (raw earned points) or "percentage"
    # (0-100 correctness rate). Both set together; neither set means no pass/fail.
    pass_mark_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    pass_mark_type: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Archive bookkeeping. Templates archive via template_status="archived"; sessions/runs
    # archive via status="archived". archived_at records when (either kind). For sessions,
    # archived_previous_status remembers the workflow status before archiving, and
    # scan_files_purged_at records when the on-disk scans were removed (archive purges scans).
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_previous_status: Mapped[str | None] = mapped_column(String, nullable=True)
    scan_files_purged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    is_template: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("mock_exams.id"), nullable=True, index=True
    )
    template_version: Mapped[int] = mapped_column(Integer, default=1)
    template_status: Mapped[str] = mapped_column(String, default="active")
    template_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set on a template that was created as a newer version of another template (reload of a
    # template that already had runs). Distinct from source_template_id (run -> template).
    previous_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("mock_exams.id"), nullable=True, index=True
    )

    source_template = relationship(
        "MockExam",
        remote_side=[id],
        foreign_keys=[source_template_id],
        back_populates="template_runs",
    )
    template_runs = relationship(
        "MockExam",
        foreign_keys=[source_template_id],
        back_populates="source_template",
    )

    students = relationship("ExamStudent", back_populates="exam", cascade="all, delete-orphan")
    physical_sheets = relationship(
        "PhysicalSheet",
        back_populates="exam",
        cascade="all, delete-orphan",
        order_by="PhysicalSheet.display_order",
    )
    scoring_sections = relationship(
        "ScoringSection",
        back_populates="exam",
        cascade="all, delete-orphan",
        order_by="ScoringSection.display_order",
    )
    questions = relationship(
        "Question",
        back_populates="exam",
        cascade="all, delete-orphan",
        order_by="Question.display_order",
    )
    problem_groups = relationship(
        "ProblemGroup", back_populates="exam", cascade="all, delete-orphan"
    )
    sheet_templates = relationship(
        "SheetTemplate", back_populates="exam", cascade="all, delete-orphan"
    )
    sheet_instances = relationship(
        "SheetInstance", back_populates="exam", cascade="all, delete-orphan"
    )
    scan_batches = relationship("ScanBatch", back_populates="exam", cascade="all, delete-orphan")
    results = relationship("StudentResult", back_populates="exam", cascade="all, delete-orphan")
    exports = relationship("Export", back_populates="exam", cascade="all, delete-orphan")
    competencies = relationship("Competency", back_populates="exam", cascade="all, delete-orphan")


class ExamStudent(Base):
    __tablename__ = "exam_students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("mock_exams.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    class_name: Mapped[str | None] = mapped_column(String, nullable=True)
    seat_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    included: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    exam = relationship("MockExam", back_populates="students")
    student = relationship("Student", back_populates="exam_links")

    @property
    def group_name(self) -> str:
        return self.class_name if self.class_name is not None else self.student.class_name


class PhysicalSheet(Base):
    __tablename__ = "physical_sheets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("mock_exams.id"), index=True)
    sheet_no: Mapped[int] = mapped_column(Integer)
    sheet_name: Mapped[str] = mapped_column(String)
    slug: Mapped[str] = mapped_column(String)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    exam = relationship("MockExam", back_populates="physical_sheets")
    questions = relationship("Question", back_populates="physical_sheet")
    templates = relationship("SheetTemplate", back_populates="physical_sheet")
    instances = relationship("SheetInstance", back_populates="physical_sheet")
    problem_groups = relationship("ProblemGroup", back_populates="physical_sheet")


class ScoringSection(Base):
    __tablename__ = "scoring_sections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("mock_exams.id"), index=True)
    code: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    exam = relationship("MockExam", back_populates="scoring_sections")
    questions = relationship("Question", back_populates="scoring_section")


# Snapshot groups for every new roster link, including links created by older services.
@event.listens_for(Session, "before_flush")
def _snapshot_roster_groups(session, flush_context, instances):
    from app.models.student import Student

    for link in list(session.new):
        if isinstance(link, ExamStudent) and link.class_name is None:
            student = link.student or session.get(Student, link.student_id)
            if student is not None:
                link.class_name = student.class_name
