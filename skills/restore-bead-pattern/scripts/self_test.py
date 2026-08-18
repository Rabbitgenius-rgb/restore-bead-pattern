#!/usr/bin/env python3
"""Offline self-test for the bundled restore and independent design CLIs.

The test creates a tiny deterministic, yarn-textured grid inside a temporary
directory.  No source fixture or generated bitmap is stored in the skill.
It checks the light-region topology rule directly and then exercises the public
CLI, including its JSON/CSV/PNG contracts and source-path privacy guarantee.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import random
import subprocess
import sys
import tempfile
from collections import Counter, deque
from pathlib import Path
from types import ModuleType
from typing import Any


# A self-test must not dirty an installed skill directory merely by importing
# the production module.
sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
RESTORE_SCRIPT = SCRIPT_DIR / "restore_pattern.py"
DESIGN_SCRIPT = SCRIPT_DIR / "design_bead_pattern.py"
MARD_221_RESOURCE = SCRIPT_DIR.parent / "assets" / "palettes" / "mard-221-compatible.json"
OUTPUT_MARKER_NAME = ".restore-bead-pattern-output"
OUTPUT_MARKER_CONTENT = "restore-bead-pattern-output-v1\n"
GRID_COLUMNS = 10
GRID_ROWS = 10
CELL_PIXELS = 20
MARD_221_GROUPS = {
    "A": 26,
    "B": 32,
    "C": 29,
    "D": 26,
    "E": 24,
    "F": 25,
    "G": 21,
    "H": 23,
    "M": 15,
}


class SelfTestFailure(AssertionError):
    """A concise assertion error intended for command-line output."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SelfTestFailure(message)


def require_value_error(action: Any, expected: str, label: str) -> None:
    try:
        action()
    except ValueError as exc:
        require(
            expected in str(exc),
            f"{label} raised the wrong error: {exc}",
        )
    else:
        raise SelfTestFailure(f"{label} unexpectedly succeeded")


def assert_output_marker(output_dir: Path) -> None:
    marker = output_dir / OUTPUT_MARKER_NAME
    require(marker.is_file() and not marker.is_symlink(), f"missing output marker: {output_dir}")
    require(
        marker.read_text(encoding="utf-8") == OUTPUT_MARKER_CONTENT,
        f"invalid output marker: {output_dir}",
    )


