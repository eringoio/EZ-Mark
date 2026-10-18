"""List filtering: sort, type, search, date range, and archived visibility."""

from datetime import datetime

from fastapi.testclient import TestClient

from app.database import SessionLocal, init_database
from app.main import create_app
from app.models import MockExam
from app.services.list_filter_service import (
    ListFilters,
    apply_filters,
    filter_context,
    month_bounds,
    month_fallback_filters,
    parse_list_filters,
)


def _exam(name, *, created, **kw):
    defaults = dict(assessment_type="generic_test", is_template=False, status="draft")
    defaults.update(kw)
    return MockExam(name=name, created_at=datetime.fromisoformat(created), **defaults)


def test_default_sort_is_newest_first() -> None:
    old = _exam("Old", created="2026-01-01")
    mid = _exam("Mid", created="2026-02-01")
    new = _exam("New", created="2026-03-01")
    result = apply_filters([old, new, mid], ListFilters())
    assert [e.name for e in result] == ["New", "Mid", "Old"]


def test_oldest_sort_reverses() -> None:
    old = _exam("Old", created="2026-01-01")
    new = _exam("New", created="2026-03-01")
    result = apply_filters([new, old], ListFilters(sort="oldest"))
    assert [e.name for e in result] == ["Old", "New"]


def test_type_filter() -> None:
    test_run = _exam("J", created="2026-01-01", assessment_type="generic_test")
    peer = _exam("P", created="2026-01-02", assessment_type="peer_review")
    result = apply_filters([test_run, peer], ListFilters(assessment_type="peer_review"))
    assert [e.name for e in result] == ["P"]


def test_partial_search_is_case_insensitive() -> None:
    a = _exam("Spring Midterm", created="2026-01-01")
    b = _exam("Autumn Final", created="2026-01-02")
    result = apply_filters([a, b], ListFilters(search="midterm"))
    assert [e.name for e in result] == ["Spring Midterm"]


def test_date_range_uses_exam_date_for_sessions() -> None:
    march = _exam("March", created="2026-01-01", exam_date="2026-03-15")
    june = _exam("June", created="2026-01-01", exam_date="2026-06-15")
    result = apply_filters(
        [march, june], ListFilters(date_from="2026-03-01", date_to="2026-03-31")
    )
    assert [e.name for e in result] == ["March"]


def test_exact_date_filter_matches_only_that_day() -> None:
    d15 = _exam("D15", created="2026-01-01", exam_date="2026-03-15")
    d16 = _exam("D16", created="2026-01-01", exam_date="2026-03-16")
    result = apply_filters([d15, d16], ListFilters(date="2026-03-15"))
    assert [e.name for e in result] == ["D15"]


def test_exact_date_takes_precedence_over_legacy_range() -> None:
    # A bookmarked URL could carry both; the exact date wins, the range is ignored.
    d15 = _exam("D15", created="2026-01-01", exam_date="2026-03-15")
    d20 = _exam("D20", created="2026-01-01", exam_date="2026-03-20")
    filters = ListFilters(date="2026-03-15", date_from="2026-03-01", date_to="2026-03-31")
    assert [e.name for e in apply_filters([d15, d20], filters)] == ["D15"]


def test_parse_keeps_legacy_date_range_and_exact_date() -> None:
    filters = parse_list_filters(
        {"date": "2026-03-15", "date_from": "2026-03-01", "date_to": "2026-03-31"}
    )
    assert filters.date == "2026-03-15"
    assert filters.date_from == "2026-03-01"
    assert filters.date_to == "2026-03-31"


def test_month_bounds_handles_december_rollover() -> None:
    assert month_bounds("2026-12-10") == ("2026-12-01", "2026-12-31")
    assert month_bounds("2026-02-15") == ("2026-02-01", "2026-02-28")


def test_month_fallback_widens_to_month_respecting_type_and_archived() -> None:
    same_month = _exam("SameMonth", created="2026-01-01", exam_date="2026-03-20")
    other_month = _exam("OtherMonth", created="2026-01-01", exam_date="2026-04-20")
    wrong_type = _exam(
        "WrongType", created="2026-01-01", exam_date="2026-03-05",
        assessment_type="peer_review",
    )
    archived = _exam(
        "Archived", created="2026-01-01", exam_date="2026-03-05", status="archived"
    )
    pool = [same_month, other_month, wrong_type, archived]
    filters = ListFilters(date="2026-03-15", assessment_type="generic_test")
    # No session sat on the 15th, so the exact list is empty...
    assert apply_filters(pool, filters) == []
    # ...and the fallback returns only the same-month, same-type, non-archived session.
    fallback = apply_filters(pool, month_fallback_filters(filters))
    assert [e.name for e in fallback] == ["SameMonth"]


