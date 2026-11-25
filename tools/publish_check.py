#!/usr/bin/env python3
"""Verify that every submitted Skill really is publicly reachable on GitHub.

The GitHub bonus is only awarded if a reviewer who is *not logged in* can open the
link and find the work. The previous session learned two traps the hard way:

* api.github.com answers 403 for anonymous requests from this network, while
  raw.githubusercontent.com and github.com/.../tree/... answer 200 - so the API is
  the wrong probe;
* a freshly pushed tree page can be served from a CDN cache without the new files,
  so a cache-buster query parameter is needed before concluding "missing".

Usage: python tools/publish_check.py [--branch main] [--repo 66Union/ai-for-science-buct]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"
# The public repos the skills state in GITHUB_URL.txt; keep the path form in sync.
DEFAULT_REPO = "66Union/ai-for-science-buct"
# GitHub serves a small interstitial to obviously non-browser agents and the file
# listing only appears deep in the real HTML, so use a browser-like UA and read the
# whole document instead of a 4 KB prefix.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
MAX_BODY = 600 * 1024


def fetch(url: str, timeout: int = 30) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(MAX_BODY).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:  # noqa: BLE001 - network probe must not crash
        return 0, f"{type(exc).__name__}: {exc}"


def check_skill(skill_dir: Path, repo: str, branch: str) -> dict:
    rel = f"skills/{skill_dir.name}"
    stamp = int(time.time())
    checks: list[dict] = []
    files = ["SKILL.md", f"{rel}/SKILL.md"]

    # 1) the directory listing page (what GITHUB_URL.txt points at)
    tree_url = f"https://github.com/{repo}/tree/{branch}/{rel}?cb={stamp}"
    status, body = fetch(tree_url)
    checks.append({"url": tree_url, "status": status,
                   "expect": "200 and the directory name in the HTML title",
                   "ok": status == 200 and skill_dir.name in body})

    # 2) raw SKILL.md at it is packaged (proof the file, not a cached page)
    raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{rel}/SKILL.md"
    status, body = fetch(raw_url)
    checks.append({"url": raw_url, "status": status,
                   "expect": "200 and a SKILL.md frontmatter",
                   "ok": status == 200 and body.startswith("---")})

    # 3) the GitHub URL written into each submission must equal #1 (minus cache-buster)
    url_file = skill_dir / "GITHUB_URL.txt"
    declared = None
    if url_file.is_file():
        first = url_file.read_text(encoding="utf-8").splitlines()
        declared = first[0].strip() if first else ""
    expected_prefix = f"https://github.com/{repo}/tree/{branch}/{rel}"
    checks.append({"url": declared or "(missing GITHUB_URL.txt)", "status": None,
                   "expect": f"equals {expected_prefix}",
                   "ok": bool(declared) and declared.startswith(expected_prefix)})
    return {"skill": skill_dir.name, "ok": all(c["ok"] for c in checks), "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--json", action="store_true")
    ns = parser.parse_args()
    skill_dirs = sorted(p for p in SKILLS.iterdir()
                        if p.is_dir() and p.name != "_core" and (p / "SKILL.md").is_file())
    reports = [check_skill(d, ns.repo, ns.branch) for d in skill_dirs]
    if ns.json:
        print(json.dumps(reports, ensure_ascii=False, indent=1))
    else:
        for report in reports:
            print(f"{'OK  ' if report['ok'] else 'FAIL'} {report['skill']}")
            for check in report["checks"]:
                if not check["ok"]:
                    print(f"     {check['url']} -> status={check['status']} ({check['expect']})")
    bad = [r["skill"] for r in reports if not r["ok"]]
    print(f"\n{len(reports) - len(bad)}/{len(reports)} skill link(s) publicly reachable")
    if bad:
        print("not reachable:", ", ".join(bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
