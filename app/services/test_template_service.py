from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from app.models import (
    Competency,
    MockExam,
    PhysicalSheet,
    ProblemGroup,
    Question,
    ScoringSection,
)
from app.services.answer_key_service import validate_answer_key
from app.services.exam_service import ExamCreateInput, validate_exam_input
from app.services.list_filter_service import (
    ListFilters,
    PaginatedResult,
    apply_filters,
    apply_filters_and_paginate,
)
from app.services.peer_review_builder_service import (
    copy_peer_review_setup,
    peer_review_template_issues,
    seed_peer_review_template_defaults,
)


@dataclass(frozen=True)
class TemplateCreateInput:
    name: str
    assessment_type: str
    scoring_mode: str
    numbering_mode: str
    template_description: str | None = None


@dataclass(frozen=True)
class TemplateUpdateInput:
    name: str
    template_description: str | None = None
    # Explicit pass mark: threshold plus how to read it ("points" or "percentage").
    pass_mark_value: float | None = None
    pass_mark_type: str | None = None


@dataclass(frozen=True)
class BuilderQuestionInput:
    id: int | None
    number_label: str | None
    correct_answer: str | None
    choice_count: int
    points: float
    scoring_section_id: int | None


@dataclass(frozen=True)
class BuilderGroupInput:
    id: int | None
    problem_title: str
    questions: list[BuilderQuestionInput]


@dataclass(frozen=True)
class BuilderSheetInput:
    id: int
    sheet_name: str
    groups: list[BuilderGroupInput]


# Shown when staff try to edit/build/reload or spawn a run from an archived template.
ARCHIVED_TEMPLATE_ERROR = (
    "This template is archived and read-only. Duplicate it to an active copy to edit, or "
    "view, export, and delete it as-is."
)


def list_templates(db: Session, filters: ListFilters | None = None) -> list[MockExam]:
    # Default (filters=None) hides archived templates and lists newest first.
    templates = list(db.scalars(select(MockExam).where(MockExam.is_template.is_(True))))
    return apply_filters(templates, filters or ListFilters())


def list_templates_paginated(db: Session, filters: ListFilters) -> PaginatedResult:
    templates = list(db.scalars(select(MockExam).where(MockExam.is_template.is_(True))))
    return apply_filters_and_paginate(templates, filters)


def ensure_template_editable(template: MockExam) -> None:
    """Route guard for mutating template actions; archived templates are read-only."""
    if template.template_status == "archived":
        raise HTTPException(status_code=409, detail=ARCHIVED_TEMPLATE_ERROR)


def get_template(db: Session, template_id: int) -> MockExam | None:
    template = db.get(MockExam, template_id)
    if template is None or not template.is_template:
        return None
    return template


def create_blank_template(db: Session, data: TemplateCreateInput) -> MockExam:
    # Reuse the existing exam-input validation (templates have no exam_date).
    validate_exam_input(
        ExamCreateInput(
            name=data.name,
            assessment_type=data.assessment_type,
            exam_date=None,
            scoring_mode=data.scoring_mode,
            numbering_mode=data.numbering_mode,
        )
    )
    template = MockExam(
        name=data.name.strip(),
        assessment_type=data.assessment_type,
        scoring_mode=data.scoring_mode,
        numbering_mode=data.numbering_mode,
        status="draft",
        is_template=True,
        # A freshly created template is immediately reusable, so it starts "active".
        # "archived" is reserved for archive_template(). status="draft" tracks the
        # separate per-exam build pipeline, which templates do not run.
        template_status="active",
        template_description=(data.template_description or "").strip() or None,
    )
    if data.assessment_type == "generic_test":
        template.physical_sheets.append(
            PhysicalSheet(
                sheet_no=1,
                sheet_name="解答用紙",
                slug="answer-sheet",
                required=True,
                display_order=1,
            )
        )
        template.scoring_sections.append(
            ScoringSection(
                code="raw_score",
                name="素点",
                display_order=1,
            )
        )
    db.add(template)
    db.commit()
    db.refresh(template)
    if data.assessment_type == "peer_review":
        # Peer-review templates store their reusable structure in a PeerReviewForm
        # (score scale, students-per-page, rater groups) plus Competency rows — not
        # in physical sheets / questions. Seed sensible editable defaults.
        seed_peer_review_template_defaults(db, template)
        db.refresh(template)
    return template


def archive_template(db: Session, template: MockExam) -> MockExam:
    template.template_status = "archived"
    template.archived_at = datetime.utcnow()
    db.commit()
    db.refresh(template)
    return template


