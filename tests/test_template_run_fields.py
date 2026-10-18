from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.models import MockExam


def _engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return engine


def test_normal_exam_defaults_to_not_template() -> None:
    with Session(_engine()) as session:
        exam = MockExam(name="Synthetic Sample Test", assessment_type="generic_test")
        session.add(exam)
        session.commit()

        assert exam.is_template is False
        assert exam.source_template_id is None
        assert exam.template_version == 1
        assert exam.template_status == "active"
        assert exam.template_description is None


def test_template_record_can_be_created() -> None:
    with Session(_engine()) as session:
        template = MockExam(
            name="SUCCESS Quiz 第1回",
            assessment_type="generic_test",
            is_template=True,
            template_description="Reusable sample setup",
        )
        session.add(template)
        session.commit()

        assert template.is_template is True
        assert template.source_template_id is None
        assert template.template_runs == []


def test_run_references_source_template() -> None:
    with Session(_engine()) as session:
        template = MockExam(name="SUCCESS Quiz 第1回", is_template=True)
        session.add(template)
        session.commit()

        run = MockExam(
            name="SUCCESS Quiz 第1回 - 2026 July Class A",
            is_template=False,
            source_template_id=template.id,
        )
        session.add(run)
        session.commit()

        assert run.source_template_id == template.id
        assert run.source_template is template
        assert template.template_runs == [run]


def test_self_reference_does_not_break_normal_queries() -> None:
    with Session(_engine()) as session:
        template = MockExam(name="Template", is_template=True)
        normal = MockExam(name="Normal exam", is_template=False)
        session.add_all([template, normal])
        session.commit()

        runs = session.scalars(select(MockExam).where(MockExam.is_template.is_(False))).all()
        templates = session.scalars(select(MockExam).where(MockExam.is_template.is_(True))).all()

        assert [exam.name for exam in runs] == ["Normal exam"]
        assert [exam.name for exam in templates] == ["Template"]
