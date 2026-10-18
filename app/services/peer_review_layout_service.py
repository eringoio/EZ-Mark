"""Peer-review marksheet geometry — the single source of truth.

Both PDF generation and scanning, plus the browser SVG preview, derive their layout
from here so they can never drift. The module also owns the automatic portrait vs
landscape decision (`calculate_peer_review_page_plan`): orientation is chosen once for
a whole session from the configuration (competency count, score scale, max students per
page, furigana), never per page, and the resolved dimensions are baked into each page's
saved coordinate map so scanning uses the exact layout the sheet was printed with.
"""

import unicodedata
from dataclasses import dataclass, field

# A4 page (portrait dimensions; landscape swaps them).
A4_SHORT_MM = 210.0
A4_LONG_MM = 297.0

# Corner markers match the normal OMR pipeline and ``alignment_service`` exactly:
# 6 mm solid squares whose outer edges sit 10 mm from the near page edges; the far
# markers' top-left corners are inset 16 mm so their outer edges are also 10 mm in.
MARKER_SIZE_MM = 6.0
MARKER_NEAR_INSET_MM = 10.0
MARKER_FAR_INSET_MM = 16.0
# Reserved clear band to the right of (and below) the top/bottom-left detection markers
# so header text and the instruction line never crowd a marker the scanner relies on.
MARKER_SAFE_MARGIN_MM = 6.0
# Left edge for free-flowing header/footer text (title, date line, instruction): clear
# of the top-left marker by ``MARKER_SAFE_MARGIN_MM``. The rating table itself starts at
# ``LEFT_MARGIN_MM`` (below the marker band) and is unaffected.
CONTENT_SAFE_LEFT_MM = MARKER_NEAR_INSET_MM + MARKER_SIZE_MM + MARKER_SAFE_MARGIN_MM

# Table margins stay inside the corner markers so grid lines never touch them.
LEFT_MARGIN_MM = 18.0
RIGHT_MARGIN_MM = 18.0
TARGET_COLUMN_X_MM = 18.0
# Student/target name column: width adapts to the longest visible name (see
# ``_target_column_width_mm``), clamped to this range. ``TARGET_COLUMN_WIDTH_MM`` is the
# fallback used when no name samples are available (e.g. a template with no roster).
TARGET_COLUMN_WIDTH_MM = 44.0
TARGET_COLUMN_MIN_MM = 22.0
TARGET_COLUMN_MAX_MM = 45.0
TARGET_COLUMN_PADDING_MM = 5.0
BUBBLE_RADIUS_MM = 1.8
MIN_SCORE_GAP_MM = 4.8  # minimum centre-to-centre spacing for reliable scanning
SCORE_COLUMN_PADDING_MM = 8.0
MIN_COMPETENCY_HEADER_WIDTH_MM = 18.0
# Compact, fixed row height so bubbles sit centred with clear separators above/below.
ROW_HEIGHT_MM = 8.0

# Vertical reservations (mm from the top, plus a bottom band for the instruction line).
# Kept tight so a landscape A4 fits 20 rows (5 criteria, scale 1-5) without crowding.
HEADER_TOP_MM = 29.0
HEADER_BAND_PLAIN_MM = 9.0
# Furigana/reading band at standard density. Sized so a landscape A4 still fits 20 full
# 8 mm rows *with* furigana: header_top(29) + band(9) + bottom_reserve(12) = 50 mm, leaving
# 160 mm = 20 rows. The reading is placed near the band top and the title dropped within the
# same band height as the plain case, so the furigana row no longer costs a student.
HEADER_BAND_FURIGANA_MM = 9.0
BOTTOM_RESERVE_MM = 12.0

# QR sits in the top-right corner, above the rating table. Kept slightly smaller than the
# detection markers' band so there is clear white space between the QR and the header/table
# (the table top is at HEADER_TOP_MM = 29 mm, giving a ~6 mm clear gap below an 18 mm QR).
QR_SIZE_MM = 18.0
QR_TOP_MM = 5.0
QR_RIGHT_GAP_MM = 20.0  # gap from the QR's right edge to the page's right edge

DEFAULT_STUDENTS_PER_PAGE = 15
VALID_ORIENTATION_MODES = ("auto", "portrait", "landscape")


