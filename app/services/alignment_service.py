"""Geometric alignment of scanned mark-sheet pages.

Generated sheets carry four 6x6 mm filled black corner markers whose top-left
positions (top-down mm coordinates) are stored in
``SheetTemplate.marker_positions_json``. This module detects those markers in a
scanned page and warps the page into the canonical coordinate space the bubble
coordinate map was generated for, so mm -> px scaling becomes exact.
"""

from dataclasses import dataclass

import cv2
import numpy as np

MARKER_SIZE_MM = 6.0
SEARCH_RADIUS_MM = 10.0
DEFAULT_DPI = 300
MM_PER_INCH = 25.4
# Detected marker quad geometry may deviate this much from the expected quad
# before the transform is rejected as a misdetection.
MAX_RELATIVE_DEVIATION = 0.15

MARKER_ORDER = ("top_left", "top_right", "bottom_right", "bottom_left")


@dataclass(frozen=True)
class AlignmentResult:
    image: np.ndarray
    status: str  # "ok" | "partial" | "failed"
    detected_markers: dict[str, tuple[float, float]]


def canonical_size_px(
    page_width_mm: float, page_height_mm: float, dpi: int = DEFAULT_DPI
) -> tuple[int, int]:
    return (
        int(round(page_width_mm / MM_PER_INCH * dpi)),
        int(round(page_height_mm / MM_PER_INCH * dpi)),
    )


def marker_centers_mm(marker_positions: dict) -> dict[str, tuple[float, float]]:
    """Stored positions are the top-left corner of each 6 mm square."""
    centers = {}
    for name, position in (marker_positions or {}).items():
        try:
            centers[name] = (
                float(position["x_mm"]) + MARKER_SIZE_MM / 2,
                float(position["y_mm"]) + MARKER_SIZE_MM / 2,
            )
        except (KeyError, TypeError, ValueError):
            continue
    return centers


