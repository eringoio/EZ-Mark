from fastapi.testclient import TestClient

from app.main import create_app


def _new_template_id(client: TestClient) -> int:
    response = client.post(
        "/templates/new",
        data={
            "name": "Guard Template",
            "assessment_type": "generic_test",
        },
        follow_redirects=True,
    )
    return int(response.url.path.rstrip("/").split("/")[-2])


def test_sheet_generation_rejected_for_template() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template_id(client)
        response = client.post(f"/exams/{template_id}/sheets/generate")
        assert response.status_code == 400


def test_scan_upload_rejected_for_template() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template_id(client)
        response = client.post(
            f"/exams/{template_id}/scan-batches/upload",
            files={"files": ("scan.pdf", b"data", "application/pdf")},
        )
        assert response.status_code == 400


def test_results_page_rejected_for_template() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template_id(client)
        response = client.get(f"/exams/{template_id}/results")
        assert response.status_code == 400
        assert "Create a test run" in response.text


def test_exports_page_rejected_for_template() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template_id(client)
        response = client.get(f"/exams/{template_id}/exports")
        assert response.status_code == 400


def test_student_import_rejected_for_template() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template_id(client)
        response = client.get(f"/exams/{template_id}/students/import")
        assert response.status_code == 400


def test_template_detail_still_works() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template_id(client)
        response = client.get(f"/templates/{template_id}")
        assert response.status_code == 200


def test_run_operational_pages_still_work() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template_id(client)
        run_response = client.post(
            f"/templates/{template_id}/runs/new",
            data={"run_name": "Guard Run", "exam_date": ""},
            follow_redirects=True,
        )
        # Creation lands on /exams/{id}/students/import
        parts = run_response.url.path.rstrip("/").split("/")
        run_id = int(parts[parts.index("exams") + 1])

        assert client.get(f"/exams/{run_id}/results").status_code == 200
        assert client.get(f"/exams/{run_id}/sheets").status_code == 200
        assert client.get(f"/exams/{run_id}/exports").status_code == 200
        assert client.get(f"/exams/{run_id}/students/import").status_code == 200
