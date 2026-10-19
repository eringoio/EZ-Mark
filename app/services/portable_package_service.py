"""Portable template/session packages: self-contained .zip export and import.

This is intentionally separate from the result-export system in ``export_service`` /
``routes/exports.py`` (which produces grade spreadsheets). A *portable package* moves a
whole template — or a whole session together with its template — between EZ Mark
installs. Database ids are never reused: every imported row gets a fresh id, and internal
references are remapped through original-id -> new-object maps built during import.

Package types and file extensions:
  * template -> ``<name>.ezmark-template.zip`` (manifest.json + template.json)
  * session  -> ``<name>.ezmark-session.zip``  (manifest.json + template/template.json +
                session/session.json + session/results.json + session/peer_review.json)

Scan binaries are only embedded when ``include_scan_files=True`` and the session is not
archived; otherwise scan path columns are recorded as the purged sentinel so the imported
session opens cleanly and image routes refuse to serve missing files.
"""

import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path, PureWindowsPath
from secrets import token_urlsafe

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    BlankSheetLabel,
    Competency,
    ExamStudent,
    MockExam,
    PeerReviewBatch,
    PeerReviewFinalResult,
    PeerReviewForm,
    PeerReviewGroupResult,
    PeerReviewPage,
    PeerReviewRaterGroup,
    PeerReviewRating,
    PeerReviewSubmissionPage,
    PeerReviewTarget,
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
    StudentResult,
)
from app.services.archive_service import PURGED_SCAN_PATH
from app.services.i18n_service import _
from app.services.qr_token_service import generate_qr_token

# Public package schema version. Version 2 is the first *public* EZ Mark package
# schema (the internal pre-migration format used private legacy fields and never
# shipped as a public package format). Import only accepts version 2 exactly;
# anything else is rejected with a generic error so old private packages are
# never reinterpreted or auto-migrated.
SCHEMA_VERSION = 2
APP_NAME = "EZ Mark"
TEMPLATE_EXT = ".ezmark-template.zip"
SESSION_EXT = ".ezmark-session.zip"
SCAN_PATH_KEYS = {
    "pdf_file_path",
    "uploaded_file_path",
    "image_path",
    "aligned_image_path",
}


class PackageError(ValueError):
    """Raised for malformed or wrong-type packages (surfaced to the user)."""


def _qr_token_exists(db: Session, token: str) -> bool:
    return any(
        db.scalar(select(model.id).where(model.qr_token == token).limit(1)) is not None
        for model in (SheetInstance, BlankSheetLabel, PeerReviewPage)
    )


def _preserved_or_new_qr_token(
    db: Session,
    token: str | None,
    *,
    warnings: list[str],
    label: str,
    generator=generate_qr_token,
) -> str:
    if token and not _qr_token_exists(db, token):
        return token

    new_token = generator()
    while _qr_token_exists(db, new_token):
        new_token = generator()
    if token:
        warnings.append(
            f"QR token collision for {label}; generated a new token for the imported record."
        )
    return new_token


# --------------------------------------------------------------------------- #
# Structure serialization (shared by templates and a session's own setup)
# --------------------------------------------------------------------------- #
def _peer_form(db: Session, exam: MockExam) -> PeerReviewForm | None:
    return db.scalar(select(PeerReviewForm).where(PeerReviewForm.exam_id == exam.id))


def serialize_structure(db: Session, exam: MockExam) -> dict:
    """Serialize the reusable structure of an exam (template or session).

    Captures assessment metadata, physical sheets, scoring sections, problem groups,
    questions, competencies, and (for peer review) the form settings + rater groups.
    Never includes students, scans, responses, ratings, or results.
    """
    payload = {
        "name": exam.name,
        "assessment_type": exam.assessment_type,
        "scoring_mode": exam.scoring_mode,
        "numbering_mode": exam.numbering_mode,
        "pass_mark_value": exam.pass_mark_value,
        "pass_mark_type": exam.pass_mark_type,
        "template_description": exam.template_description,
        "physical_sheets": [
            {
                "id": sheet.id,
                "sheet_no": sheet.sheet_no,
                "sheet_name": sheet.sheet_name,
                "slug": sheet.slug,
                "required": sheet.required,
                "display_order": sheet.display_order,
            }
            for sheet in exam.physical_sheets
        ],
        "scoring_sections": [
            {
                "id": section.id,
                "code": section.code,
                "name": section.name,
                "display_order": section.display_order,
                "description": section.description,
            }
            for section in exam.scoring_sections
        ],
        "problem_groups": [
            {
                "id": group.id,
                "physical_sheet_id": group.physical_sheet_id,
                "problem_no": group.problem_no,
                "problem_title": group.problem_title,
                "subquestion_count": group.subquestion_count,
                "display_order": group.display_order,
            }
            for group in exam.problem_groups
        ],
        "questions": [
            {
                "id": q.id,
                "physical_sheet_id": q.physical_sheet_id,
                "scoring_section_id": q.scoring_section_id,
                "question_no": q.question_no,
                "problem_no": q.problem_no,
                "subquestion_no": q.subquestion_no,
                "local_question_no": q.local_question_no,
                "local_question_label": q.local_question_label,
                "display_label": q.display_label,
                "test_section": q.test_section,
                "correct_answer": q.correct_answer,
                "choice_count": q.choice_count,
                "points": q.points,
                "item_type": q.item_type,
                "display_order": q.display_order,
            }
            for q in exam.questions
        ],
        "competencies": [
            {
                "id": c.id,
                "name": c.name,
                "name_furigana": c.name_furigana,
                "short_label": c.short_label,
                "name_ruby_json": c.name_ruby_json,
                "description": c.description,
                "min_score": c.min_score,
                "max_score": c.max_score,
                "display_order": c.display_order,
            }
            for c in exam.competencies
        ],
    }
    if exam.assessment_type == "peer_review":
        form = _peer_form(db, exam)
        if form is not None:
            payload["peer_review_form"] = {
                "title": form.title,
                "anonymous": form.anonymous,
                "score_min": form.score_min,
                "score_max": form.score_max,
                "students_per_page": form.students_per_page,
                "page_orientation_mode": form.page_orientation_mode,
                "layout_density": form.layout_density,
                "combine_groups": form.combine_groups,
                "missing_group_policy": form.missing_group_policy,
                "rater_groups": [
                    {
                        "id": g.id,
                        "name": g.name,
                        "description": g.description,
                        "weight_percent": g.weight_percent,
                        "display_order": g.display_order,
                        "active": g.active,
                    }
                    for g in form.rater_groups
                ],
            }
    return payload


