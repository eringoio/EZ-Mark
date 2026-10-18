from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.models import (
    ExamStudent,
    MockExam,
    PhysicalSheet,
    Question,
    Response,
    ScanBatch,
    ScannedPage,
    ScoringSection,
    SheetInstance,
    SheetTemplate,
    Student,
)
from app.services.scoring_service import question_analysis, recalculate_exam_results


def test_recalculate_results_uses_section_raw_scores() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = MockExam(name="Synthetic Test", assessment_type="generic_test")
        student = Student(student_number="SYN001", name="Student A", class_name="A")
        sheet = PhysicalSheet(sheet_no=1, sheet_name="Sheet", slug="sheet", display_order=1)
        section = ScoringSection(
            code="language_knowledge",
            name="Language Knowledge",
        )
        exam.physical_sheets.append(sheet)
        exam.scoring_sections.append(section)
        session.add_all([exam, student])
        session.commit()
        session.add(ExamStudent(exam_id=exam.id, student_id=student.id))
        template = SheetTemplate(exam_id=exam.id, physical_sheet_id=sheet.id)
        session.add(template)
        session.flush()
        instance = SheetInstance(
            exam_id=exam.id,
            student_id=student.id,
            physical_sheet_id=sheet.id,
            sheet_template_id=template.id,
            qr_token="opaque-token",
            printed_name=student.name,
            printed_student_number=student.student_number,
        )
        question = Question(
            exam_id=exam.id,
            physical_sheet_id=sheet.id,
            scoring_section_id=section.id,
            display_label="1",
            choice_count=4,
            correct_answer="2",
        )
        batch = ScanBatch(exam_id=exam.id, uploaded_file_name="scan.pdf", uploaded_file_path="x")
        page = ScannedPage(scan_batch=batch, sheet_instance=instance, page_no=1, image_path="x")
        response = Response(scanned_page=page, question=question, final_answer="2", status="ok")
        session.add_all([instance, question, batch, page, response])
        session.commit()

        results = recalculate_exam_results(session, exam)

        details = results[0].score_details_json["language_knowledge"]
        assert details == {"earned": 1.0, "possible": 1.0, "percentage": 100.0}
        assert results[0].pass_fail_status == "scored"


def test_zero_question_required_sheet_does_not_make_result_incomplete() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = MockExam(name="Synthetic Test", assessment_type="generic_test")
        student = Student(student_number="SYN002", name="Student B", class_name="A")
        active_sheet = PhysicalSheet(
            sheet_no=1, sheet_name="文字・語彙", slug="vocabulary", required=True, display_order=1
        )
        empty_sheet = PhysicalSheet(
            sheet_no=2,
            sheet_name="文法・読解",
            slug="grammar-reading",
            required=True,
            display_order=2,
        )
        section = ScoringSection(
            code="language_knowledge",
            name="Language Knowledge",
        )
        exam.physical_sheets.extend([active_sheet, empty_sheet])
        exam.scoring_sections.append(section)
        session.add_all([exam, student])
        session.commit()
        session.add(ExamStudent(exam_id=exam.id, student_id=student.id))
        template = SheetTemplate(exam_id=exam.id, physical_sheet_id=active_sheet.id)
        session.add(template)
        session.flush()
        instance = SheetInstance(
            exam_id=exam.id,
            student_id=student.id,
            physical_sheet_id=active_sheet.id,
            sheet_template_id=template.id,
            qr_token="opaque-token",
            printed_name=student.name,
            printed_student_number=student.student_number,
        )
        question = Question(
            exam_id=exam.id,
            physical_sheet_id=active_sheet.id,
            scoring_section_id=section.id,
            display_label="1",
            choice_count=4,
            correct_answer="2",
        )
        batch = ScanBatch(exam_id=exam.id, uploaded_file_name="scan.pdf", uploaded_file_path="x")
        page = ScannedPage(scan_batch=batch, sheet_instance=instance, page_no=1, image_path="x")
        response = Response(scanned_page=page, question=question, final_answer="2", status="ok")
        session.add_all([instance, question, batch, page, response])
        session.commit()

        results = recalculate_exam_results(session, exam)

        assert results[0].pass_fail_status != "incomplete"


