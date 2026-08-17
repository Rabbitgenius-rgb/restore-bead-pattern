#!/usr/bin/env python3
"""Recover an existing logical pixel grid from a photograph or raster image.

The script is intentionally evidence-preserving: it estimates or accepts a
rectilinear lattice, samples cell colors, reports uncertainty, and renders the
result. It does not invent semantic details or turn ordinary photos into pixel
art.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import unicodedata
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
except ImportError as exc:  # pragma: no cover - exercised only in missing runtimes
    raise SystemExit(
        "restore_pattern.py requires numpy and Pillow. Use the bundled Codex "
        "workspace Python runtime or install those two packages."
    ) from exc

try:
    from wenzhou_mold import MoldCapacityError, MoldResult, place_on_wenzhou_mold
except ImportError as exc:  # pragma: no cover - a damaged skill installation
    raise SystemExit(
        "restore_pattern.py requires the bundled wenzhou_mold.py module; "
        "reinstall the complete restore-bead-pattern skill."
    ) from exc


ALGORITHM_VERSION = "0.4.1"
SCHEMA_VERSION = "1.2"

MIB = 1024 * 1024
MAX_SOURCE_FILE_BYTES = 128 * MIB
MAX_SOURCE_PIXELS = 50_000_000
MAX_PATTERN_JSON_BYTES = 64 * MIB
MAX_PALETTE_JSON_BYTES = 4 * MIB
MAX_EDITS_CSV_BYTES = 16 * MIB
MAX_GRID_DIMENSION = 500
MAX_GRID_CELLS = 100_000
MAX_PALETTE_ENTRIES = 1_024
MAX_RENDER_AXIS_PIXELS = 16_000
MAX_RENDER_TOTAL_PIXELS = 16_000_000

OUTPUT_MARKER_NAME = ".restore-bead-pattern-output"
OUTPUT_MARKER_CONTENT = "restore-bead-pattern-output-v1\n"
CSV_FORMULA_PREFIXES = frozenset("=+-@")
SKILL_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class PaletteEntry:
    name: str
    symbol: str
    rgb: tuple[int, int, int]
    role: str = "accent"
    code: str | None = None
    synthetic: bool = False


@dataclass(frozen=True)
class PaletteBundle:
    entries: tuple[PaletteEntry, ...]
    profile: dict[str, Any] | None = None
    matching_method: str = "hybrid-rgb-feature"


@dataclass(frozen=True)
class GridCandidate:
    pitch: float
    phase_x: float
    phase_y: float
    score: float
    x_score: float
    y_score: float


@dataclass(frozen=True)
class GridSpec:
    columns: int
    rows: int
    pitch: float
    origin_x: float
    origin_y: float


WARM_MASCOT_PALETTE = (
    PaletteEntry("background", ".", (255, 255, 255), "background"),
    PaletteEntry("ivory", "W", (248, 247, 228), "fill"),
    PaletteEntry("black", "K", (12, 11, 6), "outline"),
    PaletteEntry("pink", "P", (243, 191, 165), "accent"),
    PaletteEntry("cyan", "C", (138, 208, 214), "accent"),
    PaletteEntry("red", "R", (197, 62, 64), "accent"),
)


BUILTIN_PALETTE_ALIASES = {
    "mard-221": "mard-221-compatible",
    "mard-221-compatible": "mard-221-compatible",
}


def log(message: str) -> None:
    print(message, file=sys.stderr)


def parse_grid(value: str) -> tuple[int, int] | None:
    if value.lower() == "auto":
        return None
    normalized = value.lower().replace("×", "x")
    try:
        columns_text, rows_text = normalized.split("x", 1)
        columns, rows = int(columns_text), int(rows_text)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("grid must be auto or COLSxROWS, for example 40x52") from exc
    if not (2 <= columns <= 500 and 2 <= rows <= 500):
        raise argparse.ArgumentTypeError("grid dimensions must both be between 2 and 500")
    return columns, rows


def parse_origin(value: str) -> tuple[float, float]:
    try:
        x_text, y_text = value.split(",", 1)
        return float(x_text), float(y_text)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("origin must be X,Y in source-image pixels") from exc


def parse_board_size(value: str) -> str:
    """Parse an optional Wenzhou fuse-bead mold target.

    This option deliberately stays separate from ``--grid``: the grid restores
    the source artwork's native logical cells, while the board only places the
    recovered content on a physical mold without resampling it.
    """

    normalized = value.strip().lower().replace("×", "x")
    if normalized not in {"none", "auto", "52x52", "78x78"}:
        raise argparse.ArgumentTypeError(
            "board size must be none, auto, 52x52, or 78x78"
        )
    return normalized


def parse_explicit_board_size(value: str) -> str:
    """Parse a user-selected board for a deliberate design derivation."""

    normalized = parse_board_size(value)
    if normalized not in {"52x52", "78x78"}:
        raise argparse.ArgumentTypeError(
            "scaled designs require an explicit board size: 52x52 or 78x78"
        )
    return normalized


def parse_hex_color(value: str) -> tuple[int, int, int]:
    text = value.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"invalid RGB hex color: {value}")
    return tuple(int(text[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def source_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_regular_file_size(path: Path, maximum_bytes: int, description: str) -> int:
    if not path.is_file():
        raise ValueError(f"{description} does not exist or is not a regular file: {path}")
    size = int(path.stat().st_size)
    if size > maximum_bytes:
        raise ValueError(
            f"{description} exceeds the {maximum_bytes}-byte safety limit: {path}"
        )
    return size


def _read_json_file(path: Path, maximum_bytes: int, description: str) -> Any:
    _require_regular_file_size(path, maximum_bytes, description)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {description}: {path}") from exc


def validate_grid_dimensions(columns: int, rows: int, description: str = "grid") -> None:
    if columns < 1 or rows < 1:
        raise ValueError(f"{description} dimensions must be positive")
    if columns > MAX_GRID_DIMENSION or rows > MAX_GRID_DIMENSION:
        raise ValueError(
            f"{description} dimensions exceed {MAX_GRID_DIMENSION} cells on one axis"
        )
    if columns * rows > MAX_GRID_CELLS:
        raise ValueError(f"{description} exceeds {MAX_GRID_CELLS:,} cells")


def _validate_csv_safe_text(value: str, field: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"palette {field} must not be blank")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        raise ValueError(f"palette {field} must not contain control characters")
    normalized = value.lstrip()
    if normalized and normalized[0] in CSV_FORMULA_PREFIXES:
        raise ValueError(
            f"palette {field} must not start with a spreadsheet formula prefix (=, +, -, @)"
        )


def _find_repository_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        git_marker = candidate / ".git"
        if git_marker.is_dir() or git_marker.is_file():
            return candidate.resolve()
    return None


def _path_contains(protected_path: Path, candidate_ancestor: Path) -> bool:
    try:
        protected_path.relative_to(candidate_ancestor)
    except ValueError:
        return False
    return True


def _has_valid_output_marker(target: Path) -> bool:
    marker = target / OUTPUT_MARKER_NAME
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        return marker.read_text(encoding="utf-8") == OUTPUT_MARKER_CONTENT
    except (OSError, UnicodeError):
        return False


def _write_output_marker(target: Path) -> None:
    (target / OUTPUT_MARKER_NAME).write_text(OUTPUT_MARKER_CONTENT, encoding="utf-8")


def _validate_output_target(
    target: Path,
    overwrite: bool,
    protected_inputs: Sequence[Path] = (),
) -> Path:
    raw_target = target.expanduser()
    if raw_target.is_symlink():
        raise ValueError(f"refusing output directory symlink: {raw_target}")
    resolved = raw_target.resolve()
    filesystem_root = Path(resolved.anchor).resolve()
    repository_root = _find_repository_root(SKILL_ROOT)
    protected_roots = {
        filesystem_root: "filesystem root",
        Path.home().resolve(): "home directory",
        Path.cwd().resolve(): "current working directory",
        SKILL_ROOT: "skill root",
    }
    if repository_root is not None:
        protected_roots[repository_root] = "repository root"
    for protected_root, label in protected_roots.items():
        if resolved == protected_root or _path_contains(protected_root, resolved):
            raise ValueError(
                f"refusing to use the {label} or one of its ancestors as an "
                f"output directory: {resolved}"
            )

    for protected in protected_inputs:
        protected_path = protected.expanduser().resolve()
        if _path_contains(protected_path, resolved):
            raise ValueError(
                "refusing output directory that contains a protected input "
                f"({protected_path}): {resolved}"
            )

    if resolved.exists():
        if resolved.is_symlink():
            raise ValueError(f"refusing output directory symlink: {resolved}")
        if not resolved.is_dir():
            raise ValueError(f"output path exists and is not a directory: {resolved}")
        nonempty = any(resolved.iterdir())
        if nonempty and not overwrite:
            raise ValueError(
                f"output directory is not empty: {resolved}; pass --overwrite to replace it"
            )
        if nonempty and overwrite and not _has_valid_output_marker(resolved):
            raise ValueError(
                "refusing --overwrite because the non-empty directory is not owned by "
                f"restore-bead-pattern: {resolved}"
            )
    return resolved


def load_source(path: Path) -> Image.Image:
    _require_regular_file_size(path, MAX_SOURCE_FILE_BYTES, "input image")
    try:
        with Image.open(path) as opened:
            if opened.width * opened.height > MAX_SOURCE_PIXELS:
                raise ValueError(
                    f"input image exceeds the {MAX_SOURCE_PIXELS:,}-pixel safety limit"
                )
            transposed = ImageOps.exif_transpose(opened)
            if transposed.width * transposed.height > MAX_SOURCE_PIXELS:
                raise ValueError(
                    f"input image exceeds the {MAX_SOURCE_PIXELS:,}-pixel safety limit"
                )
            image = transposed.convert("RGB")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"cannot decode input image: {path}") from exc
    if image.width < 16 or image.height < 16:
        raise ValueError("input image is too small; both dimensions must be at least 16 pixels")
    return image


def border_background(rgb: np.ndarray, strip: int | None = None) -> np.ndarray:
    height, width = rgb.shape[:2]
    band = strip or max(2, min(height, width) // 80)
    samples = np.concatenate(
        (
            rgb[:band].reshape(-1, 3),
            rgb[-band:].reshape(-1, 3),
            rgb[:, :band].reshape(-1, 3),
            rgb[:, -band:].reshape(-1, 3),
        ),
        axis=0,
    ).astype(np.float64)
    return np.median(samples, axis=0)


def foreground_bbox(rgb: np.ndarray, background: np.ndarray) -> tuple[int, int, int, int] | None:
    distance = np.sqrt(((rgb.astype(np.float32) - background.astype(np.float32)) ** 2).sum(axis=2))
    mask = Image.fromarray((distance > 24.0).astype(np.uint8) * 255, mode="L")
    close_size = max(3, min(31, (min(rgb.shape[:2]) // 80) * 2 + 1))
    if close_size % 2 == 0:
        close_size += 1
    mask = mask.filter(ImageFilter.MaxFilter(close_size))
    array = np.asarray(mask) > 0
    ys, xs = np.nonzero(array)
    if len(xs) < 16:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def edge_profiles(rgb: np.ndarray, bbox: tuple[int, int, int, int] | None) -> tuple[np.ndarray, np.ndarray]:
    image = Image.fromarray(rgb.astype(np.uint8), mode="RGB")
    radius = max(1.5, min(rgb.shape[:2]) / 360.0)
    smooth = np.asarray(image.filter(ImageFilter.GaussianBlur(radius=radius)), dtype=np.float32)
    diff_x = np.abs(np.diff(smooth, axis=1)).sum(axis=2)
    diff_y = np.abs(np.diff(smooth, axis=0)).sum(axis=2)
    if bbox is None:
        x0, y0, x1, y1 = 0, 0, rgb.shape[1], rgb.shape[0]
    else:
        x0, y0, x1, y1 = bbox
        pad_x = max(2, round((x1 - x0) * 0.04))
        pad_y = max(2, round((y1 - y0) * 0.04))
        x0, x1 = max(0, x0 - pad_x), min(rgb.shape[1], x1 + pad_x)
        y0, y1 = max(0, y0 - pad_y), min(rgb.shape[0], y1 + pad_y)
    profile_x = diff_x[y0:y1, max(0, x0) : min(diff_x.shape[1], x1)].sum(axis=0)
    profile_y = diff_y[max(0, y0) : min(diff_y.shape[0], y1), x0:x1].sum(axis=1)
    full_x = np.zeros(diff_x.shape[1], dtype=np.float64)
    full_y = np.zeros(diff_y.shape[0], dtype=np.float64)
    full_x[max(0, x0) : max(0, x0) + len(profile_x)] = profile_x
    full_y[max(0, y0) : max(0, y0) + len(profile_y)] = profile_y
    return full_x, full_y


def phase_score(profile: np.ndarray, pitch: float, phase: float) -> float:
    coords = np.arange(len(profile), dtype=np.float64)
    distance = np.abs(((coords - phase + pitch / 2.0) % pitch) - pitch / 2.0)
    sigma = max(1.2, pitch * 0.085)
    weights = np.exp(-0.5 * (distance / sigma) ** 2)
    if profile.sum() <= 0 or weights.sum() <= 0:
        return 0.0
    weighted_mean = float(np.dot(profile, weights) / weights.sum())
    baseline = float(profile.mean()) + 1e-9
    return weighted_mean / baseline


def best_phase(profile: np.ndarray, pitch: float) -> tuple[float, float]:
    step = max(0.4, pitch / 72.0)
    phases = np.arange(0.0, pitch, step)
    scored = [(phase_score(profile, pitch, float(phase)), float(phase)) for phase in phases]
    return max(scored)


def estimate_grid_candidates(
    rgb: np.ndarray,
    bbox: tuple[int, int, int, int] | None,
    minimum_pitch: float,
    maximum_pitch: float,
    count: int,
) -> list[GridCandidate]:
    profile_x, profile_y = edge_profiles(rgb, bbox)
    coarse: list[GridCandidate] = []
    for pitch in np.arange(minimum_pitch, maximum_pitch + 0.001, 0.5):
        x_score, phase_x = best_phase(profile_x, float(pitch))
        y_score, phase_y = best_phase(profile_y, float(pitch))
        combined = math.sqrt(max(0.0, x_score * y_score))
        coarse.append(GridCandidate(float(pitch), phase_x, phase_y, combined, x_score, y_score))
    seeds = sorted(coarse, key=lambda candidate: candidate.score, reverse=True)[: max(8, count * 3)]
    refined: list[GridCandidate] = []
    for seed in seeds:
        for pitch in np.arange(max(minimum_pitch, seed.pitch - 0.5), min(maximum_pitch, seed.pitch + 0.5) + 0.001, 0.1):
            x_score, phase_x = best_phase(profile_x, float(pitch))
            y_score, phase_y = best_phase(profile_y, float(pitch))
            combined = math.sqrt(max(0.0, x_score * y_score))
            refined.append(GridCandidate(float(pitch), phase_x, phase_y, combined, x_score, y_score))
    selected: list[GridCandidate] = []
    for candidate in sorted(refined, key=lambda item: item.score, reverse=True):
        if all(abs(candidate.pitch - prior.pitch) >= 0.75 for prior in selected):
            selected.append(candidate)
        if len(selected) >= count:
            break
    return selected


def lattice_covering_image(width: int, height: int, candidate: GridCandidate) -> GridSpec:
    pitch = candidate.pitch
    origin_x = candidate.phase_x + math.floor((0.0 - candidate.phase_x) / pitch) * pitch
    origin_y = candidate.phase_y + math.floor((0.0 - candidate.phase_y) / pitch) * pitch
    columns = int(math.ceil((width - origin_x) / pitch))
    rows = int(math.ceil((height - origin_y) / pitch))
    return GridSpec(columns, rows, pitch, origin_x, origin_y)


def explicit_grid_spec(
    width: int,
    height: int,
    dimensions: tuple[int, int],
    cell_size: float | None,
    origin: tuple[float, float] | None,
) -> GridSpec:
    columns, rows = dimensions
    pitch = cell_size if cell_size is not None else height / rows
    if pitch < 2:
        raise ValueError("derived cell size is below 2 pixels")
    if origin is None:
        origin_x = (width - columns * pitch) / 2.0
        origin_y = (height - rows * pitch) / 2.0
    else:
        origin_x, origin_y = origin
    return GridSpec(columns, rows, float(pitch), float(origin_x), float(origin_y))


def grid_bounds(spec: GridSpec, row: int, col: int, inset: float = 0.0) -> tuple[int, int, int, int]:
    x0 = spec.origin_x + (col + inset) * spec.pitch
    x1 = spec.origin_x + (col + 1.0 - inset) * spec.pitch
    y0 = spec.origin_y + (row + inset) * spec.pitch
    y1 = spec.origin_y + (row + 1.0 - inset) * spec.pitch
    return int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))


def patch_pixels(rgb: np.ndarray, bounds: tuple[int, int, int, int]) -> np.ndarray:
    x0, y0, x1, y1 = bounds
    height, width = rgb.shape[:2]
    x0, x1 = max(0, x0), min(width, x1)
    y0, y1 = max(0, y0), min(height, y1)
    if x1 <= x0 or y1 <= y0:
        return np.empty((0, 3), dtype=np.float32)
    return rgb[y0:y1, x0:x1].reshape(-1, 3).astype(np.float32)


def ink_representative(pixels: np.ndarray, background: np.ndarray) -> np.ndarray:
    if len(pixels) == 0:
        return background.astype(np.float32)
    distance = ((pixels - background.astype(np.float32)) ** 2).sum(axis=1)
    keep = max(8, round(len(pixels) * 0.45))
    selected = pixels[np.argpartition(distance, max(0, len(distance) - keep))[-keep:]]
    return np.median(selected, axis=0)


def cell_representatives(rgb: np.ndarray, spec: GridSpec, background: np.ndarray) -> np.ndarray:
    result = np.zeros((spec.rows, spec.columns, 3), dtype=np.float32)
    for row in range(spec.rows):
        for col in range(spec.columns):
            pixels = patch_pixels(rgb, grid_bounds(spec, row, col, inset=0.24))
            result[row, col] = np.median(pixels, axis=0) if len(pixels) else background
    return result


def kmeans_pp(values: np.ndarray, k: int, seed: int, iterations: int = 80) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    if len(values) < k:
        raise ValueError("fewer cell samples than requested palette colors")
    centers = [values[rng.integers(0, len(values))]]
    for _ in range(1, k):
        distances = np.min(
            ((values[:, None, :] - np.asarray(centers)[None, :, :]) ** 2).sum(axis=2),
            axis=1,
        )
        total = float(distances.sum())
        if total <= 1e-9:
            centers.append(values[rng.integers(0, len(values))])
        else:
            centers.append(values[rng.choice(len(values), p=distances / total)])
    centers_array = np.asarray(centers, dtype=np.float64)
    labels = np.full(len(values), -1, dtype=np.int16)
    for _ in range(iterations):
        distances = ((values[:, None, :] - centers_array[None, :, :]) ** 2).sum(axis=2)
        new_labels = distances.argmin(axis=1)
        stable = np.array_equal(labels, new_labels)
        labels = new_labels
        for index in range(k):
            members = values[labels == index]
            if len(members):
                centers_array[index] = np.median(members, axis=0)
        if stable:
            break
    return centers_array, labels


def auto_palette(
    representatives: np.ndarray,
    background: np.ndarray,
    colors: int,
    seed: int,
) -> tuple[PaletteEntry, ...]:
    values = representatives.reshape(-1, 3).astype(np.float64)
    _, labels = kmeans_pp(color_features(values), colors, seed)
    centers = np.zeros((colors, 3), dtype=np.float64)
    for index in range(colors):
        members = values[labels == index]
        centers[index] = np.median(members, axis=0) if len(members) else background
    counts = np.bincount(labels, minlength=colors)
    background_cluster = int(np.argmin(((centers - background[None, :]) ** 2).sum(axis=1)))
    other = [index for index in range(colors) if index != background_cluster]
    darkest = min(other, key=lambda index: float(centers[index].mean())) if other else background_cluster
    fill_candidates = [index for index in other if index != darkest and counts[index] >= max(2, len(values) * 0.02)]
    fill = (
        min(fill_candidates, key=lambda index: float(((centers[index] - background) ** 2).sum()))
        if fill_candidates
        else None
    )
    ordered = [background_cluster]
    if fill is not None:
        ordered.append(fill)
    if darkest not in ordered:
        ordered.append(darkest)
    ordered.extend(index for index in sorted(other, key=lambda item: -counts[item]) if index not in ordered)
    entries: list[PaletteEntry] = []
    accent_symbols = iter("ABCDEFGHJKLMNPQRSTUVWXYZ123456789")
    for source_index in ordered:
        rgb = tuple(int(round(value)) for value in np.clip(centers[source_index], 0, 255))
        if source_index == background_cluster:
            entries.append(PaletteEntry("background", ".", rgb, "background"))
        elif source_index == fill:
            entries.append(PaletteEntry("fill", "W", rgb, "fill"))
        elif source_index == darkest:
            entries.append(PaletteEntry("outline", "K", rgb, "outline"))
        else:
            symbol = next(accent_symbols)
            entries.append(PaletteEntry(f"color-{symbol.lower()}", symbol, rgb, "accent"))
    return tuple(entries)


def expected_mard_221_codes() -> set[str]:
    groups = {"A": 26, "B": 32, "C": 29, "D": 26, "E": 24, "F": 25, "G": 21, "H": 23, "M": 15}
    return {f"{group}{number}" for group, count in groups.items() for number in range(1, count + 1)}


def load_builtin_mard_221(background: np.ndarray) -> PaletteBundle:
    path = Path(__file__).resolve().parent.parent / "assets" / "palettes" / "mard-221-compatible.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("bundled MARD 221 palette is missing or unreadable") from exc
    raw_entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(raw_entries, list) or len(raw_entries) != 221:
        raise ValueError("bundled MARD 221 palette must contain exactly 221 bead colors")
    expected_codes = expected_mard_221_codes()
    entries: list[PaletteEntry] = [
        PaletteEntry(
            "background",
            ".",
            tuple(int(round(value)) for value in np.clip(background, 0, 255)),
            "background",
            None,
            True,
        )
    ]
    seen_codes: set[str] = set()
    for item in raw_entries:
        if not isinstance(item, dict):
            raise ValueError("bundled MARD 221 entries must be objects")
        code = str(item.get("code", "")).strip().upper()
        rgb_value = item.get("rgb")
        if code not in expected_codes or code in seen_codes:
            raise ValueError(f"bundled MARD 221 palette contains an invalid or duplicate code: {code or '<blank>'}")
        if not isinstance(rgb_value, list) or len(rgb_value) != 3:
            raise ValueError(f"bundled MARD 221 code {code} has invalid RGB data")
        rgb = tuple(int(channel) for channel in rgb_value)
        if any(channel < 0 or channel > 255 for channel in rgb):
            raise ValueError(f"bundled MARD 221 code {code} has an RGB channel outside 0..255")
        rgb_hex = "#" + "".join(f"{channel:02X}" for channel in rgb)
        if str(item.get("rgb_hex", rgb_hex)).upper() != rgb_hex:
            raise ValueError(f"bundled MARD 221 code {code} has inconsistent RGB and HEX values")
        # Runtime names deliberately equal purchase codes so matrix CSV, counts,
        # review edits, and JSON all speak the same unambiguous language.
        entries.append(PaletteEntry(code, code, rgb, "accent", code, False))
        seen_codes.add(code)
    if seen_codes != expected_codes:
        missing = sorted(expected_codes - seen_codes)
        extra = sorted(seen_codes - expected_codes)
        raise ValueError(f"bundled MARD 221 code set is incomplete (missing={missing}, extra={extra})")
    sources = payload.get("sources", [])
    source_urls = [str(item.get("url")) for item in sources if isinstance(item, dict) and item.get("url")]
    profile = {
        "id": "mard-221-compatible",
        "display_name": str(payload.get("display_name", "MARD 221 Compatible")),
        "code_system": "mard-221",
        "reference_type": str(payload.get("reference_type", "community-open-source-screen-rgb")),
        "compatible_size_note": str(payload.get("compatible_size_note", "Confirm physical bead size separately.")),
        "accessed_at": str(payload.get("accessed_at", "unknown")),
        "bead_color_count": 221,
        "runtime_entry_count": 222,
        "background_strategy": "synthetic-source-background-after-structural-topology",
        "matching_method": "CIEDE2000",
        "provisional": True,
        "groups": dict(payload.get("groups", {})),
        "resource_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_urls": source_urls,
        "disclaimer": str(payload.get("disclaimer", "Screen RGB values are reference-only.")),
        "license_spdx": str(payload.get("license_spdx", "MIT")),
        "copyright": str(payload.get("copyright", "Copyright (c) 2026 Jett-Wu")),
        "source_commit": str(payload.get("source_commit", "unknown")),
        "license_url": str(payload.get("license_url", "")),
        "notice_file": str(payload.get("notice_file", "THIRD_PARTY_NOTICES.md")),
        "trademark_disclaimer": str(payload.get("trademark_disclaimer", "")),
    }
    return PaletteBundle(tuple(entries), profile, "catalog-lab-ciede2000")


def _validate_palette_entries(
    entries: Sequence[PaletteEntry], description: str = "palette"
) -> None:
    if not 2 <= len(entries) <= MAX_PALETTE_ENTRIES:
        raise ValueError(
            f"{description} must contain between 2 and {MAX_PALETTE_ENTRIES} entries"
        )
    if sum(entry.role == "background" for entry in entries) != 1:
        raise ValueError(f"{description} must contain exactly one entry with role=background")
    if len({entry.name for entry in entries}) != len(entries):
        raise ValueError(f"{description} names must be unique")
    if len({entry.symbol for entry in entries}) != len(entries):
        raise ValueError(f"{description} symbols must be unique")
    codes = [entry.code for entry in entries if entry.code is not None]
    if len(set(codes)) != len(codes):
        raise ValueError(f"{description} codes must be unique when supplied")
    for entry in entries:
        _validate_csv_safe_text(entry.name, "name")
        _validate_csv_safe_text(entry.symbol, "symbol")
        _validate_csv_safe_text(entry.role, "role")
        if entry.code is not None:
            _validate_csv_safe_text(entry.code, "code")
        if len(entry.symbol) > 3:
            raise ValueError("palette symbols must contain between one and three characters")
        if len(entry.rgb) != 3 or any(channel < 0 or channel > 255 for channel in entry.rgb):
            raise ValueError("palette RGB channels must be between 0 and 255")


def parse_custom_palette(value: str) -> tuple[PaletteEntry, ...]:
    path = Path(value).expanduser().resolve()
    payload = _read_json_file(path, MAX_PALETTE_JSON_BYTES, "palette JSON")
    raw_entries = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(raw_entries, list):
        raise ValueError("palette JSON must contain an entries list")
    if not 2 <= len(raw_entries) <= MAX_PALETTE_ENTRIES:
        raise ValueError(
            f"palette JSON must contain between 2 and {MAX_PALETTE_ENTRIES} entries"
        )
    entries: list[PaletteEntry] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            raise ValueError("each palette entry must be an object")
        rgb_value = item.get("rgb")
        if isinstance(rgb_value, str):
            rgb = parse_hex_color(rgb_value)
        elif isinstance(rgb_value, list) and len(rgb_value) == 3:
            rgb = tuple(int(channel) for channel in rgb_value)
        else:
            raise ValueError("palette rgb must be #RRGGBB or [R,G,B]")
        code_value = item.get("code")
        code = str(code_value) if code_value is not None else None
        entries.append(
            PaletteEntry(
                str(item["name"]),
                str(item["symbol"]),
                rgb,  # type: ignore[arg-type]
                str(item.get("role", "accent")),
                code,
                bool(item.get("synthetic", False)),
            )
        )
    result = tuple(entries)
    _validate_palette_entries(result, "palette")
    return result


def load_palette_bundle(
    value: str,
    representatives: np.ndarray,
    background: np.ndarray,
    colors: int,
    seed: int,
) -> PaletteBundle:
    normalized = value.strip().lower()
    builtin = BUILTIN_PALETTE_ALIASES.get(normalized)
    if builtin == "mard-221-compatible":
        return load_builtin_mard_221(background)
    if value == "auto":
        return PaletteBundle(auto_palette(representatives, background, colors, seed))
    if value == "warm-mascot":
        return PaletteBundle(WARM_MASCOT_PALETTE)
    return PaletteBundle(parse_custom_palette(value))


def load_palette(
    value: str,
    representatives: np.ndarray,
    background: np.ndarray,
    colors: int,
    seed: int,
) -> tuple[PaletteEntry, ...]:
    """Backward-compatible entry-only loader used by external callers."""

    return load_palette_bundle(value, representatives, background, colors, seed).entries


def palette_index(palette: Sequence[PaletteEntry], role: str) -> int | None:
    matches = [index for index, entry in enumerate(palette) if entry.role == role]
    return matches[0] if len(matches) == 1 else None


def color_features(values: np.ndarray) -> np.ndarray:
    """Separate brightness from chroma so warm shadows do not become pink/red."""

    array = values.astype(np.float32)
    luminance = 0.299 * array[..., 0] + 0.587 * array[..., 1] + 0.114 * array[..., 2]
    red_green = array[..., 0] - array[..., 1]
    green_blue = array[..., 1] - array[..., 2]
    return np.stack((0.52 * luminance, 1.08 * red_green, 0.92 * green_blue), axis=-1)


def srgb_to_lab(values: np.ndarray) -> np.ndarray:
    """Convert sRGB values in 0..255 to CIE Lab using the D65 white point."""

    array = np.asarray(values, dtype=np.float64) / 255.0
    linear = np.where(
        array <= 0.04045,
        array / 12.92,
        ((array + 0.055) / 1.055) ** 2.4,
    )
    matrix = np.asarray(
        (
            (0.4124564, 0.3575761, 0.1804375),
            (0.2126729, 0.7151522, 0.0721750),
            (0.0193339, 0.1191920, 0.9503041),
        ),
        dtype=np.float64,
    )
    xyz = np.matmul(linear, matrix.T)
    normalized = xyz / np.asarray((0.95047, 1.0, 1.08883), dtype=np.float64)
    delta = 6.0 / 29.0
    transformed = np.where(
        normalized > delta**3,
        np.cbrt(normalized),
        normalized / (3.0 * delta**2) + 4.0 / 29.0,
    )
    x, y, z = np.moveaxis(transformed, -1, 0)
    return np.stack((116.0 * y - 16.0, 500.0 * (x - y), 200.0 * (y - z)), axis=-1)


def delta_e_2000(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    """Vectorized CIEDE2000 color difference with standard unit weights."""

    first = np.asarray(lab1, dtype=np.float64)
    second = np.asarray(lab2, dtype=np.float64)
    l1, a1, b1 = np.moveaxis(first, -1, 0)
    l2, a2, b2 = np.moveaxis(second, -1, 0)
    c1 = np.hypot(a1, b1)
    c2 = np.hypot(a2, b2)
    c_bar = (c1 + c2) / 2.0
    c_bar_seventh = c_bar**7
    g = 0.5 * (1.0 - np.sqrt(c_bar_seventh / (c_bar_seventh + 25.0**7)))
    a1_prime = (1.0 + g) * a1
    a2_prime = (1.0 + g) * a2
    c1_prime = np.hypot(a1_prime, b1)
    c2_prime = np.hypot(a2_prime, b2)
    h1_prime = np.mod(np.degrees(np.arctan2(b1, a1_prime)), 360.0)
    h2_prime = np.mod(np.degrees(np.arctan2(b2, a2_prime)), 360.0)
    h1_prime = np.where(c1_prime == 0.0, 0.0, h1_prime)
    h2_prime = np.where(c2_prime == 0.0, 0.0, h2_prime)

    delta_l_prime = l2 - l1
    delta_c_prime = c2_prime - c1_prime
    raw_delta_h = h2_prime - h1_prime
    delta_h_prime = np.where(
        c1_prime * c2_prime == 0.0,
        0.0,
        np.where(
            np.abs(raw_delta_h) <= 180.0,
            raw_delta_h,
            np.where(raw_delta_h > 180.0, raw_delta_h - 360.0, raw_delta_h + 360.0),
        ),
    )
    delta_big_h_prime = 2.0 * np.sqrt(c1_prime * c2_prime) * np.sin(np.radians(delta_h_prime) / 2.0)

    l_bar_prime = (l1 + l2) / 2.0
    c_bar_prime = (c1_prime + c2_prime) / 2.0
    hue_sum = h1_prime + h2_prime
    h_bar_prime = np.where(
        c1_prime * c2_prime == 0.0,
        hue_sum,
        np.where(
            np.abs(h1_prime - h2_prime) <= 180.0,
            hue_sum / 2.0,
            np.where(hue_sum < 360.0, (hue_sum + 360.0) / 2.0, (hue_sum - 360.0) / 2.0),
        ),
    )
    t = (
        1.0
        - 0.17 * np.cos(np.radians(h_bar_prime - 30.0))
        + 0.24 * np.cos(np.radians(2.0 * h_bar_prime))
        + 0.32 * np.cos(np.radians(3.0 * h_bar_prime + 6.0))
        - 0.20 * np.cos(np.radians(4.0 * h_bar_prime - 63.0))
    )
    delta_theta = 30.0 * np.exp(-((h_bar_prime - 275.0) / 25.0) ** 2)
    c_bar_prime_seventh = c_bar_prime**7
    r_c = 2.0 * np.sqrt(c_bar_prime_seventh / (c_bar_prime_seventh + 25.0**7))
    l_term = l_bar_prime - 50.0
    s_l = 1.0 + 0.015 * l_term**2 / np.sqrt(20.0 + l_term**2)
    s_c = 1.0 + 0.045 * c_bar_prime
    s_h = 1.0 + 0.015 * c_bar_prime * t
    r_t = -np.sin(np.radians(2.0 * delta_theta)) * r_c
    l_ratio = delta_l_prime / s_l
    c_ratio = delta_c_prime / s_c
    h_ratio = delta_big_h_prime / s_h
    return np.sqrt(np.maximum(0.0, l_ratio**2 + c_ratio**2 + h_ratio**2 + r_t * c_ratio * h_ratio))


def white_balance_for_reference(values: np.ndarray, background: np.ndarray) -> tuple[np.ndarray, dict[str, Any]]:
    """Apply conservative white balance when the photographed border is neutral."""

    border = np.asarray(background, dtype=np.float64)
    neutral = float(border.mean()) >= 140.0 and float(border.max() - border.min()) <= 45.0
    if neutral:
        gains = np.clip(255.0 / np.clip(border, 1.0, 255.0), 0.80, 1.35)
    else:
        gains = np.ones(3, dtype=np.float64)
    corrected = np.clip(np.asarray(values, dtype=np.float64) * gains, 0.0, 255.0)
    diagnostics = {
        "white_balance_applied": bool(neutral and not np.allclose(gains, 1.0, atol=1e-4)),
        "background_rgb": [round(float(value), 3) for value in border],
        "channel_gains": [round(float(value), 6) for value in gains],
    }
    return corrected, diagnostics


def quantize_catalog_clusters(
    structure_palette: Sequence[PaletteEntry],
    structure_labels: np.ndarray,
    raw_structure_labels: np.ndarray,
    catalog_palette: Sequence[PaletteEntry],
    background_rgb: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    """Map logical source-color clusters to catalog codes in CIE Lab space.

    Mapping cluster centers instead of every photographed cell suppresses yarn,
    melt, shadow, and camera texture that would otherwise explode one intended
    bead color into many nearby purchase codes.
    """

    structure_background = palette_index(structure_palette, "background")
    catalog_background = palette_index(catalog_palette, "background")
    if structure_background is None or catalog_background is None:
        raise ValueError("catalog matching requires unique structural and catalog backgrounds")
    bead_indices = np.asarray(
        [
            index
            for index, entry in enumerate(catalog_palette)
            if index != catalog_background and entry.code is not None
        ],
        dtype=np.int32,
    )
    source_indices = np.asarray(
        [index for index in range(len(structure_palette)) if index != structure_background],
        dtype=np.int32,
    )
    if len(bead_indices) < 2 or len(source_indices) < 1:
        raise ValueError("catalog matching requires bead colors and at least one source color cluster")
    source_rgb = np.asarray([structure_palette[int(index)].rgb for index in source_indices], dtype=np.float64)
    corrected, balance_diagnostics = white_balance_for_reference(source_rgb, background_rgb)
    source_lab = srgb_to_lab(corrected)
    catalog_lab = srgb_to_lab(
        np.asarray([catalog_palette[int(index)].rgb for index in bead_indices], dtype=np.float64)
    )
    distances = delta_e_2000(source_lab[:, None, :], catalog_lab[None, :, :])
    order = np.argsort(distances, axis=1, kind="stable")[:, :2]
    rows = np.arange(len(source_indices))
    best_catalog = bead_indices[order[:, 0]]
    second_catalog = bead_indices[order[:, 1]]
    best_distance = distances[rows, order[:, 0]]
    second_distance = distances[rows, order[:, 1]]

    mapping = np.full(len(structure_palette), catalog_background, dtype=np.int16)
    runner_mapping = np.full(len(structure_palette), catalog_background, dtype=np.int16)
    cluster_confidence = np.full(len(structure_palette), 0.995, dtype=np.float32)
    mapping[source_indices] = best_catalog
    runner_mapping[source_indices] = second_catalog
    fit = np.exp(-best_distance / 18.0)
    separation = 1.0 - np.exp(-np.maximum(0.0, second_distance - best_distance) / 4.0)
    cluster_confidence[source_indices] = np.clip(
        0.40 + 0.34 * fit + 0.26 * separation, 0.0, 0.995
    ).astype(np.float32)

    labels = mapping[structure_labels]
    raw_labels = mapping[raw_structure_labels]
    alternatives = runner_mapping[structure_labels]
    color_confidence = cluster_confidence[structure_labels]
    cluster_payload: list[dict[str, Any]] = []
    for position, source_index in enumerate(source_indices):
        selected = catalog_palette[int(best_catalog[position])]
        runner_up = catalog_palette[int(second_catalog[position])]
        cluster_payload.append(
            {
                "source_index": int(source_index),
                "source_role": structure_palette[int(source_index)].role,
                "source_rgb": list(structure_palette[int(source_index)].rgb),
                "selected_code": selected.code,
                "selected_rgb": list(selected.rgb),
                "delta_e": round(float(best_distance[position]), 6),
                "runner_up_code": runner_up.code,
                "runner_up_delta_e": round(float(second_distance[position]), 6),
                "margin_delta_e": round(float(second_distance[position] - best_distance[position]), 6),
            }
        )
    diagnostics = {
        "method": "CIEDE2000-on-white-balanced-logical-color-clusters",
        "catalog_color_count": int(len(bead_indices)),
        "source_cluster_count": int(len(source_indices)),
        "foreground_cell_count": int((structure_labels != structure_background).sum()),
        "mean_best_delta_e": round(float(best_distance.mean()), 6),
        "p95_best_delta_e": round(float(np.percentile(best_distance, 95)), 6),
        "mean_runner_up_margin_delta_e": round(float((second_distance - best_distance).mean()), 6),
        "cluster_mappings": cluster_payload,
        **balance_diagnostics,
    }
    return (
        labels.astype(np.int16),
        raw_labels.astype(np.int16),
        alternatives.astype(np.int16),
        color_confidence.astype(np.float32),
        mapping,
        diagnostics,
    )


def classify_cells(
    rgb: np.ndarray,
    spec: GridSpec,
    palette: Sequence[PaletteEntry],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Classify cells from central-patch evidence without semantic redrawing."""

    palette_rgb = np.asarray([entry.rgb for entry in palette], dtype=np.float32)
    palette_features = color_features(palette_rgb)
    labels = np.zeros((spec.rows, spec.columns), dtype=np.int16)
    alternatives = np.zeros_like(labels)
    confidence = np.zeros((spec.rows, spec.columns), dtype=np.float32)

    for row in range(spec.rows):
        for col in range(spec.columns):
            pixels = patch_pixels(rgb, grid_bounds(spec, row, col, inset=0.20))
            if len(pixels) == 0:
                labels[row, col] = 0
                alternatives[row, col] = 0
                confidence[row, col] = 0.0
                continue
            pixel_distances = np.sqrt(
                ((color_features(pixels)[:, None, :] - palette_features[None, :, :]) ** 2).sum(axis=2)
            )
            pixel_labels = pixel_distances.argmin(axis=1)
            fractions = np.bincount(pixel_labels, minlength=len(palette)).astype(np.float64)
            fractions /= max(1, len(pixel_labels))
            median_rgb = np.median(pixels, axis=0)
            median_feature = color_features(median_rgb[None, :])[0]
            median_distances = np.sqrt(((palette_features - median_feature[None, :]) ** 2).sum(axis=1))
            center_scores = np.exp(-median_distances / 46.0)
            scores = 0.72 * fractions + 0.28 * center_scores
            order = np.argsort(scores)[::-1]
            best = int(order[0])
            second = int(order[1]) if len(order) > 1 else best
            margin = float(scores[best] - scores[second])
            labels[row, col] = best
            alternatives[row, col] = second
            confidence[row, col] = float(
                np.clip(0.30 + 0.46 * fractions[best] + 0.52 * max(0.0, margin), 0.0, 0.995)
            )
    return labels, confidence, alternatives


