"""Tests for per-segment ruby furigana (§2/#8).

Per-segment ruby attaches a small reading above just its base run in the printed
competency title (e.g. 時間内→じかんない). The shared layout service resolves each reading
to an absolute position so the PDF and the browser SVG preview match. These tests cover the
data round-trip, the resolved positions, the whole-title fallback, the short-label
suppression rule, template→session carry, and the PDF render path.
"""

import json

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.peer_review_layout_service import _ruby_positions, fit_competency_header


def _client() -> TestClient:
    return TestClient(create_app())


def _peer_template(client: TestClient) -> int:
    response = client.post(
        "/templates/new",
        data={"name": "Ruby PR", "assessment_type": "peer_review"},
        follow_redirects=False,
    )
    return int(response.headers["location"].split("/")[2])


def _dumps(ruby: list) -> str:
    return json.dumps(ruby, ensure_ascii=False)


# --- Unit: positioning --------------------------------------------------------


def test_ruby_positions_centre_over_base_runs_left_to_right() -> None:
    title = "時間内に話せた"
    fit = fit_competency_header(title, 80.0)  # wide -> single line at normal size
    segments = [
        {"base_text": "時間内", "reading": "じかんない"},
        {"base_text": "話", "reading": "はな"},
    ]
    positions = _ruby_positions(title, segments, fit.lines, fit.font_pt, 50.0, 20.0)
    assert [p["reading"] for p in positions] == ["じかんない", "はな"]
    # 時間内 starts the title, 話 comes later -> readings run left to right.
    assert positions[0]["x_mm"] < positions[1]["x_mm"]
    # Readings sit above the title baseline (smaller y == higher on the page).
    assert all(p["y_mm"] < 20.0 for p in positions)


def test_ruby_base_not_in_title_is_skipped() -> None:
    title = "時間内に話せた"
    fit = fit_competency_header(title, 80.0)
    segments = [
        {"base_text": "xyz", "reading": "abc"},  # not present -> skipped
        {"base_text": "話", "reading": "はな"},
    ]
    positions = _ruby_positions(title, segments, fit.lines, fit.font_pt, 50.0, 20.0)
    assert [p["reading"] for p in positions] == ["はな"]


# --- Integration: data round-trip + layout ------------------------------------


def test_ruby_round_trips_and_resolves_in_layout() -> None:
    with _client() as client:
        template_id = _peer_template(client)
        base = f"/templates/{template_id}/peer-review"
        ruby = [
            {"base_text": "時間内", "reading": "じかんない"},
            {"base_text": "話", "reading": "はな"},
        ]
        client.post(
            f"{base}/competencies",
            data={"name": "時間内に話せた", "name_ruby_json": _dumps(ruby)},
        )
        state = client.get(f"{base}/builder/state").json()
        comp = next(c for c in state["competencies"] if c["name"] == "時間内に話せた")
        assert comp["name_ruby"] == ruby

        layout = client.get(f"{base}/layout").json()
        column = next(
            c
            for c in layout["pages"][0]["layout"]["competency_columns"]
            if c["name"] == "時間内に話せた"
        )
        readings = [r["reading"] for r in column["ruby_positions"]]
        assert readings == ["じかんない", "はな"]
        # Any reading reserves the taller (furigana) header band.
        assert layout["page_plan"]["has_furigana"] is True


def test_short_label_suppresses_per_segment_ruby() -> None:
    with _client() as client:
        template_id = _peer_template(client)
        base = f"/templates/{template_id}/peer-review"
        ruby = [{"base_text": "時間内", "reading": "じかんない"}]
        client.post(
            f"{base}/competencies",
            data={
                "name": "時間内に話せた",
                "short_label": "発表",
                "name_furigana": "はっぴょう",
                "name_ruby_json": _dumps(ruby),
            },
        )
        column = next(
            c
            for c in client.get(f"{base}/layout").json()["pages"][0]["layout"]["competency_columns"]
            if c["name"] == "時間内に話せた"
        )
        # Per-segment ruby is suppressed (its runs index into the full name, not the short
        # label); the printed header is the short label, with the whole-title furigana shown.
        assert column["ruby_positions"] == []
        assert "".join(column["header_lines"]) == "発表"
        assert column["name_furigana"] == "はっぴょう"


def test_clearing_ruby_removes_it() -> None:
    with _client() as client:
        template_id = _peer_template(client)
        base = f"/templates/{template_id}/peer-review"
        ruby = [{"base_text": "話", "reading": "はな"}]
        client.post(f"{base}/competencies", data={"name": "話せた", "name_ruby_json": _dumps(ruby)})
        comp = next(
            c
            for c in client.get(f"{base}/builder/state").json()["competencies"]
            if c["name"] == "話せた"
        )
        assert comp["name_ruby"] == ruby
        client.post(
            f"{base}/competencies/{comp['id']}",
            data={"name": "話せた", "name_ruby_json": "[]"},
        )
        comp = next(
            c
            for c in client.get(f"{base}/builder/state").json()["competencies"]
            if c["name"] == "話せた"
        )
        assert comp["name_ruby"] == []


def test_ruby_carries_to_session_and_pdf_generates() -> None:
    with _client() as client:
        template_id = _peer_template(client)
        base_t = f"/templates/{template_id}/peer-review"
        ruby = [{"base_text": "時間内", "reading": "じかんない"}]
        client.post(
            f"{base_t}/competencies",
            data={"name": "時間内に話せた", "name_ruby_json": _dumps(ruby)},
        )
        run = client.post(
            f"/templates/{template_id}/runs/new",
            data={"run_name": "Ruby class", "exam_date": "2026-07-01"},
            follow_redirects=False,
        )
        run_id = int(run.headers["location"].split("/")[2])
        client.post(
            f"/exams/{run_id}/students/add-manual",
            data={"student_number": "s01", "name": "AAA 1", "class_name": "3A"},
            follow_redirects=False,
        )
        # The copied session form keeps the per-segment ruby.
        state = client.get(f"/exams/{run_id}/peer-review/builder/state").json()
        comp = next(c for c in state["competencies"] if c["name"] == "時間内に話せた")
        assert comp["name_ruby"] == ruby
        # The PDF render path (which draws each ruby) succeeds end to end.
        generated = client.post(
            f"/exams/{run_id}/peer-review/sheets/generate", follow_redirects=False
        )
        assert generated.status_code == 303
