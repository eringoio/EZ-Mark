from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ScanBatch(Base):
    __tablename__ = "scan_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("mock_exams.id"), index=True)
    uploaded_file_name: Mapped[str] = mapped_column(String)
    uploaded_file_path: Mapped[str] = mapped_column(String)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String, default="uploaded", index=True)
    total_pages: Mapped[int] = mapped_column(Integer, default=0)
    detected_pages: Mapped[int] = mapped_column(Integer, default=0)
    problem_pages: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    exam = relationship("MockExam", back_populates="scan_batches")
    scanned_pages = relationship(
        "ScannedPage", back_populates="scan_batch", cascade="all, delete-orphan"
    )


class ScannedPage(Base):
    __tablename__ = "scanned_pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_batch_id: Mapped[int] = mapped_column(ForeignKey("scan_batches.id"), index=True)
    sheet_instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("sheet_instances.id"),
        nullable=True,
        index=True,
    )
    student_id: Mapped[int | None] = mapped_column(
        ForeignKey("students.id"), nullable=True, index=True
    )
    physical_sheet_id: Mapped[int | None] = mapped_column(
        ForeignKey("physical_sheets.id"),
        nullable=True,
        index=True,
    )
    page_no: Mapped[int] = mapped_column(Integer)
    image_path: Mapped[str] = mapped_column(String)
    aligned_image_path: Mapped[str | None] = mapped_column(String, nullable=True)
    # The opaque token decoded from the printed QR code (never PII). Kept so a
    # blank sheet's printed token can stay attached when a student is assigned.
    qr_token: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    qr_status: Mapped[str] = mapped_column(String, default="not_found")
    alignment_status: Mapped[str] = mapped_column(String, default="failed")
    review_status: Mapped[str] = mapped_column(String, default="needs_review", index=True)
    confidence_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    duplicate_of_page_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    replaced_by_page_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_exam_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_sheet_instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    mapping_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    scan_batch = relationship("ScanBatch", back_populates="scanned_pages")
    sheet_instance = relationship("SheetInstance", back_populates="scanned_pages")
    responses = relationship(
        "Response", back_populates="scanned_page", cascade="all, delete-orphan"
    )
