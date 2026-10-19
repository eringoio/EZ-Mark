"""Peer-review as a first-class reusable template type.

Covers: template creation succeeds, the peer-review template builder renders,
template readiness, session creation copies reusable settings but not roster/pages,
students_per_page drives auto-pagination, and Japanese pages avoid hardcoded English.
"""

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import create_app
from app.models import (
    Competency,
    ExamStudent,
    MockExam,
    PeerReviewForm,
    PeerReviewPage,
    PeerReviewRaterGroup,
    PeerReviewTarget,
    Student,
)
from app.services.peer_review_builder_service import (
    builder_state,
    peer_review_template_issues,
)


def _client() -> TestClient:
    return TestClient(create_app())


def _create_peer_template(client: TestClient, name: str = "Peer template") -> int:
    response = client.post(
        "/templates/new",
        data={"name": name, "assessment_type": "peer_review"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.endswith("/peer-review"), location
    return int(location.split("/")[2])


def test_peer_review_template_creation_succeeds() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)

        db = SessionLocal()
        try:
            template = db.get(MockExam, template_id)
            assert template.is_template is True
            assert template.assessment_type == "peer_review"
            form = db.query(PeerReviewForm).filter_by(exam_id=template_id).one()
            # Seeded reusable defaults.
            assert form.score_min == 1
            assert form.score_max == 5
            assert form.students_per_page == 15
            assert db.query(Competency).filter_by(exam_id=template_id).count() == 1
            # No real students or generated pages on a template.
            assert db.query(PeerReviewTarget).filter_by(form_id=form.id).count() == 0
            assert db.query(PeerReviewPage).filter_by(form_id=form.id).count() == 0
        finally:
            db.close()


def test_peer_review_template_builder_route_works() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)

        page = client.get(f"/templates/{template_id}/peer-review")
        assert page.status_code == 200
        assert "pr-paper" in page.text  # WYSIWYG SVG preview
        assert "Student 1" in page.text  # placeholder rows

        layout = client.get(f"/templates/{template_id}/peer-review/layout").json()
        assert layout["is_template"] is True
        assert len(layout["pages"][0]["layout"]["target_rows"]) == 15

        # The plain answer-key builder must redirect peer templates to their builder.
        redirect = client.get(f"/templates/{template_id}/builder", follow_redirects=False)
        assert redirect.status_code in (303, 307)
        assert redirect.headers["location"].endswith("/peer-review")


def test_peer_review_template_readiness() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        db = SessionLocal()
        try:
            template = db.get(MockExam, template_id)
            assert peer_review_template_issues(db, template) == []

            # Remove the only competency -> not ready.
            comp = db.query(Competency).filter_by(exam_id=template_id).one()
            db.delete(comp)
            db.commit()
            assert peer_review_template_issues(db, template) != []
        finally:
            db.close()


def test_session_from_peer_template_copies_settings_not_roster() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        client.post(
            f"/templates/{template_id}/peer-review/settings",
            data={"title": "PR", "score_min": 1, "score_max": 4, "students_per_page": 8},
        )
        client.post(
            f"/templates/{template_id}/peer-review/competencies",
            data={"name": "Teamwork"},
        )

        run_response = client.post(
            f"/templates/{template_id}/runs/new",
            data={"run_name": "Class A", "exam_date": ""},
            follow_redirects=False,
        )
        assert run_response.status_code == 303
        run_id = int(run_response.headers["location"].split("/")[2])

        db = SessionLocal()
        try:
            run = db.get(MockExam, run_id)
            assert run.is_template is False
            assert run.source_template_id == template_id

            run_form = db.query(PeerReviewForm).filter_by(exam_id=run_id).one()
            # Reusable settings copied.
            assert (run_form.score_min, run_form.score_max) == (1, 4)
            assert run_form.students_per_page == 8
            assert db.query(PeerReviewRaterGroup).filter_by(form_id=run_form.id).count() >= 1
            comp_names = {c.name for c in db.query(Competency).filter_by(exam_id=run_id)}
            assert "Teamwork" in comp_names

            # Session-specific data is NOT copied.
            assert db.query(PeerReviewTarget).filter_by(form_id=run_form.id).count() == 0
            assert db.query(PeerReviewPage).filter_by(form_id=run_form.id).count() == 0
        finally:
            db.close()


def test_students_per_page_drives_auto_pagination() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        client.post(
            f"/templates/{template_id}/peer-review/settings",
            data={"title": "PR", "score_min": 1, "score_max": 5, "students_per_page": 5},
        )
        run_id = int(
            client.post(
                f"/templates/{template_id}/runs/new",
                data={"run_name": "Class B", "exam_date": ""},
                follow_redirects=False,
            )
            .headers["location"]
            .split("/")[2]
        )

        db = SessionLocal()
        try:
            run = db.get(MockExam, run_id)
            # Attach 12 students to the run roster.
            for index in range(12):
                student = Student(
                    student_number=f"S{index:03d}", name=f"Student {index}", class_name="3A"
                )
                db.add(student)
                db.flush()
                db.add(ExamStudent(exam_id=run_id, student_id=student.id, included=True))
            db.commit()

            # builder_state syncs the roster into targets and auto-paginates, exactly
            # as visiting the session builder does.
            state = builder_state(db, run)
            # 12 students at 5 per page -> 3 pages (5, 5, 2).
            assert len(state["pages"]) == 3
            assert [len(p["target_ids"]) for p in state["pages"]] == [5, 5, 2]
        finally:
            db.close()


def test_normal_template_creation_still_redirects_to_answer_key_builder() -> None:
    with _client() as client:
        response = client.post(
            "/templates/new",
            data={"name": "Standard Test", "assessment_type": "generic_test"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].endswith("/builder")
        assert "/peer-review" not in response.headers["location"]


def test_japanese_peer_template_builder_has_no_obvious_hardcoded_english() -> None:
    with _client() as client:
        template_id = _create_peer_template(client)
        page = client.get(
            f"/templates/{template_id}/peer-review",
            headers={"accept-language": "ja"},
        )
        assert page.status_code == 200
        # Localized Japanese strings present.
        assert "評価項目" in page.text  # competencies
        assert "1ページあたりの学生数" in page.text  # students per page
        # Hardcoded English that previously leaked must be gone.
        assert "Rater groups" not in page.text
        assert "Auto-paginate targets" not in page.text
