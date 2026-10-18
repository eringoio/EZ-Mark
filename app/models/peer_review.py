from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Competency(Base):
    __tablename__ = "competencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("mock_exams.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    # Optional furigana/reading shown small above the competency name on printed sheets.
    name_furigana: Mapped[str | None] = mapped_column(String, nullable=True)
    # Optional shorter title printed on the sheet in place of the full ``name`` when the
    # full sentence is too long to fit a column cleanly. The builder keeps editing ``name``;
    # only the printed header (and furigana above it) uses ``short_label`` when it is set.
    short_label: Mapped[str | None] = mapped_column(String, nullable=True)
    # Optional per-segment ruby furigana: a JSON list of ``{"base_text": ..., "reading": ...}``
    # entries. Each reading is drawn small above just its base run in the printed title
    # (e.g. 時間内→じかんない). Positions are resolved at layout time by locating each base run
    # in the title, so they survive title edits. ``name_furigana`` remains the whole-title
    # fallback used when no per-segment ruby is set. Per-segment ruby is suppressed when a
    # ``short_label`` is in effect (its runs index into ``name``, not the short label).
    name_ruby_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_score: Mapped[int] = mapped_column(Integer, default=1)
    max_score: Mapped[int] = mapped_column(Integer, default=5)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    exam = relationship("MockExam", back_populates="competencies")


class PeerReviewForm(Base):
    __tablename__ = "peer_review_forms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("mock_exams.id"), index=True)
    title: Mapped[str] = mapped_column(String)
    anonymous: Mapped[bool] = mapped_column(Boolean, default=True)
    score_min: Mapped[int] = mapped_column(Integer, default=1)
    score_max: Mapped[int] = mapped_column(Integer, default=5)
    # Reusable layout setting: how many target rows fit on one printed page. Copied
    # from a peer-review template to each session; drives auto-pagination.
    students_per_page: Mapped[int] = mapped_column(Integer, default=15)
    # "auto" lets the layout service pick portrait/landscape; "portrait"/"landscape" force it.
    page_orientation_mode: Mapped[str] = mapped_column(String, default="auto")
    # Vertical spacing density: "standard" (legacy geometry), "compact" (fit more rows,
    # e.g. 20 with furigana), or "relaxed". Drives the layout service density profile.
    layout_density: Mapped[str] = mapped_column(String, default="standard")
    combine_groups: Mapped[bool] = mapped_column(Boolean, default=False)
    missing_group_policy: Mapped[str] = mapped_column(String, default="normalize_available")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    exam = relationship("MockExam")
    targets = relationship(
        "PeerReviewTarget",
        back_populates="form",
        cascade="all, delete-orphan",
        order_by="PeerReviewTarget.display_order",
    )
    rater_groups = relationship(
        "PeerReviewRaterGroup",
        back_populates="form",
        cascade="all, delete-orphan",
        order_by="PeerReviewRaterGroup.display_order",
    )
    pages = relationship(
        "PeerReviewPage",
        back_populates="form",
        cascade="all, delete-orphan",
        order_by="PeerReviewPage.page_no",
    )


class PeerReviewTarget(Base):
    __tablename__ = "peer_review_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    form_id: Mapped[int] = mapped_column(ForeignKey("peer_review_forms.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    display_label: Mapped[str] = mapped_column(String)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    form = relationship("PeerReviewForm", back_populates="targets")
    student = relationship("Student")


class PeerReviewRaterGroup(Base):
    __tablename__ = "peer_review_rater_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    form_id: Mapped[int] = mapped_column(ForeignKey("peer_review_forms.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    weight_percent: Mapped[float] = mapped_column(Float, default=100)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    form = relationship("PeerReviewForm", back_populates="rater_groups")


class PeerReviewPage(Base):
    __tablename__ = "peer_review_pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    form_id: Mapped[int] = mapped_column(ForeignKey("peer_review_forms.id"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String)
    qr_token: Mapped[str] = mapped_column(String, unique=True, index=True)
    coordinate_map_json: Mapped[dict] = mapped_column(JSON, default=dict)
    target_rows_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    form = relationship("PeerReviewForm", back_populates="pages")


