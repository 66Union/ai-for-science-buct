#!/usr/bin/env python3
"""Assemble a self-contained Skill submission directory and enforce the platform limits.

The platform installs the Skill at ``/skills/weekly-skill`` and runs it there, so
the submission must carry everything the code imports. Developing under
``skills/_core`` plus a topic dir means the runtime layout differs from the
development layout; this script performs that flattening in one deterministic
step and refuses to produce a bundle that breaks the published limits:

    <= 50 UTF-8 text files, each <= 500 KB, <= 2 MB total, no symlinks

Usage:
    python tools/package_skill.py <challenge-dir> <out-dir>
e.g.
    python tools/package_skill.py reading-01-document-parsing out/submit-reading-01
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "skills" / "_core"
SKILLS = REPO / "skills"

MAX_FILES = 50
MAX_FILE_BYTES = 500 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024

# Directories copied verbatim from the shared kernel into the Skill root.
CORE_PARTS = ("lib", "scripts", "tests")
# Per-challenge files that must exist.
REQUIRED_CHALLENGE_FILES = ("SKILL.md", "GITHUB_URL.txt")
EXCLUDE_NAMES = {"__pycache__", ".pytest_cache", ".DS_Store"}


def _copy_tree(src: Path, dst: Path) -> list[Path]:
    copied: list[Path] = []
    for path in sorted(src.rglob("*")):
        if any(part in EXCLUDE_NAMES for part in path.parts):
            continue
        if path.is_symlink():
            raise SystemExit(f"refusing to copy symlink: {path}")
        rel = path.relative_to(src)
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied.append(target)
    return copied


def _is_utf8_text(path: Path) -> bool:
    try:
        path.read_bytes().decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    challenge = sys.argv[1]
    out = Path(sys.argv[2]).resolve()

    challenge_dir = SKILLS / challenge
    if not challenge_dir.is_dir():
        raise SystemExit(f"no such challenge dir: {challenge_dir}")

    for name in REQUIRED_CHALLENGE_FILES:
        if not (challenge_dir / name).is_file():
            raise SystemExit(f"{challenge} is missing required file: {name}")

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for part in CORE_PARTS:
        src = CORE / part
        if not src.is_dir():
            raise SystemExit(f"shared kernel part missing: {src}")
        written.extend(_copy_tree(src, out / part))

    for name in REQUIRED_CHALLENGE_FILES:
        shutil.copy2(challenge_dir / name, out / name)
        written.append(out / name)

    # Per-challenge code and resources: the adapter runner, its tests/fixtures and any
    # templates or dictionaries live next to the SKILL.md during development, but the
    # platform installs a single flat directory, so fold them into the same layout the
    # core uses (scripts/, tests/, resources/). Copied after the core so a challenge
    # file overrides a same-named core file deliberately.
    for part in ("scripts", "tests", "resources"):
        src = challenge_dir / part
        if src.is_dir():
            written.extend(_copy_tree(src, out / part))

    # ---- limit checks -------------------------------------------------------
    violations: list[str] = []
    files = [p for p in out.rglob("*") if p.is_file()]
    total = 0
    for path in files:
        size = path.stat().st_size
        total += size
        if size > MAX_FILE_BYTES:
            violations.append(f"{path.relative_to(out)}: {size} bytes > {MAX_FILE_BYTES}")
        if not _is_utf8_text(path):
            violations.append(f"{path.relative_to(out)}: not valid UTF-8 text")
        if path.is_symlink():
            violations.append(f"{path.relative_to(out)}: symlink")
    if len(files) > MAX_FILES:
        violations.append(f"{len(files)} files > {MAX_FILES}")
    if total > MAX_TOTAL_BYTES:
        violations.append(f"total {total} bytes > {MAX_TOTAL_BYTES}")

    manifest = {
        "challenge": challenge,
        "out_dir": str(out),
        "files": [
            {"path": str(p.relative_to(out)), "bytes": p.stat().st_size}
            for p in sorted(files)
        ],
        "file_count": len(files),
        "total_bytes": total,
        "limits": {
            "max_files": MAX_FILES,
            "max_file_bytes": MAX_FILE_BYTES,
            "max_total_bytes": MAX_TOTAL_BYTES,
        },
        "violations": violations,
        "ok": not violations,
    }
    (out / "_package_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "out_dir": str(out),
        "file_count": len(files),
        "total_bytes": total,
        "violations": violations,
        "ok": not violations,
    }, ensure_ascii=False, indent=2))
    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
