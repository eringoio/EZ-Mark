from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.models import (
    MockExam,
    PhysicalSheet,
    ProblemGroup,
    Question,
    Response,
    ScanBatch,
    ScannedPage,
    ScoringSection,
    SheetInstance,
    SheetTemplate,
    Student,
)
from app.routes import review as review_routes
from app.services.coordinate_question_service import (
    BROKEN_RESPONSE_MESSAGE,
    coordinate_map_check,
    orphan_review_problem_rows,
    repair_orphan_responses_for_exam,
)
from app.services.review_service import (
    correct_response,
    count_orphan_reviewable,
    count_reviewable,
    list_review_responses,
    resolve_remaining_page_responses,
)
from app.services.templates import templates


def test_correct_response_marks_item_reviewed() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = MockExam(name="Synthetic Exam")
        student = Student(student_number="SYN001", name="Synthetic Student", class_name="A")
        sheet = PhysicalSheet(sheet_no=1, sheet_name="Sheet", slug="sheet", display_order=1)
        section = ScoringSection(code="section", name="Section")
        exam.physical_sheets.append(sheet)
        exam.scoring_sections.append(section)
        session.add_all([exam, student])
        session.commit()
        question = Question(
            exam_id=exam.id,
            physical_sheet_id=sheet.id,
            scoring_section_id=section.id,
            display_label="1",
            choice_count=4,
            correct_answer="2",
        )
        template = SheetTemplate(exam_id=exam.id, physical_sheet_id=sheet.id)
        session.add_all([question, template])
        session.flush()
        sheet_instance = SheetInstance(
            exam_id=exam.id,
            student_id=student.id,
            physical_sheet_id=sheet.id,
            sheet_template_id=template.id,
            qr_token="token",
            printed_name=student.name,
            printed_student_number=student.student_number,
        )
        batch = ScanBatch(exam_id=exam.id, uploaded_file_name="scan.pdf", uploaded_file_path="x")
        page = ScannedPage(
            scan_batch=batch,
            sheet_instance=sheet_instance,
            page_no=1,
            image_path="x",
        )
        response = Response(scanned_page=page, question=question, status="unclear")
        session.add_all([sheet_instance, batch, page, response])
        session.commit()

        assert len(list_review_responses(session, exam.id)) == 1
        corrected = correct_response(session, response.id, "2")

        assert corrected.status == "reviewed"
        assert corrected.final_answer == "2"
        assert corrected.is_correct is True
        assert list_review_responses(session, exam.id) == []


def test_list_review_responses_excludes_missing_question_metadata() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam, page = _normal_omr_page(session)
        response = Response(scanned_page=page, question_id=999999, status="unclear")
        session.add(response)
        session.commit()

        assert list_review_responses(session, exam.id) == []


def test_review_current_template_handles_missing_question_metadata() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam, page = _normal_omr_page(session)
        response = Response(scanned_page=page, question_id=999999, status="unclear")
        session.add(response)
        session.commit()
        html = templates.env.get_template("review/current.html").render(
            request=None,
            exam=exam,
            current=response,
            _=lambda key, **kwargs: key,
        )

        assert BROKEN_RESPONSE_MESSAGE in html
        assert f"/responses/{response.id}/mark-invalid" in html


