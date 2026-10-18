from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import (
    ProblemGroup,
    Question,
    Response,
    ScanBatch,
    ScannedPage,
    SheetInstance,
    SheetTemplate,
)
from app.services.review_service import REVIEWABLE_RESPONSE_STATUSES

BROKEN_RESPONSE_MESSAGE = (
    "この回答は問題データとの紐付けが壊れているため確認できません。"
    "テンプレート確認または修復を実行してください。"
)


def resolve_coordinate_question(
    db: Session,
    sheet_instance: SheetInstance,
    coordinate_question: dict[str, Any],
) -> Question | None:
    """Resolve a coordinate-map question entry to current question metadata."""
    question_id = coordinate_question.get("question_id")
    if question_id is not None:
        question = db.get(Question, int(question_id))
        if question is not None:
            return question

    matches = possible_coordinate_question_matches(db, sheet_instance, coordinate_question)
    return matches[0] if len(matches) == 1 else None


def possible_coordinate_question_matches(
    db: Session,
    sheet_instance: SheetInstance,
    coordinate_question: dict[str, Any],
) -> list[Question]:
    display_label = str(coordinate_question.get("display_label") or "").strip()
    choice_count = len(coordinate_question.get("choices") or [])
    if not display_label or choice_count <= 0:
        return []

    candidates = list(
        db.scalars(
            select(Question)
            .where(Question.exam_id == sheet_instance.exam_id)
            .where(Question.physical_sheet_id == sheet_instance.physical_sheet_id)
            .where(Question.choice_count == choice_count)
            .where(
                (Question.display_label == display_label)
                | (Question.local_question_label == display_label)
            )
            .order_by(Question.display_order, Question.id)
        )
    )
    group_label = str(coordinate_question.get("group_label") or "").strip()
    if group_label:
        candidates = [
            question
            for question in candidates
            if _question_matches_group_label(db, sheet_instance, question, group_label)
        ]
    return candidates


def update_coordinate_question_id(
    template: SheetTemplate,
    coordinate_question: dict[str, Any],
    question_id: int,
) -> bool:
    coordinate_map = deepcopy(template.coordinate_map_json or {})
    changed = False
    target_index = coordinate_question.get("coordinate_index")
    target_key = coordinate_question.get("coordinate_key")
    matching_entries = [
        (index, entry)
        for index, entry in enumerate(coordinate_map.get("questions") or [])
        if _coordinate_update_match(index, entry, coordinate_question, target_index, target_key)
    ]
    if len(matching_entries) != 1:
        return False
    _index, entry = matching_entries[0]
    if entry.get("question_id") != question_id:
        entry["question_id"] = question_id
        changed = True
    if not changed:
        return False
    old_question_id = coordinate_question.get("question_id")
    old_id_count = sum(
        1
        for item in coordinate_map.get("questions") or []
        if item.get("question_id") == old_question_id
    )
    if old_id_count <= 1:
        for group in coordinate_map.get("groups") or []:
            group["question_ids"] = [
                question_id if qid == old_question_id else qid
                for qid in group.get("question_ids") or []
            ]
    template.coordinate_map_json = coordinate_map
    flag_modified(template, "coordinate_map_json")
    return True


def repair_orphan_responses_for_exam(db: Session, exam_id: int) -> dict:
    details = []
    repaired = 0
    for response in _orphan_responses(db, exam_id):
        before = _response_repair_row(response)
        repair = repair_orphan_response(db, response)
        after = _response_repair_row(response)
        details.append({"before": before, "after": after, **repair})
        if repair["repaired"]:
            repaired += 1
    db.flush()
    return {"exam_id": exam_id, "checked": len(details), "repaired": repaired, "details": details}


def repair_orphan_response(db: Session, response: Response) -> dict:
    page = response.scanned_page
    sheet_instance = page.sheet_instance if page else None
    template = sheet_instance.sheet_template if sheet_instance else None
    if page is None or sheet_instance is None or template is None:
        return {"repaired": False, "reason": "missing_page_or_sheet_instance", "match_id": None}

    entry = coordinate_entry_for_response(template.coordinate_map_json or {}, response)
    if entry is None:
        entry = coordinate_entry_for_question_id(template.coordinate_map_json or {}, response.question_id)
    if entry is None:
        return {"repaired": False, "reason": "coordinate_entry_not_found", "match_id": None}

    matches = possible_coordinate_question_matches(db, sheet_instance, entry)
    if len(matches) != 1:
        return {
            "repaired": False,
            "reason": "ambiguous_or_missing_match",
            "match_id": None,
            "possible_matching_question_ids": [question.id for question in matches],
        }

    match = matches[0]
    response.question_id = match.id
    update_coordinate_question_id(template, entry, match.id)
    db.flush()
    db.expire(response, ["question"])
    db.expire(page, ["responses"])
    list(page.responses)
    _normalize_page_review_status(page)
    return {"repaired": True, "reason": "relinked", "match_id": match.id}


