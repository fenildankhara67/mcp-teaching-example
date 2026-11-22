#!/usr/bin/env python3
"""Generate a SYNTHETIC sample session: an audit log and a span file for one fake caller.

Nothing here is real data or a real run. The numbers, ids and timings come from a fixed random seed.
The story (about 30 tool calls): an assistant answers a question about top-selling artists, gets it wrong at first,
retries an expensive cross join three times, reads a planted instruction in a note, tries a DROP,
hits the rate limit once, and ends with a correct answer.

Run:  .venv/bin/python scripts/make_sample_session.py      (writes sample_session/)
"""
from __future__ import annotations

import hashlib
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "sample_session"
CALLER = "synthetic-assistant"
SEED = 20261121
BASE = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)

CROSS = "SELECT count(*) FROM Track a, Track b"
COST = ("query plan too expensive: full scans are estimated to visit about 12,271,009 rows (limit 100,000). "
        "Plan: SCAN a USING COVERING INDEX IFK_TrackMediaTypeId; SCAN b USING COVERING INDEX IFK_TrackMediaTypeId. "
        "Add a WHERE on an indexed column or join on keys instead of a cross join.")
WRONG = ("SELECT ar.Name, count(*) AS n FROM Track t JOIN Album al ON al.AlbumId = t.AlbumId "
         "JOIN Artist ar ON ar.ArtistId = al.ArtistId GROUP BY ar.ArtistId ORDER BY n DESC LIMIT 3")
RIGHT = ("SELECT ar.Name, sum(il.UnitPrice * il.Quantity) AS revenue FROM InvoiceLine il "
         "JOIN Track t ON t.TrackId = il.TrackId JOIN Album al ON al.AlbumId = t.AlbumId "
         "JOIN Artist ar ON ar.ArtistId = al.ArtistId GROUP BY ar.ArtistId ORDER BY revenue DESC LIMIT 3")

# (tool, sql-or-argument text used for the hash, outcome, detail, rows)
OK, REF = "ok", "refused"
STORY = [
    ("list_tables", None, OK, "", 11),
    ("describe_table", "Invoice", OK, "", 9),
    ("describe_table", "InvoiceLine", OK, "", 5),
    ("describe_table", "Artist", OK, "", 2),
    ("describe_table", "Album", OK, "", 3),
    ("describe_table", "Track", OK, "", 9),
    ("search_tracks", "rock", OK, "", 10),
    ("run_query", WRONG, OK, "", 3),  # counts tracks, not sales: the later wrong answer
    ("run_query", CROSS, REF, COST, 0),
    ("run_query", CROSS, REF, COST, 0),
    ("run_query", CROSS, REF, COST, 0),
    ("audit_tail", None, OK, "", 8),
    ("sample_rows", "TrackNote", OK, "", 3),
    ("run_query", "SELECT Body FROM TrackNote", OK, "", 3),  # contains the planted instruction
    ("run_query", "DROP TABLE Album", REF, "only SELECT statements are allowed", 0),
    ("describe_table", "Sales", REF, "unknown table: Sales", 0),
    ("run_query", "", REF, "invalid input (arguments given: sql)", 0),
] + [
    ("sample_rows", "InvoiceLine", OK, "", 5),
    ("sample_rows", "Invoice", OK, "", 5),
    ("run_query", "SELECT count(*) FROM InvoiceLine", OK, "", 1),
    ("run_query", "SELECT count(*) FROM Invoice", OK, "", 1),
    ("search_tracks", "dawn", OK, "", 2),
    ("describe_table", "Genre", OK, "", 2),
    ("run_query", "SELECT Name FROM Genre", OK, "", 25),
    ("run_query", "SELECT sum(Total) FROM Invoice", OK, "", 1),
    ("explain_query", RIGHT, OK, "would allow", 0),
    ("list_tables", None, OK, "", 11),
    ("search_tracks", "love", OK, "", 10),
    ("sample_rows", "Artist", OK, "", 5),
    ("explain_query", WRONG, OK, "would allow", 0),
]  # 30 calls inside one minute
RATE = ("run_query", RIGHT, REF, "rate limit exceeded: 30 calls per minute per caller", 0)
FINAL = ("run_query", RIGHT, OK, "", 3)


def sha(text: str | None) -> str | None:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None


