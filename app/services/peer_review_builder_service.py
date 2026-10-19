from dataclasses import dataclass
from secrets import token_urlsafe

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Competency,
    ExamStudent,
    MockExam,
    PeerReviewBatch,
    PeerReviewForm,
    PeerReviewGroupResult,
    PeerReviewPage,
    PeerReviewRaterGroup,
    PeerReviewRating,
    PeerReviewSubmissionPage,
    PeerReviewTarget,
    Student,
)
from app.services.peer_review_layout_service import (
    DEFAULT_DENSITY_MODE,
    VALID_DENSITY_MODES,
    VALID_ORIENTATION_MODES,
    calculate_peer_review_layout,
    page_plan_for_form,
)
from app.services.sheet_generation_service import latest_exam_download_path

DEFAULT_TARGETS_PER_PAGE = 15
LAYOUT_CHANGE_BLOCKED_MESSAGE = (
    "This peer-review form already has generated or scanned sheets. Changing the layout "
    "could invalidate printed copies. Create a new form/version instead."
)
COMPETENCY_DELETE_BLOCKED_MESSAGE = (
    "This competency already has scanned ratings and cannot be deleted. Create a new "
    "form/version if the printed layout must change."
)
RATER_GROUP_DELETE_BLOCKED_MESSAGE = (
    "This rater group already has scanned ratings and cannot be removed. Create a new "
    "peer-review form/version if you need a different group setup."
)


DEFAULT_RATER_GROUP_NAME = "Students"
DEFAULT_COMPETENCY_NAME = "Overall"


def get_or_create_form(db: Session, exam: MockExam) -> PeerReviewForm:
    form = db.scalar(select(PeerReviewForm).where(PeerReviewForm.exam_id == exam.id))
    if form:
        return form
    form = PeerReviewForm(
        exam_id=exam.id,
        title=exam.name,
        anonymous=True,
        students_per_page=DEFAULT_TARGETS_PER_PAGE,
    )
    db.add(form)
    db.flush()
    db.add(
        PeerReviewRaterGroup(
            form_id=form.id, name=DEFAULT_RATER_GROUP_NAME, weight_percent=100, display_order=1
        )
    )
    # Templates have no roster, so this is a no-op for them; sessions pick up students.
    _sync_targets_from_roster(db, form)
    db.commit()
    return form


def seed_peer_review_template_defaults(db: Session, template: MockExam) -> PeerReviewForm:
    """Seed a freshly created peer-review template with reusable defaults.

    Creates the reusable form (score scale + students-per-page), a default rater
    group, and one editable competency so the template is immediately usable.
    """
    form = get_or_create_form(db, template)
    if not _competencies(db, form):
        db.add(
            Competency(
                exam_id=template.id,
                name=DEFAULT_COMPETENCY_NAME,
                min_score=form.score_min,
                max_score=form.score_max,
                display_order=1,
            )
        )
        db.commit()
    return form


def copy_peer_review_setup(
    db: Session, source_exam: MockExam, target_exam: MockExam
) -> PeerReviewForm | None:
    """Copy the reusable peer-review structure between two exams/templates.

    Copies the score scale, students-per-page, and rater groups only. Targets, pages,
    QR tokens, coordinate maps, scan batches, ratings, confirmations, and results are
    never copied — those are session-specific. Competencies live on the MockExam and
    are copied by the caller (test_template_service) alongside other setup.
    """
    source_form = db.scalar(select(PeerReviewForm).where(PeerReviewForm.exam_id == source_exam.id))
    if source_form is None:
        return None
    new_form = PeerReviewForm(
        exam_id=target_exam.id,
        title=target_exam.name,
        anonymous=source_form.anonymous,
        score_min=source_form.score_min,
        score_max=source_form.score_max,
        students_per_page=source_form.students_per_page or DEFAULT_TARGETS_PER_PAGE,
        page_orientation_mode=source_form.page_orientation_mode or "auto",
        layout_density=source_form.layout_density or DEFAULT_DENSITY_MODE,
        combine_groups=source_form.combine_groups,
        missing_group_policy=source_form.missing_group_policy,
    )
    db.add(new_form)
    db.flush()
    for group in source_form.rater_groups:
        db.add(
            PeerReviewRaterGroup(
                form_id=new_form.id,
                name=group.name,
                description=group.description,
                weight_percent=group.weight_percent,
                display_order=group.display_order,
                active=group.active,
            )
        )
    db.commit()
    return new_form


