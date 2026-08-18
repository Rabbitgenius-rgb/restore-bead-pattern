# Restore and Design Pattern Contracts

Use this reference when choosing between native-grid restoration and a new raster-derived bead design, invoking either bundled script, reading its output, requesting a Wenzhou mold, supplying a palette, or applying confirmed restoration edits.

## Contents

- Restore CLI and board modes
- Generic 52×52/78×78 design CLI and output schema
- Coordinate spaces and output artifacts
- Explicit integer design scaling
- Board metadata
- Custom palette JSON
- Built-in MARD 221 compatible profile
- Review, revision, topology, and status gates

Use macOS with Python 3.10–3.12, NumPy, and Pillow as the primary validated local environment. The restore CLI also receives Ubuntu CI coverage; Windows remains unverified.

## Restore CLI

```text
restore_pattern.py restore INPUT --out DIR
  [--grid auto|COLSxROWS]
  [--cell-size PIXELS]
  [--origin X,Y]
  [--pitch-min PIXELS] [--pitch-max PIXELS]
  [--padding 0..10]
  [--palette auto|warm-mascot|mard-221-compatible|PALETTE_JSON]
  [--colors 2..16]
  [--board-size none|auto|52x52|78x78]
  [--uncertain-threshold 0..1]
  [--seed INT]
  [--render-cell-px 4..64]
  [--topology auto|off]
  [--strict] [--overwrite]
```

- Require `INPUT` to decode as an image at least 16 × 16 pixels. Require at least 64 × 64 pixels for fully automatic geometry estimation; smaller inputs need supported explicit geometry.
- Require `--out` to be new or empty unless `--overwrite` is explicit. Every committed output carries a hidden tool-owned safety marker. With `--overwrite`, replace only a marked output directory; reject symbolic links, broad system/user/workspace paths, the skill/repository root, and any ancestor of an input image, pattern, edits file, or custom palette.
- Use fully automatic native geometry by default. Treat `--grid auto` as equivalent.
- Interpret `--grid 40x52` and `--grid 40×52` as 40 native columns × 52 native rows. Do not use `--grid` to specify a mold.
- With an exact native grid but no cell size, derive square pitch from `image_height / rows` and center the matrix horizontally and vertically.
- Interpret `--cell-size` as native square pitch in EXIF-corrected source pixels.
- Interpret `--origin X,Y` as the top-left outer native grid boundary, not a cell center. Require either an exact grid or a cell size with it.
- Add one exterior native cell during automatic geometry by default so 4-neighbor background topology can be evaluated. Change this with `--padding`.
- Use `--pitch-min` and `--pitch-max` only to exclude a concrete false harmonic.
- Let `--palette auto` infer a provisional palette with `--colors 6` by default. Force at least `review` until later evidence validates that palette.
- Treat `warm-mascot` as a packaged six-color example palette, not a universal default.
- Let `--palette mard-221-compatible` (alias `mard-221`) use all 221 bundled bead codes plus one runtime-only photographic background entry. Use `--colors` only for the compact structural segmentation preceding catalog matching; never truncate the catalog.
- Run `--topology auto` only when the active structural palette has unique `background`, `fill`, and `outline` roles and a stable enclosed light region exists. For MARD 221, use the provisional structural palette for topology; never assign arbitrary fill/outline roles to catalog codes.
- Let `--strict` return nonzero unless the overall pattern status is `pass`.

Write progress and errors to stderr and exactly one compact JSON summary line to stdout on success.

## Generic Design CLI

Use the separate design script only when an ordinary photo or illustration must become a new bead design. It does not detect or restore a native grid.

```text
design_bead_pattern.py INPUT --out DIR
  [--board-size 52x52|78x78]
  [--fit-mode center-square|contain|content-contain]
  [--content-padding-cells 1..16]
  [--clusters 4..16]
  [--seed INT]
  [--ink-threshold 0.01..0.5]
  [--background auto|empty-white|bead]
  [--preview-cell-px 4..64]
  [--grid-cell-px 18..64]
  [--overwrite]
```