def ensemble_agreement(
    rgb: np.ndarray,
    spec: GridSpec,
    palette: Sequence[PaletteEntry],
    nominal: np.ndarray,
) -> tuple[np.ndarray, int]:
    votes = np.zeros(nominal.shape, dtype=np.int16)
    count = 0
    for pitch_factor in (-0.006, 0.0, 0.006):
        pitch = spec.pitch * (1.0 + pitch_factor)
        for shift_x in (-0.07, 0.0, 0.07):
            for shift_y in (-0.07, 0.0, 0.07):
                variant = GridSpec(
                    spec.columns,
                    spec.rows,
                    pitch,
                    spec.origin_x + shift_x * spec.pitch,
                    spec.origin_y + shift_y * spec.pitch,
                )
                labels, _, _ = classify_cells(rgb, variant, palette)
                votes += labels == nominal
                count += 1
    return votes.astype(np.float32) / float(count), count


def catalog_ensemble_agreement(
    rgb: np.ndarray,
    spec: GridSpec,
    structure_palette: Sequence[PaletteEntry],
    structure_to_catalog: np.ndarray,
    nominal: np.ndarray,
    uncertain_threshold: float,
    topology_enabled: bool,
) -> tuple[np.ndarray, int]:
    """Measure full code agreement while keeping 221 colors out of pixel voting."""

    structure_background = palette_index(structure_palette, "background")
    if structure_background is None:
        raise ValueError("structural segmentation palette has no unique background")
    votes = np.zeros(nominal.shape, dtype=np.int16)
    count = 0
    for pitch_factor in (-0.006, 0.0, 0.006):
        pitch = spec.pitch * (1.0 + pitch_factor)
        for shift_x in (-0.07, 0.0, 0.07):
            for shift_y in (-0.07, 0.0, 0.07):
                variant = GridSpec(
                    spec.columns,
                    spec.rows,
                    pitch,
                    spec.origin_x + shift_x * spec.pitch,
                    spec.origin_y + shift_y * spec.pitch,
                )
                raw_structure, raw_confidence, _ = classify_cells(rgb, variant, structure_palette)
                structure, _, _, _, _ = normalize_light_topology(
                    raw_structure,
                    raw_confidence,
                    structure_palette,
                    uncertain_threshold,
                    topology_enabled,
                )
                variant_labels = structure_to_catalog[structure]
                votes += variant_labels == nominal
                count += 1
    return votes.astype(np.float32) / float(count), count