def peer_review_template_issues(db: Session, exam: MockExam) -> list[str]:
    """Readiness checks for a reusable peer-review template.

    Mirrors ``validate_answer_key`` for normal templates: an empty list means the
    template is ready to spawn sessions.
    """
    form = get_or_create_form(db, exam)
    issues: list[str] = []
    if not _competencies(db, form):
        issues.append("Add at least one competency (評価項目).")
    if form.score_min >= form.score_max:
        issues.append("Score maximum must be greater than the minimum.")
    if (form.students_per_page or 0) < 1:
        issues.append("Students per page must be at least 1.")
    return issues


def builder_state(db: Session, exam: MockExam) -> dict:
    form = get_or_create_form(db, exam)
    # Read-safe: viewing the builder must never hard-fail. When the printed layout is locked
    # and the roster has grown, new targets cannot be auto-added (it would invalidate printed
    # copies) — they are surfaced as a non-fatal notice instead (see
    # ``peer_review_locked_roster_pending``), and the teacher regenerates to pick them up.
    _sync_targets_from_roster(db, form, raise_on_locked=False)
    if not form.pages and not peer_review_form_layout_is_locked(db, form):
        auto_paginate(db, form)
    db.refresh(form)
    competencies = list(
        db.scalars(
            select(Competency)
            .where(Competency.exam_id == exam.id)
            .order_by(Competency.display_order)
        )
    )
    plan = page_plan_for_form(form, competencies)
    return {
        "form": {
            "id": form.id,
            "title": form.title,
            "score_min": form.score_min,
            "score_max": form.score_max,
            "students_per_page": form.students_per_page or DEFAULT_TARGETS_PER_PAGE,
            "page_orientation_mode": form.page_orientation_mode or "auto",
            "layout_density": form.layout_density or DEFAULT_DENSITY_MODE,
            "combine_groups": form.combine_groups,
            "missing_group_policy": form.missing_group_policy,
        },
        "page_plan": plan.as_map(),
        "is_template": exam.is_template,
        "rater_groups": [
            {
                "id": group.id,
                "name": group.name,
                "description": group.description or "",
                "weight_percent": group.weight_percent,
                "display_order": group.display_order,
                "active": group.active,
            }
            for group in form.rater_groups
        ],
        "competencies": [
            {
                "id": comp.id,
                "name": comp.name,
                "name_furigana": comp.name_furigana or "",
                "short_label": comp.short_label or "",
                "name_ruby": comp.name_ruby_json or [],
                "description": comp.description or "",
                "display_order": comp.display_order,
                "min_score": comp.min_score,
                "max_score": comp.max_score,
            }
            for comp in competencies
        ],
        "active_target_count": len([t for t in form.targets if t.active]),
        "targets": [
            {
                "id": target.id,
                "student_id": target.student_id,
                "display_label": target.display_label,
                "display_order": target.display_order,
                "active": target.active,
            }
            for target in form.targets
        ],
        "pages": [
            {
                "id": page.id,
                "page_no": page.page_no,
                "target_ids": [row["target_id"] for row in page.target_rows_json],
            }
            for page in form.pages
        ],
    }


def update_form_settings(db: Session, form: PeerReviewForm, data: dict) -> PeerReviewForm:
    if _layout_fields_changed(form, data):
        _ensure_layout_editable(db, form)
    if (
        "page_orientation_mode" in data
        and data["page_orientation_mode"] not in VALID_ORIENTATION_MODES
    ):
        raise ValueError("Choose a valid page orientation (auto, portrait, or landscape).")
    if "layout_density" in data and data["layout_density"] not in VALID_DENSITY_MODES:
        raise ValueError("Choose a valid layout density (standard, compact, or relaxed).")
    for field in (
        "title",
        "score_min",
        "score_max",
        "students_per_page",
        "page_orientation_mode",
        "layout_density",
        "combine_groups",
        "missing_group_policy",
    ):
        if field in data:
            setattr(form, field, data[field])
    if form.score_min >= form.score_max:
        raise ValueError("Score maximum must be greater than the minimum.")
    if (form.students_per_page or 0) < 1:
        raise ValueError("Students per page must be at least 1.")
    # Competencies inherit the form's score scale; keep them in sync.
    for competency in _competencies(db, form):
        competency.min_score = form.score_min
        competency.max_score = form.score_max
    db.commit()
    db.refresh(form)
    return form


