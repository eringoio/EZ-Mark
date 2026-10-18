import cv2
import numpy as np

from app.services.alignment_service import align_page, canonical_size_px, marker_centers_mm
from app.services.scan_batch_service import _measure_fill

PAGE_W_MM = 297.0
PAGE_H_MM = 210.0
MARKERS = {
    "top_left": {"x_mm": 10, "y_mm": 10},
    "top_right": {"x_mm": 281, "y_mm": 10},
    "bottom_left": {"x_mm": 10, "y_mm": 194},
    "bottom_right": {"x_mm": 281, "y_mm": 194},
}
SCALE = 4  # px per mm for the synthetic source page


def _synthetic_page(markers: dict = MARKERS) -> np.ndarray:
    image = np.full((int(PAGE_H_MM * SCALE), int(PAGE_W_MM * SCALE)), 255, dtype=np.uint8)
    for marker in markers.values():
        x = int(marker["x_mm"] * SCALE)
        y = int(marker["y_mm"] * SCALE)
        size = int(6 * SCALE)
        image[y : y + size, x : x + size] = 0
    return image


def _drift(
    image: np.ndarray, angle_deg: float = 2.0, dx: int = 14, dy: int = -9, scale: float = 1.03
) -> np.ndarray:
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle_deg, scale)
    matrix[0, 2] += dx
    matrix[1, 2] += dy
    return cv2.warpAffine(image, matrix, (width, height), borderValue=255)


def test_align_page_recovers_drifted_scan() -> None:
    image = _synthetic_page()
    # Reference bubble at a known position away from any marker.
    bubble_mm = (120.0, 100.0)
    cv2.circle(
        image, (int(bubble_mm[0] * SCALE), int(bubble_mm[1] * SCALE)), int(2.2 * SCALE), 0, -1
    )

    result = align_page(_drift(image), MARKERS, PAGE_W_MM, PAGE_H_MM, dpi=300)

    assert result.status == "ok"
    assert set(result.detected_markers) == set(MARKERS)
    canonical_w, canonical_h = canonical_size_px(PAGE_W_MM, PAGE_H_MM, 300)
    assert result.image.shape[1] == canonical_w
    assert result.image.shape[0] == canonical_h
    # The bubble must land back on its canonical position.
    x_px = int(bubble_mm[0] / PAGE_W_MM * canonical_w)
    y_px = int(bubble_mm[1] / PAGE_H_MM * canonical_h)
    patch = result.image[y_px - 10 : y_px + 10, x_px - 10 : x_px + 10]
    assert (patch < 140).mean() > 0.5


def test_align_page_marker_centers_within_tolerance() -> None:
    image = _synthetic_page()
    result = align_page(image, MARKERS, PAGE_W_MM, PAGE_H_MM, dpi=300)

    assert result.status == "ok"
    for name, (cx_mm, cy_mm) in marker_centers_mm(MARKERS).items():
        detected_x, detected_y = result.detected_markers[name]
        assert abs(detected_x - cx_mm * SCALE) <= 2
        assert abs(detected_y - cy_mm * SCALE) <= 2


def test_align_page_three_markers_falls_back_to_affine() -> None:
    three = {name: pos for name, pos in MARKERS.items() if name != "bottom_right"}
    image = _synthetic_page(three)

    result = align_page(
        _drift(image, angle_deg=1.0, dx=6, dy=4, scale=1.0), MARKERS, PAGE_W_MM, PAGE_H_MM
    )

    assert result.status == "partial"
    assert len(result.detected_markers) == 3


def test_align_page_recovers_badly_shifted_scan_via_global_fallback() -> None:
    """A shift far beyond the local 10 mm search window must still align."""
    image = _synthetic_page()
    bubble_mm = (120.0, 100.0)
    cv2.circle(
        image, (int(bubble_mm[0] * SCALE), int(bubble_mm[1] * SCALE)), int(2.2 * SCALE), 0, -1
    )
    # Pure translation of ~20/15 mm onto a larger canvas (real scans have
    # margins): far beyond the 10 mm local search window.
    dx, dy = int(20 * SCALE), int(15 * SCALE)
    height, width = image.shape[:2]
    matrix = np.float32([[1, 0, dx], [0, 1, dy]])
    shifted = cv2.warpAffine(image, matrix, (width + dx, height + dy), borderValue=255)

    result = align_page(shifted, MARKERS, PAGE_W_MM, PAGE_H_MM, dpi=300)

    assert result.status == "ok"
    canonical_w, canonical_h = canonical_size_px(PAGE_W_MM, PAGE_H_MM, 300)
    x_px = int(bubble_mm[0] / PAGE_W_MM * canonical_w)
    y_px = int(bubble_mm[1] / PAGE_H_MM * canonical_h)
    patch = result.image[y_px - 10 : y_px + 10, x_px - 10 : x_px + 10]
    assert (patch < 140).mean() > 0.5


def test_global_fallback_does_not_invent_markers_on_blank_page() -> None:
    image = np.full((840, 1188), 255, dtype=np.uint8)
    # Filled bubbles all over must not be mistaken for corner markers.
    for x_mm in range(40, 260, 20):
        for y_mm in range(40, 180, 20):
            cv2.circle(image, (int(x_mm * 4), int(y_mm * 4)), int(2.2 * 4), 0, -1)

    result = align_page(image, MARKERS, PAGE_W_MM, PAGE_H_MM)

    assert result.status == "failed"
    assert result.image is image


def test_align_page_fails_without_markers() -> None:
    image = np.full((840, 1188), 255, dtype=np.uint8)

    result = align_page(image, MARKERS, PAGE_W_MM, PAGE_H_MM)

    assert result.status == "failed"
    assert result.image is image


def test_measure_fill_finds_locally_drifted_bubble() -> None:
    width, height = int(PAGE_W_MM * SCALE), int(PAGE_H_MM * SCALE)
    image = np.full((height, width), 255, dtype=np.uint8)
    # Bubble drawn 1 mm off its expected center.
    cv2.circle(image, (int(121.0 * SCALE), int(101.0 * SCALE)), int(2.2 * SCALE), 0, -1)
    choice = {"x_mm": 120.0, "y_mm": 100.0, "radius_mm": 2.2}
    coordinate_map = {"page_width_mm": PAGE_W_MM, "page_height_mm": PAGE_H_MM}

    fill = _measure_fill(image, width, height, choice, coordinate_map)

    assert fill > 0.6