# --- Layout density -----------------------------------------------------------
# Three teacher-selectable densities. ``standard`` keeps the historical plain geometry and
# full 8 mm rows; its furigana band is sized so 20 rows still fit on landscape A4 even with
# readings (so furigana no longer costs a student). ``compact`` tightens the header and rows
# to fit even more students per page without shrinking the answer bubbles (which stay at
# ``BUBBLE_RADIUS_MM``); ``relaxed`` adds breathing room.
VALID_DENSITY_MODES = ("standard", "compact", "relaxed")
DEFAULT_DENSITY_MODE = "standard"


@dataclass(frozen=True)
class DensityProfile:
    row_height_mm: float
    header_top_mm: float
    header_band_plain_mm: float
    header_band_furigana_mm: float
    bottom_reserve_mm: float


DENSITY_PROFILES = {
    "standard": DensityProfile(
        row_height_mm=ROW_HEIGHT_MM,
        header_top_mm=HEADER_TOP_MM,
        header_band_plain_mm=HEADER_BAND_PLAIN_MM,
        header_band_furigana_mm=HEADER_BAND_FURIGANA_MM,
        bottom_reserve_mm=BOTTOM_RESERVE_MM,
    ),
    "compact": DensityProfile(
        row_height_mm=6.8,
        header_top_mm=24.0,
        header_band_plain_mm=7.5,
        header_band_furigana_mm=11.0,
        bottom_reserve_mm=9.0,
    ),
    "relaxed": DensityProfile(
        row_height_mm=9.5,
        header_top_mm=32.0,
        header_band_plain_mm=11.0,
        header_band_furigana_mm=15.0,
        bottom_reserve_mm=14.0,
    ),
}


def profile_name(density: str | None) -> str:
    return density if density in VALID_DENSITY_MODES else DEFAULT_DENSITY_MODE


def density_profile(density: str | None) -> DensityProfile:
    return DENSITY_PROFILES[profile_name(density)]


# --- Text fitting (deterministic, font-metric-free) ---------------------------
# Both the ReportLab PDF and the browser SVG preview must wrap/shrink identically, so the
# fit decision (line breaks + font size) is computed here once with a deterministic width
# estimate rather than each renderer's own font metrics. The estimate treats East-Asian
# wide/ambiguous glyphs as 1 em and everything else as ~0.5 em.
PT_TO_MM = 25.4 / 72
COMPETENCY_FONT_PT = 8.0  # normal competency-title size
COMPETENCY_FONT_MIN_PT = 6.0  # smallest size we will shrink a title to
COMPETENCY_TWO_LINE_PT = 6.5  # cap when wrapping to two lines so both fit the header band
ROW_LABEL_FONT_PT = 7.0  # student/target name size
HEADER_TEXT_PADDING_MM = 2.5  # horizontal breathing room inside a competency column


def _char_width_factor(char: str) -> float:
    return 1.0 if unicodedata.east_asian_width(char) in ("W", "F", "A") else 0.5


def text_width_mm(text: str, font_pt: float) -> float:
    """Deterministic estimated rendered width of ``text`` at ``font_pt`` (in mm)."""
    return sum(_char_width_factor(char) for char in text) * font_pt * PT_TO_MM


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _wrap_into_two_lines(text: str, avail_mm: float, font_pt: float) -> list[str]:
    """Greedy fill of the first line up to ``avail_mm``; remainder on the second line.

    Japanese wraps per character, so no word boundaries are needed. Returns one line when
    the whole string fits, otherwise two lines (the second may still overflow — callers
    shrink the font or flag the item).
    """
    first = ""
    for char in text:
        if first and text_width_mm(first + char, font_pt) > avail_mm:
            break
        first += char
    second = text[len(first) :]
    return [first, second] if second else [first]


@dataclass(frozen=True)
class CompetencyHeaderFit:
    lines: tuple[str, ...]
    font_pt: float
    too_long: bool


