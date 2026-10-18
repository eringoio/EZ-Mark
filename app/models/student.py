from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_number: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)
    class_name: Mapped[str] = mapped_column(String, index=True)
    school_management_id: Mapped[str | None] = mapped_column(String, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    exam_links = relationship("ExamStudent", back_populates="student", cascade="all, delete-orphan")
    sheet_instances = relationship("SheetInstance", back_populates="student")
    results = relationship("StudentResult", back_populates="student", cascade="all, delete-orphan")

    def group_for_exam(self, exam_id: int) -> str:
        return next(
            (link.group_name for link in self.exam_links if link.exam_id == exam_id),
            self.class_name,
        )


class StudentImportBatch(Base):
    __tablename__ = "student_import_batches"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    exam_id: Mapped[int] = mapped_column(Integer, index=True)
    payload: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
