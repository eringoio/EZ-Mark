from pathlib import Path

import cv2
import numpy as np
import qrcode
from openpyxl import load_workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.models  # noqa: F401
import app.services.peer_review_scan_service as peer_review_scan_service
from app.config import get_settings
from app.database import Base, data_relative_path
from app.models import (
    Competency,
    ExamStudent,
    MockExam,
    PeerReviewFinalResult,
    PeerReviewGroupResult,
    PeerReviewRaterGroup,
    PeerReviewRating,
    Student,
)
from app.routes.review import review_queue
from app.services.export_service import export_path
from app.services.peer_review_builder_service import (
    LAYOUT_CHANGE_BLOCKED_MESSAGE,
    RATER_GROUP_DELETE_BLOCKED_MESSAGE,
    add_page_break_before_target,
    add_rater_group,
    auto_paginate,
    builder_state,
    delete_competency,
    delete_rater_group,
    get_or_create_form,
    peer_review_form_has_scans_or_ratings,
    peer_review_form_layout_is_locked,
    update_rater_group,
    update_target,
)
from app.services.peer_review_export_service import generate_peer_review_summary_export
from app.services.peer_review_insights_service import build_peer_review_insights
from app.services.peer_review_scan_service import (
    _read_page_scores,
    create_peer_review_batch,
    process_peer_review_batch,
    record_submission_page,
)
from app.services.peer_review_scoring_service import recalculate_anonymous_peer_review_results
from app.services.peer_review_sheet_service import (
    generate_peer_review_pages,
    generate_peer_review_pdf,
)
from app.services.sheet_generation_service import latest_exam_download_path


def test_anonymous_peer_review_uses_reusable_page_qr_without_reviewer_identity() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, _students = _peer_review_exam(session)
        state = builder_state(session, exam)
        assert state["form"]["score_min"] == 1
        assert state["pages"][0]["target_ids"]

        pages = generate_peer_review_pages(session, exam)
        page = pages[0]
        assert page.coordinate_map_json["type"] == "anonymous_peer_review"
        assert "reviewer" not in str(page.coordinate_map_json).lower()

        form = get_or_create_form(session, exam)
        group = form.rater_groups[0]
        batch = create_peer_review_batch(
            session, form.id, group.id, "scan.pdf", "uploaded_scans/scan.pdf"
        )
        scores = {
            (bubble["target_student_id"], bubble["competency_id"]): 4
            for bubble in page.coordinate_map_json["rating_bubbles"]
        }
        first = record_submission_page(session, batch, page, 1, "processed_pages/a.png", scores)
        second = record_submission_page(session, batch, page, 2, "processed_pages/b.png", scores)

        assert first.id != second.id
        assert first.peer_review_page_id == second.peer_review_page_id == page.id
        assert session.query(PeerReviewRating).count() == 4
        assert not hasattr(PeerReviewRating, "reviewer_student_id")


def test_weighted_peer_review_scoring_counts_reviewed_status() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        form.combine_groups = True
        form.missing_group_policy = "normalize_available"
        form.rater_groups[0].name = "Teachers"
        form.rater_groups[0].weight_percent = 50
        student_group = PeerReviewRaterGroup(
            form_id=form.id, name="Students", weight_percent=50, display_order=2
        )
        session.add(student_group)
        session.commit()

        pages = generate_peer_review_pages(session, exam)
        comp_id = session.scalar(select(Competency.id).where(Competency.exam_id == exam.id))
        teacher_batch = create_peer_review_batch(
            session, form.id, form.rater_groups[0].id, "t.pdf", "uploaded_scans/t.pdf"
        )
        student_batch = create_peer_review_batch(
            session, form.id, student_group.id, "s.pdf", "uploaded_scans/s.pdf"
        )
        target_id = students[0].id
        record_submission_page(
            session, teacher_batch, pages[0], 1, "processed_pages/t.png", {(target_id, comp_id): 5}
        )
        record_submission_page(
            session, student_batch, pages[0], 1, "processed_pages/s.png", {(target_id, comp_id): 3}
        )
        session.query(PeerReviewRating).filter(
            PeerReviewRating.rater_group_id == student_group.id
        ).update({"status": "reviewed", "final_score": 3})
        session.commit()

        recalculate_anonymous_peer_review_results(session, form)
        result = session.scalar(
            select(PeerReviewFinalResult).where(
                PeerReviewFinalResult.target_student_id == target_id
            )
        )

        assert result.final_score == 4
        assert result.total_rating_count == 2