def add_rater_group(
    db: Session,
    form: PeerReviewForm,
    name: str,
    weight_percent: float = 0,
    description: str | None = None,
) -> PeerReviewRaterGroup:
    group = PeerReviewRaterGroup(
        form_id=form.id,
        name=name.strip(),
        description=description,
        weight_percent=weight_percent,
        display_order=_next_order(form.rater_groups),
    )
    db.add(group)
    db.commit()
    return group


def update_rater_group(db: Session, form: PeerReviewForm, group_id: int, data: dict) -> None:
    group = db.get(PeerReviewRaterGroup, group_id)
    if group is None or group.form_id != form.id:
        raise ValueError("Rater group not found.")
    if data.get("active") is False and peer_review_rater_group_has_data(db, form, group_id):
        raise ValueError(RATER_GROUP_DELETE_BLOCKED_MESSAGE)
    if any(field in data for field in ("weight_percent", "display_order", "active")):
        _ensure_layout_editable(db, form)
    for field in ("name", "description", "weight_percent", "display_order", "active"):
        if field in data:
            setattr(group, field, data[field])
    db.commit()


def delete_rater_group(db: Session, form: PeerReviewForm, group_id: int) -> None:
    group = db.get(PeerReviewRaterGroup, group_id)
    if group is None or group.form_id != form.id:
        raise ValueError("Rater group not found.")
    if peer_review_rater_group_has_data(db, form, group_id):
        raise ValueError(RATER_GROUP_DELETE_BLOCKED_MESSAGE)
    group.active = False
    db.commit()


def _normalize_ruby(raw) -> list[dict] | None:
    """Clean a raw ruby list into ``[{base_text, reading}, ...]`` or ``None`` when empty.

    Drops entries missing either field and trims whitespace, so the stored value is always
    well-formed for the layout service to position.
    """
    if not isinstance(raw, list):
        return None
    cleaned: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        base = (entry.get("base_text") or "").strip()
        reading = (entry.get("reading") or "").strip()
        if base and reading:
            cleaned.append({"base_text": base, "reading": reading})
    return cleaned or None


def add_competency(
    db: Session,
    form: PeerReviewForm,
    name: str,
    description: str | None = None,
    name_furigana: str | None = None,
    short_label: str | None = None,
    name_ruby: list | None = None,
) -> Competency:
    _ensure_layout_editable(db, form)
    competency = Competency(
        exam_id=form.exam_id,
        name=name.strip(),
        name_furigana=(name_furigana or "").strip() or None,
        short_label=(short_label or "").strip() or None,
        name_ruby_json=_normalize_ruby(name_ruby),
        description=description,
        min_score=form.score_min,
        max_score=form.score_max,
        display_order=_next_order(_competencies(db, form)),
    )
    db.add(competency)
    db.commit()
    return competency


def update_competency(db: Session, form: PeerReviewForm, competency_id: int, data: dict) -> None:
    competency = db.get(Competency, competency_id)
    if competency is None or competency.exam_id != form.exam_id:
        raise ValueError("Competency not found.")
    if "display_order" in data:
        _ensure_layout_editable(db, form)
    # Furigana, per-segment ruby, and the short printed label are display-only text and
    # never move bubbles, so they stay editable even when the printed layout is locked.
    if "name_ruby" in data:
        competency.name_ruby_json = _normalize_ruby(data["name_ruby"])
    for field in ("name", "name_furigana", "short_label", "description", "display_order"):
        if field in data:
            value = data[field]
            if field in ("name_furigana", "short_label"):
                value = (value or "").strip() or None
            setattr(competency, field, value)
    competency.min_score = form.score_min
    competency.max_score = form.score_max
    db.commit()


