"""Shared list filtering for templates and sessions.

Templates (``is_template=True``) and sessions/runs (``is_template=False``) are both
``MockExam`` rows, so the same search/type/date/sort/archived filters apply to the
``/templates``, ``/exams``, ``/scan-review``, and ``/insights`` listings. This module is
intentionally free of other service imports so ``exam_service`` and
``test_template_service`` can use it without an import cycle.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from math import ceil

from app.models import MockExam

ASSESSMENT_TYPES = ("all", "generic_test", "peer_review")
SORT_VALUES = ("newest", "oldest")
ARCHIVED_VALUES = ("hide", "show", "only")
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MIN_PAGE_SIZE = 10


@dataclass(frozen=True)
class ListFilters:
    search: str | None = None
    assessment_type: str = "all"
    # ``date`` is the single exact 実施日/test-date filter. ``date_from``/``date_to`` are the
    # legacy date-range params, kept so old bookmarked URLs still work; when ``date`` is set it
    # takes precedence and the range is ignored.
    date: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    sort: str = "newest"
    archived: str = "hide"
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE


@dataclass(frozen=True)
class PaginatedResult:
    items: list[MockExam]
    total: int
    page: int
    page_size: int
    total_pages: int
    has_prev: bool
    has_next: bool
    prev_page: int | None
    next_page: int | None
    start_index: int
    end_index: int


def _valid_date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def parse_list_filters(params: Mapping[str, str]) -> ListFilters:
    """Build :class:`ListFilters` from request query params, normalising bad values."""
    assessment_type = (params.get("type") or "all").strip()
    if assessment_type not in ASSESSMENT_TYPES:
        assessment_type = "all"
    sort = (params.get("sort") or "newest").strip()
    if sort not in SORT_VALUES:
        sort = "newest"
    archived = (params.get("archived") or "hide").strip()
    if archived not in ARCHIVED_VALUES:
        archived = "hide"
    search = (params.get("search") or "").strip() or None
    page = _positive_int(params.get("page"), 1)
    page_size = _positive_int(params.get("page_size"), DEFAULT_PAGE_SIZE)
    if page_size < MIN_PAGE_SIZE:
        page_size = MIN_PAGE_SIZE
    if page_size > MAX_PAGE_SIZE:
        page_size = MAX_PAGE_SIZE
    return ListFilters(
        search=search,
        assessment_type=assessment_type,
        date=_valid_date(params.get("date")),
        date_from=_valid_date(params.get("date_from")),
        date_to=_valid_date(params.get("date_to")),
        sort=sort,
        archived=archived,
        page=page,
        page_size=page_size,
    )


def _positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def filter_context(filters: ListFilters) -> dict:
    """Selected filter values for the reusable list-filters template partial."""
    has_active = (
        bool(filters.search)
        or filters.assessment_type != "all"
        or bool(filters.date)
        or bool(filters.date_from)
        or bool(filters.date_to)
        or filters.sort != "newest"
        or filters.archived != "hide"
    )
    return {
        "search": filters.search or "",
        "type": filters.assessment_type,
        "date": filters.date or "",
        "date_from": filters.date_from or "",
        "date_to": filters.date_to or "",
        "sort": filters.sort,
        "archived": filters.archived,
        "page": filters.page,
        "page_size": filters.page_size,
        "has_active_filters": has_active,
    }


def is_archived(exam: MockExam) -> bool:
    """Archived templates use ``template_status``; sessions/runs use ``status``."""
    if exam.is_template:
        return exam.template_status == "archived"
    return exam.status == "archived"


def _effective_date(exam: MockExam) -> str | None:
    """ISO date used for date-range filtering.

    Sessions prefer ``exam_date`` (the day the test was sat) and fall back to
    ``created_at``; templates have no exam date, so they always use ``created_at``.
    """
    if not exam.is_template and exam.exam_date:
        return exam.exam_date
    if exam.created_at is not None:
        return exam.created_at.date().isoformat()
    return None


def apply_filters(exams: Iterable[MockExam], filters: ListFilters) -> list[MockExam]:
    """Filter and sort a list of ``MockExam`` rows (templates or sessions)."""
    search = filters.search.lower() if filters.search else None
    result: list[MockExam] = []
    for exam in exams:
        archived = is_archived(exam)
        if filters.archived == "hide" and archived:
            continue
        if filters.archived == "only" and not archived:
            continue
        if filters.assessment_type != "all" and exam.assessment_type != filters.assessment_type:
            continue
        if search and search not in (exam.name or "").lower():
            continue
        if filters.date:
            # Exact 実施日/test-date match. Sessions use exam_date (else created_at);
            # templates use created_at — see _effective_date.
            if _effective_date(exam) != filters.date:
                continue
        elif filters.date_from or filters.date_to:
            effective = _effective_date(exam)
            if effective is None:
                continue
            if filters.date_from and effective < filters.date_from:
                continue
            if filters.date_to and effective > filters.date_to:
                continue
        result.append(exam)
    result.sort(
        key=lambda exam: (exam.created_at or datetime.min, exam.id),
        reverse=filters.sort != "oldest",
    )
    return result


def paginate_items(items: Sequence[MockExam], filters: ListFilters) -> PaginatedResult:
    total = len(items)
    total_pages = max(1, ceil(total / filters.page_size))
    page = min(max(filters.page, 1), total_pages)
    start = (page - 1) * filters.page_size
    end = start + filters.page_size
    page_items = list(items[start:end])
    return PaginatedResult(
        items=page_items,
        total=total,
        page=page,
        page_size=filters.page_size,
        total_pages=total_pages,
        has_prev=page > 1,
        has_next=page < total_pages,
        prev_page=page - 1 if page > 1 else None,
        next_page=page + 1 if page < total_pages else None,
        start_index=start + 1 if page_items else 0,
        end_index=start + len(page_items),
    )


def apply_filters_and_paginate(
    exams: Iterable[MockExam], filters: ListFilters
) -> PaginatedResult:
    return paginate_items(apply_filters(exams, filters), filters)


def month_bounds(date_iso: str) -> tuple[str, str]:
    """First and last ISO day of the month containing ``date_iso`` (``YYYY-MM-DD``)."""
    first = datetime.strptime(date_iso, "%Y-%m-%d").date().replace(day=1)
    next_first = (
        first.replace(year=first.year + 1, month=1)
        if first.month == 12
        else first.replace(month=first.month + 1)
    )
    return first.isoformat(), (next_first - timedelta(days=1)).isoformat()


def month_fallback_filters(filters: ListFilters) -> ListFilters:
    """Widen an exact ``date`` filter to its whole month, keeping every other filter.

    Used to suggest "other items in the same month" when an exact 実施日 has no hits. Type,
    search, archived, and sort are preserved; only the exact day becomes a month range.
    """
    if not filters.date:
        return filters
    start, end = month_bounds(filters.date)
    return replace(filters, date=None, date_from=start, date_to=end)
