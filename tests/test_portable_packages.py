"""Portable template/session packages: export zips, preview, and remapped imports."""

import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal, init_database
from app.main import create_app
from app.models import (
    BlankSheetLabel,
    Competency,
    ExamStudent,
    MockExam,
    PeerReviewFinalResult,
    PeerReviewForm,
    PeerReviewPage,
    PeerReviewRaterGroup,
    PhysicalSheet,
    Question,
    Response,
    ScanBatch,
    ScannedPage,
    ScoringSection,
    SheetInstance,
    SheetTemplate,
    Student,
    StudentResult,
)
from app.services.archive_service import PURGED_SCAN_PATH, archive_exam
from app.services.portable_package_service import (
    PackageError,
    _restore_embedded_path,
    export_session_package,
    export_template_package,
    import_session_package,
    import_template_package,
    read_package,
)


def _template_with_question(db, name="PkgTpl") -> int:
    exam = MockExam(
        name=name,
        assessment_type="generic_test",
        is_template=True,
        template_status="active",
    )
    sheet = PhysicalSheet(sheet_no=1, sheet_name="S", slug="s", display_order=1)
    section = ScoringSection(code="lk", name="LK")
    exam.physical_sheets.append(sheet)
    exam.scoring_sections.append(section)
    db.add(exam)
    db.commit()
    db.add_all(
        [
            Question(
                exam_id=exam.id,
                physical_sheet_id=sheet.id,
                scoring_section_id=section.id,
                question_no=1,
                display_label="q1",
                choice_count=4,
                correct_answer="2",
            ),
            Competency(exam_id=exam.id, name="Comp", min_score=1, max_score=5, display_order=1),
        ]
    )
    db.commit()
    return exam.id


def _session_with_results(db, name) -> int:
    exam = MockExam(
        name=name,
        assessment_type="generic_test",
        status="results_ready",
        is_template=False,
    )
    student = Student(student_number="P1", name="Pkg Student", class_name="A")
    sheet = PhysicalSheet(sheet_no=1, sheet_name="S", slug="s", display_order=1)
    section = ScoringSection(code="lk", name="LK")
    exam.physical_sheets.append(sheet)
    exam.scoring_sections.append(section)
    db.add_all([exam, student])
    db.commit()
    db.add(ExamStudent(exam_id=exam.id, student_id=student.id))
    question = Question(
        exam_id=exam.id,
        physical_sheet_id=sheet.id,
        scoring_section_id=section.id,
        question_no=1,
        display_label="q1",
        choice_count=4,
        correct_answer="2",
    )
    stpl = SheetTemplate(exam_id=exam.id, physical_sheet_id=sheet.id)
    db.add_all([question, stpl])
    db.commit()
    inst = SheetInstance(
        exam_id=exam.id,
        student_id=student.id,
        physical_sheet_id=sheet.id,
        sheet_template_id=stpl.id,
        qr_token=f"{name}-tok",
        printed_name=student.name,
        printed_student_number=student.student_number,
        pdf_file_path=f"uploaded_scans/{name}/inst.pdf",
    )
    batch = ScanBatch(
        exam_id=exam.id,
        uploaded_file_name="s.pdf",
        uploaded_file_path=f"uploaded_scans/{name}/s.pdf",
        total_pages=1,
    )
    db.add_all([inst, batch])
    db.commit()
    db.add(
        BlankSheetLabel(
            exam_id=exam.id,
            physical_sheet_id=sheet.id,
            sheet_template_id=stpl.id,
            qr_token=f"{name}-blank-token",
        )
    )
    db.commit()
    page = ScannedPage(
        scan_batch_id=batch.id,
        sheet_instance_id=inst.id,
        student_id=student.id,
        page_no=1,
        image_path=f"processed_pages/{name}/p.png",
    )
    db.add(page)
    db.commit()
    db.add_all(
        [
            Response(
                scanned_page_id=page.id,
                question_id=question.id,
                final_answer="2",
                is_correct=True,
                status="ok",
            ),
            StudentResult(
                exam_id=exam.id,
                student_id=student.id,
                total_score=120,
                pass_fail_status="pass",
            ),
        ]
    )
    db.commit()
    return exam.id