def load_restore_module() -> ModuleType:
    require(RESTORE_SCRIPT.is_file(), f"missing restore script: {RESTORE_SCRIPT}")
    spec = importlib.util.spec_from_file_location("restore_pattern_self_test_target", RESTORE_SCRIPT)
    require(spec is not None and spec.loader is not None, "could not load restore_pattern.py")
    module = importlib.util.module_from_spec(spec)
    # dataclasses and a few other stdlib helpers expect the module to be present
    # while its top-level code executes.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_design_module() -> ModuleType:
    require(DESIGN_SCRIPT.is_file(), f"missing design script: {DESIGN_SCRIPT}")
    spec = importlib.util.spec_from_file_location(
        "design_bead_pattern_self_test_target", DESIGN_SCRIPT
    )
    require(spec is not None and spec.loader is not None, "could not load design_bead_pattern.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_design_contain_geometry(module: ModuleType) -> None:
    """Contain must never allocate a source-long-side squared canvas."""

    for width, height, size in (
        (1, 50_000_000, 78),
        (50_000_000, 1, 78),
        (120, 160, 52),
    ):
        geometry = module.contain_geometry(width, height, size)
        require(
            (geometry["canvas_width"], geometry["canvas_height"]) == (size, size),
            "contain geometry created a source-sized intermediate canvas",
        )
        require(
            1 <= geometry["target_width"] <= size
            and 1 <= geometry["target_height"] <= size,
            "contain target lies outside the selected board",
        )
        require(
            geometry["target_width"] == size or geometry["target_height"] == size,
            "contain target does not use the available board extent",
        )
    mask = module.np.zeros((7, 11), dtype=bool)
    mask[2:6, 3:9] = True
    require(
        module.boolean_mask_bbox(mask) == (3, 2, 9, 6),
        "content-aware bbox axis reduction returned the wrong half-open box",
    )
    require(
        module.boolean_mask_bbox(module.np.zeros((7, 11), dtype=bool)) is None,
        "content-aware bbox did not report an empty mask",
    )


def expected_mard_221_codes() -> set[str]:
    return {
        f"{group}{number}"
        for group, count in MARD_221_GROUPS.items()
        for number in range(1, count + 1)
    }


def test_mard_221_resource_contract() -> None:
    """The built-in card is exactly the 221-color base system, never 291."""

    require(MARD_221_RESOURCE.is_file(), f"missing MARD 221 resource: {MARD_221_RESOURCE}")
    payload = json.loads(MARD_221_RESOURCE.read_text(encoding="utf-8"))
    require(isinstance(payload, dict), "MARD 221 resource root must be an object")
    require(payload.get("id") == "mard-221-compatible", "MARD resource id is incorrect")
    require(payload.get("code_system") == "mard-221", "MARD resource code_system is incorrect")
    require(payload.get("reference_type") == "community-open-source-screen-rgb", "MARD RGB provenance is missing")
    require(payload.get("license_spdx") == "MIT", "MARD resource license identifier is missing")
    require(payload.get("copyright") == "Copyright (c) 2026 Jett-Wu", "MARD upstream copyright is missing")
    require(
        payload.get("source_commit") == "36ac52d570246ab600611a79edd2236bccb954e5",
        "MARD resource is not pinned to the licensed upstream commit",
    )
    require(payload.get("notice_file") == "THIRD_PARTY_NOTICES.md", "MARD notice path is missing")
    require(payload.get("groups") == MARD_221_GROUPS, "MARD resource group metadata is incorrect")
    require(
        isinstance(payload.get("disclaimer"), str)
        and "not an official" in payload["disclaimer"].lower()
        and "physical" in payload["disclaimer"].lower(),
        "MARD resource must disclose that screen RGB is not official physical measurement",
    )
    sources = payload.get("sources")
    require(isinstance(sources, list) and len(sources) >= 2, "MARD resource needs traceable source references")
    require(
        all(isinstance(source, dict) and str(source.get("url", "")).startswith("https://") for source in sources),
        "MARD resource contains an invalid source URL",
    )
    require(
        sources[0].get("type") == "licensed-redistribution-source"
        and sources[0].get("license_spdx") == "MIT",
        "MARD licensed redistribution source is not primary",
    )
    require(
        all(source.get("type") == "verification-only" for source in sources[1:]),
        "unlicensed MARD cross-checks must be verification-only",
    )

    entries = payload.get("entries")
    require(isinstance(entries, list) and len(entries) == 221, "MARD resource must contain exactly 221 bead entries")
    codes: list[str] = []
    names: set[str] = set()
    symbols: set[str] = set()
    groups: Counter[str] = Counter()
    forbidden_groups = {"P", "Q", "R", "T", "Y", "ZG"}
    for entry in entries:
        require(isinstance(entry, dict), "every MARD entry must be an object")
        code = entry.get("code")
        name = entry.get("name")
        symbol = entry.get("symbol")
        require(isinstance(code, str) and code, "MARD entry has no code")
        require(name == f"mard-{code.lower()}", f"MARD {code} has an unstable name")
        require(symbol == code, f"MARD {code} symbol must equal its purchase code")
        require(name not in names and symbol not in symbols and code not in codes, f"duplicate MARD identity: {code}")
        names.add(name)
        symbols.add(symbol)
        codes.append(code)
        group = code.rstrip("0123456789")
        groups[group] += 1
        require(group not in forbidden_groups, f"expanded MARD group {group} leaked into the 221 card")
        require(entry.get("role") == "accent", f"MARD {code} must not masquerade as a semantic/background role")
        require(not entry.get("synthetic", False), f"MARD {code} must be a physical bead entry")
        rgb = entry.get("rgb")
        require(
            isinstance(rgb, list)
            and len(rgb) == 3
            and all(type(channel) is int and 0 <= channel <= 255 for channel in rgb),
            f"MARD {code} has invalid RGB",
        )
        rgb_hex = "#" + "".join(f"{channel:02X}" for channel in rgb)
        require(entry.get("rgb_hex") == rgb_hex, f"MARD {code} RGB and HEX disagree")

    expected = expected_mard_221_codes()
    require(set(codes) == expected, "MARD 221 resource has a missing, extra, or non-contiguous code")
    require(groups == Counter(MARD_221_GROUPS), f"MARD 221 group counts are wrong: {dict(groups)}")


def test_mard_builtin_loader(module: ModuleType) -> None:
    np = module.np
    representatives = np.zeros((3, 3, 3), dtype=np.float32)
    background = np.asarray((253, 252, 251), dtype=np.float64)
    canonical = module.load_palette_bundle(
        "mard-221-compatible", representatives, background, colors=2, seed=0
    )
    alias = module.load_palette_bundle("mard-221", representatives, background, colors=16, seed=19)
    require(canonical.entries == alias.entries, "MARD short alias loads a different card")
    require(canonical.profile == alias.profile, "MARD short alias changes profile provenance")
    require(len(canonical.entries) == 222, "MARD runtime palette must be 221 beads plus one background")
    synthetic = canonical.entries[0]
    require(
        synthetic.name == "background"
        and synthetic.symbol == "."
        and synthetic.code is None
        and synthetic.synthetic is True
        and synthetic.role == "background"
        and synthetic.rgb == (253, 252, 251),
        "MARD runtime background is not a dynamic synthetic sentinel",
    )
    beads = canonical.entries[1:]
    require({entry.code for entry in beads} == expected_mard_221_codes(), "loader changed the MARD code set")
    require(
        all(entry.name == entry.symbol == entry.code and not entry.synthetic for entry in beads),
        "loaded MARD entries do not expose their purchase code consistently",
    )
    changed = module.load_palette_bundle(
        "mard-221-compatible",
        representatives,
        np.asarray((241, 242, 243), dtype=np.float64),
        colors=6,
        seed=0,
    )
    require(changed.entries[1:] == beads, "changing source background mutated bead colors")
    require(changed.entries[0].rgb == (241, 242, 243), "synthetic background ignored source border RGB")

    profile = canonical.profile
    require(isinstance(profile, dict), "MARD loader omitted palette_profile")
    required_profile = {
        "id",
        "code_system",
        "reference_type",
        "source_urls",
        "bead_color_count",
        "background_strategy",
        "matching_method",
        "license_spdx",
        "copyright",
        "source_commit",
        "license_url",
        "notice_file",
        "trademark_disclaimer",
    }
    require(required_profile <= set(profile), "MARD palette_profile is missing required provenance fields")
    require(profile["id"] == "mard-221-compatible", "MARD profile id is incorrect")
    require(profile["code_system"] == "mard-221", "MARD profile code_system is incorrect")
    require(profile["bead_color_count"] == 221, "MARD profile bead count is incorrect")
    require(profile["matching_method"] == "CIEDE2000", "MARD profile does not declare DeltaE 2000 matching")
    require(profile["license_spdx"] == "MIT", "MARD profile lost its license identifier")
    require(profile["copyright"] == "Copyright (c) 2026 Jett-Wu", "MARD profile lost upstream copyright")
    require(
        isinstance(profile["source_urls"], list)
        and len(profile["source_urls"]) >= 2
        and all(str(url).startswith("https://") for url in profile["source_urls"]),
        "MARD profile source URLs are missing or invalid",
    )

    parser_default = module.build_parser().parse_args(
        ["restore", "unused-source.png", "--out", "unused-output"]
    )
    require(parser_default.palette == "auto", "adding MARD changed the legacy default palette")
    warm = module.load_palette_bundle(
        "warm-mascot", representatives, background, colors=6, seed=0
    )
    require(warm.entries == module.WARM_MASCOT_PALETTE and warm.profile is None, "warm-mascot compatibility changed")

    with tempfile.TemporaryDirectory(prefix="restore-bead-custom-palette-") as temporary:
        path = Path(temporary) / "legacy.json"
        path.write_text(
            json.dumps(
                [
                    {"name": "background", "symbol": ".", "rgb": "#FFFFFF", "role": "background"},
                    {"name": "ink", "symbol": "I", "rgb": [1, 2, 3], "role": "accent"},
                ]
            ),
            encoding="utf-8",
        )
        legacy = module.load_palette_bundle(str(path), representatives, background, colors=6, seed=0)
    require(legacy.profile is None, "legacy custom palette unexpectedly gained a built-in profile")
    require(all(entry.code is None and not entry.synthetic for entry in legacy.entries), "legacy custom palette code defaults changed")


def test_input_limits_and_palette_text_safety(module: ModuleType) -> None:
    with tempfile.TemporaryDirectory(prefix="restore-bead-safety-limits-") as temporary:
        root = Path(temporary)
        valid_entries = [
            {"name": "background", "symbol": ".", "rgb": "#FFFFFF", "role": "background"},
            {"name": "ink", "symbol": "I", "rgb": [1, 2, 3], "role": "accent", "code": "I1"},
        ]
        palette_path = root / "palette.json"
        palette_path.write_text(json.dumps(valid_entries), encoding="utf-8")
        require(len(module.parse_custom_palette(str(palette_path))) == 2, "valid custom palette was rejected")

        unsafe_cases = (
            (1, "name", "=cmd", "spreadsheet formula prefix"),
            (1, "symbol", "+I", "spreadsheet formula prefix"),
            (1, "code", "@I1", "spreadsheet formula prefix"),
            (1, "role", "-accent", "spreadsheet formula prefix"),
            (1, "name", "bad\nname", "control characters"),
        )
        for index, field, value, expected in unsafe_cases:
            entries = [dict(item) for item in valid_entries]
            entries[index][field] = value
            palette_path.write_text(json.dumps(entries), encoding="utf-8")
            require_value_error(
                lambda path=palette_path: module.parse_custom_palette(str(path)),
                expected,
                f"unsafe palette {field}",
            )

        too_many = [valid_entries[0], *([valid_entries[1]] * module.MAX_PALETTE_ENTRIES)]
        palette_path.write_text(json.dumps(too_many), encoding="utf-8")
        require_value_error(
            lambda: module.parse_custom_palette(str(palette_path)),
            f"between 2 and {module.MAX_PALETTE_ENTRIES}",
            "oversized palette",
        )

        tiny_json = root / "tiny.json"
        tiny_json.write_text("{}", encoding="utf-8")
        require_value_error(
            lambda: module._read_json_file(tiny_json, 1, "tiny JSON"),
            "safety limit",
            "JSON byte limit",
        )

        require_value_error(
            lambda: module.validate_grid_dimensions(500, 201),
            f"{module.MAX_GRID_CELLS:,} cells",
            "grid cell limit",
        )
        require_value_error(
            lambda: module.validate_grid_dimensions(module.MAX_GRID_DIMENSION + 1, 1),
            f"{module.MAX_GRID_DIMENSION} cells",
            "grid axis limit",
        )

        oversized_pattern = root / "oversized-pattern.json"
        oversized_pattern.write_text(
            json.dumps({"grid": {"columns": module.MAX_GRID_DIMENSION + 1, "rows": 1}}),
            encoding="utf-8",
        )
        require_value_error(
            lambda: module.load_pattern(oversized_pattern),
            f"{module.MAX_GRID_DIMENSION} cells",
            "pattern grid limit",
        )

        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover
            raise SelfTestFailure("Pillow disappeared during safety tests") from exc
        image_path = root / "bounded.png"
        Image.new("RGB", (16, 16), "white").save(image_path)
        original_pixel_limit = module.MAX_SOURCE_PIXELS
        original_file_limit = module.MAX_SOURCE_FILE_BYTES
        try:
            module.MAX_SOURCE_PIXELS = 255
            require_value_error(
                lambda: module.load_source(image_path),
                "pixel safety limit",
                "source pixel limit",
            )
            module.MAX_SOURCE_PIXELS = original_pixel_limit
            module.MAX_SOURCE_FILE_BYTES = 1
            require_value_error(
                lambda: module.load_source(image_path),
                "byte safety limit",
                "source byte limit",
            )
        finally:
            module.MAX_SOURCE_PIXELS = original_pixel_limit
            module.MAX_SOURCE_FILE_BYTES = original_file_limit

        labels = module.np.zeros((100, 100), dtype=module.np.int16)
        render_palette = (
            module.PaletteEntry("background", ".", (255, 255, 255), "background"),
            module.PaletteEntry("ink", "I", (0, 0, 0), "accent"),
        )
        require_value_error(
            lambda: module.render_matrix(labels, render_palette, 50, grid=False),
            "total pixels",
            "render pixel-area limit",
        )


def test_output_target_safety(module: ModuleType) -> None:
    require(module.OUTPUT_MARKER_NAME == OUTPUT_MARKER_NAME, "production marker name drifted")
    require(module.OUTPUT_MARKER_CONTENT == OUTPUT_MARKER_CONTENT, "production marker content drifted")
    with tempfile.TemporaryDirectory(prefix="restore-bead-output-safety-") as temporary:
        root = Path(temporary)
        protected_parent = root / "protected"
        protected_parent.mkdir()
        protected_input = protected_parent / "source.png"
        protected_input.write_bytes(b"not-an-image")

        protected_roots = {
            Path(Path.cwd().anchor).resolve(),
            Path.home().resolve(),
            Path.cwd().resolve(),
            module.SKILL_ROOT.resolve(),
        }
        repository_root = module._find_repository_root(module.SKILL_ROOT)
        if repository_root is not None:
            protected_roots.add(repository_root.resolve())
        for protected_root in protected_roots:
            require_value_error(
                lambda target=protected_root: module._validate_output_target(
                    target, True, [protected_input]
                ),
                "refusing to use",
                f"protected output root {protected_root}",
            )
        require_value_error(
            lambda: module._validate_output_target(
                module.SKILL_ROOT.parent, True, [protected_input]
            ),
            "one of its ancestors",
            "skill-root ancestor output",
        )

        require_value_error(
            lambda: module._validate_output_target(
                protected_parent, True, [protected_input]
            ),
            "contains a protected input",
            "input-ancestor output",
        )

        unowned = root / "unowned"
        unowned.mkdir()
        sentinel = unowned / "sentinel.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        require_value_error(
            lambda: module._validate_output_target(unowned, True, []),
            "not owned by restore-bead-pattern",
            "unowned overwrite",
        )
        require(sentinel.read_text(encoding="utf-8") == "preserve", "unowned overwrite deleted data")

        invalid_marker = root / "invalid-marker"
        invalid_marker.mkdir()
        (invalid_marker / OUTPUT_MARKER_NAME).write_text("wrong\n", encoding="utf-8")
        require_value_error(
            lambda: module._validate_output_target(invalid_marker, True, []),
            "not owned by restore-bead-pattern",
            "invalid output marker",
        )

        owned = root / "owned"
        owned.mkdir()
        (owned / OUTPUT_MARKER_NAME).write_text(OUTPUT_MARKER_CONTENT, encoding="utf-8")
        (owned / "stale.txt").write_text("stale", encoding="utf-8")
        require(
            module._validate_output_target(owned, True, []) == owned.resolve(),
            "valid owned output was rejected",
        )

        actual = root / "actual-target"
        actual.mkdir()
        link = root / "linked-target"
        try:
            link.symlink_to(actual, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - restricted Windows hosts
            pass
        else:
            require_value_error(
                lambda: module._validate_output_target(link, True, []),
                "symlink",
                "output symlink",
            )

        raced_target = root / "raced-target"
        staging = module.begin_staging(raced_target, True, [protected_input])
        raced_target.mkdir()
        raced_sentinel = raced_target / "sentinel.txt"
        raced_sentinel.write_text("preserve", encoding="utf-8")
        require_value_error(
            lambda: module.commit_staging(staging, raced_target, True, [protected_input]),
            "not owned by restore-bead-pattern",
            "pre-commit ownership recheck",
        )
        require(
            raced_sentinel.read_text(encoding="utf-8") == "preserve",
            "pre-commit recheck deleted an unowned directory",
        )


def test_lab_and_delta_e_references(module: ModuleType) -> None:
    np = module.np
    converted = module.srgb_to_lab(
        np.asarray(((255, 255, 255), (0, 0, 0), (255, 0, 0)), dtype=np.float64)
    )
    expected_lab = np.asarray(
        ((100.0, 0.0, 0.0), (0.0, 0.0, 0.0), (53.2408, 80.0925, 67.2032)),
        dtype=np.float64,
    )
    require(np.allclose(converted, expected_lab, atol=0.002), "sRGB-to-Lab D65 conversion is inaccurate")

    first = np.asarray(
        (
            (50.0, 2.6772, -79.7751),
            (50.0, 3.1571, -77.2803),
            (50.0, 2.8361, -74.0200),
            (50.0, -1.3802, -84.2814),
        ),
        dtype=np.float64,
    )
    second = np.asarray(((50.0, 0.0, -82.7485),) * 4, dtype=np.float64)
    expected_delta = np.asarray((2.0425, 2.8615, 3.4412, 1.0000), dtype=np.float64)
    forward = module.delta_e_2000(first, second)
    reverse = module.delta_e_2000(second, first)
    require(np.allclose(forward, expected_delta, atol=0.0001), "CIEDE2000 disagrees with Sharma reference pairs")
    require(np.allclose(reverse, forward, atol=1e-12), "CIEDE2000 is not symmetric")
    require(np.allclose(module.delta_e_2000(first, first), 0.0, atol=1e-12), "CIEDE2000 self-distance is nonzero")


def test_mard_cluster_matching_and_background_isolation(module: ModuleType) -> None:
    """White background and white/warm-white beads must remain distinct."""

    np = module.np
    background_rgb = np.asarray((255, 255, 255), dtype=np.float64)
    bundle = module.load_palette_bundle(
        "mard-221-compatible", np.zeros((1, 1, 3)), background_rgb, colors=6, seed=0
    )
    structure_palette = (
        module.PaletteEntry("source-background", ".", (255, 255, 255), "background"),
        module.PaletteEntry("warm-white", "W", (245, 236, 210), "fill"),
        module.PaletteEntry("outline", "K", (0, 0, 0), "outline"),
        module.PaletteEntry("white-bead", "X", (255, 255, 255), "accent"),
    )
    structure = np.zeros((9, 9), dtype=np.int16)
    structure[2, 2:7] = 2
    structure[6, 2:7] = 2
    structure[2:7, 2] = 2
    structure[2:7, 6] = 2
    structure[3:6, 3:6] = 1
    structure[4, 4] = 3
    labels, raw_labels, alternatives, confidence, mapping, diagnostics = module.quantize_catalog_clusters(
        structure_palette, structure, structure.copy(), bundle.entries, background_rgb
    )

    code_at = lambda row, col: bundle.entries[int(labels[row, col])].code
    require(bundle.entries[int(labels[0, 0])].synthetic, "exterior white did not remain synthetic background")
    require(code_at(3, 3) == "H13", "exact MARD warm white did not match H13")
    require(code_at(4, 4) == "H2", "enclosed source-white bead did not match the nearest MARD white H2")
    require(code_at(2, 3) == "H7", "exact MARD black did not match H7")
    require(int((labels == 0).sum()) == 56, "catalog matching changed the exterior-background area")
    require(int((labels == mapping[1]).sum()) == 8, "catalog matching changed warm-white bead count")
    require(int((labels == mapping[2]).sum()) == 16, "catalog matching changed outline bead count")
    require(int((labels == mapping[3]).sum()) == 1, "catalog matching changed pure-white bead count")
    require(np.array_equal(labels, raw_labels), "raw/final catalog labels diverged without topology edits")
    require(labels.shape == alternatives.shape == confidence.shape, "catalog matching changed matrix shape")
    require(diagnostics.get("catalog_color_count") == 221, "catalog diagnostics report the wrong color count")
    require(diagnostics.get("method", "").startswith("CIEDE2000"), "catalog diagnostics omit CIEDE2000")

    # The same pure-white sample, when initially classified as background but
    # enclosed by a four-connected outline, must become the warm fill cluster
    # before the MARD card is applied.  This is the reported white-ear bug.
    raw_with_hole = structure.copy()
    raw_with_hole[4, 4] = 0
    topology_confidence = np.ones(raw_with_hole.shape, dtype=np.float32)
    normalized, _, _, topology, _ = module.normalize_light_topology(
        raw_with_hole, topology_confidence, structure_palette, 0.62, True
    )
    require(topology.get("applied") is True, "four-connected light topology was not applied")
    require(int(normalized[4, 4]) == 1, "enclosed white cell was not retained as warm foreground")
    normalized_labels, _, _, _, _, _ = module.quantize_catalog_clusters(
        structure_palette, normalized, raw_with_hole, bundle.entries, background_rgb
    )
    require(
        bundle.entries[int(normalized_labels[4, 4])].code == "H13",
        "enclosed white cell became empty/pure white instead of the warm-white cluster",
    )
    require(bundle.entries[int(normalized_labels[0, 0])].code is None, "edge background became a purchasable bead")


def normalize_topology_result(module: ModuleType, labels: Any) -> Any:
    """Call the production light-region resolver and return its final matrix."""

    normalizer = getattr(module, "normalize_light_topology", None)
    if callable(normalizer):
        confidence = module.np.ones(labels.shape, dtype=module.np.float32)
        result = normalizer(labels.copy(), confidence, module.WARM_MASCOT_PALETTE, 0.62, True)
        require(
            isinstance(result, tuple) and result and isinstance(result[0], module.np.ndarray),
            "normalize_light_topology returned an unexpected result",
        )
        return result[0]

    resolver = getattr(module, "resolve_light_regions", None)
    require(
        callable(resolver),
        "restore_pattern.py must expose normalize_light_topology(...) or resolve_light_regions(labels)",
    )
    working = labels.copy()
    result = resolver(working)
    # The resolver may mutate in place and return diagnostics, or return a
    # matrix (possibly followed by diagnostics).  Accommodate both contracts.
    if isinstance(result, module.np.ndarray):
        return result
    if isinstance(result, tuple) and result and isinstance(result[0], module.np.ndarray):
        return result[0]
    return working


def test_four_connected_light_topology(module: ModuleType) -> None:
    np = module.np
    background, fill, barrier = 0, 1, 2

    labels = np.full((8, 8), background, dtype=np.uint8)
    # A large light patch connected to the canvas edge is exterior, regardless
    # of its size, and must be cleared to background.
    labels[0:2, 0:4] = fill
    # A closed barrier contains fill plus a one-cell background-colored hole.
    labels[2, 2:7] = barrier
    labels[6, 2:7] = barrier
    labels[2:7, 2] = barrier
    labels[2:7, 6] = barrier
    labels[3:6, 3:6] = fill
    labels[4, 4] = background

    cleaned = normalize_topology_result(module, labels)
    require(cleaned.shape == labels.shape, "topology cleanup changed matrix dimensions")
    require(bool(np.all(cleaned[0:2, 0:4] == background)), "edge-connected light cells were not cleared")
    require(int(cleaned[4, 4]) == fill, "one-cell enclosed light hole was not preserved as fill")
    require(
        np.array_equal(normalize_topology_result(module, cleaned), cleaned),
        "light-region cleanup is not idempotent",
    )

    # The center is connected to the exterior only diagonally.  Four-neighbor
    # flooding must leave it enclosed; an accidental eight-neighbor flood would
    # incorrectly clear it.
    diagonal = np.full((4, 4), barrier, dtype=np.uint8)
    diagonal[0, 0] = background
    diagonal[1:3, 1:3] = background
    diagonal_cleaned = normalize_topology_result(module, diagonal)
    require(int(diagonal_cleaned[0, 0]) == background, "edge background was not kept exterior")
    require(bool(np.all(diagonal_cleaned[1:3, 1:3] == fill)), "diagonal contact incorrectly leaked through the barrier")


def test_auto_palette_keeps_small_chroma_clusters(module: ModuleType) -> None:
    """Warm fill shadows must not consume the much smaller pink cluster.

    The fixture has seven visible RGB groups but asks for six logical colors.
    Warm shadow cells outnumber pink two to one.  Raw-RGB k-means tends to spend
    its sixth center on the shadow and merge pink into it; luminance/chroma
    features should instead keep all three semantic accents.
    """

    np = module.np
    targets = {
        "background": np.asarray((255, 255, 255), dtype=np.float64),
        "fill": np.asarray((248, 247, 228), dtype=np.float64),
        "outline": np.asarray((12, 11, 6), dtype=np.float64),
        "cyan": np.asarray((138, 208, 214), dtype=np.float64),
        "red": np.asarray((197, 62, 64), dtype=np.float64),
        "pink": np.asarray((243, 191, 165), dtype=np.float64),
    }
    warm_shadow = np.asarray((235, 217, 190), dtype=np.float64)
    values = np.concatenate(
        (
            np.tile(targets["background"], (40, 1)),
            np.tile(targets["fill"], (40, 1)),
            np.tile(warm_shadow, (8, 1)),
            np.tile(targets["outline"], (12, 1)),
            np.tile(targets["cyan"], (10, 1)),
            np.tile(targets["red"], (10, 1)),
            np.tile(targets["pink"], (4, 1)),
        ),
        axis=0,
    ).reshape(4, 31, 3)

    def distance(first: Any, second: Any) -> float:
        return float(np.linalg.norm(np.asarray(first, dtype=np.float64) - np.asarray(second, dtype=np.float64)))

    # Multiple seeds ensure this property is not an accident of one k-means++
    # initialization.  Accent names/order are deliberately ignored.
    for seed in (0, 2, 7, 19):
        palette = module.auto_palette(values, targets["background"], colors=6, seed=seed)
        by_role: dict[str, list[Any]] = {}
        for entry in palette:
            by_role.setdefault(entry.role, []).append(entry)
        require(len(palette) == 6, f"auto palette returned the wrong size for seed {seed}")
        require(len(by_role.get("background", [])) == 1, "auto palette lacks a unique background")
        require(len(by_role.get("fill", [])) == 1, "auto palette lacks a unique fill")
        require(len(by_role.get("outline", [])) == 1, "auto palette lacks a unique outline")
        require(len(by_role.get("accent", [])) == 3, "warm shadow displaced one of the three accents")

        for role in ("background", "fill", "outline"):
            require(
                distance(by_role[role][0].rgb, targets[role]) <= 6.0,
                f"auto palette {role} drifted into a different light/color cluster",
            )
        accents = [entry.rgb for entry in by_role["accent"]]
        for color_name in ("cyan", "red", "pink"):
            require(
                min(distance(rgb, targets[color_name]) for rgb in accents) <= 8.0,
                f"auto palette lost the low-frequency {color_name} accent",
            )
        require(
            min(distance(rgb, warm_shadow) for rgb in accents) >= 28.0,
            "auto palette incorrectly promoted a warm fill shadow to an accent",
        )


def test_wenzhou_mold_geometry(module: ModuleType) -> None:
    """A mold is a phase-aware padding layer, never a new sampling grid."""

    np = module.np
    pitch = 1440.0 / 52.0
    labels = np.zeros((45, 37), dtype=np.int32)
    content = np.arange(1, 35 * 43 + 1, dtype=np.int32).reshape(43, 35)
    labels[1:44, 1:36] = content
    confidence = np.ones(labels.shape, dtype=np.float32)
    content_confidence = np.linspace(0.2, 0.98, content.size, dtype=np.float32).reshape(content.shape)
    confidence[1:44, 1:36] = content_confidence
    labels_before = labels.copy()
    confidence_before = confidence.copy()

    # Choose an origin whose lattice phase places the 35x43 content with the
    # same asymmetric margins seen in the source: 9/8 horizontally and 4/5
    # vertically.  This catches implementations that simply center a bitmap.
    target_origin_x = (1902.0 - 52.0 * pitch) / 2.0
    target_origin_y = (1440.0 - 52.0 * pitch) / 2.0
    spec = module.GridSpec(
        columns=37,
        rows=45,
        pitch=pitch,
        origin_x=target_origin_x + 8.0 * pitch,
        origin_y=target_origin_y + 3.0 * pitch,
    )
    result = module.place_on_wenzhou_mold(
        labels,
        confidence,
        {"left": 1, "top": 1, "right_exclusive": 36, "bottom_exclusive": 44},
        spec,
        1902,
        1440,
        mode="52x52",
        background_label=0,
    )
    metadata = result.metadata
    placement = metadata["placement"]
    require(metadata["board_size"] == 52, "explicit 52x52 mold did not select 52")
    require(metadata["selection_status"] == "explicit", "explicit mold lost its provenance")
    require(metadata["resampled"] is False, "mold metadata claims native cells were resampled")
    require(
        (placement["col_offset"], placement["row_offset"]) == (9, 4),
        "52x52 placement ignored the recovered lattice phase",
    )
    require(np.array_equal(labels, labels_before), "mold placement mutated the native label matrix")
    require(np.array_equal(confidence, confidence_before), "mold placement mutated native confidence")
    expected = np.zeros((52, 52), dtype=labels.dtype)
    expected[4:47, 9:44] = content
    require(np.array_equal(result.board_labels, expected), "52x52 board did not copy content cell-for-cell")
    require(
        np.array_equal(result.board_confidence[4:47, 9:44], content_confidence),
        "52x52 board changed native per-cell confidence",
    )
    require(
        int(np.count_nonzero(result.board_labels)) == int(np.count_nonzero(content)),
        "52x52 board split, dropped, or duplicated native cells",
    )


def test_wenzhou_mold_selection(module: ModuleType) -> None:
    """Exercise capacity boundaries and visual-scale evidence independently."""

    np = module.np

    def place(
        rows: int,
        columns: int,
        *,
        pitch: float,
        image_side: float,
        mode: str = "auto",
    ) -> Any:
        labels = np.ones((rows, columns), dtype=np.int16)
        confidence = np.full(labels.shape, 0.9, dtype=np.float32)
        spec = module.GridSpec(columns, rows, pitch, 0.0, 0.0)
        return module.place_on_wenzhou_mold(
            labels,
            confidence,
            {"left": 0, "top": 0, "right_exclusive": columns, "bottom_exclusive": rows},
            spec,
            image_side,
            image_side,
            mode=mode,
            background_label=0,
        )

    strong_52 = place(10, 10, pitch=20.0, image_side=1040.0)
    require(strong_52.metadata["board_size"] == 52, "strong 52-cell scale evidence was ignored")
    require(strong_52.metadata["selection_status"] == "detected", "strong 52 evidence was not detected")

    # Small content can physically fit either mold.  Strong 78-scale evidence
    # must still override the smallest-compatible recommendation.
    strong_78 = place(10, 10, pitch=20.0, image_side=1560.0)
    require(strong_78.metadata["board_size"] == 78, "strong 78-cell scale evidence was ignored")
    require(strong_78.metadata["selection_status"] == "detected", "strong 78 evidence was not detected")

    ambiguous = place(43, 35, pitch=20.0, image_side=1200.0)
    require(ambiguous.metadata["board_size"] == 52, "ambiguous small content did not prefer 52")
    require(
        ambiguous.metadata["selection_status"] == "review",
        "a smallest-compatible recommendation was presented as a detection",
    )
    require(
        float(ambiguous.metadata["selection_confidence"]) < 0.5,
        "ambiguous mold recommendation has misleadingly high confidence",
    )

    capacity_78 = place(60, 53, pitch=10.0, image_side=1000.0)
    require(capacity_78.metadata["board_size"] == 78, "53x60 native content did not select 78")
    require(capacity_78.metadata["resampled"] is False, "78 capacity selection resampled content")
    require(
        np.array_equal(capacity_78.board_labels[:60, :53], np.ones((60, 53), dtype=np.int16)),
        "78 board did not preserve the full 53x60 content",
    )

    try:
        place(20, 79, pitch=10.0, image_side=1000.0)
    except module.MoldCapacityError:
        pass
    else:
        raise SelfTestFailure("content wider than 78 did not raise MoldCapacityError")

    try:
        place(60, 53, pitch=10.0, image_side=1000.0, mode="52x52")
    except module.MoldCapacityError:
        pass
    else:
        raise SelfTestFailure("explicit 52x52 silently cropped or resampled 53x60 content")

    parsed_default = module.build_parser().parse_args(
        ["restore", "unused-source.png", "--out", "unused-output"]
    )
    require(parsed_default.board_size == "none", "--board-size default is not none")


def _shift(rgb: tuple[int, int, int], amount: int) -> tuple[int, int, int]:
    return tuple(max(0, min(255, channel + amount)) for channel in rgb)


def make_synthetic_source(path: Path) -> list[list[str]]:
    """Render a small logical pattern with deterministic fuzzy yarn texture."""

    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError as exc:  # pragma: no cover - same dependency as target
        raise SelfTestFailure("self_test.py requires Pillow, as does restore_pattern.py") from exc

    palette = {
        ".": (255, 255, 255),
        "W": (248, 247, 228),
        "K": (12, 11, 6),
        "P": (243, 191, 165),
        "C": (138, 208, 214),
        "R": (197, 62, 64),
    }
    matrix = [["." for _ in range(GRID_COLUMNS)] for _ in range(GRID_ROWS)]

    # This ivory patch deliberately touches the canvas edge.  It is raw fill
    # evidence but must be classified as exterior background after cleanup.
    for row, col in ((0, 0), (0, 1), (1, 0), (1, 1)):
        matrix[row][col] = "W"

    # Six-by-six closed subject.  The pure-white center is the regression case
    # for the former "small light component" deletion bug.
    # Leave the four bbox corners as background.  In a square grid the stepped
    # outline remains closed under four-neighbor topology, and the transparent
    # crop consequently contains both clear and opaque pixels.
    for index in range(3, 7):
        matrix[2][index] = "K"
        matrix[7][index] = "K"
        matrix[index][2] = "K"
        matrix[index][7] = "K"
    for row in range(3, 7):
        for col in range(3, 7):
            matrix[row][col] = "W"
    matrix[4][4] = "."
    matrix[4][5] = "P"
    matrix[5][4] = "C"
    matrix[5][5] = "R"

    width, height = GRID_COLUMNS * CELL_PIXELS, GRID_ROWS * CELL_PIXELS
    image = Image.new("RGB", (width, height), palette["."])
    draw = ImageDraw.Draw(image)
    rng = random.Random(20260815)
    for row in range(GRID_ROWS):
        for col in range(GRID_COLUMNS):
            symbol = matrix[row][col]
            base = palette[symbol]
            x0, y0 = col * CELL_PIXELS, row * CELL_PIXELS
            draw.rectangle((x0, y0, x0 + CELL_PIXELS - 1, y0 + CELL_PIXELS - 1), fill=base)
            if symbol == ".":
                continue
            # Short, low-contrast fibers stay within their logical cell.  They
            # exercise representative-color sampling without obscuring the
            # deliberately crisp lattice.
            for fiber in range(9):
                start_x = x0 + rng.randint(2, CELL_PIXELS - 5)
                start_y = y0 + 2 + fiber * 2
                end_x = min(x0 + CELL_PIXELS - 2, start_x + rng.randint(2, 6))
                end_y = min(y0 + CELL_PIXELS - 2, start_y + rng.choice((-1, 0, 1)))
                amount = rng.choice((-13, -8, 8, 13))
                draw.line((start_x, start_y, end_x, end_y), fill=_shift(base, amount), width=2)

    # A sub-pixel-sized softening resembles photographed fibers but does not
    # alter the intended grid or introduce a stored fixture.
    image.filter(ImageFilter.GaussianBlur(radius=0.28)).save(path, format="PNG")
    return matrix


def make_ordinary_design_source(path: Path) -> None:
    """Create a deterministic illustration with no pre-existing logical grid."""

    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:  # pragma: no cover - same dependency as target
        raise SelfTestFailure("design self-test requires Pillow") from exc
    image = Image.new("RGB", (120, 160), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((25, 18, 95, 88), fill=(244, 190, 45), outline=(20, 20, 20), width=5)
    draw.rectangle((42, 82, 78, 143), fill=(65, 155, 205), outline=(20, 20, 20), width=5)
    draw.ellipse((42, 46, 52, 56), fill=(10, 10, 10))
    draw.ellipse((68, 46, 78, 56), fill=(10, 10, 10))
    image.save(path, format="PNG")


def make_flat_rgba_design_source(path: Path) -> None:
    """Create a two-color alpha image whose hidden transparent RGB is black."""

    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:  # pragma: no cover
        raise SelfTestFailure("design alpha self-test requires Pillow") from exc
    image = Image.new("RGBA", (78, 78), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 15, 58, 63), fill=(235, 48, 70, 255))
    image.save(path, format="PNG")


def cli_command(
    source: Path,
    output_dir: Path,
    board_size: str | None = None,
    *,
    palette: str = "warm-mascot",
) -> list[str]:
    command = [
        sys.executable,
        str(RESTORE_SCRIPT),
        "restore",
        str(source),
        "--out",
        str(output_dir),
        "--grid",
        f"{GRID_COLUMNS}x{GRID_ROWS}",
        "--cell-size",
        str(CELL_PIXELS),
        "--origin",
        "0,0",
        "--palette",
        palette,
        "--uncertain-threshold",
        "0.62",
        "--seed",
        "0",
        "--render-cell-px",
        "12",
    ]
    if board_size is not None:
        command.extend(("--board-size", board_size))
    return command


def read_csv_matrix(path: Path, rows: int, columns: int) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        records = list(csv.reader(handle))
    require(bool(records), "pattern.csv is empty")
    header, data = records[0], records[1:]
    # Accept either `c00,...` or `row,c00,...`, but require unambiguous shape.
    has_row_ids = len(header) == columns + 1
    require(len(header) == columns + int(has_row_ids), "pattern.csv column count disagrees with JSON grid")
    require(len(data) == rows, "pattern.csv row count disagrees with JSON grid")
    matrix: list[list[str]] = []
    for row_index, record in enumerate(data):
        if has_row_ids:
            require(bool(record), f"pattern.csv row {row_index} is empty")
            record = record[1:]
        require(len(record) == columns, f"pattern.csv row {row_index} has the wrong width")
        matrix.append(record)
    return matrix


def four_components(matrix: list[list[str]], selected: set[str]) -> list[tuple[int, bool]]:
    rows, columns = len(matrix), len(matrix[0])
    seen: set[tuple[int, int]] = set()
    result: list[tuple[int, bool]] = []
    for row in range(rows):
        for col in range(columns):
            if matrix[row][col] not in selected or (row, col) in seen:
                continue
            queue = deque([(row, col)])
            seen.add((row, col))
            size, touches_edge = 0, False
            while queue:
                current_row, current_col = queue.popleft()
                size += 1
                touches_edge |= current_row in (0, rows - 1) or current_col in (0, columns - 1)
                for delta_row, delta_col in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    neighbor = current_row + delta_row, current_col + delta_col
                    if (
                        0 <= neighbor[0] < rows
                        and 0 <= neighbor[1] < columns
                        and neighbor not in seen
                        and matrix[neighbor[0]][neighbor[1]] in selected
                    ):
                        seen.add(neighbor)
                        queue.append(neighbor)
            result.append((size, touches_edge))
    return sorted(result, reverse=True)


def cell_matrix_from_json(payload: dict[str, Any], rows: int, columns: int) -> list[list[str]]:
    cells = payload.get("cells")
    require(isinstance(cells, list), "pattern.json cells must be a list")
    require(len(cells) == rows * columns, "pattern.json must contain exactly one cell per grid coordinate")
    matrix = [["" for _ in range(columns)] for _ in range(rows)]
    seen: set[tuple[int, int]] = set()
    for cell in cells:
        require(isinstance(cell, dict), "each pattern.json cell must be an object")
        row, col = cell.get("row"), cell.get("col")
        require(isinstance(row, int) and isinstance(col, int), "cell coordinates must be integers")
        require(0 <= row < rows and 0 <= col < columns, "cell coordinate is outside the grid")
        require((row, col) not in seen, "pattern.json contains duplicate cell coordinates")
        seen.add((row, col))
        symbol = cell.get("symbol")
        require(isinstance(symbol, str) and symbol, "each cell must have a symbol")
        confidence = cell.get("confidence")
        require(isinstance(confidence, (int, float)) and 0.0 <= confidence <= 1.0, "cell confidence is outside [0,1]")
        matrix[row][col] = symbol
    return matrix


def write_integer_scale_fixture(path: Path, template: dict[str, Any]) -> dict[str, Any]:
    """Write a valid 31x38-content pattern without depending on a source photo.

    The one-cell exterior frame makes the source/native distinction observable:
    scaling must crop ``content_bbox`` first, duplicate only subject cells, and
    must not turn photographed background or shading into new palette labels.
    """

    columns, rows = 33, 40
    bbox = {"left": 1, "top": 1, "right_exclusive": 32, "bottom_exclusive": 39}
    palette = json.loads(json.dumps(template["palette"]))
    backgrounds = [entry for entry in palette if entry.get("role") == "background"]
    require(len(backgrounds) == 1, "integer-scale fixture needs one background entry")
    background = backgrounds[0]
    foreground = [entry for entry in palette if entry.get("role") != "background"]
    require(len(foreground) >= 3, "integer-scale fixture needs at least three foreground colors")

    counts = {str(entry["name"]): 0 for entry in palette}
    cells: list[dict[str, Any]] = []
    for row in range(rows):
        for col in range(columns):
            inside = (
                bbox["top"] <= row < bbox["bottom_exclusive"]
                and bbox["left"] <= col < bbox["right_exclusive"]
            )
            if inside:
                local_row = row - bbox["top"]
                local_col = col - bbox["left"]
                entry_index = (local_row * 7 + local_col * 3) % len(foreground)
                entry = foreground[entry_index]
                alternative = foreground[(entry_index + 1) % len(foreground)]
                confidence = round(0.78 + ((local_row + local_col) % 9) * 0.02, 6)
                agreement = round(0.82 + ((local_row * 2 + local_col) % 7) * 0.02, 6)
            else:
                entry = background
                alternative = background
                confidence = 1.0
                agreement = 1.0
            counts[str(entry["name"])] += 1
            cell = {
                "row": row,
                "col": col,
                "label": entry["name"],
                "symbol": entry["symbol"],
                "raw_label": entry["name"],
                "alternative": alternative["name"],
                "confidence": confidence,
                "agreement": agreement,
                "reason": None,
            }
            if "code" in entry:
                cell["code"] = entry.get("code")
                cell["raw_code"] = entry.get("code")
                cell["alternative_code"] = alternative.get("code")
            cells.append(cell)

    bead_count = 31 * 38
    require(
        sum(value for name, value in counts.items() if name != background["name"]) == bead_count,
        "integer-scale fixture has the wrong foreground area",
    )
    payload = {
        "schema_version": template.get("schema_version", "1.2"),
        "algorithm_version": template.get("algorithm_version", "0.4.1"),
        "status": "pass",
        "source": {
            "sha256": "ab" * 32,
            "width_px": columns * 10,
            "height_px": rows * 10,
        },
        "grid": {
            "columns": columns,
            "rows": rows,
            "pitch_px": 10.0,
            "origin_x_px": 0.0,
            "origin_y_px": 0.0,
            "method": "integer-scale-self-test-fixture",
            "confidence": 1.0,
            "candidates": [],
            "diagnostics": {},
        },
        "content_bbox": bbox,
        "palette": palette,
        "counts": counts,
        "bead_count": bead_count,
        "uncertain_threshold": 0.62,
        "uncertain_cells": [],
        "postprocess": {"mode": "fixture", "applied": False, "ensemble_candidates": 1},
        "warnings": [],
        "quality": {
            "grid_confidence": 1.0,
            "candidate_margin": 1.0,
            "mean_foreground_cell_confidence": 0.86,
            "review_cells": 0,
            "review_ratio": 0.0,
        },
        "cells": cells,
        "artifacts": {},
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def assert_cli_contract(source: Path, output_dir: Path, completed: subprocess.CompletedProcess[str]) -> None:
    assert_output_marker(output_dir)
    required = {
        "pattern.json",
        "summary.json",
        "canvas.csv",
        "matrix.csv",
        "palette.csv",
        "review.csv",
        "pattern_preview.png",
        "pattern_grid.png",
        "pattern_review.png",
        "pattern_transparent.png",
        "canvas_grid.png",
        "source_grid_overlay.png",
        "candidates.png",
    }
    missing = sorted(name for name in required if not (output_dir / name).is_file())
    require(not missing, f"restore CLI omitted required outputs: {', '.join(missing)}")

    report_path = output_dir / "pattern.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    require(isinstance(payload, dict), "pattern.json root must be an object")
    require(str(payload.get("schema_version", "")).startswith("1."), "pattern.json has no compatible schema_version")
    require(isinstance(payload.get("algorithm_version"), str), "pattern.json has no algorithm_version")
    source_metadata = payload.get("source")
    require(isinstance(source_metadata, dict), "pattern.json source must be an object")
    require(set(source_metadata) == {"sha256", "width_px", "height_px"}, "source metadata must not contain a path")
    require(
        source_metadata["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest(),
        "pattern.json source hash is incorrect",
    )
    require(
        (source_metadata["width_px"], source_metadata["height_px"])
        == (GRID_COLUMNS * CELL_PIXELS, GRID_ROWS * CELL_PIXELS),
        "pattern.json source dimensions are incorrect",
    )
    grid = payload.get("grid")
    require(isinstance(grid, dict), "pattern.json grid must be an object")
    rows, columns = grid.get("rows"), grid.get("columns")
    require((columns, rows) == (GRID_COLUMNS, GRID_ROWS), "CLI did not honor the explicit 10x10 grid")

    palette = payload.get("palette")
    require(isinstance(palette, list) and len(palette) >= 3, "pattern.json palette is missing or too small")
    symbol_to_role: dict[str, str] = {}
    names: set[str] = set()
    for entry in palette:
        require(isinstance(entry, dict), "palette entries must be objects")
        name, symbol, role = entry.get("name"), entry.get("symbol"), entry.get("role")
        require(isinstance(name, str) and name not in names, "palette names must be unique")
        require(isinstance(symbol, str) and symbol not in symbol_to_role, "palette symbols must be unique")
        require(isinstance(role, str), "palette entry has no role")
        names.add(name)
        symbol_to_role[symbol] = role
    background_symbols = {symbol for symbol, role in symbol_to_role.items() if role == "background"}
    fill_symbols = {symbol for symbol, role in symbol_to_role.items() if role == "fill"}
    outline_symbols = {symbol for symbol, role in symbol_to_role.items() if role == "outline"}
    require(len(background_symbols) == len(fill_symbols) == len(outline_symbols) == 1, "palette roles are ambiguous")
    background_symbol = next(iter(background_symbols))
    fill_symbol = next(iter(fill_symbols))
    outline_symbol = next(iter(outline_symbols))
    background_name = next(entry["name"] for entry in palette if entry["symbol"] == background_symbol)

    json_matrix = cell_matrix_from_json(payload, rows, columns)
    csv_matrix = read_csv_matrix(output_dir / "canvas.csv", rows, columns)
    require(json_matrix == csv_matrix, "canvas.csv does not match pattern.json cell-for-cell")
    require(all(symbol in symbol_to_role for row in json_matrix for symbol in row), "matrix uses an unknown palette symbol")

    counts = payload.get("counts")
    require(isinstance(counts, dict), "pattern.json counts must be an object")
    require(sum(int(value) for value in counts.values()) == rows * columns, "palette counts do not sum to grid area")
    counted_symbols = {symbol: sum(cell == symbol for row in json_matrix for cell in row) for symbol in symbol_to_role}
    name_to_symbol = {entry["name"]: entry["symbol"] for entry in palette}
    for name, reported in counts.items():
        require(name in name_to_symbol, f"counts contains unknown palette name {name!r}")
        require(int(reported) == counted_symbols[name_to_symbol[name]], f"count for {name!r} disagrees with cells")

    threshold = payload.get("uncertain_threshold")
    require(isinstance(threshold, (int, float)) and 0.0 <= threshold <= 1.0, "invalid uncertain_threshold")
    expected_uncertain = sorted(
        (cell["row"], cell["col"])
        for cell in payload["cells"]
        if cell["label"] != background_name and float(cell["confidence"]) < float(threshold)
    )
    reported_uncertain = sorted(
        (cell["row"], cell["col"]) for cell in payload.get("uncertain_cells", [])
    )
    require(reported_uncertain == expected_uncertain, "uncertain_cells disagrees with cell confidence values")
    with (output_dir / "review.csv").open(encoding="utf-8", newline="") as handle:
        review_rows = list(csv.DictReader(handle))
    require(len(review_rows) == len(expected_uncertain), "review.csv does not contain exactly the uncertain beads")

    # End-to-end topology regression: exterior raw ivory becomes background,
    # while the enclosed pure-white center becomes subject fill.
    require(json_matrix[0][0] == background_symbol, "edge-connected ivory survived as subject fill")
    require(json_matrix[1][1] == background_symbol, "edge-connected ivory patch was not fully cleared")
    require(json_matrix[4][4] == fill_symbol, "enclosed white cell was not restored to fill")
    require(json_matrix[2][4] == outline_symbol, "closed outline was not recovered")
    require(json_matrix[4][5] != background_symbol, "accent color disappeared from the enclosed subject")

    bbox = payload.get("content_bbox")
    require(
        bbox == {"left": 2, "top": 2, "right_exclusive": 8, "bottom_exclusive": 8},
        "content_bbox does not tightly enclose the recovered subject",
    )
    cropped_csv = read_csv_matrix(output_dir / "matrix.csv", 6, 6)
    require(cropped_csv == [row[2:8] for row in json_matrix[2:8]], "matrix.csv is not the bbox crop of canvas.csv")

    foreground = set(symbol_to_role) - {background_symbol}
    require(len(four_components(json_matrix, foreground)) == 1, "restored subject is not one 4-connected component")
    background_components = four_components(json_matrix, {background_symbol})
    require(
        len(background_components) == 1 and background_components[0][1],
        "restored matrix contains an enclosed background hole",
    )

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise SelfTestFailure("Pillow disappeared after CLI execution") from exc
    for name in ("pattern_preview.png", "pattern_grid.png", "pattern_review.png"):
        with Image.open(output_dir / name) as image:
            require(image.width % 6 == 0 and image.height % 6 == 0, f"{name} is not crop-cell-aligned")
            require(image.width // 6 == image.height // 6, f"{name} uses non-square cells")
            require(image.mode in ("RGB", "RGBA"), f"{name} has unexpected mode {image.mode}")
    with Image.open(output_dir / "canvas_grid.png") as canvas:
        require(canvas.size == (columns * 12, rows * 12), "canvas_grid.png does not match grid and render scale")
    with Image.open(output_dir / "pattern_transparent.png") as transparent:
        require(transparent.mode == "RGBA", "pattern_transparent.png must be RGBA")
        alpha_min, alpha_max = transparent.getchannel("A").getextrema()
        require((alpha_min, alpha_max) == (0, 255), "transparent output needs both clear background and opaque beads")

    artifacts = payload.get("artifacts")
    require(isinstance(artifacts, dict), "pattern.json artifacts must be an object")
    for filename, metadata in artifacts.items():
        require(isinstance(filename, str) and Path(filename).name == filename, f"unsafe artifact key: {filename!r}")
        require(isinstance(metadata, dict), f"artifact metadata for {filename!r} must be an object")
        require((output_dir / filename).is_file(), f"registered artifact does not exist: {filename}")
    for filename in required - {"pattern.json", "summary.json"}:
        require(filename in artifacts, f"artifact registry omitted {filename}")

    # The source hash and dimensions are useful; its machine-specific absolute
    # location is not.  Search the complete successful command output and every
    # textual artifact, not just one JSON field.
    leaked_text = completed.stdout + "\n" + completed.stderr
    for path in output_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".json", ".csv", ".txt", ".log"}:
            leaked_text += "\n" + path.read_text(encoding="utf-8", errors="replace")
    require(str(source.resolve()) not in leaked_text, "absolute source path leaked into CLI output or artifacts")


def assert_default_has_no_board(output_dir: Path) -> dict[str, Any]:
    """The opt-in board layer must not alter the legacy default contract."""

    payload = json.loads((output_dir / "pattern.json").read_text(encoding="utf-8"))
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    require("board" not in payload, "default restore unexpectedly added top-level board metadata")
    require("board" not in summary, "default summary unexpectedly added board metadata")
    board_files = sorted(path.name for path in output_dir.iterdir() if path.name.startswith("board"))
    require(not board_files, f"default restore unexpectedly emitted board artifacts: {board_files}")
    artifacts = payload.get("artifacts", {})
    require(
        not any(str(name).startswith("board") for name in artifacts),
        "default artifact registry unexpectedly contains a board artifact",
    )
    return payload


def assert_board_artifacts(
    output_dir: Path,
    payload: dict[str, Any],
    *,
    source_overlay: bool,
    render_cell_px: int = 12,
) -> list[list[str]]:
    """Validate board metadata and reconstruct its matrix from native cells."""

    board = payload.get("board")
    require(isinstance(board, dict), "pattern.json is missing top-level board metadata")
    size = board.get("board_size")
    require(size in (52, 78), "board_size must be the integer 52 or 78")
    require((board.get("columns"), board.get("rows")) == (size, size), "board dimensions disagree with board_size")
    require(board.get("standard") == "wenzhou", "board standard is not wenzhou")
    require(board.get("resampled") is False, "board metadata must explicitly say resampled=false")

    placement = board.get("placement")
    require(isinstance(placement, dict), "board placement metadata is missing")
    bbox = payload.get("content_bbox")
    require(isinstance(bbox, dict), "native content_bbox is missing")
    require(placement.get("source_bbox") == bbox, "board source_bbox disagrees with native content_bbox")
    content_width = int(bbox["right_exclusive"]) - int(bbox["left"])
    content_height = int(bbox["bottom_exclusive"]) - int(bbox["top"])
    require(
        board.get("content_size") == {"columns": content_width, "rows": content_height},
        "board content_size disagrees with native content_bbox",
    )
    row_offset, col_offset = placement.get("row_offset"), placement.get("col_offset")
    require(isinstance(row_offset, int) and isinstance(col_offset, int), "board offsets must be integers")
    require(0 <= row_offset <= size - content_height, "board row_offset is outside capacity")
    require(0 <= col_offset <= size - content_width, "board col_offset is outside capacity")
    margins = placement.get("margins")
    require(isinstance(margins, dict), "board margins are missing")
    require(
        margins
        == {
            "left": col_offset,
            "top": row_offset,
            "right": size - col_offset - content_width,
            "bottom": size - row_offset - content_height,
        },
        "board margins are inconsistent with placement",
    )

    base_artifacts = {
        "board.csv",
        "board_preview.png",
        "board_grid.png",
        "board_transparent.png",
    }
    expected_artifacts = set(base_artifacts)
    if source_overlay:
        expected_artifacts.add("board_source_overlay.png")
    registry = payload.get("artifacts")
    require(isinstance(registry, dict), "pattern artifact registry is missing")
    for filename in expected_artifacts:
        require((output_dir / filename).is_file(), f"missing board artifact: {filename}")
        require(filename in registry, f"board artifact registry omitted {filename}")
    if source_overlay:
        require("board_source_overlay.png" in registry, "restore omitted board source overlay metadata")
    else:
        require(not (output_dir / "board_source_overlay.png").exists(), "render/revise invented a source overlay")
        require("board_source_overlay.png" not in registry, "render/revise registered a nonexistent source overlay")

    palette = payload.get("palette")
    require(isinstance(palette, list), "pattern palette is missing")
    backgrounds = [entry for entry in palette if entry.get("role") == "background"]
    require(len(backgrounds) == 1, "board test requires one background palette entry")
    background_symbol = backgrounds[0]["symbol"]
    rows = int(payload["grid"]["rows"])
    columns = int(payload["grid"]["columns"])
    native_matrix = cell_matrix_from_json(payload, rows, columns)
    native_content = [
        row[int(bbox["left"]) : int(bbox["right_exclusive"])]
        for row in native_matrix[int(bbox["top"]) : int(bbox["bottom_exclusive"])]
    ]
    expected_matrix = [[background_symbol for _ in range(size)] for _ in range(size)]
    for local_row, row in enumerate(native_content):
        expected_matrix[row_offset + local_row][col_offset : col_offset + content_width] = row
    board_matrix = read_csv_matrix(output_dir / "board.csv", size, size)
    require(board_matrix == expected_matrix, "board.csv is not a lossless placement of the native content cells")

    board_counts = board.get("counts")
    native_counts = payload.get("counts")
    require(isinstance(board_counts, dict) and isinstance(native_counts, dict), "native or board counts are missing")
    require(sum(int(value) for value in board_counts.values()) == size * size, "board counts do not sum to mold area")
    for entry in palette:
        if entry.get("role") != "background":
            name = entry["name"]
            require(
                int(board_counts.get(name, -1)) == int(native_counts.get(name, -2)),
                f"board placement changed the native count for {name!r}",
            )
    require(board.get("bead_count") == payload.get("bead_count"), "board changed the native bead count")

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise SelfTestFailure("Pillow disappeared during board artifact validation") from exc
    for filename in ("board_preview.png", "board_grid.png", "board_transparent.png"):
        with Image.open(output_dir / filename) as image:
            require(
                image.size == (size * render_cell_px, size * render_cell_px),
                f"{filename} does not match board size and render scale",
            )
    with Image.open(output_dir / "board_transparent.png") as transparent:
        require(transparent.mode == "RGBA", "board_transparent.png must be RGBA")
        require(
            transparent.getchannel("A").getextrema() == (0, 255),
            "board transparent output needs both clear background and opaque beads",
        )
    if source_overlay:
        with Image.open(output_dir / "board_source_overlay.png") as overlay:
            require(
                overlay.size == (int(payload["source"]["width_px"]), int(payload["source"]["height_px"])),
                "board source overlay dimensions disagree with the source image",
            )
    return board_matrix


def run_cli(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )
    require(
        completed.returncode == 0,
        f"{label} failed\n"
        f"command: {' '.join(command)}\n"
        f"stdout: {completed.stdout.strip()}\n"
        f"stderr: {completed.stderr.strip()}",
    )
    return completed


def run_cli_failure(
    command: list[str],
    label: str,
    *,
    expected_stderr: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )
    require(
        completed.returncode != 0,
        f"{label} unexpectedly succeeded\ncommand: {' '.join(command)}",
    )
    if expected_stderr is not None:
        require(
            expected_stderr in completed.stderr,
            f"{label} omitted the expected error {expected_stderr!r}\nstderr: {completed.stderr.strip()}",
        )
    return completed


def assert_mard_cli_contract(
    source: Path,
    output_dir: Path,
    completed: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    """Validate built-in card provenance and code-bearing public artifacts."""

    stdout_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    require(len(stdout_lines) == 1, "MARD restore did not write exactly one JSON summary line")
    stdout_summary = json.loads(stdout_lines[0])
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    require(stdout_summary == summary, "MARD stdout summary disagrees with summary.json")
    payload = json.loads((output_dir / "pattern.json").read_text(encoding="utf-8"))
    require(payload.get("schema_version") == "1.2", "MARD output did not use the additive 1.2 schema")
    require(payload.get("algorithm_version") == "0.4.1", "MARD output did not record the current algorithm version")

    profile = payload.get("palette_profile")
    require(isinstance(profile, dict), "MARD pattern omitted palette_profile")
    required_profile = {
        "id",
        "code_system",
        "reference_type",
        "source_urls",
        "bead_color_count",
        "background_strategy",
        "matching_method",
        "license_spdx",
        "copyright",
        "source_commit",
        "license_url",
        "notice_file",
        "trademark_disclaimer",
    }
    require(required_profile <= set(profile), "MARD output profile is incomplete")
    require(profile.get("id") == "mard-221-compatible", "MARD output profile id is wrong")
    require(profile.get("code_system") == "mard-221", "MARD output code system is wrong")
    require(profile.get("bead_color_count") == 221, "MARD output profile bead count is wrong")
    require(profile.get("runtime_entry_count") == 222, "MARD output profile runtime count is wrong")
    require(profile.get("matching_method") == "CIEDE2000", "MARD output profile matching method is wrong")
    require(profile.get("license_spdx") == "MIT", "MARD output profile lost its license identifier")
    require(profile.get("copyright") == "Copyright (c) 2026 Jett-Wu", "MARD output lost upstream copyright")
    require(summary.get("palette_profile") == profile, "MARD summary lost palette provenance")
    require(payload.get("status") == "review", "screen-reference MARD output must remain review")
    require(
        any("screen references" in str(warning) for warning in payload.get("warnings", [])),
        "MARD output did not warn that RGB is only a screen reference",
    )

    palette = payload.get("palette")
    require(isinstance(palette, list) and len(palette) == 222, "MARD pattern palette is not 221 plus background")
    backgrounds = [entry for entry in palette if entry.get("role") == "background"]
    require(len(backgrounds) == 1, "MARD pattern does not have exactly one background sentinel")
    background = backgrounds[0]
    require(
        background.get("name") == "background"
        and background.get("symbol") == "."
        and background.get("code") is None
        and background.get("synthetic") is True,
        "MARD pattern background is not explicitly synthetic",
    )
    bead_entries = [entry for entry in palette if entry.get("code") is not None]
    require(len(bead_entries) == 221, "MARD pattern omitted a bead color")
    codes = {entry["code"] for entry in bead_entries}
    require(codes == expected_mard_221_codes(), "MARD pattern code set differs from the resource")
    require(
        all(
            entry.get("name") == entry.get("symbol") == entry.get("code")
            and entry.get("synthetic") is False
            for entry in bead_entries
        ),
        "MARD JSON does not expose codes consistently",
    )

    grid = payload.get("grid", {})
    rows, columns = int(grid.get("rows", -1)), int(grid.get("columns", -1))
    require((columns, rows) == (GRID_COLUMNS, GRID_ROWS), "MARD CLI changed the explicit native grid")
    matrix = cell_matrix_from_json(payload, rows, columns)
    csv_matrix = read_csv_matrix(output_dir / "canvas.csv", rows, columns)
    require(matrix == csv_matrix, "MARD canvas.csv disagrees with code-bearing JSON cells")
    allowed_symbols = {".", *expected_mard_221_codes()}
    require(all(symbol in allowed_symbols for row in matrix for symbol in row), "MARD matrix contains a non-code symbol")
    require(matrix[0][0] == matrix[1][1] == ".", "edge-connected warm patch did not become source background")
    require(matrix[4][4] == matrix[3][3] != ".", "enclosed white cell did not inherit the warm foreground code")

    cells = payload.get("cells")
    require(isinstance(cells, list) and len(cells) == rows * columns, "MARD output cells are incomplete")
    for cell in cells:
        require(
            {"code", "raw_code", "alternative_code"} <= set(cell),
            "MARD cell omitted code provenance",
        )
        if cell.get("symbol") == ".":
            require(cell.get("code") is None, "synthetic background cell has a purchase code")
        else:
            require(cell.get("code") == cell.get("symbol") in codes, "MARD cell symbol is not its purchase code")

    counts = payload.get("counts")
    require(isinstance(counts, dict), "MARD output counts are missing")
    require(set(counts) == {"background", *expected_mard_221_codes()}, "MARD counts do not cover the full card")
    require(sum(int(value) for value in counts.values()) == rows * columns, "MARD counts do not sum to native area")
    require(int(counts["background"]) + int(payload.get("bead_count", -1)) == rows * columns, "synthetic background polluted bead_count")
    require(int(payload.get("bead_count", 0)) > 0, "MARD CLI recovered no beads")

    matching = payload.get("postprocess", {}).get("palette_matching")
    require(isinstance(matching, dict), "MARD output omitted matching diagnostics")
    require(matching.get("catalog_color_count") == 221, "MARD matching diagnostics report the wrong card size")
    require(str(matching.get("method", "")).startswith("CIEDE2000"), "MARD matching diagnostics omit DeltaE 2000")
    require(isinstance(matching.get("cluster_mappings"), list), "MARD cluster mapping provenance is missing")

    with (output_dir / "palette.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        palette_rows = list(reader)
        fields = set(reader.fieldnames or [])
    require({"name", "symbol", "code", "synthetic", "rgb_hex", "role", "count"} <= fields, "MARD palette.csv omitted code fields")
    require(len(palette_rows) == 222, "MARD palette.csv omitted runtime entries")
    csv_codes = {row["code"] for row in palette_rows if row.get("code")}
    require(csv_codes == expected_mard_221_codes(), "MARD palette.csv code set is incomplete")
    background_rows = [row for row in palette_rows if row.get("synthetic") == "true"]
    require(len(background_rows) == 1 and not background_rows[0].get("code"), "palette.csv background is not a code-free sentinel")

    required_files = {
        "pattern.json",
        "summary.json",
        "canvas.csv",
        "matrix.csv",
        "palette.csv",
        "review.csv",
        "pattern_preview.png",
        "pattern_grid.png",
        "pattern_review.png",
        "pattern_transparent.png",
        "canvas_grid.png",
        "source_grid_overlay.png",
        "candidates.png",
        "candidates.json",
        "THIRD_PARTY_NOTICES.md",
    }
    require(
        all((output_dir / filename).is_file() for filename in required_files),
        "MARD restore omitted a standard artifact",
    )
    registry = payload.get("artifacts", {})
    require("THIRD_PARTY_NOTICES.md" in registry, "MARD artifact registry omitted its license notice")
    notice = (output_dir / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    require(
        "Copyright (c) 2026 Jett-Wu" in notice
        and "Permission is hereby granted" in notice
        and 'THE SOFTWARE IS PROVIDED "AS IS"' in notice,
        "MARD output notice omitted the complete upstream MIT license",
    )
    leaked = completed.stdout + completed.stderr + json.dumps(payload, ensure_ascii=False)
    require(str(source.resolve()) not in leaked, "MARD output leaked the absolute source path")
    return payload


def assert_mard_revise_render_roundtrip(root: Path, original_dir: Path) -> None:
    """A code edit and pure rerender must preserve the complete 221 profile."""

    original = json.loads((original_dir / "pattern.json").read_text(encoding="utf-8"))
    target = next(cell for cell in original["cells"] if cell.get("code") is not None)
    replacement = next(
        code
        for code in sorted(expected_mard_221_codes())
        if code != target["code"] and int(original["counts"].get(code, 0)) == 0
    )
    edits = root / "mard-edits.csv"
    with edits.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("row", "col", "new_label"))
        writer.writeheader()
        writer.writerow({"row": target["row"], "col": target["col"], "new_label": replacement})

    revised_dir = root / "mard-revised"
    run_cli(
        [
            sys.executable,
            str(RESTORE_SCRIPT),
            "revise",
            str(original_dir / "pattern.json"),
            "--edits",
            str(edits),
            "--out",
            str(revised_dir),
            "--render-cell-px",
            "12",
        ],
        "MARD revise CLI",
    )
    revised = json.loads((revised_dir / "pattern.json").read_text(encoding="utf-8"))
    require((revised_dir / "THIRD_PARTY_NOTICES.md").is_file(), "revise dropped the MARD license notice")
    require(revised.get("palette_profile") == original.get("palette_profile"), "revise changed MARD profile")
    require(revised.get("palette") == original.get("palette"), "revise changed the built-in MARD card")
    require(revised.get("grid") == original.get("grid"), "MARD code edit changed native geometry")
    require(revised.get("source") == original.get("source"), "MARD code edit changed source provenance")
    require(revised.get("bead_count") == original.get("bead_count"), "MARD color edit changed bead_count")
    revised_cell = next(
        cell
        for cell in revised["cells"]
        if cell["row"] == target["row"] and cell["col"] == target["col"]
    )
    require(
        revised_cell.get("label") == revised_cell.get("symbol") == revised_cell.get("code") == replacement,
        "revise did not propagate a MARD code through label/symbol/code",
    )
    old_code = str(target["code"])
    require(
        int(revised["counts"][old_code]) == int(original["counts"][old_code]) - 1,
        "revise did not decrement the old MARD code count",
    )
    require(
        int(revised["counts"][replacement]) == int(original["counts"][replacement]) + 1,
        "revise did not increment the new MARD code count",
    )
    revised_matrix = read_csv_matrix(revised_dir / "canvas.csv", GRID_ROWS, GRID_COLUMNS)
    require(
        revised_matrix[int(target["row"])][int(target["col"])] == replacement,
        "revised canvas.csv did not emit the requested MARD code",
    )

    rendered_dir = root / "mard-rendered"
    run_cli(
        [
            sys.executable,
            str(RESTORE_SCRIPT),
            "render",
            str(revised_dir / "pattern.json"),
            "--out",
            str(rendered_dir),
            "--render-cell-px",
            "12",
        ],
        "MARD render CLI",
    )
    rendered = json.loads((rendered_dir / "pattern.json").read_text(encoding="utf-8"))
    require((rendered_dir / "THIRD_PARTY_NOTICES.md").is_file(), "render dropped the MARD license notice")
    for key in ("palette_profile", "palette", "grid", "source", "content_bbox", "counts", "bead_count", "cells"):
        require(rendered.get(key) == revised.get(key), f"render changed MARD field {key}")
    rendered_matrix = read_csv_matrix(rendered_dir / "canvas.csv", GRID_ROWS, GRID_COLUMNS)
    require(rendered_matrix == revised_matrix, "render changed the revised MARD code matrix")
    rendered_summary = json.loads((rendered_dir / "summary.json").read_text(encoding="utf-8"))
    require(
        rendered_summary.get("palette_profile") == revised.get("palette_profile"),
        "render summary lost MARD palette_profile",
    )
    with (rendered_dir / "palette.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fields = set(reader.fieldnames or [])
    require({"code", "synthetic"} <= fields and len(rows) == 222, "rendered palette.csv lost MARD code metadata")


def assert_integer_scale_cli(root: Path, template: dict[str, Any]) -> None:
    """A user-authorized integer scale is exact replication, not redrawing."""

    source_path = root / "integer-scale-source-pattern.json"
    source = write_integer_scale_fixture(source_path, template)
    source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()

    def scale_command(output: Path, factor: str, board_size: str = "78x78") -> list[str]:
        return [
            sys.executable,
            str(RESTORE_SCRIPT),
            "scale",
            str(source_path),
            "--factor",
            factor,
            "--board-size",
            board_size,
            "--out",
            str(output),
            "--render-cell-px",
            "8",
        ]

    scaled_dir = root / "integer-scale-2x"
    run_cli(scale_command(scaled_dir, "2"), "integer 2x scale CLI")
    scaled = json.loads((scaled_dir / "pattern.json").read_text(encoding="utf-8"))
    summary = json.loads((scaled_dir / "summary.json").read_text(encoding="utf-8"))

    require(scaled.get("resampled") is True, "authorized scale did not set top-level resampled=true")
    derivation = scaled.get("derivation")
    require(isinstance(derivation, dict), "authorized scale omitted derivation provenance")
    expected_derivation = {
        "kind": "integer-nearest-neighbor-content-scale",
        "factor": 2,
        "authorized_by": "explicit-user-request",
        "source_pattern_sha256": source_digest,
        "source_content_bbox": source["content_bbox"],
        "source_content_size": {"columns": 31, "rows": 38},
        "output_content_size": {"columns": 62, "rows": 76},
        "label_mapping": "exact-cell-replication",
    }
    for key, value in expected_derivation.items():
        require(derivation.get(key) == value, f"integer-scale derivation field {key} is incorrect")

    grid = scaled.get("grid", {})
    require(
        (grid.get("columns"), grid.get("rows")) == (62, 76),
        "31x38 content scaled by 2 did not become a compact 62x76 grid",
    )
    expected_bbox = {"left": 0, "top": 0, "right_exclusive": 62, "bottom_exclusive": 76}
    require(scaled.get("content_bbox") == expected_bbox, "scaled content_bbox is not the full compact grid")
    require(scaled.get("palette") == source.get("palette"), "integer scaling changed or added palette colors")

    source_matrix = cell_matrix_from_json(source, 40, 33)
    source_bbox = source["content_bbox"]
    source_content = [
        row[int(source_bbox["left"]) : int(source_bbox["right_exclusive"])]
        for row in source_matrix[int(source_bbox["top"]) : int(source_bbox["bottom_exclusive"])]
    ]
    scaled_matrix = cell_matrix_from_json(scaled, 76, 62)
    scaled_csv = read_csv_matrix(scaled_dir / "canvas.csv", 76, 62)
    require(scaled_matrix == scaled_csv, "scaled canvas.csv disagrees with pattern.json")
    for source_row in range(38):
        for source_col in range(31):
            expected_symbol = source_content[source_row][source_col]
            block = [
                scaled_matrix[source_row * 2 + delta_row][source_col * 2 : source_col * 2 + 2]
                for delta_row in range(2)
            ]
            require(
                block == [[expected_symbol, expected_symbol], [expected_symbol, expected_symbol]],
                f"source cell ({source_row},{source_col}) was not copied to one exact 2x2 block",
            )

    source_cells = {
        (int(cell["row"]) - 1, int(cell["col"]) - 1): cell
        for cell in source["cells"]
        if 1 <= int(cell["row"]) < 39 and 1 <= int(cell["col"]) < 32
    }
    scaled_cells = {
        (int(cell["row"]), int(cell["col"])): cell for cell in scaled["cells"]
    }
    repeated_fields = ("label", "symbol", "raw_label", "alternative", "confidence", "agreement")
    for (source_row, source_col), source_cell in source_cells.items():
        for delta_row in range(2):
            for delta_col in range(2):
                scaled_cell = scaled_cells[(source_row * 2 + delta_row, source_col * 2 + delta_col)]
                for field in repeated_fields:
                    require(
                        scaled_cell.get(field) == source_cell.get(field),
                        f"2x scale changed {field} for source cell ({source_row},{source_col})",
                    )

    palette = source["palette"]
    background = next(entry for entry in palette if entry.get("role") == "background")
    foreground_names = {
        str(entry["name"]) for entry in palette if entry.get("role") != "background"
    }
    for name in foreground_names:
        require(
            int(scaled["counts"].get(name, -1)) == int(source["counts"].get(name, 0)) * 4,
            f"2x scale did not multiply color {name!r} by four",
        )
    require(int(scaled["counts"].get(background["name"], -1)) == 0, "source exterior leaked into scaled content")
    require(scaled.get("bead_count") == source.get("bead_count") * 4, "2x scale did not quadruple bead_count")
    source_used = {symbol for row in source_content for symbol in row}
    scaled_used = {symbol for row in scaled_matrix for symbol in row}
    require(scaled_used == source_used, "integer scaling synthesized a color from source-photo shading")

    board = scaled.get("board")
    require(isinstance(board, dict), "integer scale omitted its explicit Wenzhou board")
    require(
        board.get("standard") == "wenzhou"
        and board.get("mode") == "explicit"
        and board.get("board_size") == 78
        and board.get("selection_status") == "explicit",
        "integer scale changed explicit 78x78 board provenance",
    )
    require(
        board.get("resampled") is False,
        "board must remain a one-to-one placement of the already-derived 62x76 grid",
    )
    design_derivation = board.get("design_derivation")
    require(
        design_derivation
        == {
            "kind": "integer-nearest-neighbor-content-scale",
            "factor": 2,
            "source_content_size": {"columns": 31, "rows": 38},
            "output_content_size": {"columns": 62, "rows": 76},
        },
        "scaled board design_derivation is incomplete or incorrect",
    )
    require(board.get("content_size") == {"columns": 62, "rows": 76}, "scaled board content_size is wrong")
    placement = board.get("placement", {})
    require(
        placement.get("margins") == {"left": 8, "top": 1, "right": 8, "bottom": 1},
        "62x76 content was not centered on 78x78 with margins 8/8/1/1",
    )
    require(
        (placement.get("col_offset"), placement.get("row_offset")) == (8, 1),
        "scaled board offsets disagree with its required margins",
    )
    board_matrix = read_csv_matrix(scaled_dir / "board.csv", 78, 78)
    expected_board = [[background["symbol"] for _ in range(78)] for _ in range(78)]
    for row, values in enumerate(scaled_matrix):
        expected_board[1 + row][8 : 8 + 62] = values
    require(board_matrix == expected_board, "scaled board is not a centered one-to-one copy of the 62x76 grid")
    require(board.get("bead_count") == scaled.get("bead_count"), "board changed scaled bead_count")
    require(
        int(board.get("counts", {}).get(background["name"], -1)) == 78 * 78 - 62 * 76,
        "scaled board background count disagrees with the four margins",
    )
    for name in foreground_names:
        require(
            int(board.get("counts", {}).get(name, -1)) == int(scaled["counts"].get(name, -2)),
            f"scaled board changed color count {name!r}",
        )
    require(summary.get("board") == board, "scaled summary board disagrees with pattern.json")
    require(not (scaled_dir / "board_source_overlay.png").exists(), "scale invented a source-photo overlay")

    rendered_dir = root / "integer-scale-rendered"
    run_cli(
        [
            sys.executable,
            str(RESTORE_SCRIPT),
            "render",
            str(scaled_dir / "pattern.json"),
            "--out",
            str(rendered_dir),
            "--render-cell-px",
            "8",
        ],
        "integer-scale render CLI",
    )
    rendered = json.loads((rendered_dir / "pattern.json").read_text(encoding="utf-8"))
    for key in ("resampled", "derivation", "source", "grid", "content_bbox", "palette", "counts", "bead_count", "cells", "board"):
        require(rendered.get(key) == scaled.get(key), f"render changed scaled provenance/data field {key}")
    require(
        read_csv_matrix(rendered_dir / "board.csv", 78, 78) == board_matrix,
        "render changed the scaled board matrix",
    )

    target = scaled["cells"][0]
    replacement = next(
        entry
        for entry in scaled["palette"]
        if entry.get("role") != "background" and entry["name"] != target["label"]
    )
    edits_path = root / "integer-scale-edits.csv"
    with edits_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("row", "col", "new_label"))
        writer.writeheader()
        writer.writerow({"row": 0, "col": 0, "new_label": replacement["name"]})
    revised_dir = root / "integer-scale-revised"
    run_cli(
        [
            sys.executable,
            str(RESTORE_SCRIPT),
            "revise",
            str(scaled_dir / "pattern.json"),
            "--edits",
            str(edits_path),
            "--out",
            str(revised_dir),
            "--render-cell-px",
            "8",
        ],
        "integer-scale revise CLI",
    )
    revised = json.loads((revised_dir / "pattern.json").read_text(encoding="utf-8"))
    for key in ("resampled", "derivation", "source", "grid", "content_bbox", "palette"):
        require(revised.get(key) == scaled.get(key), f"revise changed scaled provenance field {key}")
    require(revised["board"].get("resampled") is False, "revise mislabeled one-to-one board placement as resampling")
    require(
        revised["board"].get("design_derivation") == board.get("design_derivation"),
        "revise changed board design-derivation provenance",
    )
    require(revised["board"].get("placement") == board.get("placement"), "revise moved unchanged scaled content")
    revised_cell = next(cell for cell in revised["cells"] if cell["row"] == 0 and cell["col"] == 0)
    require(revised_cell.get("label") == replacement["name"], "revise did not edit the scaled cell")
    require(revised.get("bead_count") == scaled.get("bead_count"), "scaled color revision changed bead_count")
    revised_board = read_csv_matrix(revised_dir / "board.csv", 78, 78)
    require(revised_board[1][8] == replacement["symbol"], "scaled revision did not propagate to board offset (1,8)")

    for factor in ("1", "9"):
        invalid_dir = root / f"integer-scale-invalid-{factor}"
        run_cli_failure(
            scale_command(invalid_dir, factor),
            f"integer scale factor {factor}",
            expected_stderr="value must be between 2 and 8",
        )
        require(not (invalid_dir / "pattern.json").exists(), "invalid scale factor wrote a pattern")
    non_integer_dir = root / "integer-scale-invalid-decimal"
    run_cli_failure(scale_command(non_integer_dir, "2.5"), "non-integer scale factor")
    require(not (non_integer_dir / "pattern.json").exists(), "non-integer scale factor wrote a pattern")

    overflow_dir = root / "integer-scale-overflow"
    overflow = run_cli_failure(
        scale_command(overflow_dir, "3"),
        "integer scale board overflow",
        expected_stderr="scaled content",
    )
    require("does not fit 78x78" in overflow.stderr, "overflow error omitted the selected board size")
    require(not (overflow_dir / "pattern.json").exists(), "overflowing scale wrote a partial pattern")


def assert_revise_and_render_preserve_board(root: Path, board_output: Path) -> None:
    original = json.loads((board_output / "pattern.json").read_text(encoding="utf-8"))
    original_board = original["board"]
    edit_row, edit_col = 4, 5
    original_cell = next(
        cell for cell in original["cells"] if cell["row"] == edit_row and cell["col"] == edit_col
    )
    replacement = next(
        entry
        for entry in original["palette"]
        if entry["role"] == "accent" and entry["name"] != original_cell["label"]
    )
    edits_path = root / "edits.csv"
    with edits_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("row", "col", "new_label"))
        writer.writeheader()
        writer.writerow({"row": edit_row, "col": edit_col, "new_label": replacement["name"]})

    revised_dir = root / "revised"
    revise_command = [
        sys.executable,
        str(RESTORE_SCRIPT),
        "revise",
        str(board_output / "pattern.json"),
        "--edits",
        str(edits_path),
        "--out",
        str(revised_dir),
        "--render-cell-px",
        "12",
    ]
    run_cli(revise_command, "board revise CLI")
    revised = json.loads((revised_dir / "pattern.json").read_text(encoding="utf-8"))
    for key in ("standard", "mode", "board_size", "selection_status", "selection_confidence", "reason", "resampled"):
        require(revised["board"].get(key) == original_board.get(key), f"revise changed board provenance field {key}")
    require(revised["board"]["placement"] == original_board["placement"], "revise changed unchanged board placement")
    require(revised["bead_count"] == original["bead_count"], "non-background color edit changed bead_count")
    revised_cell = next(
        cell for cell in revised["cells"] if cell["row"] == edit_row and cell["col"] == edit_col
    )
    require(revised_cell["label"] == replacement["name"], "revise did not apply the requested native cell edit")
    revised_board_matrix = assert_board_artifacts(revised_dir, revised, source_overlay=False)
    placement = revised["board"]["placement"]
    bbox = revised["content_bbox"]
    board_row = int(placement["row_offset"]) + edit_row - int(bbox["top"])
    board_col = int(placement["col_offset"]) + edit_col - int(bbox["left"])
    require(
        revised_board_matrix[board_row][board_col] == replacement["symbol"],
        "revised native cell did not propagate to its board coordinate",
    )

    rendered_dir = root / "rendered"
    render_command = [
        sys.executable,
        str(RESTORE_SCRIPT),
        "render",
        str(revised_dir / "pattern.json"),
        "--out",
        str(rendered_dir),
        "--render-cell-px",
        "12",
    ]
    run_cli(render_command, "board render CLI")
    rendered = json.loads((rendered_dir / "pattern.json").read_text(encoding="utf-8"))
    require(rendered["cells"] == revised["cells"], "render changed the native cell matrix")
    require(rendered["counts"] == revised["counts"], "render changed native palette counts")
    require(rendered["bead_count"] == revised["bead_count"], "render changed native bead_count")
    require(rendered["board"] == revised["board"], "render did not preserve self-contained board metadata")
    rendered_board_matrix = assert_board_artifacts(rendered_dir, rendered, source_overlay=False)
    require(rendered_board_matrix == revised_board_matrix, "render changed the revised board matrix")


def assert_failed_board_render_stays_failed(root: Path, board_output: Path) -> None:
    """Rerendering cannot downgrade a recorded mold-capacity failure."""

    payload = json.loads((board_output / "pattern.json").read_text(encoding="utf-8"))
    failed = json.loads(json.dumps(payload))
    failed["status"] = "fail"
    failed["board"] = {
        "standard": "wenzhou",
        "mode": "auto",
        "selection_status": "fail",
        "reason": "native content exceeds the largest Wenzhou mold 78x78",
        "resampled": False,
        "content_size": {"columns": 79, "rows": 43},
    }
    failed_path = root / "capacity-fail-pattern.json"
    failed_path.write_text(
        json.dumps(failed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output_dir = root / "capacity-fail-rendered"
    command = [
        sys.executable,
        str(RESTORE_SCRIPT),
        "render",
        str(failed_path),
        "--out",
        str(output_dir),
        "--render-cell-px",
        "12",
    ]
    run_cli(command, "capacity-fail board render CLI")
    rendered = json.loads((output_dir / "pattern.json").read_text(encoding="utf-8"))
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    require(rendered["status"] == "fail", "render downgraded a failed board to review or pass")
    require(summary["status"] == "fail", "render summary downgraded a failed board")
    require(rendered["board"] == failed["board"], "render changed capacity-failure provenance")
    require(summary["board"] == failed["board"], "summary changed capacity-failure provenance")
    require(
        not any(path.name.startswith("board") for path in output_dir.iterdir()),
        "render emitted board artifacts for a capacity-failed board",
    )
    require(
        not any(str(name).startswith("board") for name in rendered.get("artifacts", {})),
        "render registered board artifacts for a capacity-failed board",
    )


def assert_half_cell_render_reuses_board_metadata(root: Path, board_output: Path) -> None:
    """Stored placement wins when rounded JSON lies on a half-cell boundary."""

    payload = json.loads((board_output / "pattern.json").read_text(encoding="utf-8"))
    boundary = json.loads(json.dumps(payload))
    pitch = float(boundary["grid"]["pitch_px"])
    source_width = float(boundary["source"]["width_px"])
    board_size = int(boundary["board"]["board_size"])
    centered_origin = (source_width - board_size * pitch) / 2.0
    # Put the stored native origin exactly at N+0.5 cells from the centered
    # mold origin. A fresh calculation rounds upward, but provenance says the
    # prior accepted placement used the lower shift.
    lower_shift = 20
    boundary["grid"]["origin_x_px"] = centered_origin + (lower_shift + 0.5) * pitch
    bbox = boundary["content_bbox"]
    placement = boundary["board"]["placement"]
    col_offset = int(bbox["left"]) + lower_shift
    placement["col_offset"] = col_offset
    placement["native_to_board_shift"]["columns"] = lower_shift
    placement["board_origin_x_px"] = round(
        float(boundary["grid"]["origin_x_px"]) - lower_shift * pitch, 6
    )
    placement["centered_target_origin_x_px"] = round(centered_origin, 6)
    placement["centering_error_x_px"] = round(pitch * 0.5, 6)
    content_width = int(bbox["right_exclusive"]) - int(bbox["left"])
    content_height = int(bbox["bottom_exclusive"]) - int(bbox["top"])
    placement["margins"] = {
        "left": col_offset,
        "top": int(placement["row_offset"]),
        "right": board_size - col_offset - content_width,
        "bottom": board_size - int(placement["row_offset"]) - content_height,
    }
    boundary["board"]["candidates"] = [
        {"rank": 1, "board_size": board_size, "selected": True, "stable_marker": "half-cell"}
    ]
    boundary_path = root / "half-cell-pattern.json"
    boundary_path.write_text(
        json.dumps(boundary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    output_dir = root / "half-cell-rendered"
    command = [
        sys.executable,
        str(RESTORE_SCRIPT),
        "render",
        str(boundary_path),
        "--out",
        str(output_dir),
        "--render-cell-px",
        "12",
    ]
    run_cli(command, "half-cell board render CLI")
    rendered = json.loads((output_dir / "pattern.json").read_text(encoding="utf-8"))
    require(
        rendered["board"] == boundary["board"],
        "render recomputed placement or candidates at a half-cell phase boundary",
    )
    assert_board_artifacts(output_dir, rendered, source_overlay=False)


def assert_output_overwrite_guards(
    root: Path,
    source: Path,
    existing_output: Path,
) -> None:
    owned_output = root / "owned-overwrite"
    first = run_cli(cli_command(source, owned_output), "owned output initial CLI")
    assert_cli_contract(source, owned_output, first)
    stale = owned_output / "stale.txt"
    stale.write_text("stale", encoding="utf-8")
    second = run_cli(
        [*cli_command(source, owned_output), "--overwrite"],
        "owned output overwrite CLI",
    )
    assert_cli_contract(source, owned_output, second)
    require(not stale.exists(), "owned overwrite retained an unregistered stale file")

    empty_output = root / "preexisting-empty"
    empty_output.mkdir()
    empty_completed = run_cli(
        cli_command(source, empty_output),
        "preexisting empty output CLI",
    )
    assert_cli_contract(source, empty_output, empty_completed)

    unowned = root / "unowned-overwrite"
    unowned.mkdir()
    unowned_sentinel = unowned / "sentinel.txt"
    unowned_sentinel.write_text("preserve", encoding="utf-8")
    run_cli_failure(
        [*cli_command(source, unowned), "--overwrite"],
        "unowned output overwrite CLI",
        expected_stderr="not owned by restore-bead-pattern",
    )
    require(
        unowned_sentinel.read_text(encoding="utf-8") == "preserve",
        "unowned overwrite CLI deleted existing data",
    )

    source_parent = root / "source-parent"
    source_parent.mkdir()
    nested_source = source_parent / "source.png"
    make_synthetic_source(nested_source)
    run_cli_failure(
        [*cli_command(nested_source, source_parent), "--overwrite"],
        "source-ancestor output CLI",
        expected_stderr="contains a protected input",
    )
    require(nested_source.is_file(), "source-ancestor guard failed to preserve the source image")

    run_cli_failure(
        [
            sys.executable,
            str(RESTORE_SCRIPT),
            "render",
            str(existing_output / "pattern.json"),
            "--out",
            str(existing_output),
            "--overwrite",
        ],
        "pattern-ancestor output CLI",
        expected_stderr="contains a protected input",
    )
    require(
        (existing_output / "pattern.json").is_file(),
        "pattern-ancestor guard deleted the input manifest",
    )

    edits_parent = root / "edits-parent"
    edits_parent.mkdir()
    edits = edits_parent / "edits.csv"
    edits.write_text("row,col,new_label\n", encoding="utf-8")
    run_cli_failure(
        [
            sys.executable,
            str(RESTORE_SCRIPT),
            "revise",
            str(existing_output / "pattern.json"),
            "--edits",
            str(edits),
            "--out",
            str(edits_parent),
            "--overwrite",
        ],
        "edits-ancestor output CLI",
        expected_stderr="contains a protected input",
    )
    require(edits.is_file(), "edits-ancestor guard deleted the edits CSV")


def assert_design_cli(root: Path) -> None:
    """Exercise the independent ordinary-image design boundary end to end."""

    require(DESIGN_SCRIPT.is_file(), f"missing design script: {DESIGN_SCRIPT}")
    source = root / "ordinary-illustration.png"
    make_ordinary_design_source(source)

    def command(
        output: Path,
        *,
        board_size: str | None = None,
        background: str = "empty-white",
        overwrite: bool = False,
    ) -> list[str]:
        result = [
            sys.executable,
            str(DESIGN_SCRIPT),
            str(source),
            "--out",
            str(output),
            "--clusters",
            "8",
            "--seed",
            "7",
            "--background",
            background,
            "--preview-cell-px",
            "8",
            "--grid-cell-px",
            "18",
        ]
        if board_size is not None:
            result.extend(("--board-size", board_size))
        if overwrite:
            result.append("--overwrite")
        return result

    empty_output = root / "design-empty-78"
    completed = run_cli(command(empty_output), "ordinary-image design CLI")
    stdout_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    require(len(stdout_lines) == 1, "design CLI did not emit exactly one JSON summary line")
    stdout_summary = json.loads(stdout_lines[0])
    pattern = json.loads((empty_output / "pattern.json").read_text(encoding="utf-8"))
    summary = json.loads((empty_output / "summary.json").read_text(encoding="utf-8"))
    require(stdout_summary == summary, "design stdout summary disagrees with summary.json")
    require(pattern.get("schema_version") == "design-1.0", "design schema is incorrect")
    require(pattern.get("algorithm_version") == "design-1.0.0", "design algorithm version is incorrect")
    require(pattern.get("kind") == "new-bead-pattern-design", "design kind is not explicit")
    require(pattern.get("not_restoration") is True, "ordinary image was presented as restoration")
    require(pattern.get("status") == "review", "new design bypassed visual review status")
    canvas = pattern.get("canvas", {})
    require(
        canvas.get("board_size") == canvas.get("rows") == canvas.get("columns") == 78,
        "default design board is not 78x78",
    )
    require(canvas.get("full_square_design") is False, "empty design was marked as a full bead board")
    background = pattern.get("background", {})
    require(
        background.get("synthetic") is True
        and background.get("symbol") == "."
        and background.get("code") is None
        and background.get("applied_mode") == "synthetic-empty",
        "empty-white background is not an explicit synthetic sentinel",
    )
    require(0 < int(pattern.get("bead_count", 0)) < 78 * 78, "empty design bead_count is invalid")
    require(
        int(pattern.get("counts", {}).get("background", 0))
        == 78 * 78 - int(pattern["bead_count"]),
        "empty design background count disagrees with bead_count",
    )
    require(1 <= int(pattern.get("used_color_count", 0)) <= 9, "design used an unexpected number of MARD colors")
    profile = pattern.get("palette_profile", {})
    require(
        profile.get("id") == "mard-221-compatible"
        and profile.get("bead_color_count") == 221
        and profile.get("provisional") is True,
        "design did not use the bundled provisional MARD 221 profile",
    )
    rights = pattern.get("rights", {})
    require(
        rights.get("rights_or_authorization_verified_by_tool") is False
        and rights.get("privacy_or_portrait_consent_verified_by_tool") is False
        and rights.get("output_may_remain_identifiable") is True
        and rights.get("commercial_copying_or_public_redistribution_rights_granted_by_tool") is False
        and rights.get("source_image_included") is False,
        "design rights boundary is incomplete",
    )
    required_artifacts = {
        "DESIGN_RIGHTS_NOTICE.md",
        "THIRD_PARTY_NOTICES.md",
        "design.csv",
        "design_grid.png",
        "design_preview.png",
        "design_transparent.png",
        "palette_counts.csv",
        "pattern.json",
        "summary.json",
    }
    require(set(pattern.get("artifacts", [])) == required_artifacts, "design artifact registry is incomplete")
    require(all((empty_output / name).is_file() for name in required_artifacts), "design omitted an artifact")
    require(pattern.get("rights") == summary.get("rights"), "summary lost design rights metadata")
    rights_notice = (empty_output / "DESIGN_RIGHTS_NOTICE.md").read_text(encoding="utf-8")
    require(
        "does not verify copyright" in rights_notice
        and "privacy, portrait or publicity rights" in rights_notice
        and "may remain identifiable" in rights_notice
        and "not an anonymity guarantee" in rights_notice
        and "grant rights to commercially reproduce" in rights_notice
        and "source image is not included" in rights_notice,
        "design rights notice omitted a required limitation",
    )
    third_party = (empty_output / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    require(
        "Copyright (c) 2026 Jett-Wu" in third_party
        and "Permission is hereby granted" in third_party,
        "design dropped the bundled MARD license notice",
    )
    cells = pattern.get("cells", [])
    require(len(cells) == 78 * 78, "design cells do not cover the 78x78 board")
    coordinates = {(cell.get("row"), cell.get("col")) for cell in cells}
    require(len(coordinates) == 78 * 78, "design cells contain duplicate coordinates")
    empty_cells = [cell for cell in cells if cell.get("synthetic")]
    require(
        bool(empty_cells)
        and all(cell.get("code") is None and cell.get("symbol") == "." for cell in empty_cells),
        "synthetic design cells are not code-free dots",
    )
    physical_codes = expected_mard_221_codes()
    require(
        all(cell.get("code") in physical_codes for cell in cells if not cell.get("synthetic")),
        "design emitted a code outside bundled MARD 221",
    )
    cell_counts = Counter(
        "background" if cell.get("synthetic") else str(cell.get("code"))
        for cell in cells
    )
    require(dict(cell_counts) == pattern.get("counts"), "design counts disagree with cells")
    with (empty_output / "palette_counts.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        palette_count_rows = list(csv.DictReader(handle))
    require(
        {row["code"]: int(row["count"]) for row in palette_count_rows}
        == pattern.get("counts"),
        "palette_counts.csv disagrees with design manifest",
    )
    matrix = read_csv_matrix(empty_output / "design.csv", 78, 78)
    require(
        matrix == [[cell["symbol"] for cell in cells[row * 78 : (row + 1) * 78]] for row in range(78)],
        "design.csv disagrees with pattern.json",
    )
    source_metadata = pattern.get("source", {})
    require(set(source_metadata) == {"sha256", "width_px", "height_px"}, "design source metadata leaked a path")
    leaked = completed.stdout + completed.stderr + json.dumps(pattern, ensure_ascii=False)
    require(str(source.resolve()) not in leaked, "design output leaked the absolute source path")
    require(not (empty_output / "source_grid_overlay.png").exists(), "design emitted a restoration overlay")
    assert_output_marker(empty_output)

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise SelfTestFailure("design alpha test requires Pillow") from exc
    alpha_extrema = (
        Image.open(empty_output / "design_transparent.png")
        .convert("RGBA")
        .getchannel("A")
        .getextrema()
    )
    require(alpha_extrema == (0, 255), "empty design transparent PNG lacks clear/opaque pixels")
    require(
        Image.open(empty_output / "design_grid.png").size == (78 * 18, 78 * 18),
        "78x78 code grid dimensions disagree with --grid-cell-px 18",
    )

    baseline = {
        name: (empty_output / name).read_bytes()
        for name in required_artifacts
    }
    run_cli(command(empty_output, overwrite=True), "deterministic design overwrite")
    assert_output_marker(empty_output)
    for name, expected in baseline.items():
        require(
            (empty_output / name).read_bytes() == expected,
            f"fixed-seed design overwrite changed artifact {name}",
        )

    unowned = root / "design-unowned-output"
    unowned.mkdir()
    sentinel = unowned / "preserve.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    run_cli_failure(
        command(unowned, overwrite=True),
        "design unowned overwrite guard",
        expected_stderr="not owned by restore-bead-pattern",
    )
    require(sentinel.read_text(encoding="utf-8") == "preserve", "design overwrite deleted unowned data")

    flat_source = root / "flat-transparent-illustration.png"
    make_flat_rgba_design_source(flat_source)
    flat_output = root / "design-flat-alpha-auto"
    flat_completed = run_cli(
        [
            sys.executable,
            str(DESIGN_SCRIPT),
            str(flat_source),
            "--out",
            str(flat_output),
            "--fit-mode",
            "center-square",
            "--preview-cell-px",
            "8",
            "--grid-cell-px",
            "18",
        ],
        "flat RGBA default design CLI",
    )
    flat_pattern = json.loads((flat_output / "pattern.json").read_text(encoding="utf-8"))
    require(
        flat_pattern.get("canvas", {}).get("board_size") == 78
        and flat_pattern.get("design_method", {}).get("logical_color_clusters") == 12,
        "flat design did not exercise the public defaults",
    )
    require(
        1 <= int(flat_pattern.get("design_method", {}).get("effective_color_clusters", 0)) <= 3,
        "flat design did not compact empty k-means clusters",
    )
    require(
        flat_pattern.get("background", {}).get("requested_mode") == "auto"
        and flat_pattern.get("background", {}).get("synthetic") is True
        and int(flat_pattern.get("counts", {}).get("background", 0)) > 0,
        "default auto mode did not treat transparent source area as empty",
    )
    require(
        int(flat_pattern.get("counts", {}).get("H7", 0)) == 0,
        "hidden transparent RGB or saturated red was turned into black beads",
    )
    require(
        str(flat_source.resolve())
        not in (flat_completed.stdout + flat_completed.stderr + json.dumps(flat_pattern)),
        "flat RGBA design leaked its source path",
    )
    assert_output_marker(flat_output)

    unlabeled_grid_output = root / "design-grid-too-small"
    run_cli_failure(
        [
            sys.executable,
            str(DESIGN_SCRIPT),
            str(source),
            "--out",
            str(unlabeled_grid_output),
            "--grid-cell-px",
            "17",
        ],
        "design code-label grid minimum",
        expected_stderr="--grid-cell-px must be between 18 and 64",
    )
    require(not unlabeled_grid_output.exists(), "unlabeled design grid wrote output")

    oversized_output = root / "design-render-too-large"
    run_cli_failure(
        [
            sys.executable,
            str(DESIGN_SCRIPT),
            str(source),
            "--out",
            str(oversized_output),
            "--clusters",
            "8",
            "--background",
            "empty-white",
            "--preview-cell-px",
            "8",
            "--grid-cell-px",
            "64",
        ],
        "design render pixel safety limit",
        expected_stderr="16,000,000 total pixels",
    )
    require(not oversized_output.exists(), "oversized design render left a partial output")
    require(
        not list(root.glob(".design-render-too-large.staging-*")),
        "oversized design render left a staging directory",
    )

    bead_output = root / "design-bead-52"
    bead_completed = run_cli(
        command(bead_output, board_size="52x52", background="bead"),
        "52x52 full-bead design CLI",
    )
    bead_pattern = json.loads((bead_output / "pattern.json").read_text(encoding="utf-8"))
    bead_canvas = bead_pattern.get("canvas", {})
    require(
        bead_canvas.get("board_size") == bead_canvas.get("rows") == bead_canvas.get("columns") == 52,
        "explicit design board is not 52x52",
    )
    require(bead_canvas.get("full_square_design") is True, "bead mode did not fill the full board")
    require(bead_pattern.get("bead_count") == 52 * 52, "bead mode bead_count is not full board area")
    require("background" not in bead_pattern.get("counts", {}), "bead mode emitted synthetic background counts")
    require(
        bead_pattern.get("background", {}).get("synthetic") is False
        and bead_pattern.get("background", {}).get("applied_mode") == "physical-bead-board",
        "bead mode background metadata is incorrect",
    )
    require(
        len(bead_pattern.get("cells", [])) == 52 * 52
        and all(
            not cell.get("synthetic") and cell.get("code") in physical_codes
            for cell in bead_pattern["cells"]
        ),
        "52x52 bead mode contains empty or non-MARD cells",
    )
    require(
        set(bead_pattern.get("artifacts", [])) == required_artifacts
        and all((bead_output / name).is_file() for name in required_artifacts),
        "52x52 design omitted a rights, license, data, or render artifact",
    )
    require(
        bead_pattern.get("rights", {}).get("source_image_included") is False,
        "52x52 design lost rights metadata",
    )
    bead_matrix = read_csv_matrix(bead_output / "design.csv", 52, 52)
    require(
        bead_matrix
        == [
            [cell["symbol"] for cell in bead_pattern["cells"][row * 52 : (row + 1) * 52]]
            for row in range(52)
        ],
        "52x52 design.csv disagrees with pattern.json",
    )
    bead_alpha_extrema = (
        Image.open(bead_output / "design_transparent.png")
        .convert("RGBA")
        .getchannel("A")
        .getextrema()
    )
    require(bead_alpha_extrema == (255, 255), "bead mode transparent PNG contains empty pixels")
    require(
        Image.open(bead_output / "design_grid.png").size == (52 * 18, 52 * 18),
        "52x52 code grid dimensions disagree with --grid-cell-px 18",
    )
    require(str(source.resolve()) not in (bead_completed.stdout + bead_completed.stderr + json.dumps(bead_pattern)), "52x52 design leaked source path")
    assert_output_marker(bead_output)


def test_cli() -> None:
    with tempfile.TemporaryDirectory(prefix="restore-bead-self-test-") as temporary:
        root = Path(temporary)
        source = root / "synthetic-fuzzy-grid.png"
        output_dir = root / "result"
        make_synthetic_source(source)
        command = cli_command(source, output_dir)
        completed = run_cli(command, "default restore CLI")
        assert_cli_contract(source, output_dir, completed)
        default_payload = assert_default_has_no_board(output_dir)

        board_output = root / "board-result"
        board_command = cli_command(source, board_output, "52x52")
        board_completed = run_cli(board_command, "explicit 52x52 restore CLI")
        assert_cli_contract(source, board_output, board_completed)
        board_payload = json.loads((board_output / "pattern.json").read_text(encoding="utf-8"))
        for key in ("grid", "content_bbox", "counts", "bead_count", "cells"):
            require(
                board_payload[key] == default_payload[key],
                f"enabling a board changed native restore field {key}",
            )
        board = board_payload.get("board")
        require(isinstance(board, dict), "explicit 52x52 restore omitted board metadata")
        require(board.get("mode") == "explicit", "explicit board mode was not recorded")
        require(board.get("board_size") == 52, "explicit 52x52 restore selected the wrong board")
        require(board.get("selection_status") == "explicit", "explicit board was presented as inferred")
        summary = json.loads((board_output / "summary.json").read_text(encoding="utf-8"))
        require(summary.get("board") == board, "summary board metadata disagrees with pattern.json")
        assert_board_artifacts(board_output, board_payload, source_overlay=True)
        assert_revise_and_render_preserve_board(root, board_output)
        assert_failed_board_render_stays_failed(root, board_output)
        assert_half_cell_render_reuses_board_metadata(root, board_output)
        assert_integer_scale_cli(root, default_payload)
        assert_output_overwrite_guards(root, source, output_dir)

        mard_output = root / "mard-result"
        mard_command = cli_command(
            source,
            mard_output,
            palette="mard-221-compatible",
        )
        mard_completed = run_cli(mard_command, "MARD 221 restore CLI")
        assert_mard_cli_contract(source, mard_output, mard_completed)
        assert_mard_revise_render_roundtrip(root, mard_output)
        assert_design_cli(root)

        successful_outputs = sorted(
            {path.parent for path in root.rglob("summary.json")},
            key=lambda path: str(path),
        )
        require(successful_outputs, "self-test produced no successful output directories")
        for output in successful_outputs:
            assert_output_marker(output)


def main() -> int:
    try:
        module = load_restore_module()
        design_module = load_design_module()
        test_mard_221_resource_contract()
        test_mard_builtin_loader(module)
        test_input_limits_and_palette_text_safety(module)
        test_output_target_safety(module)
        test_lab_and_delta_e_references(module)
        test_mard_cluster_matching_and_background_isolation(module)
        test_auto_palette_keeps_small_chroma_clusters(module)
        test_four_connected_light_topology(module)
        test_wenzhou_mold_geometry(module)
        test_wenzhou_mold_selection(module)
        test_design_contain_geometry(design_module)
        test_cli()
    except (SelfTestFailure, OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"restore-bead-pattern self-test: FAIL: {exc}", file=sys.stderr)
        return 1
    print("restore-bead-pattern self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