def test_recalculate_results_for_generic_test() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = MockExam(
            name="Generic Vocab",
            assessment_type="generic_test",
            pass_mark_value=75.0,
            pass_mark_type="percentage",
        )
        student = Student(student_number="GEN001", name="Student B", class_name="B")
        sheet = PhysicalSheet(sheet_no=1, sheet_name="Sheet", slug="sheet", display_order=1)
        exam.physical_sheets.append(sheet)
        session.add_all([exam, student])
        session.commit()
        session.add(ExamStudent(exam_id=exam.id, student_id=student.id))
        template = SheetTemplate(exam_id=exam.id, physical_sheet_id=sheet.id)
        session.add(template)
        session.flush()
        instance = SheetInstance(
            exam_id=exam.id,
            student_id=student.id,
            physical_sheet_id=sheet.id,
            sheet_template_id=template.id,
            qr_token="opaque-token",
            printed_name=student.name,
            printed_student_number=student.student_number,
        )
        q1 = Question(
            exam_id=exam.id,
            physical_sheet_id=sheet.id,
            display_label="1",
            choice_count=4,
            correct_answer="2",
            points=2.0,
        )
        q2 = Question(
            exam_id=exam.id,
            physical_sheet_id=sheet.id,
            display_label="2",
            choice_count=4,
            correct_answer="1",
            points=2.0,
        )
        batch = ScanBatch(exam_id=exam.id, uploaded_file_name="scan.pdf", uploaded_file_path="x")
        page = ScannedPage(scan_batch=batch, sheet_instance=instance, page_no=1, image_path="x")
        # Correct answer for q1, wrong for q2
        r1 = Response(scanned_page=page, question=q1, final_answer="2", status="ok")
        r2 = Response(scanned_page=page, question=q2, final_answer="3", status="ok")
        session.add_all([instance, q1, q2, batch, page, r1, r2])
        session.commit()

        results = recalculate_exam_results(session, exam)

        # 2 out of 4 points = 50%, which is less than the 75% pass mark
        assert results[0].total_score == 2
        assert results[0].score_details_json["percentage"] == 50.0
        assert results[0].score_details_json["max_points"] == 4.0
        assert results[0].pass_fail_status == "fail"

        # Now update to make it pass
        r2.final_answer = "1"
        session.commit()
        results = recalculate_exam_results(session, exam)
        # 4 out of 4 points = 100% >= 75%
        assert results[0].total_score == 4
        assert results[0].score_details_json["percentage"] == 100.0
        assert results[0].pass_fail_status == "pass"


def test_question_analysis_feedback_priority_metrics() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = MockExam(name="Priority", assessment_type="generic_test")
        sheet = PhysicalSheet(sheet_no=1, sheet_name="Sheet", slug="sheet", display_order=1)
        exam.physical_sheets.append(sheet)
        students = [
            Student(student_number=f"S{i:03}", name=f"Student {i}", class_name="A")
            for i in range(1, 6)
        ]
        q_low = Question(
            exam=exam,
            physical_sheet=sheet,
            display_label="1",
            correct_answer="1",
            choice_count=4,
            display_order=1,
        )
        q_high = Question(
            exam=exam,
            physical_sheet=sheet,
            display_label="2",
            correct_answer="1",
            choice_count=4,
            display_order=2,
        )
        q_blank = Question(
            exam=exam,
            physical_sheet=sheet,
            display_label="3",
            correct_answer="1",
            choice_count=4,
            display_order=3,
        )
        q_multiple = Question(
            exam=exam,
            physical_sheet=sheet,
            display_label="4",
            correct_answer="1",
            choice_count=4,
            display_order=4,
        )
        session.add_all([exam, *students, q_low, q_high, q_blank, q_multiple])
        session.flush()
        template = SheetTemplate(exam_id=exam.id, physical_sheet_id=sheet.id)
        session.add(template)
        session.flush()
        for student in students:
            session.add(ExamStudent(exam_id=exam.id, student_id=student.id))
            instance = SheetInstance(
                exam_id=exam.id,
                student_id=student.id,
                physical_sheet_id=sheet.id,
                sheet_template_id=template.id,
                qr_token=f"opaque-{student.student_number}",
                printed_name=student.name,
                printed_student_number=student.student_number,
            )
            batch = ScanBatch(
                exam_id=exam.id,
                uploaded_file_name=f"{student.student_number}.pdf",
                uploaded_file_path="x",
            )
            page = ScannedPage(scan_batch=batch, sheet_instance=instance, page_no=1, image_path="x")
            session.add_all([instance, batch, page])
            session.flush()
            session.add_all(
                [
                    Response(
                        scanned_page=page,
                        question=q_low,
                        final_answer="1" if student is students[0] else "2",
                        status="ok",
                    ),
                    Response(
                        scanned_page=page,
                        question=q_high,
                        final_answer="1" if student is not students[0] else "2",
                        status="ok",
                    ),
                    Response(
                        scanned_page=page,
                        question=q_blank,
                        final_answer=None,
                        status="blank",
                    ),
                    Response(
                        scanned_page=page,
                        question=q_multiple,
                        final_answer=None,
                        status="multiple_mark",
                    ),
                ]
            )
        session.commit()

        rows = question_analysis(session, exam)
        by_label = {row["question"].display_label: row for row in rows}

        assert by_label["1"]["priority_score"] > by_label["2"]["priority_score"]
        assert by_label["3"]["priority_reason_key"] == "many_blanks"
        assert by_label["4"]["priority_reason_key"] == "many_multiple_marks"
