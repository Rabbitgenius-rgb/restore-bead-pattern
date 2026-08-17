"""Place a recovered native cell matrix on a Wenzhou 52 or 78 mold.

This module deliberately does no image sampling, resizing, or grid inference.
It selects a supported square mold from native pitch evidence, then copies the
``content_bbox`` cells unchanged onto the selected board.  Mold-selection
confidence is separate from the upstream grid confidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np


SUPPORTED_MOLD_SIZES = (52, 78)


class MoldCapacityError(ValueError):
    """Raised when a native content matrix cannot fit a requested mold."""


@dataclass(frozen=True)
class MoldResult:
    """A board matrix plus JSON-safe selection and placement diagnostics."""

    board_labels: np.ndarray
    board_confidence: np.ndarray
    metadata: dict[str, Any]
    candidates: list[dict[str, Any]]


def _finite_number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _spec_value(spec: Any, names: Sequence[str]) -> Any:
    if isinstance(spec, Mapping):
        for name in names:
            if name in spec:
                return spec[name]
    else:
        for name in names:
            if hasattr(spec, name):
                return getattr(spec, name)
    joined = " or ".join(names)
    raise ValueError(f"grid_spec is missing {joined}")


def _grid_scalars(grid_spec: Any) -> tuple[int, int, float, float, float]:
    columns = int(_spec_value(grid_spec, ("columns", "cols")))
    rows = int(_spec_value(grid_spec, ("rows",)))
    pitch = _finite_number(_spec_value(grid_spec, ("pitch", "pitch_px", "cell_size")), "pitch")
    origin_x = _finite_number(
        _spec_value(grid_spec, ("origin_x", "origin_x_px")), "origin_x"
    )
    origin_y = _finite_number(
        _spec_value(grid_spec, ("origin_y", "origin_y_px")), "origin_y"
    )
    if columns < 1 or rows < 1:
        raise ValueError("grid_spec rows and columns must be positive")
    if pitch <= 0:
        raise ValueError("grid_spec pitch must be positive")
    return columns, rows, pitch, origin_x, origin_y


def _bbox_tuple(content_bbox: Any) -> tuple[int, int, int, int]:
    if isinstance(content_bbox, Mapping):
        try:
            values = (
                content_bbox["left"],
                content_bbox["top"],
                content_bbox["right_exclusive"],
                content_bbox["bottom_exclusive"],
            )
        except KeyError as exc:
            raise ValueError(
                "content_bbox must contain left, top, right_exclusive, and bottom_exclusive"
            ) from exc
    else:
        try:
            values = tuple(content_bbox)
        except TypeError as exc:
            raise ValueError("content_bbox must be a mapping or four-item sequence") from exc
        if len(values) != 4:
            raise ValueError("content_bbox sequence must contain exactly four items")
    try:
        left, top, right, bottom = (int(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise ValueError("content_bbox coordinates must be integers") from exc
    return left, top, right, bottom


def _parse_mode(mode: str | int) -> tuple[str, int | None]:
    if isinstance(mode, str):
        normalized = mode.strip().lower().replace("×", "x")
        if normalized == "auto":
            return "auto", None
        if "x" in normalized:
            parts = normalized.split("x")
            if len(parts) != 2:
                raise ValueError("mode must be auto, 52x52, or 78x78")
            left, right = parts
            if left != right:
                raise ValueError("Wenzhou molds must be square: use 52x52 or 78x78")
            normalized = left
        try:
            requested = int(normalized)
        except ValueError as exc:
            raise ValueError("mode must be auto, 52x52, or 78x78") from exc
    else:
        requested = int(mode)
    if requested not in SUPPORTED_MOLD_SIZES:
        raise ValueError("mode must be auto, 52x52, or 78x78")
    return "explicit", requested


def _candidate_payload(
    size: int,
    content_width: int,
    content_height: int,
    pitch: float,
    short_side: float,
) -> dict[str, Any]:
    capacity_fit = content_width <= size and content_height <= size
    projected_side = size * pitch
    mismatch = abs(projected_side - short_side) / short_side
    # Gaussian evidence score: readable and bounded, without pretending this is
    # upstream grid confidence.
    match_score = math.exp(-0.5 * (mismatch / 0.06) ** 2)
    return {
        "board_size": size,
        "columns": size,
        "rows": size,
        "capacity_fit": bool(capacity_fit),
        "projected_side_px": round(projected_side, 6),
        "image_short_side_px": round(short_side, 6),
        "short_side_mismatch_ratio": round(mismatch, 6),
        "pitch_match_score": round(match_score, 6),
    }


def _nearest_integer(value: float) -> int:
    """Round to the nearest integer deterministically, including negatives."""

    return math.floor(value + 0.5)


def _axis_placement(
    *,
    board_size: int,
    content_start: int,
    content_length: int,
    native_origin: float,
    pitch: float,
    image_extent: float,
) -> dict[str, float | int]:
    centered_target_origin = (image_extent - board_size * pitch) / 2.0
    preferred_shift = _nearest_integer((native_origin - centered_target_origin) / pitch)
    preferred_offset = content_start + preferred_shift
    maximum_offset = board_size - content_length
    offset = min(max(preferred_offset, 0), maximum_offset)
    native_to_board_shift = offset - content_start
    board_origin = native_origin - native_to_board_shift * pitch
    return {
        "offset": int(offset),
        "preferred_offset": int(preferred_offset),
        "native_to_board_shift": int(native_to_board_shift),
        "board_origin_px": float(board_origin),
        "centered_target_origin_px": float(centered_target_origin),
        "centering_error_px": float(board_origin - centered_target_origin),
    }


def place_on_wenzhou_mold(
    labels: np.ndarray,
    confidence: np.ndarray,
    content_bbox: Mapping[str, int] | Sequence[int],
    grid_spec: Any,
    image_width_px: float,
    image_height_px: float,
    *,
    mode: str | int = "auto",
    background_label: Any = 0,
    detection_tolerance: float = 0.08,
    ambiguity_margin: float = 0.08,
) -> MoldResult:
    """Select a Wenzhou mold and copy native cells to it without resampling.

    ``mode="auto"`` first filters by content capacity.  It calls a mold
    ``detected`` only when ``mold_size * native_pitch`` agrees with the source
    image's short side and is separated from the runner-up.  Otherwise it
    recommends the smallest fitting mold with ``selection_status="review"``.
    Explicit 52/78 modes have mold-selection confidence 1.0, which must not be
    substituted for the upstream grid confidence.
    """

    native_labels = np.asarray(labels)
    native_confidence = np.asarray(confidence)
    if native_labels.ndim != 2 or native_labels.size == 0:
        raise ValueError("labels must be a non-empty two-dimensional matrix")
    if native_confidence.shape != native_labels.shape:
        raise ValueError("confidence must have the same shape as labels")
    if not np.issubdtype(native_confidence.dtype, np.number):
        raise ValueError("confidence must be numeric")
    if not bool(np.isfinite(native_confidence).all()):
        raise ValueError("confidence must contain only finite values")
    if bool(((native_confidence < 0) | (native_confidence > 1)).any()):
        raise ValueError("confidence values must be within [0, 1]")

    columns, rows, pitch, origin_x, origin_y = _grid_scalars(grid_spec)
    if native_labels.shape != (rows, columns):
        raise ValueError(
            "labels shape does not match grid_spec rows and columns: "
            f"{native_labels.shape} != {(rows, columns)}"
        )
    left, top, right, bottom = _bbox_tuple(content_bbox)
    if not (0 <= left < right <= columns and 0 <= top < bottom <= rows):
        raise ValueError("content_bbox must be non-empty and lie within labels")
    content_width, content_height = right - left, bottom - top
    if content_width > SUPPORTED_MOLD_SIZES[-1] or content_height > SUPPORTED_MOLD_SIZES[-1]:
        raise MoldCapacityError(
            f"native content {content_width}x{content_height} exceeds the largest "
            "Wenzhou mold 78x78; native cells will not be resampled or split"
        )

    width = _finite_number(image_width_px, "image_width_px")
    height = _finite_number(image_height_px, "image_height_px")
    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")
    tolerance = _finite_number(detection_tolerance, "detection_tolerance")
    margin_required = _finite_number(ambiguity_margin, "ambiguity_margin")
    if not (0 < tolerance < 1) or not (0 <= margin_required < 1):
        raise ValueError("detection_tolerance must be in (0,1) and ambiguity_margin in [0,1)")

    parsed_mode, requested_size = _parse_mode(mode)
    short_side = min(width, height)
    candidates = [
        _candidate_payload(size, content_width, content_height, pitch, short_side)
        for size in SUPPORTED_MOLD_SIZES
    ]
    fitting = [candidate for candidate in candidates if candidate["capacity_fit"]]
    if not fitting:  # Defensive: the >78 check above should already explain this.
        raise MoldCapacityError(
            f"native content {content_width}x{content_height} does not fit a supported Wenzhou mold"
        )

    if parsed_mode == "explicit":
        assert requested_size is not None
        selected = next(candidate for candidate in candidates if candidate["board_size"] == requested_size)
        if not selected["capacity_fit"]:
            smallest = min(candidate["board_size"] for candidate in fitting)
            raise MoldCapacityError(
                f"native content {content_width}x{content_height} does not fit {requested_size}x{requested_size}; "
                f"use {smallest}x{smallest}; native cells will not be resampled or split"
            )
        selection_status = "explicit"
        selection_confidence = 1.0
        reason = "user-selected supported mold"
    else:
        ranked = sorted(fitting, key=lambda item: (item["short_side_mismatch_ratio"], item["board_size"]))
        best = ranked[0]
        runner = ranked[1] if len(ranked) > 1 else None
        mismatch = float(best["short_side_mismatch_ratio"])
        separation = (
            float(runner["short_side_mismatch_ratio"]) - mismatch if runner is not None else 1.0
        )
        detected = mismatch <= tolerance and separation >= margin_required
        if detected:
            selected = best
            selection_status = "detected"
            margin_score = min(1.0, max(0.0, separation / max(margin_required, 1e-9)))
            selection_confidence = float(best["pitch_match_score"]) * (0.8 + 0.2 * margin_score)
            reason = "native pitch matches the image short side at this mold size"
        else:
            selected = min(fitting, key=lambda item: item["board_size"])
            selection_status = "review"
            selection_confidence = min(0.49, float(selected["pitch_match_score"]) * 0.5)
            reason = "no reliable board-scale match; recommend the smallest mold that preserves every native cell"

    board_size = int(selected["board_size"])
    x_placement = _axis_placement(
        board_size=board_size,
        content_start=left,
        content_length=content_width,
        native_origin=origin_x,
        pitch=pitch,
        image_extent=width,
    )
    y_placement = _axis_placement(
        board_size=board_size,
        content_start=top,
        content_length=content_height,
        native_origin=origin_y,
        pitch=pitch,
        image_extent=height,
    )
    col_offset = int(x_placement["offset"])
    row_offset = int(y_placement["offset"])

    board_labels = np.full(
        (board_size, board_size), background_label, dtype=native_labels.dtype
    )
    confidence_dtype = np.result_type(native_confidence.dtype, np.float32)
    board_confidence = np.ones((board_size, board_size), dtype=confidence_dtype)
    content_labels = native_labels[top:bottom, left:right]
    content_confidence = native_confidence[top:bottom, left:right]
    board_labels[
        row_offset : row_offset + content_height,
        col_offset : col_offset + content_width,
    ] = content_labels
    board_confidence[
        row_offset : row_offset + content_height,
        col_offset : col_offset + content_width,
    ] = content_confidence

    for rank, candidate in enumerate(
        sorted(candidates, key=lambda item: (not item["capacity_fit"], item["short_side_mismatch_ratio"])),
        start=1,
    ):
        candidate["rank"] = rank
        candidate["selected"] = candidate["board_size"] == board_size

    metadata: dict[str, Any] = {
        "standard": "wenzhou",
        "mode": parsed_mode,
        "board_size": board_size,
        "columns": board_size,
        "rows": board_size,
        "resampled": False,
        "selection_status": selection_status,
        "selection_confidence": round(selection_confidence, 6),
        "reason": reason,
        "native_pitch_px": round(pitch, 6),
        "image_short_side_px": round(short_side, 6),
        "content_size": {"columns": content_width, "rows": content_height},
        "placement": {
            "row_offset": row_offset,
            "col_offset": col_offset,
            "source_bbox": {
                "left": left,
                "top": top,
                "right_exclusive": right,
                "bottom_exclusive": bottom,
            },
            "native_to_board_shift": {
                "columns": int(x_placement["native_to_board_shift"]),
                "rows": int(y_placement["native_to_board_shift"]),
            },
            "board_origin_x_px": round(float(x_placement["board_origin_px"]), 6),
            "board_origin_y_px": round(float(y_placement["board_origin_px"]), 6),
            "centered_target_origin_x_px": round(
                float(x_placement["centered_target_origin_px"]), 6
            ),
            "centered_target_origin_y_px": round(
                float(y_placement["centered_target_origin_px"]), 6
            ),
            "centering_error_x_px": round(float(x_placement["centering_error_px"]), 6),
            "centering_error_y_px": round(float(y_placement["centering_error_px"]), 6),
        },
        "candidates": candidates,
    }
    return MoldResult(board_labels, board_confidence, metadata, candidates)


__all__ = [
    "MoldCapacityError",
    "MoldResult",
    "SUPPORTED_MOLD_SIZES",
    "place_on_wenzhou_mold",
]