def move_competency(db: Session, form: PeerReviewForm, competency_id: int, direction: str) -> None:
    """Reorder one competency up/down by one slot and renumber ``display_order`` 1..n.

    Reordering moves the printed columns, so it is layout-guarded (blocked once sheets are
    generated/scanned). Renumbering contiguously keeps the order stable regardless of any
    pre-existing gaps in ``display_order``.
    """
    if direction not in ("up", "down"):
        raise ValueError("Direction must be 'up' or 'down'.")
    _ensure_layout_editable(db, form)
    competencies = _competencies(db, form)  # ordered by display_order
    index = next((i for i, c in enumerate(competencies) if c.id == competency_id), None)
    if index is None:
        raise ValueError("Competency not found.")
    target = index + (-1 if direction == "up" else 1)
    if target < 0 or target >= len(competencies):
        return  # already at the edge; nothing to do
    competencies.insert(target, competencies.pop(index))
    for order, competency in enumerate(competencies, start=1):
        competency.display_order = order
    db.commit()


def delete_competency(db: Session, form: PeerReviewForm, competency_id: int) -> None:
    competency = db.get(Competency, competency_id)
    if competency is None or competency.exam_id != form.exam_id:
        raise ValueError("Competency not found.")
    if _competency_has_ratings(db, form, competency_id):
        raise ValueError(COMPETENCY_DELETE_BLOCKED_MESSAGE)
    _ensure_layout_editable(db, form)
    db.delete(competency)
    db.commit()


def update_target(db: Session, form: PeerReviewForm, target_id: int, data: dict) -> None:
    target = db.get(PeerReviewTarget, target_id)
    if target is None or target.form_id != form.id:
        raise ValueError("Target not found.")
    if any(field in data for field in ("display_order", "active")):
        _ensure_layout_editable(db, form)
    for field in ("display_label", "display_order", "active"):
        if field in data:
            setattr(target, field, data[field])
    db.commit()


def add_page_break_before_target(
    db: Session, form: PeerReviewForm, target_id: int
) -> list[PeerReviewPage]:
    _ensure_layout_editable(db, form)
    targets = [target for target in form.targets if target.active]
    index = next((idx for idx, target in enumerate(targets) if target.id == target_id), None)
    if index is None or index == 0:
        return list(form.pages)
    return _paginate_chunks(db, form, [targets[:index], targets[index:]])


def remove_page_breaks(db: Session, form: PeerReviewForm) -> list[PeerReviewPage]:
    _ensure_layout_editable(db, form)
    return auto_paginate(db, form)


def auto_paginate(
    db: Session, form: PeerReviewForm, targets_per_page: int | None = None
) -> list[PeerReviewPage]:
    _ensure_layout_editable(db, form)
    if targets_per_page is not None:
        per_page = max(1, targets_per_page)
    else:
        # Use the rows that actually fit the resolved orientation, not the raw maximum,
        # so we never overcrowd a page.
        per_page = page_plan_for_form(form, _competencies(db, form)).students_per_page
    targets = [target for target in form.targets if target.active]
    chunks = [targets[index : index + per_page] for index in range(0, len(targets), per_page)]
    return _paginate_chunks(db, form, chunks)


def _paginate_chunks(
    db: Session, form: PeerReviewForm, chunks: list[list[PeerReviewTarget]]
) -> list[PeerReviewPage]:
    existing_by_page_no = {page.page_no: page for page in list(form.pages)}
    pages = []
    for chunk in [chunk for chunk in chunks if chunk]:
        page_no = len(pages) + 1
        target_rows = [
            {
                "target_id": target.id,
                "student_id": target.student_id,
                "display_label": target.display_label,
                "display_order": target.display_order,
            }
            for target in chunk
        ]
        page = existing_by_page_no.pop(page_no, None)
        if page is None:
            page = PeerReviewPage(
                form_id=form.id,
                page_no=page_no,
                title=f"{form.title} Page {page_no}",
                qr_token=f"prp_{token_urlsafe(24)}",
            )
            db.add(page)
        page.title = f"{form.title} Page {page_no}"
        page.target_rows_json = target_rows
        page.coordinate_map_json = {}
        pages.append(page)
    for stale_page in existing_by_page_no.values():
        db.delete(stale_page)
    db.flush() if db.info.get("defer_roster_commit") else db.commit()
    return pages