- Keep the public design path generic. Do not add source-specific landmarks or semantic feature rules.
- Treat the documented cell-pixel ranges as parser bounds, not a promise that every board/scale combination will render. The shared renderer also enforces a 16,000-pixel per-axis limit and a 16,000,000-total-pixel limit, and must reject an oversized combination before committing output.
- Require an explicit supported geometry internally; expose `--board-size 52x52|78x78` and default to 78×78. Design mode does not infer a mold from the source.
- Use `content-contain` by default to detect a non-white content box, preserve its aspect ratio, and reserve `--content-padding-cells` around it. Use `center-square` for a centered square crop or `contain` to retain the complete source with white square padding.
- Cluster the reduced raster with a fixed seed in Lab space, protect supported neutral-dark line coverage, and map cluster centroids against the complete 221-entry MARD-compatible catalog with CIEDE2000. Do not dither or claim semantic understanding.
- With `--background auto`, use a synthetic `.` background only when a neutral-white region is sufficiently border connected. Use `empty-white` to require that evidence or `bead` to force every position to a physical MARD-compatible code. Never treat `.` as a white bead.
- Require `status: review` for every run. A plausible preview does not validate identity, anatomy, text, small features, color accuracy, or copyright status.
- Prefer a new output directory. Treat `--overwrite` as the same marker-guarded replacement contract used by the restore script; never weaken its path, symlink, ancestor, or input-file protections.
- Write errors to stderr, return `2` on invalid input or arguments, and write exactly one compact JSON summary line to stdout after a successful atomic output commit.
- Do not store the source path or copy the source image into the output. Store only its SHA-256, width, and height.
- Treat SHA-256 as a stable file fingerprint that can identify a known matching file, not as anonymization. A rendered design may preserve an identifiable person, likeness, text, or other sensitive visual information. Record that privacy or portrait consent was not verified and require the caller to avoid publication without the necessary consent.
- Require the user to hold the rights needed to create and use the derivative. The tool grants no permission to reproduce, sell, publish, or redistribute copyrighted or trademarked source material.

## Board Modes

- Use `--board-size none` by default. Produce no mold canvas or board artifacts.
- Use `--board-size auto` only for a user-requested 拼豆 or Wenzhou-mold result. First recover the native grid, then evaluate the supported 52×52 and 78×78 boards.
- Use `--board-size 52x52` or `--board-size 78x78` for an explicit user choice. Set `selection_status` to `explicit`; do not treat it as visual detection.
- In `auto`, set `selection_status` to `detected` only when native pitch projected across a supported mold closely matches the image short side and clearly beats the other fitting size. Use `detection_tolerance 0.08` and `ambiguity_margin 0.08` unless the implementation version declares different thresholds.
- If direct evidence is insufficient, recommend the smallest supported board that contains the cropped native content, set `selection_status` to `review`, and force the overall result to at least `review` until confirmation.
- Do not use decreasing within-cell residual as proof of a larger board. Finer grids often lower this residual even when they split one native cell into several cells.
- If content does not fit 52×52, try 78×78 without changing the native grid. If it exceeds 78 in either dimension, report a board-capacity failure and preserve the native restoration and diagnostics.

Board selection occurs after native restoration. It must never change native pitch, origin, dimensions, labels, confidence, topology, or candidate ranking.

## Explicit Integer Design Scaling

Use a separate operation only after the source pattern has been restored and the user explicitly requests a larger subject:

```text
restore_pattern.py scale PATTERN_JSON --factor 2 --board-size 78x78 --out DIR
  [--render-cell-px 4..64] [--overwrite]
```

- Require `--factor` to be an integer from 2 through 8.
- Require an explicit `--board-size 52x52` or `--board-size 78x78`; do not infer a board during a design derivation.
- Validate that the stored `content_bbox` agrees with the manifest cells, crop exactly to it, and repeat each logical label, raw label, alternative, confidence, and agreement into a `factor × factor` block using integer nearest-neighbor cell replication.
- Do not reopen or resample the source photograph. Scaling adds size, not detail, and must not create new palette entries or interpret fiber, melt texture, shadows, or compression noise as cells.
- Reject the operation before writing output when scaled width or height exceeds the selected board. Do not crop, split, or silently choose another factor.
- Emit the normal pattern PNG/CSV/JSON artifacts and the requested board artifacts. Source overlays and grid-candidate artifacts do not apply to this post-recovery operation.

For a derived output, require top-level `resampled: true` and record:

```json
{
  "derivation": {
    "kind": "integer-nearest-neighbor-content-scale",
    "resampled": true,
    "authorized_by": "explicit-user-request",
    "factor": 2,
    "cumulative_factor": 2,
    "source_pattern_sha256": "...",
    "source_content_bbox": {
      "left": 1,
      "top": 1,
      "right_exclusive": 32,
      "bottom_exclusive": 39
    },
    "source_content_size": {"columns": 31, "rows": 38},
    "output_content_size": {"columns": 62, "rows": 76},
    "label_mapping": "exact-cell-replication",
    "detail_policy": "no-new-detail-from-source-photo"
  }
}
```