def four_connected_exterior(light: np.ndarray) -> np.ndarray:
    rows, columns = light.shape
    exterior = np.zeros_like(light, dtype=bool)
    queue: deque[tuple[int, int]] = deque()
    for col in range(columns):
        for row in (0, rows - 1):
            if light[row, col] and not exterior[row, col]:
                exterior[row, col] = True
                queue.append((row, col))
    for row in range(rows):
        for col in (0, columns - 1):
            if light[row, col] and not exterior[row, col]:
                exterior[row, col] = True
                queue.append((row, col))
    while queue:
        row, col = queue.popleft()
        for next_row, next_col in ((row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)):
            if (
                0 <= next_row < rows
                and 0 <= next_col < columns
                and light[next_row, next_col]
                and not exterior[next_row, next_col]
            ):
                exterior[next_row, next_col] = True
                queue.append((next_row, next_col))
    return exterior


def normalize_light_topology(
    labels: np.ndarray,
    confidence: np.ndarray,
    palette: Sequence[PaletteEntry],
    uncertain_threshold: float,
    enabled: bool,
) -> tuple[np.ndarray, np.ndarray, dict[tuple[int, int], str], dict[str, Any], list[str]]:
    result = labels.copy()
    adjusted_confidence = confidence.copy()
    reasons: dict[tuple[int, int], str] = {}
    diagnostics: dict[str, Any] = {
        "mode": "off" if not enabled else "four-connected-light-regions",
        "applied": False,
        "external_fill_removed": 0,
        "enclosed_background_filled": 0,
    }
    warnings: list[str] = []
    if not enabled:
        return result, adjusted_confidence, reasons, diagnostics, warnings
    background = palette_index(palette, "background")
    fill = palette_index(palette, "fill")
    outline = palette_index(palette, "outline")
    if background is None or fill is None or outline is None:
        diagnostics["mode"] = "skipped-missing-roles"
        return result, adjusted_confidence, reasons, diagnostics, warnings

    light = (labels == background) | (labels == fill)
    exterior = four_connected_exterior(light)
    interior = light & ~exterior
    interior_count = int(interior.sum())
    diagnostics["interior_light_cells"] = interior_count
    if interior_count < max(4, round(labels.size * 0.005)):
        diagnostics["mode"] = "skipped-no-stable-interior"
        return result, adjusted_confidence, reasons, diagnostics, warnings

    remove_mask = (labels == fill) & exterior
    fill_mask = (labels == background) & interior
    result[remove_mask] = background
    result[fill_mask] = fill
    diagnostics["applied"] = True
    diagnostics["external_fill_removed"] = int(remove_mask.sum())
    diagnostics["enclosed_background_filled"] = int(fill_mask.sum())
    changed = remove_mask | fill_mask
    adjusted_confidence[changed] = np.minimum(
        adjusted_confidence[changed], max(0.0, uncertain_threshold - 0.01)
    )
    for row, col in zip(*np.nonzero(remove_mask)):
        reasons[(int(row), int(col))] = "edge-connected light cell normalized to background"
    for row, col in zip(*np.nonzero(fill_mask)):
        reasons[(int(row), int(col))] = "enclosed light cell normalized to fill"
    return result, adjusted_confidence, reasons, diagnostics, warnings