@dataclass
class _StructureMaps:
    sheets: dict[int, PhysicalSheet] = field(default_factory=dict)
    sections: dict[int, ScoringSection] = field(default_factory=dict)
    groups: dict[int, ProblemGroup] = field(default_factory=dict)
    questions: dict[int, Question] = field(default_factory=dict)
    competencies: dict[int, Competency] = field(default_factory=dict)
    rater_groups: dict[int, PeerReviewRaterGroup] = field(default_factory=dict)
    form: PeerReviewForm | None = None


def build_exam_from_structure(
    db: Session,
    payload: dict,
    *,
    name: str,
    is_template: bool,
    source_template_id: int | None = None,
    exam_date: str | None = None,
) -> tuple[MockExam, _StructureMaps]:
    """Create a fresh MockExam (template or run) from a serialized structure payload.

    Mirrors ``duplicate_template`` / ``create_run_from_template`` but reads from JSON.
    Returns the new exam plus original-id -> new-object maps for layering session data.
    """
    exam = MockExam(
        name=name.strip(),
        assessment_type=payload["assessment_type"],
        scoring_mode=payload["scoring_mode"],
        numbering_mode=payload.get("numbering_mode"),
        pass_mark_value=payload.get("pass_mark_value"),
        pass_mark_type=payload.get("pass_mark_type"),
        exam_date=exam_date or None,
        template_description=payload.get("template_description"),
        status="draft",
        is_template=is_template,
        template_status="active" if is_template else "active",
        source_template_id=source_template_id,
    )
    maps = _StructureMaps()
    for sheet in payload.get("physical_sheets", []):
        new_sheet = PhysicalSheet(
            sheet_no=sheet["sheet_no"],
            sheet_name=sheet["sheet_name"],
            slug=sheet["slug"],
            required=sheet["required"],
            display_order=sheet["display_order"],
        )
        exam.physical_sheets.append(new_sheet)
        maps.sheets[sheet["id"]] = new_sheet
    for section in payload.get("scoring_sections", []):
        new_section = ScoringSection(
            code=section["code"],
            name=section["name"],
            display_order=section["display_order"],
            description=section.get("description"),
        )
        exam.scoring_sections.append(new_section)
        maps.sections[section["id"]] = new_section
    for group in payload.get("problem_groups", []):
        new_group = ProblemGroup(
            problem_no=group["problem_no"],
            problem_title=group.get("problem_title"),
            subquestion_count=group["subquestion_count"],
            display_order=group["display_order"],
        )
        if group.get("physical_sheet_id") is not None:
            new_group.physical_sheet = maps.sheets.get(group["physical_sheet_id"])
        exam.problem_groups.append(new_group)
        maps.groups[group["id"]] = new_group
    for q in payload.get("questions", []):
        new_q = Question(
            question_no=q["question_no"],
            problem_no=q["problem_no"],
            subquestion_no=q["subquestion_no"],
            local_question_no=q["local_question_no"],
            local_question_label=q.get("local_question_label"),
            display_label=q["display_label"],
            test_section=q.get("test_section"),
            correct_answer=q.get("correct_answer"),
            choice_count=q["choice_count"],
            points=q["points"],
            item_type=q.get("item_type"),
            display_order=q["display_order"],
        )
        new_q.physical_sheet = maps.sheets[q["physical_sheet_id"]]
        if q.get("scoring_section_id") is not None:
            new_q.scoring_section = maps.sections.get(q["scoring_section_id"])
        exam.questions.append(new_q)
        maps.questions[q["id"]] = new_q
    for c in payload.get("competencies", []):
        new_c = Competency(
            name=c["name"],
            name_furigana=c.get("name_furigana"),
            short_label=c.get("short_label"),
            name_ruby_json=c.get("name_ruby_json"),
            description=c.get("description"),
            min_score=c["min_score"],
            max_score=c["max_score"],
            display_order=c["display_order"],
        )
        exam.competencies.append(new_c)
        maps.competencies[c["id"]] = new_c
    db.add(exam)
    db.flush()  # assign ids for FK targets (competencies use exam_id, form below)

    peer = payload.get("peer_review_form")
    if peer is not None:
        form = PeerReviewForm(
            exam_id=exam.id,
            title=peer.get("title") or exam.name,
            anonymous=peer.get("anonymous", True),
            score_min=peer["score_min"],
            score_max=peer["score_max"],
            students_per_page=peer.get("students_per_page", 15),
            page_orientation_mode=peer.get("page_orientation_mode", "auto"),
            layout_density=peer.get("layout_density", "standard"),
            combine_groups=peer.get("combine_groups", False),
            missing_group_policy=peer.get("missing_group_policy", "normalize_available"),
        )
        db.add(form)
        db.flush()
        maps.form = form
        for g in peer.get("rater_groups", []):
            new_g = PeerReviewRaterGroup(
                form_id=form.id,
                name=g["name"],
                description=g.get("description"),
                weight_percent=g["weight_percent"],
                display_order=g["display_order"],
                active=g.get("active", True),
            )
            db.add(new_g)
            db.flush()
            maps.rater_groups[g["id"]] = new_g
    return exam, maps


