#!/usr/bin/env python3
"""Create a new 52x52 or 78x78 MARD-compatible design from an ordinary image.

This command is deliberately separate from ``restore_pattern.py``.  It makes
new pixel-art decisions and always reports ``not_restoration: true``; it must
never be presented as recovery of a logical grid that already existed in the
source image.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageOps

try:
    from restore_pattern import (
        MAX_RENDER_TOTAL_PIXELS,
        PaletteEntry,
        begin_staging,
        border_background,
        commit_staging,
        delta_e_2000,
        load_palette_bundle,
        load_source,
        render_matrix,
        source_sha256,
        srgb_to_lab,
    )
except ImportError as exc:  # pragma: no cover - damaged skill installation
    raise SystemExit(
        "design_bead_pattern.py requires the bundled restore_pattern.py module; "
        "reinstall the complete restore-bead-pattern skill."
    ) from exc


ALGORITHM_VERSION = "design-1.0.0"
SCHEMA_VERSION = "design-1.0"
DEFAULT_SEED = 0


def parse_board_size(value: str) -> int:
    normalized = value.strip().lower().replace("×", "x")
    if normalized not in {"52x52", "78x78"}:
        raise argparse.ArgumentTypeError("board size must be 52x52 or 78x78")
    return int(normalized.split("x", 1)[0])


def design_rights_payload() -> dict[str, Any]:
    return {
        "source_kind": "user-provided-image",
        "rights_or_authorization_verified_by_tool": False,
        "privacy_or_portrait_consent_verified_by_tool": False,
        "output_may_remain_identifiable": True,
        "commercial_copying_or_public_redistribution_rights_granted_by_tool": False,
        "use_condition": "use-only-where-user-already-holds-required-rights-or-authorization",
        "source_image_included": False,
        "notice_artifact": "DESIGN_RIGHTS_NOTICE.md",
    }


def design_rights_notice() -> str:
    return """# Design Rights Notice / 新设计权利声明

## 中文

- 本工具将用户提供的图片转换为新的拼豆设计，不会核验输入图片的版权、商标、隐私、肖像权或当事人同意状态。
- 仅可在使用者已经拥有所需权利或明确授权的范围内使用生成结果。
- 本工具及其输出不授予商业复制、销售、公开发布或再分发输入图片或相关衍生内容的权利。
- 生成图仍可能识别出人物、作品或其他敏感信息，应将其作为敏感衍生内容处理；未取得所需同意时不得公开发布。
- 输出文件不包含用户提供的源图；仅保留稳定的 SHA-256 文件指纹与像素尺寸。该指纹可用于匹配相同文件，不应视为匿名化保证。

## English

- This tool converts a user-provided image into a new bead design. It does not verify copyright, trademark, privacy, portrait or publicity rights, consent, or other authorization for the input.
- Use generated artifacts only where the user already holds the necessary rights or explicit authorization.
- Neither the tool nor its outputs grant rights to commercially reproduce, sell, publicly publish, or redistribute the input or related derivative content.
- A generated design may remain identifiable and must be handled as sensitive derivative content; do not publish it without all required consent.
- The source image is not included in the output; only its stable SHA-256 file fingerprint and pixel dimensions are retained. The fingerprint can match the same file and is not an anonymity guarantee.
"""


def load_design_source(path: Path) -> Image.Image:
    """Apply restore's input limits, then composite source alpha onto white."""

    validated_size = load_source(path).size
    try:
        with Image.open(path) as opened:
            source = ImageOps.exif_transpose(opened).convert("RGBA")
    except Exception as exc:
        raise ValueError(f"cannot decode input image: {path}") from exc
    if source.size != validated_size:
        raise ValueError("EXIF-corrected design source dimensions are inconsistent")
    white = Image.new("RGBA", source.size, (255, 255, 255, 255))
    white.alpha_composite(source)
    return white.convert("RGB")


def prepare_square(image: Image.Image, size: int) -> tuple[np.ndarray, dict[str, Any]]:
    side = min(image.width, image.height)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    crop = image.crop((left, top, left + side, top + side))
    reduced = crop.resize((size, size), Image.Resampling.LANCZOS)
    return np.asarray(reduced, dtype=np.uint8), {
        "crop_mode": "center-square",
        "crop_box_px": [left, top, left + side, top + side],
        "placement_box_cells": [0, 0, size, size],
        "downsample": "lanczos",
    }


