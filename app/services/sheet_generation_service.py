import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import qrcode
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import data_relative_path, ensure_data_directories
from app.models import BlankSheetLabel, MockExam, PhysicalSheet, SheetInstance, SheetTemplate
from app.services.omr_settings_service import get_effective_omr_settings
from app.services.qr_token_service import generate_qr_token

# Register an embedded Japanese-capable font when available. CID fonts are a fallback
# because some PDF viewers substitute missing Japanese base fonts with square glyphs.
JAPANESE_FONT_REGULAR = "Helvetica"
JAPANESE_FONT_BOLD = "Helvetica-Bold"
HAS_JAPANESE_FONT = False
JAPANESE_FONT_IS_EMBEDDED = False


def _register_japanese_font() -> tuple[str, str, bool, bool]:
    # (family, regular_path, bold_path|None). Priority order, cleanest first: Noto Sans
    # CJK / Noto Sans JP, then Yu Gothic, then Meiryo, with MS Gothic only as a last resort.
    # Each entry lists Linux, WSL (/mnt/c) and native Windows (C:/) paths so the same clean
    # family is chosen wherever the app runs. Regular and bold are registered as separate
    # faces whenever a distinct bold file exists, so bold header text actually renders bold.
    embedded_candidates = (
        (
            "NotoSansCJKjp",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        ),
        (
            "NotoSansCJKjp",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        ),
        ("NotoSansJP", "/mnt/c/Windows/Fonts/NotoSansJP-VF.ttf", None),
        ("NotoSansJP", "C:/Windows/Fonts/NotoSansJP-VF.ttf", None),
        ("YuGothic", "/mnt/c/Windows/Fonts/YuGothR.ttc", "/mnt/c/Windows/Fonts/YuGothB.ttc"),
        ("YuGothic", "C:/Windows/Fonts/YuGothR.ttc", "C:/Windows/Fonts/YuGothB.ttc"),
        ("Meiryo", "/mnt/c/Windows/Fonts/meiryo.ttc", "/mnt/c/Windows/Fonts/meiryob.ttc"),
        ("Meiryo", "C:/Windows/Fonts/meiryo.ttc", "C:/Windows/Fonts/meiryob.ttc"),
        ("MSGothic", "/mnt/c/Windows/Fonts/msgothic.ttc", None),
        ("MSGothic", "C:/Windows/Fonts/msgothic.ttc", None),
    )
    for family, regular_path, bold_path in embedded_candidates:
        if not Path(regular_path).exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont(family, regular_path))
        except Exception:
            continue
        bold_name = family
        if bold_path and Path(bold_path).exists():
            try:
                pdfmetrics.registerFont(TTFont(f"{family}-Bold", bold_path))
                bold_name = f"{family}-Bold"
            except Exception:
                bold_name = family
        return family, bold_name, True, True

    for candidate_font in ("HeiseiKakuGo-W5", "HeiseiMin-W3"):
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(candidate_font))
        except Exception:
            continue
        return candidate_font, candidate_font, True, False

    return "Helvetica", "Helvetica-Bold", False, False


(
    JAPANESE_FONT_REGULAR,
    JAPANESE_FONT_BOLD,
    HAS_JAPANESE_FONT,
    JAPANESE_FONT_IS_EMBEDDED,
) = _register_japanese_font()


def _get_fonts() -> tuple[str, str]:
    if HAS_JAPANESE_FONT:
        return JAPANESE_FONT_REGULAR, JAPANESE_FONT_BOLD
    return "Helvetica", "Helvetica-Bold"


A4_WIDTH_MM = 210
A4_HEIGHT_MM = 297
LANDSCAPE_A4_WIDTH_MM = 297
LANDSCAPE_A4_HEIGHT_MM = 210
POINTS_PER_MM = 72 / 25.4
LANDSCAPE_A4 = landscape(A4)
NORMAL_MARKER_POSITIONS = {
    "top_left": {"x_mm": 10, "y_mm": 10},
    "top_right": {"x_mm": 281, "y_mm": 10},
    "bottom_left": {"x_mm": 10, "y_mm": 194},
    "bottom_right": {"x_mm": 281, "y_mm": 194},
}
DEFAULT_LANGUAGE_KNOWLEDGE_GROUP_RANGES = (
    (1, 1, 6),
    (2, 7, 13),
    (3, 14, 19),
    (4, 20, 25),
    (5, 26, 35),
    (6, 36, 40),
    (7, 41, 45),
    (8, 46, 49),
    (9, 50, 58),
    (10, 59, 62),
    (11, 63, 64),
    (12, 65, 68),
    (13, 69, 70),
)
DEFAULT_LISTENING_GROUP_SIZES = (6, 7, 6, 14, 4)

