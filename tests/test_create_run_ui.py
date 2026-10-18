
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import SessionLocal
from app.models import MockExam
from app.services.test_template_service import (
    BuilderGroupInput,
    BuilderQuestionInput,
    BuilderSheetInput,
    TemplateCreateInput,
    create_blank_template,
    save_template_builder,
)


def _create_template_with_sheet(session: Session, name: str):
    template = create_blank_template(
        session,
        TemplateCreateInput(
            name=name,
            assessment_type="generic_test",
            scoring_mode="generic_raw",
            numbering_mode="reset_per_problem",
        ),
    )
    sheet = template.physical_sheets[0]
    section = template.scoring_sections[0]
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
                                number_label=str(index),
                                correct_answer="1",
                                choice_count=4,
                                points=1.0,
                                scoring_section_id=section.id,
                            )
                            for index in (1, 2)
                        ],
                    )
                ],
            )
        ],
    )
    return template


def test_new_run_form_loads() -> None:
    from app.main import create_app

    with TestClient(create_app()) as client:
        with SessionLocal() as session:
            template = _create_template_with_sheet(session, "Run UI Template A")
            template_id = template.id
        response = client.get(f"/templates/{template_id}/runs/new")
        assert response.status_code == 200
        assert "Create test run" in response.text


def test_post_creates_independent_run() -> None:
    from app.main import create_app

    with TestClient(create_app()) as client:
        with SessionLocal() as session:
            template = _create_template_with_sheet(session, "Run UI Template B")
            template_id = template.id
        response = client.post(
            f"/templates/{template_id}/runs/new",
            data={"run_name": "Run UI 2026 Class A", "exam_date": "2026-07-01"},
            follow_redirects=True,
        )
        # Creation lands on the student import page for the new run.
        assert response.status_code == 200
        parts = response.url.path.rstrip("/").split("/")
        run_id = int(parts[parts.index("exams") + 1])

        detail = client.get(f"/exams/{run_id}")
        assert "Run UI 2026 Class A" in detail.text
        assert "Created from template" in detail.text
        with SessionLocal() as session:
            run = session.get(MockExam, run_id)
            assert run.is_template is False
            assert run.source_template_id == template_id
            assert len(run.questions) == 2
            assert run.students == []