# --------------------------------------------------------------------------- #
# Session runtime serialization (roster, scans, responses, results, peer data)
# --------------------------------------------------------------------------- #
def serialize_session_runtime(db: Session, exam: MockExam, *, include_scan_files: bool) -> dict:
    """Serialize the session-specific data layered on top of the shared structure."""
    students = list(
        db.scalars(select(Student).join(ExamStudent).where(ExamStudent.exam_id == exam.id))
    )
    # A student may also be referenced only via results/instances; include the full set.
    student_ids = {s.id for s in students}
    for link in exam.students:
        if link.student_id not in student_ids:
            student = db.get(Student, link.student_id)
            if student is not None:
                students.append(student)
                student_ids.add(student.id)

    data: dict = {
        "name": exam.name,
        "assessment_type": exam.assessment_type,
        "exam_date": exam.exam_date,
        "scoring_mode": exam.scoring_mode,
        "numbering_mode": exam.numbering_mode,
        "pass_mark_value": exam.pass_mark_value,
        "pass_mark_type": exam.pass_mark_type,
        "status": exam.status,
        "archived": exam.status == "archived",
        "students": [
            {
                "id": s.id,
                "student_number": s.student_number,
                "name": s.name,
                "class_name": s.class_name,
                "school_management_id": s.school_management_id,
            }
            for s in students
        ],
        "exam_students": [
            {
                "student_id": link.student_id,
                "seat_order": link.seat_order,
                "included": link.included,
                "class_name": link.group_name,
            }
            for link in exam.students
        ],
    }
    if exam.assessment_type != "peer_review":
        _serialize_normal_runtime(db, exam, data, include_scan_files=include_scan_files)
    return data


def _scan_path(value: str | None, include_scan_files: bool) -> str | None:
    """Keep the path when binaries travel with the package; otherwise mark it purged."""
    if value is None:
        return None
    if value == PURGED_SCAN_PATH:
        return PURGED_SCAN_PATH
    return value if include_scan_files else PURGED_SCAN_PATH


def _serialize_normal_runtime(
    db: Session, exam: MockExam, data: dict, *, include_scan_files: bool
) -> None:
    data["sheet_templates"] = [
        {
            "id": t.id,
            "physical_sheet_id": t.physical_sheet_id,
            "version": t.version,
            "paper_size": t.paper_size,
            "coordinate_map_json": t.coordinate_map_json,
            "marker_positions_json": t.marker_positions_json,
        }
        for t in exam.sheet_templates
    ]
    data["sheet_instances"] = [
        {
            "id": i.id,
            "student_id": i.student_id,
            "physical_sheet_id": i.physical_sheet_id,
            "sheet_template_id": i.sheet_template_id,
            "qr_token": i.qr_token,
            "printed_name": i.printed_name,
            "printed_student_number": i.printed_student_number,
            "pdf_file_path": _scan_path(i.pdf_file_path, include_scan_files),
            "page_no": i.page_no,
            "status": i.status,
        }
        for i in exam.sheet_instances
    ]
    data["blank_sheet_labels"] = [
        {
            "physical_sheet_id": b.physical_sheet_id,
            "sheet_template_id": b.sheet_template_id,
            "qr_token": b.qr_token,
            "pdf_file_path": _scan_path(b.pdf_file_path, include_scan_files),
        }
        for b in db.scalars(select(BlankSheetLabel).where(BlankSheetLabel.exam_id == exam.id))
    ]
    batches = list(exam.scan_batches)
    data["scan_batches"] = [
        {
            "id": b.id,
            "uploaded_file_name": b.uploaded_file_name,
            "uploaded_file_path": _scan_path(b.uploaded_file_path, include_scan_files),
            "status": b.status,
            "total_pages": b.total_pages,
            "detected_pages": b.detected_pages,
            "problem_pages": b.problem_pages,
            "error_message": b.error_message,
        }
        for b in batches
    ]
    data["scanned_pages"] = [
        {
            "id": p.id,
            "scan_batch_id": p.scan_batch_id,
            "sheet_instance_id": p.sheet_instance_id,
            "student_id": p.student_id,
            "physical_sheet_id": p.physical_sheet_id,
            "page_no": p.page_no,
            "image_path": _scan_path(p.image_path, include_scan_files) or PURGED_SCAN_PATH,
            "aligned_image_path": _scan_path(p.aligned_image_path, include_scan_files),
            "qr_token": p.qr_token,
            "qr_status": p.qr_status,
            "alignment_status": p.alignment_status,
            "review_status": p.review_status,
            "confidence_summary": p.confidence_summary,
            "duplicate_of_page_id": p.duplicate_of_page_id,
            "replaced_by_page_id": p.replaced_by_page_id,
            "source_exam_id": p.source_exam_id,
            "source_sheet_instance_id": p.source_sheet_instance_id,
            "mapping_note": p.mapping_note,
        }
        for b in batches
        for p in b.scanned_pages
    ]
    response_rows = list(
        db.scalars(select(Response).join(Question).where(Question.exam_id == exam.id))
    )
    data["responses"] = [
        {
            "scanned_page_id": r.scanned_page_id,
            "question_id": r.question_id,
            "detected_answer": r.detected_answer,
            "final_answer": r.final_answer,
            "is_correct": r.is_correct,
            "confidence": r.confidence,
            "fill_values_json": r.fill_values_json,
            "status": r.status,
            "reviewed_by": r.reviewed_by,
        }
        for r in response_rows
    ]
    data["student_results"] = [
        {
            "student_id": res.student_id,
            "raw_json": res.raw_json,
            "score_details_json": res.score_details_json,
            "total_score": res.total_score,
            "pass_fail_status": res.pass_fail_status,
        }
        for res in exam.results
    ]


