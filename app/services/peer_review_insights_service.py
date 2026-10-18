from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Competency,
    MockExam,
    PeerReviewFinalResult,
    PeerReviewForm,
    PeerReviewGroupResult,
    PeerReviewRating,
    Student,
)
from app.services.peer_review_review_service import PROBLEM_STATUSES


def build_peer_review_insights(db: Session, exam: MockExam) -> dict:
    form = db.scalar(select(PeerReviewForm).where(PeerReviewForm.exam_id == exam.id))
    if form is None:
        return _empty_insights()

    students = _students_by_id(db, form)
    groups = list(form.rater_groups)
    competencies = list(
        db.scalars(select(Competency).where(Competency.exam_id == exam.id).order_by(Competency.display_order))
    )
    finals = list(
        db.scalars(
            select(PeerReviewFinalResult)
            .where(PeerReviewFinalResult.form_id == form.id)
            .order_by(PeerReviewFinalResult.final_score.desc())
        )
    )
    group_results = list(
        db.scalars(
            select(PeerReviewGroupResult)
            .where(PeerReviewGroupResult.form_id == form.id)
            .order_by(PeerReviewGroupResult.group_average_score.desc())
        )
    )
    group_by_id = {group.id: group for group in groups}
    group_results_by_student = {}
    for result in group_results:
        group_results_by_student.setdefault(result.target_student_id, {})[result.rater_group_id] = result

    combined = [_final_row(result, students, group_by_id) for result in finals if result.final_score is not None]
    combined.sort(key=lambda row: row["final_score"], reverse=True)
    for rank, row in enumerate(combined, start=1):
        row["rank"] = rank

    group_rankings = []
    for group in groups:
        rows = [
            _group_row(result, students, competencies)
            for result in group_results
            if result.rater_group_id == group.id and result.group_average_score is not None
        ]
        rows.sort(key=lambda row: row["average"], reverse=True)
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        group_rankings.append({"group": group, "rows": rows})

    side_by_side = []
    for student_id, by_group in group_results_by_student.items():
        student = students.get(student_id)
        row_groups = []
        for group in groups:
            result = by_group.get(group.id)
            row_groups.append(
                {
                    "group": group,
                    "average": result.group_average_score if result else None,
                    "rating_count": result.rating_count if result else 0,
                }
            )
        scored_groups = [item for item in row_groups if item["average"] is not None]
        averages = [item["average"] for item in scored_groups]
        leader = max(scored_groups, key=lambda item: item["average"]) if len(averages) >= 2 else None
        side_by_side.append(
            {
                "student": student,
                "groups": row_groups,
                "difference": max(averages) - min(averages) if len(averages) >= 2 else None,
                "leader_name": leader["group"].name if leader else None,
            }
        )
    side_by_side.sort(key=lambda row: row["student"].student_number if row["student"] else "")

    competency_rankings = []
    for comp in competencies:
        combined_rows = []
        if form.combine_groups:
            for result in finals:
                comp_data = (result.competency_breakdown_json or {}).get(str(comp.id), {})
                if comp_data.get("average") is not None:
                    combined_rows.append(
                        {
                            "student": students.get(result.target_student_id),
                            "average": comp_data["average"],
                            "count": comp_data.get("count") or 0,
                        }
                    )
            combined_rows.sort(key=lambda row: row["average"], reverse=True)
        per_group = []
        for group in groups:
            rows = []
            for result in group_results:
                if result.rater_group_id != group.id:
                    continue
                comp_data = (result.competency_breakdown_json or {}).get(str(comp.id), {})
                if comp_data.get("average") is not None:
                    rows.append(
                        {
                            "student": students.get(result.target_student_id),
                            "average": comp_data["average"],
                            "count": comp_data.get("count") or 0,
                        }
                    )
            rows.sort(key=lambda row: row["average"], reverse=True)
            per_group.append({"group": group, "rows": rows})
        competency_rankings.append(
            {"competency": comp, "combined_rows": combined_rows, "group_rankings": per_group}
        )

    scores = [row["final_score"] for row in combined]
    group_scores = [result.group_average_score for result in group_results if result.group_average_score is not None]
    total_ratings = db.scalar(
        select(func.count(PeerReviewRating.id)).where(PeerReviewRating.form_id == form.id)
    ) or 0
    needs_review_count = db.scalar(
        select(func.count(PeerReviewRating.id))
        .where(PeerReviewRating.form_id == form.id)
        .where(PeerReviewRating.status.in_(PROBLEM_STATUSES))
    ) or 0

    # Only count as "has results" when there is actual scored data, not merely empty
    # group sections (the recalc always writes one group_result per target/group, even
    # when every rating was a confirmed blank/invalid and the averages are all None).
    has_results = bool(
        combined
        or any(section["rows"] for section in group_rankings)
        or any(
            block["combined_rows"] or any(group["rows"] for group in block["group_rankings"])
            for block in competency_rankings
        )
    )

    return {
        "form": form,
        "has_results": has_results,
        "summary": {
            "students_reviewed": len(group_results_by_student) or len(combined),
            "total_anonymous_ratings": total_ratings,
            "group_count": len(groups),
            "group_names": [group.name for group in groups],
            "class_average": _avg(scores or group_scores),
            "highest_score": max(scores or group_scores) if (scores or group_scores) else None,
            "lowest_score": min(scores or group_scores) if (scores or group_scores) else None,
            "needs_review_count": needs_review_count,
            "combine_groups": form.combine_groups,
        },
        "combined_ranking": combined,
        "group_rankings": group_rankings,
        "competency_rankings": competency_rankings,
        "side_by_side": side_by_side,
        "visual_bars": {"max_score": form.score_max or 5},
    }


