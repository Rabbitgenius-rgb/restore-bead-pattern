#!/usr/bin/env python3
"""Validate public repository hygiene without using private fixtures."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "restore-bead-pattern"
EXPECTED_SKILL_FILES = {
    "SKILL.md",
    "LICENSE.txt",
    "THIRD_PARTY_NOTICES.md",
    "agents/openai.yaml",
    "assets/palettes/mard-221-compatible.json",
    "references/contracts.md",
    "scripts/grid_estimator.py",
    "scripts/restore_pattern.py",
    "scripts/self_test.py",
    "scripts/wenzhou_mold.py",
    "third_party/Jett-Wu-MIT.txt",
}
PRIVATE_PATTERNS = (
    b"/Users/",
    b"/var/folders/",
    b"Downloads/",
    b"codex-clipboard",
    b"ChatGPT/",
)


def fail(message: str) -> None:
    raise SystemExit(f"release validation failed: {message}")


def main() -> None:
    if not SKILL.is_dir():
        fail("missing installable skill directory")

    actual = {
        path.relative_to(SKILL).as_posix()
        for path in SKILL.rglob("*")
        if path.is_file()
    }
    if actual != EXPECTED_SKILL_FILES:
        fail(f"skill allowlist mismatch: missing={sorted(EXPECTED_SKILL_FILES - actual)}, extra={sorted(actual - EXPECTED_SKILL_FILES)}")

    skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", skill_text, flags=re.DOTALL)
    if not match:
        fail("SKILL.md has no valid frontmatter block")
    frontmatter: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            fail("SKILL.md frontmatter contains a malformed line")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if not key or not value or key in frontmatter:
            fail("SKILL.md frontmatter contains a blank or duplicate key")
        frontmatter[key] = value.strip('"\'')
    if set(frontmatter) != {"name", "description"}:
        fail("SKILL.md frontmatter must contain only name and description")
    if frontmatter["name"] != SKILL.name:
        fail("skill name does not match its directory")

    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if path.suffix in {".pyc", ".pyo"} or "__pycache__" in path.parts:
            fail(f"cache artifact included: {path.relative_to(ROOT)}")
        data = path.read_bytes()
        for pattern in PRIVATE_PATTERNS:
            if pattern in data:
                fail(f"private path pattern {pattern!r} found in {path.relative_to(ROOT)}")

    upstream_license = (SKILL / "third_party" / "Jett-Wu-MIT.txt").read_text(encoding="utf-8")
    if "Copyright (c) 2026 Jett-Wu" not in upstream_license or "MIT License" not in upstream_license:
        fail("upstream palette license notice is incomplete")

    print("restore-bead-pattern release validation: PASS")


if __name__ == "__main__":
    main()
