from dataclasses import dataclass

from app.services.omr_settings_service import EffectiveOmrSettings


@dataclass(frozen=True)
class BubbleClassification:
    detected_answer: str | None
    status: str
    confidence: float | None


def classify_bubble_fills(
    fills: dict[str, float],
    mark_threshold: float = 0.35,
    weak_threshold: float = 0.25,
    min_gap_between_top_two: float = 0.12,
    settings: EffectiveOmrSettings | None = None,
) -> BubbleClassification:
    if settings is not None:
        mark_threshold = settings.mark_threshold
        weak_threshold = settings.weak_threshold
        min_gap_between_top_two = settings.min_gap_between_top_two
    if not fills:
        return BubbleClassification(None, "invalid", None)
    ranked = sorted(fills.items(), key=lambda item: item[1], reverse=True)
    top_choice, top_fill = ranked[0]
    second_fill = ranked[1][1] if len(ranked) > 1 else 0
    confidence = top_fill - second_fill
    marked = [choice for choice, fill in fills.items() if fill >= mark_threshold]
    if top_fill < weak_threshold:
        return BubbleClassification(None, "blank", confidence)
    if len(marked) > 1:
        return BubbleClassification(None, "multiple_mark", confidence)
    if top_fill >= mark_threshold and confidence < min_gap_between_top_two:
        return BubbleClassification(top_choice, "unclear", confidence)
    if top_fill >= mark_threshold:
        return BubbleClassification(top_choice, "ok", confidence)
    return BubbleClassification(top_choice, "unclear", confidence)