def test_peer_review_pdf_generation_is_anonymous_and_page_breaks_are_reusable() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        add_rater_group(session, form, "Teachers", 50)
        add_page_break_before_target(session, form, form.targets[1].id)

        summary = generate_peer_review_pdf(session, exam)
        pages = generate_peer_review_pages(session, exam)

        assert summary.download_path.endswith("_anonymous_peer_review.pdf")
        assert latest_exam_download_path(session, exam) == summary.download_path
        assert summary.pages_created == 2
        assert pages[0].qr_token != pages[1].qr_token
        assert all("reviewer" not in str(page.coordinate_map_json).lower() for page in pages)
        assert {row["student_id"] for page in pages for row in page.target_rows_json} == {
            student.id for student in students
        }


def test_peer_review_scan_processing_allows_duplicate_reusable_qr(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        page = generate_peer_review_pages(session, exam)[0]
        group = form.rater_groups[0]
        batch = create_peer_review_batch(
            session, form.id, group.id, "scan.pdf", "uploaded_scans/scan.pdf"
        )

        monkeypatch.setattr(
            peer_review_scan_service,
            "_extract_pages_for_peer_review",
            lambda _batch: ["processed_pages/a.png", "processed_pages/b.png"],
        )
        monkeypatch.setattr(
            peer_review_scan_service, "_detect_qr_token", lambda _path: page.qr_token
        )
        competency = session.scalar(select(Competency).where(Competency.exam_id == exam.id))
        monkeypatch.setattr(
            peer_review_scan_service,
            "_read_page_scores",
            lambda _page, _path, _db: (
                {(students[0].id, competency.id): 5, (students[1].id, competency.id): None},
                {(students[0].id, competency.id): 0.9, (students[1].id, competency.id): 0.1},
                {(students[0].id, competency.id): "ok", (students[1].id, competency.id): "blank"},
            ),
        )

        summary = process_peer_review_batch(session, batch)

        assert summary.total_pages == 2
        assert summary.detected_pages == 2
        assert summary.problem_pages == 2
        assert summary.ratings_created == 4
        assert session.query(PeerReviewRating).count() == 4
        assert {rating.submission_page_id for rating in session.query(PeerReviewRating)} == {1, 2}


def test_peer_review_reader_uses_saved_mm_coordinate_map_positions() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, students = _peer_review_exam(session)
        page = generate_peer_review_pages(session, exam)[0]
        coordinate_map = page.coordinate_map_json
        assert coordinate_map["coordinate_units"] == "mm_top_left"
        assert "x" not in coordinate_map["rating_bubbles"][0]
        target_id = students[0].id
        competency_id = coordinate_map["rating_bubbles"][0]["competency_id"]
        selected = next(
            bubble
            for bubble in coordinate_map["rating_bubbles"]
            if bubble["target_student_id"] == target_id
            and bubble["competency_id"] == competency_id
            and bubble["score"] == 4
        )
        image = np.full((2970, 2100), 255, dtype=np.uint8)
        x_px = int(selected["x_mm"] / coordinate_map["page_width_mm"] * image.shape[1])
        y_px = int(selected["y_mm"] / coordinate_map["page_height_mm"] * image.shape[0])
        radius_px = int(selected["radius_mm"] / coordinate_map["page_width_mm"] * image.shape[1])
        cv2.circle(image, (x_px, y_px), radius_px, 0, thickness=-1)
        output_dir = get_settings().data_root / "processed_pages"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "peer_layout_test.png"
        cv2.imwrite(str(output_path), image)

        scores, _confidences, statuses = _read_page_scores(
            page, "processed_pages/peer_layout_test.png", session
        )

        assert scores[(target_id, competency_id)] == 4
        assert statuses[(target_id, competency_id)] == "ok"


def test_peer_review_auto_paginate_preserves_qr_tokens_when_possible() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, _students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        auto_paginate(session, form, targets_per_page=1)
        tokens = {page.page_no: page.qr_token for page in form.pages}

        auto_paginate(session, form, targets_per_page=2)
        session.refresh(form)

        assert form.pages[0].qr_token == tokens[1]
        assert len({page.qr_token for page in form.pages}) == len(form.pages)


def test_peer_review_page_break_preserves_existing_qr_tokens() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, _students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        auto_paginate(session, form)
        first_token = form.pages[0].qr_token

        add_page_break_before_target(session, form, form.targets[1].id)
        session.refresh(form)

        assert form.pages[0].qr_token == first_token
        assert form.pages[1].qr_token != first_token


def test_peer_review_layout_change_blocked_after_ratings_exist() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        page = generate_peer_review_pages(session, exam)[0]
        comp_id = session.scalar(select(Competency.id).where(Competency.exam_id == exam.id))
        batch = create_peer_review_batch(
            session, form.id, form.rater_groups[0].id, "scan.pdf", "uploaded_scans/scan.pdf"
        )
        record_submission_page(
            session, batch, page, 1, "processed_pages/a.png", {(students[0].id, comp_id): 4}
        )

        assert peer_review_form_has_scans_or_ratings(session, form.id)
        try:
            auto_paginate(session, form, targets_per_page=1)
        except ValueError as exc:
            assert str(exc) == LAYOUT_CHANGE_BLOCKED_MESSAGE
        else:
            raise AssertionError("Expected layout change to be blocked.")


def test_peer_review_target_change_blocked_after_ratings_exist() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        page = generate_peer_review_pages(session, exam)[0]
        comp_id = session.scalar(select(Competency.id).where(Competency.exam_id == exam.id))
        batch = create_peer_review_batch(
            session, form.id, form.rater_groups[0].id, "scan.pdf", "uploaded_scans/scan.pdf"
        )
        record_submission_page(
            session, batch, page, 1, "processed_pages/a.png", {(students[0].id, comp_id): 4}
        )

        try:
            update_target(session, form, form.targets[0].id, {"active": False})
        except ValueError as exc:
            assert str(exc) == LAYOUT_CHANGE_BLOCKED_MESSAGE
        else:
            raise AssertionError("Expected target change to be blocked.")


def test_peer_review_competency_delete_blocked_after_ratings() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        page = generate_peer_review_pages(session, exam)[0]
        comp = session.scalar(select(Competency).where(Competency.exam_id == exam.id))
        batch = create_peer_review_batch(
            session, form.id, form.rater_groups[0].id, "scan.pdf", "uploaded_scans/scan.pdf"
        )
        record_submission_page(
            session, batch, page, 1, "processed_pages/a.png", {(students[0].id, comp.id): 4}
        )

        try:
            delete_competency(session, form, comp.id)
        except ValueError as exc:
            assert "cannot be deleted" in str(exc)
        else:
            raise AssertionError("Expected competency delete to be blocked.")


def test_peer_review_weighted_competency_scores_use_group_weights() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        form.combine_groups = True
        form.rater_groups[0].name = "Teachers"
        form.rater_groups[0].weight_percent = 70
        student_group = PeerReviewRaterGroup(
            form_id=form.id, name="Students", weight_percent=30, display_order=2
        )
        session.add(student_group)
        session.commit()
        page = generate_peer_review_pages(session, exam)[0]
        comp_id = session.scalar(select(Competency.id).where(Competency.exam_id == exam.id))
        teacher_batch = create_peer_review_batch(
            session, form.id, form.rater_groups[0].id, "t.pdf", "uploaded_scans/t.pdf"
        )
        student_batch = create_peer_review_batch(
            session, form.id, student_group.id, "s.pdf", "uploaded_scans/s.pdf"
        )
        record_submission_page(
            session, teacher_batch, page, 1, "processed_pages/t.png", {(students[0].id, comp_id): 5}
        )
        record_submission_page(
            session, student_batch, page, 1, "processed_pages/s.png", {(students[0].id, comp_id): 1}
        )

        recalculate_anonymous_peer_review_results(session, form)
        result = session.scalar(
            select(PeerReviewFinalResult).where(
                PeerReviewFinalResult.target_student_id == students[0].id
            )
        )

        assert round(result.final_score, 2) == 3.8
        assert round(result.competency_breakdown_json[str(comp_id)]["average"], 2) == 3.8


def test_peer_review_insights_side_by_side_mode_uses_group_results() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        form.combine_groups = False
        session.add(
            PeerReviewGroupResult(
                exam_id=exam.id,
                form_id=form.id,
                rater_group_id=form.rater_groups[0].id,
                target_student_id=students[0].id,
                group_average_score=4.5,
                rating_count=2,
                competency_breakdown_json={},
            )
        )
        session.commit()

        insights = build_peer_review_insights(session, exam)

        assert insights["combined_ranking"] == []
        assert insights["side_by_side"][0]["groups"][0]["average"] == 4.5
        assert insights["group_rankings"][0]["rows"][0]["average"] == 4.5


def test_peer_review_layout_change_blocked_after_pdf_generation() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, _students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        generate_peer_review_pdf(session, exam)

        assert peer_review_form_layout_is_locked(session, form)
        try:
            auto_paginate(session, form, targets_per_page=1)
        except ValueError as exc:
            assert str(exc) == LAYOUT_CHANGE_BLOCKED_MESSAGE
        else:
            raise AssertionError("Expected printed layout change to be blocked.")


def test_peer_review_layout_change_blocked_when_exam_status_printed() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, _students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        exam.status = "printed"
        session.commit()

        assert peer_review_form_layout_is_locked(session, form)
        try:
            add_page_break_before_target(session, form, form.targets[1].id)
        except ValueError as exc:
            assert str(exc) == LAYOUT_CHANGE_BLOCKED_MESSAGE
        else:
            raise AssertionError("Expected printed status layout change to be blocked.")


def test_peer_review_safe_non_layout_edit_allowed_before_scans_if_applicable() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, _students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)

        update_target(session, form, form.targets[0].id, {"display_label": "Display Only"})

        session.refresh(form.targets[0])
        assert form.targets[0].display_label == "Display Only"


