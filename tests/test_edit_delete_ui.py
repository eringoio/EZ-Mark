from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import create_app
from app.models import MockExam


def _new_template_id(client: TestClient) -> int:
    response = client.post(
        "/templates/new",
        data={
            "name": "EditDelete Template",
            "assessment_type": "generic_test",
            "numbering_mode": "continuous",
        },
        follow_redirects=True,
    )
    # Creation lands on /templates/{id}/builder
    return int(response.url.path.rstrip("/").split("/")[-2])


def _new_run_id(client: TestClient, template_id: int) -> int:
    response = client.post(
        f"/templates/{template_id}/runs/new",
        data={"run_name": "EditDelete Run", "exam_date": ""},
        follow_redirects=True,
    )
    # Creation lands on /exams/{id}/students/import
    parts = response.url.path.rstrip("/").split("/")
    return int(parts[parts.index("exams") + 1])


def test_template_edit_form_loads_and_updates() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template_id(client)

        form = client.get(f"/templates/{template_id}/edit")
        assert form.status_code == 200
        assert "Edit template" in form.text

        updated = client.post(
            f"/templates/{template_id}/edit",
            data={
                "name": "Renamed Template",
                "template_description": "new desc",
                "pass_mark_value": "25",
                "pass_mark_type": "points",
            },
            follow_redirects=True,
        )
        assert updated.status_code == 200
        assert "Renamed Template" in updated.text

        with SessionLocal() as session:
            template = session.get(MockExam, template_id)
            assert template.name == "Renamed Template"
            assert template.pass_mark_value == 25
            assert template.pass_mark_type == "points"


def test_template_delete_removes_it() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template_id(client)

        response = client.post(f"/templates/{template_id}/delete", follow_redirects=True)
        assert response.status_code == 200

        with SessionLocal() as session:
            assert session.get(MockExam, template_id) is None


def test_exam_edit_and_delete() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template_id(client)
        run_id = _new_run_id(client, template_id)

        form = client.get(f"/exams/{run_id}/edit")
        assert form.status_code == 200
        assert "Edit assessment" in form.text

        updated = client.post(
            f"/exams/{run_id}/update",
            data={"name": "Renamed Run", "exam_date": "2026-07-01"},
            follow_redirects=True,
        )
        assert updated.status_code == 200
        assert "Renamed Run" in updated.text

        with SessionLocal() as session:
            run = session.get(MockExam, run_id)
            assert run.name == "Renamed Run"
            assert run.exam_date == "2026-07-01"

        deleted = client.post(f"/exams/{run_id}/delete", follow_redirects=True)
        assert deleted.status_code == 200
        with SessionLocal() as session:
            assert session.get(MockExam, run_id) is None


def test_exam_edit_route_rejects_template() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template_id(client)
        # A template must not be editable/deletable through the /exams operational routes.
        assert client.get(f"/exams/{template_id}/edit").status_code == 400
        assert client.post(f"/exams/{template_id}/delete").status_code == 400
