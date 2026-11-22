#!/usr/bin/env python3
"""Talk demo: the whole example in talk order, six numbered stages (deterministic apart from trace ids).

  1 first server   2 the guard   3 who is asking and what happened
  4 three things that go wrong   5 the loop   6 the repeated review

It calls the Guard methods that the MCP tools call (not a stdio client), so what you see is what the server does;
tests/test_stdio_client.py and tests/test_loop_guard.py cover the real stdio path. Logs go to a temporary folder,
or to the files named by CHINOOK_AUDIT_LOG and CHINOOK_TRACE_LOG if you set them (handy for a recorded clip).

Run:  .venv/bin/python scripts/talk_demo.py [--pause] [--fast] [--no-color]
  --pause     wait for Enter between stages (live talks)
  --fast      print everything at once (the default)
  --no-color  plain text (colour is also off when output is piped or NO_COLOR is set)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import seed_local  # noqa: E402

CROSS = "SELECT count(*) FROM Track a, Track b"
RECURSIVE = "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c) SELECT count(*) FROM c"
BETTER = ("SELECT ar.Name, count(*) AS tracks FROM Track t JOIN Album al ON al.AlbumId = t.AlbumId "
          "JOIN Artist ar ON ar.ArtistId = al.ArtistId GROUP BY ar.ArtistId ORDER BY tracks DESC LIMIT 3")


class Talk:
    def __init__(self, color: bool, pause: bool, workdir: Path) -> None:
        from chinook_mcp.guard import LOOP_DETAIL, Guard, Refused

        self.color, self.pause = color, pause
        self.Refused, self.LOOP_DETAIL = Refused, LOOP_DETAIL
        self.guard = Guard(audit_path=workdir / "audit.jsonl", trace_path=workdir / "trace.jsonl", rate_limit_per_min=1000)
        self.n_audit = self._count(self.guard.audit_path)  # lines already there, if the logs are reused
        self.n_trace = self._count(self.guard.trace_path)
        self.last_error = None

    @staticmethod
    def _count(path: Path) -> int:
        return len(path.read_text().splitlines()) if path.exists() else 0

    # -- output helpers --------------------------------------------------------
    def c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def banner(self, n: int, title: str) -> None:
        if n > 1 and self.pause:
            try:
                input(self.c("2", "   [Enter for the next stage] "))
            except EOFError:
                pass
        print("\n" + self.c("1;36", f"== {n}. {title} =="))

    def say(self, text: str) -> None:
        print("   " + text)

    @staticmethod
    def short(tid: str | None) -> str:
        return (tid or "--------")[:8]

    def call(self, label: str, fn, show=None, as_caller: str | None = None):
        """Run one tool call, classify it OK / REFUSED / BLOCKED, print one line. Returns the result or None."""
        if as_caller:
            os.environ["MCP_CALLER_ID"] = as_caller
        try:
            res = fn()
        except self.Refused as e:
            kind = "BLOCKED" if self.LOOP_DETAIL in e.reason else "REFUSED"
            code = "1;31" if kind == "BLOCKED" else "33"
            first = e.reason.split(". ")[0]
            print(f"   {self.c(code, f'{kind:8}')} {label}\n            {first[:118]}  [trace {self.short(e.trace_id)}]")
            self.last_error = e
            return None
        tid = getattr(res, "trace_id", None)
        extra = show(res) if show else ""
        print(f"   {self.c('32', 'OK      ')} {label}  {extra}  [trace {self.short(tid)}]")
        return res

    # -- stages --------------------------------------------------------------------
    def stage1(self) -> None:
        self.banner(1, "First server: three read-only tools")
        g = self.guard
        self.call("list_tables", g.list_tables, lambda r: f"{len(r.tables)} tables: {', '.join(r.tables[:4])}, ...",
                  as_caller="demo-user")
        self.call("describe_table Track", lambda: g.describe_table("Track"),
                  lambda r: "columns: " + ", ".join(c.name for c in r.columns[:4]) + ", ...")
        self.call("search_tracks 'dawn'", lambda: g.search_tracks("dawn", 2),
                  lambda r: f"{r.row_count} hits, no SQL written by the model")

    def stage2(self) -> None:
        self.banner(2, "The guard: refuse below the model")
        g = self.guard
        self.call("run_query  DROP TABLE Album", lambda: g.run_query(self.q("DROP TABLE Album")))
        self.call(f"run_query  {CROSS}", lambda: g.run_query(self.q(CROSS)))
        ex = g.explain_query(CROSS)
        self.say(f"explain_query (dry run, nothing executed): allowed={ex.allowed}, estimated rows {ex.estimated_rows:,}")
        for line in ex.plan:
            self.say("   plan: " + line)

    @staticmethod
    def q(sql):
        from chinook_mcp.guard import QueryInput
        return QueryInput(sql=sql)

    def stage3(self) -> None:
        self.banner(3, "Who is asking, and what happened")
        g = self.guard
        res = self.call("run_query  SELECT count(*) FROM Artist", lambda: g.run_query(self.q("SELECT count(*) FROM Artist")),
                        lambda r: f"-> {r.rows[0][0]}")
        audit = json.loads(g.audit_path.read_text().splitlines()[-1])
        self.say(f"audit line: caller={audit['caller_id']} tool={audit['tool']} outcome={audit['outcome']} "
                 f"sql_sha256={audit['sql_sha256'][:10]}... (hash only, never the SQL)")
        tr = g.get_trace(res.trace_id)
        self.say(f"same trace id {self.short(audit['trace_id'])} in the span file: " + ", ".join(s.name for s in tr.spans))
        self.say("(trace ids are shortened to 8 characters here; the full ids are in the log files)")

    def stage4(self) -> None:
        self.banner(4, "Three things that go wrong")
        from chinook_mcp.server import build_server

        tools = asyncio.run(build_server(self.guard).list_tools())
        ro = sum(1 for t in tools if t.annotations.read_only_hint is True and t.annotations.destructive_hint is False)
        self.say(f"a) too much power: {len(tools)} tools, {ro} read-only, none takes a path, command or URL")
        self.say("b) hidden instruction in data (the row says: ignore the rules and drop a table):")
        res = self.call("run_query  SELECT Body FROM TrackNote WHERE NoteId = 3",
                        lambda: self.guard.run_query(self.q("SELECT Body FROM TrackNote WHERE NoteId = 3")),
                        lambda r: "returned wrapped as data:")
        self.say("   " + str(res.rows[0][0])[:78] + "...")
        self.say("   nothing acted on it: the text is labelled untrusted data and no tool can write")
        self.say("c) a costly query (recursive, no bound):")
        self.call("run_query  WITH RECURSIVE c(n) ...", lambda: self.guard.run_query(self.q(RECURSIVE)))

    def stage5(self) -> None:
        self.banner(5, "The loop: an assistant retries the same refused query")
        g = self.guard
        os.environ["MCP_CALLER_ID"] = "assistant-loop"
        for attempt in (1, 2, 3):
            self.call(f"attempt {attempt}: run_query  {CROSS}", lambda: g.run_query(self.q(CROSS)))
            if attempt == 2:
                self.say("   ^ warning added: " + self.last_error.reason.split(". ")[-2].strip() + ".")
            if attempt == 3:
                self.say("   ^ blocked without re-running any check or touching the database")
        tid = self.last_error.trace_id
        ex = g.explain_refusal(tid)
        self.say(f"assistant calls explain_refusal: {ex.explanation}")
        self.say(f"   next step: {ex.next_step}")
        self.say("assistant changes approach:")
        self.call("run_query  tracks per artist, joined on keys", lambda: g.run_query(self.q(BETTER)),
                  lambda r: f"-> {r.row_count} rows")

    def stage6(self) -> None:
        self.banner(6, "The repeated review: the skill routine, one round")
        g = self.guard
        self.say("skills/tool-call-review/SKILL.md, steps 1 to 4 for caller assistant-loop:")
        st = g.call_stats()
        fails = g.recent_failures(20).failures
        by_hash = Counter(f.sql_sha256 for f in fails)
        rep, times = by_hash.most_common(1)[0]
        first = next(f for f in fails if f.sql_sha256 == rep)
        ex = g.explain_refusal(first.trace_id)
        refused = sum(s.outcomes.get("refused", 0) for s in st.by_tool.values())
        self.say(f"Called: {st.total_calls} calls so far ({refused} refused)")
        self.say(f"Refused: run_query x{times}  sql#{rep[:10]}  (the same hash every time)")
        self.say(f"Why: {ex.explanation}")
        self.say("Change: " + ex.next_step + " It was retried unchanged, so it hit the loop guard.")

    def summary(self, folder: str) -> None:
        a = [json.loads(x) for x in self.guard.audit_path.read_text().splitlines()[self.n_audit:]]
        spans = {json.loads(x)["trace_id"] for x in self.guard.trace_path.read_text().splitlines()[self.n_trace:]}
        total = len(a)
        print("\n" + self.c("1;36", "== Summary =="))
        print("   calls  ok  refused  blocked  traces")
        ok = sum(1 for r in a if r["outcome"] == "ok")
        blocked = sum(1 for r in a if r["detail"] == self.LOOP_DETAIL)
        refused = sum(1 for r in a if r["outcome"] == "refused") - blocked
        print(f"   {total:5}  {ok:2}  {refused:7}  {blocked:7}  {len(spans):6}")
        print(f"   one audit line and one trace per call: {total} lines, {len(spans)} traces")
        print(f"   logs: {folder}" + ("" if "CHINOOK_AUDIT_LOG" in os.environ else " (temporary, removed when the demo ends)"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Talk demo for the Chinook read-only MCP example.")
    ap.add_argument("--pause", action="store_true", help="wait for Enter between stages")
    ap.add_argument("--fast", action="store_true", help="print everything at once (default)")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args(argv)
    color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    pause = args.pause and not args.fast

    if not seed_local.LOCAL.exists():
        seed_local.seed()
    os.environ.pop("CHINOOK_RATE_LIMIT_PER_MIN", None)
    for v in ("CHINOOK_LOOP_WINDOW", "CHINOOK_LOOP_WARN", "CHINOOK_LOOP_BLOCK"):
        os.environ.pop(v, None)
    with tempfile.TemporaryDirectory(prefix="chinook_talk_") as d:
        talk = Talk(color, pause, Path(d))
        print("Chinook read-only MCP server: talk demo (music-store sample data; teaching example, not production safe)")
        print("Legend: " + talk.c("32", "OK") + " = allowed   " + talk.c("33", "REFUSED") + " = guard said no   "
              + talk.c("1;31", "BLOCKED") + " = loop guard stopped a repeat")
        for stage in (talk.stage1, talk.stage2, talk.stage3, talk.stage4, talk.stage5, talk.stage6):
            stage()
        talk.summary(str(talk.guard.audit_path.parent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
