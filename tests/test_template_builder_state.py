"""Builder state for templates whose questions have no ProblemGroup rows.

Workbook-imported templates store questions without problem groups or
problem_no, which previously made the template builder render as empty.
The serializer must synthesize groups, and saving the builder must persist
them without changing question identities.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.models import MockExam, PhysicalSheet, Question, ScoringSection
from app.routes.test_templates import _parse_builder_state, _serialize_builder_state
from app.services.test_template_service import save_template_builder


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _imported_style_template(session: Session) -> MockExam:
    """A template shaped like a workbook import: questions but no groups."""
    template = MockExam(
        name="Synthetic Test Import",
        assessment_type="generic_test",
        scoring_mode="generic_raw",
        numbering_mode="continuous",
        status="draft",
        is_template=True,
        template_status="active",
    )
    sheet = PhysicalSheet(sheet_no=1, sheet_name="文字・語彙", slug="vocabulary", display_order=1)
    section = ScoringSection(code="language_knowledge", name="Language Knowledge")
    template.physical_sheets.append(sheet)
    template.scoring_sections.append(section)
    session.add(template)
    session.flush()
    item_types = ["漢字読み", "漢字読み", "漢字読み", "文脈規定", "文脈規定"]
    for index, item_type in enumerate(item_types, start=1):
        template.questions.append(
            Question(
                physical_sheet_id=sheet.id,
                scoring_section_id=section.id,
                question_no=index,
                local_question_no=index,
                display_label=str(index),
                correct_answer=str((index % 4) + 1),
                choice_count=4,
                points=1.0,
                item_type=item_type,
                display_order=index,
            )
        )
    session.commit()
    return template


def test_builder_state_synthesizes_groups_for_groupless_questions() -> None:
    with _session() as session:
        template = _imported_style_template(session)

        state = _serialize_builder_state(template)

        sheet_state = state["sheets"][0]
        assert len(sheet_state["groups"]) == 2  # one per contiguous item_type run
        first, second = sheet_state["groups"]
        assert first["id"] is None and second["id"] is None
        assert first["problem_title"] == "漢字読み"
        assert second["problem_title"] == "文脈規定"
        all_question_ids = [
            question["id"] for group in sheet_state["groups"] for question in group["questions"]
        ]
        assert sorted(all_question_ids) == sorted(q.id for q in template.questions)
        # Every question keeps its saved answer in the builder.
        assert all(
            question["correct_answer"]
            for group in sheet_state["groups"]
            for question in group["questions"]
        )


def test_saving_synthesized_state_persists_groups_and_keeps_question_ids() -> None:
    with _session() as session:
        template = _imported_style_template(session)
        original_question_ids = {question.id for question in template.questions}

        state = _serialize_builder_state(template)
        save_template_builder(session, template, _parse_builder_state(state))

        assert len(template.problem_groups) == 2
        assert all(
            group.physical_sheet_id == template.physical_sheets[0].id
            for group in template.problem_groups
        )
        assert {question.id for question in template.questions} == original_question_ids
        assert all(question.problem_no is not None for question in template.questions)

        # A second load now serializes the persisted groups (no more None ids).
        reloaded = _serialize_builder_state(template)
        group_ids = [group["id"] for group in reloaded["sheets"][0]["groups"]]
        assert all(group_id is not None for group_id in group_ids)
        assert len(reloaded["sheets"][0]["groups"]) == 2


def test_builder_state_keeps_existing_groups_intact() -> None:
    """Templates built in the builder (groups already saved) are unaffected."""
    with _session() as session:
        template = _imported_style_template(session)
        state = _serialize_builder_state(template)
        save_template_builder(session, template, _parse_builder_state(state))
        before = _serialize_builder_state(template)

        after = _serialize_builder_state(template)

        assert before == after