def serialize_peer_runtime(db: Session, exam: MockExam, *, include_scan_files: bool) -> dict:
    """Peer-review session runtime: targets, pages, scan batches, ratings, results."""
    form = _peer_form(db, exam)
    if form is None:
        return {}
    batches = list(db.scalars(select(PeerReviewBatch).where(PeerReviewBatch.form_id == form.id)))
    submission_pages = list(
        db.scalars(
            select(PeerReviewSubmissionPage)
            .join(PeerReviewBatch, PeerReviewSubmissionPage.batch_id == PeerReviewBatch.id)
            .where(PeerReviewBatch.form_id == form.id)
        )
    )
    ratings = list(db.scalars(select(PeerReviewRating).where(PeerReviewRating.form_id == form.id)))
    finals = list(
        db.scalars(select(PeerReviewFinalResult).where(PeerReviewFinalResult.form_id == form.id))
    )
    groups = list(
        db.scalars(select(PeerReviewGroupResult).where(PeerReviewGroupResult.form_id == form.id))
    )
    return {
        "targets": [
            {
                "id": t.id,
                "student_id": t.student_id,
                "display_label": t.display_label,
                "display_order": t.display_order,
                "active": t.active,
            }
            for t in form.targets
        ],
        "pages": [
            {
                "id": p.id,
                "page_no": p.page_no,
                "title": p.title,
                "qr_token": p.qr_token,
                "coordinate_map_json": p.coordinate_map_json,
                "target_rows_json": p.target_rows_json,
            }
            for p in form.pages
        ],
        "batches": [
            {
                "id": b.id,
                "rater_group_id": b.rater_group_id,
                "uploaded_file_name": b.uploaded_file_name,
                "uploaded_file_path": _scan_path(b.uploaded_file_path, include_scan_files),
                "status": b.status,
                "total_pages": b.total_pages,
                "detected_pages": b.detected_pages,
                "problem_pages": b.problem_pages,
            }
            for b in batches
        ],
        "submission_pages": [
            {
                "id": p.id,
                "batch_id": p.batch_id,
                "peer_review_page_id": p.peer_review_page_id,
                "page_no_in_upload": p.page_no_in_upload,
                "qr_status": p.qr_status,
                "alignment_status": p.alignment_status,
                "review_status": p.review_status,
                "image_path": _scan_path(p.image_path, include_scan_files) or PURGED_SCAN_PATH,
                "aligned_image_path": _scan_path(p.aligned_image_path, include_scan_files),
                "confidence_summary": p.confidence_summary,
            }
            for p in submission_pages
        ],
        "ratings": [
            {
                "batch_id": r.batch_id,
                "rater_group_id": r.rater_group_id,
                "submission_page_id": r.submission_page_id,
                "peer_review_page_id": r.peer_review_page_id,
                "target_student_id": r.target_student_id,
                "competency_id": r.competency_id,
                "detected_score": r.detected_score,
                "final_score": r.final_score,
                "confidence": r.confidence,
                "status": r.status,
                "reviewed_by": r.reviewed_by,
            }
            for r in ratings
        ],
        "final_results": [
            {
                "target_student_id": f.target_student_id,
                "final_score": f.final_score,
                "combine_groups": f.combine_groups,
                "group_breakdown_json": f.group_breakdown_json,
                "competency_breakdown_json": f.competency_breakdown_json,
                "total_rating_count": f.total_rating_count,
            }
            for f in finals
        ],
        "group_results": [
            {
                "rater_group_id": g.rater_group_id,
                "target_student_id": g.target_student_id,
                "rating_count": g.rating_count,
                "competency_breakdown_json": g.competency_breakdown_json,
                "group_average_score": g.group_average_score,
            }
            for g in groups
        ],
    }


# --------------------------------------------------------------------------- #
# Zip building (export)
# --------------------------------------------------------------------------- #
def _safe_name(value: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in (value or "").strip())
    return safe or "package"


def _package_dir() -> Path:
    path = get_settings().data_root / "package_exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_relative_path(value: str | None) -> Path | None:
    if not value or value == PURGED_SCAN_PATH:
        return None
    windows_path = PureWindowsPath(value)
    path = Path(value.replace("\\", "/"))
    if windows_path.drive or windows_path.root or path.is_absolute() or ".." in path.parts:
        return None
    return path


def _collect_embedded_files(payloads: list[dict]) -> list[dict]:
    data_root = get_settings().data_root.resolve()
    entries: list[dict] = []

    def walk(value, prefix: str) -> None:
        if isinstance(value, dict):
            for key, child in list(value.items()):
                if key in SCAN_PATH_KEYS and isinstance(child, str):
                    rel = _safe_relative_path(child)
                    if rel is None:
                        continue
                    source = (data_root / rel).resolve()
                    try:
                        source.relative_to(data_root)
                    except ValueError:
                        continue
                    package_path = f"files/{prefix}/{len(entries) + 1}_{source.name}"
                    if source.exists() and source.is_file():
                        value[key] = {
                            "original_relative_path": rel.as_posix(),
                            "package_file_path": package_path,
                        }
                        entries.append({"source": source, "package_path": package_path})
                    else:
                        value[key] = PURGED_SCAN_PATH
                else:
                    walk(child, prefix)
        elif isinstance(value, list):
            for child in value:
                walk(child, prefix)

    for payload in payloads:
        walk(payload, "session")
    return entries