Set the derived logical grid method to `integer-nearest-neighbor-content-scale` and its coordinate space to `derived-content-local`. Preserve the source image hash and original source dimensions as ancestry, not as a claim that the new grid was independently observed in the photograph.

Keep `board.resampled: false`: the board copies the already-derived logical cells one-to-one. Add `board.design_derivation` with `kind`, `factor`, `source_content_size`, and `output_content_size` to link the board to the deliberate top-level derivation. Preserve both objects through `render` and `revise`.

## Coordinate Spaces

Keep these spaces separate:

1. **Native canvas**: the full sampled logical matrix, including exterior analysis padding. Native `row` and `col` indices are zero-based.
2. **Native content**: `content_bbox` cropped from the native canvas. Store the box as `left`, `top`, `right_exclusive`, and `bottom_exclusive` in native cell coordinates.
3. **Board canvas**: exactly 52×52 or 78×78 cells. Copy native content cells into it one-for-one and fill only the surrounding cells with background.
4. **Derived design canvas, when explicitly authorized**: an integer repetition of native content cells produced by the `scale` operation. This is not a second observation of the source photograph.
5. **New design canvas**: exactly 52×52 or 78×78 zero-based cells created directly from an ordinary raster by `design_bead_pattern.py`. It is neither a native canvas nor a board placement of recovered cells.

Never copy native exterior padding onto the board. During normal restoration and board placement, never interpolate, resample, split, merge, stretch, or synthesize content. The explicit `scale` operation is the sole exception and performs exact integer cell repetition only. Require `board.resampled` to remain `false` in both cases because physical-board placement is always one-to-one.

Store board placement offsets as zero-based board coordinates. Map a native content cell `(row, col)` to board cell:

```text
board_row = board.placement.row_offset + row - content_bbox.top
board_col = board.placement.col_offset + col - content_bbox.left
```

## Restore Outputs

| File | Meaning |
| --- | --- |
| `summary.json` | Compact status, native grid, board summary when present, counts, quality, warnings, and artifact names; matches stdout. |
| `pattern.json` | Source of truth for hash, native grid, palette, cells, confidence, provenance, status, and optional board metadata. |
| `canvas.csv` | Full native sampled matrix of palette symbols, including exterior background. |
| `matrix.csv` | Native matrix cropped to `content_bbox`. |
| `palette.csv` | Palette names, symbols, optional purchase codes, RGB, roles, and native full-canvas counts. |
| `pattern_preview.png` | Cropped native pattern without grid lines. |
| `pattern_grid.png` | Cropped native pattern with cell lines and symbols. |
| `pattern_review.png` | Cropped native grid with low-confidence non-background cells outlined in magenta. |
| `pattern_transparent.png` | Cropped native RGBA pattern with background alpha zero. |
| `canvas_grid.png` | Full native sampled matrix with grid lines. |
| `source_grid_overlay.png` | Selected native lattice over the EXIF-corrected source. |
| `candidates.png` | Contact sheet of leading materially distinct native lattice candidates. |
| `candidates.json` | Machine-readable ranked native lattice candidates. |
| `review.csv` | Proposed low-confidence native cells and blank revision fields. |
| `board.csv` | Full 52×52 or 78×78 board matrix of palette symbols. |
| `board_preview.png` | Full board canvas without grid lines. |
| `board_grid.png` | Full board canvas with cell lines and symbols. |
| `board_transparent.png` | Full board RGBA canvas with background alpha zero. |
| `board_source_overlay.png` | Selected board extent and one-to-one native content placement projected over the source. |

Produce `board.*` metadata and all five board artifacts only when board selection succeeds. An overlay visualizes the selected placement; it is not proof that a physical mold was visible in the source.

Do not store the input path in `pattern.json` or `summary.json`. Store only source SHA-256 and pixel dimensions. Never package the source image, screenshots, private paths, or run outputs inside the skill.

Every `pattern.json` native cell contains zero-based coordinates, final label and symbol, sampled raw label, next-best alternative, confidence, perturbation agreement, and a reason when topology or review handling affected it. Catalog-backed cells also contain `code`, `raw_code`, and `alternative_code`; the synthetic background uses `code: null`. Define `uncertain_cells` as exactly the non-background native cells whose final confidence is below `uncertain_threshold`.

