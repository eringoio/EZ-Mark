import re

from app.models import Competency, PeerReviewFinalResult, PeerReviewForm, PeerReviewRating
from app.services.export_service import _write_export


def generate_peer_review_summary_export(db, form: PeerReviewForm, file_type: str = "xlsx") -> str:
    rows = []
    competencies = sorted(form.exam.competencies, key=lambda comp: comp.display_order)
    safe_competency_names = _safe_unique_names([(str(comp.id), comp.name) for comp in competencies])
    for result in sorted(form_results(db, form), key=lambda item: item.student.student_number):
        row = {
            "student_number": result.student.student_number,
            "student_name": result.student.name,
            "class_name": result.student.group_for_exam(form.exam_id),
            "final_score": result.final_score if form.combine_groups else None,
            "total_rating_count": result.total_rating_count,
        }
        group_breakdown = result.group_breakdown_json or {}
        safe_group_names = _safe_unique_names(
            [(str(group_id), group["name"]) for group_id, group in group_breakdown.items()]
        )
        for group_id, group in group_breakdown.items():
            group_name = safe_group_names[str(group_id)]
            row[f"{group_name}_average"] = group["average"]
            row[f"{group_name}_count"] = group["rating_count"]
        combined_competencies = result.competency_breakdown_json or {}
        for competency in competencies:
            comp_key = str(competency.id)
            comp_name = safe_competency_names[comp_key]
            if form.combine_groups:
                row[f"{comp_name}_combined_average"] = combined_competencies.get(comp_key, {}).get(
                    "average"
                )
                row[f"{comp_name}_combined_count"] = combined_competencies.get(comp_key, {}).get(
                    "count"
                )
            for group_id, group in group_breakdown.items():
                group_name = safe_group_names[str(group_id)]
                group_competency = (group.get("competencies") or {}).get(comp_key, {})
                row[f"{comp_name}_{group_name}_average"] = group_competency.get("average")
                row[f"{comp_name}_{group_name}_count"] = group_competency.get("count")
        rows.append(row)
    headers = _summary_headers(rows, competencies, safe_competency_names)
    return _write_export(db, form.exam, "peer_review_summary", file_type, headers, rows)


def generate_peer_review_raw_ratings_export(
    db, form: PeerReviewForm, file_type: str = "xlsx"
) -> str:
    ratings = db.query(PeerReviewRating).filter(PeerReviewRating.form_id == form.id).all()
    rows = [
        {
            "batch_id": rating.batch_id,
            "batch_name": rating.batch.uploaded_file_name,
            "rater_group": rating.rater_group.name,
            "upload_file": rating.batch.uploaded_file_name,
            "page_no": rating.peer_review_page.page_no,
            "target_student_number": rating.target_student.student_number,
            "target_student_name": rating.target_student.name,
            "competency": rating.competency.name,
            "detected_score": rating.detected_score,
            "final_score": rating.final_score,
            "status": rating.status,
            "confidence": rating.confidence,
            "reviewed_manually": bool(rating.reviewed_at),
        }
        for rating in ratings
    ]
    headers = [
        "batch_id",
        "batch_name",
        "rater_group",
        "upload_file",
        "page_no",
        "target_student_number",
        "target_student_name",
        "competency",
        "detected_score",
        "final_score",
        "status",
        "confidence",
        "reviewed_manually",
    ]
    return _write_export(db, form.exam, "peer_review_raw_ratings", file_type, headers, rows)


def generate_peer_review_group_results_export(
    db, form: PeerReviewForm, file_type: str = "xlsx"
) -> str:
    rows = []
    for result in form_results(db, form):
        for group in result.group_breakdown_json.values():
            rows.append(
                {
                    "student_number": result.student.student_number,
                    "student_name": result.student.name,
                    "rater_group": group["name"],
                    "group_weight": group["weight_percent"],
                    "group_average": group["average"],
                    "rating_count": group["rating_count"],
                    "competency_breakdown": group["competencies"],
                }
            )
    headers = [
        "student_number",
        "student_name",
        "rater_group",
        "group_weight",
        "group_average",
        "rating_count",
        "competency_breakdown",
    ]
    return _write_export(db, form.exam, "peer_review_group_results", file_type, headers, rows)


def form_results(db, form: PeerReviewForm):
    return db.query(PeerReviewFinalResult).filter(PeerReviewFinalResult.form_id == form.id).all()


def _headers(rows: list[dict], preferred: list[str]) -> list[str]:
    headers = list(preferred)
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(key)
    return headers


def _summary_headers(
    rows: list[dict], competencies: list[Competency], safe_competency_names: dict[str, str]
) -> list[str]:
    preferred = [
        "student_number",
        "student_name",
        "class_name",
        "final_score",
        "total_rating_count",
    ]
    dynamic = []
    for row in rows:
        for key in row:
            if key not in preferred and key not in dynamic:
                dynamic.append(key)

    def sort_key(header: str) -> tuple[int, str]:
        for index, comp in enumerate(competencies):
            if header.startswith(f"{safe_competency_names[str(comp.id)]}_"):
                return (100 + index, header)
        return (0, header)

    dynamic.sort(key=sort_key)
    return preferred + dynamic


def _safe_unique_names(items: list[tuple[str, str]], max_length: int = 48) -> dict[str, str]:
    counts: dict[str, int] = {}
    safe_names: dict[str, str] = {}
    for item_id, label in items:
        base = _safe_column_part(label, max_length=max_length)
        counts[base] = counts.get(base, 0) + 1
        suffix = "" if counts[base] == 1 else f"_{counts[base]}"
        safe_names[item_id] = f"{base[: max_length - len(suffix)]}{suffix}"
    return safe_names


def _safe_column_part(value: str, max_length: int = 48) -> str:
    cleaned = re.sub(r"[^\w]+", "_", (value or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return (cleaned or "unnamed")[:max_length]
