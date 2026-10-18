from dataclasses import dataclass
from io import BytesIO

import qrcode
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import data_relative_path, ensure_data_directories
from app.models import (
    Competency,
    MockExam,
    PeerReviewBatch,
    PeerReviewFinalResult,
    PeerReviewForm,
    PeerReviewGroupResult,
    PeerReviewPage,
    PeerReviewRating,
    PeerReviewSubmissionPage,
)
from app.services.i18n_service import _
from app.services.omr_settings_service import get_effective_omr_settings
from app.services.peer_review_builder_service import (
    LAYOUT_CHANGE_BLOCKED_MESSAGE,
    auto_paginate,
    get_or_create_form,
    peer_review_form_has_scans_or_ratings,
    sync_targets_from_roster,
)
from app.services.peer_review_layout_service import (
    MARKER_SIZE_MM,
    PeerReviewPagePlan,
    calculate_peer_review_layout,
    coordinate_map_from_layout,
    page_plan_for_form,
)
from app.services.sheet_generation_service import ROW_STRIPE_GRAY, _get_fonts, _safe_filename

POINTS_PER_MM = 72 / 25.4
NO_ACTIVE_TARGETS_MESSAGE = "no_active_targets"
LAYOUT_DOES_NOT_FIT_MESSAGE = "layout_does_not_fit"


@dataclass(frozen=True)
class PeerReviewSheetSummary:
    pdf_path: str
    pages_created: int
    download_path: str


def generate_peer_review_pages(db: Session, exam: MockExam) -> list[PeerReviewPage]:
    form = get_or_create_form(db, exam)
    # Always (re)build targets from the live roster before printing so generated pages
    # carry real student names, never placeholders.
    if not exam.is_template:
        sync_targets_from_roster(db, form)
    if not [target for target in form.targets if target.active]:
        raise ValueError(NO_ACTIVE_TARGETS_MESSAGE)
    competencies = list(
        db.scalars(
            select(Competency)
            .where(Competency.exam_id == exam.id)
            .order_by(Competency.display_order)
        )
    )
    if not competencies:
        raise ValueError("Peer-review forms need at least one competency.")
    plan = page_plan_for_form(form, competencies)
    if not plan.fits:
        raise ValueError(LAYOUT_DOES_NOT_FIT_MESSAGE)
    # Paginate when there are no pages yet, or when an existing page now exceeds the rows
    # that fit the resolved orientation (e.g. orientation/scale changed after pagination).
    # Manual page breaks that stay within capacity are preserved.
    overflowing = any(len(page.target_rows_json) > plan.students_per_page for page in form.pages)
    if not form.pages or overflowing:
        auto_paginate(db, form)
        db.refresh(form)
    for page in form.pages:
        coordinate_map = coordinate_map_for_page(form, page, competencies, plan)
        if (
            page.coordinate_map_json
            and page.coordinate_map_json != coordinate_map
            and peer_review_form_has_scans_or_ratings(db, form.id)
        ):
            raise ValueError(LAYOUT_CHANGE_BLOCKED_MESSAGE)
        page.coordinate_map_json = coordinate_map
    db.commit()
    return list(form.pages)


def generate_peer_review_pdf(db: Session, exam: MockExam) -> PeerReviewSheetSummary:
    pages = generate_peer_review_pages(db, exam)
    form = get_or_create_form(db, exam)
    competencies = list(
        db.scalars(
            select(Competency)
            .where(Competency.exam_id == exam.id)
            .order_by(Competency.display_order)
        )
    )
    # One resolved orientation for the whole session — never per page.
    plan = page_plan_for_form(form, competencies)
    ensure_data_directories()
    output_dir = get_settings().data_root / "generated_pdfs" / f"exam_{exam.id}"
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / f"{_safe_filename(exam.name)}_anonymous_peer_review.pdf"
    canvas = Canvas(str(pdf_path), pagesize=(_mm(plan.page_width_mm), _mm(plan.page_height_mm)))
    total_pages = len(pages)
    for page in pages:
        _draw_peer_review_page(canvas, exam, form, page, competencies, plan, total_pages)
        canvas.showPage()
    canvas.save()
    relative_path = data_relative_path(pdf_path)
    exam.status = "printed"
    db.commit()
    return PeerReviewSheetSummary(
        pdf_path=relative_path,
        pages_created=len(pages),
        download_path=relative_path,
    )


