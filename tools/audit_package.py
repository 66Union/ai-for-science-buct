#!/usr/bin/env python3
"""Audit a packaged Skill bundle against the rules a reviewer or the platform can check.

This complements tools/e2e_check.py: e2e asks "does it produce a valid result.json",
this asks "is the bundle itself acceptable" - the things that are cheap to check and
expensive to get wrong:

* SKILL.md exists, is UTF-8 without BOM, and has valid name/description YAML frontmatter;
* GITHUB_URL.txt points at a plausible public repository path;
* platform limits (<= 50 UTF-8 text files, each <= 500 KB, <= 2 MB total, no symlinks);
* no credential material and no absolute developer-machine paths in shipped files;
* a runner under scripts/ that accepts --prepare/--finalize/--all;
* the documented minimal example is present;
* evidence/run-report emitters are mentioned in the code (the topic requires honest
  status and provenance, not just a result).

Usage: python tools/audit_package.py out/submit-reading-03-translation [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

LIMITS = {"max_files": 50, "max_file_bytes": 500 * 1024, "max_total_bytes": 2 * 1024 * 1024}

SECRET_PATTERNS = (
    re.compile(r"\.secrets[/\\]"),
    re.compile(r"PLAYGROUND_TOKEN\s*=\s*[A-Za-z0-9]"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bhht_[A-Za-z0-9]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
)
DEV_PATH_PATTERNS = (
    re.compile(r"\bD:[\\/]AI-for-science"),
    re.compile(r"\bC:[\\/]Users[\\/]86188"),
)

REQUIRED_SKILL_SECTIONS = (
    ("技能定位", "position"),
    ("适用条件", "conditions"),
    ("调用方式", "usage"),
    ("核心工作流", "workflow"),
    ("异常处理", "errors"),
    ("限制", "limits"),
    ("自查", "self-check"),
    ("示例", "example"),
)


def utf8_text(path: Path) -> str | None:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return None  # BOM is a defect for a shipped text resource
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def check_frontmatter(text: str) -> list[str]:
    problems: list[str] = []
    if not text.startswith("---"):
        return ["SKILL.md does not start with a YAML frontmatter block"]
    end = text.find("\n---", 4)
    if end < 0:
        return ["SKILL.md frontmatter is not closed"]
    block = text[3:end]
    fields = {}
    for line in block.splitlines():
        match = re.match(r"^([A-Za-z_]+)\s*:\s*(.*)$", line.strip())
        if match:
            fields[match.group(1)] = match.group(2).strip().strip('"\'')
    for required in ("name", "description"):
        if not fields.get(required):
            problems.append(f"frontmatter is missing a non-empty {required}")
    name = fields.get("name") or ""
    if name and not re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,63}", name):
        problems.append(f"frontmatter name is not kebab-case: {name!r}")
    return problems


def audit(package: Path) -> dict:
    problems: list[str] = []
    warnings: list[str] = []
    if not package.is_dir():
        return {"package": str(package), "ok": False, "problems": ["package directory missing"]}

    files = sorted(p for p in package.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)
    if len(files) > LIMITS["max_files"]:
        problems.append(f"{len(files)} files exceeds the {LIMITS['max_files']} limit")
    if total > LIMITS["max_total_bytes"]:
        problems.append(f"total {total} bytes exceeds the 2 MB limit")
    for path in files:
        rel = path.relative_to(package)
        if path.is_symlink():
            problems.append(f"symlink in bundle: {rel}")
        if path.stat().st_size > LIMITS["max_file_bytes"]:
            problems.append(f"{rel} is {path.stat().st_size} bytes > 500 KB")

    text_files: dict[str, str] = {}
    for path in files:
        rel = path.relative_to(package)
        if path.suffix.lower() in {".json", ".md", ".txt", ".py", ".yaml", ".yml", ".bib", ".tex"}:
            text = utf8_text(path)
            if text is None:
                problems.append(f"{rel} is not valid UTF-8 text (or has a BOM)")
            else:
                text_files[str(rel)] = text
    if not text_files:
        problems.append("no UTF-8 text resources in the bundle")

    # SKILL.md
    skill_md = None
    for key, text in text_files.items():
        if key.replace("/", "\\") == "SKILL.md" or key == "SKILL.md":
            skill_md = text
            break
    if skill_md is None:
        problems.append("SKILL.md missing from the bundle root")
    else:
        problems.extend(check_frontmatter(skill_md))
        for label, _slug in REQUIRED_SKILL_SECTIONS:
            if label not in skill_md:
                problems.append(f"SKILL.md has no section mentioning {label}")
        if len(skill_md) < 8000:
            problems.append(f"SKILL.md is only {len(skill_md)} chars; docs depth is a scored item")

    # GITHUB_URL.txt
    url_text = text_files.get("GITHUB_URL.txt")
    if not url_text:
        problems.append("GITHUB_URL.txt missing")
    else:
        first = url_text.splitlines()[0].strip() if url_text.strip() else ""
        if not first.startswith("https://github.com/"):
            problems.append(f"GITHUB_URL.txt first line is not a GitHub URL: {first!r}")

    # developer-machine paths and credentials in shipped code
    for rel, text in text_files.items():
        for pattern in DEV_PATH_PATTERNS:
            if pattern.search(text):
                problems.append(f"{rel} contains an absolute developer path: "
                                f"{pattern.search(text).group(0)}")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                problems.append(f"{rel} contains a credential-looking string: {pattern.pattern}")
        if "_selftest_tmp" in text and rel.startswith("tests"):
            warnings.append(f"{rel} references _selftest_tmp (fine only if cleaned up in finally)")

    # runner + modes
    runners = sorted(k for k in text_files
                     if k.replace("\\", "/").startswith("scripts/run_"))
    # the shared kernel ships run_reading.py into every bundle; the topic's own
    # entry point is what a reviewer and the agent actually invoke
    topic_runners = [r for r in runners if not r.replace("\\", "/").endswith("run_reading.py")]
    if topic_runners:
        runners = topic_runners
    if not runners:
        problems.append("no scripts/run_*.py entry point")
    else:
        body = text_files[runners[0]]
        is_shared_single_stage = runners[0].replace("\\", "/").endswith("scripts/run_reading.py")
        if is_shared_single_stage:
            warnings.append("single-stage shared runner (run_reading.py): two-phase flags not required")
        else:
            for flag in ("--prepare", "--finalize", "--all"):
                if flag not in body:
                    problems.append(f"runner does not expose {flag}")
        for emitter in ("evidence", "run_report"):
            if emitter not in body:
                warnings.append(f"runner never mentions {emitter}")

    # package manifest (written by package_skill.py) must agree with reality
    manifest = None
    if "_package_manifest.json" in text_files:
        try:
            manifest = json.loads(text_files["_package_manifest.json"])
        except Exception:
            problems.append("_package_manifest.json is not valid JSON")
    if manifest and not manifest.get("ok", False):
        problems.append("_package_manifest.json reports violations")

    return {
        "package": package.name,
        "ok": not problems,
        "file_count": len(files),
        "text_file_count": len(text_files),
        "total_bytes": total,
        "skill_md_chars": len(skill_md or ""),
        "runner": runners[0] if runners else None,
        "problems": problems,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("packages", nargs="+")
    parser.add_argument("--json", action="store_true")
    ns = parser.parse_args()
    reports = [audit(Path(p)) for p in ns.packages]
    if ns.json:
        print(json.dumps(reports, ensure_ascii=False, indent=1))
    else:
        for report in reports:
            print(f"\n=== {report.get('package')} ===")
            print(f"  ok={report.get('ok')} files={report.get('file_count')} "
                  f"text={report.get('text_file_count')} bytes={report.get('total_bytes')} "
                  f"skill_md={report.get('skill_md_chars')} runner={report.get('runner')}")
            for problem in report.get("problems") or []:
                print("  PROBLEM:", problem)
            for warning in report.get("warnings") or []:
                print("  warn   :", warning)
    return 0 if all(report.get("ok") for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
