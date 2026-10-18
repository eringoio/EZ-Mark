"""Tests for the peer-review follow-up batch: printed short label (#9), competency
reordering (#13), and the richer page-split / fit explanation data (#11/#12).

These exercise the shared layout service plus the template builder routes (both builders
share the same service and JS, so testing one path covers the geometry/state contract).
"""

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.peer_review_layout_service import calculate_peer_review_page_plan


def _client() -> TestClient:
    return TestClient(create_app())


def _peer_template(client: TestClient) -> int:
    response = client.post(
        "/templates/new",
        data={"name": "Followup PR", "assessment_type": "peer_review"},
        follow_redirects=False,
    )
    return int(response.headers["location"].split("/")[2])


# --- #9 printed short label ----------------------------------------------------


def test_short_label_is_printed_instead_of_full_name() -> None:
    with _client() as client:
        template_id = _peer_template(client)
        base = f"/templates/{template_id}/peer-review"
        long_name = "難しい言葉をわかりやすく説明できた"
        client.post(
            f"{base}/competencies",
            data={"name": long_name, "short_label": "説明", "name_furigana": "せつめい"},
        )
        for index in range(4):  # narrow the columns
            client.post(f"{base}/competencies", data={"name": f"Item {index}"})

        columns = client.get(f"{base}/layout").json()["pages"][0]["layout"]["competency_columns"]
        column = next(c for c in columns if c["name"] == long_name)
        # The printed header renders the short label; the full name stays as the identity.
        assert "".join(column["header_lines"]) == "説明"
        assert column["header_too_long"] is False

        state = client.get(f"{base}/builder/state").json()
        comp = next(c for c in state["competencies"] if c["name"] == long_name)
        assert comp["short_label"] == "説明"
        assert comp["name"] == long_name


def test_short_label_can_be_cleared() -> None:
    with _client() as client:
        template_id = _peer_template(client)
        base = f"/templates/{template_id}/peer-review"
        resp = client.post(f"{base}/competencies", data={"name": "Teamwork", "short_label": "TW"})
        assert resp.status_code in (200, 303)
        state = client.get(f"{base}/builder/state").json()
        comp = next(c for c in state["competencies"] if c["name"] == "Teamwork")
        assert comp["short_label"] == "TW"
        client.post(
            f"{base}/competencies/{comp['id']}", data={"name": "Teamwork", "short_label": "  "}
        )
        state = client.get(f"{base}/builder/state").json()
        comp = next(c for c in state["competencies"] if c["name"] == "Teamwork")
        assert comp["short_label"] == ""  # blank stored as NULL -> "" in state


# --- #13 reordering ------------------------------------------------------------


def test_move_competency_reorders_and_renumbers() -> None:
    with _client() as client:
        template_id = _peer_template(client)
        base = f"/templates/{template_id}/peer-review"
        # Template seeds one competency ("Overall"); add two more.
        client.post(f"{base}/competencies", data={"name": "Alpha"})
        client.post(f"{base}/competencies", data={"name": "Beta"})
        before = [c["name"] for c in client.get(f"{base}/builder/state").json()["competencies"]]
        assert len(before) == 3

        first_id = client.get(f"{base}/builder/state").json()["competencies"][0]["id"]
        client.post(f"{base}/competencies/{first_id}/move", data={"direction": "down"})

        state = client.get(f"{base}/builder/state").json()
        names = [c["name"] for c in state["competencies"]]
        assert names[0] == before[1]
        assert names[1] == before[0]
        # display_order is renumbered contiguously from 1.
        assert [c["display_order"] for c in state["competencies"]] == [1, 2, 3]


def test_move_competency_at_edge_is_noop() -> None:
    with _client() as client:
        template_id = _peer_template(client)
        base = f"/templates/{template_id}/peer-review"
        client.post(f"{base}/competencies", data={"name": "Alpha"})
        state = client.get(f"{base}/builder/state").json()
        first_id = state["competencies"][0]["id"]
        order_before = [c["name"] for c in state["competencies"]]
        client.post(f"{base}/competencies/{first_id}/move", data={"direction": "up"})
        order_after = [
            c["name"] for c in client.get(f"{base}/builder/state").json()["competencies"]
        ]
        assert order_after == order_before


def test_move_competency_bad_direction_is_rejected() -> None:
    with _client() as client:
        template_id = _peer_template(client)
        base = f"/templates/{template_id}/peer-review"
        first_id = client.get(f"{base}/builder/state").json()["competencies"][0]["id"]
        resp = client.post(
            f"{base}/competencies/{first_id}/move", data={"direction": "sideways"}
        )
        assert resp.status_code == 400


# --- #11/#12 richer fit explanation -------------------------------------------


def test_plan_exposes_what_if_capacities() -> None:
    plan = calculate_peer_review_page_plan(
        competency_count=5,
        score_min=1,
        score_max=5,
        max_students_per_page=20,
        has_furigana=True,
        orientation_mode="landscape",
        density="standard",
    )
    # Standard density now fits all 20 even with furigana (the band was tightened), so the
    # "hide furigana" lever is no longer needed (no_furigana == the actual per-page)...
    assert plan.students_per_page == 20
    assert plan.students_per_page_no_furigana == 20
    # ...and the "what if compact" capacity is still exposed for the builder's fix hints.
    assert plan.students_per_page_compact >= 20
    payload = plan.as_map()
    assert payload["students_per_page_no_furigana"] == 20
    assert payload["students_per_page_compact"] >= 20
