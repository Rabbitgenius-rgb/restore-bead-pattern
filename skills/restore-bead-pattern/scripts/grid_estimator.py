"""Automatic logical-grid estimation for axis-aligned dark-edge pixel crafts.

The estimator is intentionally independent from pattern reconstruction and file
I/O.  It accepts an RGB image in memory and returns JSON-serializable candidate
registrations.  Pillow and NumPy are the only runtime dependencies.

Supported inputs are photographed bead, tufted, cross-stitch, or other pixel
works with a light border/background and enough dark structure to reveal the
latent square lattice.  Rotation and perspective correction belong upstream.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from PIL import Image, ImageFilter


_DARK_THRESHOLDS = (0.30, 0.38, 0.46, 0.54)
_ALGORITHM = "dark-oriented-edge circular-coherence v1"


def _as_rgb_float(image_rgb: Any) -> np.ndarray:
    """Normalize a Pillow image or array-like value to finite H×W×3 float RGB."""
    if isinstance(image_rgb, Image.Image):
        array = np.asarray(image_rgb.convert("RGBA"), dtype=np.float32) / 255.0
    else:
        try:
            source = np.asarray(image_rgb)
        except Exception as exc:  # pragma: no cover - defensive API boundary
            raise TypeError("image_rgb must be a Pillow image or an array-like image") from exc
        if source.ndim == 2:
            source = np.repeat(source[..., None], 3, axis=2)
        if source.ndim != 3 or source.shape[2] not in (3, 4):
            raise ValueError("image_rgb must have shape H×W×3 or H×W×4")
        if np.issubdtype(source.dtype, np.integer):
            observed_min = int(np.min(source))
            observed_max = int(np.max(source))
            if observed_min < 0:
                raise ValueError("integer image_rgb values must be non-negative")
            if observed_max <= 1:
                scale = 1.0
            elif observed_max <= 255:
                scale = 255.0
            elif observed_max <= 65535:
                scale = 65535.0
            else:
                raise ValueError("integer image_rgb must use an 8-bit or 16-bit value range")
            array = source.astype(np.float32) / scale
        else:
            array = source.astype(np.float32)
            finite = array[np.isfinite(array)]
            if finite.size == 0:
                raise ValueError("image_rgb contains no finite pixels")
            observed_max = float(np.max(finite))
            if observed_max > 1.5:
                if observed_max <= 255.0 + 1e-6:
                    array /= 255.0
                else:
                    raise ValueError("floating image_rgb must use either 0..1 or 0..255 values")

    if array.shape[0] < 64 or array.shape[1] < 64:
        raise ValueError("image_rgb is too small for reliable grid estimation (minimum 64×64 px)")
    if not np.all(np.isfinite(array)):
        raise ValueError("image_rgb contains NaN or infinite values")
    if float(np.min(array)) < -1e-6 or float(np.max(array)) > 1.0 + 1e-6:
        raise ValueError("image_rgb values fall outside the supported 0..1 / 0..255 range")
    array = np.clip(array, 0.0, 1.0)

    if array.shape[2] == 4:
        alpha = array[..., 3:4]
        array = array[..., :3] * alpha + (1.0 - alpha)
    return array[..., :3]


def _smooth_rgb(rgb: np.ndarray, radius: float) -> np.ndarray:
    uint8 = np.asarray(np.floor(np.clip(rgb, 0.0, 1.0) * 255.0 + 0.5), dtype=np.uint8)
    image = Image.fromarray(uint8, mode="RGB")
    return np.asarray(image.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32) / 255.0


def _luminance(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722


def _background_normalize(luminance: np.ndarray) -> tuple[np.ndarray, float]:
    height, width = luminance.shape
    band = max(2, round(min(height, width) * 0.02))
    border = np.concatenate(
        (
            luminance[:band, :].ravel(),
            luminance[-band:, :].ravel(),
            luminance[:, :band].ravel(),
            luminance[:, -band:].ravel(),
        )
    )
    background = float(np.median(border))
    if background < 0.25:
        raise ValueError(
            "grid estimation requires a light border/background; the detected border is too dark"
        )
    return luminance / background, background


def _long_axis_support(mask: np.ndarray, axis: int, radius: int) -> np.ndarray:
    """Count true pixels in an oriented local window without requiring SciPy."""
    padding = [(0, 0)] * mask.ndim
    padding[axis] = (radius, radius)
    padded = np.pad(mask.astype(np.int16), padding, mode="constant")
    cumulative = np.cumsum(padded, axis=axis, dtype=np.int32)
    zero_shape = list(cumulative.shape)
    zero_shape[axis] = 1
    cumulative = np.concatenate((np.zeros(zero_shape, dtype=np.int32), cumulative), axis=axis)

    low = [slice(None)] * mask.ndim
    high = [slice(None)] * mask.ndim
    low[axis] = slice(0, mask.shape[axis])
    high[axis] = slice(radius * 2 + 1, radius * 2 + 1 + mask.shape[axis])
    return cumulative[tuple(high)] - cumulative[tuple(low)]


def _oriented_edge_projections(
    luminance: np.ndarray, threshold: float, support_radius: int
) -> tuple[np.ndarray, np.ndarray, float, float]:
    dark = luminance < threshold
    vertical = dark[:, 1:] != dark[:, :-1]
    horizontal = dark[1:, :] != dark[:-1, :]

    vertical_support = _long_axis_support(vertical, axis=0, radius=support_radius)
    horizontal_support = _long_axis_support(horizontal, axis=1, radius=support_radius)
    support_floor = max(1, support_radius // 2)
    vertical_weight = vertical * np.maximum(vertical_support - support_floor, 0)
    horizontal_weight = horizontal * np.maximum(horizontal_support - support_floor, 0)

    x_projection = vertical_weight.sum(axis=0).astype(np.float64)
    y_projection = horizontal_weight.sum(axis=1).astype(np.float64)
    return x_projection, y_projection, float(x_projection.sum()), float(y_projection.sum())


def _ensemble_projections(
    luminance: np.ndarray, support_radius: int
) -> tuple[np.ndarray, np.ndarray, list[dict[str, float]]]:
    x_members: list[np.ndarray] = []
    y_members: list[np.ndarray] = []
    masses: list[dict[str, float]] = []
    for threshold in _DARK_THRESHOLDS:
        px, py, x_mass, y_mass = _oriented_edge_projections(
            luminance, threshold, support_radius
        )
        if x_mass <= 0.0 or y_mass <= 0.0:
            continue
        x_members.append(px / x_mass)
        y_members.append(py / y_mass)
        masses.append(
            {"threshold": float(threshold), "x_edge_mass": x_mass, "y_edge_mass": y_mass}
        )
    if len(x_members) < 2:
        raise ValueError(
            "insufficient continuous dark edges to estimate a grid; provide a clearer, axis-aligned image"
        )
    return np.mean(x_members, axis=0), np.mean(y_members, axis=0), masses


def _dominant_axis_interval(
    projection: np.ndarray, minimum_support: int, maximum_gap: int
) -> tuple[int, int]:
    """Return the highest-mass run, bridging only short internal gaps."""
    active = np.flatnonzero(projection >= minimum_support)
    if active.size == 0:
        active = np.flatnonzero(projection > 0)
    if active.size == 0:
        raise ValueError("no stable dark material interval was found")

    runs: list[tuple[int, int]] = []
    start = int(active[0])
    previous = start
    for coordinate_value in active[1:]:
        coordinate = int(coordinate_value)
        if coordinate - previous > maximum_gap + 1:
            runs.append((start, previous))
            start = coordinate
        previous = coordinate
    runs.append((start, previous))

    def run_score(run: tuple[int, int]) -> float:
        start_value, end_value = run
        mass = float(projection[start_value : end_value + 1].sum())
        return mass * math.sqrt(float(end_value - start_value + 1))

    return max(runs, key=run_score)


def _robust_dark_bbox(
    luminance: np.ndarray, support_radius: int
) -> tuple[tuple[float, float, float, float], list[list[float]]]:
    height, width = luminance.shape
    boxes: list[tuple[float, float, float, float]] = []
    for threshold in _DARK_THRESHOLDS:
        dark = luminance < threshold
        if int(dark.sum()) < max(64, round(dark.size * 0.0001)):
            continue
        x_projection = dark.sum(axis=0)
        y_projection = dark.sum(axis=1)
        x_interval = _dominant_axis_interval(
            x_projection,
            minimum_support=max(2, round(height * 0.002)),
            maximum_gap=max(3, support_radius * 2),
        )
        y_interval = _dominant_axis_interval(
            y_projection,
            minimum_support=max(2, round(width * 0.002)),
            maximum_gap=max(3, support_radius * 2),
        )
        boxes.append(
            (
                float(x_interval[0]),
                float(y_interval[0]),
                float(x_interval[1]),
                float(y_interval[1]),
            )
        )
    if len(boxes) < 2:
        raise ValueError(
            "no stable dark craft structure was found; crop the subject or provide manual grid settings"
        )
    median = np.median(np.asarray(boxes, dtype=np.float64), axis=0)
    left, top, right, bottom = (float(value) for value in median)
    if right <= left or bottom <= top:
        raise ValueError("the detected dark-material bounds are degenerate")
    return (left, top, right, bottom), [list(box) for box in boxes]


def _coherence_and_phase(signal: np.ndarray, pitch: float) -> tuple[float, float]:
    coordinates = np.arange(signal.size, dtype=np.float64)
    phasor = np.sum(signal * np.exp(2j * np.pi * coordinates / pitch))
    coherence = float(abs(phasor) / (float(signal.sum()) + 1e-12))
    phase = float((np.angle(phasor) / (2.0 * np.pi) * pitch) % pitch)
    return coherence, phase


def _pitch_measure(x_projection: np.ndarray, y_projection: np.ndarray, pitch: float) -> dict[str, float]:
    x_coherence, x_phase = _coherence_and_phase(x_projection, pitch)
    y_coherence, y_phase = _coherence_and_phase(y_projection, pitch)
    return {
        "pitch": float(pitch),
        "score": float(math.sqrt(max(0.0, x_coherence * y_coherence))),
        "x_coherence": x_coherence,
        "y_coherence": y_coherence,
        "x_phase": x_phase,
        "y_phase": y_phase,
    }


def _local_peaks(rows: list[dict[str, float]]) -> list[dict[str, float]]:
    peaks: list[dict[str, float]] = []
    for index in range(1, len(rows) - 1):
        if rows[index]["score"] >= rows[index - 1]["score"] and rows[index]["score"] >= rows[index + 1]["score"]:
            peaks.append(rows[index])
    return sorted(peaks, key=lambda row: row["score"], reverse=True)


def _refine_peak(
    x_projection: np.ndarray,
    y_projection: np.ndarray,
    seed: float,
    coarse_step: float,
    low: float,
    high: float,
) -> dict[str, float]:
    radius = coarse_step * 2.5
    fine_step = max(0.0025, coarse_step / 20.0)
    start = max(low, seed - radius)
    stop = min(high, seed + radius)
    rows = [
        _pitch_measure(x_projection, y_projection, float(pitch))
        for pitch in np.arange(start, stop + fine_step * 0.5, fine_step)
    ]
    return max(rows, key=lambda row: row["score"])


def _material_cell_count(span_pixels: float, pitch: float) -> int:
    # Dark threshold contours sit slightly inside fuzzy physical edges.  The
    # 0.1-cell correction represents roughly 0.05 cell of erosion per side.
    return max(1, int(math.floor(span_pixels / pitch + 0.1 + 0.5)))


def _validate_bounds(
    image_shape: tuple[int, int, int], pitch_min: float | None, pitch_max: float | None
) -> tuple[float, float]:
    height, width = image_shape[:2]
    default_low = max(3.0, min(height, width) / 260.0)
    default_high = min(height, width) / 8.0
    low = default_low if pitch_min is None else float(pitch_min)
    high = default_high if pitch_max is None else float(pitch_max)
    if not math.isfinite(low) or not math.isfinite(high) or low <= 1.0 or high <= low:
        raise ValueError("pitch_min and pitch_max must be finite values with 1 < pitch_min < pitch_max")
    if high - low < 0.5:
        raise ValueError("pitch search interval is too narrow; use a range of at least 0.5 px")
    return low, high


def estimate_grid(
    image_rgb: Any,
    pitch_min: float | None = None,
    pitch_max: float | None = None,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Return likely latent square-grid registrations for an in-memory RGB image.

    Args:
        image_rgb: Pillow image or H×W×3/4 array-like value. Integer arrays use
            their dtype range; float arrays may use 0..1 or 0..255.
        pitch_min: Optional inclusive lower search bound in source-image pixels.
        pitch_max: Optional inclusive upper search bound in source-image pixels.
        top_k: Maximum number of distinct local-pitch candidates to return.

    Returns:
        A best-first list of JSON-serializable dictionaries. Each dictionary has
        ``pitch``, ``origin`` (tight subject-grid top-left), ``cols``, ``rows``,
        ``confidence`` (heuristic 0..1), and ``diagnostics``.

    Raises:
        TypeError: Input cannot be interpreted as an image.
        ValueError: Input/range is invalid or the image lacks sufficient
            axis-aligned dark-edge evidence. Failures are never silently guessed.
    """
    if isinstance(top_k, bool) or not isinstance(top_k, (int, np.integer)) or not 1 <= int(top_k) <= 20:
        raise ValueError("top_k must be an integer from 1 through 20")
    top_k = int(top_k)

    rgb = _as_rgb_float(image_rgb)
    low, high = _validate_bounds(rgb.shape, pitch_min, pitch_max)
    height, width = rgb.shape[:2]
    blur_radius = max(1.5, min(height, width) / 440.0)
    support_radius = max(2, round(min(height, width) / 300.0))
    smooth = _smooth_rgb(rgb, blur_radius)
    normalized_luminance, background_luminance = _background_normalize(_luminance(smooth))

    dark_fraction = float(np.mean(normalized_luminance < _DARK_THRESHOLDS[-1]))
    if dark_fraction < 0.0001:
        raise ValueError("too few dark pixels were found to reveal a logical grid")
    if dark_fraction > 0.65:
        raise ValueError(
            "dark pixels cover most of the image; a light-background dark-edge estimator is not applicable"
        )

    x_projection, y_projection, edge_masses = _ensemble_projections(
        normalized_luminance, support_radius
    )
    bbox, threshold_boxes = _robust_dark_bbox(normalized_luminance, support_radius)

    coarse_step = max(0.05, min(0.25, (high - low) / 1200.0))
    coarse_rows = [
        _pitch_measure(x_projection, y_projection, float(pitch))
        for pitch in np.arange(low, high + coarse_step * 0.5, coarse_step)
    ]
    peaks = _local_peaks(coarse_rows)
    if not peaks:
        raise ValueError("no interior pitch peak was found; broaden the search bounds or correct the image")

    global_best = max(coarse_rows, key=lambda row: row["score"])
    interior_best = peaks[0]
    boundary_distance = min(global_best["pitch"] - low, high - global_best["pitch"])
    if boundary_distance <= coarse_step * 1.5 and global_best["score"] >= interior_best["score"] * 0.98:
        raise ValueError(
            "the strongest pitch lies on a search boundary; broaden pitch_min/pitch_max instead of accepting it"
        )

    # Keep at least one unreturned runner-up so confidence does not change merely
    # because the caller requests a smaller top_k.
    candidate_pool_limit = max(top_k + 1, 6)
    selected_seeds: list[dict[str, float]] = []
    for peak in peaks:
        minimum_separation = max(0.5, peak["pitch"] * 0.03)
        if all(abs(peak["pitch"] - kept["pitch"]) >= minimum_separation for kept in selected_seeds):
            selected_seeds.append(peak)
        if len(selected_seeds) >= candidate_pool_limit * 3:
            break

    refined = [
        _refine_peak(
            x_projection,
            y_projection,
            seed["pitch"],
            coarse_step,
            low,
            high,
        )
        for seed in selected_seeds
    ]
    refined.sort(key=lambda row: row["score"], reverse=True)

    distinct: list[dict[str, float]] = []
    for row in refined:
        if all(abs(row["pitch"] - kept["pitch"]) >= max(0.5, row["pitch"] * 0.025) for kept in distinct):
            distinct.append(row)
        if len(distinct) >= candidate_pool_limit:
            break
    if not distinct:
        raise ValueError("pitch candidates collapsed during refinement")

    best_score = distinct[0]["score"]
    if best_score < 0.06 or min(distinct[0]["x_coherence"], distinct[0]["y_coherence"]) < 0.04:
        raise ValueError(
            "dark edges do not show a strong shared x/y lattice; supply manual grid settings or a clearer image"
        )

    left, top, right, bottom = bbox
    span_x = right - left + 1.0
    span_y = bottom - top + 1.0
    center_x = (left + right) * 0.5
    center_y = (top + bottom) * 0.5

    results: list[dict[str, Any]] = []
    for rank, row in enumerate(distinct[:top_k], start=1):
        pitch = row["pitch"]
        cols = _material_cell_count(span_x, pitch)
        rows = _material_cell_count(span_y, pitch)
        if cols < 3 or rows < 3:
            continue
        origin_x = center_x - cols * pitch * 0.5
        origin_y = center_y - rows * pitch * 0.5

        next_score = distinct[rank]["score"] if rank < len(distinct) else 0.0
        peak_gap = max(0.0, (row["score"] - next_score) / (row["score"] + 1e-12))
        axis_balance = min(row["x_coherence"], row["y_coherence"]) / (
            max(row["x_coherence"], row["y_coherence"]) + 1e-12
        )
        absolute_strength = min(1.0, row["score"] / 0.35)
        relative_score = row["score"] / (best_score + 1e-12)
        confidence = relative_score * (
            0.45 * absolute_strength + 0.35 * axis_balance + 0.20 * peak_gap
        )
        confidence = float(max(0.0, min(1.0, confidence)))

        results.append(
            {
                "pitch": float(pitch),
                "origin": [float(origin_x), float(origin_y)],
                "cols": int(cols),
                "rows": int(rows),
                "confidence": confidence,
                "diagnostics": {
                    "algorithm": _ALGORITHM,
                    "rank": int(rank),
                    "pitch_score": float(row["score"]),
                    "x_coherence": float(row["x_coherence"]),
                    "y_coherence": float(row["y_coherence"]),
                    "x_edge_phase_mod_pitch": float(row["x_phase"]),
                    "y_edge_phase_mod_pitch": float(row["y_phase"]),
                    "relative_score": float(relative_score),
                    "peak_gap": float(peak_gap),
                    "axis_balance": float(axis_balance),
                    "absolute_strength": float(absolute_strength),
                    "confidence_is_heuristic": True,
                    "origin_method": "integer grid centered on robust dark-material bbox",
                    "subject_bbox_px": [left, top, right, bottom],
                    "subject_span_cells_float": [float(span_x / pitch), float(span_y / pitch)],
                    "threshold_bboxes_px": threshold_boxes,
                    "image_px": [int(width), int(height)],
                    "background_luminance": float(background_luminance),
                    "dark_fraction": dark_fraction,
                    "blur_radius_px": float(blur_radius),
                    "edge_support_radius_px": int(support_radius),
                    "dark_thresholds": [float(value) for value in _DARK_THRESHOLDS],
                    "edge_masses": edge_masses,
                    "pitch_search_px": [float(low), float(high)],
                    "coarse_pitch_step_px": float(coarse_step),
                    "assumptions": [
                        "axis-aligned grid",
                        "square logical cells",
                        "light border/background",
                        "sufficient continuous dark structure",
                    ],
                },
            }
        )

    if not results:
        raise ValueError("pitch peaks were found, but none produced a plausible grid of at least 3×3 cells")
    return results


__all__ = ["estimate_grid"]
