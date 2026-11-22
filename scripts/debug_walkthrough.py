#!/usr/bin/env python3
"""Scripted walkthrough (about a second): debug an assistant's own tool calls using the SYNTHETIC sample session.

Starts from "the assistant gave a wrong answer", then uses call_stats, recent_failures, get_trace and
explain_refusal to find the repeated expensive query and the blocked DROP.
Works on a temporary copy of sample_session/, so the sample files are never changed.

Run:  .venv/bin/python scripts/debug_walkthrough.py
"""
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import make_sample_session  # noqa: E402

if not (make_sample_session.OUT / "audit.jsonl").exists():
    make_sample_session.write()
os.environ.pop("CHINOOK_AUDIT_LOG", None)
os.environ.pop("CHINOOK_TRACE_LOG", None)
os.environ["MCP_CALLER_ID"] = make_sample_session.CALLER

from chinook_mcp.guard import Guard, Refused  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="chinook_walkthrough_"))
shutil.copy(make_sample_session.OUT / "audit.jsonl", tmp / "audit.jsonl")
shutil.copy(make_sample_session.OUT / "trace.jsonl", tmp / "trace.jsonl")
guard = Guard(audit_path=tmp / "audit.jsonl", trace_path=tmp / "trace.jsonl")

print("SYNTHETIC sample session (not real data). Caller:", make_sample_session.CALLER)
print('\nProblem: "the assistant gave a wrong answer". What did it call, and what did the server do?')

print("\n=== Step 1. call_stats: what did it call?")
st = guard.call_stats()
print(f"window: {st.window}")
print(f"total calls: {st.total_calls}")
for tool, s in st.by_tool.items():
    print(f"  {tool:15} {dict(s.outcomes)}  p50={s.p50_ms} ms  p95={s.p95_ms} ms")
print("-> run_query has several refused calls. Look at the failures.")

print("\n=== Step 2. recent_failures: which calls were refused?")
fails = guard.recent_failures(20).failures
for f in fails:
    h = f.sql_sha256[:10] if f.sql_sha256 else "-"
    print(f"  {f.ts[11:19]} {f.tool:14} {f.outcome:8} sql#{h}  {f.detail[:60]}")
repeats = Counter(f.sql_sha256 for f in fails if f.sql_sha256)
hash_, times = repeats.most_common(1)[0]
print(f"-> the same SQL hash (sql#{hash_[:10]}) was refused {times} times: the assistant retried one query unchanged.")
first = next(f for f in fails if f.sql_sha256 == hash_)
drop = next(f for f in fails if f.detail.startswith("only SELECT"))

print("\n=== Step 3. get_trace: what did the server do with the first retry?")
tr = guard.get_trace(first.trace_id)
print(f"  tool={tr.entry.tool} outcome={tr.entry.outcome}")
for sp in tr.spans:
    print(f"  span {sp.name:17} {sp.status:6} {sp.duration_ms:6.3f} ms")
print("-> it stopped at sql_guard_check; there is no query_execution span, so nothing ran.")

print("\n=== Step 4. explain_refusal: why, and what should the assistant try?")
for label, f in (("expensive query", first), ("DROP attempt", drop)):
    ex = guard.explain_refusal(f.trace_id)
    print(f"  [{label}] {ex.explanation}\n    next step: {ex.next_step}")

print("\n=== Step 5. An unknown trace id, or one belonging to another caller, gets one and the same refusal")
for tid in ("0" * 32, "f" * 32):
    try:
        guard.get_trace(tid)
    except Refused as e:
        print(f"  {tid[:8]}...: {str(e).split(' [')[0]}")

print("\nFinding: the early answer came from a query that counted tracks, not sales; the assistant then retried a refused")
print("cross join 3 times, read a planted instruction without acting on it, and its DROP was blocked.")
print(f"(Walkthrough worked on a temporary copy: {tmp})")
