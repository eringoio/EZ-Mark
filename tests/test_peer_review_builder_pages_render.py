"""Smoke test: both peer-review builder HTML pages render with the new i18n keys.

The feature tests hit the JSON endpoints; this confirms the full HTML builder (which wires
every PR_I18N key) renders for both the template and session builders, and that the
new short-label/ruby/reorder strings resolve (no raw ``peer_review.*`` keys leak through).
"""

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def _peer_template(client: TestClient) -> int:
    response = client.post(
        "/templates/new",
        data={"name": "Render PR", "assessment_type": "peer_review"},
        follow_redirects=False,
    )
    return int(response.headers["location"].split("/")[2])


def test_template_builder_page_renders_with_new_keys() -> None:
    with _client() as client:
        template_id = _peer_template(client)
        page = client.get(f"/templates/{template_id}/peer-review")
        assert page.status_code == 200
        body = page.text
        # New i18n keys are resolved, not leaked as raw dotted keys.
        assert "peer_review.ruby_label" not in body
        assert "peer_review.short_label_label" not in body
        assert "ruby_label" in body  # the PR_I18N entry name is present


def test_session_builder_page_renders_with_new_keys() -> None:
    with _client() as client:
        template_id = _peer_template(client)
        run = client.post(
            f"/templates/{template_id}/runs/new",
            data={"run_name": "Render class", "exam_date": "2026-07-01"},
            follow_redirects=False,
        )
        run_id = int(run.headers["location"].split("/")[2])
        page = client.get(f"/exams/{run_id}/peer-review/builder")
        assert page.status_code == 200
        body = page.text
        assert "peer_review.session_roster_note" not in body
        assert "peer_review.move_up" not in body
