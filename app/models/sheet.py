from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SheetTemplate(Base):
    __tablename__ = "sheet_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("mock_exams.id"), index=True)
    physical_sheet_id: Mapped[int] = mapped_column(ForeignKey("physical_sheets.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    paper_size: Mapped[str] = mapped_column(String, default="A4")
    coordinate_map_json: Mapped[dict] = mapped_column(JSON, default=dict)
    marker_positions_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    exam = relationship("MockExam", back_populates="sheet_templates")
    physical_sheet = relationship("PhysicalSheet", back_populates="templates")
    instances = relationship("SheetInstance", back_populates="sheet_template")


class BlankSheetLabel(Base):
    """QR token for a printed blank mark sheet not yet linked to a student.

    The token identifies the session and physical sheet at scan time; the
    student is chosen during upload review. Kept separate from SheetInstance
    because instances always belong to a student.
    """

    __tablename__ = "blank_sheet_labels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("mock_exams.id"), index=True)
    physical_sheet_id: Mapped[int] = mapped_column(ForeignKey("physical_sheets.id"), index=True)
    sheet_template_id: Mapped[int] = mapped_column(ForeignKey("sheet_templates.id"), index=True)
    qr_token: Mapped[str] = mapped_column(String, unique=True, index=True)
    pdf_file_path: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    physical_sheet = relationship("PhysicalSheet")
    sheet_template = relationship("SheetTemplate")


class SheetInstance(Base):
    __tablename__ = "sheet_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("mock_exams.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    physical_sheet_id: Mapped[int] = mapped_column(ForeignKey("physical_sheets.id"), index=True)
    sheet_template_id: Mapped[int] = mapped_column(ForeignKey("sheet_templates.id"), index=True)
    qr_token: Mapped[str] = mapped_column(String, unique=True, index=True)
    printed_name: Mapped[str] = mapped_column(String)
    printed_student_number: Mapped[str] = mapped_column(String)
    pdf_file_path: Mapped[str | None] = mapped_column(String, nullable=True)
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, default="generated", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    exam = relationship("MockExam", back_populates="sheet_instances")
    student = relationship("Student", back_populates="sheet_instances")
    physical_sheet = relationship("PhysicalSheet", back_populates="instances")
    sheet_template = relationship("SheetTemplate", back_populates="instances")
    scanned_pages = relationship("ScannedPage", back_populates="sheet_instance")