def _peer_session(db, name) -> int:
    exam = MockExam(
        name=name,
        assessment_type="peer_review",
        status="results_ready",
        scoring_mode="peer_average",
        is_template=False,
    )
    student = Student(student_number="PR1", name="Peer Student", class_name="A")
    db.add_all([exam, student])
    db.commit()
    db.add(ExamStudent(exam_id=exam.id, student_id=student.id))
    comp = Competency(exam_id=exam.id, name="Overall", min_score=1, max_score=5, display_order=1)
    db.add(comp)
    db.commit()
    form = PeerReviewForm(exam_id=exam.id, title=name, score_min=1, score_max=5, students_per_page=15)
    db.add(form)
    db.commit()
    db.add(
        PeerReviewPage(
            form_id=form.id,
            page_no=1,
            title="Page 1",
            qr_token=f"prp-{name}-token",
            coordinate_map_json={},
            target_rows_json=[],
        )
    )
    db.commit()
    db.add(PeerReviewRaterGroup(form_id=form.id, name="Students", weight_percent=100, display_order=1))
    db.add(
        PeerReviewFinalResult(
            exam_id=exam.id,
            form_id=form.id,
            target_student_id=student.id,
            final_score=4.0,
            total_rating_count=3,
        )
    )
    db.commit()
    return exam.id


# --- Template packages -------------------------------------------------------
def test_template_package_export_zip_excludes_runtime_data() -> None:
    init_database()
    with SessionLocal() as db:
        tid = _template_with_question(db, "PkgTplExport")
        path = export_template_package(db, db.get(MockExam, tid))
        package = read_package(path)
    assert package["manifest"]["schema_version"] == 2
    assert package["manifest"]["package_type"] == "template"
    template = package["template"]
    assert len(template["questions"]) == 1
    assert len(template["competencies"]) == 1
    for forbidden in ("students", "scans", "scanned_pages", "results", "student_results"):
        assert forbidden not in template


def test_template_import_creates_active_remapped_template() -> None:
    init_database()
    with SessionLocal() as db:
        tid = _template_with_question(db, "PkgTplImport")
        original = db.get(MockExam, tid)
        orig_q_ids = {q.id for q in original.questions}
        package = read_package(export_template_package(db, original))
        new_template = import_template_package(db, package)
        db.refresh(new_template)
        assert new_template.id != tid
        assert new_template.is_template is True
        assert new_template.template_status == "active"
        assert {q.id for q in new_template.questions}.isdisjoint(orig_q_ids)


def test_template_package_routes_preview_and_confirm() -> None:
    init_database()
    with SessionLocal() as db:
        tid = _template_with_question(db, "PkgTplRoute")
    with TestClient(create_app()) as client:
        export = client.get(f"/templates/{tid}/portable-export")
        assert export.status_code == 200
        files = {"file": ("t.ezmark-template.zip", export.content, "application/zip")}
        preview = client.post("/templates/import-package/preview", files=files)
        assert preview.status_code == 200
        assert "PkgTplRoute" in preview.text
        token = preview.text.split('name="token" value="')[1].split('"')[0]
        confirm = client.post(
            "/templates/import-package/confirm", data={"token": token}, follow_redirects=False
        )
        assert confirm.status_code == 303
        assert client.get(confirm.headers["location"]).status_code == 200


# --- Session packages --------------------------------------------------------
def test_session_package_export_includes_template_roster_results() -> None:
    init_database()
    with SessionLocal() as db:
        sid = _session_with_results(db, "PkgSessExport")
        path = export_session_package(db, db.get(MockExam, sid))
    archive = zipfile.ZipFile(path)
    names = set(archive.namelist())
    assert {"manifest.json", "template/template.json", "session/session.json", "session/results.json"} <= names
    package = read_package(path)
    assert package["manifest"]["package_type"] == "session"
    assert len(package["session"]["students"]) == 1
    assert len(package["results"]["student_results"]) == 1
    assert len(package["template"]["questions"]) == 1


