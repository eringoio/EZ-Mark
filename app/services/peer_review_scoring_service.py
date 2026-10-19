from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    Competency,
    PeerReviewFinalResult,
    PeerReviewForm,
    PeerReviewGroupResult,
    PeerReviewRating,
)

SCORABLE_PEER_REVIEW_STATUSES = {"ok", "reviewed"}


def recalculate_anonymous_peer_review_results(
    db: Session, form: PeerReviewForm
) -> list[PeerReviewFinalResult]:
    db.execute(delete(PeerReviewGroupResult).where(PeerReviewGroupResult.form_id == form.id))
    db.execute(delete(PeerReviewFinalResult).where(PeerReviewFinalResult.form_id == form.id))
    competencies = list(
        db.scalars(
            select(Competency)
            .where(Competency.exam_id == form.exam_id)
            .order_by(Competency.display_order)
        )
    )
    finals = []
    for target in [target for target in form.targets if target.active]:
        group_breakdown = {}
        total_count = 0
        for group in [group for group in form.rater_groups if group.active]:
            ratings = list(
                db.scalars(
                    select(PeerReviewRating)
                    .where(PeerReviewRating.form_id == form.id)
                    .where(PeerReviewRating.rater_group_id == group.id)
                    .where(PeerReviewRating.target_student_id == target.student_id)
                    .where(PeerReviewRating.status.in_(SCORABLE_PEER_REVIEW_STATUSES))
                )
            )
            comp_breakdown = {}
            comp_avgs = []
            count = 0
            for comp in competencies:
                scores = [
                    rating.final_score
                    for rating in ratings
                    if rating.competency_id == comp.id and rating.final_score is not None
                ]
                avg = sum(scores) / len(scores) if scores else None
                comp_breakdown[str(comp.id)] = {
                    "name": comp.name,
                    "average": avg,
                    "count": len(scores),
                }
                if avg is not None:
                    comp_avgs.append(avg)
                    count += len(scores)
            group_average = sum(comp_avgs) / len(comp_avgs) if comp_avgs else None
            total_count += count
            db.add(
                PeerReviewGroupResult(
                    exam_id=form.exam_id,
                    form_id=form.id,
                    rater_group_id=group.id,
                    target_student_id=target.student_id,
                    rating_count=count,
                    competency_breakdown_json=comp_breakdown,
                    group_average_score=group_average,
                    calculated_at=datetime.utcnow(),
                )
            )
            group_breakdown[str(group.id)] = {
                "name": group.name,
                "weight_percent": group.weight_percent,
                "average": group_average,
                "rating_count": count,
                "competencies": comp_breakdown,
            }
        final_score = _weighted_score(form, group_breakdown) if form.combine_groups else None
        competency_breakdown = _weighted_competency_breakdown(form, group_breakdown, competencies)
        final = PeerReviewFinalResult(
            exam_id=form.exam_id,
            form_id=form.id,
            target_student_id=target.student_id,
            final_score=final_score,
            combine_groups=form.combine_groups,
            group_breakdown_json=group_breakdown,
            competency_breakdown_json=competency_breakdown,
            total_rating_count=total_count,
            calculated_at=datetime.utcnow(),
        )
        db.add(final)
        finals.append(final)
    db.commit()
    return finals


def _weighted_score(form: PeerReviewForm, group_breakdown: dict) -> float | None:
    available = [
        group
        for group in group_breakdown.values()
        if group["average"] is not None and group["weight_percent"] > 0
    ]
    weighted_groups = [group for group in group_breakdown.values() if group["weight_percent"] > 0]
    if form.missing_group_policy == "require_all" and len(available) != len(weighted_groups):
        return None
    weight_total = sum(group["weight_percent"] for group in available)
    if not available or weight_total <= 0:
        return None
    return sum(group["average"] * (group["weight_percent"] / weight_total) for group in available)


def _weighted_competency_breakdown(
    form: PeerReviewForm, group_breakdown: dict, competencies: list[Competency]
) -> dict:
    breakdown = {}
    for comp in competencies:
        comp_id = str(comp.id)
        groups_for_comp = {}
        total_count = 0
        for group_id, group in group_breakdown.items():
            comp_data = group.get("competencies", {}).get(comp_id)
            if not comp_data:
                continue
            total_count += comp_data.get("count") or 0
            groups_for_comp[group_id] = {
                "average": comp_data.get("average"),
                "weight_percent": group.get("weight_percent") or 0,
            }
        average = _weighted_score(form, groups_for_comp) if form.combine_groups else None
        if average is not None or total_count:
            breakdown[comp_id] = {"name": comp.name, "average": average, "count": total_count}
    return breakdown