def align_page(
    image: np.ndarray,
    marker_positions: dict,
    page_width_mm: float,
    page_height_mm: float,
    dpi: int = DEFAULT_DPI,
) -> AlignmentResult:
    """Warp ``image`` into canonical page space using corner markers.

    Returns the original image with status ``"failed"`` when fewer than three
    markers are found or the detected geometry is implausible.
    """
    expected_mm = marker_centers_mm(marker_positions)
    if len(expected_mm) < 3:
        return AlignmentResult(image=image, status="failed", detected_markers={})

    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    src_height, src_width = gray.shape[:2]
    scale_x = src_width / page_width_mm
    scale_y = src_height / page_height_mm
    search_radius = (SEARCH_RADIUS_MM * scale_x, SEARCH_RADIUS_MM * scale_y)
    expected_area = (MARKER_SIZE_MM * scale_x) * (MARKER_SIZE_MM * scale_y)
    expected_px = {
        name: (cx_mm * scale_x, cy_mm * scale_y) for name, (cx_mm, cy_mm) in expected_mm.items()
    }

    detected: dict[str, tuple[float, float]] = {}
    for name, center_px in expected_px.items():
        center = _find_marker(gray, center_px, search_radius, expected_area)
        if center is not None:
            detected[name] = center

    # Second pass: rotation/scale moves far-edge markers further than the
    # search window allows. Estimate a similarity transform from the markers
    # already found and re-search the missing ones at their predicted spots.
    missing = [name for name in expected_px if name not in detected]
    if missing and len(detected) >= 2:
        found_names = list(detected)
        src = np.float32([expected_px[n] for n in found_names])
        dst = np.float32([detected[n] for n in found_names])
        transform, _inliers = cv2.estimateAffinePartial2D(src, dst)
        if transform is not None:
            for name in missing:
                ex, ey = expected_px[name]
                predicted = (
                    transform[0, 0] * ex + transform[0, 1] * ey + transform[0, 2],
                    transform[1, 0] * ex + transform[1, 1] * ey + transform[1, 2],
                )
                center = _find_marker(gray, predicted, search_radius, expected_area)
                if center is not None:
                    detected[name] = center

    # Last resort for badly shifted/drifted scans: search the whole page for
    # solid black square candidates and fill in the still-missing corners.
    # Only adopted when the completed quad is geometrically plausible, so
    # normal scans and genuine failures are unaffected.
    if len(detected) < 4:
        augmented = _augment_with_global_candidates(gray, expected_px, expected_area, detected)
        if len(augmented) >= 4 and _plausible_quad(augmented, expected_mm):
            detected = augmented

    canonical_w, canonical_h = canonical_size_px(page_width_mm, page_height_mm, dpi)
    dst_scale = dpi / MM_PER_INCH
    if len(detected) >= 4 and _plausible_quad(detected, expected_mm):
        names = [n for n in MARKER_ORDER if n in detected][:4]
        src = np.float32([detected[n] for n in names])
        dst = np.float32(
            [(expected_mm[n][0] * dst_scale, expected_mm[n][1] * dst_scale) for n in names]
        )
        transform = cv2.getPerspectiveTransform(src, dst)
        warped = cv2.warpPerspective(
            image,
            transform,
            (canonical_w, canonical_h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(255, 255, 255) if image.ndim == 3 else 255,
        )
        return AlignmentResult(image=warped, status="ok", detected_markers=detected)

    if len(detected) >= 3 and _plausible_triangle(detected, expected_mm):
        names = [n for n in MARKER_ORDER if n in detected][:3]
        src = np.float32([detected[n] for n in names])
        dst = np.float32(
            [(expected_mm[n][0] * dst_scale, expected_mm[n][1] * dst_scale) for n in names]
        )
        transform = cv2.getAffineTransform(src, dst)
        warped = cv2.warpAffine(
            image,
            transform,
            (canonical_w, canonical_h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(255, 255, 255) if image.ndim == 3 else 255,
        )
        return AlignmentResult(image=warped, status="partial", detected_markers=detected)

    return AlignmentResult(image=image, status="failed", detected_markers=detected)


def _find_marker(
    gray: np.ndarray,
    expected_center: tuple[float, float],
    search_radius: tuple[float, float],
    expected_area: float,
) -> tuple[float, float] | None:
    height, width = gray.shape[:2]
    cx, cy = expected_center
    rx, ry = search_radius
    x_min = max(0, int(cx - rx))
    x_max = min(width, int(cx + rx))
    y_min = max(0, int(cy - ry))
    y_max = min(height, int(cy + ry))
    window = gray[y_min:y_max, x_min:x_max]
    if window.size == 0:
        return None

    _, binary = cv2.threshold(window, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel = np.ones((3, 3), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    contours, _hierarchy = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best: tuple[float, float] | None = None
    best_score = float("inf")
    max_distance = float(np.hypot(rx, ry))
    for contour in contours:
        area = cv2.contourArea(contour)
        if not (0.4 * expected_area <= area <= 2.5 * expected_area):
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if h == 0 or w == 0:
            continue
        aspect = w / h
        if not (0.6 <= aspect <= 1.6):
            continue
        extent = area / (w * h)
        if extent < 0.6:
            continue
        # Markers are solid black; QR finder patterns are squares with a white
        # ring inside. Require the inner half-size box to be almost fully dark.
        inner = binary[
            y + h // 4 : y + h - h // 4,
            x + w // 4 : x + w - w // 4,
        ]
        if inner.size == 0 or (inner > 0).mean() < 0.85:
            continue
        moments = cv2.moments(contour)
        if moments["m00"] == 0:
            continue
        marker_cx = moments["m10"] / moments["m00"] + x_min
        marker_cy = moments["m01"] / moments["m00"] + y_min
        distance = float(np.hypot(marker_cx - cx, marker_cy - cy))
        # Prefer close candidates of the expected size: a nearby title glyph or
        # stray mark should lose against the true marker further away.
        score = distance / max_distance + 0.5 * abs(float(np.log(area / expected_area)))
        if score < best_score:
            best_score = score
            best = (marker_cx, marker_cy)
    return best


# A drifted scan can move markers at most this fraction of the page diagonal
# away from their expected spot before the global fallback refuses the match.
_GLOBAL_FALLBACK_MAX_SHIFT = 0.15


def _augment_with_global_candidates(
    gray: np.ndarray,
    expected_px: dict[str, tuple[float, float]],
    expected_area: float,
    detected: dict[str, tuple[float, float]],
) -> dict[str, tuple[float, float]]:
    """Fill missing corner markers from a whole-page solid-square search."""
    candidates = _global_square_candidates(gray, expected_area)
    # Drop candidates that are the already-detected markers.
    marker_side = float(np.sqrt(expected_area))
    candidates = [
        candidate
        for candidate in candidates
        if all(
            np.hypot(candidate[0] - dx, candidate[1] - dy) > marker_side
            for dx, dy in detected.values()
        )
    ]
    if not candidates:
        return detected

    height, width = gray.shape[:2]
    max_distance = _GLOBAL_FALLBACK_MAX_SHIFT * float(np.hypot(width, height))
    missing = [name for name in expected_px if name not in detected]
    pairs = sorted(
        (
            (float(np.hypot(cx - expected_px[name][0], cy - expected_px[name][1])), name, (cx, cy))
            for name in missing
            for cx, cy in candidates
        ),
        key=lambda item: item[0],
    )
    augmented = dict(detected)
    used_candidates: set[tuple[float, float]] = set()
    for distance, name, center in pairs:
        if distance > max_distance:
            break
        if name in augmented or center in used_candidates:
            continue
        augmented[name] = center
        used_candidates.add(center)
    return augmented


def _global_square_candidates(gray: np.ndarray, expected_area: float) -> list[tuple[float, float]]:
    """Centers of solid black, square-ish blobs of roughly marker size.

    Filters are tighter than the local window search: filled answer bubbles
    are circles (extent ~0.785), so requiring extent >= 0.82 excludes them.
    """
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel = np.ones((3, 3), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    contours, _hierarchy = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    centers: list[tuple[float, float]] = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if not (0.6 * expected_area <= area <= 1.8 * expected_area):
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if h == 0 or w == 0:
            continue
        aspect = w / h
        if not (0.7 <= aspect <= 1.4):
            continue
        if area / (w * h) < 0.82:
            continue
        inner = binary[y + h // 4 : y + h - h // 4, x + w // 4 : x + w - w // 4]
        if inner.size == 0 or (inner > 0).mean() < 0.85:
            continue
        moments = cv2.moments(contour)
        if moments["m00"] == 0:
            continue
        centers.append((moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]))
    return centers


def _plausible_quad(
    detected: dict[str, tuple[float, float]],
    expected_mm: dict[str, tuple[float, float]],
) -> bool:
    names = [n for n in MARKER_ORDER if n in detected and n in expected_mm]
    if len(names) < 4:
        return False
    names = names[:4]
    points = np.float32([detected[n] for n in names])
    if not cv2.isContourConvex(points.reshape(-1, 1, 2)):
        return False
    return _side_ratios_match([detected[n] for n in names], [expected_mm[n] for n in names])


def _plausible_triangle(
    detected: dict[str, tuple[float, float]],
    expected_mm: dict[str, tuple[float, float]],
) -> bool:
    names = [n for n in MARKER_ORDER if n in detected and n in expected_mm][:3]
    if len(names) < 3:
        return False
    return _side_ratios_match([detected[n] for n in names], [expected_mm[n] for n in names])


def _side_ratios_match(
    detected_points: list[tuple[float, float]],
    expected_points: list[tuple[float, float]],
) -> bool:
    """Compare pairwise-distance ratios of the detected polygon to the expected
    one; absolute scale is unknown so only relative geometry is checked."""
    detected_distances = _pairwise_distances(detected_points)
    expected_distances = _pairwise_distances(expected_points)
    reference = max(expected_distances)
    if reference == 0:
        return False
    detected_reference = max(detected_distances)
    if detected_reference == 0:
        return False
    for d_detected, d_expected in zip(detected_distances, expected_distances, strict=True):
        normalized_detected = d_detected / detected_reference
        normalized_expected = d_expected / reference
        if abs(normalized_detected - normalized_expected) > MAX_RELATIVE_DEVIATION:
            return False
    return True


def _pairwise_distances(points: list[tuple[float, float]]) -> list[float]:
    distances = []
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            dx = points[i][0] - points[j][0]
            dy = points[i][1] - points[j][1]
            distances.append(float(np.hypot(dx, dy)))
    return distances