def test_archived_session_package_excludes_scan_files() -> None:
    init_database()
    with SessionLocal() as db:
        sid = _session_with_results(db, "PkgSessArchived")
        archive_exam(db, db.get(MockExam, sid))
        package = read_package(export_session_package(db, db.get(MockExam, sid)))
    assert package["manifest"]["include_scan_files"] is False
    assert package["manifest"]["archived_source"] is True
    assert package["session"]["scanned_pages"][0]["image_path"] == PURGED_SCAN_PATH


def test_session_import_links_template_and_remaps_ids() -> None:
    init_database()
    with SessionLocal() as db:
        sid = _session_with_results(db, "PkgSessImport")
        original = db.get(MockExam, sid)
        orig_student_ids = {link.student_id for link in original.students}
        package = read_package(export_session_package(db, original))
        template, session = import_session_package(db, package)
        db.refresh(session)
        assert template.is_template is True
        assert session.is_template is False
        assert session.source_template_id == template.id
        assert session.id != sid
        new_student_ids = {link.student_id for link in session.students}
        assert new_student_ids.isdisjoint(orig_student_ids)
        assert len(session.results) == 1


def test_session_import_preserves_non_colliding_qr_tokens() -> None:
    init_database()
    with SessionLocal() as db:
        sid = _session_with_results(db, "PkgSessPreserve")
        package = read_package(export_session_package(db, db.get(MockExam, sid)))
        package["session"]["sheet_instances"][0]["qr_token"] = "preserved-sheet-token"
        package["session"]["blank_sheet_labels"][0]["qr_token"] = "preserved-blank-token"

        _template, session = import_session_package(db, package)
        db.refresh(session)

        assert session.sheet_instances[0].qr_token == "preserved-sheet-token"
        blank = db.scalar(select(BlankSheetLabel).where(BlankSheetLabel.exam_id == session.id))
        assert blank.qr_token == "preserved-blank-token"
        assert package["import_warnings"] == []


def test_session_import_regenerates_only_colliding_qr_tokens() -> None:
    init_database()
    with SessionLocal() as db:
        sid = _session_with_results(db, "PkgSessCollision")
        package = read_package(export_session_package(db, db.get(MockExam, sid)))
        package["session"]["blank_sheet_labels"][0]["qr_token"] = "noncolliding-blank-token"

        _template, session = import_session_package(db, package)
        db.refresh(session)

        assert session.sheet_instances[0].qr_token != "PkgSessCollision-tok"
        blank = db.scalar(select(BlankSheetLabel).where(BlankSheetLabel.exam_id == session.id))
        assert blank.qr_token == "noncolliding-blank-token"
        assert package["import_warnings"]


def test_session_import_regenerates_only_colliding_blank_label_qr_tokens() -> None:
    init_database()
    with SessionLocal() as db:
        sid = _session_with_results(db, "PkgBlankCollision")
        package = read_package(export_session_package(db, db.get(MockExam, sid)))
        package["session"]["sheet_instances"][0]["qr_token"] = "noncolliding-sheet-token"

        _template, session = import_session_package(db, package)
        db.refresh(session)

        assert session.sheet_instances[0].qr_token == "noncolliding-sheet-token"
        blank = db.scalar(select(BlankSheetLabel).where(BlankSheetLabel.exam_id == session.id))
        assert blank.qr_token != "PkgBlankCollision-blank-token"
        assert len(package["import_warnings"]) == 1