def update_template(db: Session, template: MockExam, data: TemplateUpdateInput) -> MockExam:
    """Edit a template's metadata and explicit pass mark.

    The answer key (questions/correct answers) is changed via reload, not here.
    """
    if not template.is_template:
        raise ValueError("Cannot edit a non-template exam as a template.")
    if not data.name.strip():
        raise ValueError("Template name is required.")
    template.name = data.name.strip()
    template.template_description = (data.template_description or "").strip() or None
    if data.pass_mark_type not in (None, "points", "percentage"):
        raise ValueError('Pass mark type must be "points" or "percentage".')
    if data.pass_mark_value is not None and not data.pass_mark_type:
        raise ValueError("Pass mark value needs a pass mark type.")
    if data.pass_mark_type and data.pass_mark_value is None:
        raise ValueError("Pass mark type needs a pass mark value.")
    template.pass_mark_value = data.pass_mark_value
    template.pass_mark_type = data.pass_mark_type
    db.commit()
    db.refresh(template)
    return template


def save_template_builder(
    db: Session,
    template: MockExam,
    sheets: list[BuilderSheetInput],
) -> MockExam:
    if not template.is_template:
        raise ValueError("The template builder can only edit templates.")

    sheet_by_id = {sheet.id: sheet for sheet in template.physical_sheets}
    question_by_id = {question.id: question for question in template.questions}
    group_by_id = {group.id: group for group in template.problem_groups}
    section_ids = {section.id for section in template.scoring_sections}
    kept_question_ids: set[int] = set()
    kept_group_ids: set[int] = set()
    global_question_no = 1
    submitted_sheet_ids = {sheet.id for sheet in sheets}

    for sheet_input in sheets:
        physical_sheet = sheet_by_id.get(sheet_input.id)
        if physical_sheet is None:
            raise ValueError("Unknown physical sheet.")
        sheet_name = sheet_input.sheet_name.strip()
        if not sheet_name:
            raise ValueError("Sheet name is required.")
        physical_sheet.sheet_name = sheet_name

        for problem_no, group_input in enumerate(sheet_input.groups, start=1):
            problem_title = group_input.problem_title.strip() or f"もんだい{problem_no}"
            if group_input.id is not None and group_input.id in group_by_id:
                group = group_by_id[group_input.id]
                if group.physical_sheet_id != physical_sheet.id:
                    raise ValueError("Problem group belongs to a different sheet.")
            else:
                group = ProblemGroup(
                    physical_sheet=physical_sheet,
                    problem_no=problem_no,
                    problem_title=problem_title,
                    subquestion_count=len(group_input.questions),
                    display_order=problem_no,
                )
                template.problem_groups.append(group)
                db.flush()
            group.problem_no = problem_no
            group.problem_title = problem_title
            group.subquestion_count = len(group_input.questions)
            group.display_order = problem_no
            kept_group_ids.add(group.id)

            for subquestion_no, question_input in enumerate(group_input.questions, start=1):
                _validate_builder_question(question_input, section_ids, template.assessment_type)
                number_label = _question_number_label(question_input.number_label, subquestion_no)
                if question_input.id is not None and question_input.id in question_by_id:
                    question = question_by_id[question_input.id]
                    if question.physical_sheet_id != physical_sheet.id:
                        raise ValueError("Question belongs to a different sheet.")
                else:
                    question = Question(
                        physical_sheet=physical_sheet,
                        scoring_section_id=question_input.scoring_section_id,
                        question_no=global_question_no,
                        problem_no=problem_no,
                        subquestion_no=subquestion_no,
                        local_question_no=subquestion_no,
                        local_question_label=number_label,
                        display_label=f"{problem_title}-問{number_label}",
                        correct_answer=(question_input.correct_answer or "").strip() or None,
                        choice_count=question_input.choice_count,
                        points=question_input.points,
                        display_order=global_question_no,
                    )
                    template.questions.append(question)
                    db.flush()
                question.scoring_section_id = question_input.scoring_section_id
                question.question_no = global_question_no
                question.problem_no = problem_no
                question.subquestion_no = subquestion_no
                question.local_question_no = subquestion_no
                question.local_question_label = number_label
                question.display_label = f"{problem_title}-問{number_label}"
                question.correct_answer = (question_input.correct_answer or "").strip() or None
                question.choice_count = question_input.choice_count
                question.points = question_input.points
                question.display_order = global_question_no
                kept_question_ids.add(question.id)
                global_question_no += 1

    sheet_ids = submitted_sheet_ids
    removed_question_ids = {
        question.id
        for question in template.questions
        if question.physical_sheet_id in sheet_ids and question.id not in kept_question_ids
    }
    removed_group_ids = {
        group.id
        for group in template.problem_groups
        if group.physical_sheet_id in sheet_ids and group.id not in kept_group_ids
    }
    if template.template_runs and (removed_question_ids or removed_group_ids):
        raise ValueError(
            "This template already has test runs, so existing questions or 問題 groups "
            "cannot be deleted from the builder."
        )

    for question in list(template.questions):
        if question.physical_sheet_id in sheet_ids and question.id not in kept_question_ids:
            template.questions.remove(question)
    for group in list(template.problem_groups):
        if group.physical_sheet_id in sheet_ids and group.id not in kept_group_ids:
            template.problem_groups.remove(group)

    template.numbering_mode = "reset_per_problem"
    db.commit()
    db.refresh(template)
    return template


