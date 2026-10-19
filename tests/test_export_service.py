from app.models import MockExam, ScoringSection
from app.services.export_service import _student_summary_headers


def test_student_summary_headers_include_section_columns() -> None:
    exam = MockExam(name="Synthetic Test")
    section = ScoringSection(code="language_knowledge", name="Language Knowledge")
    exam.scoring_sections = [section]

    headers = _student_summary_headers(exam)

    assert "language_knowledge_raw_correct" in headers
    assert "language_knowledge_score" in headers
    assert "pass_fail_status" in headers