def regenerate_peer_review_sheets(db: Session, exam: MockExam) -> PeerReviewSheetSummary:
    """Destructively rebuild a peer-review form's printed sheets.

    Deletes every artefact tied to the previously printed layout — scan batches,
    submission pages, ratings, group/final results, and the pages themselves (so their
    QR tokens and coordinate maps are dropped) — then regenerates fresh pages (new QR
    tokens), a new PDF, and re-locks the layout. This is the only sanctioned way past
    the normal locked-layout protection, and the caller must confirm the data loss.
    """
    form = get_or_create_form(db, exam)
    page_ids = [page.id for page in form.pages]
    batch_ids = [
        batch_id
        for (batch_id,) in db.execute(
            select(PeerReviewBatch.id).where(PeerReviewBatch.form_id == form.id)
        )
    ]
    db.execute(delete(PeerReviewRating).where(PeerReviewRating.form_id == form.id))
    db.execute(delete(PeerReviewGroupResult).where(PeerReviewGroupResult.form_id == form.id))
    db.execute(delete(PeerReviewFinalResult).where(PeerReviewFinalResult.form_id == form.id))
    if page_ids or batch_ids:
        db.execute(
            delete(PeerReviewSubmissionPage).where(
                PeerReviewSubmissionPage.peer_review_page_id.in_(page_ids or [-1])
                | PeerReviewSubmissionPage.batch_id.in_(batch_ids or [-1])
            )
        )
    db.execute(delete(PeerReviewBatch).where(PeerReviewBatch.form_id == form.id))
    # Drop the old pages outright so auto-pagination mints brand-new QR tokens and
    # coordinate maps; old printed copies become unreadable, as intended.
    for page in list(form.pages):
        db.delete(page)
    # Clear the printed lock so regeneration is allowed (we just removed the scans/ratings
    # that the lock protects). generate_peer_review_pdf re-locks via status="printed".
    if exam.status == "printed":
        exam.status = "ready_to_print"
    db.commit()
    db.refresh(form)
    return generate_peer_review_pdf(db, exam)


def coordinate_map_for_page(
    form: PeerReviewForm,
    page: PeerReviewPage,
    competencies: list[Competency],
    plan: PeerReviewPagePlan | None = None,
) -> dict:
    layout = calculate_peer_review_layout(form, page, competencies, plan)
    return coordinate_map_from_layout(form, page, layout)