def coordinate_entry_for_question_id(coordinate_map: dict, question_id: int) -> dict | None:
    matches = [
        entry
        for entry in coordinate_entries(coordinate_map)
        if entry.get("question_id") == question_id
    ]
    return matches[0] if len(matches) == 1 else None


def coordinate_entry_for_response(coordinate_map: dict, response: Response) -> dict | None:
    """Infer the coordinate row that produced a legacy response.

    Response rows are created in coordinate-map order for each scanned page. Until a
    coordinate key is persisted on Response, the page-local response ordinal is the
    strongest available link back to the exact physical row.
    """
    page = response.scanned_page
    if page is None:
        return None
    page_responses = sorted(page.responses, key=lambda item: item.id)
    try:
        response_index = [item.id for item in page_responses].index(response.id)
    except ValueError:
        return None
    questions = coordinate_entries(coordinate_map)
    if response_index >= len(questions):
        return None
    entry = questions[response_index]
    return entry if entry.get("question_id") == response.question_id else None


def coordinate_map_check(db: Session, template: SheetTemplate) -> dict:
    coordinate_map = template.coordinate_map_json or {}
    questions = coordinate_entries(coordinate_map)
    missing = []
    valid_count = 0
    probe_instance = _probe_sheet_instance(db, template)
    for entry in questions:
        question = db.get(Question, int(entry["question_id"])) if entry.get("question_id") else None
        if question is not None:
            valid_count += 1
            continue
        possible = possible_coordinate_question_matches(db, probe_instance, entry) if probe_instance else []
        missing.append(
            {
                "question_id": entry.get("question_id"),
                "display_label": entry.get("display_label"),
                "group_label": entry.get("group_label"),
                "choice_count": len(entry.get("choices") or []),
                "possible_matching_question_ids": [question.id for question in possible],
                "recommended_repair_action": "auto_repair" if len(possible) == 1 else "manual_template_check",
            }
        )
    return {
        "template_id": template.id,
        "coordinate_question_count": len(questions),
        "valid_question_count": valid_count,
        "missing_question_ids": [row["question_id"] for row in missing],
        "missing_questions": missing,
        "duplicate_question_ids": _duplicate_values(
            [entry.get("question_id") for entry in questions if entry.get("question_id") is not None]
        ),
        "entries_by_question_id": _entries_by_field(questions, "question_id"),
        "duplicate_display_group_labels": _duplicate_values(
            [_display_group_key(entry) for entry in questions]
        ),
        "duplicate_coordinate_keys": _duplicate_values(
            [entry["coordinate_key"] for entry in questions]
        ),
        "coordinate_entries": [
            {
                "index": entry["coordinate_index"],
                "question_id": entry.get("question_id"),
                "display_label": entry.get("display_label"),
                "group_label": entry.get("group_label"),
                "choice_count": len(entry.get("choices") or []),
                "coordinate_key": entry["coordinate_key"],
            }
            for entry in questions
        ],
    }


def coordinate_entries(coordinate_map: dict) -> list[dict[str, Any]]:
    return [
        {
            **entry,
            "coordinate_index": index,
            "coordinate_key": coordinate_entry_key(entry, index),
        }
        for index, entry in enumerate(coordinate_map.get("questions") or [])
    ]


def coordinate_entry_key(entry: dict[str, Any], index: int) -> str:
    existing = str(entry.get("coordinate_key") or "").strip()
    if existing:
        return existing
    group_label = str(entry.get("group_label") or "").strip()
    display_label = str(entry.get("display_label") or "").strip()
    choice_count = len(entry.get("choices") or [])
    return f"{index}|{group_label}|{display_label}|{choice_count}"


def coordinate_entries_matching_question(
    db: Session,
    sheet_instance: SheetInstance,
    question: Question,
    coordinate_map: dict,
) -> list[dict[str, Any]]:
    matches = []
    labels = {
        str(question.display_label or "").strip(),
        str(question.local_question_label or "").strip(),
    }
    labels.discard("")
    for entry in coordinate_entries(coordinate_map):
        display_label = str(entry.get("display_label") or "").strip()
        if display_label not in labels:
            continue
        if len(entry.get("choices") or []) != question.choice_count:
            continue
        group_label = str(entry.get("group_label") or "").strip()
        if group_label and not _question_matches_group_label(
            db, sheet_instance, question, group_label
        ):
            continue
        matches.append(entry)
    return matches


def _coordinate_update_match(
    index: int,
    entry: dict[str, Any],
    coordinate_question: dict[str, Any],
    target_index: int | None,
    target_key: str | None,
) -> bool:
    if target_index is not None:
        return index == int(target_index)
    if target_key:
        return coordinate_entry_key(entry, index) == target_key
    return (
        entry.get("question_id") == coordinate_question.get("question_id")
        and _same_coordinate_metadata(entry, coordinate_question)
    )


