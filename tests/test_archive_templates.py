"""Template archive lifecycle: hidden by default, read-only, duplicable, exportable."""

import zipfile
from io import BytesIO

from fastapi.testclient import TestClient

from app.main import create_app


def _new_template(client: TestClient, name: str) -> int:
    response = client.post(
        "/templates/new",
        data={"name": name, "assessment_type": "generic_test"},
        follow_redirects=True,
    )
    # Creation lands on /templates/{id}/builder
    return int(response.url.path.rstrip("/").split("/")[-2])


def _archive(client: TestClient, template_id: int) -> None:
    response = client.post(f"/templates/{template_id}/archive", follow_redirects=False)
    assert response.status_code == 303


def test_active_template_visible_archived_hidden_by_default() -> None:
    with TestClient(create_app()) as client:
        _new_template(client, "Active Tpl Visible")
        archived = _new_template(client, "Archived Tpl Hidden")
        _archive(client, archived)

        body = client.get("/templates").text
        assert "Active Tpl Visible" in body
        assert "Archived Tpl Hidden" not in body


def test_archived_template_shows_with_show_and_only() -> None:
    with TestClient(create_app()) as client:
        archived = _new_template(client, "Archived Tpl Filter")
        _archive(client, archived)

        assert "Archived Tpl Filter" in client.get("/templates?archived=show").text
        assert "Archived Tpl Filter" in client.get("/templates?archived=only").text
        # archived=only must exclude active templates
        active = _new_template(client, "Active Tpl NotInOnly")  # noqa: F841
        assert "Active Tpl NotInOnly" not in client.get("/templates?archived=only").text


def test_archived_template_mutations_blocked() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template(client, "Archived Tpl Blocked")
        _archive(client, template_id)

        assert client.get(f"/templates/{template_id}/edit").status_code == 409
        assert client.get(f"/templates/{template_id}/builder").status_code == 409
        assert client.get(f"/templates/{template_id}/runs/new").status_code == 409
        assert (
            client.post(
                f"/templates/{template_id}/runs/new",
                data={"run_name": "Nope", "exam_date": ""},
            ).status_code
            == 409
        )


def test_archived_template_can_be_viewed() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template(client, "Archived Tpl View")
        _archive(client, template_id)
        assert client.get(f"/templates/{template_id}").status_code == 200


def test_archived_template_duplicate_is_active_and_editable() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template(client, "Archived Tpl Dup")
        _archive(client, template_id)

        response = client.post(f"/templates/{template_id}/duplicate", follow_redirects=False)
        assert response.status_code == 303
        copy_path = response.headers["location"]
        # Copy lands on its edit page → editable (active), so 200 not 409.
        assert client.get(copy_path).status_code == 200
        copy_id = int(copy_path.rstrip("/").split("/")[-2])
        assert client.get(f"/templates/{copy_id}/builder").status_code == 200


def test_archived_template_portable_export_zip() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template(client, "Archived Tpl Export")
        _archive(client, template_id)

        response = client.get(f"/templates/{template_id}/portable-export")
        assert response.status_code == 200
        archive = zipfile.ZipFile(BytesIO(response.content))
        assert "manifest.json" in archive.namelist()
        assert "template.json" in archive.namelist()


def test_archived_template_can_be_deleted() -> None:
    with TestClient(create_app()) as client:
        template_id = _new_template(client, "Archived Tpl Delete")
        _archive(client, template_id)
        assert (
            client.post(f"/templates/{template_id}/delete", follow_redirects=False).status_code
            == 303
        )
        assert client.get(f"/templates/{template_id}").status_code == 404