# Very light alternating row shading (~#F6F6F6). Light enough that a scanner never reads
# it as a filled bubble; just enough that a reader does not lose their row. Drawn under the
# grid lines and bubbles (bubble interiors are painted white on top).
ROW_STRIPE_GRAY = 0.965


@dataclass(frozen=True)
class PositionedChoice:
    choice: str
    x_mm: float
    y_mm: float
    radius_mm: float


@dataclass(frozen=True)
class PositionedQuestion:
    question_id: int
    display_label: str
    label_x_mm: float
    label_y_mm: float
    choices: list[PositionedChoice]


@dataclass(frozen=True)
class PositionedQuestionGroup:
    group_label: str
    origin_x_mm: float
    origin_y_mm: float
    row_gap_mm: float
    choice_gap_mm: float
    questions: list[PositionedQuestion]


@dataclass(frozen=True)
class PositionedSheetLayout:
    page_width_mm: float
    page_height_mm: float
    marker_positions: dict
    groups: list[PositionedQuestionGroup]


@dataclass(frozen=True)
class GeneratedSheetSummary:
    class_pdf_paths: list[str]
    sheet_instances_created: int
    templates_created: int
    zip_path: str | None
    download_path: str


def generate_exam_sheets(db: Session, exam: MockExam) -> GeneratedSheetSummary:
    if not exam.students:
        raise ValueError("Import students before generating sheets.")

    if exam.assessment_type == "peer_review":
        from app.models import Competency
        from app.services.peer_review_sheet_service import generate_peer_review_pdf

        competencies = list(
            db.scalars(
                select(Competency)
                .where(Competency.exam_id == exam.id)
                .order_by(Competency.display_order)
            )
        )
        if not competencies:
            raise ValueError("Configure competencies before generating sheets.")

        summary = generate_peer_review_pdf(db, exam)
        return GeneratedSheetSummary(
            class_pdf_paths=[summary.pdf_path],
            sheet_instances_created=0,
            templates_created=summary.pages_created,
            zip_path=None,
            download_path=summary.download_path,
        )

    if not exam.questions:
        raise ValueError("Generate questions and save an answer key before generating sheets.")

    ensure_data_directories()
    output_dir = get_settings().data_root / "generated_pdfs" / f"exam_{exam.id}"
    output_dir.mkdir(parents=True, exist_ok=True)

    active_sheets = _active_physical_sheets(exam)
    if not active_sheets:
        raise ValueError("No physical sheets contain questions. Add questions before generating sheets.")

    templates_by_sheet = _create_templates(db, exam, active_sheets)
    generated_pdf_paths: list[str] = []
    instances_created = 0
    included_links = [link for link in exam.students if link.included]
    included_links = sorted(
        included_links,
        key=lambda link: (
            link.seat_order is None,
            link.seat_order or 0,
            link.group_name,
            link.student.student_number,
        ),
    )
    if not included_links:
        raise ValueError("Add included students to the roster before generating sheets.")

    pdf_path = output_dir / _exam_pdf_filename(exam)
    canvas = Canvas(str(pdf_path), pagesize=LANDSCAPE_A4)
    page_no = 1
    for link in included_links:
        for physical_sheet in active_sheets:
            template = templates_by_sheet[physical_sheet.id]
            instance = _sheet_instance_for_generation(
                db=db,
                exam=exam,
                student_id=link.student_id,
                physical_sheet_id=physical_sheet.id,
                template_id=template.id,
                printed_name=link.student.name,
                printed_student_number=link.student.student_number,
                pdf_file_path=data_relative_path(pdf_path),
                page_no=page_no,
            )
            _draw_sheet(canvas, exam, physical_sheet, template, instance)
            canvas.showPage()
            instances_created += 1
            page_no += 1
    canvas.save()
    generated_pdf_paths.append(data_relative_path(pdf_path))

    zip_path = _build_zip(output_dir, generated_pdf_paths) if len(generated_pdf_paths) > 1 else None
    download_path = data_relative_path(zip_path) if zip_path is not None else generated_pdf_paths[0]
    exam.status = "printed"
    db.commit()
    return GeneratedSheetSummary(
        class_pdf_paths=generated_pdf_paths,
        sheet_instances_created=instances_created,
        templates_created=len(templates_by_sheet),
        zip_path=data_relative_path(zip_path) if zip_path is not None else None,
        download_path=download_path,
    )


