"""Tests for the compact + auto-fit layout batch.

Covers layout density profiles (standard reproduces the legacy geometry; compact fits
more rows; relaxed fewer), the auto-width target/name column, and the deterministic
competency-title auto-fit (wrap to two lines, shrink, flag when it still overflows).
Integration tests confirm density round-trips through the builder route and that a
compact sheet fits 20 students with furigana end-to-end.
"""

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.peer_review_layout_service import (
    HEADER_BAND_FURIGANA_MM,
    HEADER_BAND_PLAIN_MM,
    HEADER_TOP_MM,
    ROW_HEIGHT_MM,
    TARGET_COLUMN_MAX_MM,
    TARGET_COLUMN_MIN_MM,
    TARGET_COLUMN_WIDTH_MM,
    calculate_peer_review_page_plan,
    fit_competency_header,
)
from app.services.peer_review_layout_service import (
    _target_column_width_mm as target_column_width_mm,
)


def _plan(density="standard", has_furigana=False, max_spp=20, competency_count=5):
    return calculate_peer_review_page_plan(
        competency_count=competency_count,
        score_min=1,
        score_max=5,
        max_students_per_page=max_spp,
        has_furigana=has_furigana,
        orientation_mode="landscape",
        density=density,
    )


# --- Density profiles ---------------------------------------------------------


def test_standard_density_reproduces_legacy_geometry() -> None:
    plan = _plan(density="standard", has_furigana=False)
    assert plan.row_height_mm == ROW_HEIGHT_MM
    assert plan.header_top_mm == HEADER_TOP_MM
    assert plan.table_top_mm == HEADER_TOP_MM + HEADER_BAND_PLAIN_MM
    # Landscape A4 historically fits exactly 20 rows for 5 criteria, scale 1-5, no furigana.
    assert plan.students_per_page == 20


def test_standard_furigana_now_fits_twenty() -> None:
    standard_plain = _plan(density="standard", has_furigana=False)
    standard = _plan(density="standard", has_furigana=True)
    compact = _plan(density="compact", has_furigana=True)
    # The furigana band is sized so it no longer costs the 20th row at standard density:
    # 20 full-size rows fit on landscape A4 with furigana, matching the plain case.
    assert standard.students_per_page == 20
    assert standard_plain.students_per_page == 20  # plain standard is unchanged
    assert standard.table_top_mm == HEADER_TOP_MM + HEADER_BAND_FURIGANA_MM
    assert standard.row_height_mm == ROW_HEIGHT_MM  # full-size rows, not shrunk
    # Compact still fits at least 20 (it shrinks rows to gain headroom for more).
    assert compact.students_per_page >= 20
    assert compact.density == "compact"
    assert compact.row_height_mm < ROW_HEIGHT_MM


def test_relaxed_density_fits_fewer_rows_than_standard() -> None:
    standard = _plan(density="standard", has_furigana=False, max_spp=30)
    relaxed = _plan(density="relaxed", has_furigana=False, max_spp=30)
    assert relaxed.students_per_page < standard.students_per_page


def test_unknown_density_falls_back_to_standard() -> None:
    plan = _plan(density="nonsense")
    assert plan.density == "standard"
    assert plan.row_height_mm == ROW_HEIGHT_MM


# --- Auto-width target/name column --------------------------------------------


def test_target_column_narrows_for_short_names_and_clamps() -> None:
    short = target_column_width_mm(["1. AAA 1", "2. AAA 2", "3. AAA 3"])
    long = target_column_width_mm(["12. ウォニンク・エリック・ファン・デル・メール"])
    assert short == TARGET_COLUMN_MIN_MM  # clamps to the minimum, far below the old 44 mm
    assert short < long
    assert TARGET_COLUMN_MIN_MM <= long <= TARGET_COLUMN_MAX_MM
    # No samples (a template with no roster) keeps the familiar fallback width.
    assert target_column_width_mm(None) == TARGET_COLUMN_WIDTH_MM


def test_narrow_names_give_competency_columns_more_width() -> None:
    wide_names = _plan()  # default fallback target column (44 mm)
    narrow = calculate_peer_review_page_plan(
        competency_count=5,
        score_min=1,
        score_max=5,
        max_students_per_page=20,
        has_furigana=False,
        orientation_mode="landscape",
        density="standard",
        target_label_samples=["1. A", "2. B"],
    )
    assert narrow.target_column_width_mm < wide_names.target_column_width_mm