def fit_competency_header(name: str, column_width_mm: float) -> CompetencyHeaderFit:
    """Decide how a competency title is drawn so it never overflows its column.

    Priority: (1) one line at the normal size; (2) two lines at a slightly smaller size
    that still fits the header band height; (3) two lines at the minimum size, flagged
    ``too_long`` so the builder can warn. The header band height is never grown, so this
    never costs students-per-page.
    """
    name = name or ""
    avail = max(1.0, column_width_mm - HEADER_TEXT_PADDING_MM)
    if text_width_mm(name, COMPETENCY_FONT_PT) <= avail:
        return CompetencyHeaderFit((name,), COMPETENCY_FONT_PT, False)
    font_pt = COMPETENCY_TWO_LINE_PT
    while font_pt >= COMPETENCY_FONT_MIN_PT:
        lines = _wrap_into_two_lines(name, avail, font_pt)
        if len(lines) <= 2 and all(text_width_mm(line, font_pt) <= avail for line in lines):
            return CompetencyHeaderFit(tuple(lines), font_pt, False)
        font_pt -= 0.5
    lines = _wrap_into_two_lines(name, avail, COMPETENCY_FONT_MIN_PT)
    return CompetencyHeaderFit(tuple(lines[:2]), COMPETENCY_FONT_MIN_PT, True)


# --- Per-segment ruby furigana ------------------------------------------------
# Ruby vertical lift above a header line's baseline, expressed as a multiple of the line
# font size (so a shrunk title keeps its reading proportionally close). Kept small so the
# readings stay inside the (taller, furigana) header band.
RUBY_RISE_FACTOR = 1.0
RUBY_FONT_PT = 5.0  # printed ruby size (matches the legacy whole-title furigana)


def competency_ruby_segments(competency) -> list[dict]:
    """Validated per-segment ruby entries for a competency: ``[{base_text, reading}, ...]``.

    Tolerates missing/legacy data (returns ``[]``) and drops entries missing either field.
    """
    raw = getattr(competency, "name_ruby_json", None) or []
    segments: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        base = (entry.get("base_text") or "").strip()
        reading = (entry.get("reading") or "").strip()
        if base and reading:
            segments.append({"base_text": base, "reading": reading})
    return segments


def _ruby_positions(
    printed_title: str,
    segments: list[dict],
    lines: tuple[str, ...],
    font_pt: float,
    center_x_mm: float,
    name_y_mm: float,
) -> tuple[dict, ...]:
    """Resolve each ruby reading to an absolute position centred over its base run.

    Base runs are located in ``printed_title`` by sequential search (so repeated text and
    edits behave predictably), then mapped onto the wrapped ``lines`` to find which header
    line the run sits on and its horizontal centre. ``name_y_mm`` is the baseline of a
    single line; wrapped lines are offset by the same line gap the renderers use.
    """
    if not segments or not printed_title:
        return ()
    line_starts: list[int] = []
    cursor = 0
    for line in lines:
        line_starts.append(cursor)
        cursor += len(line)
    count = len(lines)
    line_gap_mm = font_pt * PT_TO_MM * 1.05
    rise_mm = font_pt * PT_TO_MM * RUBY_RISE_FACTOR
    positions: list[dict] = []
    search_from = 0
    for segment in segments:
        base = segment["base_text"]
        index = printed_title.find(base, search_from)
        if index < 0:
            index = printed_title.find(base)
        if index < 0:
            continue  # base run not present in the printed title; skip gracefully
        start, end = index, index + len(base)
        search_from = end
        # Which wrapped line does the run start on? Clip a run that crosses the wrap to the
        # line it starts on (a base run is normally short and stays on one line).
        line_index = 0
        for li in range(count):
            line_end = line_starts[li] + len(lines[li])
            if start < line_end:
                line_index = li
                break
        else:
            line_index = count - 1
        line = lines[line_index]
        local_start = max(0, start - line_starts[line_index])
        local_end = min(len(line), end - line_starts[line_index])
        if local_end <= local_start:
            continue
        line_width = text_width_mm(line, font_pt)
        line_left = center_x_mm - line_width / 2
        run_left = line_left + text_width_mm(line[:local_start], font_pt)
        run_width = text_width_mm(line[local_start:local_end], font_pt)
        x_mm = run_left + run_width / 2
        line_center_y = name_y_mm + (line_index - (count - 1) / 2) * line_gap_mm
        positions.append(
            {
                "reading": segment["reading"],
                "x_mm": round(x_mm, 3),
                "y_mm": round(line_center_y - rise_mm, 3),
                "line_index": line_index,
            }
        )
    return tuple(positions)


