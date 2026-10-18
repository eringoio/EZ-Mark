from fastapi.testclient import TestClient

from app.main import create_app


def _new_template_id(client: TestClient) -> int:
    response = client.post(
        "/templates/new",
        data={
            "name": "Wording Template",
            "assessment_type": "generic_test",
            "numbering_mode": "continuous",
        },
        follow_redirects=True,
    )
    # Creation lands on /templates/{id}/builder
    return int(response.url.path.rstrip("/").split("/")[-2])


def test_template_detail_uses_template_wording() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template_id(client)
        response = client.get(f"/templates/{template_id}")
        assert response.status_code == 200
        assert "Test template" in response.text
        assert "Reusable setup. No students or scan results are stored here." in response.text
        assert "Runs created from this template" in response.text


def test_run_page_uses_run_wording_and_source_template() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template_id(client)
        run_response = client.post(
            f"/templates/{template_id}/runs/new",
            data={"run_name": "Wording Run", "exam_date": ""},
            follow_redirects=True,
        )
        # Creation lands on the student import page for the new run.
        assert run_response.status_code == 200
        parts = run_response.url.path.rstrip("/").split("/")
        run_id = int(parts[parts.index("exams") + 1])

        detail = client.get(f"/exams/{run_id}")
        assert detail.status_code == 200
        assert "Wording Run" in detail.text
        assert "Created from template: Wording Template" in detail.text


def test_guard_message_is_user_friendly() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template_id(client)
        response = client.get(f"/exams/{template_id}/results")
        assert response.status_code == 400
        assert "Create a test run from this template before adding students" in response.text
