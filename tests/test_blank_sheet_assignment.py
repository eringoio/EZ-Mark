"""Blank mark sheet workflow: print blank -> scan -> assign student.

The printed QR token of a blank sheet must remain the token of the final
assigned sheet instance, and reprocessing must never wipe the manual work.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.models  # noqa: F401
import app.routes.scans as scans_routes
from app.database import Base
from app.models import (
    BlankSheetLabel,
    ExamStudent,
    MockExam,
    PhysicalSheet,
    Question,
    ScanBatch,
    ScannedPage,
    ScoringSection,
    SheetInstance,
    SheetTemplate,
    Student,
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        yield db


def _exam_with_blank_scan(session: Session) -> tuple[MockExam, Student, ScannedPage, str]:
    exam = MockExam(name="Sample Test June", assessment_type="generic_test")
    student = Student(student_number="SYN001", name="Student A", class_name="A")
    sheet = PhysicalSheet(sheet_no=1, sheet_name="文字・語彙", slug="vocabulary", display_order=1)
    section = ScoringSection(code="language_knowledge", name="LK")
    exam.physical_sheets.append(sheet)
    exam.scoring_sections.append(section)
    session.add_all([exam, student])
    session.commit()
    session.add(ExamStudent(exam_id=exam.id, student_id=student.id))
    session.add(
        Question(
            exam_id=exam.id,
            physical_sheet_id=sheet.id,
            scoring_section_id=section.id,
            display_label="1",
            choice_count=4,
            correct_answer="1",
        )
    )
    template = SheetTemplate(exam_id=exam.id, physical_sheet_id=sheet.id)
    session.add(template)
    session.flush()
    blank_token = "blank-opaque-token"
    session.add(
        BlankSheetLabel(
            exam_id=exam.id,
            physical_sheet_id=sheet.id,
            sheet_template_id=template.id,
            qr_token=blank_token,
        )
    )
    batch = ScanBatch(exam_id=exam.id, uploaded_file_name="scan.pdf", uploaded_file_path="x")
    page = ScannedPage(
        scan_batch=batch,
        page_no=1,
        image_path="x",
        qr_token=blank_token,
        qr_status="blank",
        physical_sheet_id=sheet.id,
    )
    session.add_all([batch, page])
    session.commit()
    return exam, student, page, blank_token


def _patch_cv_helpers(monkeypatch) -> None:
    monkeypatch.setattr(scans_routes, "align_scanned_page", lambda page, instance: None)
    monkeypatch.setattr(scans_routes, "_read_responses_for_sheet", lambda db, page, instance: 0)


def test_assigning_blank_sheet_keeps_printed_qr_token(session, monkeypatch) -> None:
    exam, student, page, blank_token = _exam_with_blank_scan(session)
    _patch_cv_helpers(monkeypatch)

    scans_routes.map_scanned_page(
        page_id=page.id,
        student_id=student.id,
        physical_sheet_id=exam.physical_sheets[0].id,
        db=session,
    )

    instance = session.scalar(select(SheetInstance).where(SheetInstance.qr_token == blank_token))
    assert instance is not None
    assert instance.student_id == student.id
    assert instance.physical_sheet_id == exam.physical_sheets[0].id
    assert page.sheet_instance_id == instance.id
    assert page.student_id == student.id


def test_assigning_blank_sheet_adopts_token_on_unscanned_existing_instance(
    session, monkeypatch
) -> None:
    exam, student, page, blank_token = _exam_with_blank_scan(session)
    sheet = exam.physical_sheets[0]
    template = exam.sheet_templates[0]
    existing = SheetInstance(
        exam_id=exam.id,
        student_id=student.id,
        physical_sheet_id=sheet.id,
        sheet_template_id=template.id,
        qr_token="printed-but-unused-token",
        printed_name=student.name,
        printed_student_number=student.student_number,
    )
    session.add(existing)
    session.commit()
    _patch_cv_helpers(monkeypatch)

    scans_routes.map_scanned_page(
        page_id=page.id,
        student_id=student.id,
        physical_sheet_id=sheet.id,
        db=session,
    )

    session.refresh(existing)
    assert existing.qr_token == blank_token
    assert page.sheet_instance_id == existing.id


def test_unrelated_unmatched_page_still_gets_fresh_token(session, monkeypatch) -> None:
    exam, student, page, blank_token = _exam_with_blank_scan(session)
    page.qr_token = None
    page.qr_status = "not_found"
    session.commit()
    _patch_cv_helpers(monkeypatch)

    scans_routes.map_scanned_page(
        page_id=page.id,
        student_id=student.id,
        physical_sheet_id=exam.physical_sheets[0].id,
        db=session,
    )

    instance = session.get(SheetInstance, page.sheet_instance_id)
    assert instance is not None
    assert instance.qr_token != blank_token
    # The blank label's token is still free for the sheet that actually carries it.
    label = session.scalar(select(BlankSheetLabel).where(BlankSheetLabel.qr_token == blank_token))
    assert label is not None


def test_manual_mapping_zero_responses_needs_review(session, monkeypatch) -> None:
    exam, student, page, _blank_token = _exam_with_blank_scan(session)
    _patch_cv_helpers(monkeypatch)

    scans_routes.map_scanned_page(
        page_id=page.id,
        student_id=student.id,
        physical_sheet_id=exam.physical_sheets[0].id,
        db=session,
    )

    assert page.review_status == "needs_review"
    assert page.mapping_note == "回答を読み取れませんでした。画像またはテンプレートを確認してください。"
