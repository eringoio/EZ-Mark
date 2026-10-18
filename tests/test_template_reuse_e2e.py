import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.models import ExamStudent, Response, ScanBatch, ScannedPage, Student
from app.services.exam_service import ensure_not_template
from app.services.scoring_service import recalculate_exam_results
from app.services.sheet_generation_service import generate_exam_sheets
from app.services.test_template_service import (
    BuilderGroupInput,
    BuilderQuestionInput,
    BuilderSheetInput,
    TemplateCreateInput,
    create_blank_template,
    create_run_from_template,
    save_template_builder,
)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _template_created(session: Session):
    """A blank Standard Test template seeded with two questions via the builder."""
    template = create_blank_template(
        session,
        TemplateCreateInput(
            name="SUCCESS Test",
            assessment_type="generic_test",
            scoring_mode="generic_raw",
            numbering_mode="reset_per_problem",
        ),
    )
    sheet = template.physical_sheets[0]
    save_template_builder(
        session,
        template,
        [
            BuilderSheetInput(
                id=sheet.id,
                sheet_name=sheet.sheet_name,
                groups=[
                    BuilderGroupInput(
                        id=None,
                        problem_title="もんだい1",
                        questions=[
                            BuilderQuestionInput(
                                id=None,
                                number_label="1",
                                correct_answer="1",
                                choice_count=4,
                                points=1.0,
                                scoring_section_id=template.scoring_sections[0].id,
                            ),
                            BuilderQuestionInput(
                                id=None,
                                number_label="2",
                                correct_answer="2",
                                choice_count=4,
                                points=1.0,
                                scoring_section_id=template.scoring_sections[0].id,
                            ),
                        ],
                    )
                ],
            )
        ],
    )
    return template


def _add_students(session, run, class_name, numbers) -> list[Student]:
    students = []
    for number in numbers:
        student = Student(student_number=number, name=f"Student {number}", class_name=class_name)
        session.add(student)
        session.commit()
        session.add(ExamStudent(exam_id=run.id, student_id=student.id, included=True))
        students.append(student)
    session.commit()
    return students


def test_template_reuse_runs_are_independent() -> None:
    with _session() as session:
        template = _template_created(session)

        # Run A with its own students + sheets.
        run_a = create_run_from_template(session, template, "SUCCESS Test - Class A")
        students_a = _add_students(session, run_a, "A", ["A001", "A002"])
        generate_exam_sheets(session, run_a)

        # Run B from the same template with different students + sheets.
        run_b = create_run_from_template(session, template, "SUCCESS Test - Class B")
        students_b = _add_students(session, run_b, "B", ["B001", "B002"])
        generate_exam_sheets(session, run_b)

        instances_a = list(run_a.sheet_instances)
        instances_b = list(run_b.sheet_instances)

        # Separate sheet instances, one per student per (single) physical sheet.
        assert len(instances_a) == 2
        assert len(instances_b) == 2
        assert {i.id for i in instances_a}.isdisjoint({i.id for i in instances_b})
        assert all(i.exam_id == run_a.id for i in instances_a)
        assert all(i.exam_id == run_b.id for i in instances_b)

        # QR tokens are unique across both runs.
        tokens = [i.qr_token for i in instances_a + instances_b]
        assert len(set(tokens)) == len(tokens)

        # Students are not mixed.
        a_ids = {s.id for s in students_a}
        b_ids = {s.id for s in students_b}
        assert a_ids.isdisjoint(b_ids)
        assert {i.student_id for i in instances_a} == a_ids
        assert {i.student_id for i in instances_b} == b_ids

        # Questions were copied from the same template into each run (independent rows).
        assert len(run_a.questions) == len(run_b.questions) == len(template.questions) == 2
        template_q_ids = {q.id for q in template.questions}
        assert {q.id for q in run_a.questions}.isdisjoint(template_q_ids)
        assert {q.id for q in run_b.questions}.isdisjoint({q.id for q in run_a.questions})

        # Sheet generation on the template itself is rejected.
        with pytest.raises(ValueError):
            ensure_not_template(template)


def test_template_edit_after_runs_does_not_mutate_runs() -> None:
    with _session() as session:
        template = _template_created(session)
        run_a = create_run_from_template(session, template, "Run A")
        run_b = create_run_from_template(session, template, "Run B")

        run_a_answers = sorted(q.correct_answer for q in run_a.questions)
        run_b_answers = sorted(q.correct_answer for q in run_b.questions)

        # Edit the template's answer key after the runs exist.
        for question in template.questions:
            question.correct_answer = "9"
        session.commit()
        session.expire_all()

        assert sorted(q.correct_answer for q in run_a.questions) == run_a_answers
        assert sorted(q.correct_answer for q in run_b.questions) == run_b_answers
        assert run_a_answers == ["1", "2"]


def test_results_are_isolated_per_run() -> None:
    with _session() as session:
        template = _template_created(session)
        run_a = create_run_from_template(session, template, "Run A")
        _add_students(session, run_a, "A", ["A001"])
        generate_exam_sheets(session, run_a)
        run_b = create_run_from_template(session, template, "Run B")
        _add_students(session, run_b, "B", ["B001"])
        generate_exam_sheets(session, run_b)

        # Simulate a scanned page + correct responses for run A's single student.
        instance = run_a.sheet_instances[0]
        batch = ScanBatch(exam_id=run_a.id, uploaded_file_name="s.pdf", uploaded_file_path="x")
        page = ScannedPage(scan_batch=batch, sheet_instance=instance, page_no=1, image_path="x")
        session.add_all([batch, page])
        session.flush()
        for question in run_a.questions:
            session.add(
                Response(
                    scanned_page=page,
                    question=question,
                    final_answer=question.correct_answer,
                    status="ok",
                )
            )
        session.commit()

        recalculate_exam_results(session, run_a)

        # Run A has results; run B and the template have none.
        assert len(run_a.results) >= 1
        assert run_b.results == []
        assert template.results == []
