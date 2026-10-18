import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.models import Competency, MockExam, ProblemGroup, Question
from app.services.test_template_service import (
    TemplateCreateInput,
    TemplateUpdateInput,
    archive_template,
    create_blank_template,
    create_run_from_template,
    delete_template,
    get_template,
    list_runs_for_template,
    list_templates,
    update_template,
)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _generic_template_input(name: str = "SUCCESS Quiz 第1回") -> TemplateCreateInput:
    return TemplateCreateInput(
        name=name,
        assessment_type="generic_test",
        scoring_mode="generic_raw",
        numbering_mode="continuous",
        template_description="Reusable generic test setup",
    )







def test_list_templates_excludes_runs() -> None:
    with _session() as session:
        template = create_blank_template(session, _generic_template_input())
        run = MockExam(
            name="SUCCESS Quiz 第1回 - 2026 July Class A",
            is_template=False,
            source_template_id=template.id,
        )
        session.add(run)
        session.commit()

        templates = list_templates(session)

        assert [exam.id for exam in templates] == [template.id]


def test_get_template_rejects_normal_run() -> None:
    with _session() as session:
        run = MockExam(name="Normal exam", is_template=False)
        session.add(run)
        session.commit()

        assert get_template(session, run.id) is None
        assert get_template(session, 9999) is None


def test_get_template_returns_template() -> None:
    with _session() as session:
        template = create_blank_template(session, _generic_template_input())

        assert get_template(session, template.id) is template


def test_archive_template_does_not_delete_runs() -> None:
    with _session() as session:
        template = create_blank_template(session, _generic_template_input())
        run = MockExam(
            name="SUCCESS Quiz 第1回 - 2026 July Class A",
            is_template=False,
            source_template_id=template.id,
        )
        session.add(run)
        session.commit()

        archive_template(session, template)

        assert template.template_status == "archived"
        surviving = session.get(MockExam, run.id)
        assert surviving is not None
        assert surviving.source_template_id == template.id


def _populate_template_questions(session: Session, template: MockExam) -> None:
    sheet1 = template.physical_sheets[0]
    listening_sheet = sheet1
    section1 = template.scoring_sections[0]
    section3 = template.scoring_sections[-1]
    session.add_all(
        [
            ProblemGroup(
                exam_id=template.id,
                physical_sheet_id=sheet1.id,
                problem_no=1,
                problem_title="問題1",
                subquestion_count=2,
                display_order=1,
            ),
            Question(
                exam_id=template.id,
                physical_sheet_id=sheet1.id,
                scoring_section_id=section1.id,
                question_no=1,
                problem_no=1,
                subquestion_no=1,
                local_question_no=1,
                display_label="問1",
                test_section="language_knowledge",
                correct_answer="2",
                choice_count=4,
                points=1.0,
                item_type="vocabulary",
                display_order=1,
            ),
            Question(
                exam_id=template.id,
                physical_sheet_id=listening_sheet.id,
                scoring_section_id=section3.id,
                question_no=2,
                display_label="問2",
                test_section="listening",
                correct_answer="3",
                choice_count=4,
                points=2.0,
                item_type="listening",
                display_order=2,
            ),
        ]
    )
    session.commit()


def test_create_run_from_template_copies_setup() -> None:
    with _session() as session:
        template = create_blank_template(session, _generic_template_input())
        _populate_template_questions(session, template)

        run = create_run_from_template(session, template, "Run A", exam_date="2026-07-01")

        assert run.is_template is False
        assert run.source_template_id == template.id
        assert run.assessment_type == "generic_test"
        assert run.exam_date == "2026-07-01"
        assert run.status == "draft"

        assert len(run.physical_sheets) == len(template.physical_sheets)
        assert len(run.scoring_sections) == len(template.scoring_sections)
        assert len(run.problem_groups) == 1
        assert len(run.questions) == 2

        # Copied rows are brand new, not the template's rows.
        run_sheet_ids = {sheet.id for sheet in run.physical_sheets}
        template_sheet_ids = {sheet.id for sheet in template.physical_sheets}
        assert run_sheet_ids.isdisjoint(template_sheet_ids)

        q1 = next(q for q in run.questions if q.display_label == "問1")
        assert q1.correct_answer == "2"
        assert q1.points == 1.0
        assert q1.item_type == "vocabulary"
        # Copied question points at the run's own sheet and section, not the template's.
        assert q1.physical_sheet in run.physical_sheets
        assert q1.scoring_section in run.scoring_sections
        assert q1.physical_sheet_id in run_sheet_ids

        group = run.problem_groups[0]
        assert group.physical_sheet in run.physical_sheets


def test_run_has_no_runtime_data() -> None:
    with _session() as session:
        template = create_blank_template(session, _generic_template_input())
        _populate_template_questions(session, template)

        run = create_run_from_template(session, template, "Run A")

        assert run.students == []
        assert run.scan_batches == []
        assert run.sheet_instances == []
        assert run.sheet_templates == []
        assert run.results == []
        assert run.exports == []