class PeerReviewBatch(Base):
    __tablename__ = "peer_review_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    form_id: Mapped[int] = mapped_column(ForeignKey("peer_review_forms.id"), index=True)
    rater_group_id: Mapped[int] = mapped_column(
        ForeignKey("peer_review_rater_groups.id"), index=True
    )
    uploaded_file_name: Mapped[str] = mapped_column(String)
    uploaded_file_path: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="uploaded", index=True)
    total_pages: Mapped[int] = mapped_column(Integer, default=0)
    detected_pages: Mapped[int] = mapped_column(Integer, default=0)
    problem_pages: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    form = relationship("PeerReviewForm")
    rater_group = relationship("PeerReviewRaterGroup")


class PeerReviewSubmissionPage(Base):
    __tablename__ = "peer_review_submission_pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("peer_review_batches.id"), index=True)
    peer_review_page_id: Mapped[int] = mapped_column(ForeignKey("peer_review_pages.id"), index=True)
    page_no_in_upload: Mapped[int] = mapped_column(Integer)
    qr_status: Mapped[str] = mapped_column(String, default="ok")
    alignment_status: Mapped[str] = mapped_column(String, default="ok")
    review_status: Mapped[str] = mapped_column(String, default="ok", index=True)
    image_path: Mapped[str] = mapped_column(String)
    aligned_image_path: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    batch = relationship("PeerReviewBatch")
    peer_review_page = relationship("PeerReviewPage")


class PeerReviewRating(Base):
    __tablename__ = "peer_review_ratings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("mock_exams.id"), index=True)
    form_id: Mapped[int] = mapped_column(ForeignKey("peer_review_forms.id"), index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("peer_review_batches.id"), index=True)
    rater_group_id: Mapped[int] = mapped_column(
        ForeignKey("peer_review_rater_groups.id"), index=True
    )
    submission_page_id: Mapped[int] = mapped_column(
        ForeignKey("peer_review_submission_pages.id"), index=True
    )
    peer_review_page_id: Mapped[int] = mapped_column(ForeignKey("peer_review_pages.id"), index=True)
    target_student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    competency_id: Mapped[int] = mapped_column(ForeignKey("competencies.id"), index=True)
    detected_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String, default="ok", index=True)
    reviewed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    target_student = relationship("Student")
    competency = relationship("Competency")
    rater_group = relationship("PeerReviewRaterGroup")
    batch = relationship("PeerReviewBatch")
    peer_review_page = relationship("PeerReviewPage")


class PeerReviewGroupResult(Base):
    __tablename__ = "peer_review_group_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("mock_exams.id"), index=True)
    form_id: Mapped[int] = mapped_column(ForeignKey("peer_review_forms.id"), index=True)
    rater_group_id: Mapped[int] = mapped_column(
        ForeignKey("peer_review_rater_groups.id"), index=True
    )
    target_student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    rating_count: Mapped[int] = mapped_column(Integer, default=0)
    competency_breakdown_json: Mapped[dict] = mapped_column(JSON, default=dict)
    group_average_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PeerReviewFinalResult(Base):
    __tablename__ = "peer_review_final_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("mock_exams.id"), index=True)
    form_id: Mapped[int] = mapped_column(ForeignKey("peer_review_forms.id"), index=True)
    target_student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    final_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    combine_groups: Mapped[bool] = mapped_column(Boolean, default=False)
    group_breakdown_json: Mapped[dict] = mapped_column(JSON, default=dict)
    competency_breakdown_json: Mapped[dict] = mapped_column(JSON, default=dict)
    total_rating_count: Mapped[int] = mapped_column(Integer, default=0)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    student = relationship("Student")

    @property
    def final_average_score(self) -> float | None:
        return self.final_score

    @property
    def received_score_count(self) -> int:
        return self.total_rating_count