def contain_geometry(width: int, height: int, size: int) -> dict[str, int]:
    """Return bounded target geometry without allocating a source-sized square."""

    if width < 1 or height < 1 or size < 1:
        raise ValueError("contain dimensions must be positive")
    scale = min(size / width, size / height)
    target_width = min(size, max(1, int(round(width * scale))))
    target_height = min(size, max(1, int(round(height * scale))))
    return {
        "canvas_width": size,
        "canvas_height": size,
        "target_width": target_width,
        "target_height": target_height,
        "placement_left": (size - target_width) // 2,
        "placement_top": (size - target_height) // 2,
    }


def prepare_contain(image: Image.Image, size: int) -> tuple[np.ndarray, dict[str, Any]]:
    geometry = contain_geometry(image.width, image.height, size)
    target_width = geometry["target_width"]
    target_height = geometry["target_height"]
    left = geometry["placement_left"]
    top = geometry["placement_top"]
    canvas = Image.new("RGB", (size, size), (255, 255, 255))
    canvas.paste(
        image.resize((target_width, target_height), Image.Resampling.LANCZOS),
        (left, top),
    )
    return np.asarray(canvas, dtype=np.uint8), {
        "crop_mode": "contain-square-pad",
        "crop_box_px": [0, 0, image.width, image.height],
        "placement_box_cells": [
            left,
            top,
            left + target_width,
            top + target_height,
        ],
        "intermediate_canvas_cells": [size, size],
        "downsample": "lanczos",
    }


def boolean_mask_bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """Return a half-open bbox using axis reductions, not full pixel indices."""

    if mask.ndim != 2:
        raise ValueError("foreground mask must be two-dimensional")
    active_rows = np.flatnonzero(mask.any(axis=1))
    active_cols = np.flatnonzero(mask.any(axis=0))
    if active_rows.size == 0 or active_cols.size == 0:
        return None
    return (
        int(active_cols[0]),
        int(active_rows[0]),
        int(active_cols[-1]) + 1,
        int(active_rows[-1]) + 1,
    )


def prepare_content_contain(
    image: Image.Image, size: int, padding_cells: int
) -> tuple[np.ndarray, dict[str, Any]]:
    source = np.asarray(image, dtype=np.uint8)
    foreground = source.min(axis=2) < 248
    foreground_bbox = boolean_mask_bbox(foreground)
    if foreground_bbox is None:
        raise ValueError("content-aware fit found no non-white subject")
    raw_left, raw_top, raw_right, raw_bottom = foreground_bbox
    source_margin = 2
    left = max(0, raw_left - source_margin)
    top = max(0, raw_top - source_margin)
    right = min(image.width, raw_right + source_margin)
    bottom = min(image.height, raw_bottom + source_margin)
    crop = image.crop((left, top, right, bottom))

    available = size - 2 * padding_cells
    scale = min(available / crop.width, available / crop.height)
    target_width = min(available, max(1, int(round(crop.width * scale))))
    target_height = min(available, max(1, int(round(crop.height * scale))))
    placement_left = (size - target_width) // 2
    placement_top = (size - target_height) // 2
    canvas = Image.new("RGB", (size, size), (255, 255, 255))
    canvas.paste(
        crop.resize((target_width, target_height), Image.Resampling.LANCZOS),
        (placement_left, placement_top),
    )
    return np.asarray(canvas, dtype=np.uint8), {
        "crop_mode": "content-aware-contain",
        "crop_box_px": [left, top, right, bottom],
        "foreground_bbox_px": [raw_left, raw_top, raw_right, raw_bottom],
        "placement_box_cells": [
            placement_left,
            placement_top,
            placement_left + target_width,
            placement_top + target_height,
        ],
        "requested_padding_cells": padding_cells,
        "source_margin_px": source_margin,
        "downsample": "lanczos",
    }