def _target_column_width_mm(label_samples: list[str] | None) -> float:
    """Width for the target/name column: longest visible name + padding, clamped.

    Falls back to ``TARGET_COLUMN_WIDTH_MM`` when there are no samples (a template with no
    roster), so template previews keep their familiar width.
    """
    samples = [s for s in (label_samples or []) if s]
    if not samples:
        return TARGET_COLUMN_WIDTH_MM
    widest = max(text_width_mm(sample, ROW_LABEL_FONT_PT) for sample in samples)
    return _clamp(widest + TARGET_COLUMN_PADDING_MM, TARGET_COLUMN_MIN_MM, TARGET_COLUMN_MAX_MM)


@dataclass(frozen=True)
class PeerReviewPagePlan:
    """Resolved page geometry decision for a whole peer-review session."""

    orientation: str  # "portrait" | "landscape"
    page_width_mm: float
    page_height_mm: float
    students_per_page: int  # rows that actually fit (<= configured maximum)
    max_students_per_page: int
    has_furigana: bool
    header_band_height_mm: float
    table_top_mm: float
    fits: bool
    density: str = DEFAULT_DENSITY_MODE
    row_height_mm: float = ROW_HEIGHT_MM
    header_top_mm: float = HEADER_TOP_MM
    bottom_reserve_mm: float = BOTTOM_RESERVE_MM
    target_column_width_mm: float = TARGET_COLUMN_WIDTH_MM
    layout_warnings: list = field(default_factory=list)
    # "What if" capacities at the resolved orientation, used by the builder to explain why
    # the chosen rows-per-page is what it is and how to raise it (hide furigana / go
    # compact). Capped at ``max_students_per_page`` like ``students_per_page``.
    students_per_page_no_furigana: int = 0
    students_per_page_compact: int = 0

    def as_map(self) -> dict:
        return {
            "orientation": self.orientation,
            "page_width_mm": self.page_width_mm,
            "page_height_mm": self.page_height_mm,
            "students_per_page": self.students_per_page,
            "max_students_per_page": self.max_students_per_page,
            "has_furigana": self.has_furigana,
            "density": self.density,
            "fits": self.fits,
            "students_per_page_no_furigana": self.students_per_page_no_furigana,
            "students_per_page_compact": self.students_per_page_compact,
            "layout_warnings": list(self.layout_warnings),
        }


@dataclass(frozen=True)
class PeerReviewBubblePosition:
    target_id: int
    target_student_id: int
    competency_id: int
    score: int
    x_mm: float
    y_mm: float
    radius_mm: float

    def as_map(self) -> dict:
        return {
            "target_id": self.target_id,
            "target_student_id": self.target_student_id,
            "competency_id": self.competency_id,
            "score": self.score,
            "x_mm": self.x_mm,
            "y_mm": self.y_mm,
            "radius_mm": self.radius_mm,
        }


@dataclass(frozen=True)
class PeerReviewCompetencyColumn:
    competency_id: int
    name: str
    display_order: int
    x_mm: float
    width_mm: float
    name_furigana: str = ""
    score_positions: tuple = ()
    # Auto-fit result: the printed title wrapped to 1-2 lines and the font size that keeps
    # it inside the column. ``header_too_long`` flags a title that overflows even at the
    # minimum size so the builder can warn.
    header_lines: tuple = ()
    header_font_pt: float = COMPETENCY_FONT_PT
    header_too_long: bool = False
    # Per-segment ruby furigana already resolved to absolute paper coordinates: each entry
    # is ``{"reading", "x_mm", "y_mm", "line_index"}`` centred over its base run in the
    # printed (possibly wrapped/shrunk) title. Empty when the column uses the whole-title
    # ``name_furigana`` fallback or has no reading at all.
    ruby_positions: tuple = ()

    @property
    def right_mm(self) -> float:
        return self.x_mm + self.width_mm

    @property
    def center_x_mm(self) -> float:
        return self.x_mm + self.width_mm / 2

    def as_map(self) -> dict:
        return {
            "competency_id": self.competency_id,
            "name": self.name,
            "name_furigana": self.name_furigana,
            "display_order": self.display_order,
            "x_mm": self.x_mm,
            "width_mm": self.width_mm,
            "center_x_mm": self.center_x_mm,
            "header_lines": list(self.header_lines) or [self.name],
            "header_font_pt": self.header_font_pt,
            "header_too_long": self.header_too_long,
            "ruby_positions": [dict(r) for r in self.ruby_positions],
            "score_positions": [
                {"score": score, "x_mm": x_mm} for score, x_mm in self.score_positions
            ],
        }


