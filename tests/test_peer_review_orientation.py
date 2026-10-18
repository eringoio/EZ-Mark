"""Tests for automatic portrait/landscape selection of peer-review marksheets.

Unit tests exercise the page-plan decision directly; integration tests confirm the
resolved orientation flows consistently into the layout JSON, the generated PDF, and
the saved scan coordinate map, and that normal OMR sheet generation is unaffected.
"""

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import create_app
from app.models import MockExam, PeerReviewForm, PeerReviewPage
from app.services.peer_review_layout_service import calculate_peer_review_page_plan
from tests.pdf_helpers import open_document

# --- Unit tests: the orientation/fit decision ---------------------------------


def _plan(competency_count, score_max=5, max_spp=10, has_furigana=False, mode="auto", score_min=1):
    return calculate_peer_review_page_plan(
        competency_count=competency_count,
        score_min=score_min,
        score_max=score_max,
        max_students_per_page=max_spp,
        has_furigana=has_furigana,
        orientation_mode=mode,
    )


def test_few_competencies_many_students_chooses_portrait() -> None:
    plan = _plan(competency_count=2, max_spp=12)
    assert plan.orientation == "portrait"
    assert plan.fits is True
    assert plan.students_per_page == 12


def test_many_competencies_fewer_students_chooses_landscape() -> None:
    plan = _plan(competency_count=6, max_spp=8)
    assert plan.orientation == "landscape"
    assert plan.fits is True
    assert plan.students_per_page == 8


def test_forced_portrait_works_when_it_fits() -> None:
    plan = _plan(competency_count=2, max_spp=10, mode="portrait")
    assert plan.orientation == "portrait"
    assert plan.fits is True


def test_forced_landscape_works_when_it_fits() -> None:
    plan = _plan(competency_count=2, max_spp=8, mode="landscape")
    assert plan.orientation == "landscape"
    assert plan.fits is True


def test_forced_portrait_warns_when_it_cannot_fit() -> None:
    plan = _plan(competency_count=8, max_spp=10, mode="portrait")
    assert plan.orientation == "portrait"
    assert plan.fits is False
    assert any(w["code"] == "does_not_fit" for w in plan.layout_warnings)


def test_auto_reduces_students_per_page_when_max_is_too_high() -> None:
    # Portrait A4 now fits up to 30 compact rows, so ask for more than that to force a
    # reduction and the localized warning.
    plan = _plan(competency_count=1, max_spp=40)
    assert plan.fits is True
    assert plan.students_per_page < 40
    reduced = [w for w in plan.layout_warnings if w["code"] == "per_page_reduced"]
    assert reduced and reduced[0]["max"] == 40 and reduced[0]["actual"] == plan.students_per_page


def test_auto_errors_clearly_when_nothing_fits() -> None:
    # Too many wide columns to fit even in landscape.
    plan = _plan(competency_count=20, score_max=9, max_spp=10)
    assert plan.fits is False
    assert any(w["code"] == "does_not_fit" for w in plan.layout_warnings)


def test_furigana_does_not_cost_a_row_at_standard_density() -> None:
    # The standard furigana band is sized so furigana no longer costs a row of capacity:
    # 20 full-size rows fit on landscape A4 with or without furigana.
    without = _plan(competency_count=1, max_spp=30, has_furigana=False, mode="landscape")
    with_furigana = _plan(competency_count=1, max_spp=30, has_furigana=True, mode="landscape")
    assert with_furigana.students_per_page == without.students_per_page == 20


# --- Integration: layout JSON / PDF / coordinate map all agree ----------------


def _client() -> TestClient:
    return TestClient(create_app())


def _peer_template_with_competencies(client: TestClient, total: int) -> int:
    response = client.post(
        "/templates/new",
        data={"name": "Orientation PR", "assessment_type": "peer_review"},
        follow_redirects=False,
    )
    template_id = int(response.headers["location"].split("/")[2])
    # Templates seed one competency; add until we reach `total`.
    for index in range(total - 1):
        client.post(
            f"/templates/{template_id}/peer-review/competencies",
            data={"name": f"Competency {index + 2}"},
        )
    return template_id