def _draw_peer_review_page(
    canvas: Canvas,
    exam: MockExam,
    form: PeerReviewForm,
    page: PeerReviewPage,
    competencies: list[Competency],
    plan: PeerReviewPagePlan | None = None,
    total_pages: int | None = None,
) -> None:
    layout = calculate_peer_review_layout(form, page, competencies, plan)
    settings = get_effective_omr_settings()
    grid_gray = settings.grid_line_gray
    # Row separators are even lighter than the column grid so a scanner never mistakes
    # a border for a pencil mark.
    row_gray = min(0.92, grid_gray + 0.1)
    outline_gray = settings.bubble_outline_gray
    label_gray = settings.choice_label_gray
    height = _mm(layout.page_height_mm)
    font_regular, font_bold = _get_fonts()
    canvas.setTitle(form.title)
    _draw_markers(canvas, layout.marker_positions, layout.page_height_mm)

    # --- Localized header (test/session name, date, page) — kept compact so the first
    #     rating row never reaches up into the title block. ---
    canvas.setFillColorRGB(0, 0, 0)
    date_text = exam.exam_date or _("peer_review.no_date")
    # Header text starts clear of the top-left detection marker (single source of truth
    # in the layout service) so the title never visually touches the black square.
    content_left = _mm(layout.content_left_mm)
    canvas.setFont(font_bold, 13)
    canvas.drawString(content_left, height - _mm(10), form.title[:72])
    # One compact sub-line: date + page X / Y, values only. Labels (実施日 / ページ) are
    # dropped — the title above already names the sheet, and the bare values read cleanly.
    page_label = str(page.page_no)
    if total_pages:
        page_label = f"{page_label} / {total_pages}"
    canvas.setFont(font_regular, 8)
    canvas.drawString(content_left, height - _mm(16), f"{date_text}　{page_label}")
    _draw_qr(
        canvas,
        page.qr_token,
        _mm(layout.qr["x_mm"]),
        height - _mm(layout.qr["y_mm"] + layout.qr["size_mm"]),
        layout.qr["size_mm"],
    )

    def y_at(mm_from_top: float) -> float:
        return height - _mm(mm_from_top)

    # --- Very light alternating row shading, painted first (under the grid, names, and
    #     white-filled bubbles) so a rater never loses a row. Far lighter than any pencil
    #     mark, so a scanner never reads it as a filled rating. ---
    canvas.setFillColorRGB(ROW_STRIPE_GRAY, ROW_STRIPE_GRAY, ROW_STRIPE_GRAY)
    for index, row in enumerate(layout.target_rows):
        if index % 2 == 1:
            canvas.rect(
                _mm(layout.table_left_mm),
                y_at(row.row_bottom_mm),
                _mm(layout.table_right_mm - layout.table_left_mm),
                _mm(row.row_bottom_mm - row.row_top_mm),
                stroke=0,
                fill=1,
            )
    canvas.setFillColorRGB(0, 0, 0)

    # --- Table outline + vertical separators between competency columns (light grid) ---
    canvas.setStrokeColorRGB(grid_gray, grid_gray, grid_gray)
    canvas.setLineWidth(0.4)
    for x_mm in layout.column_separators_mm:
        canvas.line(_mm(x_mm), y_at(layout.header_top_mm), _mm(x_mm), y_at(layout.table_bottom_mm))
    for edge_mm in (layout.header_top_mm, layout.table_top_mm, layout.table_bottom_mm):
        canvas.line(
            _mm(layout.table_left_mm), y_at(edge_mm), _mm(layout.table_right_mm), y_at(edge_mm)
        )

    # --- Header band: target label + per-competency title (auto-fit), furigana ---
    target_col = layout.target_column
    name_max_w = _mm(max(2.0, target_col["width_mm"] - 2.0))
    canvas.setFillColorRGB(0, 0, 0)
    canvas.setFont(font_bold, 8)
    canvas.drawString(_mm(target_col["x_mm"]), y_at(layout.name_y_mm), _("peer_review.target")[:16])
    for column in layout.competency_columns:
        center = _mm(column.center_x_mm)
        # Per-segment ruby: each reading sits small above just its base run (positions
        # already resolved by the layout service). Falls back to one whole-title furigana
        # centred over the column when no per-segment ruby is set.
        if column.ruby_positions:
            canvas.setFont(font_regular, 5)
            for ruby in column.ruby_positions:
                canvas.drawCentredString(
                    _mm(ruby["x_mm"]), y_at(ruby["y_mm"]), ruby["reading"][:16]
                )
        elif column.name_furigana:
            canvas.setFont(font_regular, 5)
            canvas.drawCentredString(center, y_at(layout.furigana_y_mm), column.name_furigana[:24])
        # Auto-fit title: 1-2 lines at the size the layout service chose so it never spills
        # out of the column. No score legend row — each bubble below prints its own number.
        lines = column.header_lines or (column.name,)
        canvas.setFont(font_bold, column.header_font_pt)
        line_gap_mm = column.header_font_pt / POINTS_PER_MM * 1.05
        count = len(lines)
        for line_index, line in enumerate(lines):
            offset_mm = (line_index - (count - 1) / 2) * line_gap_mm
            canvas.drawCentredString(center, y_at(layout.name_y_mm + offset_mm), line)
        canvas.setFillColorRGB(0, 0, 0)

    # --- Target rows with grouped rating bubbles ---
    bubbles_by_row: dict[int, list] = {}
    for bubble in layout.rating_bubbles:
        bubbles_by_row.setdefault(bubble.target_id, []).append(bubble)
    last_index = len(layout.target_rows) - 1
    for index, row in enumerate(layout.target_rows):
        y = y_at(row.row_center_y_mm)
        canvas.setFillColorRGB(0, 0, 0)
        canvas.setFont(font_bold, 7)
        label = _truncate_to_width(canvas, row.display_label, font_bold, 7, name_max_w)
        canvas.drawString(_mm(target_col["x_mm"]), y - 2.4, label)
        for bubble in bubbles_by_row.get(row.target_id, []):
            x = _mm(bubble.x_mm)
            # Light outline + light digit: students can read the scale, the scanner can't.
            # White interior so the row stripe never shows inside a bubble.
            canvas.setStrokeColorRGB(outline_gray, outline_gray, outline_gray)
            canvas.setFillColorRGB(1, 1, 1)
            canvas.circle(x, y, _mm(bubble.radius_mm), stroke=1, fill=1)
            canvas.setFillColorRGB(label_gray, label_gray, label_gray)
            canvas.setFont(font_regular, 5)
            canvas.drawCentredString(x, y - 1.7, str(bubble.score))
        if index < last_index:
            canvas.setStrokeColorRGB(row_gray, row_gray, row_gray)
            canvas.setLineWidth(0.18)
            canvas.line(
                _mm(layout.table_left_mm),
                y_at(row.separator_y_mm),
                _mm(layout.table_right_mm),
                y_at(row.separator_y_mm),
            )

    canvas.setFillColorRGB(0, 0, 0)
    canvas.setStrokeColorRGB(0, 0, 0)