@dataclass(frozen=True)
class PeerReviewTargetRow:
    target_id: int
    student_id: int
    display_label: str
    display_order: int
    # Explicit row geometry (mm from top) shared by PDF, SVG preview and scanner so
    # nothing re-derives row math browser-side and drifts from the printed sheet.
    row_top_mm: float
    row_center_y_mm: float
    row_bottom_mm: float
    separator_y_mm: float

    @property
    def y_mm(self) -> float:
        """Bubble/label baseline — always the vertical centre of the row."""
        return self.row_center_y_mm

    def as_map(self) -> dict:
        return {
            "target_id": self.target_id,
            "student_id": self.student_id,
            "display_label": self.display_label,
            "display_order": self.display_order,
            "y_mm": self.row_center_y_mm,
            "row_top_mm": self.row_top_mm,
            "row_center_y_mm": self.row_center_y_mm,
            "row_bottom_mm": self.row_bottom_mm,
            "separator_y_mm": self.separator_y_mm,
        }


@dataclass(frozen=True)
class PeerReviewPageLayout:
    page_width_mm: float
    page_height_mm: float
    orientation: str
    score_values: list[int]
    target_rows: list[PeerReviewTargetRow]
    competency_columns: list[PeerReviewCompetencyColumn]
    rating_bubbles: list[PeerReviewBubblePosition]
    marker_positions: dict
    qr: dict
    # Visual table geometry shared by the PDF, the SVG preview, and the coordinate map.
    table_left_mm: float
    table_right_mm: float
    header_top_mm: float
    table_top_mm: float
    table_bottom_mm: float
    row_height_mm: float
    name_y_mm: float
    furigana_y_mm: float
    content_left_mm: float
    column_separators_mm: list[float]
    target_column: dict
    layout_warnings: list = field(default_factory=list)


def _orientation_dims(orientation: str) -> tuple[float, float]:
    if orientation == "landscape":
        return A4_LONG_MM, A4_SHORT_MM
    return A4_SHORT_MM, A4_LONG_MM


def min_competency_width_mm(score_count: int) -> float:
    """Smallest competency column width that keeps bubbles readable/scannable."""
    score_count = max(1, score_count)
    bubbles_width = (
        (score_count - 1) * MIN_SCORE_GAP_MM + 2 * BUBBLE_RADIUS_MM + SCORE_COLUMN_PADDING_MM
    )
    return max(MIN_COMPETENCY_HEADER_WIDTH_MM, bubbles_width)