# --- Competency-title auto-fit ------------------------------------------------


def test_short_title_stays_one_line_at_normal_size() -> None:
    fit = fit_competency_header("発表", 40)
    assert fit.lines == ("発表",)
    assert fit.font_pt == 8.0
    assert fit.too_long is False


def test_long_title_wraps_to_two_lines_and_shrinks() -> None:
    fit = fit_competency_header("難しい言葉をわかりやすく説明できた", 32)
    assert len(fit.lines) == 2
    assert fit.font_pt < 8.0
    assert fit.too_long is False
    # Each wrapped line is a non-empty slice of the original, in order.
    assert "".join(fit.lines) == "難しい言葉をわかりやすく説明できた"


def test_title_too_narrow_is_flagged_too_long() -> None:
    fit = fit_competency_header("難しい言葉をわかりやすく説明できた", 18)
    assert fit.too_long is True
    assert len(fit.lines) == 2


# --- Integration via the builder routes ---------------------------------------


def _client() -> TestClient:
    return TestClient(create_app())


def _peer_template(client: TestClient) -> int:
    response = client.post(
        "/templates/new",
        data={"name": "Compact PR", "assessment_type": "peer_review"},
        follow_redirects=False,
    )
    return int(response.headers["location"].split("/")[2])


def test_density_round_trips_through_builder_state() -> None:
    with _client() as client:
        template_id = _peer_template(client)
        base = f"/templates/{template_id}/peer-review"

        saved = client.post(f"{base}/builder/state", json={"layout_density": "compact"})
        assert saved.status_code == 200
        assert saved.json()["form"]["layout_density"] == "compact"

        # The resolved plan in the layout payload reflects the chosen density.
        layout = client.get(f"{base}/layout").json()
        assert layout["page_plan"]["density"] == "compact"

        bad = client.post(f"{base}/builder/state", json={"layout_density": "huge"})
        assert bad.status_code == 400


def test_layout_payload_exposes_auto_fit_header_lines() -> None:
    with _client() as client:
        template_id = _peer_template(client)
        base = f"/templates/{template_id}/peer-review"
        client.post(
            f"{base}/competencies",
            data={"name": "難しい言葉をわかりやすく説明できた"},
        )
        # Add a few more so the columns are narrow enough to force wrapping.
        for index in range(4):
            client.post(f"{base}/competencies", data={"name": f"Item {index}"})

        columns = client.get(f"{base}/layout").json()["pages"][0]["layout"]["competency_columns"]
        long_col = next(c for c in columns if c["name"].startswith("難しい"))
        assert len(long_col["header_lines"]) == 2
        assert long_col["header_font_pt"] <= 8.0


def test_compact_session_fits_twenty_with_furigana_end_to_end() -> None:
    with _client() as client:
        template_id = _peer_template(client)
        base_t = f"/templates/{template_id}/peer-review"
        # Five competencies, one with furigana (flips the taller band), compact density.
        client.post(
            f"{base_t}/competencies",
            data={"name": "時間内に話せた", "name_furigana": "じかんない"},
        )
        for index in range(3):
            client.post(f"{base_t}/competencies", data={"name": f"Item {index}"})
        # Compact density lifts the per-page capacity; the teacher still asks for 20/page.
        client.post(
            f"{base_t}/builder/state",
            json={"layout_density": "compact", "students_per_page": 20},
        )

        run = client.post(
            f"/templates/{template_id}/runs/new",
            data={"run_name": "Compact class", "exam_date": "2026-07-01"},
            follow_redirects=False,
        )
        run_id = int(run.headers["location"].split("/")[2])
        for n in range(1, 21):  # 20 students
            client.post(
                f"/exams/{run_id}/students/add-manual",
                data={"student_number": f"s{n:02d}", "name": f"AAA {n}", "class_name": "3A"},
                follow_redirects=False,
            )

        layout = client.get(f"/exams/{run_id}/peer-review/layout").json()
        assert layout["page_plan"]["density"] == "compact"
        assert layout["page_plan"]["has_furigana"] is True
        # All 20 students fit on a single page under compact density.
        assert len(layout["pages"]) == 1
        assert len(layout["pages"][0]["layout"]["target_rows"]) == 20

        generated = client.post(
            f"/exams/{run_id}/peer-review/sheets/generate", follow_redirects=False
        )
        assert generated.status_code == 303