def test_peer_review_alignment_failed_does_not_read_raw_coordinates(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, _students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        page = generate_peer_review_pages(session, exam)[0]
        batch = create_peer_review_batch(
            session, form.id, form.rater_groups[0].id, "scan.png", "uploaded_scans/scan.png"
        )
        image_path = _blank_peer_review_image("alignment_failed.png")
        monkeypatch.setattr(
            peer_review_scan_service, "_extract_pages_for_peer_review", lambda _batch: [image_path]
        )
        monkeypatch.setattr(
            peer_review_scan_service, "_detect_qr_token", lambda _path: page.qr_token
        )
        monkeypatch.setattr(
            peer_review_scan_service,
            "_align_peer_review_page",
            lambda _page, _path: (None, "failed"),
        )

        def fail_if_read(*_args):
            raise AssertionError("Raw coordinates should not be read after failed alignment.")

        monkeypatch.setattr(peer_review_scan_service, "_read_page_scores", fail_if_read)

        summary = process_peer_review_batch(session, batch)

        assert summary.ratings_created == 2
        assert summary.problem_pages == 1
        ratings = session.query(PeerReviewRating).all()
        assert {rating.status for rating in ratings} == {"unclear"}
        assert all(rating.confidence == 0 for rating in ratings)


def test_peer_review_alignment_success_reads_scores(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        page = generate_peer_review_pages(session, exam)[0]
        selected = page.coordinate_map_json["rating_bubbles"][0]
        image_path = _peer_review_scan_image(
            page, {(students[0].id, selected["competency_id"]): selected["score"]}
        )
        batch = create_peer_review_batch(
            session, form.id, form.rater_groups[0].id, "scan.png", image_path
        )
        monkeypatch.setattr(
            peer_review_scan_service, "_extract_pages_for_peer_review", lambda _batch: [image_path]
        )

        summary = process_peer_review_batch(session, batch)

        assert summary.detected_pages == 1
        assert session.query(PeerReviewRating).filter(PeerReviewRating.status == "ok").count() >= 1


def test_normal_review_redirects_to_peer_review_review_for_peer_review_exam() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, _students = _peer_review_exam(session)

        response = review_queue(None, exam.id, db=session)

        assert response.status_code == 303
        assert response.headers["location"] == f"/exams/{exam.id}/peer-review/review"


def test_peer_review_rater_group_delete_allowed_before_ratings() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, _students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        group = add_rater_group(session, form, "Teachers", 50)

        delete_rater_group(session, form, group.id)

        session.refresh(group)
        assert group.active is False


def test_peer_review_rater_group_delete_blocked_after_batch_exists() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, _students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        group = add_rater_group(session, form, "Teachers", 50)
        create_peer_review_batch(session, form.id, group.id, "scan.pdf", "uploaded_scans/scan.pdf")

        try:
            delete_rater_group(session, form, group.id)
        except ValueError as exc:
            assert str(exc) == RATER_GROUP_DELETE_BLOCKED_MESSAGE
        else:
            raise AssertionError("Expected rater group delete to be blocked.")


def test_peer_review_rater_group_deactivation_blocked_after_ratings_exist() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        page = generate_peer_review_pages(session, exam)[0]
        group = form.rater_groups[0]
        comp_id = session.scalar(select(Competency.id).where(Competency.exam_id == exam.id))
        batch = create_peer_review_batch(
            session, form.id, group.id, "scan.pdf", "uploaded_scans/scan.pdf"
        )
        record_submission_page(
            session, batch, page, 1, "processed_pages/a.png", {(students[0].id, comp_id): 4}
        )

        try:
            update_rater_group(session, form, group.id, {"active": False})
        except ValueError as exc:
            assert str(exc) == RATER_GROUP_DELETE_BLOCKED_MESSAGE
        else:
            raise AssertionError("Expected rater group deactivation to be blocked.")


def test_peer_review_summary_export_includes_competency_columns() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        form.combine_groups = True
        form.rater_groups[0].name = "Teachers"
        form.rater_groups[0].weight_percent = 70
        session.add(Competency(exam_id=exam.id, name="Clarity", display_order=2))
        session.commit()
        student_group = add_rater_group(session, form, "Students", 30)
        page = generate_peer_review_pages(session, exam)[0]
        comp_ids = list(
            session.scalars(
                select(Competency.id)
                .where(Competency.exam_id == exam.id)
                .order_by(Competency.display_order)
            )
        )
        teacher_batch = create_peer_review_batch(
            session, form.id, form.rater_groups[0].id, "t.pdf", "uploaded_scans/t.pdf"
        )
        student_batch = create_peer_review_batch(
            session, form.id, student_group.id, "s.pdf", "uploaded_scans/s.pdf"
        )
        record_submission_page(
            session,
            teacher_batch,
            page,
            1,
            "processed_pages/t.png",
            {(students[0].id, comp_ids[0]): 5, (students[0].id, comp_ids[1]): 4},
        )
        record_submission_page(
            session,
            student_batch,
            page,
            1,
            "processed_pages/s.png",
            {(students[0].id, comp_ids[0]): 3, (students[0].id, comp_ids[1]): 2},
        )
        recalculate_anonymous_peer_review_results(session, form)

        relative_path = generate_peer_review_summary_export(session, form, "xlsx")
        headers, row = _first_export_row(relative_path)

        assert "Clarity_combined_average" in headers
        assert "Clarity_2_combined_average" in headers
        assert "Clarity_Teachers_average" in headers
        assert "Clarity_Students_count" in headers
        assert "reviewer_student_id" not in headers
        assert row["final_score"] is not None


def test_peer_review_summary_export_handles_combine_groups_false() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        form.combine_groups = False
        form.rater_groups[0].name = "Teachers"
        session.commit()
        page = generate_peer_review_pages(session, exam)[0]
        comp_id = session.scalar(select(Competency.id).where(Competency.exam_id == exam.id))
        batch = create_peer_review_batch(
            session, form.id, form.rater_groups[0].id, "t.pdf", "uploaded_scans/t.pdf"
        )
        record_submission_page(
            session, batch, page, 1, "processed_pages/t.png", {(students[0].id, comp_id): 5}
        )
        recalculate_anonymous_peer_review_results(session, form)

        relative_path = generate_peer_review_summary_export(session, form, "xlsx")
        headers, row = _first_export_row(relative_path)

        assert "Clarity_Teachers_average" in headers
        assert "Clarity_combined_average" not in headers
        assert row["final_score"] is None


def test_peer_review_generated_sheet_can_be_read_by_real_scanner(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, students = _peer_review_exam(session)
        session.add(Competency(exam_id=exam.id, name="Delivery", display_order=2))
        session.commit()
        form = get_or_create_form(session, exam)
        page = generate_peer_review_pages(session, exam)[0]
        comp_ids = list(
            session.scalars(
                select(Competency.id)
                .where(Competency.exam_id == exam.id)
                .order_by(Competency.display_order)
            )
        )
        image_path = _peer_review_scan_image(
            page,
            {
                (students[0].id, comp_ids[0]): 5,
                (students[0].id, comp_ids[1]): 3,
                (students[1].id, comp_ids[0]): 2,
            },
            file_name="generated_peer_review_scan.png",
        )
        batch = create_peer_review_batch(
            session, form.id, form.rater_groups[0].id, "scan.png", image_path
        )
        monkeypatch.setattr(
            peer_review_scan_service, "_extract_pages_for_peer_review", lambda _batch: [image_path]
        )

        summary = process_peer_review_batch(session, batch)

        assert summary.detected_pages == 1
        ratings = {
            (rating.target_student_id, rating.competency_id): rating.final_score
            for rating in session.query(PeerReviewRating)
            if rating.status == "ok"
        }
        assert ratings[(students[0].id, comp_ids[0])] == 5
        assert ratings[(students[0].id, comp_ids[1])] == 3
        assert ratings[(students[1].id, comp_ids[0])] == 2


def test_peer_review_stepper_print_link_uses_peer_review_route() -> None:
    template = Path("app/templates/exams/detail.html").read_text(encoding="utf-8")
    stepper = template.split("<!-- Clickable stepper", 1)[1].split(
        "{% for label, url in steps %}", 1
    )[0]
    peer_block = stepper.split('{% if exam.assessment_type == "peer_review" %}', 1)[1].split(
        "{% else %}", 1
    )[0]
    normal_block = stepper.split("{% else %}", 1)[1].split("{% endif %}", 1)[0]

    assert '"/exams/" ~ exam.id ~ "/peer-review/builder"' in peer_block
    assert '"/exams/" ~ exam.id ~ "/peer-review/scan-batches"' in peer_block
    assert '"/exams/" ~ exam.id ~ "/peer-review/review"' in peer_block
    assert '"/exams/" ~ exam.id ~ "/sheets"' not in peer_block
    assert '"/exams/" ~ exam.id ~ "/sheets"' in normal_block


def _peer_review_exam(session: Session) -> tuple[MockExam, list[Student]]:
    exam = MockExam(name="Presentation Review", assessment_type="peer_review")
    students = [
        Student(student_number="001", name="Student One", class_name="A"),
        Student(student_number="002", name="Student Two", class_name="A"),
    ]
    session.add_all([exam, *students])
    session.flush()
    for index, student in enumerate(students, start=1):
        session.add(ExamStudent(exam_id=exam.id, student_id=student.id, seat_order=index))
    session.add(Competency(exam_id=exam.id, name="Clarity", display_order=1))
    session.commit()
    return exam, students


def _blank_peer_review_image(file_name: str) -> str:
    output_dir = get_settings().data_root / "processed_pages"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / file_name
    cv2.imwrite(str(path), np.full((2970, 2100, 3), 255, dtype=np.uint8))
    return data_relative_path(path)


def _peer_review_scan_image(
    page,
    selected_scores: dict[tuple[int, int], int],
    file_name: str = "peer_review_scan.png",
) -> str:
    coordinate_map = page.coordinate_map_json
    scale = 10
    width = int(coordinate_map["page_width_mm"] * scale)
    height = int(coordinate_map["page_height_mm"] * scale)
    image = np.full((height, width, 3), 255, dtype=np.uint8)
    for marker in coordinate_map["marker_positions"].values():
        x = int(marker["x_mm"] * scale)
        y = int(marker["y_mm"] * scale)
        side = int(6 * scale)
        cv2.rectangle(image, (x, y), (x + side, y + side), (0, 0, 0), thickness=-1)
    # Place the QR exactly where the layout service puts it (top-right, above the table)
    # so the synthetic scan matches the real sheet and never covers a rating bubble.
    qr = coordinate_map.get("qr", {"x_mm": 168, "y_mm": 6, "size_mm": 22})
    qr_px = max(60, int(qr["size_mm"] * scale))
    qr_image = qrcode.make(page.qr_token).convert("RGB").resize((qr_px, qr_px))
    qy = int(qr["y_mm"] * scale)
    qx = int(qr["x_mm"] * scale)
    image[qy : qy + qr_px, qx : qx + qr_px] = np.array(qr_image)
    for bubble in coordinate_map["rating_bubbles"]:
        key = (bubble["target_student_id"], bubble["competency_id"])
        if selected_scores.get(key) != bubble["score"]:
            continue
        x = int(bubble["x_mm"] * scale)
        y = int(bubble["y_mm"] * scale)
        radius = max(4, int(bubble["radius_mm"] * scale))
        cv2.circle(image, (x, y), radius, (0, 0, 0), thickness=-1)
    output_dir = get_settings().data_root / "processed_pages"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / file_name
    cv2.imwrite(str(path), image)
    return data_relative_path(path)


def _first_export_row(relative_path: str) -> tuple[list[str], dict]:
    workbook = load_workbook(export_path(relative_path))
    sheet = workbook.active
    headers = [cell.value for cell in sheet[1]]
    values = [cell.value for cell in sheet[2]]
    return headers, dict(zip(headers, values, strict=False))
