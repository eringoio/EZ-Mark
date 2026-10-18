"""Rendering tests for the redesigned, localized peer-review results page.

Covers the empty state, a session with group + competency rankings (no combined ranking),
and that the Japanese page does not leak the old hardcoded English section labels.
"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import create_app
from app.models import Competency, MockExam, PeerReviewRating
from app.services.peer_review_builder_service import get_or_create_form
from app.services.peer_review_scoring_service import (
    recalculate_anonymous_peer_review_results,
)


def _client() -> TestClient:
    return TestClient(create_app())


def _peer_run_with_ratings(client: TestClient) -> int:
    tpl = client.post(
        "/templates/new",
        data={"name": "PR Results", "assessment_type": "peer_review"},
        follow_redirects=False,
    )
    template_id = int(tpl.headers["location"].split("/")[2])
    run = client.post(
        f"/templates/{template_id}/runs/new",
        data={"run_name": "Class A", "exam_date": "2026-07-01"},
        follow_redirects=False,
    )
    run_id = int(run.headers["location"].split("/")[2])
    for number, name in (("PR-RESULTS-S1", "Alice"), ("PR-RESULTS-S2", "Bob")):
        client.post(
            f"/exams/{run_id}/students/add-manual",
            data={"student_number": number, "name": name, "class_name": "3A"},
            follow_redirects=False,
        )
    # Generate sheets so the form/pages/targets exist.
    client.post(
        f"/exams/{run_id}/peer-review/sheets/generate", headers={"Accept": "application/json"}
    )

    # Seed clean ratings directly, then recalculate.
    db = SessionLocal()
    try:
        exam = db.get(MockExam, run_id)
        form = get_or_create_form(db, exam)
        comp_id = db.scalar(select(Competency.id).where(Competency.exam_id == run_id))
        group_id = form.rater_groups[0].id
        for target in form.targets:
            for score in (4, 5):
                db.add(
                    PeerReviewRating(
                        exam_id=run_id,
                        form_id=form.id,
                        batch_id=0,
                        rater_group_id=group_id,
                        submission_page_id=0,
                        peer_review_page_id=form.pages[0].id,
                        target_student_id=target.student_id,
                        competency_id=comp_id,
                        detected_score=score,
                        final_score=score,
                        status="ok",
                    )
                )
        db.commit()
        recalculate_anonymous_peer_review_results(db, form)
    finally:
        db.close()
    return run_id


def test_empty_results_page_renders() -> None:
    with _client() as client:
        tpl = client.post(
            "/templates/new",
            data={"name": "PR Empty", "assessment_type": "peer_review"},
            follow_redirects=False,
        )
        template_id = int(tpl.headers["location"].split("/")[2])
        run = client.post(
            f"/templates/{template_id}/runs/new",
            data={"run_name": "Class A", "exam_date": "2026-07-01"},
            follow_redirects=False,
        )
        run_id = int(run.headers["location"].split("/")[2])
        response = client.get(f"/exams/{run_id}/results")
        assert response.status_code == 200


def test_results_page_with_group_and_competency_rankings() -> None:
    with _client() as client:
        run_id = _peer_run_with_ratings(client)
        response = client.get(f"/exams/{run_id}/results")
        assert response.status_code == 200
        # Ranking + competency sections render with the student names.
        assert "Alice" in response.text
        assert "Bob" in response.text


def test_ja_results_page_has_no_hardcoded_english_labels() -> None:
    with _client() as client:
        run_id = _peer_run_with_ratings(client)
        response = client.get(f"/exams/{run_id}/results", headers={"Accept-Language": "ja"})
        assert response.status_code == 200
        text = response.text
        # Old hardcoded English section headers must be gone.
        for stale in (
            "Side-by-Side Group Comparison",
            "Combined Ranking",
            "Competency Rankings",
            "Students reviewed",
            "Total anonymous ratings",
        ):
            assert stale not in text
        # Localized JA labels present.
        assert "総合ランキング" in text
        assert "評価対象者数" in text