def test_review_debug_reports_orphan_response_rows(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_SCAN_DEBUG", "1")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam, page = _normal_omr_page(session)
        response = Response(scanned_page=page, question_id=999999, status="unclear")
        session.add(response)
        session.commit()

        payload = review_routes.review_exam_debug(exam.id, _local_request(), db=session)

        assert payload["orphan_response_count"] == 1
        assert payload["orphan_reviewable_response_count"] == 1
        assert payload["orphan_response_ids"] == [response.id]
        assert payload["orphan_responses"][0]["question_exists"] is False


def test_stale_coordinate_question_id_is_repaired_by_stable_metadata() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam, page, template, question = _stale_coordinate_page(session)
        response = Response(scanned_page=page, question_id=999999, status="unclear")
        session.add(response)
        session.commit()

        assert count_reviewable(session, exam.id) == 0
        assert count_orphan_reviewable(session, exam.id) == 1

        result = repair_orphan_responses_for_exam(session, exam.id)
        session.commit()

        assert result["repaired"] == 1
        assert response.question_id == question.id
        assert template.coordinate_map_json["questions"][0]["question_id"] == question.id
        assert count_reviewable(session, exam.id) == 1
        assert count_orphan_reviewable(session, exam.id) == 0
        assert [item.id for item in list_review_responses(session, exam.id)] == [response.id]


def test_coordinate_map_check_reports_recoverable_missing_question_id() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        _exam, _page, template, question = _stale_coordinate_page(session)
        session.commit()

        payload = coordinate_map_check(session, template)

        assert payload["coordinate_question_count"] == 1
        assert payload["valid_question_count"] == 0
        assert payload["missing_question_ids"] == [999999]
        assert payload["missing_questions"][0]["possible_matching_question_ids"] == [question.id]
        assert payload["missing_questions"][0]["recommended_repair_action"] == "auto_repair"


def test_review_page_queue_contains_recovered_item_after_repair() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam, page, _template, question = _stale_coordinate_page(session)
        response = Response(scanned_page=page, question_id=999999, status="unclear")
        session.add(response)
        session.commit()

        repair_orphan_responses_for_exam(session, exam.id)
        session.commit()
        responses = list_review_responses(session, exam.id)
        html = templates.env.get_template("review/current.html").render(
            request=None,
            exam=exam,
            current=responses[0],
            _=lambda key, **kwargs: key,
        )

        assert responses[0].question_id == question.id
        assert f'action="/responses/{response.id}/correct"' in html
        assert question.display_label in html


def test_review_overlay_keys_current_response_to_exact_coordinate_row() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        _exam, page, _template, _question_31, question_32 = _duplicate_coordinate_page(session)
        response = Response(
            id=107,
            scanned_page=page,
            question=question_32,
            detected_answer="3",
            status="unclear",
        )
        session.add(response)
        session.commit()

        page_response_map, debug = review_routes._build_page_response_map_with_debug(
            session, [response], response
        )

        assert len(page_response_map) == 1
        assert "1|もんだい5|3.2|4" in page_response_map
        assert "0|もんだい5|3.1|4" not in page_response_map
        assert page_response_map["1|もんだい5|3.2|4"]["response_id"] == 107
        assert debug["selected_coordinate_entry_key"] == "1|もんだい5|3.2|4"
        assert debug["current_question_id"] == question_32.id


def test_duplicate_question_id_coordinate_map_does_not_duplicate_current_overlay() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        _exam, page, _template, _question_31, question_32 = _duplicate_coordinate_page(session)
        response = Response(
            scanned_page=page,
            question=question_32,
            detected_answer="3",
            status="unclear",
        )
        session.add(response)
        session.commit()

        page_response_map, debug = review_routes._build_page_response_map_with_debug(
            session, [response], response
        )

        current_rows = [
            payload for payload in page_response_map.values() if payload["is_current"]
        ]
        assert len(current_rows) == 1
        assert debug["duplicate_coordinate_question_ids"] == [question_32.id]
        assert debug["coordinate_entries_matching_current_response"] == [
            {
                "index": 1,
                "question_id": question_32.id,
                "display_label": "3.2",
                "group_label": "もんだい5",
                "choice_count": 4,
                "coordinate_key": "1|もんだい5|3.2|4",
            }
        ]


def test_review_route_check_reports_duplicate_coordinate_question_ids(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_SCAN_DEBUG", "1")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam, page, _template, _question_31, question_32 = _duplicate_coordinate_page(session)
        response = Response(
            scanned_page=page,
            question=question_32,
            detected_answer="3",
            status="unclear",
        )
        session.add(response)
        session.commit()

        payload = review_routes.review_route_check(exam.id, _local_request(), db=session)

        assert payload["reviewable_response_count"] == 1
        assert payload["duplicate_coordinate_question_ids"] == [question_32.id]
        assert payload["selected_coordinate_entry_key"] == "1|もんだい5|3.2|4"


def test_repaired_orphan_response_maps_to_one_coordinate_row() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam, page, template, _question_31, question_32 = _duplicate_coordinate_page(
            session, duplicate_question_id=999999
        )
        first_response = Response(scanned_page=page, question_id=999999, status="ok")
        review_response = Response(scanned_page=page, question_id=999999, status="unclear")
        session.add_all([first_response, review_response])
        session.commit()

        result = repair_orphan_responses_for_exam(session, exam.id)
        session.commit()

        questions = template.coordinate_map_json["questions"]
        assert result["repaired"] == 1
        assert review_response.question_id == question_32.id
        assert questions[0]["question_id"] == 999999
        assert questions[1]["question_id"] == question_32.id
        assert count_reviewable(session, exam.id) == 1


def test_clicking_current_overlay_bubble_saves_response_once() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam, page, _template, _question_31, question_32 = _duplicate_coordinate_page(session)
        response = Response(
            id=107,
            scanned_page=page,
            question=question_32,
            detected_answer="3",
            status="unclear",
        )
        session.add(response)
        session.commit()

        page_response_map, _debug = review_routes._build_page_response_map_with_debug(
            session, [response], response
        )
        review_routes.correct_response_route(107, final_answer="3", db=session)

        saved = session.get(Response, 107)
        assert len(page_response_map) == 1
        assert saved.status == "reviewed"
        assert saved.final_answer == "3"
        assert session.query(Response).filter(Response.id == 107).count() == 1
        assert count_reviewable(session, exam.id) == 0


def test_review_queue_count_remains_one_with_duplicate_coordinate_question_ids() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam, page, _template, _question_31, question_32 = _duplicate_coordinate_page(session)
        response = Response(scanned_page=page, question=question_32, status="unclear")
        session.add(response)
        session.commit()

        assert count_reviewable(session, exam.id) == 1
        assert [item.id for item in list_review_responses(session, exam.id)] == [response.id]


def test_unrecoverable_orphan_response_is_reported_not_dropped() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam, page, _template, _question = _stale_coordinate_page(session, choice_count=5)
        response = Response(scanned_page=page, question_id=999999, status="unclear")
        session.add(response)
        session.commit()

        result = repair_orphan_responses_for_exam(session, exam.id)
        problems = orphan_review_problem_rows(session, exam.id)
        html = templates.env.get_template("review/index.html").render(
            request=_template_request(),
            active_nav="scan_review",
            current_locale="ja",
            supported_locales={"ja": "日本語"},
            exam=exam,
            responses=[],
            current=None,
            page_response_map={},
            queue_groups=[],
            prev_response_id=None,
            next_response_id=None,
            view_page=None,
            stale_needs_review_pages=[],
            orphan_review_items=problems,
            broken_response_message=BROKEN_RESPONSE_MESSAGE,
            error=None,
            _=lambda key, **kwargs: key,
        )

        assert result["repaired"] == 0
        assert count_reviewable(session, exam.id) == 0
        assert count_orphan_reviewable(session, exam.id) == 1
        assert problems[0]["recoverable"] is False
        assert BROKEN_RESPONSE_MESSAGE in html


def test_resolve_remaining_page_responses_marks_only_problem_statuses_blank() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam, page = _normal_omr_page(session)
        other_page = ScannedPage(
            scan_batch=page.scan_batch,
            sheet_instance=page.sheet_instance,
            page_no=2,
            image_path="y",
            review_status="needs_review",
        )
        statuses = ["blank", "multiple_mark", "unclear", "invalid", "reviewed", "ok"]
        responses = [
            Response(scanned_page=page, question_id=999999, status=status)
            for status in statuses
        ]
        other_response = Response(scanned_page=other_page, question_id=999999, status="unclear")
        session.add_all([other_page, *responses, other_response])
        session.commit()

        changed = resolve_remaining_page_responses(session, page.id, "blank")

        assert changed == 4
        assert [response.status for response in responses[:4]] == ["reviewed"] * 4
        assert all(response.final_answer is None for response in responses[:4])
        assert all(response.is_correct is False for response in responses[:4])
        assert responses[4].status == "reviewed"
        assert responses[5].status == "ok"
        assert other_response.status == "unclear"
        session.refresh(page)
        session.refresh(page.scan_batch)
        assert page.review_status == "ok"
        assert page.scan_batch.status == "needs_review"


def test_resolve_remaining_page_responses_marks_only_problem_statuses_invalid() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        _exam, page = _normal_omr_page(session)
        responses = [
            Response(scanned_page=page, question_id=999999, status=status)
            for status in ["blank", "multiple_mark", "unclear", "invalid", "reviewed", "ok"]
        ]
        session.add_all(responses)
        session.commit()

        changed = resolve_remaining_page_responses(session, page.id, "invalid")

        assert changed == 4
        assert [response.status for response in responses[:4]] == ["reviewed"] * 4
        assert responses[4].status == "reviewed"
        assert responses[5].status == "ok"
        session.refresh(page)
        assert page.review_status == "ok"


def test_mark_remaining_page_route_redirects_and_applies_change() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam, page = _normal_omr_page(session)
        response = Response(scanned_page=page, question_id=999999, status="unclear")
        session.add(response)
        session.commit()

        redirect = review_routes.mark_remaining_page_invalid_route(page.id, db=session)

        assert redirect.status_code == 303
        assert redirect.headers["location"] == f"/exams/{exam.id}/review"
        assert response.status == "reviewed"


def test_review_template_includes_bulk_page_actions() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam, page = _normal_omr_page(session)
        response = Response(scanned_page=page, question_id=999999, status="unclear")
        session.add(response)
        session.commit()
        html = templates.env.get_template("review/index.html").render(
            request=_template_request(),
            active_nav="scan_review",
            current_locale="en",
            supported_locales={"en": "English"},
            exam=exam,
            responses=[response],
            current=response,
            page_response_map={},
            queue_groups=[{"header": "Sheet", "items": [{"response": response, "q_label": "1"}]}],
            prev_response_id=None,
            next_response_id=None,
            view_page=None,
            stale_needs_review_pages=[],
            orphan_review_items=[],
            broken_response_message=BROKEN_RESPONSE_MESSAGE,
            error=None,
            bulk_page_remaining_count=1,
            _=lambda key, **kwargs: (
                "Mark remaining {count} on this page as blank".format(**kwargs)
                if key == "review.bulk_page.remaining_blank"
                else key
            ),
        )

        assert f"/scanned-pages/{page.id}/mark-remaining-blank" in html
        assert "Mark remaining 1 on this page as blank" in html


def _normal_omr_page(session: Session) -> tuple[MockExam, ScannedPage]:
    exam = MockExam(name="Synthetic Exam")
    student = Student(student_number="SYN001", name="Synthetic Student", class_name="A")
    sheet = PhysicalSheet(sheet_no=1, sheet_name="Sheet", slug="sheet", display_order=1)
    exam.physical_sheets.append(sheet)
    session.add_all([exam, student])
    session.flush()
    template = SheetTemplate(exam_id=exam.id, physical_sheet_id=sheet.id)
    session.add(template)
    session.flush()
    sheet_instance = SheetInstance(
        exam_id=exam.id,
        student_id=student.id,
        physical_sheet_id=sheet.id,
        sheet_template_id=template.id,
        qr_token="token",
        printed_name=student.name,
        printed_student_number=student.student_number,
    )
    batch = ScanBatch(exam_id=exam.id, uploaded_file_name="scan.pdf", uploaded_file_path="x")
    page = ScannedPage(
        scan_batch=batch,
        sheet_instance=sheet_instance,
        page_no=1,
        image_path="x",
        review_status="needs_review",
    )
    session.add_all([sheet_instance, batch, page])
    session.commit()
    return exam, page


def _stale_coordinate_page(
    session: Session,
    choice_count: int = 4,
) -> tuple[MockExam, ScannedPage, SheetTemplate, Question]:
    exam = MockExam(name="Synthetic Exam")
    student = Student(student_number="SYN001", name="Synthetic Student", class_name="A")
    sheet = PhysicalSheet(sheet_no=1, sheet_name="聴解", slug="listening", display_order=1)
    exam.physical_sheets.append(sheet)
    session.add_all([exam, student])
    session.flush()
    group = ProblemGroup(
        exam_id=exam.id,
        physical_sheet_id=sheet.id,
        problem_no=5,
        problem_title="もんだい5",
        subquestion_count=1,
    )
    question = Question(
        exam_id=exam.id,
        physical_sheet_id=sheet.id,
        problem_no=5,
        subquestion_no=2,
        local_question_label="3.2",
        display_label="もんだい5-問3.2",
        choice_count=4,
        correct_answer="2",
    )
    template = SheetTemplate(
        exam_id=exam.id,
        physical_sheet_id=sheet.id,
        coordinate_map_json={
            "page_width_mm": 210,
            "page_height_mm": 297,
            "questions": [
                {
                    "question_id": 999999,
                    "display_label": "3.2",
                    "group_label": "もんだい5",
                    "choices": [
                        {"choice": str(index), "x_mm": 20 + index * 8, "y_mm": 40, "radius_mm": 2}
                        for index in range(1, choice_count + 1)
                    ],
                }
            ],
            "groups": [{"group_label": "もんだい5", "question_ids": [999999]}],
        },
    )
    session.add_all([group, question, template])
    session.flush()
    sheet_instance = SheetInstance(
        exam_id=exam.id,
        student_id=student.id,
        physical_sheet_id=sheet.id,
        sheet_template_id=template.id,
        qr_token="token",
        printed_name=student.name,
        printed_student_number=student.student_number,
    )
    batch = ScanBatch(exam_id=exam.id, uploaded_file_name="scan.pdf", uploaded_file_path="x")
    page = ScannedPage(
        scan_batch=batch,
        sheet_instance=sheet_instance,
        page_no=1,
        image_path="x",
        alignment_status="weak",
        review_status="needs_review",
    )
    session.add_all([sheet_instance, batch, page])
    session.commit()
    return exam, page, template, question


def _duplicate_coordinate_page(
    session: Session,
    duplicate_question_id: int | None = None,
) -> tuple[MockExam, ScannedPage, SheetTemplate, Question, Question]:
    exam = MockExam(name="Synthetic Exam")
    student = Student(student_number="SYN001", name="Synthetic Student", class_name="A")
    sheet = PhysicalSheet(sheet_no=1, sheet_name="聴解", slug="listening", display_order=1)
    section = ScoringSection(code="listening", name="聴解")
    exam.physical_sheets.append(sheet)
    exam.scoring_sections.append(section)
    session.add_all([exam, student])
    session.flush()
    group = ProblemGroup(
        exam_id=exam.id,
        physical_sheet_id=sheet.id,
        problem_no=5,
        problem_title="もんだい5",
        subquestion_count=2,
    )
    question_31 = Question(
        exam_id=exam.id,
        physical_sheet_id=sheet.id,
        scoring_section_id=section.id,
        problem_no=5,
        subquestion_no=1,
        local_question_label="3.1",
        display_label="もんだい5-問3.1",
        choice_count=4,
        correct_answer="2",
    )
    question_32 = Question(
        exam_id=exam.id,
        physical_sheet_id=sheet.id,
        scoring_section_id=section.id,
        problem_no=5,
        subquestion_no=2,
        local_question_label="3.2",
        display_label="もんだい5-問3.2",
        choice_count=4,
        correct_answer="3",
    )
    session.add_all([group, question_31, question_32])
    session.flush()
    shared_question_id = duplicate_question_id if duplicate_question_id is not None else question_32.id
    template = SheetTemplate(
        exam_id=exam.id,
        physical_sheet_id=sheet.id,
        coordinate_map_json={
            "page_width_mm": 210,
            "page_height_mm": 297,
            "questions": [
                {
                    "question_id": shared_question_id,
                    "display_label": "3.1",
                    "group_label": "もんだい5",
                    "choices": [
                        {"choice": str(index), "x_mm": 20 + index * 8, "y_mm": 40, "radius_mm": 2}
                        for index in range(1, 5)
                    ],
                },
                {
                    "question_id": shared_question_id,
                    "display_label": "3.2",
                    "group_label": "もんだい5",
                    "choices": [
                        {"choice": str(index), "x_mm": 20 + index * 8, "y_mm": 50, "radius_mm": 2}
                        for index in range(1, 5)
                    ],
                },
            ],
            "groups": [{"group_label": "もんだい5", "question_ids": [shared_question_id]}],
        },
    )
    session.add(template)
    session.flush()
    sheet_instance = SheetInstance(
        exam_id=exam.id,
        student_id=student.id,
        physical_sheet_id=sheet.id,
        sheet_template_id=template.id,
        qr_token="token",
        printed_name=student.name,
        printed_student_number=student.student_number,
    )
    batch = ScanBatch(exam_id=exam.id, uploaded_file_name="scan.pdf", uploaded_file_path="x")
    page = ScannedPage(
        scan_batch=batch,
        sheet_instance=sheet_instance,
        page_no=1,
        image_path="x",
        alignment_status="weak",
        review_status="needs_review",
    )
    session.add_all([sheet_instance, batch, page])
    session.commit()
    return exam, page, template, question_31, question_32


def _local_request():
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"host": "127.0.0.1:8000"},
    )


def _template_request():
    return SimpleNamespace(url_for=lambda _name, **kwargs: kwargs.get("path", ""))
