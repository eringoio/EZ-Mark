from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class StudentResult(Base):
    __tablename__ = "student_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("mock_exams.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    raw_json: Mapped[dict] = mapped_column(JSON, default=dict)
    score_details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # Fractional-capable total (e.g. 7.5). Kept as float end to end; never rounded up.
    total_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    pass_fail_status: Mapped[str] = mapped_column(String, default="not_scored", index=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    exam = relationship("MockExam", back_populates="results")
    student = relationship("Student", back_populates="results")


class Export(Base):
    __tablename__ = "exports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("mock_exams.id"), index=True)
    export_type: Mapped[str] = mapped_column(String, index=True)
    file_path: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    exam = relationship("MockExam", back_populates="exports")