def test_session_import_routes_show_both_cards_and_open() -> None:
    init_database()
    with SessionLocal() as db:
        sid = _session_with_results(db, "PkgSessRoute")
    with TestClient(create_app()) as client:
        export = client.get(f"/exams/{sid}/portable-export")
        assert export.status_code == 200
        files = {"file": ("s.ezmark-session.zip", export.content, "application/zip")}
        preview = client.post("/sessions/import-package/preview", files=files)
        assert preview.status_code == 200
        # Preview shows both the template card and session card.
        assert "PkgSessRoute" in preview.text
        token = preview.text.split('name="token" value="')[1].split('"')[0]
        confirm = client.post("/sessions/import-package/confirm", data={"token": token})
        assert confirm.status_code == 200
        assert "QR code token" in confirm.text
        assert "new QR codes were generated" in confirm.text
        # Success page links to both the imported template and session; the session opens.
        session_link = [
            seg for seg in confirm.text.split('"') if seg.startswith("/exams/")
        ][0]
        assert client.get(session_link).status_code == 200


def test_peer_session_package_includes_peer_results() -> None:
    init_database()
    with SessionLocal() as db:
        sid = _peer_session(db, "PkgPeer")
        package = read_package(export_session_package(db, db.get(MockExam, sid)))
        assert "peer_review" in package
        assert len(package["peer_review"]["final_results"]) == 1
        package["peer_review"]["pages"][0]["qr_token"] = "preserved-peer-token"
        template, session = import_session_package(db, package)
        db.refresh(session)
        assert session.assessment_type == "peer_review"
        assert session.source_template_id == template.id
        form = db.scalar(select(PeerReviewForm).where(PeerReviewForm.exam_id == session.id))
        assert form.pages[0].qr_token == "preserved-peer-token"
    with TestClient(create_app()) as client:
        assert client.get(f"/exams/{session.id}").status_code == 200


def test_peer_session_import_regenerates_only_colliding_page_qr_tokens() -> None:
    init_database()
    with SessionLocal() as db:
        sid = _peer_session(db, "PkgPeerCollision")
        package = read_package(export_session_package(db, db.get(MockExam, sid)))

        _template, session = import_session_package(db, package)
        db.refresh(session)

        form = db.scalar(select(PeerReviewForm).where(PeerReviewForm.exam_id == session.id))
        assert form.pages[0].qr_token != "prp-PkgPeerCollision-token"
        assert len(package["import_warnings"]) == 1


def test_wrong_package_type_is_rejected() -> None:
    init_database()
    with SessionLocal() as db:
        tid = _template_with_question(db, "PkgWrongType")
        template_path = export_template_package(db, db.get(MockExam, tid))
    with TestClient(create_app()) as client:
        files = {"file": ("t.zip", template_path.read_bytes(), "application/zip")}
        # A template package uploaded to the session importer must be rejected.
        rejected = client.post("/sessions/import-package/preview", files=files)
        assert rejected.status_code == 400


def test_invalid_zip_rejected() -> None:
    with TestClient(create_app()) as client:
        files = {"file": ("bad.zip", b"not a zip", "application/zip")}
        assert client.post("/templates/import-package/preview", files=files).status_code == 400
        # A real zip without a valid manifest is also rejected.
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("random.txt", "hello")
        files = {"file": ("empty.zip", buffer.getvalue(), "application/zip")}
        assert client.post("/templates/import-package/preview", files=files).status_code == 400


def test_incompatible_schema_version_rejected_at_service_level() -> None:
    """Packages whose manifest schema_version is not 2 are rejected (old private
    packages and any other/unknown version), with a generic safe error."""
    init_database()
    with SessionLocal() as db:
        tid = _template_with_question(db, "PkgSchemaVersion")
        path = export_template_package(db, db.get(MockExam, tid))
    for bad_version in (1, 3, 99):
        package = read_package(path)
        package["manifest"]["schema_version"] = bad_version
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(package["manifest"]))
            archive.writestr("template.json", json.dumps(package["template"]))
        target = path.with_name(f"bad-v{bad_version}.zip")
        target.write_bytes(buffer.getvalue())
        with pytest.raises(PackageError) as exc:
            read_package(target)
        assert "incompatible EZ Mark schema version" in str(exc.value)


