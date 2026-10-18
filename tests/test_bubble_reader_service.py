from app.services.bubble_reader_service import classify_bubble_fills
from app.services.omr_settings_service import EffectiveOmrSettings


def test_clear_bubble_classifies_ok() -> None:
    result = classify_bubble_fills({"1": 0.1, "2": 0.6, "3": 0.12, "4": 0.08})

    assert result.detected_answer == "2"
    assert result.status == "ok"


def test_blank_bubble_classifies_blank() -> None:
    result = classify_bubble_fills({"1": 0.1, "2": 0.12, "3": 0.08, "4": 0.05})

    assert result.detected_answer is None
    assert result.status == "blank"


def test_multiple_marks_classifies_multiple() -> None:
    result = classify_bubble_fills({"1": 0.42, "2": 0.5, "3": 0.1, "4": 0.05})

    assert result.detected_answer is None
    assert result.status == "multiple_mark"


def test_close_top_two_classifies_unclear() -> None:
    result = classify_bubble_fills({"1": 0.1, "2": 0.42, "3": 0.36, "4": 0.05})

    assert result.detected_answer is None
    assert result.status == "multiple_mark"


def test_custom_thresholds_affect_classification() -> None:
    fills = {"1": 0.1, "2": 0.32, "3": 0.12, "4": 0.08}

    standard = classify_bubble_fills(fills)
    light = classify_bubble_fills(
        fills,
        settings=EffectiveOmrSettings(mark_threshold=0.30, weak_threshold=0.20),
    )

    assert standard.status == "unclear"
    assert light.detected_answer == "2"
    assert light.status == "ok"
