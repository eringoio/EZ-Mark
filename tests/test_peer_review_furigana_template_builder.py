"""Peer-review furigana clarity + JS-driven template builder.

Covers two fixes:

1. Competency furigana (``Competency.name_furigana``) is the reading shown *above* the
   competency column title on the printed/preview header — never student furigana. The
   layout's single source of truth places furigana above the name, and that geometry is
   shared by the PDF and the SVG preview.
2. The peer-review *template* builder is a JS app shell (initial state/layout JSON +
   ``window.PR_I18N`` + ``peer-review-builder.js``) with ``data-base`` pointing at the
   template peer-review routes, mirroring the session builder. Templates have no real
   roster, so the builder shows a note instead of a roster editor.
"""

from fastapi.testclient import TestClient

from app.main import create_app

# The competency example from the spec: a spoken-performance criterion with its reading.
COMP_NAME = "時間内に話すことができた"
COMP_FURIGANA = "じかんないにはなすことができた"


def _client() -> TestClient:
    return TestClient(create_app())


def _create_peer_template(client: TestClient, name: str = "PR template") -> int:
    response = client.post(
        "/templates/new",
        data={"name": name, "assessment_type": "peer_review"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].split("/")[2])


# --- Furigana: reading shown above the competency column title -----------------


def test_competency_furigana_persists_and_sits_above_name() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        client.post(
            f"/templates/{template_id}/peer-review/competencies",
            data={"name": COMP_NAME, "name_furigana": COMP_FURIGANA},
        )

        # Persisted on the competency and exposed via the builder state.
        state = client.get(f"/templates/{template_id}/peer-review/builder/state").json()
        comp = next(c for c in state["competencies"] if c["name"] == COMP_NAME)
        assert comp["name_furigana"] == COMP_FURIGANA

        # Present in the layout payload that drives the PDF and the SVG preview.
        layout = client.get(f"/templates/{template_id}/peer-review/layout").json()
        page_layout = layout["pages"][0]["layout"]
        column = next(c for c in page_layout["competency_columns"] if c["name"] == COMP_NAME)
        assert column["name_furigana"] == COMP_FURIGANA

        # Single source of truth: furigana is drawn above the name (smaller y = higher).
        # Both the ReportLab PDF and the JS/Jinja SVG read these same coordinates.
        assert page_layout["furigana_y_mm"] < page_layout["name_y_mm"]
        assert layout["page_plan"]["has_furigana"] is True

        # The builder page embeds the furigana so the client preview renders it.
        page = client.get(f"/templates/{template_id}/peer-review")
        assert COMP_FURIGANA in page.text


def test_editing_competency_furigana_through_template_route_persists() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        state = client.get(f"/templates/{template_id}/peer-review/builder/state").json()
        comp = state["competencies"][0]
        assert comp["name_furigana"] == ""  # seeded competency has no furigana yet

        client.post(
            f"/templates/{template_id}/peer-review/competencies/{comp['id']}",
            data={"name": comp["name"], "name_furigana": COMP_FURIGANA},
        )

        updated = client.get(f"/templates/{template_id}/peer-review/builder/state").json()
        assert updated["competencies"][0]["name_furigana"] == COMP_FURIGANA


def test_adding_first_furigana_flips_has_furigana_for_layout_geometry() -> None:
    # Furigana adds a header band, so the first furigana is a layout-changing edit.
    with _client() as client:
        template_id = _create_peer_template(client)
        before = client.get(f"/templates/{template_id}/peer-review/layout").json()
        assert before["page_plan"]["has_furigana"] is False

        comp_id = before["competencies"][0]["id"]
        name = before["competencies"][0]["name"]
        client.post(
            f"/templates/{template_id}/peer-review/competencies/{comp_id}",
            data={"name": name, "name_furigana": COMP_FURIGANA},
        )

        after = client.get(f"/templates/{template_id}/peer-review/layout").json()
        assert after["page_plan"]["has_furigana"] is True


# --- JS-driven template builder shell -----------------------------------------


def test_template_builder_exposes_js_app_shell() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        page = client.get(f"/templates/{template_id}/peer-review")

        assert page.status_code == 200
        assert 'id="pr-builder-app"' in page.text
        assert f'data-base="/templates/{template_id}/peer-review"' in page.text
        assert "/static/js/peer-review-builder.js" in page.text
        assert "window.PR_I18N" in page.text


def test_template_builder_embeds_initial_state_and_layout_json() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        page = client.get(f"/templates/{template_id}/peer-review")

        assert 'id="pr-initial-state"' in page.text
        assert 'id="pr-initial-layout"' in page.text
        # The embedded state flags template mode so the JS hides the roster editor.
        assert '"is_template": true' in page.text


def test_template_builder_state_endpoint_returns_template_state() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        state = client.get(f"/templates/{template_id}/peer-review/builder/state").json()

        assert state["is_template"] is True
        assert state["competencies"]  # seeded default competency
        assert state["targets"] == []  # templates carry no real roster


def test_template_builder_state_post_saves_settings() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        response = client.post(
            f"/templates/{template_id}/peer-review/builder/state",
            json={
                "title": "PR",
                "score_min": 1,
                "score_max": 4,
                "students_per_page": 6,
                "page_orientation_mode": "auto",
            },
        )
        assert response.status_code == 200
        saved = response.json()
        assert saved["form"]["score_max"] == 4
        assert saved["form"]["students_per_page"] == 6
        # Placeholder preview follows the new students-per-page (capped by what fits).
        layout = client.get(f"/templates/{template_id}/peer-review/layout").json()
        assert layout["pages"][0]["layout"]["target_rows"]


def test_template_builder_shows_roster_note_not_roster_editor() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        page = client.get(f"/templates/{template_id}/peer-review")

        # A note explains rosters come later instead of a roster/target editing UI.
        assert "Student rosters are added after creating a session" in page.text
        # No server-rendered roster/target editing affordances in template mode.
        assert 'data-action="target-active"' not in page.text
        assert 'data-action="target-label"' not in page.text


def test_template_builder_japanese_furigana_label_is_localized() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        page = client.get(
            f"/templates/{template_id}/peer-review",
            headers={"accept-language": "ja"},
        )

        assert page.status_code == 200
        # The new furigana label makes clear it shows above the competency column title.
        assert "ふりがな（評価項目の上に表示）" in page.text
        assert "テンプレートでは学生名簿は設定しません" in page.text
        # No hardcoded English for the new labels.
        assert "Furigana / reading shown above the column title" not in page.text
        assert "Student rosters are added after creating a session" not in page.text