def test_incompatible_schema_version_rejected_at_routes() -> None:
    init_database()
    with SessionLocal() as db:
        tid = _template_with_question(db, "PkgSchemaRoute")
        path = export_template_package(db, db.get(MockExam, tid))
    package = read_package(path)
    package["manifest"]["schema_version"] = 1
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(package["manifest"]))
        archive.writestr("template.json", json.dumps(package["template"]))
    with TestClient(create_app()) as client:
        files = {"file": ("t.zip", buffer.getvalue(), "application/zip")}
        # Preview must reject the incompatible package with the generic message...
        preview = client.post("/templates/import-package/preview", files=files)
        assert preview.status_code == 400
        assert "incompatible EZ Mark schema version" in preview.text
        # ...and confirm must reject it too, not reinterpret it silently.
        from app.routes.test_templates import PREVIEW_DIR, PREVIEW_TOKEN_RE

        token = "e" * 32
        assert PREVIEW_TOKEN_RE.match(token)
        preview_dir = get_settings().data_root / PREVIEW_DIR
        preview_dir.mkdir(parents=True, exist_ok=True)
        (preview_dir / f"{token}.zip").write_bytes(buffer.getvalue())
        confirm = client.post("/templates/import-package/confirm", data={"token": token})
        assert confirm.status_code == 400
        assert "incompatible EZ Mark schema version" in confirm.text


def test_path_traversal_in_embedded_file_reference_is_refused() -> None:
    """A package whose payload points outside files/ must not escape the data root."""
    init_database()
    with SessionLocal() as db:
        sid = _session_with_results(db, "PkgZipSlip")
        export = export_session_package(db, db.get(MockExam, sid))
    package = read_package(export)
    package["session"]["sheet_instances"][0]["pdf_file_path"] = {
        "original_relative_path": "uploaded_scans/evil.pdf",
        "package_file_path": "files/../../evil.pdf",
    }
    # Rebuild the zip so the malicious reference is baked in before the import's
    # embedded-file restore pass runs (read_package re-validates it).
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(package["manifest"]))
        archive.writestr("template/template.json", json.dumps(package["template"]))
        archive.writestr("session/session.json", json.dumps(package["session"]))
        archive.writestr("session/results.json", json.dumps(package["results"]))
    malicious = export.with_name("malicious-traversal.zip")
    malicious.write_bytes(buffer.getvalue())
    with SessionLocal() as db:
        _template, session = import_session_package(db, read_package(malicious))
        db.refresh(session)
        assert session.sheet_instances[0].pdf_file_path == PURGED_SCAN_PATH
        outside = get_settings().data_root.parent / "evil.pdf"
        assert not outside.exists()
    # A payload claiming an absolute "original path" is refused as well.
    assert (
        _restore_embedded_path(
            {"original_relative_path": str(Path("C:/evil") / "evil.pdf")}
        )
        == PURGED_SCAN_PATH
    )


def test_session_package_with_scan_files_round_trips() -> None:
    """include_scan_files=True embeds binaries; the import restores readable paths."""
    init_database()
    with SessionLocal() as db:
        sid = _session_with_results(db, "PkgSessScans")
        exam = db.get(MockExam, sid)
        # Materialize the scan files the payload points at (relative to data root).
        data_root = get_settings().data_root
        pdf_rel = exam.sheet_instances[0].pdf_file_path
        (data_root / pdf_rel).parent.mkdir(parents=True, exist_ok=True)
        (data_root / pdf_rel).write_bytes(b"%PDF-1.4 synthetic")
        page_rel = exam.scan_batches[0].scanned_pages[0].image_path
        (data_root / page_rel).parent.mkdir(parents=True, exist_ok=True)
        (data_root / page_rel).write_bytes(b"nyi")

        package = read_package(
            export_session_package(db, exam, include_scan_files=True)
        )
        assert package["manifest"]["embedded_file_count"] >= 1
        _template, session = import_session_package(db, package)
        db.refresh(session)
        restored_pdf = session.sheet_instances[0].pdf_file_path
        assert restored_pdf and restored_pdf != PURGED_SCAN_PATH
        assert (data_root / restored_pdf).read_bytes() == b"%PDF-1.4 synthetic"
        restored_page = session.scan_batches[0].scanned_pages[0].image_path
        assert restored_page and restored_page != PURGED_SCAN_PATH
        assert (data_root / restored_page).read_bytes() == b"nyi"