def _question_number_label(value: str | None, fallback: int) -> str:
    label = (value or "").strip()
    return label or str(fallback)


def _validate_builder_question(
    question: BuilderQuestionInput,
    section_ids: set[int],
    assessment_type: str,
) -> None:
    if question.choice_count < 2 or question.choice_count > 5:
        raise ValueError("Choices must be between 2 and 5.")
    if question.points < 0:
        raise ValueError("Points must be 0 or greater.")
    if question.scoring_section_id is not None and question.scoring_section_id not in section_ids:
        raise ValueError("Unknown scoring section.")
    if assessment_type != "peer_review" and question.scoring_section_id is None:
        raise ValueError("Scoring section is required for each question.")
    if question.correct_answer:
        valid_answers = {str(number) for number in range(1, question.choice_count + 1)}
        if question.correct_answer not in valid_answers:
            raise ValueError("Correct answer must be within the choice range.")


def delete_template(db: Session, template: MockExam) -> None:
    """Delete a template and its setup. Runs/versions are kept but disassociated."""
    if not template.is_template:
        raise ValueError("Cannot delete a non-template exam as a template.")
    # Runs created from this template stay (independent), but drop the dangling reference.
    for run in list(template.template_runs):
        run.source_template_id = None
    db.execute(
        sa_update(MockExam)
        .where(MockExam.previous_template_id == template.id)
        .values(previous_template_id=None)
    )
    db.delete(template)
    db.commit()


def create_run_from_template(
    db: Session,
    template: MockExam,
    run_name: str,
    exam_date: str | None = None,
) -> MockExam:
    """Create an independent test run by deep-copying a template's setup.

    Setup (sheets, scoring sections, problem groups, questions, competencies) is copied
    into brand-new rows so the run never live-links back to the template; editing the
    template later must not change existing runs. Students, generated sheets, scans,
    responses, results, and exports are never copied.
    """
    if not template.is_template:
        raise ValueError("Cannot create a run from a non-template exam.")
    if template.assessment_type == "peer_review":
        issues = peer_review_template_issues(db, template)
        if issues:
            raise ValueError("This peer-review template is not ready yet: " + " ".join(issues))
    elif validate_answer_key(template):
        raise ValueError(
            "This template is not ready yet. Please complete the answer key in the template builder first."
        )

    run = MockExam(
        name=run_name.strip(),
        assessment_type=template.assessment_type,
        scoring_mode=template.scoring_mode,
        numbering_mode=template.numbering_mode,
        pass_mark_value=template.pass_mark_value,
        pass_mark_type=template.pass_mark_type,
        exam_date=exam_date or None,
        status="draft",
        is_template=False,
        source_template_id=template.id,
        template_version=template.template_version,
    )

    # Map template child id -> freshly copied object, so copied questions/problem groups
    # can be re-pointed at the run's own sheets and sections.
    sheet_map: dict[int, PhysicalSheet] = {}
    for sheet in template.physical_sheets:
        new_sheet = PhysicalSheet(
            sheet_no=sheet.sheet_no,
            sheet_name=sheet.sheet_name,
            slug=sheet.slug,
            required=sheet.required,
            display_order=sheet.display_order,
        )
        run.physical_sheets.append(new_sheet)
        sheet_map[sheet.id] = new_sheet

    section_map: dict[int, ScoringSection] = {}
    for section in template.scoring_sections:
        new_section = ScoringSection(
            code=section.code,
            name=section.name,
            display_order=section.display_order,
            description=section.description,
        )
        run.scoring_sections.append(new_section)
        section_map[section.id] = new_section

    for group in template.problem_groups:
        new_group = ProblemGroup(
            problem_no=group.problem_no,
            problem_title=group.problem_title,
            subquestion_count=group.subquestion_count,
            display_order=group.display_order,
        )
        if group.physical_sheet_id is not None:
            new_group.physical_sheet = sheet_map.get(group.physical_sheet_id)
        run.problem_groups.append(new_group)

    for question in template.questions:
        new_question = Question(
            question_no=question.question_no,
            problem_no=question.problem_no,
            subquestion_no=question.subquestion_no,
            local_question_no=question.local_question_no,
            local_question_label=question.local_question_label,
            display_label=question.display_label,
            test_section=question.test_section,
            correct_answer=question.correct_answer,
            choice_count=question.choice_count,
            points=question.points,
            item_type=question.item_type,
            display_order=question.display_order,
        )
        new_question.physical_sheet = sheet_map[question.physical_sheet_id]
        if question.scoring_section_id is not None:
            new_question.scoring_section = section_map.get(question.scoring_section_id)
        run.questions.append(new_question)

    for competency in template.competencies:
        run.competencies.append(
            Competency(
                name=competency.name,
                name_furigana=competency.name_furigana,
                short_label=competency.short_label,
                name_ruby_json=competency.name_ruby_json,
                description=competency.description,
                min_score=competency.min_score,
                max_score=competency.max_score,
                display_order=competency.display_order,
            )
        )

    db.add(run)
    db.commit()
    db.refresh(run)
    # Copy reusable peer-review structure (score scale, students-per-page, rater
    # groups) into a fresh form for the run. Targets, pages, scans, and results stay
    # session-specific and are never copied.
    if template.assessment_type == "peer_review":
        copy_peer_review_setup(db, template, run)
        db.refresh(run)
    return run


