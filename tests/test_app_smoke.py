from fastapi.testclient import TestClient

from app.main import create_app


def test_dashboard_loads() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Assessment workspace" in response.text


def test_healthz_returns_json_status() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["app"] == "ezmark"


def test_peer_review_flow() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/exams",
            data={
                "name": "Class 3-A Peer Feedback",
                "assessment_type": "peer_review",
                "numbering_mode": "continuous",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Class 3-A Peer Feedback" in response.text

        exam_id = response.url.path.split("/")[-1]

        response = client.get(f"/exams/{exam_id}/competencies")
        assert response.status_code == 200
        assert "Configure Competencies" in response.text

        response = client.post(
            f"/exams/{exam_id}/competencies",
            data={
                "name": "Teamwork Collaboration",
                "description": "How well does the classmate collaborate?",
                "min_score": 1,
                "max_score": 5,
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Teamwork Collaboration" in response.text