def generate_blank_sheets(db: Session, exam: MockExam, copies: int = 1) -> str:
    """Generate extra mark sheets not linked to a student.

    Each page carries an unassigned QR token (BlankSheetLabel) so the session
    and physical sheet are recognized at scan time; the teacher assigns the
    student during upload review. Returns the relative download path.
    """
    if exam.assessment_type == "peer_review":
        raise ValueError("Blank sheets are not available for peer review sessions.")
    if not exam.questions:
        raise ValueError("Generate questions and save an answer key before generating sheets.")

    ensure_data_directories()
    output_dir = get_settings().data_root / "generated_pdfs" / f"exam_{exam.id}"
    output_dir.mkdir(parents=True, exist_ok=True)

    active_sheets = _active_physical_sheets(exam)
    if not active_sheets:
        raise ValueError("No physical sheets contain questions. Add questions before generating sheets.")

    # Blank sheets must use the exact coordinate maps the student sheets use,
    # so they only ever reference existing latest templates. Templates are
    # created fresh only when nothing was generated for this session yet.
    latest_template_ids = _latest_template_ids_by_sheet(exam)
    if all(sheet.id in latest_template_ids for sheet in active_sheets):
        templates_by_sheet = {
            sheet.id: db.get(SheetTemplate, latest_template_ids[sheet.id])
            for sheet in active_sheets
        }
    elif not exam.sheet_instances:
        templates_by_sheet = _create_templates(db, exam, active_sheets)
    else:
        raise ValueError("Regenerate the student mark sheets first so blank sheets match their layout.")

    pdf_path = output_dir / _blank_pdf_filename(exam)
    relative_path = data_relative_path(pdf_path)
    canvas = Canvas(str(pdf_path), pagesize=LANDSCAPE_A4)
    for _copy_no in range(max(1, copies)):
        for physical_sheet in active_sheets:
            template = templates_by_sheet[physical_sheet.id]
            label = BlankSheetLabel(
                exam_id=exam.id,
                physical_sheet_id=physical_sheet.id,
                sheet_template_id=template.id,
                qr_token=generate_qr_token(),
                pdf_file_path=relative_path,
            )
            db.add(label)
            db.flush()
            _draw_sheet_page(
                canvas,
                exam,
                physical_sheet,
                template,
                printed_student_number="",
                printed_name="",
                qr_token=label.qr_token,
            )
            canvas.showPage()
    canvas.save()
    db.commit()
    return relative_path


def _blank_pdf_filename(exam: MockExam) -> str:
    date_part = exam.exam_date or "undated"
    return f"{date_part}_{_safe_filename(exam.name)}_blank_sheets.pdf"


def _sheet_instance_for_generation(
    db: Session,
    exam: MockExam,
    student_id: int,
    physical_sheet_id: int,
    template_id: int,
    printed_name: str,
    printed_student_number: str,
    pdf_file_path: str,
    page_no: int,
) -> SheetInstance:
    instance = db.scalar(
        select(SheetInstance)
        .where(SheetInstance.exam_id == exam.id)
        .where(SheetInstance.student_id == student_id)
        .where(SheetInstance.physical_sheet_id == physical_sheet_id)
    )
    if instance is None:
        instance = SheetInstance(
            exam_id=exam.id,
            student_id=student_id,
            physical_sheet_id=physical_sheet_id,
            sheet_template_id=template_id,
            qr_token=generate_qr_token(),
            printed_name=printed_name,
            printed_student_number=printed_student_number,
            pdf_file_path=pdf_file_path,
            page_no=page_no,
            status="generated",
        )
        db.add(instance)
    else:
        instance.sheet_template_id = template_id
        instance.printed_name = printed_name
        instance.printed_student_number = printed_student_number
        instance.pdf_file_path = pdf_file_path
        instance.page_no = page_no
        instance.status = "generated"
        instance.created_at = datetime.utcnow()
    db.flush()
    return instance


def _active_physical_sheets(exam: MockExam) -> list[PhysicalSheet]:
    return [
        sheet
        for sheet in exam.physical_sheets
        if any(question.physical_sheet_id == sheet.id for question in exam.questions)
    ]


