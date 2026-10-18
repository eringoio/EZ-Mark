"""Focused tests for the peer-review builder fixes.

Covers: peer-review never uses the normal ``/sheets`` flow, roster -> target sync
produces real student labels (no placeholders), zero-target PDF generation fails
loud, the generated PDF carries a localized header, the shared layout exposes clear
competency column separators, competency furigana round-trips into the layout/preview,
and the Japanese session builder does not leak targeted hardcoded English.
"""

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import create_app
from app.models import MockExam, PeerReviewForm, PeerReviewTarget
from tests.pdf_helpers import extract_text, open_document, page_count


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


def _create_run(client: TestClient, template_id: int, name: str, exam_date: str = "") -> int:
    response = client.post(
        f"/templates/{template_id}/runs/new",
        data={"run_name": name, "exam_date": exam_date},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].split("/")[2])


def _add_student(
    client: TestClient, run_id: int, number: str, name: str, klass: str = "3A"
) -> None:
    response = client.post(
        f"/exams/{run_id}/students/add-manual",
        data={"student_number": number, "name": name, "class_name": klass},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def _create_generic_run(client: TestClient) -> int:
    template = client.post(
        "/templates/new",
        data={"name": "Standard Test", "assessment_type": "generic_test"},
        follow_redirects=False,
    )
    template_id = int(template.headers["location"].split("/")[2])
    return _create_run(client, template_id, "Standard run")


# --- Routing: peer review must not use the normal /sheets flow -----------------


def test_peer_review_sheets_get_redirects_to_builder() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        run_id = _create_run(client, template_id, "Class A")

        response = client.get(f"/exams/{run_id}/sheets", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == f"/exams/{run_id}/peer-review/builder"


def test_peer_review_normal_generate_post_redirects_to_builder() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        run_id = _create_run(client, template_id, "Class A")

        response = client.post(f"/exams/{run_id}/sheets/generate", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == f"/exams/{run_id}/peer-review/builder"


def test_normal_exam_still_uses_normal_sheets_route() -> None:
    with _client() as client:
        run_id = _create_generic_run(client)

        response = client.get(f"/exams/{run_id}/sheets", follow_redirects=False)

        assert response.status_code == 200
        assert "peer-review" not in response.text


def test_peer_review_students_continue_links_to_peer_review_builder() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        run_id = _create_run(client, template_id, "Class A")
        _add_student(client, run_id, "2505ma04", "aaa")

        page = client.get(f"/exams/{run_id}/students")

        assert page.status_code == 200
        assert f'href="/exams/{run_id}/peer-review/builder"' in page.text
        # The peer-review roster must not push staff into the normal sheet generator.
        assert f'href="/exams/{run_id}/sheets"' not in page.text


# --- Target sync: real roster, no placeholders --------------------------------


def test_target_sync_uses_order_number_and_name() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        run_id = _create_run(client, template_id, "Class A")
        _add_student(client, run_id, "2505ma04", "aaa")
        _add_student(client, run_id, "2505ma05", "bbb")

        layout = client.get(f"/exams/{run_id}/peer-review/layout").json()
        labels = [
            row["display_label"]
            for page in layout["pages"]
            for row in page["layout"]["target_rows"]
        ]

        # Labels are order number + name; the student code is never included.
        assert "1. aaa" in labels
        assert "2. bbb" in labels
        assert not any("2505ma04" in label or "2505ma05" in label for label in labels)
        # No "Student N" placeholders once real students exist.
        assert not any(label.startswith("Student ") for label in labels)
        assert layout["placeholder"] is False

        db = SessionLocal()
        try:
            run = db.get(MockExam, run_id)
            form = db.query(PeerReviewForm).filter_by(exam_id=run_id).one()
            targets = db.query(PeerReviewTarget).filter_by(form_id=form.id).all()
            assert {t.display_label for t in targets} == {"1. aaa", "2. bbb"}
            assert run.assessment_type == "peer_review"
        finally:
            db.close()


def test_layout_placeholder_only_before_any_students() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        run_id = _create_run(client, template_id, "Empty class")

        layout = client.get(f"/exams/{run_id}/peer-review/layout").json()

        assert layout["placeholder"] is True
        assert layout["active_target_count"] == 0


# --- PDF generation -----------------------------------------------------------


def test_peer_review_pdf_fails_clearly_with_zero_targets() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        run_id = _create_run(client, template_id, "Empty class")

        response = client.post(
            f"/exams/{run_id}/peer-review/sheets/generate", follow_redirects=False
        )

        assert response.status_code == 400
        assert "No PDF was created" in response.text


def test_peer_review_pdf_header_is_localized_and_has_pages() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        run_id = _create_run(client, template_id, "Class A", exam_date="2026-07-01")
        _add_student(client, run_id, "2505ma04", "aaa")
        _add_student(client, run_id, "2505ma05", "bbb")

        generated = client.post(
            f"/exams/{run_id}/peer-review/sheets/generate", follow_redirects=False
        )
        assert generated.status_code == 303

        pdf = client.get(f"/exams/{run_id}/peer-review/sheets/download/latest")
        assert pdf.status_code == 200
        document = open_document(pdf.content)
        assert page_count(document) >= 1
        text = extract_text(document, 0)
        document.close()
        # The session title appears exactly once: the old duplicate "Test: <name>" /
        # "テスト名: <name>" sub-line was removed so the header is not redundant.
        assert text.count("Class A") == 1
        assert "テスト名" not in text
        # Compact value-only sub-line: bare date + "X / Y" page number, with no 実施日 / ページ
        # (or "Date" / "Page") labels cluttering the header.
        assert "2026-07-01" in text
        assert "1 / 1" in text
        assert "Date" not in text
        assert "Page" not in text
        assert "印刷スケール" not in text
        assert "Print at 100% scale" not in text
        # Order number + name printed, no student code and no placeholder rows.
        assert "1. aaa" in text
        assert "2505ma04" not in text
        assert "Student 1" not in text


def test_builder_loads_after_adding_students_to_a_generated_form() -> None:
    """Viewing the builder must never 500 when the printed layout is locked.

    Regression: generating sheets locks the layout; adding a student afterwards previously
    made the builder page hard-fail (the roster sync raised). The builder must still render,
    and surface a non-fatal notice telling the teacher to recreate the sheet.
    """
    with _client() as client:
        template_id = _create_peer_template(client)
        run_id = _create_run(client, template_id, "Class A", exam_date="2026-07-01")
        _add_student(client, run_id, "2505ma04", "aaa")
        _add_student(client, run_id, "2505ma05", "bbb")

        generated = client.post(
            f"/exams/{run_id}/peer-review/sheets/generate", follow_redirects=False
        )
        assert generated.status_code == 303

        # New student added after the sheet was generated (layout now locked).
        _add_student(client, run_id, "2505ma06", "ccc")

        page = client.get(f"/exams/{run_id}/peer-review/builder")
        assert page.status_code == 200
        # The non-fatal "recreate the sheet to include new students" notice is shown.
        assert "pr-banner is-warn" in page.text


# --- Layout: clear competency column separators -------------------------------


def test_layout_payload_exposes_column_separators_and_score_positions() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        client.post(
            f"/templates/{template_id}/peer-review/competencies",
            data={"name": "Delivery"},
        )

        layout = client.get(f"/templates/{template_id}/peer-review/layout").json()
        page_layout = layout["pages"][0]["layout"]

        # One separator left of the target column, one between target/ratings, and one
        # after each competency column.
        assert (
            len(page_layout["column_separators_mm"]) >= len(page_layout["competency_columns"]) + 2
        )
        assert page_layout["target_column"]["width_mm"] > 0
        for column in page_layout["competency_columns"]:
            assert column["score_positions"], "each competency exposes its score bubble x positions"
            assert column["width_mm"] > 0


# --- Furigana -----------------------------------------------------------------


def test_competency_furigana_saved_and_in_layout_and_preview() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        client.post(
            f"/templates/{template_id}/peer-review/competencies",
            data={"name": "発表", "name_furigana": "はっぴょう"},
        )

        layout = client.get(f"/templates/{template_id}/peer-review/layout").json()
        furiganas = [c["name_furigana"] for c in layout["pages"][0]["layout"]["competency_columns"]]
        assert "はっぴょう" in furiganas

        # The server-rendered template preview (SVG) shows the furigana text.
        builder = client.get(f"/templates/{template_id}/peer-review")
        assert "はっぴょう" in builder.text


# --- Localization -------------------------------------------------------------


def test_japanese_session_builder_has_no_targeted_hardcoded_english() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        run_id = _create_run(client, template_id, "Class A")
        _add_student(client, run_id, "2505ma04", "aaa")

        page = client.get(
            f"/exams/{run_id}/peer-review/builder",
            headers={"accept-language": "ja"},
        )

        assert page.status_code == 200
        # Server-rendered labels are localized (the JS builder i18n payload carries the
        # same keys as JSON for the client to render directly on the editable sheet).
        assert "PDFを作成" in page.text  # generate PDF button
        assert "シート上の列をクリック" in page.text  # on-sheet edit hint (edit_on_sheet_hint)
        assert "匿名ピア評価" in page.text  # anonymous peer review pill
        assert "Generate PDF" not in page.text
        assert "Rater groups" not in page.text