def _create_run(client: TestClient, template_id: int, name: str) -> int:
    response = client.post(
        f"/templates/{template_id}/runs/new",
        data={"run_name": name, "exam_date": "2026-07-01"},
        follow_redirects=False,
    )
    return int(response.headers["location"].split("/")[2])


def _add_student(client: TestClient, run_id: int, number: str, name: str) -> None:
    client.post(
        f"/exams/{run_id}/students/add-manual",
        data={"student_number": number, "name": name, "class_name": "3A"},
        follow_redirects=False,
    )


def test_layout_json_reports_resolved_landscape_for_many_competencies() -> None:
    with _client() as client:
        template_id = _peer_template_with_competencies(client, total=6)

        layout = client.get(f"/templates/{template_id}/peer-review/layout").json()

        assert layout["page_plan"]["orientation"] == "landscape"
        assert layout["pages"][0]["layout"]["orientation"] == "landscape"
        assert layout["pages"][0]["layout"]["page_width_mm"] == 297.0


def test_generated_pdf_matches_layout_orientation_and_coordinate_map() -> None:
    with _client() as client:
        template_id = _peer_template_with_competencies(client, total=6)
        run_id = _create_run(client, template_id, "Landscape class")
        _add_student(client, run_id, "2505ma04", "aaa")
        _add_student(client, run_id, "2505ma05", "bbb")

        layout = client.get(f"/exams/{run_id}/peer-review/layout").json()
        assert layout["page_plan"]["orientation"] == "landscape"

        generated = client.post(
            f"/exams/{run_id}/peer-review/sheets/generate", follow_redirects=False
        )
        assert generated.status_code == 303

        pdf = client.get(f"/exams/{run_id}/peer-review/sheets/download/latest")
        document = open_document(pdf.content)
        # Landscape: physical page is wider than tall.
        page_width, page_height = document[0].get_size()
        assert page_width > page_height

        db = SessionLocal()
        try:
            run = db.get(MockExam, run_id)
            form = db.query(PeerReviewForm).filter_by(exam_id=run_id).one()
            pages = db.query(PeerReviewPage).filter_by(form_id=form.id).all()
            assert pages
            for page in pages:
                # Scanning reads the saved map, which records the resolved orientation.
                assert page.coordinate_map_json["orientation"] == "landscape"
                assert page.coordinate_map_json["page_width_mm"] == 297.0
            assert run.assessment_type == "peer_review"
        finally:
            db.close()


def test_portrait_session_stays_portrait_end_to_end() -> None:
    with _client() as client:
        template_id = _peer_template_with_competencies(client, total=2)
        run_id = _create_run(client, template_id, "Portrait class")
        _add_student(client, run_id, "2505ma04", "aaa")

        layout = client.get(f"/exams/{run_id}/peer-review/layout").json()
        assert layout["page_plan"]["orientation"] == "portrait"
        assert layout["pages"][0]["layout"]["page_height_mm"] == 297.0


def test_normal_sheet_route_is_not_hijacked_by_peer_review() -> None:
    with _client() as client:
        template = client.post(
            "/templates/new",
            data={"name": "Standard Test", "assessment_type": "generic_test"},
            follow_redirects=False,
        )
        template_id = int(template.headers["location"].split("/")[2])
        run_id = _create_run_fresh(client, template_id)
        _add_student(client, run_id, "1001", "Synthetic A")

        # The normal sheets page renders (no peer-review redirect).
        page = client.get(f"/exams/{run_id}/sheets", follow_redirects=False)
        assert page.status_code == 200
        assert "peer-review" not in page.text

        # The normal generator runs its own answer-key readiness check (a blank template
        # is not ready) and returns the normal 400 page rather than a 303 redirect into
        # peer-review logic.
        generated = client.post(f"/exams/{run_id}/sheets/generate", follow_redirects=False)
        assert generated.status_code == 400
        assert "peer-review" not in generated.text


def _create_run_fresh(client: TestClient, template_id: int) -> int:
    response = client.post(
        f"/templates/{template_id}/runs/new",
        data={"run_name": "Sample run", "exam_date": ""},
        follow_redirects=False,
    )
    return int(response.headers["location"].split("/")[2])
