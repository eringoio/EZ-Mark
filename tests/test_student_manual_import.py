"""Manual student entry validation on /exams/{id}/students/add-manual.

Partially filled rows must fail loud (HTTP 400, field highlighting, no partial commit);
fully empty rows are ignored; valid rows import and trigger peer-review roster sync.
"""

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import create_app
from app.models import ExamStudent, MockExam, PeerReviewTarget


def _client() -> TestClient:
    return TestClient(create_app())


def _run(client: TestClient) -> int:
    response = client.post(
        "/exams",
        data={
            "name": "Roster Run",
            "assessment_type": "generic_test",
            "numbering_mode": "continuous",
        },
        follow_redirects=False,
    )
    return int(response.headers["location"].split("/")[2])


def _add_manual(client: TestClient, run_id: int, rows: list[tuple[str, str, str]]):
    # httpx encodes parallel form fields from dict values that are lists, preserving
    # empty strings (which is what a real browser submits for blank inputs).
    data = {
        "student_number": [sn for sn, _nm, _cn in rows],
        "name": [nm for _sn, nm, _cn in rows],
        "class_name": [cn for _sn, _nm, cn in rows],
    }
    return client.post(f"/exams/{run_id}/students/add-manual", data=data, follow_redirects=False)


def _included_count(run_id: int) -> int:
    db = SessionLocal()
    try:
        return (
            db.query(ExamStudent)
            .filter(ExamStudent.exam_id == run_id, ExamStudent.included.is_(True))
            .count()
        )
    finally:
        db.close()


def test_partial_row_returns_400_and_no_commit() -> None:
    with _client() as client:
        run_id = _run(client)
        response = _add_manual(client, run_id, [("MANUAL-SYN1", "", "")])
        assert response.status_code == 400
        assert "required" in response.text.lower()
        assert _included_count(run_id) == 0


def test_missing_name_highlights_name_field() -> None:
    with _client() as client:
        run_id = _run(client)
        response = _add_manual(client, run_id, [("MANUAL-SYN1", "", "3A")])
        assert response.status_code == 400
        assert 'class="si-input si-invalid" type="text" name="name"' in response.text


def test_missing_class_highlights_class_field() -> None:
    with _client() as client:
        run_id = _run(client)
        response = _add_manual(client, run_id, [("MANUAL-SYN1", "Taro", "")])
        assert response.status_code == 400
        assert 'class="si-input si-invalid" type="text" name="class_name"' in response.text


def test_empty_rows_ignored_and_valid_rows_import() -> None:
    with _client() as client:
        run_id = _run(client)
        response = _add_manual(
            client,
            run_id,
            [("", "", ""), ("MANUAL-SYN1", "Taro", "3A"), ("", "", "")],
        )
        assert response.status_code == 303
        assert _included_count(run_id) == 1


def test_duplicate_within_form_errors() -> None:
    with _client() as client:
        run_id = _run(client)
        response = _add_manual(
            client, run_id, [("MANUAL-SYN1", "Taro", "3A"), ("MANUAL-SYN1", "Hanako", "3A")]
        )
        assert response.status_code == 400
        assert _included_count(run_id) == 0


def test_duplicate_already_in_session_errors() -> None:
    with _client() as client:
        run_id = _run(client)
        assert _add_manual(client, run_id, [("MANUAL-SYN1", "Taro", "3A")]).status_code == 303
        response = _add_manual(client, run_id, [("MANUAL-SYN1", "Taro", "3A")])
        assert response.status_code == 400
        assert _included_count(run_id) == 1


def test_add_row_clone_clears_invalid_state() -> None:
    # The "+ add row" clone must start clean: it must strip the validation state that a
    # submitted invalid first row carries (values, si-invalid class, aria-invalid), so a
    # freshly added row never looks pre-flagged as invalid.
    with _client() as client:
        run_id = _run(client)
        response = client.get(f"/exams/{run_id}/students/import")
        assert response.status_code == 200
        assert "function addStudentRow" in response.text
        assert "classList.remove('si-invalid')" in response.text
        assert "removeAttribute('aria-invalid')" in response.text


def test_peer_review_roster_sync_after_manual_add() -> None:
    with _client() as client:
        tpl = client.post(
            "/templates/new",
            data={"name": "PR Tpl", "assessment_type": "peer_review"},
            follow_redirects=False,
        )
        template_id = int(tpl.headers["location"].split("/")[2])
        run = client.post(
            f"/templates/{template_id}/runs/new",
            data={"run_name": "Class A", "exam_date": "2026-07-01"},
            follow_redirects=False,
        )
        run_id = int(run.headers["location"].split("/")[2])
        assert _add_manual(client, run_id, [("MANUAL-PR-SYN1", "Yamada", "3A")]).status_code == 303

        db = SessionLocal()
        try:
            run_exam = db.get(MockExam, run_id)
            from app.models import PeerReviewForm

            form = db.query(PeerReviewForm).filter(PeerReviewForm.exam_id == run_exam.id).first()
            targets = db.query(PeerReviewTarget).filter(PeerReviewTarget.form_id == form.id).all()
            assert len(targets) == 1
        finally:
            db.close()