def _restore_embedded_path(value) -> str | None:
    """Resolve an embedded scan-path reference back to a safe data-root-relative path.

    Anything unsafe (absolute paths, traversal, non-strings) becomes the purged
    sentinel rather than being written or linked outside the data root.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("restored_relative_path") or value.get("original_relative_path")
    if not isinstance(value, str) or value == PURGED_SCAN_PATH:
        return PURGED_SCAN_PATH if value == PURGED_SCAN_PATH else None
    validated = _safe_relative_path(value)
    return validated.as_posix() if validated is not None else PURGED_SCAN_PATH


def _restore_embedded_files(archive: zipfile.ZipFile, payloads: list[dict]) -> None:
    data_root = get_settings().data_root.resolve()
    import_dir = data_root / "package_imports" / token_urlsafe(12)
    names = set(archive.namelist())

    def walk(value) -> None:
        if isinstance(value, dict):
            if "package_file_path" in value:
                package_path = str(value["package_file_path"])
                if package_path not in names or not package_path.startswith("files/"):
                    value["restored_relative_path"] = PURGED_SCAN_PATH
                    return
                target = (import_dir / Path(package_path).name).resolve()
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    target.relative_to(data_root)
                except ValueError:
                    value["restored_relative_path"] = PURGED_SCAN_PATH
                    return
                target.write_bytes(archive.read(package_path))
                value["restored_relative_path"] = target.relative_to(data_root).as_posix()
                return
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for payload in payloads:
        walk(payload)


def export_template_package(db: Session, template: MockExam) -> Path:
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "package_type": "template",
        "app": APP_NAME,
        "exported_at": datetime.utcnow().isoformat(),
        "name": template.name,
    }
    template_json = serialize_structure(db, template)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("template.json", json.dumps(template_json, ensure_ascii=False, indent=2))
    path = _package_dir() / f"{_safe_name(template.name)}{TEMPLATE_EXT}"
    path.write_bytes(buffer.getvalue())
    return path


def export_session_package(
    db: Session, exam: MockExam, *, include_scan_files: bool = False
) -> Path:
    # Archived sessions have no scan files on disk; never claim to include them.
    if exam.status == "archived":
        include_scan_files = False
    template_json = serialize_structure(db, exam)
    session_json = serialize_session_runtime(db, exam, include_scan_files=include_scan_files)
    # Responses/results live in their own file; keep session.json focused on structure/roster.
    results_json = {
        "responses": session_json.pop("responses", []),
        "student_results": session_json.pop("student_results", []),
    }
    source_template = exam.source_template if getattr(exam, "source_template", None) else None
    template_name = (
        source_template.name if source_template is not None else f"{exam.name} template snapshot"
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "package_type": "session",
        "app": APP_NAME,
        "exported_at": datetime.utcnow().isoformat(),
        "template_name": template_name,
        "session_name": exam.name,
        "include_scan_files": include_scan_files,
        "archived_source": exam.status == "archived",
        "scan_files_omitted_reason": (
            "source session is archived" if exam.status == "archived" else None
        ),
    }
    peer_json = (
        serialize_peer_runtime(db, exam, include_scan_files=include_scan_files)
        if exam.assessment_type == "peer_review"
        else None
    )
    file_entries = (
        _collect_embedded_files(
            [payload for payload in (session_json, results_json, peer_json) if payload is not None]
        )
        if include_scan_files
        else []
    )
    manifest["embedded_file_count"] = len(file_entries)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        manifest["embedded_file_count"] = len(file_entries)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr(
            "template/template.json", json.dumps(template_json, ensure_ascii=False, indent=2)
        )
        archive.writestr(
            "session/session.json", json.dumps(session_json, ensure_ascii=False, indent=2)
        )
        archive.writestr(
            "session/results.json", json.dumps(results_json, ensure_ascii=False, indent=2)
        )
        if peer_json is not None:
            archive.writestr(
                "session/peer_review.json", json.dumps(peer_json, ensure_ascii=False, indent=2)
            )
        for entry in file_entries:
            archive.write(entry["source"], entry["package_path"])
    path = _package_dir() / f"{_safe_name(exam.name)}{SESSION_EXT}"
    path.write_bytes(buffer.getvalue())
    return path


# --------------------------------------------------------------------------- #
# Reading / preview
# --------------------------------------------------------------------------- #
def _read_json(archive: zipfile.ZipFile, name: str) -> dict:
    try:
        return json.loads(archive.read(name).decode("utf-8"))
    except KeyError as exc:
        raise PackageError(f"Package is missing {name}.") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise PackageError(f"Package file {name} is not valid JSON.") from exc


def read_package(path: Path) -> dict:
    """Read manifest + payloads from a package zip into a dict (no DB writes)."""
    if not zipfile.is_zipfile(path):
        raise PackageError("This file is not a valid EZ Mark package.")
    with zipfile.ZipFile(path) as archive:
        manifest = _read_json(archive, "manifest.json")
        if manifest.get("app") != APP_NAME or "package_type" not in manifest:
            raise PackageError("This file is not a valid EZ Mark package.")
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise PackageError(_("packages.incompatible_version"))
        package_type = manifest["package_type"]
        result: dict = {"manifest": manifest, "package_type": package_type}
        if package_type == "template":
            result["template"] = _read_json(archive, "template.json")
        elif package_type == "session":
            result["template"] = _read_json(archive, "template/template.json")
            result["session"] = _read_json(archive, "session/session.json")
            result["results"] = _read_json(archive, "session/results.json")
            if "session/peer_review.json" in archive.namelist():
                result["peer_review"] = _read_json(archive, "session/peer_review.json")
            _restore_embedded_files(
                archive,
                [
                    payload
                    for payload in (
                        result.get("session"),
                        result.get("results"),
                        result.get("peer_review"),
                    )
                    if payload is not None
                ],
            )
        else:
            raise PackageError("Unknown package type.")
    return result


def template_preview(package: dict) -> dict:
    template = package["template"]
    peer = template.get("peer_review_form") or {}
    return {
        "name": template.get("name", ""),
        "assessment_type": template.get("assessment_type", ""),
        "question_count": len(template.get("questions", [])),
        "competency_count": len(template.get("competencies", [])),
        "rater_group_count": len(peer.get("rater_groups", [])),
    }


def session_preview(package: dict) -> dict:
    template = package["template"]
    session = package["session"]
    results = package.get("results", {})
    peer = package.get("peer_review", {})
    manifest = package["manifest"]
    rating_count = len(peer.get("ratings", [])) if peer else 0
    return {
        "template": {
            "name": template.get("name", ""),
            "assessment_type": template.get("assessment_type", ""),
            "question_count": len(template.get("questions", [])),
            "competency_count": len(template.get("competencies", [])),
        },
        "session": {
            "name": session.get("name", ""),
            "date": session.get("exam_date"),
            "assessment_type": session.get("assessment_type", ""),
            "student_count": len(session.get("students", [])),
            "result_count": len(results.get("student_results", [])),
            "rating_count": rating_count,
            "scan_files_included": bool(manifest.get("include_scan_files")),
            "archived_source": bool(manifest.get("archived_source")),
        },
    }


# --------------------------------------------------------------------------- #
# Import (confirm)
# --------------------------------------------------------------------------- #
def _unique_name(db: Session, name: str, *, is_template: bool) -> str:
    """Append ' (imported)' when a same-name record already exists, then number it."""
    base = name.strip() or "Untitled"
    existing = {
        e.name for e in db.scalars(select(MockExam).where(MockExam.is_template.is_(is_template)))
    }
    if base not in existing:
        return base
    candidate = f"{base} (imported)"
    suffix = 2
    while candidate in existing:
        candidate = f"{base} (imported {suffix})"
        suffix += 1
    return candidate


def import_template_package(db: Session, package: dict) -> MockExam:
    """Create a new active template from a template package. Ids are remapped."""
    if package.get("package_type") != "template":
        raise PackageError("This is not a template package.")
    payload = package["template"]
    name = _unique_name(db, payload.get("name", "Imported template"), is_template=True)
    template, _maps = build_exam_from_structure(db, payload, name=name, is_template=True)
    db.commit()
    db.refresh(template)
    return template


def import_session_package(db: Session, package: dict) -> tuple[MockExam, MockExam]:
    """Create an imported template + an imported active session linked to it.

    Returns ``(template, session)``. All ids are remapped; the session's
    ``source_template_id`` points at the freshly imported template.
    """
    if package.get("package_type") != "session":
        raise PackageError("This is not a session package.")
    template_payload = package["template"]
    session_payload = package["session"]
    results_payload = package.get("results", {})
    peer_payload = package.get("peer_review", {})
    manifest = package["manifest"]

    template_name = _unique_name(
        db, template_payload.get("name", "Imported template"), is_template=True
    )
    template, _t_maps = build_exam_from_structure(
        db, template_payload, name=template_name, is_template=True
    )
    db.flush()

    session_name = _unique_name(
        db, session_payload.get("name", "Imported session"), is_template=False
    )
    session, maps = build_exam_from_structure(
        db,
        template_payload,
        name=session_name,
        is_template=False,
        source_template_id=template.id,
        exam_date=session_payload.get("exam_date"),
    )
    db.flush()
    has_scan_data = bool(
        session_payload.get("scan_batches")
        or session_payload.get("scanned_pages")
        or peer_payload.get("batches")
        or peer_payload.get("submission_pages")
    )
    if has_scan_data and not manifest.get("include_scan_files"):
        session.status = "archived"
        session.archived_previous_status = session_payload.get("status")
        session.scan_files_purged_at = datetime.utcnow()
        session.archived_at = datetime.utcnow()
    else:
        session.status = session_payload.get("status") or "draft"

    # Roster: always create fresh Student rows (never reuse ids across installs).
    student_map: dict[int, Student] = {}
    for s in session_payload.get("students", []):
        student = Student(
            student_number=s["student_number"],
            name=s["name"],
            class_name=s["class_name"],
            school_management_id=s.get("school_management_id"),
        )
        db.add(student)
        db.flush()
        student_map[s["id"]] = student
    for link in session_payload.get("exam_students", []):
        student = student_map.get(link["student_id"])
        if student is None:
            continue
        db.add(
            ExamStudent(
                exam_id=session.id,
                student_id=student.id,
                class_name=link.get("class_name"),
                seat_order=link.get("seat_order"),
                included=link.get("included", True),
            )
        )

    if session.assessment_type == "peer_review":
        _import_peer_runtime(
            db,
            session,
            maps,
            student_map,
            peer_payload,
            results_payload,
            warnings=package.setdefault("import_warnings", []),
        )
    else:
        _import_normal_runtime(
            db,
            session,
            maps,
            student_map,
            session_payload,
            results_payload,
            warnings=package.setdefault("import_warnings", []),
        )

    db.commit()
    db.refresh(template)
    db.refresh(session)
    return template, session


def _import_normal_runtime(
    db, session, maps, student_map, session_payload, results_payload, *, warnings
) -> None:
    template_map: dict[int, SheetTemplate] = {}
    for t in session_payload.get("sheet_templates", []):
        new_t = SheetTemplate(
            exam_id=session.id,
            physical_sheet_id=maps.sheets[t["physical_sheet_id"]].id,
            version=t.get("version", 1),
            paper_size=t.get("paper_size", "A4"),
            coordinate_map_json=t.get("coordinate_map_json") or {},
            marker_positions_json=t.get("marker_positions_json") or {},
        )
        db.add(new_t)
        db.flush()
        template_map[t["id"]] = new_t

    instance_map: dict[int, SheetInstance] = {}
    for i in session_payload.get("sheet_instances", []):
        student = student_map.get(i["student_id"])
        sheet_template = template_map.get(i["sheet_template_id"])
        if student is None or sheet_template is None:
            continue
        new_i = SheetInstance(
            exam_id=session.id,
            student_id=student.id,
            physical_sheet_id=maps.sheets[i["physical_sheet_id"]].id,
            sheet_template_id=sheet_template.id,
            qr_token=_preserved_or_new_qr_token(
                db, i.get("qr_token"), warnings=warnings, label=f"sheet instance {i.get('id')}"
            ),
            printed_name=i["printed_name"],
            printed_student_number=i["printed_student_number"],
            pdf_file_path=_restore_embedded_path(i.get("pdf_file_path")),
            page_no=i.get("page_no"),
            status=i.get("status", "generated"),
        )
        db.add(new_i)
        db.flush()
        instance_map[i["id"]] = new_i

    blank_token_map: dict[tuple[int, int], str] = {}
    for b in session_payload.get("blank_sheet_labels", []):
        sheet_template = template_map.get(b["sheet_template_id"])
        if sheet_template is None:
            continue
        new_token = _preserved_or_new_qr_token(
            db,
            b.get("qr_token"),
            warnings=warnings,
            label=f"blank sheet label {b.get('physical_sheet_id')}",
        )
        blank_token_map[(b["physical_sheet_id"], b["sheet_template_id"])] = new_token
        db.add(
            BlankSheetLabel(
                exam_id=session.id,
                physical_sheet_id=maps.sheets[b["physical_sheet_id"]].id,
                sheet_template_id=sheet_template.id,
                qr_token=new_token,
                pdf_file_path=_restore_embedded_path(b.get("pdf_file_path")),
            )
        )

    batch_map: dict[int, ScanBatch] = {}
    for b in session_payload.get("scan_batches", []):
        new_b = ScanBatch(
            exam_id=session.id,
            uploaded_file_name=b["uploaded_file_name"],
            uploaded_file_path=_restore_embedded_path(b.get("uploaded_file_path"))
            or PURGED_SCAN_PATH,
            status=b.get("status", "uploaded"),
            total_pages=b.get("total_pages", 0),
            detected_pages=b.get("detected_pages", 0),
            problem_pages=b.get("problem_pages", 0),
            error_message=b.get("error_message"),
        )
        db.add(new_b)
        db.flush()
        batch_map[b["id"]] = new_b

    page_map: dict[int, ScannedPage] = {}
    pages = session_payload.get("scanned_pages", [])
    for p in pages:
        batch = batch_map.get(p["scan_batch_id"])
        if batch is None:
            continue
        instance = instance_map.get(p["sheet_instance_id"]) if p.get("sheet_instance_id") else None
        blank_token = None
        if instance is None and p.get("physical_sheet_id"):
            for label in session_payload.get("blank_sheet_labels", []):
                if label.get("physical_sheet_id") == p.get("physical_sheet_id") and label.get(
                    "qr_token"
                ) == p.get("qr_token"):
                    blank_token = blank_token_map.get(
                        (label["physical_sheet_id"], label["sheet_template_id"])
                    )
                    break
        student = student_map.get(p["student_id"]) if p.get("student_id") else None
        new_p = ScannedPage(
            scan_batch_id=batch.id,
            sheet_instance_id=instance.id if instance else None,
            student_id=student.id if student else None,
            physical_sheet_id=(
                maps.sheets[p["physical_sheet_id"]].id if p.get("physical_sheet_id") else None
            ),
            page_no=p["page_no"],
            image_path=_restore_embedded_path(p.get("image_path")) or PURGED_SCAN_PATH,
            aligned_image_path=_restore_embedded_path(p.get("aligned_image_path")),
            qr_token=(
                instance.qr_token
                if instance is not None and p.get("qr_token")
                else blank_token or p.get("qr_token")
            ),
            qr_status=p.get("qr_status", "not_found"),
            alignment_status=p.get("alignment_status", "failed"),
            review_status=p.get("review_status", "needs_review"),
            confidence_summary=p.get("confidence_summary"),
            source_exam_id=p.get("source_exam_id"),
            mapping_note=p.get("mapping_note"),
        )
        db.add(new_p)
        db.flush()
        page_map[p["id"]] = new_p
    # Self-referential page links resolved once all pages exist.
    for p in pages:
        new_p = page_map.get(p["id"])
        if new_p is None:
            continue
        if p.get("duplicate_of_page_id") and p["duplicate_of_page_id"] in page_map:
            new_p.duplicate_of_page_id = page_map[p["duplicate_of_page_id"]].id
        if p.get("replaced_by_page_id") and p["replaced_by_page_id"] in page_map:
            new_p.replaced_by_page_id = page_map[p["replaced_by_page_id"]].id

    for r in results_payload.get("responses", []):
        page = page_map.get(r["scanned_page_id"])
        question = maps.questions.get(r["question_id"])
        if page is None or question is None:
            continue
        db.add(
            Response(
                scanned_page_id=page.id,
                question_id=question.id,
                detected_answer=r.get("detected_answer"),
                final_answer=r.get("final_answer"),
                is_correct=r.get("is_correct"),
                confidence=r.get("confidence"),
                fill_values_json=r.get("fill_values_json"),
                status=r.get("status", "ok"),
                reviewed_by=r.get("reviewed_by"),
            )
        )

    for res in results_payload.get("student_results", []):
        student = student_map.get(res["student_id"])
        if student is None:
            continue
        db.add(
            StudentResult(
                exam_id=session.id,
                student_id=student.id,
                raw_json=res.get("raw_json") or {},
                score_details_json=res.get("score_details_json") or {},
                total_score=res.get("total_score"),
                pass_fail_status=res.get("pass_fail_status", "not_scored"),
            )
        )


def _import_peer_runtime(
    db, session, maps, student_map, peer_payload, results_payload, *, warnings
) -> None:
    form = maps.form
    if form is None or not peer_payload:
        return
    target_map: dict[int, PeerReviewTarget] = {}
    for t in peer_payload.get("targets", []):
        student = student_map.get(t["student_id"])
        if student is None:
            continue
        new_t = PeerReviewTarget(
            form_id=form.id,
            student_id=student.id,
            display_label=t["display_label"],
            display_order=t["display_order"],
            active=t.get("active", True),
        )
        db.add(new_t)
        db.flush()
        target_map[t["id"]] = new_t

    page_map: dict[int, PeerReviewPage] = {}
    for p in peer_payload.get("pages", []):
        rows = []
        for row in p.get("target_rows_json") or []:
            new_row = dict(row)
            old_target = row.get("target_id")
            if old_target in target_map:
                new_row["target_id"] = target_map[old_target].id
            old_student = row.get("student_id")
            if old_student in student_map:
                new_row["student_id"] = student_map[old_student].id
            rows.append(new_row)
        new_p = PeerReviewPage(
            form_id=form.id,
            page_no=p["page_no"],
            title=p["title"],
            qr_token=_preserved_or_new_qr_token(
                db,
                p.get("qr_token"),
                warnings=warnings,
                label=f"peer review page {p.get('id')}",
                generator=lambda: f"prp_{token_urlsafe(24)}",
            ),
            coordinate_map_json=p.get("coordinate_map_json") or {},
            target_rows_json=rows,
        )
        db.add(new_p)
        db.flush()
        page_map[p["id"]] = new_p

    batch_map: dict[int, PeerReviewBatch] = {}
    for b in peer_payload.get("batches", []):
        rater_group = maps.rater_groups.get(b["rater_group_id"])
        if rater_group is None:
            continue
        new_b = PeerReviewBatch(
            form_id=form.id,
            rater_group_id=rater_group.id,
            uploaded_file_name=b["uploaded_file_name"],
            uploaded_file_path=_restore_embedded_path(b.get("uploaded_file_path"))
            or PURGED_SCAN_PATH,
            status=b.get("status", "uploaded"),
            total_pages=b.get("total_pages", 0),
            detected_pages=b.get("detected_pages", 0),
            problem_pages=b.get("problem_pages", 0),
        )
        db.add(new_b)
        db.flush()
        batch_map[b["id"]] = new_b

    submission_map: dict[int, PeerReviewSubmissionPage] = {}
    for p in peer_payload.get("submission_pages", []):
        batch = batch_map.get(p["batch_id"])
        page = page_map.get(p["peer_review_page_id"])
        if batch is None or page is None:
            continue
        new_p = PeerReviewSubmissionPage(
            batch_id=batch.id,
            peer_review_page_id=page.id,
            page_no_in_upload=p["page_no_in_upload"],
            qr_status=p.get("qr_status", "ok"),
            alignment_status=p.get("alignment_status", "ok"),
            review_status=p.get("review_status", "ok"),
            image_path=_restore_embedded_path(p.get("image_path")) or PURGED_SCAN_PATH,
            aligned_image_path=_restore_embedded_path(p.get("aligned_image_path")),
            confidence_summary=p.get("confidence_summary"),
        )
        db.add(new_p)
        db.flush()
        submission_map[p["id"]] = new_p

    for r in peer_payload.get("ratings", []):
        batch = batch_map.get(r["batch_id"])
        rater_group = maps.rater_groups.get(r["rater_group_id"])
        submission = submission_map.get(r["submission_page_id"])
        page = page_map.get(r["peer_review_page_id"])
        student = student_map.get(r["target_student_id"])
        competency = maps.competencies.get(r["competency_id"])
        if None in (batch, rater_group, submission, page, student, competency):
            continue
        db.add(
            PeerReviewRating(
                exam_id=session.id,
                form_id=form.id,
                batch_id=batch.id,
                rater_group_id=rater_group.id,
                submission_page_id=submission.id,
                peer_review_page_id=page.id,
                target_student_id=student.id,
                competency_id=competency.id,
                detected_score=r.get("detected_score"),
                final_score=r.get("final_score"),
                confidence=r.get("confidence"),
                status=r.get("status", "ok"),
                reviewed_by=r.get("reviewed_by"),
            )
        )

    for f in peer_payload.get("final_results", []):
        student = student_map.get(f["target_student_id"])
        if student is None:
            continue
        db.add(
            PeerReviewFinalResult(
                exam_id=session.id,
                form_id=form.id,
                target_student_id=student.id,
                final_score=f.get("final_score"),
                combine_groups=f.get("combine_groups", False),
                group_breakdown_json=f.get("group_breakdown_json") or {},
                competency_breakdown_json=f.get("competency_breakdown_json") or {},
                total_rating_count=f.get("total_rating_count", 0),
            )
        )

    for g in peer_payload.get("group_results", []):
        student = student_map.get(g["target_student_id"])
        rater_group = maps.rater_groups.get(g["rater_group_id"])
        if student is None or rater_group is None:
            continue
        db.add(
            PeerReviewGroupResult(
                exam_id=session.id,
                form_id=form.id,
                rater_group_id=rater_group.id,
                target_student_id=student.id,
                rating_count=g.get("rating_count", 0),
                competency_breakdown_json=g.get("competency_breakdown_json") or {},
                group_average_score=g.get("group_average_score"),
            )
        )