## Design Outputs

Generic design mode emits exactly these artifacts:

| File | Meaning |
| --- | --- |
| `summary.json` | Compact design summary; matches the single JSON object written to stdout. |
| `pattern.json` | Full design manifest with source hash and dimensions, selected-board canvas metadata, method, provisional palette profile, cluster mappings, counts, every cell, review notes, rights metadata, and artifact names. |
| `design.csv` | Selected 52×52 or 78×78 zero-based matrix of MARD-compatible codes with a `row\col` header. |
| `palette_counts.csv` | Used code or synthetic background, screen-reference HEX value, count, and `synthetic` flag; counts sum to the selected board area. |
| `design_preview.png` | Selected-board design rendered without grid lines. |
| `design_grid.png` | Selected-board design rendered with cell lines and code labels. |
| `design_transparent.png` | RGBA preview with synthetic empty cells transparent; identical coverage to the opaque preview when every position is a bead. |
| `THIRD_PARTY_NOTICES.md` | Copied attribution required for the bundled MARD-compatible data. |
| `DESIGN_RIGHTS_NOTICE.md` | Bilingual notice that the tool did not verify source rights and grants no commercial-copying or public-redistribution rights. |

Require at least these `pattern.json` invariants:

```json
{
  "schema_version": "design-1.0",
  "algorithm_version": "design-1.0.0",
  "status": "review",
  "kind": "new-bead-pattern-design",
  "not_restoration": true,
  "source": {"sha256": "...", "width_px": 1200, "height_px": 1600},
  "canvas": {
    "columns": 78,
    "rows": 78,
    "board_standard": "wenzhou",
    "board_size": 78,
    "full_square_design": false,
    "board_cell_count": 6084,
    "empty_background_cells": 1200
  },
  "palette_profile": {
    "id": "mard-221-compatible",
    "code_system": "mard-221",
    "bead_color_count": 221,
    "provisional": true
  },
  "bead_count": 4884
}
```

- Treat the numeric background and bead counts above as an illustrative 78×78 example. Require `board_cell_count == bead_count + empty_background_cells == board_size²`; set `full_square_design: true` exactly when `empty_background_cells` is zero.
- Require `cells` to contain exactly `board_size²` entries with zero-based `row`, `col`, `symbol`, and `synthetic`; physical cells have a non-null MARD-compatible `code`, while synthetic empty cells use `code: null` and symbol `.`. Require `counts` and `palette_counts.csv` to sum to the same area.
- Require `design_method` to disclose crop/contain geometry, downsampling, cluster count, seed, no-dithering policy, dark-line protection, and catalog matching.
- Require `review_notes` to state that this is a new design rather than a restoration, that semantic details need inspection, and that MARD screen RGB references require physical-card confirmation.
- Require identical structured `rights` metadata in `pattern.json` and `summary.json`, and register `DESIGN_RIGHTS_NOTICE.md` in `artifacts`. The notice must state that source rights, privacy, and portrait consent were not verified; the source image is not included; the SHA-256 is a stable file fingerprint; the design may remain identifiable; and the tool grants no commercial reproduction or public redistribution rights.
- Do not pass a design `pattern.json` to restoration `revise`, `render`, or `scale`; those commands consume the restoration schema and provenance model.
- Do not expect source overlays, native-grid candidates, confidence maps, or restoration board metadata from design mode. Read `background.applied_mode`, `content_bbox`, and `empty_background_cells` for design-specific empty-hole semantics.

## Board Metadata

When a board exists, include this object in `pattern.json` and its compact counterpart in `summary.json`:

