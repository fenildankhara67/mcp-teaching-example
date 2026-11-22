#!/usr/bin/env python3
"""Scripted demo (about a second): a normal question, a DROP attempt, an expensive cross join,
a recursive CTE, a planted-instruction row, the purpose-built tools (search, customer lookup with masked email, my_invoices), explain_query (dry run) on the cross join, audit_tail,
then the audit log and the matching trace ids.

Run from the project root:  .venv/bin/python scripts/demo.py
Uses temporary audit and span files, so it does not touch your real logs.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import seed_local  # noqa: E402

if not seed_local.LOCAL.exists():
    seed_local.seed()

tmp = Path(tempfile.mkdtemp(prefix="chinook_demo_"))
os.environ["MCP_CALLER_ID"] = "demo-user"
from chinook_mcp.guard import Guard, QueryInput, Refused  # noqa: E402

guard = Guard(audit_path=tmp / "audit.jsonl", trace_path=tmp / "trace.jsonl")


def step(title: str, sql: str) -> None:
    print(f"\n=== {title}\n> {sql}")
    try:
        res = guard.run_query(QueryInput(sql=sql))
        print(f"OK  rows={res.row_count}  trace_id={res.trace_id}")
        for row in res.rows[:3]:
            print("   ", row)
    except Refused as e:
        print(f"REFUSED  {str(e)[:300]}")


step("1. Normal question", "SELECT Name, Milliseconds FROM Track WHERE TrackId = 5")
step("2. DROP attempt", "DROP TABLE Album")
step("3. Expensive cross join", "SELECT count(*) FROM Track a, Track b")
step("4. Recursive CTE (size unknown)", "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c) SELECT count(*) FROM c")
step("5. Planted instruction in a notes row", "SELECT Body FROM TrackNote WHERE NoteId = 3")

print("\n=== 6. Purpose-built tools (no SQL written by the model)")
hit = guard.search_tracks("dawn", 2)
print(f"search_tracks('dawn')  rows={hit.row_count}  first={hit.rows[0][1:3]}")
cust = guard.customer_lookup(1)
print(f"customer_lookup(1)     email_masked={cust.email_masked}  (address, phone, fax never returned)")
raw = guard.run_query(QueryInput(sql="SELECT CustomerId, Email, Phone FROM Customer WHERE CustomerId = 1"))
print(f"run_query on Email/Phone columns -> {raw.rows[0]}  (read as NULL by the authorizer)")
inv = guard.my_invoices(2)
print(f"my_invoices (caller demo-user = customer 3)  rows={inv.row_count}  invoice ids={[r[0] for r in inv.rows]}")
os.environ["MCP_CALLER_ID"] = "stranger"
try:
    guard.my_invoices(2)
except Refused as e:
    print(f"my_invoices as unmapped caller -> REFUSED  {str(e)[:70]}")
os.environ["MCP_CALLER_ID"] = "demo-user"

print("\n=== 7. explain_query (dry run, nothing executed)")
ex = guard.explain_query("SELECT count(*) FROM Track a, Track b")
print(f"allowed={ex.allowed}  type={ex.statement_type}  estimated_rows={ex.estimated_rows:,}")
for line in ex.plan:
    print("   plan:", line)

print("\n=== 8. audit_tail (this caller only; hash and outcome, never SQL)")
for e in guard.audit_tail(3).entries:
    print(f"{e.outcome:8} {e.tool:14} sha256={e.sql_sha256[:12] if e.sql_sha256 else None}  trace_id={e.trace_id}")

print("\n=== Audit log file")
audit = [json.loads(x) for x in (tmp / "audit.jsonl").read_text().splitlines()]
for r in audit:
    print(f"{r['outcome']:8} {r['trace_id']}  {r['detail'][:60]}")

print("\n=== Spans for the refused cross join (matching trace id)")
tid = audit[2]["trace_id"]
for s in (json.loads(x) for x in (tmp / "trace.jsonl").read_text().splitlines()):
    if s["trace_id"] == tid:
        ms = (s["end_ns"] - s["start_ns"]) / 1e6
        print(f"{tid}  {s['name']:16} {s['status']:6} {ms:6.2f} ms")
print(f"\nLogs written to {tmp}")
