from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ProblemGroup(Base):
    __tablename__ = "problem_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("mock_exams.id"), index=True)
    physical_sheet_id: Mapped[int | None] = mapped_column(
        ForeignKey("physical_sheets.id"),
        nullable=True,
        index=True,
    )
    problem_no: Mapped[int] = mapped_column(Integer)
    problem_title: Mapped[str | None] = mapped_column(String, nullable=True)
    subquestion_count: Mapped[int] = mapped_column(Integer)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    exam = relationship("MockExam", back_populates="problem_groups")
    physical_sheet = relationship("PhysicalSheet", back_populates="problem_groups")


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("mock_exams.id"), index=True)
    physical_sheet_id: Mapped[int] = mapped_column(ForeignKey("physical_sheets.id"), index=True)
    scoring_section_id: Mapped[int | None] = mapped_column(
        ForeignKey("scoring_sections.id"),
        nullable=True,
        index=True,
    )
    question_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    problem_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subquestion_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    local_question_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    local_question_label: Mapped[str | None] = mapped_column(String, nullable=True)
    display_label: Mapped[str] = mapped_column(String)
    test_section: Mapped[str | None] = mapped_column(String, nullable=True)
    correct_answer: Mapped[str | None] = mapped_column(String, nullable=True)
    choice_count: Mapped[int] = mapped_column(Integer, default=4)
    # Canonical scoring weight for Standard Tests. Fractional points are allowed.
    points: Mapped[float] = mapped_column(Float, default=1.0)
    item_type: Mapped[str | None] = mapped_column(String, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    exam = relationship("MockExam", back_populates="questions")
    physical_sheet = relationship("PhysicalSheet", back_populates="questions")
    scoring_section = relationship("ScoringSection", back_populates="questions")
    responses = relationship("Response", back_populates="question", cascade="all, delete-orphan")