def _row_capacity(page_height_mm: float, table_top_mm: float, profile: DensityProfile) -> int:
    usable = page_height_mm - profile.bottom_reserve_mm - table_top_mm
    return max(0, int(usable // profile.row_height_mm))


def calculate_peer_review_page_plan(
    competency_count: int,
    score_min: int,
    score_max: int,
    max_students_per_page: int,
    has_furigana: bool,
    target_label_samples: list[str] | None = None,
    orientation_mode: str = "auto",
    density: str = DEFAULT_DENSITY_MODE,
) -> PeerReviewPagePlan:
    """Decide portrait vs landscape and how many rows actually fit per page.

    The physical sheet size is never shrunk to fit (OMR scanning needs stable
    dimensions); instead the number of rows per page is reduced and/or the orientation
    is switched. ``density`` picks the vertical spacing profile (``standard`` reproduces
    the historical geometry); the target/name column width adapts to the supplied name
    samples. Returns the resolved plan plus structured ``layout_warnings`` (codes the UI
    localizes) such as ``per_page_reduced`` and ``does_not_fit``.
    """
    if orientation_mode not in VALID_ORIENTATION_MODES:
        orientation_mode = "auto"
    profile = density_profile(density)
    competency_count = max(1, competency_count)
    score_count = max(1, score_max - score_min + 1)
    min_comp = min_competency_width_mm(score_count)
    max_spp = max(1, max_students_per_page or 1)
    band = profile.header_band_furigana_mm if has_furigana else profile.header_band_plain_mm
    table_top = profile.header_top_mm + band
    target_col = _target_column_width_mm(target_label_samples)

    def evaluate(orientation: str) -> dict:
        width_mm, height_mm = _orientation_dims(orientation)
        usable_width = width_mm - LEFT_MARGIN_MM - RIGHT_MARGIN_MM - target_col
        width_ok = competency_count * min_comp <= usable_width + 0.01
        capacity = _row_capacity(height_mm, table_top, profile)
        return {
            "orientation": orientation,
            "width_mm": width_mm,
            "height_mm": height_mm,
            "width_ok": width_ok,
            "capacity": capacity,
        }

    if orientation_mode == "portrait":
        candidates = [evaluate("portrait")]
    elif orientation_mode == "landscape":
        candidates = [evaluate("landscape")]
    else:
        candidates = [evaluate("portrait"), evaluate("landscape")]

    warnings: list[dict] = []
    fitting = [c for c in candidates if c["width_ok"] and c["capacity"] >= 1]
    if fitting:
        # Prefer the orientation that fits the most rows; tie -> portrait (easier to
        # print and scan).
        best = max(
            fitting,
            key=lambda c: (min(max_spp, c["capacity"]), c["orientation"] == "portrait"),
        )
        students_per_page = min(max_spp, best["capacity"])
        fits = True
        if students_per_page < max_spp:
            warnings.append(
                {"code": "per_page_reduced", "max": max_spp, "actual": students_per_page}
            )
    else:
        # Nothing fits cleanly: keep a stable sheet, show the best effort, and warn loudly.
        best = max(candidates, key=lambda c: (c["width_ok"], c["capacity"]))
        students_per_page = min(max_spp, best["capacity"]) if best["capacity"] >= 1 else 1
        students_per_page = max(1, students_per_page)
        fits = False
        warnings.append({"code": "does_not_fit"})

    # "What if" capacities at the resolved orientation so the builder can explain the
    # rows-per-page outcome and offer concrete fixes (hide furigana / switch to compact).
    best_height = best["height_mm"]
    plain_table_top = profile.header_top_mm + profile.header_band_plain_mm
    spp_no_furigana = min(max_spp, _row_capacity(best_height, plain_table_top, profile))
    compact = density_profile("compact")
    compact_band = compact.header_band_furigana_mm if has_furigana else compact.header_band_plain_mm
    compact_table_top = compact.header_top_mm + compact_band
    spp_compact = min(max_spp, _row_capacity(best_height, compact_table_top, compact))

    return PeerReviewPagePlan(
        orientation=best["orientation"],
        page_width_mm=best["width_mm"],
        page_height_mm=best["height_mm"],
        students_per_page=students_per_page,
        max_students_per_page=max_spp,
        has_furigana=has_furigana,
        header_band_height_mm=band,
        table_top_mm=table_top,
        fits=fits,
        density=profile_name(density),
        row_height_mm=profile.row_height_mm,
        header_top_mm=profile.header_top_mm,
        bottom_reserve_mm=profile.bottom_reserve_mm,
        target_column_width_mm=target_col,
        layout_warnings=warnings,
        students_per_page_no_furigana=spp_no_furigana,
        students_per_page_compact=spp_compact,
    )


def _competency_has_reading(competency) -> bool:
    """True when a competency carries any reading: a whole-title furigana or per-segment ruby."""
    if (getattr(competency, "name_furigana", "") or "").strip():
        return True
    return bool(competency_ruby_segments(competency))


def _has_furigana(competencies: list) -> bool:
    # The taller (furigana) header band is reserved whenever any competency has a reading,
    # whether a whole-title furigana or per-segment ruby, so the readings have room above.
    return any(_competency_has_reading(comp) for comp in competencies)


def _form_target_label_samples(form) -> list[str]:
    """Active target labels for sizing the name column (empty for template forms)."""
    return [
        target.display_label
        for target in getattr(form, "targets", []) or []
        if getattr(target, "active", True)
    ]


def page_plan_for_form(
    form, competencies: list, target_label_samples: list[str] | None = None
) -> PeerReviewPagePlan:
    if target_label_samples is None:
        target_label_samples = _form_target_label_samples(form)
    return calculate_peer_review_page_plan(
        competency_count=len(competencies),
        score_min=form.score_min,
        score_max=form.score_max,
        max_students_per_page=getattr(form, "students_per_page", None) or DEFAULT_STUDENTS_PER_PAGE,
        has_furigana=_has_furigana(competencies),
        target_label_samples=target_label_samples,
        orientation_mode=getattr(form, "page_orientation_mode", "auto") or "auto",
        density=getattr(form, "layout_density", None) or DEFAULT_DENSITY_MODE,
    )


def calculate_peer_review_layout(
    form, page, competencies: list, plan: PeerReviewPagePlan | None = None
) -> PeerReviewPageLayout:
    if plan is None:
        samples = [row.get("display_label", "") for row in page.target_rows_json]
        plan = page_plan_for_form(form, competencies, samples)

    page_width_mm = plan.page_width_mm
    page_height_mm = plan.page_height_mm
    table_top_mm = plan.table_top_mm
    header_top_mm = plan.header_top_mm
    row_height_mm = plan.row_height_mm
    band_mm = plan.header_band_height_mm
    target_col_width_mm = plan.target_column_width_mm
    table_left_mm = LEFT_MARGIN_MM
    table_right_mm = page_width_mm - RIGHT_MARGIN_MM

    # Header band labels are placed proportionally inside the band (which varies by
    # density) so they always sit just above the first row separator without crowding the
    # band top. The score legend row is intentionally gone — every bubble already carries
    # its own number, so the band only holds the (optional) furigana reading and the title.
    if plan.has_furigana:
        furigana_y_mm = header_top_mm + band_mm * 0.40
        name_y_mm = header_top_mm + band_mm * 0.80
    else:
        furigana_y_mm = header_top_mm + band_mm * 0.45  # unused without furigana
        name_y_mm = header_top_mm + band_mm * 0.68

    score_values = list(range(form.score_min, form.score_max + 1))
    target_rows = []
    for index, target in enumerate(page.target_rows_json):
        row_top = table_top_mm + index * row_height_mm
        target_rows.append(
            PeerReviewTargetRow(
                target_id=target["target_id"],
                student_id=target["student_id"],
                display_label=target["display_label"],
                display_order=target["display_order"],
                row_top_mm=row_top,
                row_center_y_mm=row_top + row_height_mm / 2,
                row_bottom_mm=row_top + row_height_mm,
                separator_y_mm=row_top + row_height_mm,
            )
        )
    competency_columns = _competency_columns(
        competencies, score_values, page_width_mm, target_col_width_mm, name_y_mm
    )
    bubbles = []
    for row in target_rows:
        for column in competency_columns:
            for score, x_mm in column.score_positions:
                bubbles.append(
                    PeerReviewBubblePosition(
                        target_id=row.target_id,
                        target_student_id=row.student_id,
                        competency_id=column.competency_id,
                        score=score,
                        x_mm=x_mm,
                        y_mm=row.row_center_y_mm,
                        radius_mm=BUBBLE_RADIUS_MM,
                    )
                )
    table_bottom_mm = target_rows[-1].row_bottom_mm if target_rows else table_top_mm
    separators = [table_left_mm, TARGET_COLUMN_X_MM + target_col_width_mm]
    separators.extend(column.right_mm for column in competency_columns)
    far_marker_x = page_width_mm - MARKER_FAR_INSET_MM
    far_marker_y = page_height_mm - MARKER_FAR_INSET_MM
    return PeerReviewPageLayout(
        page_width_mm=page_width_mm,
        page_height_mm=page_height_mm,
        orientation=plan.orientation,
        score_values=score_values,
        target_rows=target_rows,
        competency_columns=competency_columns,
        rating_bubbles=bubbles,
        marker_positions={
            "top_left": {"x_mm": MARKER_NEAR_INSET_MM, "y_mm": MARKER_NEAR_INSET_MM},
            "top_right": {"x_mm": far_marker_x, "y_mm": MARKER_NEAR_INSET_MM},
            "bottom_left": {"x_mm": MARKER_NEAR_INSET_MM, "y_mm": far_marker_y},
            "bottom_right": {"x_mm": far_marker_x, "y_mm": far_marker_y},
        },
        qr={
            "x_mm": page_width_mm - QR_SIZE_MM - QR_RIGHT_GAP_MM,
            "y_mm": QR_TOP_MM,
            "size_mm": QR_SIZE_MM,
        },
        table_left_mm=table_left_mm,
        table_right_mm=table_right_mm,
        header_top_mm=header_top_mm,
        table_top_mm=table_top_mm,
        table_bottom_mm=table_bottom_mm,
        row_height_mm=row_height_mm,
        name_y_mm=name_y_mm,
        furigana_y_mm=furigana_y_mm,
        content_left_mm=CONTENT_SAFE_LEFT_MM,
        column_separators_mm=sorted({round(x, 3) for x in separators}),
        target_column={"x_mm": TARGET_COLUMN_X_MM, "width_mm": target_col_width_mm},
        layout_warnings=list(plan.layout_warnings),
    )


def coordinate_map_from_layout(form, page, layout: PeerReviewPageLayout) -> dict:
    return {
        "type": "anonymous_peer_review",
        "coordinate_units": "mm_top_left",
        "orientation": layout.orientation,
        "page_width_mm": layout.page_width_mm,
        "page_height_mm": layout.page_height_mm,
        "form_id": form.id,
        "page_id": page.id,
        "page_no": page.page_no,
        "qr_token": page.qr_token,
        "score_min": form.score_min,
        "score_max": form.score_max,
        "row_height_mm": layout.row_height_mm,
        "target_rows": [row.as_map() for row in layout.target_rows],
        "competency_columns": [column.as_map() for column in layout.competency_columns],
        "rating_bubbles": [bubble.as_map() for bubble in layout.rating_bubbles],
        "marker_positions": layout.marker_positions,
        "qr": layout.qr,
    }


def _competency_columns(
    competencies: list,
    score_values: list[int],
    page_width_mm: float,
    target_col_width_mm: float = TARGET_COLUMN_WIDTH_MM,
    name_y_mm: float = 0.0,
) -> list[PeerReviewCompetencyColumn]:
    start_x = TARGET_COLUMN_X_MM + target_col_width_mm
    available_width = page_width_mm - start_x - RIGHT_MARGIN_MM
    count = max(1, len(competencies))
    width = available_width / count
    columns = []
    for index, competency in enumerate(competencies):
        x_mm = start_x + index * width
        score_positions = tuple(
            (score, _score_x_mm(x_mm, width, score_index, len(score_values)))
            for score_index, score in enumerate(score_values)
        )
        name = competency.name or ""
        # The printed header uses the optional short label when set, falling back to the
        # full competency name. The builder keeps editing/storing the full ``name``; only
        # the sheet header (and the furigana above it) is shortened.
        short_label = (getattr(competency, "short_label", "") or "").strip()
        printed_title = short_label or name
        # Auto-fit wraps/shrinks the title so it never overflows the width-resolved column.
        fit = fit_competency_header(printed_title, width)
        center_x = x_mm + width / 2
        # Per-segment ruby attaches to runs of the full ``name``; it is only drawn when the
        # printed title *is* the name (no short label overriding it), so the run positions
        # stay valid. Otherwise the whole-title ``name_furigana`` fallback is used.
        ruby_positions: tuple = ()
        if not short_label:
            segments = competency_ruby_segments(competency)
            ruby_positions = _ruby_positions(
                printed_title, segments, fit.lines, fit.font_pt, center_x, name_y_mm
            )
        columns.append(
            PeerReviewCompetencyColumn(
                competency_id=competency.id,
                name=name,
                name_furigana=getattr(competency, "name_furigana", "") or "",
                display_order=competency.display_order or index,
                x_mm=x_mm,
                width_mm=width,
                score_positions=score_positions,
                header_lines=fit.lines,
                header_font_pt=fit.font_pt,
                header_too_long=fit.too_long,
                ruby_positions=ruby_positions,
            )
        )
    return columns


def _score_x_mm(
    column_x_mm: float, column_width_mm: float, score_index: int, score_count: int
) -> float:
    if score_count <= 1:
        return column_x_mm + column_width_mm / 2
    usable_width = max(MIN_SCORE_GAP_MM * (score_count - 1), column_width_mm - 8.0)
    gap = usable_width / (score_count - 1)
    first_x = column_x_mm + (column_width_mm - usable_width) / 2
    return first_x + score_index * gap