```json
{
  "board": {
    "standard": "wenzhou",
    "board_size": 52,
    "columns": 52,
    "rows": 52,
    "mode": "auto",
    "selection_status": "detected",
    "selection_confidence": 0.999786,
    "reason": "native pitch matches the image short side at this mold size",
    "native_pitch_px": 27.657955,
    "image_short_side_px": 1440.0,
    "content_size": {"columns": 35, "rows": 43},
    "counts": {
      "background": 1646,
      "fill": 609,
      "outline": 313,
      "color-a": 63,
      "color-b": 44,
      "color-c": 29
    },
    "bead_count": 1058,
    "placement": {
      "row_offset": 4,
      "col_offset": 9,
      "source_bbox": {"left": 1, "top": 1, "right_exclusive": 36, "bottom_exclusive": 44},
      "native_to_board_shift": {"columns": 8, "rows": 3},
      "margins": {"left": 9, "top": 4, "right": 8, "bottom": 5},
      "board_origin_x_px": 234.565,
      "board_origin_y_px": -4.278,
      "centered_target_origin_x_px": 231.89317,
      "centered_target_origin_y_px": 0.89317,
      "centering_error_x_px": 2.67183,
      "centering_error_y_px": -5.17117
    },
    "resampled": false,
    "candidates": [
      {
        "board_size": 52,
        "columns": 52,
        "rows": 52,
        "capacity_fit": true,
        "projected_side_px": 1438.21366,
        "image_short_side_px": 1440.0,
        "short_side_mismatch_ratio": 0.001241,
        "pitch_match_score": 0.999786,
        "rank": 1,
        "selected": true
      },
      {
        "board_size": 78,
        "columns": 78,
        "rows": 78,
        "capacity_fit": true,
        "projected_side_px": 2157.32049,
        "image_short_side_px": 1440.0,
        "short_side_mismatch_ratio": 0.498139,
        "pitch_match_score": 0.0,
        "rank": 2,
        "selected": false
      }
    ]
  }
}
```

- Use `mode: explicit` with `selection_status: explicit` for an exact user choice.
- Use `mode: auto` with `selection_status: detected` only for strong direct evidence, otherwise use `review`.
- Store `board_size`, candidate `board_size`, `columns`, and `rows` as integers (`52` or `78`), even though CLI choices use `52x52` and `78x78`.
- Keep `selection_confidence` independent from native grid confidence. Set it to `1` for an explicit choice without implying visual certainty.
- Require `content_size` to match `content_bbox` dimensions and `board.csv` to match `rows × columns`.
- Require board `counts` to sum to `rows × columns`; define `bead_count` as the non-background total without changing native counts.
- Record the native-to-board offset, four board margins, and source projection in `placement`; do not rewrite native cell coordinates.
- List both supported sizes in `candidates` when both were evaluated, including rejected capacity candidates.

## Palette JSON

Accept either a top-level array or an object containing `entries`:

```json
{
  "entries": [
    {"name": "background", "symbol": ".", "rgb": "#FFFFFF", "role": "background"},
    {"name": "ivory", "symbol": "W", "rgb": [248, 247, 228], "role": "fill"},
    {"name": "black", "symbol": "K", "rgb": "#0C0B06", "role": "outline"}
  ]
}
```

Require at least two entries, exactly one `background` role, unique names, unique symbols of one to three characters, and RGB channels from 0 through 255. Support role strings `background`, `fill`, `outline`, and `accent`; keep custom strings as metadata but disable role-dependent topology when required roles are absent.

Preserve supplied palette values exactly. A palette role does not authorize semantic redrawing.

## Built-in MARD 221 Compatible Profile

The bundled resource is `assets/palettes/mard-221-compatible.json`. It contains exactly 221 bead entries and no empty-background entry:

```text
A1..A26   B1..B32   C1..C29   D1..D26   E1..E24
F1..F25   G1..G21   H1..H23   M1..M15
```

Reject the resource if codes are missing, duplicated, outside those nine groups, or if RGB and HEX disagree. Do not mix P/Q/R/T/Y/ZG extended-series entries into this 221-code profile.

In restore mode, prepend one synthetic entry at runtime:

```json
{
  "name": "background",
  "symbol": ".",
  "code": null,
  "synthetic": true,
  "rgb": [255, 255, 255],
  "role": "background"
}
```

Derive its RGB from the source border; the shown value is illustrative. It represents empty photographic space, not a bead. Require restore-mode runtime palette length 222, exactly one synthetic background, and 221 coded bead colors.

Generic design mode uses the same 221 physical codes plus the separate runtime synthetic background. It maps every non-empty design cell to a physical code and uses the synthetic entry only when `--background auto` or `empty-white` applies a supported border-connected neutral-white region. Keep these semantics distinct even when a physical designed cell looks white.

Recover structure before catalog color:

1. Infer a compact logical source palette using `--colors` clusters.
2. Recover the foreground mask and enclosed light cells with the standard 4-neighbor topology rule.
3. White-balance the logical cluster centers conservatively from a bright neutral source border.
4. Map each logical foreground cluster—not each textured photo cell independently—to its nearest MARD code using CIE Lab and CIEDE2000.
5. Map all cells in one logical cluster to the same code. Record the selected code, runner-up, ΔE, and margin under `postprocess.palette_matching.cluster_mappings`.

