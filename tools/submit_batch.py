#!/usr/bin/env python3
"""Package, dry-run and submit every challenge skill in a fixed order.

The platform is the only judge of whether a bundle is acceptable, so this driver is
deliberately thin: it runs the real CLI exactly as a human would, records the server
response for every attempt, and stops as soon as the platform refuses (quota, bad
bundle, rate limit) instead of hammering the API.

Usage:
    python tools/submit_batch.py --trace .tmp/trace-final.jsonl --dry-run
    python tools/submit_batch.py --trace .tmp/trace-final.jsonl --submit --only reading-03-translation
    python tools/submit_batch.py --trace .tmp/trace-final.jsonl --submit --from writing-02-language-polish

Every real submission appends one JSON line to out/submit-log.jsonl with the attempt
id, score state, quota snapshot and bundle hash, so the run can be audited later.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / ".tools" / "playground.cmd"
PLAN = REPO / "tools" / "submit_plan.json"
LOG = REPO / "out" / "submit-log.jsonl"
TOKEN_FILE = REPO / ".secrets" / "playground_token.txt"


def clean_package(package: Path) -> list[str]:
    """Remove test by-products from a bundle before it is uploaded.

    Running the bundle's own tests writes lib/__pycache__/*.pyc inside it, and a
    submission that ships compiled bytecode looks careless to a reviewer (and breaks
    the 'UTF-8 text resources' rule). Cheap to detect, cheap to fix, so it is done
    here instead of relying on every author remembering.
    """
    removed: list[str] = []
    for path in list(package.rglob("*")):
        if path.is_dir() and path.name in {"__pycache__", ".pytest_cache"}:
            for child in path.rglob("*"):
                if child.is_file():
                    removed.append(str(child.relative_to(package)))
            import shutil
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file() and path.suffix == ".pyc":
            removed.append(str(path.relative_to(package)))
            path.unlink(missing_ok=True)
    for leftover in package.rglob("_selftest_tmp*"):
        removed.append(str(leftover.relative_to(package)))
        import shutil
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)
        else:
            leftover.unlink(missing_ok=True)
    return removed


def token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def run_cli(args: list[str], *, timeout: int = 900) -> tuple[int, str]:
    env = dict(os.environ)
    env["PLAYGROUND_TOKEN"] = token()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # The CLI prints UTF-8 JSON on a console whose default code page on Windows is
    # GBK; letting Python decode it with the locale encoding crashes the reader
    # thread and hides the real server response. Decode explicitly.
    proc = subprocess.run([str(CLI), *args], capture_output=True, text=True,
                          timeout=timeout, env=env, shell=False,
                          encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def parse_json_tail(text: str) -> dict:
    """The CLI prints a human line and then a JSON document; find the document."""
    start = text.find("{")
    while start != -1:
        try:
            return json.loads(text[start:])
        except Exception:
            start = text.find("{", start + 1)
    return {}


def find_key(payload: Any, key: str, depth: int = 0) -> Any:
    """Depth-limited recursive lookup; the quota block moved between CLI versions."""
    if depth > 5:
        return None
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = find_key(value, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload[:20]:
            found = find_key(value, key, depth + 1)
            if found is not None:
                return found
    return None


def summarise(payload: dict) -> dict:
    """Pull the useful fields out of either response shape.

    --dry-run answers {status, bundle: <path>, bundle_sha256, bundle_bytes, manifest},
    while a real submit answers {attempt: {...}, bundle: {...}, ...}; the driver must
    not crash on the difference.
    """
    def as_dict(value: Any) -> dict:
        return value if isinstance(value, dict) else {}

    attempt = as_dict(find_key(payload, "attempt")) or as_dict(payload.get("attempt"))
    bundle = as_dict(payload.get("bundle"))
    quota = as_dict(find_key(payload, "contest_upload_quota"))
    return {
        "attempt_id": attempt.get("id"),
        "status": attempt.get("status") or payload.get("status"),
        "display_score": (attempt.get("scoringState") or {}).get("displayScore"),
        "queue_position": (attempt.get("scoringQueue") or {}).get("queuePosition"),
        "bundle_sha256": payload.get("bundle_sha256") or bundle.get("bundle_sha256"),
        "bundle_bytes": payload.get("bundle_bytes") or bundle.get("bundle_bytes"),
        "quota_used": quota.get("used"),
        "quota_limit": quota.get("limit"),
        "quota_refused": quota.get("refused"),
        "quota_window_end": quota.get("window_end"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--only", default=None)
    parser.add_argument("--from", dest="start_at", default=None)
    parser.add_argument("--skip-existing", action="store_true",
                        help="skip a challenge that already has a logged attempt id")
    ns = parser.parse_args()
    if not ns.dry_run and not ns.submit:
        print("choose --dry-run or --submit")
        return 2

    trace = (REPO / ns.trace).resolve() if not Path(ns.trace).is_absolute() else Path(ns.trace)
    if not trace.is_file():
        print(f"trace not found: {trace}")
        return 2

    plan = json.loads(PLAN.read_text(encoding="utf-8"))["entries"]
    note = os.environ.get("PLAYGROUND_SUBMIT_NOTE", "").strip()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    done_challenges = set()
    if LOG.is_file():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("attempt_id"):
                done_challenges.add(row.get("challenge_id"))

    results: list[dict] = []
    in_scope = False
    for entry in plan:
        directory = entry["dir"]
        if ns.start_at:
            in_scope = in_scope or directory == ns.start_at
            if not in_scope:
                continue
        if ns.only and directory != ns.only:
            continue
        if ns.skip_existing and entry["challenge_id"] in done_challenges:
            print(f"skip {directory} (already submitted)")
            continue
        package = REPO / "out" / f"submit-{directory}"
        if not package.is_dir():
            print(f"missing package: {package}")
            results.append({"dir": directory, "ok": False, "reason": "missing package"})
            continue
        print(f"\n=== {directory} -> {entry['challenge_id']} ===")
        removed = clean_package(package)
        if removed:
            print(f"  cleaned {len(removed)} test by-product(s): {removed[:6]}")
        argv = ["submit", "--challenge-id", entry["challenge_id"],
                "--outputs", str(package), "--trace", str(trace),
                "--harness", "deepseek-harness", "--model", "deepseek-flash"]
        if ns.dry_run:
            argv.append("--dry-run")
        code, out = run_cli(argv)
        # keep the platform's own response verbatim: it is the only audit trail for
        # which attempt id was created and how much quota it consumed
        (REPO / "out" / f"{'dryrun' if ns.dry_run else 'submit'}-{directory}-raw.log").write_text(
            out, encoding="utf-8", errors="replace")
        payload = parse_json_tail(out)
        row = {
            "dir": directory,
            "challenge_id": entry["challenge_id"],
            "title": entry.get("title"),
            "mode": "dry-run" if ns.dry_run else "submit",
            "exit_code": code,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "note": note,
        }
        row.update(summarise(payload))
        if not payload:
            row["raw_tail"] = out[-1200:]
        ok = code == 0 and bool(payload)
        row["ok"] = ok
        results.append(row)
        print(json.dumps({k: v for k, v in row.items() if k != "raw_tail"}, ensure_ascii=False))
        if not ok:
            print("  CLI tail:", out[-800:].replace("\n", "\n  "))
            if ns.submit:
                print("  stopping because the platform/CLI did not accept this bundle")
                break
        if ns.submit:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            with LOG.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({**row, "started_at": started_at},
                                        ensure_ascii=False) + "\n")

    ok_count = sum(1 for row in results if row.get("ok"))
    print(f"\n{ok_count}/{len(results)} package(s) OK")
    if ns.submit:
        print(f"log: {LOG}")
    return 0 if ok_count == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