def _create_templates(
    db: Session, exam: MockExam, physical_sheets: list[PhysicalSheet] | None = None
) -> dict[int, SheetTemplate]:
    templates: dict[int, SheetTemplate] = {}
    for physical_sheet in physical_sheets or exam.physical_sheets:
        questions = [
            question
            for question in exam.questions
            if question.physical_sheet_id == physical_sheet.id
        ]
        if not questions:
            continue
        coordinate_map = _coordinate_map_for_questions(
            questions, physical_sheet.sheet_name, physical_sheet.slug
        )
        template = SheetTemplate(
            exam_id=exam.id,
            physical_sheet_id=physical_sheet.id,
            version=1,
            paper_size="A4-landscape",
            coordinate_map_json=coordinate_map,
            marker_positions_json=NORMAL_MARKER_POSITIONS,
        )
        db.add(template)
        db.flush()
        templates[physical_sheet.id] = template
    return templates


def _coordinate_map_for_questions(
    questions: list, sheet_name: str | None = None, sheet_slug: str | None = None
) -> dict:
    layout = calculate_sheet_layout(questions, sheet_name, sheet_slug)
    return _coordinate_map_from_layout(layout)


def calculate_sheet_layout(
    questions: list,
    sheet_name: str | None = None,
    sheet_slug: str | None = None,
) -> PositionedSheetLayout:
    ordered_questions = sorted(questions, key=lambda q: (q.display_order or 0, q.question_no or 0))
    question_groups = _group_questions_for_layout(ordered_questions, sheet_name, sheet_slug)
    row_gap = 6.3
    choice_gap = 9.4
    bubble_radius = 2.2
    left_margin = 14
    top_y = 48
    bottom_safe = 194
    column_gap = 3
    column_count = 5 if len(ordered_questions) >= 55 else 4 if len(ordered_questions) >= 30 else 3
    column_width = (LANDSCAPE_A4_WIDTH_MM - left_margin - 12 - (column_count - 1) * column_gap) / column_count
    max_rows_per_block = max(1, int((bottom_safe - top_y - 4) // row_gap))
    column_x = [left_margin + index * (column_width + column_gap) for index in range(column_count)]

    positioned_groups: list[PositionedQuestionGroup] = []
    current_column = 0
    current_y = top_y
    for group_label, group_questions in question_groups:
        chunks = [
            group_questions[index : index + max_rows_per_block]
            for index in range(0, len(group_questions), max_rows_per_block)
        ]
        for chunk_index, chunk in enumerate(chunks):
            block_height = 4 + len(chunk) * row_gap
            if current_y + block_height > bottom_safe and current_y > top_y:
                current_column += 1
                current_y = top_y
            if current_column >= column_count:
                raise ValueError("Question layout exceeds one landscape A4 page.")
            # When a single group (e.g. one もんだい) overflows into the next column, the
            # continuation block carries no header: the table just continues, with no repeated
            # もんだい title and no "continued" marker.
            label = group_label if chunk_index == 0 else ""
            origin_x = column_x[current_column]
            choices_start_x = origin_x + 16
            questions_for_block: list[PositionedQuestion] = []
            for row_index, question in enumerate(chunk):
                y_mm = current_y + 4 + row_index * row_gap
                choices = [
                    PositionedChoice(
                        choice=str(choice),
                        x_mm=choices_start_x + (choice - 1) * choice_gap,
                        y_mm=y_mm,
                        radius_mm=bubble_radius,
                    )
                    for choice in range(1, question.choice_count + 1)
                ]
                questions_for_block.append(
                    PositionedQuestion(
                        question_id=question.id,
                        display_label=_question_display_label(question),
                        label_x_mm=origin_x + 8,
                        label_y_mm=y_mm,
                        choices=choices,
                    )
                )
            positioned_groups.append(
                PositionedQuestionGroup(
                    group_label=label,
                    origin_x_mm=origin_x,
                    origin_y_mm=current_y,
                    row_gap_mm=row_gap,
                    choice_gap_mm=choice_gap,
                    questions=questions_for_block,
                )
            )
            current_y += block_height + 2

    return PositionedSheetLayout(
        page_width_mm=LANDSCAPE_A4_WIDTH_MM,
        page_height_mm=LANDSCAPE_A4_HEIGHT_MM,
        marker_positions=NORMAL_MARKER_POSITIONS,
        groups=positioned_groups,
    )


def _group_questions_for_layout(
    questions: list,
    sheet_name: str | None = None,
    sheet_slug: str | None = None,
) -> list[tuple[str, list]]:
    if any(question.problem_no is not None for question in questions):
        title_by_problem_no = {
            group.problem_no: group.problem_title
            for question in questions
            if question.problem_no is not None
            for group in getattr(question.exam, "problem_groups", [])
            if group.physical_sheet_id == question.physical_sheet_id
        }
        groups: dict[tuple[int, int], list] = {}
        for question in questions:
            key = (question.problem_no if question.problem_no is not None else 9999, len(groups))
            if question.problem_no is not None:
                key = (question.problem_no, question.problem_no)
            groups.setdefault(key, []).append(question)
        return [
            (title_by_problem_no.get(key[0]) or f"もんだい{key[0]}", group)
            for key, group in sorted(groups.items(), key=lambda item: item[0])
        ]

    inferred_groups = _infer_numbered_sheet_problem_groups(questions, sheet_name, sheet_slug)
    if inferred_groups:
        return inferred_groups

    if any(question.item_type or question.test_section for question in questions):
        groups_by_label: dict[str, list] = {}
        for question in questions:
            label = question.item_type or question.test_section or "Questions"
            groups_by_label.setdefault(label, []).append(question)
        return list(groups_by_label.items())

    chunk_size = 14
    return [
        (f"Questions {index + 1}", questions[index : index + chunk_size])
        for index in range(0, len(questions), chunk_size)
    ]


def _infer_numbered_sheet_problem_groups(
    questions: list,
    sheet_name: str | None,
    sheet_slug: str | None = None,
) -> list[tuple[str, list]] | None:
    slug = (sheet_slug or "").strip()
    if slug in {"vocabulary", "language-reading"} or (
        not slug and sheet_name and ("文字" in sheet_name or "語彙" in sheet_name)
    ):
        by_question_no = {_numeric_question_no(question): question for question in questions}
        groups = []
        for problem_no, start_no, end_no in DEFAULT_LANGUAGE_KNOWLEDGE_GROUP_RANGES:
            group_questions = [
                by_question_no[question_no]
                for question_no in range(start_no, end_no + 1)
                if question_no in by_question_no
            ]
            if group_questions:
                groups.append((f"もんだい{problem_no}", group_questions))
        if sum(len(group) for _label, group in groups) == len(questions):
            return groups

    if slug == "listening" or (not slug and sheet_name and "聴解" in sheet_name):
        groups = []
        start_index = 0
        for problem_no, group_size in enumerate(DEFAULT_LISTENING_GROUP_SIZES, start=1):
            group_questions = questions[start_index : start_index + group_size]
            if group_questions:
                groups.append((f"もんだい{problem_no}", group_questions))
            start_index += group_size
        if start_index >= len(questions):
            return groups

    return None


def _numeric_question_no(question) -> int | None:
    for value in (question.local_question_no, question.question_no, question.display_label):
        if value is None:
            continue
        try:
            number = float(str(value).strip())
        except ValueError:
            continue
        if number.is_integer():
            return int(number)
    return None


def _question_display_label(question) -> str:
    if getattr(question, "local_question_label", None):
        return str(question.local_question_label)
    if question.subquestion_no is not None:
        return str(question.subquestion_no)
    if question.local_question_no is not None:
        return str(question.local_question_no)
    return str(question.display_label or question.question_no or question.id)


def _coordinate_map_from_layout(layout: PositionedSheetLayout) -> dict:
    question_maps = []
    for group in layout.groups:
        for question in group.questions:
            question_maps.append(
                {
                    "question_id": question.question_id,
                    "display_label": question.display_label,
                    "group_label": group.group_label,
                    "label_x_mm": question.label_x_mm,
                    "label_y_mm": question.label_y_mm,
                    "choices": [
                        {
                            "choice": choice.choice,
                            "x_mm": choice.x_mm,
                            "y_mm": choice.y_mm,
                            "radius_mm": choice.radius_mm,
                        }
                        for choice in question.choices
                    ],
                }
            )
    return {
        "page_width_mm": layout.page_width_mm,
        "page_height_mm": layout.page_height_mm,
        "orientation": "landscape",
        "marker_positions": layout.marker_positions,
        "groups": [
            {
                "group_label": group.group_label,
                "origin_x_mm": group.origin_x_mm,
                "origin_y_mm": group.origin_y_mm,
                "row_gap_mm": group.row_gap_mm,
                "choice_gap_mm": group.choice_gap_mm,
                "question_ids": [question.question_id for question in group.questions],
            }
            for group in layout.groups
        ],
        "questions": question_maps,
    }


def _draw_sheet(
    canvas: Canvas,
    exam: MockExam,
    physical_sheet: PhysicalSheet,
    template: SheetTemplate,
    instance: SheetInstance,
) -> None:
    _draw_sheet_page(
        canvas,
        exam,
        physical_sheet,
        template,
        printed_student_number=instance.printed_student_number,
        printed_name=instance.printed_name,
        qr_token=instance.qr_token,
    )


def _draw_sheet_page(
    canvas: Canvas,
    exam: MockExam,
    physical_sheet: PhysicalSheet,
    template: SheetTemplate,
    *,
    printed_student_number: str,
    printed_name: str,
    qr_token: str,
) -> None:

    font_regular, font_bold = _get_fonts()

    width, height = LANDSCAPE_A4
    canvas.setTitle(exam.name)
    _draw_markers(canvas, template.marker_positions_json, LANDSCAPE_A4_HEIGHT_MM)

    # Title: session name + sheet name only. The session title already covers it
    # decorative "■" is gone — the four corner markers are the
    # only fixed alignment glyphs the scanner needs.
    title_parts = [exam.name, physical_sheet.sheet_name]
    canvas.setFont(font_bold, 14)
    canvas.drawString(_mm(22), height - _mm(18), "・".join(part for part in title_parts if part))
    # Metadata line: bare values (student number, name, date) with no 学生番号/氏名/実施日
    # labels. Blank fields keep an underline so a handwritten entry still has a guide.
    canvas.setFont(font_regular, 10)
    metadata_y = height - _mm(31)
    canvas.drawString(_mm(22), metadata_y, printed_student_number)
    if not printed_student_number:
        canvas.line(_mm(22), metadata_y - 2, _mm(52), metadata_y - 2)
    name_text = printed_name
    max_name_width = _mm(82)
    if canvas.stringWidth(name_text, font_regular, 10) > max_name_width:
        while len(name_text) > 2 and canvas.stringWidth(name_text + "...", font_regular, 10) > max_name_width:
            name_text = name_text[:-1]
        name_text = f"{name_text}..."
    canvas.drawString(_mm(82), metadata_y, name_text)
    if not printed_name:
        canvas.line(_mm(82), metadata_y - 2, _mm(160), metadata_y - 2)
    canvas.drawString(_mm(170), metadata_y, exam.exam_date or "")
    if not exam.exam_date:
        canvas.line(_mm(170), metadata_y - 2, _mm(200), metadata_y - 2)

    _draw_qr(canvas, qr_token, width - _mm(44), height - _mm(46))

    _draw_bubbles(canvas, template.coordinate_map_json, font_regular)


def _draw_markers(
    canvas: Canvas,
    marker_positions: dict | None = None,
    page_height_mm: float = A4_HEIGHT_MM,
) -> None:
    marker_size = _mm(6)
    positions = marker_positions or {
        "top_left": {"x_mm": 10, "y_mm": 10},
        "top_right": {"x_mm": 194, "y_mm": 10},
        "bottom_left": {"x_mm": 10, "y_mm": 281},
        "bottom_right": {"x_mm": 194, "y_mm": 281},
    }
    for marker in positions.values():
        canvas.rect(
            _mm(marker["x_mm"]),
            _mm(page_height_mm - marker["y_mm"] - 6),
            marker_size,
            marker_size,
            fill=1,
        )


def _draw_qr(canvas: Canvas, token: str, x: float, y: float) -> None:
    buffer = BytesIO()
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(token)
    qr.make(fit=True)
    qr.make_image(fill_color="black", back_color="white").save(buffer, format="PNG")
    buffer.seek(0)
    canvas.drawImage(ImageReader(buffer), x, y, width=_mm(28), height=_mm(28), mask="auto")


def _draw_bubbles(canvas: Canvas, coordinate_map: dict, font_name: str = "Helvetica") -> None:
    settings = get_effective_omr_settings()
    page_height_mm = coordinate_map.get("page_height_mm", A4_HEIGHT_MM)
    page_height = _mm(page_height_mm)
    questions_by_id = {question["question_id"]: question for question in coordinate_map["questions"]}
    _draw_question_group_tables(
        canvas, coordinate_map, questions_by_id, font_name, page_height, settings.grid_line_gray
    )

    canvas.setFont(font_name, 7)
    for question in coordinate_map["questions"]:
        first_choice = question["choices"][0]
        y = page_height - _mm(first_choice["y_mm"])
        label_gray = max(0.25, min(0.9, settings.choice_label_gray - 0.32))
        canvas.setFillColorRGB(label_gray, label_gray, label_gray)
        canvas.drawRightString(
            _mm(question.get("label_x_mm", 42)),
            y - 2.4,
            str(question["display_label"]),
        )
        for choice in question["choices"]:
            x = _mm(choice["x_mm"])
            radius = _mm(choice["radius_mm"])
            gray = settings.bubble_outline_gray
            canvas.setStrokeColorRGB(gray, gray, gray)
            # White interior so row shading never shows inside a bubble; the choice digit is
            # drawn on top. Filled detection still relies on dark pencil pixels inside.
            canvas.setFillColorRGB(1, 1, 1)
            canvas.circle(x, y, radius, stroke=1, fill=1)
            gray = settings.choice_label_gray
            canvas.setFillColorRGB(gray, gray, gray)
            canvas.drawCentredString(x, y - 2.1, choice["choice"])
    canvas.setFillColorRGB(0, 0, 0)
    canvas.setStrokeColorRGB(0, 0, 0)


def _draw_question_group_tables(
    canvas: Canvas,
    coordinate_map: dict,
    questions_by_id: dict[int, dict],
    font_name: str,
    page_height: float,
    grid_line_gray: float = 0.78,
) -> None:
    canvas.saveState()
    canvas.setStrokeColorRGB(grid_line_gray, grid_line_gray, grid_line_gray)
    canvas.setLineWidth(0.35)
    for group in coordinate_map.get("groups", []):
        questions = [
            questions_by_id[question_id]
            for question_id in group.get("question_ids", [])
            if question_id in questions_by_id
        ]
        if not questions:
            continue

        row_gap = group.get("row_gap_mm", 6.3)
        # Continuation blocks (a group that overflowed into the next column) have no label and
        # so draw no header band — rows start just below the top border, with no repeated
        # もんだい title. Bubble coordinates are unchanged either way.
        group_label = group.get("group_label", "")
        has_header = bool(group_label)
        first_y_mm = questions[0]["choices"][0]["y_mm"]
        last_y_mm = questions[-1]["choices"][0]["y_mm"]
        if has_header:
            top_mm = max(0, first_y_mm - row_gap * 1.15)
            header_bottom_mm = first_y_mm - row_gap * 0.45
        else:
            top_mm = max(0, first_y_mm - row_gap * 0.5)
            header_bottom_mm = top_mm
        bottom_mm = last_y_mm + row_gap * 0.5
        left_mm = min(question.get("label_x_mm", 0) for question in questions) - 7
        first_choice_x = min(question["choices"][0]["x_mm"] for question in questions)
        last_choice_x = max(question["choices"][-1]["x_mm"] for question in questions)
        choice_gap = group.get("choice_gap_mm", 9.4)
        right_mm = last_choice_x + choice_gap * 0.5
        label_divider_mm = first_choice_x - choice_gap * 0.5

        # Very light alternating row shading, painted first so it sits under the grid lines,
        # question numbers and (white-filled) bubbles. Every other answer row is shaded.
        canvas.setFillColorRGB(ROW_STRIPE_GRAY, ROW_STRIPE_GRAY, ROW_STRIPE_GRAY)
        for row_index, question in enumerate(questions):
            if row_index % 2 == 0:
                continue
            row_top_mm = (
                header_bottom_mm
                if row_index == 0
                else question["choices"][0]["y_mm"] - row_gap * 0.5
            )
            row_bottom_mm = (
                questions[row_index + 1]["choices"][0]["y_mm"] - row_gap * 0.5
                if row_index + 1 < len(questions)
                else bottom_mm
            )
            canvas.rect(
                _mm(left_mm),
                page_height - _mm(row_bottom_mm),
                _mm(right_mm - left_mm),
                _mm(row_bottom_mm - row_top_mm),
                stroke=0,
                fill=1,
            )
        canvas.setFillColorRGB(0, 0, 0)

        canvas.rect(
            _mm(left_mm),
            page_height - _mm(bottom_mm),
            _mm(right_mm - left_mm),
            _mm(bottom_mm - top_mm),
            stroke=1,
            fill=0,
        )
        if has_header:
            canvas.line(
                _mm(left_mm),
                page_height - _mm(header_bottom_mm),
                _mm(right_mm),
                page_height - _mm(header_bottom_mm),
            )
        canvas.line(
            _mm(label_divider_mm),
            page_height - _mm(header_bottom_mm),
            _mm(label_divider_mm),
            page_height - _mm(bottom_mm),
        )
        max_choices = max(len(question["choices"]) for question in questions)
        for boundary_index in range(1, max_choices):
            boundary_x = first_choice_x + (boundary_index - 0.5) * choice_gap
            canvas.line(
                _mm(boundary_x),
                page_height - _mm(header_bottom_mm),
                _mm(boundary_x),
                page_height - _mm(bottom_mm),
            )
        for question in questions[1:]:
            y_mm = question["choices"][0]["y_mm"] - row_gap * 0.5
            canvas.line(
                _mm(left_mm),
                page_height - _mm(y_mm),
                _mm(right_mm),
                page_height - _mm(y_mm),
            )

        if has_header:
            canvas.setFillColorRGB(0.48, 0.48, 0.48)
            canvas.setFont(font_name, 7)
            canvas.drawCentredString(
                _mm((left_mm + right_mm) / 2),
                page_height - _mm(top_mm + 3.7),
                group_label,
            )
            canvas.setFillColorRGB(0, 0, 0)
    canvas.restoreState()


def _build_zip(output_dir: Path, class_pdf_paths: list[str]) -> Path:
    zip_path = output_dir / "all_class_pdfs.zip"
    data_root = get_settings().data_root
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as archive:
        for relative_path in class_pdf_paths:
            absolute_path = data_root / relative_path
            archive.write(absolute_path, arcname=absolute_path.name)
    return zip_path


def get_generated_file(relative_path: str) -> Path:
    path = get_settings().data_root / relative_path
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(relative_path)
    return path


def latest_exam_download_path(db: Session, exam: MockExam) -> str | None:
    if exam.assessment_type == "peer_review":
        path = (
            get_settings().data_root
            / "generated_pdfs"
            / f"exam_{exam.id}"
            / f"{_safe_filename(exam.name)}_anonymous_peer_review.pdf"
        )
        return data_relative_path(path) if path.exists() else None
    instance = db.scalar(
        select(SheetInstance)
        .where(SheetInstance.exam_id == exam.id)
        .order_by(SheetInstance.created_at.desc())
    )
    if instance is None or instance.pdf_file_path is None:
        return None
    latest_template_ids = _latest_template_ids_by_sheet(exam)
    paths = sorted(
        {
            sheet_instance.pdf_file_path
            for sheet_instance in exam.sheet_instances
            if sheet_instance.pdf_file_path
            and (
                not latest_template_ids
                or sheet_instance.sheet_template_id
                in latest_template_ids.values()
            )
        }
    )
    if len(paths) == 1:
        return paths[0]
    zip_path = f"generated_pdfs/exam_{exam.id}/all_class_pdfs.zip"
    try:
        get_generated_file(zip_path)
    except FileNotFoundError:
        return None
    return zip_path


def latest_exam_zip(db: Session, exam: MockExam) -> str | None:
    path = latest_exam_download_path(db, exam)
    return path if path and path.lower().endswith(".zip") else None


def _latest_template_ids_by_sheet(exam: MockExam) -> dict[int, int]:
    latest: dict[int, SheetTemplate] = {}
    for template in exam.sheet_templates:
        current = latest.get(template.physical_sheet_id)
        if current is None or template.created_at > current.created_at:
            latest[template.physical_sheet_id] = template
    return {sheet_id: template.id for sheet_id, template in latest.items()}


def latest_sheet_generated_at(db: Session, exam: MockExam) -> datetime | None:
    if exam.assessment_type == "peer_review":
        from app.models import PeerReviewPage

        return db.scalar(
            select(PeerReviewPage.updated_at)
            .join(PeerReviewPage.form)
            .where(PeerReviewPage.form.has(exam_id=exam.id))
            .order_by(PeerReviewPage.updated_at.desc())
            .limit(1)
        )
    return db.scalar(
        select(SheetInstance.created_at)
        .where(SheetInstance.exam_id == exam.id)
        .order_by(SheetInstance.created_at.desc())
        .limit(1)
    )


def _safe_filename(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in ".-_" else "_" for char in value.strip())
    safe = re.sub(r"_+", "_", safe)
    return safe.strip("_") or "file"


def _exam_pdf_filename(exam: MockExam) -> str:
    date_part = exam.exam_date or "undated"
    return f"{date_part}_{_safe_filename(exam.name)}_answer_sheets.pdf"


def _mm(value: float) -> float:
    return value * POINTS_PER_MM