def _draw_markers(canvas: Canvas, marker_positions: dict, page_height_mm: float) -> None:
    for marker in marker_positions.values():
        x_mm = marker["x_mm"]
        y_mm = marker["y_mm"]
        canvas.rect(
            _mm(x_mm),
            _mm(page_height_mm - y_mm - MARKER_SIZE_MM),
            _mm(MARKER_SIZE_MM),
            _mm(MARKER_SIZE_MM),
            fill=1,
        )


def _draw_qr(canvas: Canvas, token: str, x: float, y: float, size_mm: float = 26.0) -> None:
    buffer = BytesIO()
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(token)
    qr.make(fit=True)
    qr.make_image(fill_color="black", back_color="white").save(buffer, format="PNG")
    buffer.seek(0)
    canvas.drawImage(
        ImageReader(buffer), x, y, width=_mm(size_mm), height=_mm(size_mm), mask="auto"
    )


def _truncate_to_width(
    canvas: Canvas, text: str, font: str, size: float, max_width_pt: float
) -> str:
    """Shorten ``text`` with an ellipsis so it fits ``max_width_pt`` at the given font.

    Used for student/target names: the column auto-sizes to the longest name (clamped),
    so only an extreme outlier is ever truncated — the table stays aligned regardless.
    """
    text = text or ""
    if canvas.stringWidth(text, font, size) <= max_width_pt:
        return text
    ellipsis = "…"
    while text and canvas.stringWidth(text + ellipsis, font, size) > max_width_pt:
        text = text[:-1]
    return text + ellipsis if text else ellipsis


def _mm(value: float) -> float:
    return value * POINTS_PER_MM