def sync_targets_from_roster(
    db: Session, form: PeerReviewForm, *, raise_on_locked: bool = True
) -> bool:
    """Generate PeerReviewTargets from the session's real student roster.

    Each included student becomes one target labelled ``"<order>. <name>"``
    (e.g. ``1. 山田 太郎``) — the roster order number and student name, never the student
    code. The order is ``ExamStudent.seat_order`` when set, otherwise the displayed
    roster position. Returns ``True`` when new targets were added. Templates have no
    roster, so this is a harmless no-op for them.

    Existing targets and already-paginated page rows are also relabelled (display-only),
    so sheets never keep stale student-code labels. Relabelling never moves bubbles or
    changes coordinate maps, so it is safe even when the printed layout is locked.

    When the printed layout is locked and the roster has new students, adding targets
    would invalidate printed sheets. By default we fail loud; read-only callers (the
    preview/layout payload) pass ``raise_on_locked=False`` to keep rendering instead.
    """
    links = list(
        db.scalars(
            select(ExamStudent)
            .where(ExamStudent.exam_id == form.exam_id)
            .where(ExamStudent.included.is_(True))
        )
    )
    # Order by seat_order when present, otherwise roster position (id order).
    links.sort(key=lambda link: (link.seat_order is None, link.seat_order or 0, link.id))
    order_map = {
        link.student_id: (link.seat_order if link.seat_order is not None else position)
        for position, link in enumerate(links, start=1)
    }

    existing_student_ids = {target.student_id for target in form.targets}
    missing_links = [link for link in links if link.student_id not in existing_student_ids]
    locked = bool(form.pages) and peer_review_form_layout_is_locked(db, form)
    added = False
    if missing_links:
        if locked:
            if raise_on_locked:
                raise ValueError(LAYOUT_CHANGE_BLOCKED_MESSAGE)
            # Cannot add new targets to a locked layout, but still relabel existing ones.
        else:
            next_order = max((target.display_order for target in form.targets), default=0) + 1
            for link in missing_links:
                student = db.get(Student, link.student_id)
                db.add(
                    PeerReviewTarget(
                        form_id=form.id,
                        student_id=link.student_id,
                        display_label=_target_label(student, order_map.get(link.student_id)),
                        display_order=next_order,
                    )
                )
                next_order += 1
                added = True
            db.flush()

    changed = _relabel_targets(db, form, order_map)
    pages_changed = _refresh_page_target_labels(db, form)
    if added or changed or pages_changed:
        db.flush() if db.info.get("defer_roster_commit") else db.commit()
    return added


def _relabel_targets(db: Session, form: PeerReviewForm, order_map: dict[int, int]) -> bool:
    """Refresh every target's display label to ``"<order>. <name>"`` (display-only)."""
    changed = False
    for target in db.scalars(select(PeerReviewTarget).where(PeerReviewTarget.form_id == form.id)):
        student = target.student or db.get(Student, target.student_id)
        order = order_map.get(target.student_id, target.display_order)
        label = _target_label(student, order)
        if target.display_label != label:
            target.display_label = label
            changed = True
    return changed


def _refresh_page_target_labels(db: Session, form: PeerReviewForm) -> bool:
    """Refresh display labels stored in already-paginated page rows (display-only).

    Only ``display_label`` text changes — target ids, student ids, and bubble
    coordinate maps are untouched, so existing scan coordinate maps stay valid.
    """
    labels = {
        target.id: target.display_label
        for target in db.scalars(
            select(PeerReviewTarget).where(PeerReviewTarget.form_id == form.id)
        )
    }
    changed = False
    for page in form.pages:
        rows = page.target_rows_json or []
        new_rows = []
        page_changed = False
        for row in rows:
            new_row = dict(row)
            target_id = row.get("target_id")
            if target_id in labels and row.get("display_label") != labels[target_id]:
                new_row["display_label"] = labels[target_id]
                page_changed = True
            new_rows.append(new_row)
        if page_changed:
            # Reassign so SQLAlchemy tracks the JSON change.
            page.target_rows_json = new_rows
            changed = True
    return changed


def sync_peer_review_targets(db: Session, exam: MockExam) -> bool:
    """Roster -> target sync entry point keyed by exam (used from student routes)."""
    if exam.is_template:
        return False
    form = db.scalar(select(PeerReviewForm).where(PeerReviewForm.exam_id == exam.id))
    if form is None:
        return False
    added = sync_targets_from_roster(db, form, raise_on_locked=False)
    if added and not peer_review_form_layout_is_locked(db, form):
        # A deferred commit does not expire this relationship automatically.
        # Include newly flushed targets before building the page rows.
        db.expire(form, ["targets"])
        auto_paginate(db, form)
    return added