def test_run_is_independent_of_template_changes() -> None:
    with _session() as session:
        template = create_blank_template(session, _generic_template_input())
        _populate_template_questions(session, template)

        run = create_run_from_template(session, template, "Run A")
        run_id = run.id

        # Mutate the template after the run exists.
        template.physical_sheets[0].sheet_name = "CHANGED"
        template_q1 = next(q for q in template.questions if q.display_label == "問1")
        template_q1.correct_answer = "9"
        template_q1.points = 99.0
        session.commit()

        session.expire_all()
        reloaded = session.get(MockExam, run_id)
        assert "CHANGED" not in [sheet.sheet_name for sheet in reloaded.physical_sheets]
        run_q1 = next(q for q in reloaded.questions if q.display_label == "問1")
        assert run_q1.correct_answer == "2"
        assert run_q1.points == 1.0


def test_create_run_from_non_template_raises() -> None:
    with _session() as session:
        normal = MockExam(name="Normal exam", is_template=False)
        session.add(normal)
        session.commit()

        with pytest.raises(ValueError):
            create_run_from_template(session, normal, "Should fail")


def test_peer_review_competencies_are_copied() -> None:
    with _session() as session:
        template = create_blank_template(
            session,
            TemplateCreateInput(
                name="Peer template",
                assessment_type="peer_review",
                scoring_mode="peer_average",
                numbering_mode="continuous",
            ),
        )
        session.add(
            Competency(
                exam_id=template.id,
                name="Teamwork Collaboration",
                description="How well does the classmate collaborate?",
                min_score=1,
                max_score=5,
                display_order=1,
            )
        )
        session.commit()

        run = create_run_from_template(session, template, "Peer run")

        names = [c.name for c in run.competencies]
        # Peer templates seed a default "Overall" competency; the added one is copied too.
        assert "Teamwork Collaboration" in names
        template_competency_ids = {c.id for c in template.competencies}
        assert all(c.id not in template_competency_ids for c in run.competencies)


def test_list_runs_for_template() -> None:
    with _session() as session:
        template = create_blank_template(session, _generic_template_input())
        other_template = create_blank_template(
            session, _generic_template_input(name="SUCCESS Quiz 第1回")
        )
        run_a = MockExam(name="Run A", is_template=False, source_template_id=template.id)
        run_b = MockExam(name="Run B", is_template=False, source_template_id=template.id)
        other_run = MockExam(
            name="Other run", is_template=False, source_template_id=other_template.id
        )
        session.add_all([run_a, run_b, other_run])
        session.commit()

        run_names = {run.name for run in list_runs_for_template(session, template)}

        assert run_names == {"Run A", "Run B"}


def test_update_template_changes_metadata_and_pass_mark() -> None:
    with _session() as session:
        template = create_blank_template(session, _generic_template_input())

        update_template(
            session,
            template,
            TemplateUpdateInput(
                name="Renamed Test",
                template_description="Updated",
                pass_mark_value=25,
                pass_mark_type="points",
            ),
        )

        assert template.name == "Renamed Test"
        assert template.template_description == "Updated"
        assert template.pass_mark_value == 25
        assert template.pass_mark_type == "points"


def test_update_template_without_pass_mark_stays_neutral() -> None:
    with _session() as session:
        template = create_blank_template(session, _generic_template_input())

        update_template(
            session,
            template,
            TemplateUpdateInput(name="Renamed Test"),
        )

        assert template.pass_mark_value is None
        assert template.pass_mark_type is None


def test_update_template_rejects_partial_pass_mark_settings() -> None:
    with _session() as session:
        template = create_blank_template(session, _generic_template_input())
        with pytest.raises(ValueError):
            update_template(
                session,
                template,
                TemplateUpdateInput(
                    name="S1",
                    pass_mark_value=20,
                    pass_mark_type=None,
                ),
            )
        with pytest.raises(ValueError):
            update_template(
                session,
                template,
                TemplateUpdateInput(
                    name="S1",
                    pass_mark_value=None,
                    pass_mark_type="points",
                ),
            )
        with pytest.raises(ValueError):
            update_template(
                session,
                template,
                TemplateUpdateInput(
                    name="S1",
                    pass_mark_value=20,
                    pass_mark_type="weird",
                ),
            )


def test_update_template_rejects_empty_name() -> None:
    with _session() as session:
        template = create_blank_template(session, _generic_template_input())
        with pytest.raises(ValueError):
            update_template(session, template, TemplateUpdateInput(name="  "))


def test_delete_template_removes_setup_but_keeps_runs() -> None:
    with _session() as session:
        template = create_blank_template(session, _generic_template_input())
        run = MockExam(name="Run A", is_template=False, source_template_id=template.id)
        session.add(run)
        session.commit()
        template_id = template.id
        run_id = run.id

        delete_template(session, template)

        assert get_template(session, template_id) is None
        surviving = session.get(MockExam, run_id)
        assert surviving is not None  # run kept
        assert surviving.source_template_id is None  # but disassociated


def test_delete_template_nulls_version_links() -> None:
    with _session() as session:
        original = create_blank_template(session, _generic_template_input())
        version = MockExam(name="SUCCESS Test v2", is_template=True, previous_template_id=original.id)
        session.add(version)
        session.commit()
        version_id = version.id

        delete_template(session, original)

        reloaded = session.get(MockExam, version_id)
        assert reloaded is not None
        assert reloaded.previous_template_id is None
