#!/usr/bin/env python3
"""Poll the Playground scoring queue and report only what changed.

The queue is long (thousands of attempts ahead), so a monitor that prints the same
table every 30 minutes is noise and costs a model turn each time. This script diffs
the platform state against out/score-snapshot.json and prints a compact JSON answer:

    {"changed": false, "queue_min": 4218, "scored": 0, ...}          -> nothing to do
    {"changed": true, "new_scores": [...], "evaluation_errors": [...]} -> act now

It is called by the scheduled monitor; it is also safe to run by hand.

Usage:
    python tools/check_scores.py            # diff and update the snapshot
    python tools/check_scores.py --raw      # also print per-attempt rows
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / ".tools" / "playground.cmd"
ATTEMPTS = REPO / "out" / "attempts.json"
SNAPSHOT = REPO / "out" / "score-snapshot.json"
TOKEN_FILE = REPO / ".secrets" / "playground_token.txt"


def token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def status_of(attempt_id: int) -> dict:
    """Ask the CLI for one attempt and return the JSON object it printed."""
    env = dict(os.environ)
    env["PLAYGROUND_TOKEN"] = token()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.run([str(CLI), "status", "--attempt-id", str(attempt_id)],
                              capture_output=True, text=True, timeout=120, env=env,
                              shell=False, encoding="utf-8", errors="replace")
        text = (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}
    start = text.find("{")
    while start != -1:
        try:
            return json.loads(text[start:])
        except Exception:
            start = text.find("{", start + 1)
    return {"_error": "no JSON in CLI output", "_tail": text[-300:]}


def compact(target: dict, payload: dict) -> dict:
    queue = payload.get("scoringQueue") or {}
    state = payload.get("scoringState") or {}
    results = payload.get("resultsJson")
    return {
        "attempt_id": target["attempt_id"],
        "label": target.get("label"),
        "dir": target.get("dir"),
        "challenge_id": target.get("challenge_id"),
        "status": payload.get("status"),
        "stage": queue.get("stage"),
        "queue_position": queue.get("queuePosition"),
        "waiting_seconds": queue.get("waitingSeconds"),
        "worker_received": queue.get("workerReceivedAt"),
        "scoring_state": state.get("state"),
        "worker_status": state.get("workerStatus"),
        "display_score": state.get("displayScore"),
        "score": payload.get("score"),
        "results": results,
        "scoring_details": payload.get("scoringDetails"),
        "error": payload.get("_error"),
    }


def score_key(row: dict) -> str | None:
    """A stable identity for 'this attempt has a score now'."""
    if row.get("display_score") is not None:
        return f"display={row['display_score']}"
    results = row.get("results")
    if isinstance(results, dict):
        if results.get("harbor_reward") is not None:
            return f"reward={results['harbor_reward']}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", action="store_true")
    ns = parser.parse_args()

    targets = json.loads(ATTEMPTS.read_text(encoding="utf-8"))["targets"]
    previous = {}
    if SNAPSHOT.is_file():
        try:
            previous = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        except Exception:
            previous = {}

    rows = [compact(target, status_of(target["attempt_id"])) for target in targets]
    prev_rows = {str(row.get("attempt_id")): row for row in previous.get("rows", [])}

    new_scores: list[dict] = []
    evaluation_errors: list[dict] = []
    cleared: list[dict] = []
    for row in rows:
        key = score_key(row)
        before = prev_rows.get(str(row["attempt_id"]), {})
        if key and key != score_key(before):
            new_scores.append(row)
        results = row.get("results")
        if isinstance(results, dict) and results.get("harbor_status") == "evaluation_error":
            if not (isinstance(before.get("results"), dict)
                    and before["results"].get("harbor_status") == "evaluation_error"):
                evaluation_errors.append(row)
        if before.get("stage") != row.get("stage"):
            cleared.append({"attempt_id": row["attempt_id"], "label": row.get("label"),
                            "from": before.get("stage"), "to": row.get("stage")})

    queues = [row["queue_position"] for row in rows
              if isinstance(row.get("queue_position"), int)]
    scored = [row for row in rows if score_key(row)]
    summary = {
        "checked_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S%z"),
        "changed": bool(new_scores or evaluation_errors or cleared),
        "attempts": len(rows),
        "scored": len(scored),
        "scored_attempts": [
            {"attempt_id": row["attempt_id"], "label": row.get("label"),
             "score": row.get("display_score"), "results": row.get("results")}
            for row in scored
        ],
        "new_scores": new_scores,
        "evaluation_errors": evaluation_errors,
        "stage_changes": cleared,
        "queue_min": min(queues) if queues else None,
        "queue_max": max(queues) if queues else None,
        "still_queued": sum(1 for row in rows if row.get("stage") == "queued"),
    }
    SNAPSHOT.write_text(json.dumps({"checked_at": summary["checked_at"], "rows": rows},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if ns.raw:
        print("\nper attempt:")
        for row in sorted(rows, key=lambda r: (r.get("queue_position") is None,
                                               r.get("queue_position") or 0)):
            print(f"  {row['attempt_id']} {row.get('label', ''):<18} "
                  f"stage={row.get('stage')} queue={row.get('queue_position')} "
                  f"waited={row.get('waiting_seconds')}s score={row.get('display_score')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