This cluster-level rule prevents yarn, melt texture, shadows, and camera noise from turning one intended bead color into many nearby purchase codes. Keep color confidence no higher than structural confidence, especially for topology-corrected cells.

When this profile is used in restore mode, add top-level `palette_profile` to `pattern.json` and `summary.json`. Require at least:

```json
{
  "id": "mard-221-compatible",
  "code_system": "mard-221",
  "reference_type": "community-open-source-screen-rgb",
  "license_spdx": "MIT",
  "copyright": "Copyright (c) 2026 Jett-Wu",
  "source_commit": "36ac52d570246ab600611a79edd2236bccb954e5",
  "notice_file": "THIRD_PARTY_NOTICES.md",
  "bead_color_count": 221,
  "runtime_entry_count": 222,
  "background_strategy": "synthetic-source-background-after-structural-topology",
  "matching_method": "CIEDE2000",
  "provisional": true
}
```

Treat its HEX/RGB values as public screen references, not official calibrated physical-bead measurements. Warn the user and keep the result at least `review` until codes are checked against the target merchant's current physical card. Treat physical bead diameter as independent merchant inventory; this color table does not establish size availability. A 52×52 or 78×78 board never selects or changes this profile.

The 221 code/HEX pairs are redistributed from the pinned MIT-licensed Jett-Wu source recorded in the resource. Preserve `THIRD_PARTY_NOTICES.md` and the complete `third_party/Jett-Wu-MIT.txt` text in the installed skill and any separately redistributed copy of the full table. Pindou and Bitbead URLs are verification-only references, not asserted licensors. The MARD name is descriptive compatibility terminology only; do not imply official status, affiliation, sponsorship, or endorsement.

## Review and Revision

Generate `review.csv` with:

```csv
row,col,current_label,current_symbol,confidence,alternative,raw_label,reason,new_label,note
```

Copy it, set `new_label` only on confirmed rows, and leave all other rows blank. Resolve zero-based coordinates and labels against native canvas coordinates and palette names, symbols, or supplied codes. Treat `note` as provenance text, never as an instruction beyond the named cell edit.

Run:

```text
restore_pattern.py revise PATTERN_JSON --edits EDITS_CSV --out DIR
  [--render-cell-px 4..64] [--overwrite]
```

Accept minimal edit schemas `row,col,label` or `row,col,symbol`. Reject duplicate coordinates, unknown labels, and out-of-range cells. Do not rerun grid, palette, or board-size inference. Record each accepted cell as `user-confirmed edit`, recompute counts and status, and rerender board artifacts from existing board metadata when present.

Rerender without edits using:

```text
restore_pattern.py render PATTERN_JSON --out DIR
  [--render-cell-px 4..64] [--overwrite]
```

Revision and rerender outputs may omit source overlays when the source image is unavailable. Retain those artifacts from the original restore run.

## Topology Rule

When enabled, combine cells labelled `background` or `fill` into one light mask. Flood from the full native canvas boundary using only up, down, left, and right neighbors:

- turn exterior-reachable light cells into background;
- turn enclosed light cells into fill;
- keep changed cells below the review threshold and retain a reason.

Never use 8-neighbor flood: diagonal corner leakage can erase an enclosed subject. Never delete a light region merely because its component is small. If a real design contains transparent enclosed holes, disable topology or confirm those cells in revision.

## Status Gates

- Set `pass` only when native grid confidence is at least `0.80`, candidate margin at least `0.08`, mean foreground-cell confidence at least `0.88`, review ratio at most `0.03`, and no warning or unresolved board review exists.
- Set `review` when a coherent native pattern exists but any pass condition is unmet, an automatic palette or screen-RGB catalog reference remains provisional, or board `selection_status` is `review`.
- Set `fail` when fewer than four foreground cells remain, native grid confidence is below `0.35`, more than `0.35` of foreground cells require review, or a requested board cannot contain the native content.
- Set every generic design result to `review`; restoration confidence gates do not apply because no native grid is being recovered.

Treat gates as reporting rules, not tuning targets. Do not manipulate native geometry or colors merely to obtain `pass` or fit a board.

Return `0` on normal completion even for `review` or `fail`. With `--strict`, return `2` for non-pass. Return `2` for invalid arguments or malformed inputs and write no success summary.