def duplicate_template(db: Session, template: MockExam) -> MockExam:
    """Deep-copy a template's setup into a new template with ' (copy)' appended to the name.

    Only copies physical sheets, scoring sections, problem groups, questions, and competencies.
    Students, scans, responses, results, and exports are never copied.
    """
    if not template.is_template:
        raise ValueError("Cannot duplicate a non-template exam.")

    new_template = MockExam(
        name=f"{template.name} (copy)",
        assessment_type=template.assessment_type,
        scoring_mode=template.scoring_mode,
        numbering_mode=template.numbering_mode,
        pass_mark_value=template.pass_mark_value,
        pass_mark_type=template.pass_mark_type,
        status="draft",
        is_template=True,
        template_status="active",
        template_description=template.template_description,
    )

    sheet_map: dict[int, PhysicalSheet] = {}
    for sheet in template.physical_sheets:
        new_sheet = PhysicalSheet(
            sheet_no=sheet.sheet_no,
            sheet_name=sheet.sheet_name,
            slug=sheet.slug,
            required=sheet.required,
            display_order=sheet.display_order,
        )
        new_template.physical_sheets.append(new_sheet)
        sheet_map[sheet.id] = new_sheet

    section_map: dict[int, ScoringSection] = {}
    for section in template.scoring_sections:
        new_section = ScoringSection(
            code=section.code,
            name=section.name,
            display_order=section.display_order,
            description=section.description,
        )
        new_template.scoring_sections.append(new_section)
        section_map[section.id] = new_section

    for group in template.problem_groups:
        new_group = ProblemGroup(
            problem_no=group.problem_no,
            problem_title=group.problem_title,
            subquestion_count=group.subquestion_count,
            display_order=group.display_order,
        )
        if group.physical_sheet_id is not None:
            new_group.physical_sheet = sheet_map.get(group.physical_sheet_id)
        new_template.problem_groups.append(new_group)

    for question in template.questions:
        new_question = Question(
            question_no=question.question_no,
            problem_no=question.problem_no,
            subquestion_no=question.subquestion_no,
            local_question_no=question.local_question_no,
            local_question_label=question.local_question_label,
            display_label=question.display_label,
            test_section=question.test_section,
            correct_answer=question.correct_answer,
            choice_count=question.choice_count,
            points=question.points,
            item_type=question.item_type,
            display_order=question.display_order,
        )
        new_question.physical_sheet = sheet_map[question.physical_sheet_id]
        if question.scoring_section_id is not None:
            new_question.scoring_section = section_map.get(question.scoring_section_id)
        new_template.questions.append(new_question)

    for competency in template.competencies:
        new_template.competencies.append(
            Competency(
                name=competency.name,
                name_furigana=competency.name_furigana,
                short_label=competency.short_label,
                name_ruby_json=competency.name_ruby_json,
                description=competency.description,
                min_score=competency.min_score,
                max_score=competency.max_score,
                display_order=competency.display_order,
            )
        )

    db.add(new_template)
    db.commit()
    db.refresh(new_template)
    if template.assessment_type == "peer_review":
        copy_peer_review_setup(db, template, new_template)
        db.refresh(new_template)
    return new_template


def list_runs_for_template(db: Session, template: MockExam) -> list[MockExam]:
    return list(
        db.scalars(
            select(MockExam)
            .where(MockExam.is_template.is_(False))
            .where(MockExam.source_template_id == template.id)
            .order_by(MockExam.created_at.desc())
        )
    )
