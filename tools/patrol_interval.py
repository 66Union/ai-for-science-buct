#!/usr/bin/env python3
"""Recommend when the next score-patrol should run, from the observed queue.

Why this exists
---------------
The Playground scoring queue holds thousands of attempts and drains at a rate
that varies by hour. A fixed 30-minute patrol burns a model turn (and the whole
session context) every half hour even when the oldest attempt is 2500 positions
from the front; a fixed 2-hour patrol can sit for hours on a score that already
landed. The number that actually decides "is anything likely to change soon?" is
the queue position of our oldest pending attempt, so the patrol interval is
derived from it, with the observed drain rate as a second input.

Policy (simple, explainable, bounded)
  * position tier: closer to the front -> shorter interval;
  * ETA tier: queue_min / observed drain rate (positions/hour) -> hours to front;
  * imminent: an attempt already handed to a worker (stage != queued) -> 10 min;
  * interval = min(position tier, ETA tier) clamped to [10, 180] minutes;
  * next DTSTART is now + interval, rounded UP to a 5-minute boundary.

Output is JSON (schedule_edit arguments are printed ready to use):
  {queue_min, rate_per_hour, eta_hours, position_tier_min, eta_tier_min,
   imminent, recommended_interval_min, next_dtstart_local, rrule, reason}

Usage:
  python tools/patrol_interval.py            # human/agent readable JSON
  python tools/patrol_interval.py --min 15   # clamp low end differently
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timedelta

REPO = pathlib.Path(__file__).resolve().parents[1]
HISTORY = REPO / "out" / "queue-history.json"
SNAPSHOT = REPO / "out" / "score-snapshot.json"
UNTIL_UTC = "20261129T160000Z"  # contest end 2026-11-29 24:00 Asia/Shanghai


def _load_history() -> list[dict]:
    if not HISTORY.is_file():
        return []
    try:
        data = json.loads(HISTORY.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _load_rows() -> list[dict]:
    if not SNAPSHOT.is_file():
        return []
    try:
        data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = data.get("rows") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else []


def _parse_ts(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def position_tier(queue_min: int) -> int:
    for limit, minutes in ((300, 10), (800, 20), (1500, 30), (2500, 45), (3500, 60)):
        if queue_min <= limit:
            return minutes
    return 90


def eta_tier(eta_hours: float) -> int:
    if eta_hours <= 0.5:
        return 10
    if eta_hours <= 1:
        return 20
    if eta_hours <= 2:
        return 30
    if eta_hours <= 4:
        return 60
    if eta_hours <= 8:
        return 90
    return 120


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min", type=int, default=10, help="lower clamp in minutes")
    parser.add_argument("--max", type=int, default=180, help="upper clamp in minutes")
    ns = parser.parse_args()

    history = _load_history()
    rows = _load_rows()

    queue_min = None
    if history:
        for point in reversed(history):
            if isinstance(point.get("queue_min"), int):
                queue_min = point["queue_min"]
                break
    if queue_min is None:
        positions = [row.get("queue_position") for row in rows
                     if isinstance(row.get("queue_position"), int)]
        queue_min = min(positions) if positions else None

    # observed drain rate over the last few checkpoints (positions per hour,
    # positive = the queue is moving toward the front)
    rate = None
    recent = [point for point in history[-10:] if isinstance(point.get("queue_min"), int)]
    if len(recent) >= 2:
        first, last = recent[0], recent[-1]
        t0, t1 = _parse_ts(first.get("checked_at")), _parse_ts(last.get("checked_at"))
        if t0 and t1:
            hours = (t1 - t0).total_seconds() / 3600.0
            if hours >= 0.15:
                rate = round((first["queue_min"] - last["queue_min"]) / hours, 1)

    eta_hours = None
    if queue_min is not None and rate and rate > 1:
        eta_hours = round(queue_min / rate, 2)

    # any attempt already handed to a worker -> poll soon regardless of position.
    # Attempts that already have a score are history, not work in flight.
    imminent = []
    for row in rows:
        if row.get("display_score") is not None:
            continue
        results = row.get("results") if isinstance(row.get("results"), dict) else {}
        if results.get("harbor_status") == "scored":
            continue
        stage = str(row.get("stage") or "")
        state = str(row.get("scoring_state") or "")
        if stage and stage != "queued":
            imminent.append(str(row.get("attempt_id")))
        elif state and state not in ("queued", "failed", "scored", ""):
            imminent.append(str(row.get("attempt_id")))

    reasons: list[str] = []
    if imminent:
        interval = 10
        reasons.append("attempt(s) already handed to a worker: %s" % ", ".join(imminent[:6]))
    elif queue_min is None:
        interval = 60
        reasons.append("no queue position available; conservative default")
    else:
        pos_tier = position_tier(queue_min)
        reasons.append("queue_min=%d -> position tier %d min" % (queue_min, pos_tier))
        if eta_hours is not None:
            e_tier = eta_tier(eta_hours)
            reasons.append("observed drain %.1f pos/h -> ETA %.1fh -> ETA tier %d min"
                           % (rate, eta_hours, e_tier))
            # Time-based estimate beats raw position once the drain rate is known: a
            # position of 2255 moving at 26 pos/h is ~3.5 days away, so polling it every
            # 45 minutes is pure waste. Closeness only *tightens* the interval.
            interval = e_tier
            if queue_min <= 300:
                interval = min(interval, 20)
                reasons.append("queue_min <= 300 tightens to <=20 min")
            elif queue_min <= 800:
                interval = min(interval, 45)
                reasons.append("queue_min <= 800 tightens to <=45 min")
        else:
            reasons.append("drain rate unknown (%d history point(s)); position tier only"
                           % len(recent))
    interval = max(ns.min, min(ns.max, int(round(interval / 5.0) * 5)))

    now = datetime.now().replace(microsecond=0)
    nxt = now + timedelta(minutes=interval)
    # round up to the next 5-minute boundary so the delivery time is tidy
    extra = (-nxt.minute) % 5
    nxt = (nxt + timedelta(minutes=extra)).replace(second=0)
    if nxt <= now:
        nxt = now + timedelta(minutes=5)

    rrule = ("DTSTART;TZID=Asia/Shanghai:%s\nRRULE:FREQ=MINUTELY;INTERVAL=%d;UNTIL=%s"
             % (nxt.strftime("%Y%m%dT%H%M%S"), interval, UNTIL_UTC))
    out = {
        "now_local": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "queue_min": queue_min,
        "queue_max": next((point.get("queue_max") for point in reversed(history)
                           if point.get("queue_max") is not None), None),
        "still_queued": next((point.get("still_queued") for point in reversed(history)
                              if point.get("still_queued") is not None), None),
        "rate_per_hour": rate,
        "eta_hours": eta_hours,
        "imminent_attempts": imminent[:6],
        "recommended_interval_min": interval,
        "next_dtstart_local": nxt.strftime("%Y-%m-%dT%H:%M:%S"),
        "rrule": rrule,
        "reason": "; ".join(reasons),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