def content_bbox(labels: np.ndarray, background: int) -> dict[str, int] | None:
    rows, columns = np.nonzero(labels != background)
    if len(rows) == 0:
        return None
    return {
        "left": int(columns.min()),
        "top": int(rows.min()),
        "right_exclusive": int(columns.max()) + 1,
        "bottom_exclusive": int(rows.max()) + 1,
    }


def crop_by_bbox(array: np.ndarray, bbox: dict[str, int]) -> np.ndarray:
    return array[
        bbox["top"] : bbox["bottom_exclusive"],
        bbox["left"] : bbox["right_exclusive"],
    ]


def render_matrix(
    labels: np.ndarray,
    palette: Sequence[PaletteEntry],
    cell_px: int,
    *,
    grid: bool,
    transparent: bool = False,
    confidence: np.ndarray | None = None,
    uncertain_threshold: float = 0.62,
) -> Image.Image:
    rows, columns = labels.shape
    width_px, height_px = columns * cell_px, rows * cell_px
    if max(width_px, height_px) > MAX_RENDER_AXIS_PIXELS:
        raise ValueError(
            f"rendered image would exceed {MAX_RENDER_AXIS_PIXELS} pixels on one axis; "
            "lower --render-cell-px"
        )
    if width_px * height_px > MAX_RENDER_TOTAL_PIXELS:
        raise ValueError(
            f"rendered image would exceed {MAX_RENDER_TOTAL_PIXELS:,} total pixels; "
            "lower --render-cell-px"
        )
    mode = "RGBA" if transparent else "RGB"
    background_color = (255, 255, 255, 0) if transparent else (255, 255, 255)
    image = Image.new(mode, (width_px, height_px), background_color)
    draw = ImageDraw.Draw(image)
    background = palette_index(palette, "background")
    for row in range(rows):
        for col in range(columns):
            entry = palette[int(labels[row, col])]
            if transparent and int(labels[row, col]) == background:
                continue
            fill = (*entry.rgb, 255) if transparent else entry.rgb
            x0, y0 = col * cell_px, row * cell_px
            draw.rectangle((x0, y0, x0 + cell_px - 1, y0 + cell_px - 1), fill=fill)
            if grid:
                line = (105, 105, 105, 150) if transparent else (150, 150, 150)
                draw.rectangle((x0, y0, x0 + cell_px - 1, y0 + cell_px - 1), outline=line, width=1)
                if cell_px >= 18 and int(labels[row, col]) != background:
                    text_color = (255, 255, 255, 255) if sum(entry.rgb) < 260 and transparent else None
                    if text_color is None:
                        text_color = (255, 255, 255) if sum(entry.rgb) < 260 else (35, 35, 35)
                    box = draw.textbbox((0, 0), entry.symbol)
                    tx = x0 + (cell_px - (box[2] - box[0])) / 2
                    ty = y0 + (cell_px - (box[3] - box[1])) / 2 - box[1]
                    draw.text((tx, ty), entry.symbol, fill=text_color)
            if confidence is not None and float(confidence[row, col]) < uncertain_threshold and int(labels[row, col]) != background:
                marker = (255, 0, 180, 255) if transparent else (255, 0, 180)
                width = max(2, cell_px // 8)
                draw.rectangle((x0 + 1, y0 + 1, x0 + cell_px - 2, y0 + cell_px - 2), outline=marker, width=width)
    return image


def save_rendered_artifacts(
    output: Path,
    labels: np.ndarray,
    confidence: np.ndarray,
    palette: Sequence[PaletteEntry],
    bbox: dict[str, int],
    cell_px: int,
    uncertain_threshold: float,
) -> dict[str, dict[str, Any]]:
    cropped_labels = crop_by_bbox(labels, bbox)
    cropped_confidence = crop_by_bbox(confidence, bbox)
    images = {
        "pattern_preview.png": render_matrix(cropped_labels, palette, cell_px, grid=False),
        "pattern_grid.png": render_matrix(cropped_labels, palette, cell_px, grid=True),
        "pattern_review.png": render_matrix(
            cropped_labels,
            palette,
            cell_px,
            grid=True,
            confidence=cropped_confidence,
            uncertain_threshold=uncertain_threshold,
        ),
        "pattern_transparent.png": render_matrix(
            cropped_labels, palette, cell_px, grid=False, transparent=True
        ),
        "canvas_grid.png": render_matrix(labels, palette, cell_px, grid=True),
    }
    artifacts: dict[str, dict[str, Any]] = {}
    for filename, image in images.items():
        image.save(output / filename)
        artifacts[filename] = {
            "width_px": image.width,
            "height_px": image.height,
            "mode": image.mode,
        }
    return artifacts


def save_source_overlay(image: Image.Image, spec: GridSpec, output: Path) -> dict[str, Any]:
    overlay = image.convert("RGBA")
    draw = ImageDraw.Draw(overlay, "RGBA")
    for col in range(spec.columns + 1):
        x = spec.origin_x + col * spec.pitch
        draw.line((x, spec.origin_y, x, spec.origin_y + spec.rows * spec.pitch), fill=(0, 180, 255, 150), width=1)
    for row in range(spec.rows + 1):
        y = spec.origin_y + row * spec.pitch
        draw.line((spec.origin_x, y, spec.origin_x + spec.columns * spec.pitch, y), fill=(0, 180, 255, 150), width=1)
    draw.rectangle(
        (
            spec.origin_x,
            spec.origin_y,
            spec.origin_x + spec.columns * spec.pitch,
            spec.origin_y + spec.rows * spec.pitch,
        ),
        outline=(255, 45, 45, 230),
        width=max(2, round(spec.pitch * 0.08)),
    )
    overlay.convert("RGB").save(output)
    return {"width_px": overlay.width, "height_px": overlay.height, "mode": "RGB"}


def candidate_contact_sheet(
    image: Image.Image,
    specs: Sequence[tuple[GridSpec, float]],
    output: Path,
) -> dict[str, Any]:
    tiles: list[Image.Image] = []
    for index, (spec, score) in enumerate(specs[:5], start=1):
        overlay = image.convert("RGBA")
        draw = ImageDraw.Draw(overlay, "RGBA")
        for col in range(spec.columns + 1):
            x = spec.origin_x + col * spec.pitch
            draw.line((x, spec.origin_y, x, spec.origin_y + spec.rows * spec.pitch), fill=(0, 180, 255, 150), width=1)
        for row in range(spec.rows + 1):
            y = spec.origin_y + row * spec.pitch
            draw.line((spec.origin_x, y, spec.origin_x + spec.columns * spec.pitch, y), fill=(0, 180, 255, 150), width=1)
        overlay.thumbnail((480, 360), Image.Resampling.LANCZOS)
        tile = Image.new("RGB", (500, 400), "white")
        tile.paste(overlay.convert("RGB"), ((500 - overlay.width) // 2, 30))
        ImageDraw.Draw(tile).text(
            (10, 8),
            f"#{index}  {spec.columns}x{spec.rows}  pitch={spec.pitch:.3f}  score={score:.3f}",
            fill=(20, 20, 20),
        )
        tiles.append(tile)
    if not tiles:
        tiles = [Image.new("RGB", (500, 120), "white")]
    columns = min(2, len(tiles))
    rows = math.ceil(len(tiles) / columns)
    sheet = Image.new("RGB", (columns * 500, rows * 400), (230, 230, 230))
    for index, tile in enumerate(tiles):
        sheet.paste(tile, ((index % columns) * 500, (index // columns) * 400))
    sheet.save(output)
    return {"width_px": sheet.width, "height_px": sheet.height, "mode": sheet.mode}


def write_matrix_csv(path: Path, labels: np.ndarray, palette: Sequence[PaletteEntry]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["row\\col", *range(labels.shape[1])])
        for row in range(labels.shape[0]):
            writer.writerow([row, *(palette[int(value)].symbol for value in labels[row])])


def write_palette_csv(path: Path, palette: Sequence[PaletteEntry], counts: dict[str, int]) -> None:
    catalog = any(entry.code is not None or entry.synthetic for entry in palette)
    fieldnames = ["name", "symbol"]
    if catalog:
        fieldnames.extend(("code", "synthetic"))
    fieldnames.extend(("rgb_hex", "role", "count"))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for entry in palette:
            row = {
                "name": entry.name,
                "symbol": entry.symbol,
                "rgb_hex": "#" + "".join(f"{channel:02X}" for channel in entry.rgb),
                "role": entry.role,
                "count": counts.get(entry.name, 0),
            }
            if catalog:
                row["code"] = entry.code or ""
                row["synthetic"] = "true" if entry.synthetic else "false"
            writer.writerow(row)


def write_review_csv(
    path: Path,
    labels: np.ndarray,
    raw_labels: np.ndarray,
    alternatives: np.ndarray,
    confidence: np.ndarray,
    palette: Sequence[PaletteEntry],
    threshold: float,
    reasons: dict[tuple[int, int], str],
) -> int:
    background = palette_index(palette, "background")
    catalog = any(entry.code is not None or entry.synthetic for entry in palette)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "row",
            "col",
            "current_label",
            "current_symbol",
        ]
        if catalog:
            fieldnames.extend(("current_code", "alternative_code", "raw_code"))
        fieldnames.extend((
            "confidence",
            "alternative",
            "raw_label",
            "reason",
            "new_label",
            "note",
        ))
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in range(labels.shape[0]):
            for col in range(labels.shape[1]):
                label_index = int(labels[row, col])
                if label_index == background or float(confidence[row, col]) >= threshold:
                    continue
                current = palette[label_index]
                alternative = palette[int(alternatives[row, col])]
                raw = palette[int(raw_labels[row, col])]
                record = {
                    "row": row,
                    "col": col,
                    "current_label": current.name,
                    "current_symbol": current.symbol,
                    "confidence": f"{float(confidence[row, col]):.4f}",
                    "alternative": alternative.name,
                    "raw_label": raw.name,
                    "reason": reasons.get((row, col), "sampling disagreement or weak color margin"),
                    "new_label": "",
                    "note": "",
                }
                if catalog:
                    record["current_code"] = current.code or ""
                    record["alternative_code"] = alternative.code or ""
                    record["raw_code"] = raw.code or ""
                writer.writerow(record)
                count += 1
    return count


def expand_grid(spec: GridSpec, padding: int) -> GridSpec:
    if padding <= 0:
        return spec
    return GridSpec(
        spec.columns + padding * 2,
        spec.rows + padding * 2,
        spec.pitch,
        spec.origin_x - padding * spec.pitch,
        spec.origin_y - padding * spec.pitch,
    )


def fallback_auto_specs(
    rgb: np.ndarray,
    bbox: tuple[int, int, int, int] | None,
    pitch_min: float,
    pitch_max: float,
) -> tuple[list[tuple[GridSpec, float]], dict[str, Any]]:
    candidates = estimate_grid_candidates(rgb, bbox, pitch_min, pitch_max, 5)
    if not candidates or bbox is None:
        raise ValueError("could not estimate a stable grid from the image")
    x0, y0, x1, y1 = bbox
    specs: list[tuple[GridSpec, float]] = []
    for candidate in candidates:
        columns = max(2, int(round((x1 - x0) / candidate.pitch)))
        rows = max(2, int(round((y1 - y0) / candidate.pitch)))
        origin_x = (x0 + x1 - columns * candidate.pitch) / 2.0
        origin_y = (y0 + y1 - rows * candidate.pitch) / 2.0
        specs.append((GridSpec(columns, rows, candidate.pitch, origin_x, origin_y), candidate.score))
    top = specs[0][1]
    runner = specs[1][1] if len(specs) > 1 else 0.0
    confidence = max(0.0, min(1.0, (top - runner) / max(top, 1e-9)))
    return specs, {"method": "fallback-edge-phase", "confidence": confidence}


def automatic_grid_specs(
    rgb: np.ndarray,
    pitch_min: float | None,
    pitch_max: float | None,
) -> tuple[list[tuple[GridSpec, float]], dict[str, Any]]:
    height, width = rgb.shape[:2]
    minimum = pitch_min if pitch_min is not None else max(4.0, min(height, width) / 220.0)
    maximum = pitch_max if pitch_max is not None else min(min(height, width) / 5.0, minimum + 90.0)
    if minimum >= maximum:
        raise ValueError("--pitch-min must be smaller than --pitch-max")
    try:
        from grid_estimator import estimate_grid  # type: ignore

        result = estimate_grid(rgb, pitch_min=minimum, pitch_max=maximum, top_k=5)
        raw_candidates = result.get("candidates", result) if isinstance(result, dict) else result
        if not isinstance(raw_candidates, list) or not raw_candidates:
            raise ValueError("automatic estimator returned no grid candidates")
        specs: list[tuple[GridSpec, float]] = []
        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            columns = int(item.get("columns", item.get("cols")))
            rows = int(item["rows"])
            pitch = float(item.get("pitch", item.get("cell_size")))
            origin = item.get("origin")
            origin_x = float(item.get("origin_x", origin[0] if origin else 0.0))
            origin_y = float(item.get("origin_y", origin[1] if origin else 0.0))
            score = float(item.get("confidence", item.get("score", 0.0)))
            if columns >= 2 and rows >= 2 and pitch >= 2:
                specs.append((GridSpec(columns, rows, pitch, origin_x, origin_y), score))
        if not specs:
            raise ValueError("automatic estimator returned malformed candidates")
        specs.sort(key=lambda item: item[1], reverse=True)
        if isinstance(result, dict):
            diagnostics = result.get("diagnostics", {})
        else:
            best_item = max(raw_candidates, key=lambda item: float(item.get("confidence", item.get("score", 0.0))))
            diagnostics = best_item.get("diagnostics", {})
        diagnostics = dict(diagnostics) if isinstance(diagnostics, dict) else {}
        diagnostics.setdefault("method", "oriented-edge-circular-coherence")
        diagnostics.setdefault("confidence", specs[0][1])
        return specs, diagnostics
    except (ImportError, AttributeError):
        background = border_background(rgb)
        bbox = foreground_bbox(rgb, background)
        return fallback_auto_specs(rgb, bbox, minimum, maximum)


def resolve_grid(
    rgb: np.ndarray,
    dimensions: tuple[int, int] | None,
    cell_size: float | None,
    origin: tuple[float, float] | None,
    pitch_min: float | None,
    pitch_max: float | None,
    padding: int,
) -> tuple[GridSpec, list[tuple[GridSpec, float]], dict[str, Any]]:
    height, width = rgb.shape[:2]
    if cell_size is not None and cell_size < 2:
        raise ValueError("--cell-size must be at least 2 source pixels")
    if dimensions is not None:
        spec = explicit_grid_spec(width, height, dimensions, cell_size, origin)
        return spec, [(spec, 1.0)], {"method": "user-specified-grid", "confidence": 1.0}

    if cell_size is not None:
        background = border_background(rgb)
        bbox = foreground_bbox(rgb, background)
        if bbox is None:
            raise ValueError("could not find a subject for the supplied cell size")
        x0, y0, x1, y1 = bbox
        columns = max(2, int(round((x1 - x0) / cell_size)))
        rows = max(2, int(round((y1 - y0) / cell_size)))
        if origin is None:
            origin_x = (x0 + x1 - columns * cell_size) / 2.0
            origin_y = (y0 + y1 - rows * cell_size) / 2.0
        else:
            origin_x, origin_y = origin
        tight = GridSpec(columns, rows, cell_size, origin_x, origin_y)
        spec = expand_grid(tight, padding)
        return spec, [(spec, 1.0)], {"method": "user-specified-cell-size", "confidence": 1.0}

    if origin is not None:
        raise ValueError("--origin requires either --grid COLSxROWS or --cell-size")
    tight_specs, diagnostics = automatic_grid_specs(rgb, pitch_min, pitch_max)
    padded = [(expand_grid(spec, padding), score) for spec, score in tight_specs]
    return padded[0][0], padded, diagnostics


def begin_staging(
    target: Path,
    overwrite: bool,
    protected_inputs: Sequence[Path] = (),
) -> Path:
    resolved = _validate_output_target(target, overwrite, protected_inputs)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    # Recheck after creating a missing parent so a path swap cannot silently
    # turn a validated target into an unowned or protected directory.
    resolved = _validate_output_target(resolved, overwrite, protected_inputs)
    return Path(tempfile.mkdtemp(prefix=f".{resolved.name}.staging-", dir=resolved.parent))


def commit_staging(
    staging: Path,
    target: Path,
    overwrite: bool,
    protected_inputs: Sequence[Path] = (),
) -> None:
    _write_output_marker(staging)
    resolved = _validate_output_target(target, overwrite, protected_inputs)
    if resolved.exists():
        shutil.rmtree(resolved)
    os.replace(staging, resolved)
    if resolved.is_symlink() or not _has_valid_output_marker(resolved):
        raise RuntimeError(f"output ownership marker verification failed: {resolved}")


def palette_payload(palette: Sequence[PaletteEntry]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for entry in palette:
        item: dict[str, Any] = {
            "name": entry.name,
            "symbol": entry.symbol,
            "rgb": list(entry.rgb),
            "rgb_hex": "#" + "".join(f"{channel:02X}" for channel in entry.rgb),
            "role": entry.role,
        }
        if entry.code is not None or entry.synthetic:
            item["code"] = entry.code
            item["synthetic"] = entry.synthetic
        result.append(item)
    return result


def build_cells(
    labels: np.ndarray,
    raw_labels: np.ndarray,
    alternatives: np.ndarray,
    confidence: np.ndarray,
    agreement: np.ndarray,
    palette: Sequence[PaletteEntry],
    reasons: dict[tuple[int, int], str],
) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    catalog = any(entry.code is not None or entry.synthetic for entry in palette)
    for row in range(labels.shape[0]):
        for col in range(labels.shape[1]):
            label = palette[int(labels[row, col])]
            raw = palette[int(raw_labels[row, col])]
            alternative = palette[int(alternatives[row, col])]
            cell: dict[str, Any] = {
                "row": row,
                "col": col,
                "label": label.name,
                "symbol": label.symbol,
                "raw_label": raw.name,
                "alternative": alternative.name,
                "confidence": round(float(confidence[row, col]), 6),
                "agreement": round(float(agreement[row, col]), 6),
                "reason": reasons.get((row, col)),
            }
            if catalog:
                cell["code"] = label.code
                cell["raw_code"] = raw.code
                cell["alternative_code"] = alternative.code
            cells.append(cell)
    return cells


def counts_for(labels: np.ndarray, palette: Sequence[PaletteEntry]) -> dict[str, int]:
    return {entry.name: int((labels == index).sum()) for index, entry in enumerate(palette)}


def emit_csv_and_renders(
    output: Path,
    labels: np.ndarray,
    raw_labels: np.ndarray,
    alternatives: np.ndarray,
    confidence: np.ndarray,
    palette: Sequence[PaletteEntry],
    bbox: dict[str, int],
    threshold: float,
    reasons: dict[tuple[int, int], str],
    cell_px: int,
) -> tuple[dict[str, dict[str, Any]], int]:
    artifacts = save_rendered_artifacts(output, labels, confidence, palette, bbox, cell_px, threshold)
    write_matrix_csv(output / "canvas.csv", labels, palette)
    write_matrix_csv(output / "matrix.csv", crop_by_bbox(labels, bbox), palette)
    counts = counts_for(labels, palette)
    write_palette_csv(output / "palette.csv", palette, counts)
    review_count = write_review_csv(
        output / "review.csv",
        labels,
        raw_labels,
        alternatives,
        confidence,
        palette,
        threshold,
        reasons,
    )
    for filename in ("canvas.csv", "matrix.csv", "palette.csv", "review.csv"):
        artifacts[filename] = {"bytes": (output / filename).stat().st_size}
    return artifacts, review_count


def emit_palette_notice(
    output: Path,
    artifacts: dict[str, dict[str, Any]],
    profile: Any,
) -> None:
    """Keep the upstream license beside every full MARD palette copy."""

    if not isinstance(profile, dict) or profile.get("id") != "mard-221-compatible":
        return
    source = SKILL_ROOT / "THIRD_PARTY_NOTICES.md"
    if not source.is_file():
        raise ValueError(f"installed skill is missing the MARD third-party notice: {source}")
    destination = output / "THIRD_PARTY_NOTICES.md"
    shutil.copyfile(source, destination)
    artifacts[destination.name] = {"bytes": destination.stat().st_size}


def board_payload_for(
    result: MoldResult,
    palette: Sequence[PaletteEntry],
) -> dict[str, Any]:
    """Build self-contained board metadata without changing native counts."""

    payload = dict(result.metadata)
    payload["resampled"] = False
    payload["candidates"] = result.candidates
    payload["counts"] = counts_for(result.board_labels, palette)
    background = palette_index(palette, "background")
    payload["bead_count"] = (
        int(result.board_labels.size - (result.board_labels == background).sum())
        if background is not None
        else int(result.board_labels.size)
    )
    placement = dict(payload["placement"])
    content = payload["content_size"]
    size = int(payload["board_size"])
    placement["margins"] = {
        "left": int(placement["col_offset"]),
        "top": int(placement["row_offset"]),
        "right": size - int(placement["col_offset"]) - int(content["columns"]),
        "bottom": size - int(placement["row_offset"]) - int(content["rows"]),
    }
    payload["placement"] = placement
    return payload


def emit_board_artifacts(
    output: Path,
    result: MoldResult,
    palette: Sequence[PaletteEntry],
    threshold: float,
    cell_px: int,
    *,
    source_image: Image.Image | None = None,
) -> dict[str, dict[str, Any]]:
    """Write an optional Wenzhou board as a lossless view of native cells."""

    images = {
        "board_preview.png": render_matrix(
            result.board_labels, palette, cell_px, grid=False
        ),
        "board_grid.png": render_matrix(
            result.board_labels, palette, cell_px, grid=True
        ),
        "board_transparent.png": render_matrix(
            result.board_labels, palette, cell_px, grid=False, transparent=True
        ),
    }
    artifacts: dict[str, dict[str, Any]] = {}
    for filename, rendered in images.items():
        rendered.save(output / filename)
        artifacts[filename] = {
            "width_px": rendered.width,
            "height_px": rendered.height,
            "mode": rendered.mode,
        }
    write_matrix_csv(output / "board.csv", result.board_labels, palette)
    artifacts["board.csv"] = {"bytes": (output / "board.csv").stat().st_size}
    if source_image is not None:
        placement = result.metadata["placement"]
        size = int(result.metadata["board_size"])
        board_spec = GridSpec(
            columns=size,
            rows=size,
            pitch=float(result.metadata["native_pitch_px"]),
            origin_x=float(placement["board_origin_x_px"]),
            origin_y=float(placement["board_origin_y_px"]),
        )
        artifacts["board_source_overlay.png"] = save_source_overlay(
            source_image, board_spec, output / "board_source_overlay.png"
        )
    return artifacts


def restore_command(args: argparse.Namespace) -> int:
    source_path = Path(args.input).expanduser().resolve()
    image = load_source(source_path)
    rgb = np.asarray(image, dtype=np.uint8)
    spec, candidate_specs, grid_diagnostics = resolve_grid(
        rgb,
        args.grid,
        args.cell_size,
        args.origin,
        args.pitch_min,
        args.pitch_max,
        args.padding,
    )
    validate_grid_dimensions(spec.columns, spec.rows)
    background_rgb = border_background(rgb)
    representatives = cell_representatives(rgb, spec, background_rgb)
    palette_bundle = load_palette_bundle(
        args.palette, representatives, background_rgb, args.colors, args.seed
    )
    palette = palette_bundle.entries
    palette_matching: dict[str, Any] | None = None
    if palette_bundle.matching_method == "catalog-lab-ciede2000":
        # A compact provisional palette recovers only subject/background
        # structure. The 221-color catalog is applied afterward to foreground
        # representatives, keeping physical color codes out of pixel-level
        # voting and preserving white bead colors separately from empty space.
        structure_palette = auto_palette(
            representatives, background_rgb, args.colors, args.seed
        )
        raw_structure, structure_confidence, _ = classify_cells(
            rgb, spec, structure_palette
        )
        structure, structure_confidence, reasons, topology, warnings = normalize_light_topology(
            raw_structure,
            structure_confidence,
            structure_palette,
            args.uncertain_threshold,
            args.topology == "auto",
        )
        structure_background = palette_index(structure_palette, "background")
        if structure_background is None:
            raise ValueError("automatic structural palette has no unique background")
        labels, raw_labels, alternatives, color_confidence, structure_to_catalog, palette_matching = quantize_catalog_clusters(
            structure_palette,
            structure,
            raw_structure,
            palette,
            background_rgb,
        )
        agreement, ensemble_count = catalog_ensemble_agreement(
            rgb,
            spec,
            structure_palette,
            structure_to_catalog,
            labels,
            args.uncertain_threshold,
            args.topology == "auto",
        )
        base_confidence = np.minimum(structure_confidence, color_confidence)
        confidence = np.minimum(
            base_confidence,
            np.clip(0.72 * base_confidence + 0.28 * agreement, 0.0, 1.0),
        )
        for coordinate, reason in list(reasons.items()):
            if reason == "edge-connected light cell normalized to background":
                reasons[coordinate] = "edge-connected light cell kept as photographic background before MARD matching"
            elif reason == "enclosed light cell normalized to fill":
                reasons[coordinate] = "enclosed light cell retained as bead foreground before MARD matching"
        topology = {
            **topology,
            "segmentation_palette_size": len(structure_palette),
            "segmentation_method": "automatic-structure-before-catalog-quantization",
        }
    else:
        raw_labels, raw_confidence, alternatives = classify_cells(rgb, spec, palette)
        agreement, ensemble_count = ensemble_agreement(rgb, spec, palette, raw_labels)
        confidence = np.clip(0.62 * raw_confidence + 0.38 * agreement, 0.0, 1.0)
        labels, confidence, reasons, topology, warnings = normalize_light_topology(
            raw_labels,
            confidence,
            palette,
            args.uncertain_threshold,
            args.topology == "auto",
        )
    background = palette_index(palette, "background")
    if background is None:
        raise ValueError("palette must define exactly one background role")
    bbox = content_bbox(labels, background)
    if bbox is None:
        raise ValueError("no subject remained after classification")
    counts = counts_for(labels, palette)
    bead_count = int(labels.size - counts[palette[background].name])
    uncertain_mask = (labels != background) & (confidence < args.uncertain_threshold)
    uncertain_cells = [
        {"row": int(row), "col": int(col)} for row, col in zip(*np.nonzero(uncertain_mask))
    ]
    uncertain_fraction = len(uncertain_cells) / max(1, bead_count)
    grid_confidence = float(grid_diagnostics.get("confidence", candidate_specs[0][1]))
    foreground_confidence = confidence[labels != background]
    mean_cell_confidence = float(foreground_confidence.mean()) if foreground_confidence.size else 0.0
    runner_score = float(candidate_specs[1][1]) if len(candidate_specs) > 1 else 0.0
    candidate_margin = max(0.0, (float(candidate_specs[0][1]) - runner_score) / max(float(candidate_specs[0][1]), 1e-9))
    quality = {
        "grid_confidence": round(grid_confidence, 6),
        "candidate_margin": round(candidate_margin, 6),
        "mean_foreground_cell_confidence": round(mean_cell_confidence, 6),
        "review_cells": len(uncertain_cells),
        "review_ratio": round(uncertain_fraction, 6),
    }
    status = "review"
    if bead_count < 4 or grid_confidence < 0.35 or uncertain_fraction > 0.35:
        status = "fail"
        if bead_count < 4:
            warnings.append("too few non-background cells were recovered")
        if grid_confidence < 0.35:
            warnings.append("grid confidence is below the minimum recoverable threshold")
        if uncertain_fraction > 0.35:
            warnings.append("too many foreground cells require review")
    elif (
        grid_confidence >= 0.80
        and candidate_margin >= 0.08
        and mean_cell_confidence >= 0.88
        and uncertain_fraction <= 0.03
        and not warnings
    ):
        status = "pass"
    if args.palette == "auto":
        warnings.append("auto palette labels are provisional; inspect palette.csv and rerun with a custom palette if colors merge or split")
        status = "review" if status == "pass" else status
    if palette_bundle.profile is not None and palette_bundle.profile.get("provisional"):
        warnings.append(
            "MARD 221 compatible RGB values are community/retailer screen references, "
            "not calibrated physical-bead measurements; confirm codes against the target merchant's card"
        )
        status = "review" if status == "pass" else status

    board_result: MoldResult | None = None
    board_payload: dict[str, Any] | None = None
    if args.board_size != "none":
        try:
            board_result = place_on_wenzhou_mold(
                labels,
                confidence,
                bbox,
                spec,
                image.width,
                image.height,
                mode=args.board_size,
                background_label=background,
            )
            board_payload = board_payload_for(board_result, palette)
            if board_payload["selection_status"] == "review":
                warnings.append(
                    "Wenzhou mold size is a smallest-compatible recommendation, "
                    "not a direct visual detection; confirm 52x52 or 78x78"
                )
                status = "review" if status == "pass" else status
        except MoldCapacityError as exc:
            board_payload = {
                "standard": "wenzhou",
                "mode": "auto" if args.board_size == "auto" else "explicit",
                "selection_status": "fail",
                "reason": str(exc),
                "resampled": False,
                "content_size": {
                    "columns": bbox["right_exclusive"] - bbox["left"],
                    "rows": bbox["bottom_exclusive"] - bbox["top"],
                },
            }
            warnings.append(f"Wenzhou mold capacity failure: {exc}")
            status = "fail"

    protected_inputs = [source_path]
    palette_name = str(args.palette).strip().lower()
    if palette_name not in {"auto", "warm-mascot", *BUILTIN_PALETTE_ALIASES}:
        protected_inputs.append(Path(args.palette).expanduser().resolve())
    target = Path(args.out)
    staging = begin_staging(target, args.overwrite, protected_inputs)
    try:
        artifacts, review_count = emit_csv_and_renders(
            staging,
            labels,
            raw_labels,
            alternatives,
            confidence,
            palette,
            bbox,
            args.uncertain_threshold,
            reasons,
            args.render_cell_px,
        )
        emit_palette_notice(staging, artifacts, palette_bundle.profile)
        artifacts["source_grid_overlay.png"] = save_source_overlay(
            image, spec, staging / "source_grid_overlay.png"
        )
        artifacts["candidates.png"] = candidate_contact_sheet(
            image, candidate_specs, staging / "candidates.png"
        )
        if board_result is not None:
            artifacts.update(
                emit_board_artifacts(
                    staging,
                    board_result,
                    palette,
                    args.uncertain_threshold,
                    args.render_cell_px,
                    source_image=image,
                )
            )
        cells = build_cells(
            labels, raw_labels, alternatives, confidence, agreement, palette, reasons
        )
        candidates_payload = [
            {
                "rank": index,
                "columns": candidate.columns,
                "rows": candidate.rows,
                "pitch_px": round(candidate.pitch, 6),
                "origin_x_px": round(candidate.origin_x, 6),
                "origin_y_px": round(candidate.origin_y, 6),
                "score": round(float(score), 6),
            }
            for index, (candidate, score) in enumerate(candidate_specs, start=1)
        ]
        (staging / "candidates.json").write_text(
            json.dumps(candidates_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        artifacts["candidates.json"] = {"bytes": (staging / "candidates.json").stat().st_size}
        pattern = {
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "status": status,
            "source": {
                "sha256": source_sha256(source_path),
                "width_px": image.width,
                "height_px": image.height,
            },
            "grid": {
                "columns": spec.columns,
                "rows": spec.rows,
                "pitch_px": round(spec.pitch, 6),
                "origin_x_px": round(spec.origin_x, 6),
                "origin_y_px": round(spec.origin_y, 6),
                "method": grid_diagnostics.get("method", "unknown"),
                "confidence": round(grid_confidence, 6),
                "candidates": candidates_payload,
                "diagnostics": {
                    key: value
                    for key, value in grid_diagnostics.items()
                    if key not in {"method", "confidence", "candidates"}
                },
            },
            "content_bbox": bbox,
            "palette": palette_payload(palette),
            "counts": counts,
            "bead_count": bead_count,
            "uncertain_threshold": args.uncertain_threshold,
            "uncertain_cells": uncertain_cells,
            "postprocess": {
                **topology,
                "ensemble_candidates": ensemble_count,
                **({"palette_matching": palette_matching} if palette_matching is not None else {}),
            },
            "warnings": warnings,
            "quality": quality,
            "cells": cells,
            "artifacts": artifacts,
        }
        if palette_bundle.profile is not None:
            pattern["palette_profile"] = palette_bundle.profile
        if board_payload is not None:
            pattern["board"] = board_payload
        (staging / "pattern.json").write_text(
            json.dumps(pattern, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        artifacts["pattern.json"] = {"bytes": (staging / "pattern.json").stat().st_size}
        summary = {
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "status": status,
            "grid": pattern["grid"],
            "content_bbox": bbox,
            "counts": counts,
            "bead_count": bead_count,
            "uncertain_count": review_count,
            "quality": quality,
            "warnings": warnings,
            "artifacts": sorted([*artifacts, "summary.json"]),
            "output_dir": Path(args.out).name,
        }
        if palette_bundle.profile is not None:
            summary["palette_profile"] = palette_bundle.profile
        if board_payload is not None:
            summary["board"] = board_payload
        (staging / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        commit_staging(staging, target, args.overwrite, protected_inputs)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 2 if args.strict and status != "pass" else 0


def load_pattern(path: Path) -> tuple[dict[str, Any], tuple[PaletteEntry, ...], np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    payload = _read_json_file(path, MAX_PATTERN_JSON_BYTES, "pattern JSON")
    if not isinstance(payload, dict):
        raise ValueError("pattern JSON root must be an object")
    grid = payload.get("grid")
    if not isinstance(grid, dict):
        raise ValueError("pattern JSON grid must be an object")
    try:
        columns, rows = int(grid["columns"]), int(grid["rows"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("pattern grid dimensions are invalid") from exc
    validate_grid_dimensions(columns, rows, "pattern grid")

    raw_palette = payload.get("palette")
    if not isinstance(raw_palette, list):
        raise ValueError("pattern palette must be a list")
    if not 2 <= len(raw_palette) <= MAX_PALETTE_ENTRIES:
        raise ValueError(
            f"pattern palette must contain between 2 and {MAX_PALETTE_ENTRIES} entries"
        )
    palette_entries: list[PaletteEntry] = []
    try:
        for entry in raw_palette:
            if not isinstance(entry, dict):
                raise ValueError("pattern palette entries must be objects")
            rgb_value = entry["rgb"]
            if not isinstance(rgb_value, list) or len(rgb_value) != 3:
                raise ValueError("pattern palette RGB values must contain three channels")
            palette_entries.append(
                PaletteEntry(
                    str(entry["name"]),
                    str(entry["symbol"]),
                    tuple(int(value) for value in rgb_value),  # type: ignore[arg-type]
                    str(entry.get("role", "accent")),
                    str(entry["code"]) if entry.get("code") is not None else None,
                    bool(entry.get("synthetic", False)),
                )
            )
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("pattern palette contains an invalid entry") from exc
    palette = tuple(palette_entries)
    _validate_palette_entries(palette, "pattern palette")

    cells = payload.get("cells")
    if not isinstance(cells, list) or len(cells) != rows * columns:
        raise ValueError("pattern cells do not cover the full grid")
    name_to_index = {entry.name: index for index, entry in enumerate(palette)}
    labels = np.zeros((rows, columns), dtype=np.int16)
    raw_labels = np.zeros_like(labels)
    alternatives = np.zeros_like(labels)
    confidence = np.zeros((rows, columns), dtype=np.float32)
    seen: set[tuple[int, int]] = set()
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValueError("pattern cells must be objects")
        try:
            row, col = int(cell["row"]), int(cell["col"])
            label_name = str(cell["label"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("pattern cell contains invalid coordinates or label") from exc
        if not (0 <= row < rows and 0 <= col < columns) or (row, col) in seen:
            raise ValueError("pattern cells contain invalid or duplicate coordinates")
        if label_name not in name_to_index:
            raise ValueError(f"pattern cell uses unknown palette label: {label_name}")
        seen.add((row, col))
        labels[row, col] = name_to_index[label_name]
        raw_labels[row, col] = name_to_index.get(str(cell.get("raw_label", cell["label"])), labels[row, col])
        alternatives[row, col] = name_to_index.get(str(cell.get("alternative", cell["label"])), labels[row, col])
        try:
            confidence_value = float(cell.get("confidence", 1.0))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("pattern cell confidence is invalid") from exc
        if not math.isfinite(confidence_value) or not 0.0 <= confidence_value <= 1.0:
            raise ValueError("pattern cell confidence must be finite and between 0 and 1")
        confidence[row, col] = confidence_value
    return payload, palette, labels, raw_labels, alternatives, confidence


def _validated_stored_bbox(
    payload: dict[str, Any], labels: np.ndarray, background: int
) -> dict[str, int]:
    """Return a manifest bbox only when it still agrees with its cells."""

    stored = payload.get("content_bbox")
    if not isinstance(stored, dict):
        raise ValueError("pattern has no valid content_bbox")
    try:
        bbox = {
            "left": int(stored["left"]),
            "top": int(stored["top"]),
            "right_exclusive": int(stored["right_exclusive"]),
            "bottom_exclusive": int(stored["bottom_exclusive"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("pattern has no valid content_bbox") from exc
    computed = content_bbox(labels, background)
    if computed is None:
        raise ValueError("pattern contains no non-background cells")
    if bbox != computed:
        raise ValueError("pattern content_bbox does not match its cell labels")
    return bbox


def _agreement_matrix(payload: dict[str, Any], shape: tuple[int, int]) -> np.ndarray:
    agreement = np.ones(shape, dtype=np.float32)
    for cell in payload.get("cells", []):
        row, col = int(cell["row"]), int(cell["col"])
        value = float(cell.get("agreement", 1.0))
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("pattern cell agreement must be finite and between 0 and 1")
        agreement[row, col] = value
    return agreement


def _repeat_cells(array: np.ndarray, factor: int) -> np.ndarray:
    """Integer nearest-neighbor expansion in logical-cell space."""

    return np.repeat(np.repeat(array, factor, axis=0), factor, axis=1)


def scale_pattern_command(args: argparse.Namespace) -> int:
    """Create an explicitly authorized larger design from a recovered pattern.

    Scaling happens after photographic recovery. Each source logical cell is
    copied into an exact ``factor x factor`` block; the source photograph is
    never sampled again, so fibers, shadows, and compression noise cannot turn
    into new colors or invented detail. The derived cells are then placed
    one-for-one on the requested board.
    """

    pattern_path = Path(args.pattern_json).expanduser().resolve()
    payload, palette, labels, raw_labels, alternatives, confidence = load_pattern(
        pattern_path
    )
    background = palette_index(palette, "background")
    if background is None:
        raise ValueError("pattern palette has no unique background role")
    source_bbox = _validated_stored_bbox(payload, labels, background)
    factor = int(args.factor)

    source_labels = crop_by_bbox(labels, source_bbox)
    source_raw = crop_by_bbox(raw_labels, source_bbox)
    source_alternatives = crop_by_bbox(alternatives, source_bbox)
    source_confidence = crop_by_bbox(confidence, source_bbox)
    source_agreement = crop_by_bbox(
        _agreement_matrix(payload, labels.shape), source_bbox
    )
    source_height, source_width = source_labels.shape
    output_width = source_width * factor
    output_height = source_height * factor
    board_size = int(args.board_size.split("x", 1)[0])
    if output_width > board_size or output_height > board_size:
        raise ValueError(
            f"scaled content {output_width}x{output_height} does not fit "
            f"{board_size}x{board_size}; choose a smaller --factor or a larger supported mold"
        )

    scaled_labels = _repeat_cells(source_labels, factor)
    scaled_raw = _repeat_cells(source_raw, factor)
    scaled_alternatives = _repeat_cells(source_alternatives, factor)
    scaled_confidence = _repeat_cells(source_confidence, factor)
    scaled_agreement = _repeat_cells(source_agreement, factor)
    bbox = {
        "left": 0,
        "top": 0,
        "right_exclusive": output_width,
        "bottom_exclusive": output_height,
    }

    reasons: dict[tuple[int, int], str] = {}
    for cell in payload.get("cells", []):
        reason = cell.get("reason")
        if not reason:
            continue
        source_row = int(cell["row"])
        source_col = int(cell["col"])
        if not (
            source_bbox["top"] <= source_row < source_bbox["bottom_exclusive"]
            and source_bbox["left"] <= source_col < source_bbox["right_exclusive"]
        ):
            continue
        output_row = (source_row - source_bbox["top"]) * factor
        output_col = (source_col - source_bbox["left"]) * factor
        for row_delta in range(factor):
            for col_delta in range(factor):
                reasons[(output_row + row_delta, output_col + col_delta)] = str(reason)

    threshold = float(payload.get("uncertain_threshold", 0.62))
    counts = counts_for(scaled_labels, palette)
    bead_count = int(scaled_labels.size - counts[palette[background].name])
    uncertain_mask = (scaled_labels != background) & (scaled_confidence < threshold)
    uncertain = [
        {"row": int(row), "col": int(col)}
        for row, col in zip(*np.nonzero(uncertain_mask))
    ]
    foreground_confidence = scaled_confidence[scaled_labels != background]
    mean_confidence = (
        float(foreground_confidence.mean()) if foreground_confidence.size else 0.0
    )
    old_quality = payload.get("quality", {})
    review_ratio = len(uncertain) / max(1, bead_count)
    quality = {
        "grid_confidence": round(
            float(
                old_quality.get(
                    "grid_confidence", payload.get("grid", {}).get("confidence", 0.0)
                )
            ),
            6,
        ),
        "candidate_margin": round(float(old_quality.get("candidate_margin", 0.0)), 6),
        "mean_foreground_cell_confidence": round(mean_confidence, 6),
        "review_cells": len(uncertain),
        "review_ratio": round(review_ratio, 6),
    }
    warnings = [
        str(warning)
        for warning in payload.get("warnings", [])
        if not str(warning).startswith("Wenzhou mold size is a smallest-compatible recommendation")
    ]

    source_grid = dict(payload.get("grid", {}))
    source_pitch = float(source_grid.get("pitch_px", 1.0))
    derived_pitch = source_pitch / factor
    grid_confidence = float(source_grid.get("confidence", quality["grid_confidence"]))
    derived_grid = {
        "columns": output_width,
        "rows": output_height,
        "pitch_px": round(derived_pitch, 6),
        "origin_x_px": 0.0,
        "origin_y_px": 0.0,
        "method": "integer-nearest-neighbor-content-scale",
        "confidence": round(grid_confidence, 6),
        "candidates": [],
        "diagnostics": {
            "coordinate_space": "derived-content-local",
            "factor": factor,
            "source_grid": {
                key: source_grid[key]
                for key in (
                    "columns",
                    "rows",
                    "pitch_px",
                    "origin_x_px",
                    "origin_y_px",
                    "method",
                    "confidence",
                )
                if key in source_grid
            },
        },
    }
    source_size = {"columns": source_width, "rows": source_height}
    output_size = {"columns": output_width, "rows": output_height}
    previous_derivation = payload.get("derivation")
    prior_cumulative = (
        int(previous_derivation.get("cumulative_factor", previous_derivation.get("factor", 1)))
        if isinstance(previous_derivation, dict)
        else 1
    )
    derivation: dict[str, Any] = {
        "kind": "integer-nearest-neighbor-content-scale",
        "resampled": True,
        "authorized_by": "explicit-user-request",
        "factor": factor,
        "cumulative_factor": prior_cumulative * factor,
        "source_pattern_sha256": source_sha256(pattern_path),
        "source_content_bbox": dict(source_bbox),
        "source_content_size": source_size,
        "output_content_size": output_size,
        "label_mapping": "exact-cell-replication",
        "detail_policy": "no-new-detail-from-source-photo",
    }
    if isinstance(previous_derivation, dict):
        derivation["parent"] = previous_derivation

    design_spec = GridSpec(
        columns=output_width,
        rows=output_height,
        pitch=derived_pitch,
        origin_x=0.0,
        origin_y=0.0,
    )
    board_result = place_on_wenzhou_mold(
        scaled_labels,
        scaled_confidence,
        bbox,
        design_spec,
        output_width * derived_pitch,
        output_height * derived_pitch,
        mode=args.board_size,
        background_label=background,
    )
    board_payload = board_payload_for(board_result, palette)
    # The design was resampled deliberately, but placement of the resulting
    # cells on the physical board is still one logical cell to one board hole.
    board_payload["resampled"] = False
    board_payload["design_derivation"] = {
        "kind": derivation["kind"],
        "factor": factor,
        "source_content_size": source_size,
        "output_content_size": output_size,
    }

    protected_inputs = [pattern_path]
    target = Path(args.out)
    staging = begin_staging(target, args.overwrite, protected_inputs)
    try:
        artifacts, review_count = emit_csv_and_renders(
            staging,
            scaled_labels,
            scaled_raw,
            scaled_alternatives,
            scaled_confidence,
            palette,
            bbox,
            threshold,
            reasons,
            args.render_cell_px,
        )
        emit_palette_notice(staging, artifacts, payload.get("palette_profile"))
        artifacts.update(
            emit_board_artifacts(
                staging,
                board_result,
                palette,
                threshold,
                args.render_cell_px,
            )
        )
        new_payload = dict(payload)
        new_payload.update(
            {
                "schema_version": SCHEMA_VERSION,
                "algorithm_version": ALGORITHM_VERSION,
                "status": str(payload.get("status", "review")),
                "resampled": True,
                "derivation": derivation,
                "grid": derived_grid,
                "content_bbox": bbox,
                "counts": counts,
                "bead_count": bead_count,
                "uncertain_cells": uncertain,
                "quality": quality,
                "warnings": warnings,
                "cells": build_cells(
                    scaled_labels,
                    scaled_raw,
                    scaled_alternatives,
                    scaled_confidence,
                    scaled_agreement,
                    palette,
                    reasons,
                ),
                "board": board_payload,
                "artifacts": artifacts,
            }
        )
        postprocess = dict(new_payload.get("postprocess", {}))
        postprocess["design_scale"] = {
            "method": "integer-nearest-neighbor-cell-repeat",
            "factor": factor,
            "source_content_size": source_size,
            "output_content_size": output_size,
        }
        new_payload["postprocess"] = postprocess
        (staging / "pattern.json").write_text(
            json.dumps(new_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        summary: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "status": new_payload["status"],
            "resampled": True,
            "derivation": derivation,
            "grid": derived_grid,
            "content_bbox": bbox,
            "counts": counts,
            "bead_count": bead_count,
            "uncertain_count": review_count,
            "quality": quality,
            "warnings": warnings,
            "board": board_payload,
            "artifacts": sorted([*artifacts, "pattern.json", "summary.json"]),
            "output_dir": Path(args.out).name,
        }
        if "palette_profile" in new_payload:
            summary["palette_profile"] = new_payload["palette_profile"]
        (staging / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        commit_staging(staging, target, args.overwrite, protected_inputs)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def rebuild_existing_board(
    payload: dict[str, Any],
    labels: np.ndarray,
    confidence: np.ndarray,
    bbox: dict[str, int],
    palette: Sequence[PaletteEntry],
    background: int,
) -> tuple[MoldResult | None, dict[str, Any] | None]:
    """Rerender an existing board choice without rerunning mold inference."""

    previous = payload.get("board")
    if not isinstance(previous, dict):
        return None, None
    board_size_value = previous.get("board_size")
    if board_size_value is None or previous.get("selection_status") == "fail":
        return None, dict(previous)
    board_size_text = str(board_size_value).lower().replace("×", "x").split("x", 1)[0]
    try:
        board_size = int(board_size_text)
    except ValueError as exc:
        raise ValueError("pattern board_size must be 52 or 78") from exc
    grid = payload.get("grid", {})
    source = payload.get("source", {})
    spec = GridSpec(
        columns=int(grid["columns"]),
        rows=int(grid["rows"]),
        pitch=float(grid["pitch_px"]),
        origin_x=float(grid["origin_x_px"]),
        origin_y=float(grid["origin_y_px"]),
    )
    result = place_on_wenzhou_mold(
        labels,
        confidence,
        bbox,
        spec,
        float(source["width_px"]),
        float(source["height_px"]),
        mode=f"{board_size}x{board_size}",
        background_label=background,
    )
    previous_placement = previous.get("placement")
    if isinstance(previous_placement, dict):
        previous_bbox = previous_placement.get("source_bbox", {})
        previous_shift = previous_placement.get("native_to_board_shift", {})
        try:
            column_shift = int(
                previous_shift.get(
                    "columns",
                    int(previous_placement["col_offset"]) - int(previous_bbox["left"]),
                )
            )
            row_shift = int(
                previous_shift.get(
                    "rows",
                    int(previous_placement["row_offset"]) - int(previous_bbox["top"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            column_shift = row_shift = 0
        content_width = bbox["right_exclusive"] - bbox["left"]
        content_height = bbox["bottom_exclusive"] - bbox["top"]
        col_offset = bbox["left"] + column_shift
        row_offset = bbox["top"] + row_shift
        preserves_mapping = (
            0 <= col_offset <= board_size - content_width
            and 0 <= row_offset <= board_size - content_height
        )
        if preserves_mapping:
            board_labels = np.full(
                (board_size, board_size), background, dtype=labels.dtype
            )
            board_confidence = np.ones(
                (board_size, board_size),
                dtype=np.result_type(confidence.dtype, np.float32),
            )
            board_labels[
                row_offset : row_offset + content_height,
                col_offset : col_offset + content_width,
            ] = crop_by_bbox(labels, bbox)
            board_confidence[
                row_offset : row_offset + content_height,
                col_offset : col_offset + content_width,
            ] = crop_by_bbox(confidence, bbox)
            metadata = dict(result.metadata)
            placement = dict(metadata["placement"])
            for key in (
                "board_origin_x_px",
                "board_origin_y_px",
                "centered_target_origin_x_px",
                "centered_target_origin_y_px",
                "centering_error_x_px",
                "centering_error_y_px",
            ):
                if key in previous_placement:
                    placement[key] = previous_placement[key]
            placement.update(
                {
                    "row_offset": row_offset,
                    "col_offset": col_offset,
                    "source_bbox": dict(bbox),
                    "native_to_board_shift": {
                        "columns": column_shift,
                        "rows": row_shift,
                    },
                }
            )
            metadata["placement"] = placement
            result = MoldResult(
                board_labels=board_labels,
                board_confidence=board_confidence,
                metadata=metadata,
                candidates=result.candidates,
            )
        else:
            # The selected size still fits, but an edit expanded content beyond
            # the old absolute board mapping. The module supplies a new valid
            # phase-aware placement and records that it moved.
            result.metadata["placement"]["repositioned_after_revision"] = True
    updated = board_payload_for(result, palette)
    # Board size selection is provenance. Revision may update the content bbox
    # and placement, but it must not silently reclassify an earlier automatic
    # recommendation as an explicit or newly detected board.
    for key in ("mode", "selection_status", "selection_confidence", "reason"):
        if key in previous:
            updated[key] = previous[key]
    if "design_derivation" in previous:
        updated["design_derivation"] = previous["design_derivation"]
    if isinstance(previous_placement, dict) and previous_placement.get("source_bbox") == bbox:
        # A pure rerender (or a color-only edit) must be byte-stable at the
        # metadata level. Recomputing candidate projections from the six-place
        # grid values stored in JSON can otherwise cross a half-cell rounding
        # boundary or perturb diagnostics by a few decimals.
        preserved = dict(previous)
        preserved["counts"] = updated["counts"]
        preserved["bead_count"] = updated["bead_count"]
        # Board placement remains one-to-one even when the input design was an
        # explicitly scaled derivative. Preserve that distinction and its
        # design provenance through render/revise.
        preserved["resampled"] = False
        updated = preserved
    return result, updated


def revise_or_render_command(args: argparse.Namespace, revise: bool) -> int:
    pattern_path = Path(args.pattern_json).expanduser().resolve()
    protected_inputs = [pattern_path]
    payload, palette, labels, raw_labels, alternatives, confidence = load_pattern(pattern_path)
    reasons: dict[tuple[int, int], str] = {}
    for cell in payload["cells"]:
        if cell.get("reason"):
            reasons[(int(cell["row"]), int(cell["col"]))] = str(cell["reason"])
    agreement = np.asarray(
        [float(cell.get("agreement", 1.0)) for cell in payload["cells"]], dtype=np.float32
    ).reshape(labels.shape)
    edits_applied = 0
    if revise:
        name_lookup = {entry.name: index for index, entry in enumerate(palette)}
        symbol_lookup = {entry.symbol: index for index, entry in enumerate(palette)}
        code_lookup = {entry.code: index for index, entry in enumerate(palette) if entry.code is not None}
        edits_path = Path(args.edits).expanduser().resolve()
        _require_regular_file_size(edits_path, MAX_EDITS_CSV_BYTES, "edits CSV")
        protected_inputs.append(edits_path)
        try:
            with edits_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except Exception as exc:
            raise ValueError(f"cannot read edits CSV: {edits_path}") from exc
        if len(rows) > MAX_GRID_CELLS:
            raise ValueError(f"edits CSV exceeds {MAX_GRID_CELLS:,} rows")
        seen_edits: set[tuple[int, int]] = set()
        for edit in rows:
            row, col = int(edit["row"]), int(edit["col"])
            if not (0 <= row < labels.shape[0] and 0 <= col < labels.shape[1]):
                raise ValueError(f"edit coordinate is outside the grid: ({row},{col})")
            if (row, col) in seen_edits:
                raise ValueError(f"duplicate edit coordinate: ({row},{col})")
            requested = (
                edit.get("new_label")
                if "new_label" in edit
                else (edit.get("label") or edit.get("symbol") or "")
            )
            requested = (requested or "").strip()
            if not requested:
                continue
            seen_edits.add((row, col))
            if requested in name_lookup:
                index = name_lookup[requested]
            elif requested in symbol_lookup:
                index = symbol_lookup[requested]
            elif requested in code_lookup:
                index = code_lookup[requested]
            else:
                raise ValueError(f"unknown edit label, symbol, or code: {requested}")
            labels[row, col] = index
            alternatives[row, col] = index
            confidence[row, col] = 1.0
            agreement[row, col] = 1.0
            reasons[(row, col)] = "user-confirmed edit"
            edits_applied += 1

    background = palette_index(palette, "background")
    if background is None:
        raise ValueError("pattern palette has no unique background role")
    bbox = content_bbox(labels, background)
    if bbox is None:
        raise ValueError("pattern contains no non-background cells")
    threshold = float(payload.get("uncertain_threshold", 0.62))
    counts = counts_for(labels, palette)
    uncertain_mask = (labels != background) & (confidence < threshold)
    uncertain = [
        {"row": int(row), "col": int(col)} for row, col in zip(*np.nonzero(uncertain_mask))
    ]
    bead_count = int(labels.size - counts[palette[background].name])
    review_ratio = len(uncertain) / max(1, bead_count)
    mean_confidence = float(confidence[labels != background].mean()) if bead_count else 0.0
    old_quality = payload.get("quality", {})
    grid_confidence = float(old_quality.get("grid_confidence", payload.get("grid", {}).get("confidence", 0.0)))
    candidate_margin = float(old_quality.get("candidate_margin", 0.0))
    warnings = list(payload.get("warnings", []))
    quality = {
        "grid_confidence": round(grid_confidence, 6),
        "candidate_margin": round(candidate_margin, 6),
        "mean_foreground_cell_confidence": round(mean_confidence, 6),
        "review_cells": len(uncertain),
        "review_ratio": round(review_ratio, 6),
    }
    if bead_count < 4 or grid_confidence < 0.35 or review_ratio > 0.35:
        status = "fail"
    elif (
        grid_confidence >= 0.80
        and candidate_margin >= 0.08
        and mean_confidence >= 0.88
        and review_ratio <= 0.03
        and not warnings
    ):
        status = "pass"
    else:
        status = "review"
    board_result: MoldResult | None = None
    board_payload: dict[str, Any] | None = None
    try:
        board_result, board_payload = rebuild_existing_board(
            payload, labels, confidence, bbox, palette, background
        )
    except MoldCapacityError as exc:
        previous_board = payload.get("board", {})
        board_payload = {
            "standard": "wenzhou",
            "mode": previous_board.get("mode", "explicit"),
            "board_size": previous_board.get("board_size"),
            "selection_status": "fail",
            "reason": str(exc),
            "resampled": False,
            "content_size": {
                "columns": bbox["right_exclusive"] - bbox["left"],
                "rows": bbox["bottom_exclusive"] - bbox["top"],
            },
        }
        if "design_derivation" in previous_board:
            board_payload["design_derivation"] = previous_board["design_derivation"]
        warning = f"Wenzhou mold capacity failure after revision: {exc}"
        if warning not in warnings:
            warnings.append(warning)
        status = "fail"
    if board_payload is not None:
        if board_payload.get("selection_status") == "fail":
            status = "fail"
        elif board_payload.get("selection_status") == "review":
            status = "review" if status == "pass" else status
    target = Path(args.out)
    staging = begin_staging(target, args.overwrite, protected_inputs)
    try:
        artifacts, review_count = emit_csv_and_renders(
            staging,
            labels,
            raw_labels,
            alternatives,
            confidence,
            palette,
            bbox,
            threshold,
            reasons,
            args.render_cell_px,
        )
        emit_palette_notice(staging, artifacts, payload.get("palette_profile"))
        if board_result is not None:
            artifacts.update(
                emit_board_artifacts(
                    staging,
                    board_result,
                    palette,
                    threshold,
                    args.render_cell_px,
                )
            )
        new_payload = dict(payload)
        new_payload.update(
            {
                "schema_version": SCHEMA_VERSION,
                "algorithm_version": ALGORITHM_VERSION,
                "status": status,
                "content_bbox": bbox,
                "counts": counts,
                "bead_count": bead_count,
                "uncertain_cells": uncertain,
                "quality": quality,
                "warnings": warnings,
                "cells": build_cells(labels, raw_labels, alternatives, confidence, agreement, palette, reasons),
                "artifacts": artifacts,
            }
        )
        if board_payload is not None:
            new_payload["board"] = board_payload
        else:
            new_payload.pop("board", None)
        if revise:
            revisions = list(new_payload.get("revisions", []))
            revisions.append({"kind": "user-confirmed-csv-edits", "count": edits_applied})
            new_payload["revisions"] = revisions
        (staging / "pattern.json").write_text(
            json.dumps(new_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        summary = {
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "status": status,
            "grid": new_payload["grid"],
            "content_bbox": bbox,
            "counts": counts,
            "bead_count": new_payload["bead_count"],
            "uncertain_count": review_count,
            "quality": quality,
            "warnings": warnings,
            "edits_applied": edits_applied,
            "artifacts": sorted([*artifacts, "pattern.json", "summary.json"]),
            "output_dir": Path(args.out).name,
        }
        if "palette_profile" in new_payload:
            summary["palette_profile"] = new_payload["palette_profile"]
        if board_payload is not None:
            summary["board"] = board_payload
        (staging / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        commit_staging(staging, target, args.overwrite, protected_inputs)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def bounded_int(minimum: int, maximum: int):
    def parse(value: str) -> int:
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(f"value must be between {minimum} and {maximum}")
        return parsed

    return parse


def probability(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("value must be between 0 and 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Restore an existing discrete cell grid, or explicitly derive an "
            "integer-scaled design from a recovered pattern."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    restore = subparsers.add_parser("restore", help="recover a grid pattern from an image")
    restore.add_argument("input", help="source image")
    restore.add_argument("--out", required=True, help="new output directory")
    restore.add_argument("--grid", type=parse_grid, default=None, metavar="auto|COLSxROWS")
    restore.add_argument("--cell-size", type=float, help="cell pitch in source-image pixels")
    restore.add_argument("--origin", type=parse_origin, help="top-left grid origin as X,Y")
    restore.add_argument("--pitch-min", type=float, help="minimum automatic pitch")
    restore.add_argument("--pitch-max", type=float, help="maximum automatic pitch")
    restore.add_argument("--padding", type=bounded_int(0, 10), default=1, help="background cells added around an auto grid")
    restore.add_argument(
        "--board-size",
        type=parse_board_size,
        default="none",
        metavar="none|auto|52x52|78x78",
        help=(
            "optionally place the recovered native cells on a 52x52 or 78x78 "
            "Wenzhou fuse-bead mold; this never changes --grid or resamples cells"
        ),
    )
    restore.add_argument(
        "--palette",
        default="auto",
        help="auto, warm-mascot, mard-221-compatible (alias mard-221), or a palette JSON path",
    )
    restore.add_argument(
        "--colors",
        type=bounded_int(2, 16),
        default=6,
        help="auto-palette cluster count; with MARD 221 this controls structural segmentation only",
    )
    restore.add_argument("--uncertain-threshold", type=probability, default=0.62)
    restore.add_argument("--seed", type=int, default=0)
    restore.add_argument("--render-cell-px", type=bounded_int(4, 64), default=22)
    restore.add_argument("--topology", choices=("auto", "off"), default="auto")
    restore.add_argument("--strict", action="store_true", help="return nonzero unless status is pass")
    restore.add_argument("--overwrite", action="store_true")
    restore.set_defaults(handler=restore_command)

    scale = subparsers.add_parser(
        "scale",
        help="derive a larger design by exact integer replication of recovered cells",
    )
    scale.add_argument("pattern_json", help="existing recovered pattern JSON")
    scale.add_argument(
        "--factor",
        type=bounded_int(2, 8),
        required=True,
        help="integer cell-replication factor (2..8)",
    )
    scale.add_argument(
        "--board-size",
        type=parse_explicit_board_size,
        required=True,
        metavar="52x52|78x78",
        help="explicit Wenzhou mold for the derived design",
    )
    scale.add_argument("--out", required=True)
    scale.add_argument("--render-cell-px", type=bounded_int(4, 64), default=22)
    scale.add_argument("--overwrite", action="store_true")
    scale.set_defaults(handler=scale_pattern_command)

    revise = subparsers.add_parser("revise", help="apply user-confirmed cell edits")
    revise.add_argument("pattern_json")
    revise.add_argument("--edits", required=True, help="CSV with row,col,label or row,col,symbol")
    revise.add_argument("--out", required=True)
    revise.add_argument("--render-cell-px", type=bounded_int(4, 64), default=22)
    revise.add_argument("--overwrite", action="store_true")
    revise.set_defaults(handler=lambda args: revise_or_render_command(args, True))

    render = subparsers.add_parser("render", help="rerender an existing pattern JSON")
    render.add_argument("pattern_json")
    render.add_argument("--out", required=True)
    render.add_argument("--render-cell-px", type=bounded_int(4, 64), default=22)
    render.add_argument("--overwrite", action="store_true")
    render.set_defaults(handler=lambda args: revise_or_render_command(args, False))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        log(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