def image_luminance_and_chroma(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    values = rgb.astype(np.float64)
    luminance = 0.2126 * values[..., 0] + 0.7152 * values[..., 1] + 0.0722 * values[..., 2]
    chroma = values.max(axis=2) - values.min(axis=2)
    return luminance, chroma


def border_connected(mask: np.ndarray) -> np.ndarray:
    rows, columns = mask.shape
    result = np.zeros_like(mask, dtype=bool)
    stack: list[tuple[int, int]] = []
    for col in range(columns):
        stack.extend(((0, col), (rows - 1, col)))
    for row in range(rows):
        stack.extend(((row, 0), (row, columns - 1)))
    while stack:
        row, col = stack.pop()
        if result[row, col] or not mask[row, col]:
            continue
        result[row, col] = True
        for next_row, next_col in (
            (row - 1, col),
            (row + 1, col),
            (row, col - 1),
            (row, col + 1),
        ):
            if 0 <= next_row < rows and 0 <= next_col < columns:
                stack.append((next_row, next_col))
    return result


def weighted_kmeans_lab(
    rgb: np.ndarray,
    clusters: int,
    seed: int,
    protected_ink: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    points_rgb = rgb.reshape(-1, 3).astype(np.float64)
    points_lab = srgb_to_lab(points_rgb)
    features = points_lab * np.asarray((1.0, 1.12, 1.12), dtype=np.float64)
    saturation = (points_rgb.max(axis=1) - points_rgb.min(axis=1)) / 255.0
    weights = 1.0 + 1.6 * saturation + 1.8 * protected_ink.reshape(-1)
    rng = np.random.default_rng(seed)
    centers = np.empty((clusters, 3), dtype=np.float64)
    mean = np.average(features, axis=0, weights=weights)
    centers[0] = features[int(np.argmax(weights * ((features - mean) ** 2).sum(axis=1)))]
    nearest = ((features - centers[0]) ** 2).sum(axis=1)
    for index in range(1, clusters):
        probabilities = weights * nearest
        total = float(probabilities.sum())
        chosen = (
            int(rng.integers(0, len(features)))
            if total <= 0
            else int(rng.choice(len(features), p=probabilities / total))
        )
        centers[index] = features[chosen]
        nearest = np.minimum(nearest, ((features - centers[index]) ** 2).sum(axis=1))

    labels = np.full(len(features), -1, dtype=np.int16)
    for _ in range(50):
        distances = ((features[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        next_labels = distances.argmin(axis=1).astype(np.int16)
        stable = np.array_equal(labels, next_labels)
        labels = next_labels
        for index in range(clusters):
            selected = labels == index
            if selected.any():
                centers[index] = np.average(features[selected], axis=0, weights=weights[selected])
        if stable:
            break

    label_grid = labels.reshape(rgb.shape[:2])
    gradient_x = np.abs(np.diff(points_lab.reshape(*rgb.shape[:2], 3), axis=1)).sum(axis=2)
    gradient_y = np.abs(np.diff(points_lab.reshape(*rgb.shape[:2], 3), axis=0)).sum(axis=2)
    gradient = np.zeros(rgb.shape[:2], dtype=np.float64)
    gradient[:, 1:] += gradient_x
    gradient[1:, :] += gradient_y
    cleaned = label_grid.copy()
    for row in range(1, rgb.shape[0] - 1):
        for col in range(1, rgb.shape[1] - 1):
            if protected_ink[row, col] or gradient[row, col] >= 7.5:
                continue
            neighborhood = label_grid[row - 1 : row + 2, col - 1 : col + 2].reshape(-1)
            counts = np.bincount(neighborhood, minlength=clusters)
            majority = int(counts.argmax())
            if counts[majority] >= 7:
                cleaned[row, col] = majority

    active_clusters = [int(value) for value in np.unique(cleaned)]
    compact = np.empty_like(cleaned, dtype=np.int16)
    centroid_rgb = np.empty((len(active_clusters), 3), dtype=np.float64)
    for compact_index, source_index in enumerate(active_clusters):
        selected_grid = cleaned == source_index
        compact[selected_grid] = compact_index
        selected = selected_grid.reshape(-1)
        centroid_rgb[compact_index] = np.average(
            points_rgb[selected], axis=0, weights=weights[selected]
        )
    return compact, centroid_rgb


def map_clusters_to_mard(
    labels: np.ndarray,
    centroid_rgb: np.ndarray,
    palette: Sequence[PaletteEntry],
    protected_ink: np.ndarray,
) -> tuple[np.ndarray, list[dict[str, Any]], int]:
    physical_indices = np.asarray(
        [index for index, entry in enumerate(palette) if entry.code is not None],
        dtype=np.int16,
    )
    physical_rgb = np.asarray([palette[int(index)].rgb for index in physical_indices], dtype=np.float64)
    physical_lab = srgb_to_lab(physical_rgb)
    centroid_lab = srgb_to_lab(centroid_rgb)
    cluster_to_palette = np.empty(len(centroid_rgb), dtype=np.int16)
    mappings: list[dict[str, Any]] = []
    for cluster in range(len(centroid_rgb)):
        distances = delta_e_2000(centroid_lab[cluster][None, :], physical_lab)
        ranking = np.argsort(distances)
        selected = int(physical_indices[int(ranking[0])])
        runner = int(physical_indices[int(ranking[1])])
        cluster_to_palette[cluster] = selected
        mappings.append(
            {
                "cluster": cluster,
                "cell_count": int((labels == cluster).sum()),
                "source_rgb": [int(round(value)) for value in centroid_rgb[cluster]],
                "selected_code": palette[selected].code,
                "selected_rgb": list(palette[selected].rgb),
                "delta_e_2000": round(float(distances[int(ranking[0])]), 4),
                "runner_up_code": palette[runner].code,
                "runner_up_delta_e_2000": round(float(distances[int(ranking[1])]), 4),
            }
        )
    mapped = cluster_to_palette[labels]
    black_index = next(index for index, entry in enumerate(palette) if entry.code == "H7")
    forced = protected_ink & (mapped != black_index)
    mapped[protected_ink] = black_index
    return mapped, mappings, int(forced.sum())


def choose_empty_background(
    rgb: np.ndarray, mode: str
) -> tuple[np.ndarray, dict[str, Any]]:
    luminance, chroma = image_luminance_and_chroma(rgb)
    neutral = (luminance >= 244.0) & (chroma <= 12.0)
    connected = border_connected(neutral)
    border = np.concatenate((neutral[0], neutral[-1], neutral[:, 0], neutral[:, -1]))
    border_ratio = float(border.mean())
    connected_ratio = float(connected.mean())
    apply_empty = mode == "empty-white" or (
        mode == "auto" and border_ratio >= 0.70 and connected_ratio >= 0.08
    )
    if mode == "empty-white" and (border_ratio < 0.35 or connected_ratio < 0.02):
        raise ValueError(
            "--background empty-white needs a border-connected light neutral background"
        )
    if mode == "bead":
        apply_empty = False
    return (connected if apply_empty else np.zeros_like(connected)), {
        "requested_mode": mode,
        "applied_mode": "synthetic-empty" if apply_empty else "physical-bead-board",
        "method": "four-connected-light-neutral-border-flood",
        "border_neutral_ratio": round(border_ratio, 6),
        "empty_cell_ratio": round(connected_ratio if apply_empty else 0.0, 6),
        "symbol": "." if apply_empty else None,
        "code": None,
        "counts_as_bead": not apply_empty,
    }


def content_bbox(labels: np.ndarray, background_index: int | None) -> dict[str, int]:
    foreground = np.ones(labels.shape, dtype=bool)
    if background_index is not None:
        foreground = labels != background_index
    if not foreground.any():
        raise ValueError("design contains no physical bead cells")
    rows, cols = np.where(foreground)
    return {
        "left": int(cols.min()),
        "top": int(rows.min()),
        "right_exclusive": int(cols.max()) + 1,
        "bottom_exclusive": int(rows.max()) + 1,
    }


def render_design(
    labels: np.ndarray,
    palette: Sequence[PaletteEntry],
    cell_px: int,
    *,
    grid: bool,
    transparent: bool,
) -> Image.Image:
    return render_matrix(
        labels,
        palette,
        cell_px,
        grid=grid,
        transparent=transparent,
    )


def write_matrix_csv(
    path: Path, labels: np.ndarray, palette: Sequence[PaletteEntry]
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["row\\col", *range(labels.shape[1])])
        for row in range(labels.shape[0]):
            writer.writerow(
                [row, *(palette[int(value)].code or palette[int(value)].symbol for value in labels[row])]
            )


def design_command(args: argparse.Namespace) -> int:
    source_path = Path(args.input).expanduser().resolve()
    image = load_design_source(source_path)
    board_size = int(args.board_size)
    if args.fit_mode == "center-square":
        reduced_rgb, geometry = prepare_square(image, board_size)
    elif args.fit_mode == "contain":
        reduced_rgb, geometry = prepare_contain(image, board_size)
    else:
        reduced_rgb, geometry = prepare_content_contain(
            image, board_size, args.content_padding_cells
        )

    luminance, chroma = image_luminance_and_chroma(reduced_rgb)
    ink_score = np.clip((82.0 - luminance) / 82.0, 0.0, 1.0)
    protected_ink = (
        (chroma <= 55.0)
        & (luminance <= 75.0)
        & (ink_score >= args.ink_threshold)
    ) | (luminance <= 28.0)
    cluster_labels, centroids = weighted_kmeans_lab(
        reduced_rgb, args.clusters, args.seed, protected_ink
    )
    background_rgb = border_background(reduced_rgb)
    bundle = load_palette_bundle(
        "mard-221-compatible",
        reduced_rgb,
        background_rgb,
        args.clusters,
        args.seed,
    )
    palette = bundle.entries
    labels, mappings, forced_black = map_clusters_to_mard(
        cluster_labels, centroids, palette, protected_ink
    )
    empty_mask, background_diagnostics = choose_empty_background(
        reduced_rgb, args.background
    )
    background_index = 0 if empty_mask.any() else None
    if background_index is not None:
        labels[empty_mask] = background_index

    bbox = content_bbox(labels, background_index)
    counts = {
        (entry.code or "background"): int((labels == index).sum())
        for index, entry in enumerate(palette)
        if bool((labels == index).any())
    }
    background_count = counts.get("background", 0) if background_index is not None else 0
    bead_count = int(labels.size - background_count)
    used_physical_codes = sorted(code for code in counts if code != "background")
    profile = dict(bundle.profile or {})
    profile["background_strategy"] = "design-border-neutral-or-full-board"

    artifacts = [
        "DESIGN_RIGHTS_NOTICE.md",
        "THIRD_PARTY_NOTICES.md",
        "design.csv",
        "design_grid.png",
        "design_preview.png",
        "design_transparent.png",
        "palette_counts.csv",
        "pattern.json",
        "summary.json",
    ]
    target = Path(args.out)
    staging = begin_staging(target, args.overwrite, [source_path])
    try:
        render_design(
            labels, palette, args.preview_cell_px, grid=False, transparent=False
        ).save(staging / "design_preview.png")
        render_design(
            labels, palette, args.grid_cell_px, grid=True, transparent=False
        ).save(staging / "design_grid.png")
        render_design(
            labels, palette, args.preview_cell_px, grid=False, transparent=True
        ).save(staging / "design_transparent.png")
        write_matrix_csv(staging / "design.csv", labels, palette)
        with (staging / "palette_counts.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.writer(handle)
            writer.writerow(["code", "rgb_hex", "count", "synthetic"])
            for key in sorted(counts, key=lambda value: (-counts[value], value)):
                entry = next(
                    item
                    for item in palette
                    if (item.code or "background") == key
                )
                writer.writerow(
                    [
                        key,
                        "#" + "".join(f"{channel:02X}" for channel in entry.rgb),
                        counts[key],
                        "true" if entry.synthetic else "false",
                    ]
                )
        notice_source = Path(__file__).resolve().parent.parent / "THIRD_PARTY_NOTICES.md"
        if not notice_source.is_file():
            raise ValueError("installed skill is missing THIRD_PARTY_NOTICES.md")
        shutil.copyfile(notice_source, staging / "THIRD_PARTY_NOTICES.md")
        (staging / "DESIGN_RIGHTS_NOTICE.md").write_text(
            design_rights_notice(), encoding="utf-8"
        )

        cells = []
        for row in range(board_size):
            for col in range(board_size):
                entry = palette[int(labels[row, col])]
                cells.append(
                    {
                        "row": row,
                        "col": col,
                        "code": entry.code,
                        "symbol": entry.code or entry.symbol,
                        "synthetic": entry.synthetic,
                    }
                )
        pattern = {
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "status": "review",
            "kind": "new-bead-pattern-design",
            "not_restoration": True,
            "source": {
                "sha256": source_sha256(source_path),
                "width_px": image.width,
                "height_px": image.height,
            },
            "canvas": {
                "columns": board_size,
                "rows": board_size,
                "board_standard": "wenzhou",
                "board_size": board_size,
                "board_cell_count": board_size * board_size,
                "full_square_design": background_index is None,
                "empty_background_cells": background_count,
            },
            "content_bbox": bbox,
            "background": {
                **background_diagnostics,
                "synthetic": background_index is not None,
                "symbol": "." if background_index is not None else None,
                "code": None,
                "cell_count": background_count,
            },
            "design_method": {
                **geometry,
                "logical_color_clusters": args.clusters,
                "effective_color_clusters": len(mappings),
                "seed": args.seed,
                "dithering": False,
                "gradient_policy": "fixed-k Lab clustering plus conservative local majority cleanup",
                "catalog_matching": "cluster-centroid Lab to bundled MARD 221 using CIEDE2000",
                "black_line_protection": {
                    "method": "dark neutral target-cell protection",
                    "coverage_threshold": args.ink_threshold,
                    "protected_cells": int(protected_ink.sum()),
                    "forced_code": "H7",
                    "forced_black_cells": forced_black,
                },
                "cluster_mappings": mappings,
            },
            "palette_profile": profile,
            "rights": design_rights_payload(),
            "used_color_count": len(used_physical_codes),
            "counts": counts,
            "bead_count": bead_count,
            "review_notes": [
                "This is a new raster-to-bead design, not a restoration of an existing source grid.",
                "Composition, pixel placement, and color reduction are design decisions and require visual review.",
                "MARD RGB values are provisional screen references; confirm codes against the target merchant's physical card.",
            ],
            "cells": cells,
            "artifacts": artifacts,
        }
        (staging / "pattern.json").write_text(
            json.dumps(pattern, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        summary = {
            key: pattern[key]
            for key in (
                "schema_version",
                "algorithm_version",
                "status",
                "kind",
                "not_restoration",
                "canvas",
                "content_bbox",
                "background",
                "palette_profile",
                "rights",
                "used_color_count",
                "counts",
                "bead_count",
                "review_notes",
                "artifacts",
            )
        }
        (staging / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        commit_staging(staging, target, args.overwrite, [source_path])
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a new 52x52 or 78x78 MARD-compatible fuse-bead design from an "
            "ordinary photo or illustration; this is not grid restoration."
        )
    )
    parser.add_argument("--version", action="version", version=ALGORITHM_VERSION)
    parser.add_argument("input", help="ordinary source image")
    parser.add_argument("--out", required=True, help="new output directory")
    parser.add_argument(
        "--board-size",
        type=parse_board_size,
        default=78,
        metavar="52x52|78x78",
        help="Wenzhou-style square board (default: 78x78)",
    )
    parser.add_argument(
        "--fit-mode",
        choices=("center-square", "contain", "content-contain"),
        default="content-contain",
    )
    parser.add_argument("--content-padding-cells", type=int, default=2)
    parser.add_argument("--clusters", type=int, default=12)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--ink-threshold", type=float, default=0.055)
    parser.add_argument(
        "--background",
        choices=("auto", "empty-white", "bead"),
        default="auto",
        help="auto-detect a white empty border, require one, or fill every board cell",
    )
    parser.add_argument("--preview-cell-px", type=int, default=10)
    parser.add_argument(
        "--grid-cell-px",
        type=int,
        default=22,
        help="code-labeled grid cell size (18..64; dynamic render limit applies)",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 4 <= args.clusters <= 16:
        parser.error("--clusters must be between 4 and 16")
    if not 1 <= args.content_padding_cells <= 16:
        parser.error("--content-padding-cells must be between 1 and 16")
    if not 0.01 <= args.ink_threshold <= 0.5:
        parser.error("--ink-threshold must be between 0.01 and 0.5")
    if not 4 <= args.preview_cell_px <= 64:
        parser.error("--preview-cell-px must be between 4 and 64")
    if not 18 <= args.grid_cell_px <= 64:
        parser.error("--grid-cell-px must be between 18 and 64")
    for option, cell_px in (
        ("--preview-cell-px", args.preview_cell_px),
        ("--grid-cell-px", args.grid_cell_px),
    ):
        rendered_side = int(args.board_size) * cell_px
        if rendered_side * rendered_side > MAX_RENDER_TOTAL_PIXELS:
            parser.error(
                f"{option} would exceed {MAX_RENDER_TOTAL_PIXELS:,} total pixels "
                f"for a {args.board_size}x{args.board_size} board"
            )
    try:
        return design_command(args)
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
