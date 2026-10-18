from fastapi.testclient import TestClient

from app.main import create_app


def _create_template(client: TestClient, name: str = "Localized Template") -> int:
    response = client.post(
        "/templates/new",
        data={
            "name": name,
            "assessment_type": "generic_test",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return int(response.headers["location"].rstrip("/").split("/")[-2])


def test_templates_list_loads() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/templates")
        assert response.status_code == 200
        assert "Test Templates" in response.text


def test_new_template_page_loads() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/templates/new")
        assert response.status_code == 200
        assert "Create test template" in response.text


def test_create_detail_and_archive_template() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/templates/new",
            data={
                "name": "Synthetic Template",
                "assessment_type": "generic_test",
                "template_description": "Synthetic reusable setup",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Synthetic Template" in response.text
        assert "Template builder" in response.text

        template_id = response.url.path.rstrip("/").split("/")[-2]

        detail = client.get(f"/templates/{template_id}")
        assert detail.status_code == 200
        assert "Synthetic reusable setup" in detail.text

        archived = client.post(f"/templates/{template_id}/archive", follow_redirects=True)
        assert archived.status_code == 200


def test_builder_uses_state_canvas_shell() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/templates/new",
            data={
                "name": "Canvas Template",
                "assessment_type": "generic_test",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "template-builder-app" in response.text
        assert "/static/js/template-builder.js" in response.text
        assert "Mark-sheet preview" not in response.text
        assert "Save to refresh" not in response.text


def test_builder_state_round_trip_saves_nested_sheet_data() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/templates/new",
            data={
                "name": "State Template",
                "assessment_type": "generic_test",
            },
            follow_redirects=True,
        )
        template_id = int(response.url.path.rstrip("/").split("/")[-2])

        state_response = client.get(f"/templates/{template_id}/builder/state")
        assert state_response.status_code == 200
        state = state_response.json()
        section_id = state["scoring_sections"][0]["id"]
        state["sheets"][0]["name"] = "文字・語彙 Edited"
        state["sheets"][0]["groups"] = [
            {
                "id": None,
                "problem_title": "もんだいA",
                "questions": [
                    {
                        "id": None,
                        "correct_answer": "3",
                        "choice_count": 4,
                        "points": 2,
                        "scoring_section_id": section_id,
                    }
                ],
            }
        ]

        save_response = client.post(f"/templates/{template_id}/builder/state", json=state)
        assert save_response.status_code == 200

        saved = client.get(f"/templates/{template_id}/builder/state").json()
        assert saved["sheets"][0]["name"] == "文字・語彙 Edited"
        assert saved["sheets"][0]["groups"][0]["problem_title"] == "もんだいA"
        question = saved["sheets"][0]["groups"][0]["questions"][0]
        assert question["correct_answer"] == "3"
        assert question["points"] == 2
        assert question["scoring_section_id"] == section_id


def test_builder_state_blocks_deleting_existing_questions_after_runs() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/templates/new",
            data={
                "name": "Used Template",
                "assessment_type": "generic_test",
            },
            follow_redirects=True,
        )
        template_id = int(response.url.path.rstrip("/").split("/")[-2])
        state = client.get(f"/templates/{template_id}/builder/state").json()
        section_id = state["scoring_sections"][0]["id"]
        state["sheets"][0]["groups"] = [
            {
                "id": None,
                "problem_title": "もんだい1",
                "questions": [
                    {
                        "id": None,
                        "correct_answer": "1",
                        "choice_count": 4,
                        "points": 1,
                        "scoring_section_id": section_id,
                    }
                ],
            }
        ]
        saved = client.post(f"/templates/{template_id}/builder/state", json=state).json()["state"]

        run = client.post(
            f"/templates/{template_id}/runs/new",
            data={"run_name": "Used Template Run", "exam_date": ""},
            follow_redirects=False,
        )
        assert run.status_code == 303

        saved["sheets"][0]["groups"][0]["questions"] = []
        blocked = client.post(f"/templates/{template_id}/builder/state", json=saved)
        assert blocked.status_code == 400
        assert "already has test runs" in blocked.json()["error"]


def test_exam_list_still_loads() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/exams")
        assert response.status_code == 200


def test_japanese_run_flow_pages_do_not_show_targeted_hardcoded_english() -> None:
    with TestClient(create_app()) as client:
        client.get("/lang/ja", headers={"referer": "/templates"}, follow_redirects=False)
        template_id = _create_template(client)

        run_new = client.get(f"/templates/{template_id}/runs/new")
        assert run_new.status_code == 200
        assert "1/3：試験情報" in run_new.text
        assert "学生名簿へ進む" in run_new.text
        assert "Step 1 of 3" not in run_new.text
        assert "Continue to roster" not in run_new.text

        create_run = client.post(
            f"/templates/{template_id}/runs/new",
            data={"run_name": "3年A組 2026年7月", "exam_date": ""},
            follow_redirects=False,
        )
        assert create_run.status_code == 303
        exam_id = int(create_run.headers["location"].split("/")[2])

        students = client.get(f"/exams/{exam_id}/students")
        assert students.status_code == 200
        assert "2/3：学生名簿" in students.text
        assert "用紙作成へ進む" in students.text
        assert "Continue to generate sheets" not in students.text
        assert "Roster" not in students.text

        sheets = client.get(f"/exams/{exam_id}/sheets")
        assert sheets.status_code == 200
        assert "3/3：用紙作成" in sheets.text
        assert "最終作成日時" in sheets.text
        assert "まだ作成されていません" in sheets.text
        assert "Step 3 of 3" not in sheets.text
        assert "Last generated" not in sheets.text
        assert "Not generated yet" not in sheets.text

        results = client.get(f"/exams/{exam_id}/results")
        assert results.status_code == 200
        assert "確認を優先したい問題" in results.text
        assert "まだ優先して確認する問題はありません" in results.text
        assert "Feedback priority" not in results.text