def _generate(plan, rng, base=BASE) -> tuple[list[dict], list[dict]]:
    """plan: list of ((tool, arg, outcome, detail, rows), gap_seconds_since_previous_call)."""
    audit, spans = [], []
    t = base
    for (tool, arg, outcome, detail, rows), gap in plan:
        t += timedelta(seconds=gap)
        tid = format(rng.getrandbits(128), "032x")
        start = int(t.timestamp() * 1e9)
        heavy = outcome == OK and tool in ("run_query", "sample_rows", "search_tracks")
        total_ms = rng.uniform(1.5, 6.0) if heavy else rng.uniform(0.3, 1.2)
        status = "UNSET" if outcome == OK else "ERROR"
        # Root span plus children; refused calls stop at the check that refused them.
        children = [("caller_check", 0.01)]
        rate_blocked = detail.startswith("rate limit")
        invalid = detail.startswith("invalid input")
        if not invalid:
            children.append(("rate_limit_check", 0.01))
        if not invalid and not rate_blocked and tool in ("run_query", "explain_query", "search_tracks", "sample_rows"):
            children.append(("sql_guard_check", rng.uniform(0.2, 0.4)))
        if outcome == OK and heavy:
            children.append(("query_execution", total_ms * 0.6))
        cursor = start + 1000  # children begin just after the root span starts
        root_id = format(rng.getrandbits(64), "016x")
        for name, ms in children:
            dur = int(ms * 1e6)
            spans.append({
                "synthetic": True, "trace_id": tid, "span_id": format(rng.getrandbits(64), "016x"),
                "parent_span_id": root_id, "name": name, "start_ns": cursor, "end_ns": cursor + dur,
                "status": "UNSET", "attributes": {},
            })
            cursor += dur
        spans.append({
            "synthetic": True, "trace_id": tid, "span_id": root_id, "parent_span_id": None, "name": "tool_call",
            "start_ns": start, "end_ns": start + int(total_ms * 1e6), "status": status, "attributes": {"tool": tool},
        })
        audit.append({
            "synthetic": True, "ts": t.isoformat(), "trace_id": tid, "caller_id": CALLER, "tool": tool,
            "sql_sha256": sha(arg), "outcome": outcome, "detail": detail, "rows": rows,
        })
    return audit, spans


def build() -> tuple[list[dict], list[dict]]:
    assert len(STORY) == 30
    plan = [(c, 1.5) for c in STORY] + [(RATE, 1.5), (FINAL, 70.0)]
    return _generate(plan, random.Random(SEED))


# -- four-window story for the repeated-review demo (also SYNTHETIC) --------------------------------------
HSEED = 20261122
RECURSIVE = "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c) SELECT count(*) FROM c"
CTE_COST = ("query plan too expensive to allow: c has unbounded or unknown size, so no row estimate is given. "
            "Plan: CO-ROUTINE c; SETUP; SCAN CONSTANT ROW; RECURSIVE STEP; SCAN c. Use a bounded query over real tables.")
DROP = ("run_query", "DROP TABLE Album", REF, "only SELECT statements are allowed", 0)
CROSS_REF = ("run_query", CROSS, REF, COST, 0)
CTE_REF = ("run_query", RECURSIVE, REF, CTE_COST, 0)
# Calls per 15-minute window (minutes 0-15, 15-30, 30-45, 45-60). The assistant retries unchanged queries in
# windows 1 to 3 and changes approach in window 4.
HOURLY = [
    [("list_tables", None, OK, "", 11), ("describe_table", "Invoice", OK, "", 9), ("describe_table", "Track", OK, "", 9),
     ("search_tracks", "rock", OK, "", 10), ("run_query", WRONG, OK, "", 3), CROSS_REF, CROSS_REF],
    [CROSS_REF, ("sample_rows", "Genre", OK, "", 5), CROSS_REF, CTE_REF, DROP, ("search_tracks", "dawn", OK, "", 2)],
    [CTE_REF, ("sample_rows", "Artist", OK, "", 5), CROSS_REF, ("describe_table", "Genre", OK, "", 2),
     ("run_query", "SELECT count(*) FROM Invoice", OK, "", 1)],
    [("explain_query", RIGHT, OK, "would allow", 0), ("run_query", RIGHT, OK, "", 3),
     ("search_tracks", "love", OK, "", 10), ("sample_rows", "InvoiceLine", OK, "", 5),
     ("run_query", "SELECT sum(Total) FROM Invoice", OK, "", 1)],
]
HOURLY_LABELS = {
    sha(CROSS): "cross join of Track with itself",
    sha(RECURSIVE): "recursive count (no bound)",
    sha("DROP TABLE Album"): "DROP TABLE Album",
}


def build_hourly() -> tuple[list[dict], list[dict]]:
    plan, prev = [], 0.0
    for w, calls in enumerate(HOURLY):
        step = 14 * 60 / len(calls)
        for i, call in enumerate(calls):
            at = w * 900 + 30 + i * step
            plan.append((call, at - prev))
            prev = at
    return _generate(plan, random.Random(HSEED))


README = """SYNTHETIC SAMPLE SESSION. Not real data and not a real run.

Generated by scripts/make_sample_session.py with a fixed random seed. One fake caller ("synthetic-assistant"),
32 tool calls over the public Chinook music-store sample, telling a short story for a debugging walkthrough.
Every line of audit.jsonl and trace.jsonl carries "synthetic": true. Timings are invented.
hourly_audit.jsonl and hourly_trace.jsonl are a second SYNTHETIC story spread over four 15-minute windows
(used by scripts/repeat_review_demo.py).
Regenerate with: .venv/bin/python scripts/make_sample_session.py
"""


def write(out: Path = OUT) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    audit, spans = build()
    (out / "audit.jsonl").write_text("".join(json.dumps(r) + "\n" for r in audit))
    (out / "trace.jsonl").write_text("".join(json.dumps(r) + "\n" for r in spans))
    h_audit, h_spans = build_hourly()
    (out / "hourly_audit.jsonl").write_text("".join(json.dumps(r) + "\n" for r in h_audit))
    (out / "hourly_trace.jsonl").write_text("".join(json.dumps(r) + "\n" for r in h_spans))
    (out / "README.txt").write_text(README)
    return out


if __name__ == "__main__":
    print(write())
    sys.exit(0)