def _students_by_id(db: Session, form: PeerReviewForm) -> dict[int, Student]:
    student_ids = [target.student_id for target in form.targets]
    return {student.id: student for student in db.scalars(select(Student).where(Student.id.in_(student_ids)))}


def _final_row(result: PeerReviewFinalResult, students: dict, group_by_id: dict) -> dict:
    groups = []
    for group_id, group_data in (result.group_breakdown_json or {}).items():
        groups.append(
            {
                "group": group_by_id.get(int(group_id)) if str(group_id).isdigit() else None,
                "name": group_data.get("name"),
                "average": group_data.get("average"),
                "rating_count": group_data.get("rating_count") or 0,
            }
        )
    comp_items = [
        item
        for item in (result.competency_breakdown_json or {}).values()
        if item.get("average") is not None
    ]
    comp_items.sort(key=lambda item: item["average"], reverse=True)
    return {
        "student": students.get(result.target_student_id),
        "final_score": result.final_score,
        "total_rating_count": result.total_rating_count,
        "groups": groups,
        "strongest_competency": comp_items[0] if comp_items else None,
        "weakest_competency": comp_items[-1] if comp_items else None,
    }


def _group_row(result: PeerReviewGroupResult, students: dict, competencies: list[Competency]) -> dict:
    breakdown = result.competency_breakdown_json or {}
    return {
        "student": students.get(result.target_student_id),
        "average": result.group_average_score,
        "rating_count": result.rating_count,
        "competencies": [
            {
                "competency": comp,
                "average": breakdown.get(str(comp.id), {}).get("average"),
                "count": breakdown.get(str(comp.id), {}).get("count") or 0,
            }
            for comp in competencies
        ],
    }


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _empty_insights() -> dict:
    return {
        "form": None,
        "has_results": False,
        "summary": {
            "students_reviewed": 0,
            "total_anonymous_ratings": 0,
            "group_count": 0,
            "group_names": [],
            "class_average": None,
            "highest_score": None,
            "lowest_score": None,
            "needs_review_count": 0,
            "combine_groups": False,
        },
        "combined_ranking": [],
        "group_rankings": [],
        "competency_rankings": [],
        "side_by_side": [],
        "visual_bars": {"max_score": 5},
    }