def _target_label(student: Student | None, order: int | None) -> str:
    """Printed peer-review target label: ``"<order>. <name>"`` (never the student code)."""
    name = (student.name if student is not None else "").strip()
    if order is None:
        return name
    if not name:
        return f"{order}."
    return f"{order}. {name}"


# Backwards-compatible private alias used elsewhere in this module.
_sync_targets_from_roster = sync_targets_from_roster


def _competencies(db: Session, form: PeerReviewForm) -> list[Competency]:
    return list(
        db.scalars(
            select(Competency)
            .where(Competency.exam_id == form.exam_id)
            .order_by(Competency.display_order)
        )
    )


def _next_order(items) -> int:
    return max((item.display_order or 0 for item in items), default=0) + 1


def peer_review_form_has_scans_or_ratings(db: Session, form_id: int) -> bool:
    rating_count = (
        db.scalar(
            select(func.count(PeerReviewRating.id)).where(PeerReviewRating.form_id == form_id)
        )
        or 0
    )
    if rating_count:
        return True
    batch_count = (
        db.scalar(select(func.count(PeerReviewBatch.id)).where(PeerReviewBatch.form_id == form_id))
        or 0
    )
    if batch_count:
        return True
    submission_count = (
        db.scalar(
            select(func.count(PeerReviewSubmissionPage.id))
            .join(PeerReviewPage, PeerReviewSubmissionPage.peer_review_page_id == PeerReviewPage.id)
            .where(PeerReviewPage.form_id == form_id)
        )
        or 0
    )
    return submission_count > 0


def _ensure_layout_editable(db: Session, form: PeerReviewForm) -> None:
    if peer_review_form_layout_is_locked(db, form):
        raise ValueError(LAYOUT_CHANGE_BLOCKED_MESSAGE)


def _layout_fields_changed(form: PeerReviewForm, data: dict) -> bool:
    return any(
        field in data and getattr(form, field) != data[field]
        for field in ("score_min", "score_max", "page_orientation_mode", "layout_density")
    )


def _competency_has_ratings(db: Session, form: PeerReviewForm, competency_id: int) -> bool:
    return bool(
        db.scalar(
            select(func.count(PeerReviewRating.id))
            .where(PeerReviewRating.form_id == form.id)
            .where(PeerReviewRating.competency_id == competency_id)
        )
    )


def peer_review_locked_roster_pending(db: Session, form: PeerReviewForm) -> bool:
    """True when the printed layout is locked but the roster has students not yet on a sheet.

    Adding those students would move the printed layout, so the read-safe roster sync leaves
    them off; the builder shows a non-fatal notice and the teacher regenerates to include them.
    """
    if not (bool(form.pages) and peer_review_form_layout_is_locked(db, form)):
        return False
    existing = {target.student_id for target in form.targets}
    links = db.scalars(
        select(ExamStudent)
        .where(ExamStudent.exam_id == form.exam_id)
        .where(ExamStudent.included.is_(True))
    )
    return any(link.student_id not in existing for link in links)


def peer_review_form_layout_is_locked(db: Session, form: PeerReviewForm) -> bool:
    """True when changing layout could invalidate generated, copied, or scanned sheets."""
    if peer_review_form_has_scans_or_ratings(db, form.id):
        return True
    exam = form.exam or db.get(MockExam, form.exam_id)
    if exam is None:
        return False
    if exam.status == "printed":
        return True
    has_generated_layout = any(page.coordinate_map_json for page in form.pages)
    return has_generated_layout and latest_exam_download_path(db, exam) is not None


@dataclass
class _PreviewPage:
    """Lightweight stand-in for PeerReviewPage so the layout service can compute a
    preview without persisting placeholder targets/pages on a template."""

    page_no: int
    qr_token: str
    target_rows_json: list