def _duplicate_values(values: list[Any]) -> list[Any]:
    counts: dict[Any, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return [value for value, count in counts.items() if count > 1]


def _entries_by_field(entries: list[dict[str, Any]], field: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        key = str(entry.get(field))
        grouped.setdefault(key, []).append(
            {
                "index": entry["coordinate_index"],
                "coordinate_key": entry["coordinate_key"],
                "display_label": entry.get("display_label"),
                "group_label": entry.get("group_label"),
                "choice_count": len(entry.get("choices") or []),
            }
        )
    return grouped


def _display_group_key(entry: dict[str, Any]) -> str:
    return "|".join(
        [
            str(entry.get("group_label") or "").strip(),
            str(entry.get("display_label") or "").strip(),
            str(len(entry.get("choices") or [])),
        ]
    )


def orphan_review_problem_rows(db: Session, exam_id: int) -> list[dict]:
    rows = []
    for response in _orphan_responses(db, exam_id):
        page = response.scanned_page
        sheet_instance = page.sheet_instance if page else None
        template = sheet_instance.sheet_template if sheet_instance else None
        entry = (
            coordinate_entry_for_response(template.coordinate_map_json or {}, response)
            if template
            else None
        )
        if entry is None and template:
            entry = coordinate_entry_for_question_id(
                template.coordinate_map_json or {}, response.question_id
            )
        matches = possible_coordinate_question_matches(db, sheet_instance, entry) if sheet_instance and entry else []
        rows.append(
            {
                "response_id": response.id,
                "scanned_page_id": response.scanned_page_id,
                "question_id": response.question_id,
                "status": response.status,
                "display_label": entry.get("display_label") if entry else None,
                "group_label": entry.get("group_label") if entry else None,
                "choice_count": len(entry.get("choices") or []) if entry else None,
                "possible_matching_question_ids": [question.id for question in matches],
                "recoverable": len(matches) == 1,
                "message": BROKEN_RESPONSE_MESSAGE,
            }
        )
    return rows


def _orphan_responses(db: Session, exam_id: int) -> list[Response]:
    return list(
        db.scalars(
            select(Response)
            .join(ScannedPage, Response.scanned_page_id == ScannedPage.id)
            .join(ScanBatch, ScannedPage.scan_batch_id == ScanBatch.id)
            .outerjoin(Question, Response.question_id == Question.id)
            .where(ScanBatch.exam_id == exam_id)
            .where(Response.status.in_(REVIEWABLE_RESPONSE_STATUSES))
            .where(Question.id.is_(None))
            .order_by(Response.id)
        )
    )


def _question_matches_group_label(
    db: Session,
    sheet_instance: SheetInstance,
    question: Question,
    group_label: str,
) -> bool:
    if question.problem_no is None:
        return False
    normalized = _norm(group_label)
    group = db.scalar(
        select(ProblemGroup)
        .where(ProblemGroup.exam_id == sheet_instance.exam_id)
        .where(ProblemGroup.physical_sheet_id == sheet_instance.physical_sheet_id)
        .where(ProblemGroup.problem_no == question.problem_no)
    )
    labels = {
        f"もんだい{question.problem_no}",
        f"問題{question.problem_no}",
        str(question.problem_no),
    }
    if group and group.problem_title:
        labels.add(group.problem_title)
    return normalized in {_norm(label) for label in labels}


def _same_coordinate_metadata(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left.get("display_label") == right.get("display_label")
        and left.get("group_label") == right.get("group_label")
        and len(left.get("choices") or []) == len(right.get("choices") or [])
    )


def _probe_sheet_instance(db: Session, template: SheetTemplate) -> SheetInstance | None:
    return db.scalar(
        select(SheetInstance)
        .where(SheetInstance.sheet_template_id == template.id)
        .order_by(SheetInstance.id)
    )


def _response_repair_row(response: Response) -> dict:
    return {
        "response_id": response.id,
        "question_id": response.question_id,
        "question_exists": response.question is not None,
        "scanned_page_id": response.scanned_page_id,
        "status": response.status,
    }


def _norm(value: str) -> str:
    return str(value).replace(" ", "").replace("　", "").strip().lower()


def _normalize_page_review_status(page: ScannedPage) -> None:
    if page.review_status not in {"ok", "needs_review"}:
        return
    if page.sheet_instance_id is None or page.alignment_status == "failed":
        page.review_status = "needs_review"
        return
    if not page.responses:
        page.review_status = "needs_review"
        return
    if any(response.status in REVIEWABLE_RESPONSE_STATUSES for response in page.responses):
        page.review_status = "needs_review"
        return
    page.review_status = "ok"