def test_session_package_without_scan_files_marks_paths_purged() -> None:
    """The default export embeds no binaries; imported scan paths are the purged sentinel."""
    init_database()
    with SessionLocal() as db:
        sid = _session_with_results(db, "PkgSessNoScans")
        package = read_package(export_session_package(db, db.get(MockExam, sid)))
        template, session = import_session_package(db, package)
        db.refresh(session)
        assert session.status == "archived"
        assert session.sheet_instances[0].pdf_file_path == PURGED_SCAN_PATH
        assert session.scan_batches[0].scanned_pages[0].image_path == PURGED_SCAN_PATH
        assert template.is_template is True


def test_fractional_scores_and_pass_mark_preserved() -> None:
    """points, fractional totals, and the explicit pass-mark type/value all round-trip."""
    init_database()
    with SessionLocal() as db:
        sid = _session_with_results(db, "PkgFrac")
        exam = db.get(MockExam, sid)
        exam.pass_mark_value = 0.6
        exam.pass_mark_type = "ratio"
        question = exam.questions[0]
        question.points = 0.5
        result = db.scalar(select(StudentResult).where(StudentResult.exam_id == sid))
        result.total_score = 7.5
        result.score_details_json = {"points_earned": 7.5, "points_possible": 10}
        result.pass_fail_status = "pass"
        db.commit()

        package_template, session = import_session_package(
            db, read_package(export_session_package(db, exam))
        )
        db.refresh(session)
        assert session.pass_mark_value == pytest.approx(0.6)
        assert session.pass_mark_type == "ratio"
        assert session.questions[0].points == pytest.approx(0.5)  # points, not integer
        imported = db.scalar(select(StudentResult).where(StudentResult.exam_id == session.id))
        assert imported.total_score == pytest.approx(7.5)
        assert imported.score_details_json == {"points_earned": 7.5, "points_possible": 10}
        assert imported.pass_fail_status == "pass"
        assert package_template.is_template is True


def test_peer_template_package_round_trip() -> None:
    """A peer evaluation template exports/imports with its form and rater groups."""
    init_database()
    with SessionLocal() as db:
        exam = MockExam(
            name="PeerTplPkg",
            assessment_type="peer_review",
            scoring_mode="peer_average",
            is_template=True,
        )
        db.add(exam)
        db.commit()
        db.add(Competency(exam_id=exam.id, name="Teamwork", min_score=1, max_score=5))
        db.commit()
        form = PeerReviewForm(exam_id=exam.id, title="Peer Tpl", score_min=1, score_max=5)
        db.add(form)
        db.commit()
        db.add(PeerReviewRaterGroup(form_id=form.id, name="Students", weight_percent=100))
        db.commit()
        tid = exam.id

        package = read_package(export_template_package(db, db.get(MockExam, tid)))
        assert package["template"]["assessment_type"] == "peer_review"
        assert package["template"]["peer_review_form"]["title"] == "Peer Tpl"
        imported = import_template_package(db, package)
        db.refresh(imported)
        assert imported.id != tid
        new_form = db.scalar(select(PeerReviewForm).where(PeerReviewForm.exam_id == imported.id))
        assert new_form is not None
        assert new_form.title == "Peer Tpl"
        assert [g.name for g in new_form.rater_groups] == ["Students"]
        assert new_form.anonymous is True


def test_peer_template_package_has_no_runtime_student_data() -> None:
    init_database()
    with SessionLocal() as db:
        build_id = _peer_session(db, "PkgPeerTplScrub")
        package = read_package(export_template_package(db, db.get(MockExam, build_id)))
    for forbidden in ("students", "batches", "ratings", "final_results", "submission_pages"):
        assert forbidden not in package["template"]
        assert forbidden not in package["template"].get("peer_review_form", {})