def _layout_to_dict(layout) -> dict:
    return {
        "page_width_mm": layout.page_width_mm,
        "page_height_mm": layout.page_height_mm,
        "orientation": layout.orientation,
        "qr": layout.qr,
        "score_values": layout.score_values,
        "target_rows": [row.as_map() for row in layout.target_rows],
        "competency_columns": [column.as_map() for column in layout.competency_columns],
        "rating_bubbles": [bubble.as_map() for bubble in layout.rating_bubbles],
        "marker_positions": layout.marker_positions,
        "table_left_mm": layout.table_left_mm,
        "table_right_mm": layout.table_right_mm,
        "header_top_mm": layout.header_top_mm,
        "table_top_mm": layout.table_top_mm,
        "table_bottom_mm": layout.table_bottom_mm,
        "name_y_mm": layout.name_y_mm,
        "furigana_y_mm": layout.furigana_y_mm,
        "content_left_mm": layout.content_left_mm,
        "column_separators_mm": layout.column_separators_mm,
        "target_column": layout.target_column,
    }


def peer_review_layout_payload(db: Session, exam: MockExam) -> dict:
    """Calculated paper layout for the builder preview.

    For templates (no roster) this renders ``students_per_page`` placeholder rows so
    the preview matches the generated PDF. For sessions it renders the real pages.
    Uses the same layout service as PDF generation/scanning — the single source of
    truth — so the preview cannot drift from the printed sheet.
    """
    form = get_or_create_form(db, exam)
    # Sessions render the real roster; sync (read-safe) and paginate so live students
    # show immediately instead of "Student 1" placeholders.
    if not exam.is_template:
        sync_targets_from_roster(db, form, raise_on_locked=False)
        if (
            [t for t in form.targets if t.active]
            and not form.pages
            and not peer_review_form_layout_is_locked(db, form)
        ):
            auto_paginate(db, form)
        db.refresh(form)
    competencies = _competencies(db, form)
    active_target_count = len([t for t in form.targets if t.active])
    plan = page_plan_for_form(form, competencies)
    use_placeholder = exam.is_template or not form.pages
    if use_placeholder:
        # Placeholder rows mirror the rows that actually fit the resolved orientation.
        per_page = max(1, plan.students_per_page)
        placeholder_rows = [
            {
                "target_id": -(index + 1),
                "student_id": -(index + 1),
                "display_label": f"Student {index + 1}",
                "display_order": index + 1,
            }
            for index in range(per_page)
        ]
        page = _PreviewPage(page_no=1, qr_token="preview", target_rows_json=placeholder_rows)
        layout = calculate_peer_review_layout(form, page, competencies, plan)
        pages = [{"page_no": 1, "layout": _layout_to_dict(layout)}]
    else:
        pages = [
            {
                "page_no": page.page_no,
                "layout": _layout_to_dict(
                    calculate_peer_review_layout(form, page, competencies, plan)
                ),
            }
            for page in form.pages
        ]
    return {
        "is_template": exam.is_template,
        "placeholder": use_placeholder,
        "active_target_count": active_target_count,
        "score_min": form.score_min,
        "score_max": form.score_max,
        "students_per_page": form.students_per_page or DEFAULT_TARGETS_PER_PAGE,
        "page_orientation_mode": form.page_orientation_mode or "auto",
        "layout_density": form.layout_density or DEFAULT_DENSITY_MODE,
        "page_plan": plan.as_map(),
        "competencies": [
            {
                "id": comp.id,
                "name": comp.name,
                "name_furigana": comp.name_furigana or "",
                "short_label": comp.short_label or "",
            }
            for comp in competencies
        ],
        "pages": pages,
    }


def peer_review_rater_group_has_data(db: Session, form: PeerReviewForm, group_id: int) -> bool:
    batch_ids = select(PeerReviewBatch.id).where(
        PeerReviewBatch.form_id == form.id,
        PeerReviewBatch.rater_group_id == group_id,
    )
    checks = (
        select(func.count(PeerReviewBatch.id)).where(
            PeerReviewBatch.form_id == form.id,
            PeerReviewBatch.rater_group_id == group_id,
        ),
        select(func.count(PeerReviewSubmissionPage.id)).where(
            PeerReviewSubmissionPage.batch_id.in_(batch_ids)
        ),
        select(func.count(PeerReviewRating.id)).where(
            PeerReviewRating.form_id == form.id,
            PeerReviewRating.rater_group_id == group_id,
        ),
        select(func.count(PeerReviewGroupResult.id)).where(
            PeerReviewGroupResult.form_id == form.id,
            PeerReviewGroupResult.rater_group_id == group_id,
        ),
    )
    return any((db.scalar(query) or 0) > 0 for query in checks)
