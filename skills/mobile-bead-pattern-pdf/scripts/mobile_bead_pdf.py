#!/usr/bin/env python3
"""Reflow a vector, cell-coded bead-pattern PDF for phone construction."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import statistics
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pdfplumber
from pypdf import PdfReader
from reportlab.lib.colors import Color, HexColor, white
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas


ALGORITHM_VERSION = "mobile-pdf-1.0.0"
SCHEMA_VERSION = "mobile-pdf-1.0"
PAGE_W = 396.0
PAGE_H = 792.0
ZONE_SIZE = 52
MAX_SLICE_COLUMNS = 26
MAX_GRID_DIMENSION = 208
MAX_INPUT_BYTES = 100 * 1024 * 1024
MAX_INPUT_PAGES = 50
MAX_PAGE_RECTS = 250_000
CODE_PATTERN = re.compile(r"[A-Z][0-9]{1,2}")
OUTPUT_AUTHOR_MARKER = "mobile-bead-pattern-pdf"


@dataclass(frozen=True)
class Pattern:
    source_title: str
    columns: int
    rows: int
    matrix: list[list[dict | None]]
    palette: dict[str, tuple[int, int, int]]
    counts: Counter
    source_page: int


@dataclass(frozen=True)
class Slice:
    zone_row: int
    zone_col: int
    part_index: int
    part_count: int
    row_start: int
    row_end: int
    col_start: int
    col_end: int
    bead_count: int

    @property
    def zone(self) -> str:
        return zone_name(self.zone_row, self.zone_col)

    @property
    def key(self) -> str:
        if self.part_count == 1:
            return "F"
        return "L" if self.part_index == 0 else "R"

    @property
    def label(self) -> str:
        if self.part_count == 1:
            return "FULL"
        return "LEFT" if self.part_index == 0 else "RIGHT"

    @property
    def destination(self) -> str:
        return f"zone-{self.zone}-{self.key}"


def as_rgb(value: object) -> tuple[int, int, int]:
    if isinstance(value, (int, float)):
        channel = max(0, min(255, round(float(value) * 255)))
        return (channel, channel, channel)
    if isinstance(value, (tuple, list)) and len(value) == 1:
        channel = max(0, min(255, round(float(value[0]) * 255)))
        return (channel, channel, channel)
    if isinstance(value, (tuple, list)) and len(value) == 3:
        return tuple(max(0, min(255, round(float(channel) * 255))) for channel in value)
    raise ValueError(f"unsupported PDF fill color: {value!r}")


def safe_title(value: str | None) -> str:
    if not value:
        return "MOBILE BEAD PATTERN"
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = re.sub(r"\s+", " ", ascii_text).strip(" -|\t")
    if not ascii_text:
        return "MOBILE BEAD PATTERN"
    return ascii_text[:48]


def page_grid_candidate(page: object) -> tuple[int, float, float, list[dict], list[float], list[float]] | None:
    groups: Counter = Counter()
    for rect in page.rects:
        width = float(rect["width"])
        height = float(rect["height"])
        if rect["stroke"] and width > 1 and abs(width - height) <= 0.03:
            groups[(round(width, 3), round(height, 3))] += 1

    best = None
    for (width_key, height_key), _ in groups.most_common():
        stroked = [
            rect
            for rect in page.rects
            if rect["stroke"]
            and abs(float(rect["width"]) - width_key) <= 0.02
            and abs(float(rect["height"]) - height_key) <= 0.02
        ]
        by_position: dict[tuple[float, float], dict] = {}
        for rect in stroked:
            position = (round(float(rect["x0"]), 6), round(float(rect["top"]), 6))
            if position not in by_position or (by_position[position]["fill"] and not rect["fill"]):
                by_position[position] = rect
        outlines = list(by_position.values())
        xs = sorted({round(float(rect["x0"]), 6) for rect in outlines})
        ys = sorted({round(float(rect["top"]), 6) for rect in outlines})
        if len(xs) < 2 or len(ys) < 2 or len(outlines) != len(xs) * len(ys):
            continue
        if len(outlines) < 16:
            continue
        candidate = (len(outlines), width_key, height_key, outlines, xs, ys)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best


def extract_pattern(source: Path) -> Pattern:
    with pdfplumber.open(source) as pdf:
        if len(pdf.pages) > MAX_INPUT_PAGES:
            raise ValueError(f"input has more than {MAX_INPUT_PAGES} pages")
        best = None
        first_text = ""
        for page_index, page in enumerate(pdf.pages):
            if len(page.rects) > MAX_PAGE_RECTS:
                raise ValueError(f"page {page_index + 1} exceeds the vector-object safety limit")
            if page_index == 0:
                lines = [line.strip() for line in (page.extract_text() or "").splitlines() if line.strip()]
                first_text = lines[0] if lines else ""
            candidate = page_grid_candidate(page)
            if candidate is None:
                continue
            _, width_key, height_key, _, xs, ys = candidate
            x_positions = set(xs)
            y_positions = set(ys)
            aligned_fills = len(
                {
                    (round(float(rect["x0"]), 6), round(float(rect["top"]), 6))
                    for rect in page.rects
                    if rect["fill"]
                    and abs(float(rect["width"]) - width_key) <= 0.02
                    and abs(float(rect["height"]) - height_key) <= 0.02
                    and round(float(rect["x0"]), 6) in x_positions
                    and round(float(rect["top"]), 6) in y_positions
                }
            )
            score = (candidate[0], aligned_fills, len(page.chars))
            if best is None or score > best[0]:
                best = (score, page_index, candidate)

        if best is None:
            raise ValueError("no complete square vector grid was found")

        _, page_index, (_, cell_w_key, cell_h_key, outlines, xs, ys) = best
        page = pdf.pages[page_index]
        columns, rows = len(xs), len(ys)
        if columns > MAX_GRID_DIMENSION or rows > MAX_GRID_DIMENSION:
            raise ValueError(
                f"grid {columns}x{rows} exceeds the supported {MAX_GRID_DIMENSION}x{MAX_GRID_DIMENSION} maximum"
            )

        cell_w = statistics.median(float(rect["width"]) for rect in outlines)
        cell_h = statistics.median(float(rect["height"]) for rect in outlines)
        step_x = statistics.median(b - a for a, b in zip(xs, xs[1:]))
        step_y = statistics.median(b - a for a, b in zip(ys, ys[1:]))
        base_center_x = xs[0] + cell_w / 2
        base_center_y = ys[0] + cell_h / 2

        chars_by_cell: dict[tuple[int, int], list[dict]] = defaultdict(list)
        for char in page.chars:
            text = str(char.get("text", ""))
            if not re.fullmatch(r"[A-Z0-9]", text):
                continue
            if not (0.5 <= float(char["size"]) <= cell_h * 0.95):
                continue
            center_x = (float(char["x0"]) + float(char["x1"])) / 2
            center_y = (float(char["top"]) + float(char["bottom"])) / 2
            col = round((center_x - base_center_x) / step_x)
            row = round((center_y - base_center_y) / step_y)
            if not (0 <= col < columns and 0 <= row < rows):
                continue
            if xs[col] <= center_x <= xs[col] + cell_w and ys[row] <= center_y <= ys[row] + cell_h:
                chars_by_cell[(row, col)].append(char)

        x_lookup = {value: index for index, value in enumerate(xs)}
        y_lookup = {value: index for index, value in enumerate(ys)}
        matrix: list[list[dict | None]] = [[None for _ in range(columns)] for _ in range(rows)]
        samples: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
        counts: Counter = Counter()

        for rect in page.rects:
            if not rect["fill"]:
                continue
            if abs(float(rect["width"]) - cell_w_key) > 0.02 or abs(float(rect["height"]) - cell_h_key) > 0.02:
                continue
            x_key = round(float(rect["x0"]), 6)
            y_key = round(float(rect["top"]), 6)
            if x_key not in x_lookup or y_key not in y_lookup:
                continue
            row, col = y_lookup[y_key], x_lookup[x_key]
            chars = sorted(chars_by_cell.get((row, col), []), key=lambda item: float(item["x0"]))
            code = "".join(str(char["text"]) for char in chars)
            if not CODE_PATTERN.fullmatch(code):
                raise ValueError(f"invalid or missing cell code at row {row + 1}, col {col + 1}: {code!r}")
            rgb = as_rgb(rect["non_stroking_color"])
            if matrix[row][col] is not None:
                previous = matrix[row][col]
                if previous["code"] == code and previous["rgb"] == rgb:
                    continue
                raise ValueError(f"conflicting duplicate filled cell at row {row + 1}, col {col + 1}")
            matrix[row][col] = {"code": code, "rgb": rgb}
            samples[code].append(rgb)
            counts[code] += 1

        if not counts:
            raise ValueError("the detected grid contains no coded filled cells")
        for (row, col), chars in chars_by_cell.items():
            code = "".join(str(char["text"]) for char in sorted(chars, key=lambda item: float(item["x0"])))
            if CODE_PATTERN.fullmatch(code) and matrix[row][col] is None:
                raise ValueError(f"coded cell has no recoverable fill at row {row + 1}, col {col + 1}")
        palette = {}
        for code, colors in samples.items():
            palette[code] = Counter(colors).most_common(1)[0][0]
            if any(rgb != palette[code] for rgb in colors):
                raise ValueError(f"source contains inconsistent screen colors for code {code}")

        grid_lines = [line.strip() for line in (page.extract_text() or "").splitlines() if line.strip()]
        source_title = first_text or (grid_lines[0] if grid_lines else "")
        return Pattern(source_title, columns, rows, matrix, palette, counts, page_index + 1)


def balanced_ranges(start: int, end: int, max_width: int) -> list[tuple[int, int]]:
    width = end - start
    parts = max(1, math.ceil(width / max_width))
    base, extra = divmod(width, parts)
    ranges = []
    cursor = start
    for index in range(parts):
        part_width = base + (1 if index < extra else 0)
        ranges.append((cursor, cursor + part_width))
        cursor += part_width
    return ranges


def zone_row_label(index: int) -> str:
    value = index + 1
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def zone_name(zone_row: int, zone_col: int) -> str:
    return f"{zone_row_label(zone_row)}{zone_col + 1}"


def make_slices(pattern: Pattern) -> tuple[list[Slice], list[Slice]]:
    all_slices: list[Slice] = []
    zone_rows = math.ceil(pattern.rows / ZONE_SIZE)
    zone_cols = math.ceil(pattern.columns / ZONE_SIZE)
    for zone_row in range(zone_rows):
        row_start = zone_row * ZONE_SIZE
        row_end = min(pattern.rows, row_start + ZONE_SIZE)
        for zone_col in range(zone_cols):
            zone_col_start = zone_col * ZONE_SIZE
            zone_col_end = min(pattern.columns, zone_col_start + ZONE_SIZE)
            ranges = balanced_ranges(zone_col_start, zone_col_end, MAX_SLICE_COLUMNS)
            for part_index, (col_start, col_end) in enumerate(ranges):
                bead_count = sum(
                    1
                    for row in pattern.matrix[row_start:row_end]
                    for item in row[col_start:col_end]
                    if item is not None
                )
                all_slices.append(
                    Slice(
                        zone_row,
                        zone_col,
                        part_index,
                        len(ranges),
                        row_start,
                        row_end,
                        col_start,
                        col_end,
                        bead_count,
                    )
                )
    return all_slices, [item for item in all_slices if item.bead_count]


def pdf_color(rgb: tuple[int, int, int]) -> Color:
    return Color(*(channel / 255 for channel in rgb))


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    values = []
    for channel in rgb:
        value = channel / 255
        values.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2]


def draw_background(canvas: Canvas) -> None:
    canvas.setFillColor(HexColor("#F7F8FA"))
    canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)


def draw_footer(canvas: Canvas, page_number: int) -> None:
    canvas.setFillColor(HexColor("#747C88"))
    canvas.setFont("Helvetica", 7.2)
    canvas.drawString(20, 12, "Screen colors are references; confirm with a physical color card.")
    canvas.drawRightString(PAGE_W - 20, 12, str(page_number))


def centered_stroked(
    canvas: Canvas,
    text: str,
    center_x: float,
    baseline_y: float,
    size: float,
    fill: Color,
    stroke: Color,
    stroke_width: float,
) -> None:
    width = stringWidth(text, "Helvetica-Bold", size)
    canvas.setLineWidth(stroke_width)
    text_object = canvas.beginText()
    text_object.setTextOrigin(center_x - width / 2, baseline_y)
    text_object.setFont("Helvetica-Bold", size)
    text_object.setFillColor(fill)
    text_object.setStrokeColor(stroke)
    text_object.setTextRenderMode(2)
    text_object.textOut(text)
    canvas.drawText(text_object)
    reset_object = canvas.beginText()
    reset_object.setTextRenderMode(0)
    canvas.drawText(reset_object)


def draw_code(canvas: Canvas, code: str, rgb: tuple[int, int, int], x: float, y: float, cell: float) -> None:
    match = re.fullmatch(r"([A-Z])([0-9]{1,2})", code)
    if not match:
        return
    prefix, number = match.groups()
    dark = relative_luminance(rgb) < 0.38
    fill = white if dark else HexColor("#15181D")
    stroke = HexColor("#15181D") if dark else white
    canvas.setFillColor(fill)
    canvas.setFont("Helvetica-Bold", cell * 0.27)
    canvas.drawString(x + cell * 0.07, y + cell * 0.69, prefix)
    number_size = cell * (0.62 if len(number) == 1 else 0.54)
    centered_stroked(
        canvas,
        number,
        x + cell * 0.54,
        y + cell * 0.18,
        number_size,
        fill,
        stroke,
        max(0.14, cell * 0.016),
    )


def draw_matrix(
    canvas: Canvas,
    pattern: Pattern,
    x: float,
    y: float,
    cell: float,
    *,
    row_start: int = 0,
    row_end: int | None = None,
    col_start: int = 0,
    col_end: int | None = None,
    codes: bool = False,
) -> None:
    row_end = pattern.rows if row_end is None else row_end
    col_end = pattern.columns if col_end is None else col_end
    local_rows = row_end - row_start
    local_cols = col_end - col_start
    for local_row, row in enumerate(range(row_start, row_end)):
        for local_col, col in enumerate(range(col_start, col_end)):
            item = pattern.matrix[row][col]
            x0 = x + local_col * cell
            y0 = y + (local_rows - 1 - local_row) * cell
            canvas.setFillColor(pdf_color(item["rgb"]) if item else HexColor("#FBFCFD"))
            canvas.rect(x0, y0, cell, cell, fill=1, stroke=0)
            canvas.setStrokeColor(HexColor("#D8DDE4"))
            canvas.setLineWidth(0.28)
            canvas.rect(x0, y0, cell, cell, fill=0, stroke=1)
            if codes and item:
                draw_code(canvas, item["code"], item["rgb"], x0, y0, cell)
    canvas.setStrokeColor(HexColor("#AAB2BE"))
    canvas.setLineWidth(0.75)
    canvas.rect(x, y, local_cols * cell, local_rows * cell, fill=0, stroke=1)


def draw_guides(canvas: Canvas, item: Slice, x: float, y: float, cell: float) -> None:
    local_rows = item.row_end - item.row_start
    local_cols = item.col_end - item.col_start
    grid_w = local_cols * cell
    grid_h = local_rows * cell
    accent = HexColor("#E65527")
    normal = HexColor("#657080")

    for absolute_col in range(item.col_start + 1, item.col_end + 1):
        if absolute_col in (item.col_start + 1, item.col_end) or absolute_col % 5 == 0:
            local = absolute_col - item.col_start - 0.5
            center_x = x + local * cell
            canvas.setFont("Helvetica-Bold" if absolute_col % 5 == 0 else "Helvetica", 6.1)
            canvas.setFillColor(accent if absolute_col % 5 == 0 else normal)
            canvas.drawCentredString(center_x, y + grid_h + 4, str(absolute_col))
            canvas.drawCentredString(center_x, y - 9, str(absolute_col))

    for absolute_row in range(item.row_start + 1, item.row_end + 1):
        if absolute_row in (item.row_start + 1, item.row_end) or absolute_row % 5 == 0:
            local = absolute_row - item.row_start - 0.5
            center_y = y + (local_rows - local) * cell - 2
            canvas.setFont("Helvetica-Bold" if absolute_row % 5 == 0 else "Helvetica", 6.1)
            canvas.setFillColor(accent if absolute_row % 5 == 0 else normal)
            canvas.drawRightString(x - 4.5, center_y, str(absolute_row))
            canvas.drawString(x + grid_w + 4.5, center_y, str(absolute_row))

    canvas.setStrokeColor(HexColor("#C97935"))
    for boundary in range(item.row_start + 1, item.row_end):
        if boundary % 5 == 0:
            line_y = y + (item.row_end - boundary) * cell
            canvas.setLineWidth(0.65 if boundary % 10 else 0.9)
            canvas.line(x, line_y, x + grid_w, line_y)
    for boundary in range(item.col_start + 1, item.col_end):
        if boundary % 5 == 0:
            line_x = x + (boundary - item.col_start) * cell
            canvas.setLineWidth(0.65 if boundary % 10 else 0.9)
            canvas.line(line_x, y, line_x, y + grid_h)


def draw_cover(canvas: Canvas, pattern: Pattern, display_title: str, detail_pages: int, page_number: int) -> None:
    draw_background(canvas)
    canvas.bookmarkPage("cover")
    canvas.addOutlineEntry("Overview", "cover", level=0)
    canvas.setFillColor(HexColor("#171B21"))
    canvas.setFont("Helvetica-Bold", 21)
    canvas.drawString(22, 748, display_title)
    canvas.setFillColor(HexColor("#616A77"))
    canvas.setFont("Helvetica", 9.2)
    canvas.drawString(
        22,
        728,
        f"Grid {pattern.columns} x {pattern.rows}  |  {sum(pattern.counts.values()):,} beads  |  {len(pattern.counts)} colors",
    )

    preview_cell = min((PAGE_W - 44) / pattern.columns, 330 / pattern.rows)
    preview_w = pattern.columns * preview_cell
    preview_h = pattern.rows * preview_cell
    preview_x = (PAGE_W - preview_w) / 2
    preview_y = 350
    draw_matrix(canvas, pattern, preview_x, preview_y, preview_cell)

    canvas.setFillColor(HexColor("#20252C"))
    canvas.setFont("Helvetica-Bold", 15)
    canvas.drawString(22, 310, "PHONE CONSTRUCTION EDITION")
    canvas.setFillColor(HexColor("#5E6774"))
    canvas.setFont("Helvetica", 8.8)
    lines = [
        "Pattern data is unchanged; only the viewing layout is split.",
        "Zones are at most 52 x 52 cells; slices are at most 26 cells wide.",
        f"{detail_pages} enlarged non-empty construction pages use absolute coordinates.",
        "Open SECTION INDEX to jump directly to any active zone.",
    ]
    for index, line in enumerate(lines):
        canvas.drawString(22, 286 - index * 20, line)

    canvas.setFillColor(white)
    canvas.setStrokeColor(HexColor("#DDE2E8"))
    canvas.roundRect(22, 105, PAGE_W - 44, 66, 7, fill=1, stroke=1)
    canvas.setFillColor(HexColor("#D43C2C"))
    canvas.setFont("Helvetica-Bold", 12)
    canvas.drawString(36, 145, "NAVIGATION")
    canvas.setFillColor(HexColor("#303640"))
    canvas.setFont("Helvetica", 9)
    canvas.drawString(36, 126, f"Palette pages -> Section index -> {detail_pages} non-empty slices")
    canvas.drawString(36, 111, "Links work in viewers that support internal PDF navigation.")
    canvas.linkRect("", "section-index", Rect=(22, 105, PAGE_W - 22, 171), relative=0, thickness=0)
    draw_footer(canvas, page_number)
    canvas.showPage()


def draw_palette_pages(canvas: Canvas, pattern: Pattern, start_page: int) -> int:
    ordered = sorted(pattern.counts.items(), key=lambda entry: (-entry[1], entry[0]))
    chunks = [ordered[index : index + 26] for index in range(0, len(ordered), 26)]
    page_number = start_page
    for chunk_index, chunk in enumerate(chunks, start=1):
        destination = f"palette-{chunk_index}"
        draw_background(canvas)
        canvas.bookmarkPage(destination)
        canvas.addOutlineEntry(f"Palette {chunk_index}/{len(chunks)}", destination, level=0)
        canvas.setFillColor(HexColor("#171B21"))
        canvas.setFont("Helvetica-Bold", 22)
        canvas.drawString(22, 748, f"COLOR LIST  {chunk_index}/{len(chunks)}")
        canvas.setFillColor(HexColor("#626B77"))
        canvas.setFont("Helvetica", 9)
        canvas.drawString(22, 728, "Recovered code and required quantity")
        canvas.setFillColor(HexColor("#D43C2C"))
        canvas.setFont("Helvetica-Bold", 8.5)
        canvas.drawRightString(PAGE_W - 22, 744, "SECTION INDEX")
        canvas.linkRect("", "section-index", Rect=(PAGE_W - 110, 730, PAGE_W - 20, 764), relative=0, thickness=0)

        for index, (code, count) in enumerate(chunk):
            column = index % 2
            row = index // 2
            x = 22 + column * 182
            y = 671 - row * 49
            canvas.setFillColor(white)
            canvas.setStrokeColor(HexColor("#DFE4EA"))
            canvas.roundRect(x, y, 166, 42, 5, fill=1, stroke=1)
            canvas.setFillColor(pdf_color(pattern.palette[code]))
            canvas.roundRect(x + 5, y + 5, 58, 32, 4, fill=1, stroke=0)
            text_fill = white if relative_luminance(pattern.palette[code]) < 0.38 else HexColor("#171B21")
            canvas.setFillColor(text_fill)
            canvas.setFont("Helvetica-Bold", 10)
            canvas.drawCentredString(x + 34, y + 15, code)
            canvas.setFillColor(HexColor("#252B33"))
            canvas.setFont("Helvetica-Bold", 10)
            canvas.drawString(x + 72, y + 22, code)
            canvas.setFillColor(HexColor("#647080"))
            canvas.setFont("Helvetica", 9)
            canvas.drawString(x + 72, y + 8, f"qty {count:,}")
        draw_footer(canvas, page_number)
        canvas.showPage()
        page_number += 1
    return page_number


def slices_for_zone(all_slices: list[Slice], zone_row: int, zone_col: int) -> list[Slice]:
    return [item for item in all_slices if item.zone_row == zone_row and item.zone_col == zone_col]


def draw_section_index(canvas: Canvas, pattern: Pattern, all_slices: list[Slice], page_number: int) -> None:
    zone_rows = math.ceil(pattern.rows / ZONE_SIZE)
    zone_cols = math.ceil(pattern.columns / ZONE_SIZE)
    draw_background(canvas)
    canvas.bookmarkPage("section-index")
    canvas.addOutlineEntry("Section index", "section-index", level=0)
    canvas.setFillColor(HexColor("#171B21"))
    canvas.setFont("Helvetica-Bold", 23)
    canvas.drawString(22, 748, "SECTION INDEX")
    canvas.setFillColor(HexColor("#626B77"))
    canvas.setFont("Helvetica", 9)
    canvas.drawString(22, 728, "Tap a labeled zone to open its first non-empty construction page.")

    cell = min((PAGE_W - 44) / pattern.columns, 352 / pattern.rows)
    grid_w = pattern.columns * cell
    grid_h = pattern.rows * cell
    x = (PAGE_W - grid_w) / 2
    y = 325
    draw_matrix(canvas, pattern, x, y, cell)
    canvas.setStrokeColor(HexColor("#EA4E3D"))
    canvas.setLineWidth(1.45)
    for zone_col in range(1, zone_cols):
        line_x = x + zone_col * ZONE_SIZE * cell
        canvas.line(line_x, y, line_x, y + grid_h)
    for zone_row in range(1, zone_rows):
        line_y = y + (pattern.rows - zone_row * ZONE_SIZE) * cell
        canvas.line(x, line_y, x + grid_w, line_y)

    for zone_row in range(zone_rows):
        for zone_col in range(zone_cols):
            name = zone_name(zone_row, zone_col)
            zone_slices = slices_for_zone(all_slices, zone_row, zone_col)
            active = [item for item in zone_slices if item.bead_count]
            col_start = zone_col * ZONE_SIZE
            col_end = min(pattern.columns, col_start + ZONE_SIZE)
            row_start = zone_row * ZONE_SIZE
            row_end = min(pattern.rows, row_start + ZONE_SIZE)
            left = x + col_start * cell
            right = x + col_end * cell
            top = y + (pattern.rows - row_start) * cell
            bottom = y + (pattern.rows - row_end) * cell
            center_x = (left + right) / 2
            center_y = (top + bottom) / 2
            canvas.setFillColor(Color(1, 1, 1, alpha=0.88))
            canvas.setStrokeColor(HexColor("#EA4E3D") if active else HexColor("#AAB2BE"))
            canvas.setLineWidth(0.9)
            canvas.circle(center_x, center_y, 10, fill=1, stroke=1)
            canvas.setFillColor(HexColor("#D43C2C") if active else HexColor("#7C8591"))
            canvas.setFont("Helvetica-Bold", 8)
            canvas.drawCentredString(center_x, center_y - 3, name)
            if active:
                canvas.linkRect("", active[0].destination, Rect=(left, bottom, right, top), relative=0, thickness=0)

    canvas.setFillColor(HexColor("#20252C"))
    canvas.setFont("Helvetica-Bold", 13)
    canvas.drawString(22, 282, "ZONE MAP")
    canvas.setFillColor(HexColor("#5F6875"))
    canvas.setFont("Helvetica", 8.5)
    canvas.drawString(22, 264, "Row bands use letters; column bands use numbers. Gray zones contain no beads.")

    gap_x = 10
    button_w = (PAGE_W - 44 - gap_x * (zone_cols - 1)) / zone_cols
    button_h = 31
    start_y = 216
    for zone_row in range(zone_rows):
        for zone_col in range(zone_cols):
            name = zone_name(zone_row, zone_col)
            zone_slices = slices_for_zone(all_slices, zone_row, zone_col)
            active = [item for item in zone_slices if item.bead_count]
            bx = 22 + zone_col * (button_w + gap_x)
            by = start_y - zone_row * 40
            canvas.setFillColor(white)
            canvas.setStrokeColor(HexColor("#DFE4EA"))
            canvas.roundRect(bx, by, button_w, button_h, 5, fill=1, stroke=1)
            canvas.setFillColor(HexColor("#D43C2C") if active else HexColor("#7C8591"))
            canvas.setFont("Helvetica-Bold", 10)
            canvas.drawCentredString(bx + button_w / 2, by + 15, name)
            canvas.setFont("Helvetica-Bold", 5.8)
            status = " + ".join(item.label for item in active) if active else "NO BEADS"
            canvas.drawCentredString(bx + button_w / 2, by + 6, status)
            if active:
                canvas.linkRect("", active[0].destination, Rect=(bx, by, bx + button_w, by + button_h), relative=0, thickness=0)
    draw_footer(canvas, page_number)
    canvas.showPage()


def draw_detail_page(
    canvas: Canvas,
    pattern: Pattern,
    item: Slice,
    zone_slices: list[Slice],
    page_number: int,
) -> None:
    draw_background(canvas)
    canvas.bookmarkPage(item.destination)
    canvas.addOutlineEntry(f"Zone {item.zone} {item.label}", item.destination, level=0)
    canvas.setFillColor(HexColor("#171B21"))
    canvas.setFont("Helvetica-Bold", 19)
    canvas.drawString(18, 757, f"ZONE {item.zone} - {item.label}")
    canvas.setFillColor(HexColor("#626B77"))
    canvas.setFont("Helvetica", 8.3)
    canvas.drawString(
        18,
        740,
        f"Rows {item.row_start + 1}-{item.row_end}  |  Columns {item.col_start + 1}-{item.col_end}  |  Full cell codes",
    )

    canvas.setFillColor(HexColor("#D43C2C"))
    canvas.setFont("Helvetica-Bold", 7.5)
    canvas.drawRightString(PAGE_W - 18, 757, "INDEX")
    canvas.linkRect("", "section-index", Rect=(PAGE_W - 66, 744, PAGE_W - 16, 772), relative=0, thickness=0)
    others = [candidate for candidate in zone_slices if candidate.bead_count and candidate != item]
    if others:
        other = others[0]
        label = "NEXT SLICE" if other.part_index > item.part_index else "PREVIOUS SLICE"
        canvas.drawRightString(PAGE_W - 18, 740, label)
        canvas.linkRect("", other.destination, Rect=(PAGE_W - 105, 727, PAGE_W - 16, 747), relative=0, thickness=0)
    else:
        canvas.setFillColor(HexColor("#8B939E"))
        canvas.drawRightString(PAGE_W - 18, 740, "ONLY NON-EMPTY SLICE")

    local_rows = item.row_end - item.row_start
    local_cols = item.col_end - item.col_start
    cell = min((PAGE_W - 62) / local_cols, (PAGE_H - 100) / local_rows)
    grid_w = local_cols * cell
    grid_h = local_rows * cell
    x = (PAGE_W - grid_w) / 2
    y = 45
    draw_matrix(
        canvas,
        pattern,
        x,
        y,
        cell,
        row_start=item.row_start,
        row_end=item.row_end,
        col_start=item.col_start,
        col_end=item.col_end,
        codes=True,
    )
    draw_guides(canvas, item, x, y, cell)
    draw_footer(canvas, page_number)
    canvas.showPage()


def validate_output(path: Path, expected_pages: int) -> dict[str, object]:
    reader = PdfReader(path)
    if len(reader.pages) != expected_pages:
        raise ValueError(f"output page count mismatch: expected {expected_pages}, got {len(reader.pages)}")
    sizes = {
        (round(float(page.mediabox.width), 2), round(float(page.mediabox.height), 2))
        for page in reader.pages
    }
    if sizes != {(PAGE_W, PAGE_H)}:
        raise ValueError(f"output contains unexpected page sizes: {sorted(sizes)}")
    page_ids = {
        page.indirect_reference.idnum
        for page in reader.pages
        if page.indirect_reference is not None
    }
    links = []
    for page in reader.pages:
        for annotation_ref in page.get("/Annots") or []:
            annotation = annotation_ref.get_object()
            if annotation.get("/Subtype") == "/Link":
                links.append(annotation)
                destination = annotation.get("/Dest")
                if destination is None or not destination:
                    raise ValueError("one or more internal links are missing a direct destination")
                target = destination[0]
                if not hasattr(target, "idnum") or target.idnum not in page_ids:
                    raise ValueError("one or more internal links target a missing page")
                rect = [float(value) for value in annotation.get("/Rect")]
                if len(rect) != 4 or rect[0] < 0 or rect[1] < 0 or rect[2] > PAGE_W or rect[3] > PAGE_H:
                    raise ValueError("one or more internal link rectangles fall outside the page")
    if not links:
        raise ValueError("output contains no internal links")
    return {"page_count": len(reader.pages), "link_annotations": len(links)}


def owned_output_fingerprint(path: Path) -> tuple[int, int, int, int, str] | None:
    try:
        path_stat = os.lstat(path)
        if not stat.S_ISREG(path_stat.st_mode):
            return None
        with path.open("rb") as stream:
            opened_stat = os.fstat(stream.fileno())
            if (opened_stat.st_dev, opened_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                return None
            if stream.read(5) != b"%PDF-":
                return None
            stream.seek(0)
            digest = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            stream.seek(0)
            reader = PdfReader(stream)
            if not reader.metadata or reader.metadata.author != OUTPUT_AUTHOR_MARKER:
                return None
            final_stat = os.fstat(stream.fileno())
            if (
                final_stat.st_dev,
                final_stat.st_ino,
                final_stat.st_size,
                final_stat.st_mtime_ns,
            ) != (
                opened_stat.st_dev,
                opened_stat.st_ino,
                opened_stat.st_size,
                opened_stat.st_mtime_ns,
            ):
                return None
    except Exception:
        return None
    return (
        opened_stat.st_dev,
        opened_stat.st_ino,
        opened_stat.st_size,
        opened_stat.st_mtime_ns,
        digest.hexdigest(),
    )


def build(source: Path, output: Path, requested_title: str | None, overwrite: bool) -> dict[str, object]:
    source = source.expanduser().resolve()
    requested_output = output.expanduser()
    if requested_output.is_symlink():
        raise ValueError("output must not be a symbolic link")
    output = requested_output.resolve()
    if not source.is_file():
        raise ValueError(f"input PDF does not exist: {source}")
    if source.suffix.lower() != ".pdf":
        raise ValueError("input must be a PDF")
    if source.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"input exceeds the {MAX_INPUT_BYTES // (1024 * 1024)} MiB safety limit")
    if output.suffix.lower() != ".pdf":
        raise ValueError("output must use a .pdf extension")
    if source == output:
        raise ValueError("output must not overwrite the source PDF")
    existing_output_fingerprint = None
    if output.exists():
        if not output.is_file():
            raise ValueError("output path exists and is not a regular file")
        if not overwrite:
            raise ValueError("output already exists; choose a new path or pass --overwrite")
        existing_output_fingerprint = owned_output_fingerprint(output)
        if existing_output_fingerprint is None:
            raise ValueError("--overwrite may replace only a PDF previously created by this tool")
    output.parent.mkdir(parents=True, exist_ok=True)

    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    pattern = extract_pattern(source)
    all_slices, detail_slices = make_slices(pattern)
    if not detail_slices:
        raise ValueError("no non-empty construction slices were produced")
    palette_pages = math.ceil(len(pattern.counts) / 26)
    expected_pages = 1 + palette_pages + 1 + len(detail_slices)
    display_title = safe_title(requested_title or pattern.source_title)

    descriptor, temporary_name = tempfile.mkstemp(prefix=".mobile-bead-", suffix=".pdf", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        canvas = Canvas(str(temporary), pagesize=(PAGE_W, PAGE_H), pageCompression=1)
        canvas.setTitle(f"{display_title} - mobile bead pattern")
        canvas.setAuthor(OUTPUT_AUTHOR_MARKER)
        canvas.setSubject("Phone-first construction layout for a supplied vector bead-pattern PDF")
        draw_cover(canvas, pattern, display_title, len(detail_slices), 1)
        next_page = draw_palette_pages(canvas, pattern, 2)
        draw_section_index(canvas, pattern, all_slices, next_page)
        page_number = next_page + 1
        for item in detail_slices:
            draw_detail_page(
                canvas,
                pattern,
                item,
                slices_for_zone(all_slices, item.zone_row, item.zone_col),
                page_number,
            )
            page_number += 1
        canvas.save()
        validation = validate_output(temporary, expected_pages)
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_sha256:
            raise ValueError("source PDF changed during processing")
        directory_descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            fcntl.flock(directory_descriptor, fcntl.LOCK_EX)
            if existing_output_fingerprint is None:
                try:
                    os.link(temporary, output)
                except FileExistsError as error:
                    raise ValueError("output appeared during processing; no file was replaced") from error
            else:
                if output.is_symlink() or owned_output_fingerprint(output) != existing_output_fingerprint:
                    raise ValueError("output changed during processing; no file was replaced")
                os.replace(temporary, output)
        finally:
            fcntl.flock(directory_descriptor, fcntl.LOCK_UN)
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "status": "pass",
        "kind": "mobile-bead-pattern-pdf",
        "source_sha256": source_sha256,
        "source_page_one_based": pattern.source_page,
        "source_grid": {"columns": pattern.columns, "rows": pattern.rows},
        "bead_count": sum(pattern.counts.values()),
        "color_count": len(pattern.counts),
        "zone_count": math.ceil(pattern.rows / ZONE_SIZE) * math.ceil(pattern.columns / ZONE_SIZE),
        "construction_slices": len(detail_slices),
        "omitted_empty_slices": len(all_slices) - len(detail_slices),
        "slice_bead_sum": sum(item.bead_count for item in detail_slices),
        "page_count": validation["page_count"],
        "page_size_points": [int(PAGE_W), int(PAGE_H)],
        "link_annotations": validation["link_annotations"],
        "sha256": digest,
        "bytes": output.stat().st_size,
        "output": output.name,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reflow a supported vector, cell-coded bead-pattern PDF into a phone-readable construction PDF."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--title")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        summary = build(args.input, args.output, args.title, args.overwrite)
    except Exception as exc:
        print(f"mobile-bead-pattern-pdf: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
