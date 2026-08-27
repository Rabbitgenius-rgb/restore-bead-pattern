#!/usr/bin/env python3
"""Offline structural self-test for mobile-bead-pattern-pdf."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader
from reportlab.lib.colors import Color, HexColor
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

import mobile_bead_pdf
from mobile_bead_pdf import PAGE_H, PAGE_W, build


def create_fixture(
    path: Path,
    columns: int,
    rows: int,
    fills: list[tuple[int, int, str, tuple[int, int, int]]],
    *,
    omit_code_at: tuple[int, int] | None = None,
    fill_and_stroke_at: tuple[int, int] | None = None,
) -> None:
    canvas = Canvas(str(path), pagesize=(700, 700), pageCompression=1)
    canvas.setTitle("Synthetic vector bead fixture")
    canvas.setFont("Helvetica-Bold", 20)
    canvas.drawString(40, 640, "Synthetic fixture cover")
    canvas.showPage()
    canvas.setFont("Helvetica", 12)
    canvas.drawString(40, 640, "The vector grid intentionally appears on page three.")
    canvas.showPage()

    cell = min(10.0, 540.0 / columns, 540.0 / rows)
    grid_width = columns * cell
    grid_height = rows * cell
    origin_x = (700 - grid_width) / 2
    origin_y = (700 - grid_height) / 2

    for row in range(rows):
        for col in range(columns):
            x = origin_x + col * cell
            y = origin_y + (rows - 1 - row) * cell
            canvas.setStrokeColor(HexColor("#C7CDD5"))
            canvas.setLineWidth(0.3)
            canvas.rect(x, y, cell, cell, fill=0, stroke=1)

    for row, col, code, rgb in fills:
        x = origin_x + col * cell
        y = origin_y + (rows - 1 - row) * cell
        canvas.setFillColor(Color(*(value / 255 for value in rgb)))
        canvas.setStrokeColor(HexColor("#333333"))
        canvas.rect(x, y, cell, cell, fill=1, stroke=1 if fill_and_stroke_at == (row, col) else 0)
        if omit_code_at == (row, col):
            continue
        font_size = cell * 0.31
        canvas.setFillColor(HexColor("#111111"))
        canvas.setFont("Helvetica", font_size)
        width = stringWidth(code, "Helvetica", font_size)
        canvas.drawString(x + (cell - width) / 2, y + cell * 0.36, code)

    canvas.showPage()
    canvas.save()


def draw_scoring_grid(
    canvas: Canvas,
    fills: list[tuple[int, int, str, tuple[int, int, int]]],
    *,
    fill_and_stroke: bool,
) -> None:
    columns = rows = 4
    cell = 40.0
    origin_x = origin_y = 270.0
    for row in range(rows):
        for col in range(columns):
            x = origin_x + col * cell
            y = origin_y + (rows - 1 - row) * cell
            canvas.setStrokeColor(HexColor("#C7CDD5"))
            canvas.setLineWidth(0.5)
            canvas.rect(x, y, cell, cell, fill=0, stroke=1)
    for row, col, code, rgb in fills:
        x = origin_x + col * cell
        y = origin_y + (rows - 1 - row) * cell
        canvas.setFillColor(Color(*(value / 255 for value in rgb)))
        canvas.setStrokeColor(HexColor("#333333"))
        canvas.rect(x, y, cell, cell, fill=1, stroke=1 if fill_and_stroke else 0)
        canvas.setFillColor(HexColor("#111111"))
        canvas.setFont("Helvetica", 12)
        width = stringWidth(code, "Helvetica", 12)
        canvas.drawString(x + (cell - width) / 2, y + 14, code)


def create_scoring_fixture(path: Path) -> None:
    canvas = Canvas(str(path), pagesize=(700, 700), pageCompression=1)
    canvas.setTitle("Synthetic multi-page scoring fixture")
    draw_scoring_grid(canvas, [(0, 0, "A1", (200, 80, 60))], fill_and_stroke=False)
    canvas.showPage()
    draw_scoring_grid(
        canvas,
        [
            (0, 0, "B1", (40, 80, 120)),
            (1, 1, "B2", (60, 100, 140)),
            (2, 2, "B3", (80, 120, 160)),
        ],
        fill_and_stroke=True,
    )
    canvas.showPage()
    canvas.save()


def assert_pdf(summary: dict[str, object], output: Path, expected_pages: int, expected_beads: int) -> None:
    if summary["status"] != "pass" or summary["kind"] != "mobile-bead-pattern-pdf":
        raise AssertionError("unexpected output status or kind")
    if summary["page_count"] != expected_pages or summary["bead_count"] != expected_beads:
        raise AssertionError("page or bead count mismatch")
    if summary["slice_bead_sum"] != expected_beads:
        raise AssertionError("construction slices do not conserve bead count")
    reader = PdfReader(output)
    if len(reader.pages) != expected_pages:
        raise AssertionError("PDF page count mismatch")
    if {
        (round(float(page.mediabox.width), 2), round(float(page.mediabox.height), 2))
        for page in reader.pages
    } != {(PAGE_W, PAGE_H)}:
        raise AssertionError("PDF page sizes are not uniform phone dimensions")
    links = [
        annotation.get_object()
        for page in reader.pages
        for annotation in (page.get("/Annots") or [])
        if annotation.get_object().get("/Subtype") == "/Link"
    ]
    if not links or any(annotation.get("/Dest") is None for annotation in links):
        raise AssertionError("internal link destinations are incomplete")


def expect_failure(callable_object: object, message: str) -> None:
    try:
        callable_object()
    except ValueError:
        return
    raise AssertionError(message)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mobile-bead-self-test-") as temporary_dir:
        root = Path(temporary_dir)

        small_source = root / "small-source.pdf"
        small_output = root / "small-mobile.pdf"
        small_fills = [
            (2, 2, "H2", (244, 242, 232)),
            (10, 8, "H7", (18, 18, 20)),
            (20, 20, "M15", (119, 134, 151)),
            (33, 32, "H2", (244, 242, 232)),
        ]
        create_fixture(small_source, 34, 35, small_fills, fill_and_stroke_at=(10, 8))
        source_hash = hashlib.sha256(small_source.read_bytes()).hexdigest()
        small_summary = build(small_source, small_output, "SMALL SYNTHETIC", False)
        if hashlib.sha256(small_source.read_bytes()).hexdigest() != source_hash:
            raise AssertionError("source PDF changed")
        if small_summary["source_page_one_based"] != 3:
            raise AssertionError("grid page discovery assumed the wrong page")
        if small_summary["source_grid"] != {"columns": 34, "rows": 35}:
            raise AssertionError("small grid extraction mismatch")
        if small_summary["construction_slices"] != 2 or small_summary["omitted_empty_slices"] != 0:
            raise AssertionError("34-column balanced slicing mismatch")
        assert_pdf(small_summary, small_output, 5, len(small_fills))

        scoring_source = root / "scoring-source.pdf"
        scoring_output = root / "scoring-mobile.pdf"
        create_scoring_fixture(scoring_source)
        scoring_summary = build(scoring_source, scoring_output, None, False)
        if scoring_summary["source_page_one_based"] != 2 or scoring_summary["bead_count"] != 3:
            raise AssertionError("multi-page scoring did not prefer the stronger fill-and-stroke grid")

        uneven_source = root / "uneven-source.pdf"
        uneven_output = root / "uneven-mobile.pdf"
        uneven_fills = [
            (1, 1, "A1", (254, 212, 77)),
            (52, 52, "B12", (26, 91, 168)),
        ]
        create_fixture(uneven_source, 53, 53, uneven_fills)
        uneven_summary = build(uneven_source, uneven_output, None, False)
        if uneven_summary["source_grid"] != {"columns": 53, "rows": 53}:
            raise AssertionError("non-multiple grid extraction mismatch")
        if uneven_summary["construction_slices"] != 2 or uneven_summary["omitted_empty_slices"] != 4:
            raise AssertionError("empty-slice omission mismatch")
        assert_pdf(uneven_summary, uneven_output, 5, len(uneven_fills))

        right_only_source = root / "right-only-source.pdf"
        right_only_output = root / "right-only-mobile.pdf"
        create_fixture(right_only_source, 34, 35, [(5, 30, "C18", (42, 118, 174))])
        right_only_summary = build(right_only_source, right_only_output, None, False)
        if right_only_summary["construction_slices"] != 1 or right_only_summary["omitted_empty_slices"] != 1:
            raise AssertionError("right-only slice routing mismatch")
        assert_pdf(right_only_summary, right_only_output, 4, 1)
        if "RIGHT" not in (PdfReader(right_only_output).pages[-1].extract_text() or ""):
            raise AssertionError("right-only zone did not route to its active slice")

        existing_output = root / "existing.pdf"
        existing_output.write_bytes(b"sentinel")
        expect_failure(
            lambda: build(small_source, existing_output, None, False),
            "existing output was replaced without --overwrite",
        )
        expect_failure(
            lambda: build(small_source, existing_output, None, True),
            "unowned existing output was replaced with --overwrite",
        )
        if existing_output.read_bytes() != b"sentinel":
            raise AssertionError("failed run modified an existing output")

        overwritten_summary = build(small_source, small_output, "SMALL SYNTHETIC", True)
        if overwritten_summary["bead_count"] != len(small_fills):
            raise AssertionError("owned output overwrite changed construction data")

        appeared_output = root / "appeared-output.pdf"
        original_validate_output = mobile_bead_pdf.validate_output

        def create_competing_output(path: Path, expected_pages: int) -> dict[str, object]:
            result = original_validate_output(path, expected_pages)
            appeared_output.write_bytes(b"competing output")
            return result

        with patch("mobile_bead_pdf.validate_output", side_effect=create_competing_output):
            expect_failure(
                lambda: build(small_source, appeared_output, None, False),
                "a competing output was overwritten during no-clobber commit",
            )
        if appeared_output.read_bytes() != b"competing output":
            raise AssertionError("no-clobber race modified the competing output")

        changed_output = root / "changed-output.pdf"
        build(small_source, changed_output, None, False)

        def replace_owned_output(path: Path, expected_pages: int) -> dict[str, object]:
            result = original_validate_output(path, expected_pages)
            changed_output.write_bytes(b"replacement output")
            return result

        with patch("mobile_bead_pdf.validate_output", side_effect=replace_owned_output):
            expect_failure(
                lambda: build(small_source, changed_output, None, True),
                "a changed output was overwritten during guarded commit",
            )
        if changed_output.read_bytes() != b"replacement output":
            raise AssertionError("guarded overwrite modified a changed output")

        linked_output = root / "linked-output.pdf"
        linked_output.symlink_to(existing_output)
        expect_failure(
            lambda: build(small_source, linked_output, None, True),
            "symbolic-link output was not rejected",
        )

        expect_failure(
            lambda: build(small_source, small_source, None, True),
            "source/output identity was not rejected",
        )

        missing_code_source = root / "missing-code.pdf"
        missing_code_output = root / "missing-code-mobile.pdf"
        create_fixture(
            missing_code_source,
            8,
            8,
            [(2, 2, "H2", (200, 100, 80))],
            omit_code_at=(2, 2),
        )
        expect_failure(
            lambda: build(missing_code_source, missing_code_output, None, False),
            "missing per-cell code was not rejected",
        )
        if missing_code_output.exists():
            raise AssertionError("failed extraction left a partial output")

        oversized_source = root / "oversized-source.pdf"
        oversized_output = root / "oversized-mobile.pdf"
        create_fixture(oversized_source, 209, 2, [(0, 0, "H2", (40, 40, 40))])
        expect_failure(
            lambda: build(oversized_source, oversized_output, None, False),
            "grid above the 208-cell dimension limit was not rejected",
        )
        if oversized_output.exists():
            raise AssertionError("oversized extraction left a partial output")

    print("mobile-bead-pattern-pdf self-test: PASS")


if __name__ == "__main__":
    main()