def test_month_fallback_filters_noop_without_exact_date() -> None:
    filters = ListFilters(assessment_type="generic_test")
    assert month_fallback_filters(filters) is filters


def test_archived_hide_show_only_for_sessions() -> None:
    active = _exam("Active S", created="2026-01-01", status="results_ready")
    archived = _exam("Archived S", created="2026-01-02", status="archived")
    pool = [active, archived]
    assert [e.name for e in apply_filters(pool, ListFilters(archived="hide"))] == ["Active S"]
    names_show = {e.name for e in apply_filters(pool, ListFilters(archived="show"))}
    assert names_show == {"Active S", "Archived S"}
    assert [e.name for e in apply_filters(pool, ListFilters(archived="only"))] == ["Archived S"]


def test_archived_uses_template_status_for_templates() -> None:
    active = _exam("Active T", created="2026-01-01", is_template=True, template_status="active")
    archived = _exam(
        "Archived T", created="2026-01-02", is_template=True, template_status="archived"
    )
    pool = [active, archived]
    assert [e.name for e in apply_filters(pool, ListFilters(archived="hide"))] == ["Active T"]
    assert [e.name for e in apply_filters(pool, ListFilters(archived="only"))] == ["Archived T"]


def test_parse_list_filters_normalises_bad_values() -> None:
    filters = parse_list_filters(
        {"type": "bogus", "sort": "weird", "archived": "maybe", "search": "  hi  ",
         "date_from": "not-a-date"}
    )
    assert filters.assessment_type == "all"
    assert filters.sort == "newest"
    assert filters.archived == "hide"
    assert filters.search == "hi"
    assert filters.date_from is None


def test_filter_context_flags_active_filters() -> None:
    assert filter_context(ListFilters())["has_active_filters"] is False
    assert filter_context(ListFilters(search="x"))["has_active_filters"] is True


def test_exams_page_type_filter_narrows_results() -> None:
    init_database()
    with SessionLocal() as db:
        db.add_all(
            [
                MockExam(name="FilterRun", assessment_type="generic_test"),
                MockExam(name="FilterPeer", assessment_type="peer_review"),
            ]
        )
        db.commit()
    with TestClient(create_app()) as client:
        peer_only = client.get("/exams?type=peer_review").text
        assert "FilterPeer" in peer_only
        assert "FilterRun" not in peer_only


def test_filter_bar_renders_styled_not_native() -> None:
    init_database()
    with TestClient(create_app()) as client:
        html = client.get("/exams").text
    # The redesigned bar uses design-system classes and the single exact-date field, not the
    # old inline-styled native range inputs.
    assert 'class="filter-bar"' in html
    assert 'name="date"' in html
    assert 'class="list-filters"' not in html
    assert 'name="date_from"' not in html


def test_exact_date_no_hits_renders_month_fallback() -> None:
    init_database()
    with SessionLocal() as db:
        db.add(
            MockExam(
                name="MonthFallbackMate",
                assessment_type="generic_test",
                exam_date="2026-09-19",
            )
        )
        db.commit()
    with TestClient(create_app()) as client:
        # Exact match on the test date shows the session and no fallback notice.
        exact = client.get("/exams?date=2026-09-19").text
        assert "MonthFallbackMate" in exact
        assert "Other items in the same month" not in exact
        # A different day in the same month has no exact hit, so the page explains it and
        # surfaces the session under the same-month fallback heading.
        fallback = client.get("/exams?date=2026-09-17").text
        assert "No results found for this test date." in fallback
        assert "Other items in the same month" in fallback
        assert "MonthFallbackMate" in fallback


def test_long_japanese_title_renders_as_normal_link() -> None:
    init_database()
    long_title = "合格模試 サンプル 第1回 何度でも使える雛型テンプレート"
    with SessionLocal() as db:
        db.add(MockExam(name=long_title, assessment_type="generic_test", is_template=True))
        db.commit()
    with TestClient(create_app()) as client:
        html = client.get("/templates").text
    # The title renders inside a single horizontal summary-card-title link (CSS line-clamps
    # it); it must not be split into a vertical one-character-per-line column.
    assert 'class="summary-card-title" href="/templates/' in html
    assert long_title in html


def test_package_import_pages_render_upload_panel() -> None:
    init_database()
    with TestClient(create_app()) as client:
        template_pkg = client.get("/templates/import-package").text
        session_pkg = client.get("/sessions/import-package").text
    for html in (template_pkg, session_pkg):
        assert 'class="upload-panel"' in html
        assert "file-dropzone" in html
