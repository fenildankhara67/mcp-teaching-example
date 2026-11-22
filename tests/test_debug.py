"""Caller-scoped debugging tools and the synthetic sample session."""
import importlib.util
import json
import math
import shutil
from pathlib import Path

import pytest

from chinook_mcp.guard import NO_SUCH_TRACE, REFUSAL_REASONS, Guard, QueryInput, Refused
from chinook_mcp.server import build_server

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("make_sample_session", ROOT / "scripts" / "make_sample_session.py")
sample = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sample)


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


@pytest.fixture
def two_callers(guard, monkeypatch):
    """alice and bob each make one good and one refused call. Returns their trace ids."""
    ids = {}
    for who in ("alice", "bob"):
        monkeypatch.setenv("MCP_CALLER_ID", who)
        good = guard.run_query(QueryInput(sql=f"SELECT '{who}'")).trace_id
        with pytest.raises(Refused) as ei:
            guard.run_query(QueryInput(sql="DROP TABLE Album"))
        ids[who] = (good, ei.value.trace_id)
    monkeypatch.setenv("MCP_CALLER_ID", "alice")
    return ids


# -- isolation -------------------------------------------------------------------
def test_get_trace_own_ok_foreign_and_unknown_same_refusal(guard, two_callers):
    alice_good, _ = two_callers["alice"]
    bob_good, bob_bad = two_callers["bob"]
    t = guard.get_trace(alice_good)
    assert t.entry.trace_id == alice_good and any(s.name == "tool_call" for s in t.spans)
    messages = []
    for tid in (bob_good, bob_bad, "0" * 32):
        with pytest.raises(Refused) as ei:
            guard.get_trace(tid)
        messages.append(ei.value.reason)
    assert messages == [NO_SUCH_TRACE] * 3


def test_explain_refusal_foreign_and_unknown_same_refusal(guard, two_callers):
    _, bob_bad = two_callers["bob"]
    msgs = []
    for tid in (bob_bad, "1" * 32):
        with pytest.raises(Refused) as ei:
            guard.explain_refusal(tid)
        msgs.append(ei.value.reason)
    assert msgs == [NO_SUCH_TRACE] * 2


def test_recent_failures_and_stats_only_own(guard, two_callers, monkeypatch):
    _, alice_bad = two_callers["alice"]
    _, bob_bad = two_callers["bob"]
    f = guard.recent_failures(20)
    assert [x.trace_id for x in f.failures] == [alice_bad]
    assert bob_bad not in f.model_dump_json()
    st = guard.call_stats()
    assert st.caller_id == "alice" and st.total_calls == 3  # run_query x2 and recent_failures; bob's calls excluded
    assert st.by_tool["run_query"].outcomes == {"ok": 1, "refused": 1}


async def test_no_argument_reaches_another_callers_data(guard, two_callers, monkeypatch):
    bob_good, bob_bad = two_callers["bob"]
    server = build_server(guard)
    for tool, args in [
        ("get_trace", {"trace_id": bob_good}), ("get_trace", {"trace_id": bob_bad, "caller_id": "bob"}),
        ("explain_refusal", {"trace_id": bob_bad}), ("recent_failures", {"n": 20, "caller_id": "bob"}),
        ("call_stats", {"caller_id": "bob"}),
    ]:
        try:
            res = await server.call_tool(tool, args)
        except Exception:
            continue  # refused: nothing leaked
        assert bob_good not in str(res) and bob_bad not in str(res), tool
    assert all("caller_id" not in t.input_schema.get("properties", {}) for t in await server.list_tools())


def test_get_trace_returns_no_sql_text(guard):
    res = guard.run_query(QueryInput(sql="SELECT 'secret-marker-text'"))
    dumped = guard.get_trace(res.trace_id).model_dump_json()
    assert "secret-marker-text" not in dumped and "SELECT" not in dumped


# -- explain_refusal -----------------------------------------------------------------
def test_explain_refusal_known_reasons(guard):
    cases = {"DROP TABLE Album": "SELECT", "SELECT count(*) FROM Track a, Track b": "WHERE",
             "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c) SELECT count(*) FROM c": "bounded"}
    for sql, word in cases.items():
        with pytest.raises(Refused) as ei:
            guard.run_query(QueryInput(sql=sql))
        ex = guard.explain_refusal(ei.value.trace_id)
        assert ex.known_reason and word in (ex.explanation + ex.next_step)
    with pytest.raises(Refused) as ei:
        guard.describe_table("Nope")
    assert "list_tables" in guard.explain_refusal(ei.value.trace_id).next_step


def test_explain_refusal_unknown_reason_and_non_refused(guard, audit_path):
    # Plant an audit line with a reason that is not in the lookup table.
    rec = {"ts": "2026-01-01T00:00:00+00:00", "trace_id": "a" * 32, "caller_id": "anonymous", "tool": "run_query",
           "sql_sha256": None, "outcome": "refused", "detail": "something new", "rows": 0}
    audit_path.write_text(json.dumps(rec) + "\n")
    ex = guard.explain_refusal("a" * 32)
    assert ex.known_reason is False and "not in the server's fixed lookup table" in ex.explanation
    ok = guard.run_query(QueryInput(sql="SELECT 1"))
    with pytest.raises(Refused, match="not refused"):
        guard.explain_refusal(ok.trace_id)


def test_every_lookup_prefix_is_distinct_and_documented():
    prefixes = [p for p, _, _ in REFUSAL_REASONS]
    assert len(prefixes) == len(set(prefixes))
    assert all(m and n for _, m, n in REFUSAL_REASONS)


# -- sample session ---------------------------------------------------------------------
def test_sample_generation_is_deterministic_and_labelled(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    sample.write(a)
    sample.write(b)
    for name in ("audit.jsonl", "trace.jsonl", "README.txt"):
        assert (a / name).read_bytes() == (b / name).read_bytes()
    assert (a / "README.txt").read_text().startswith("SYNTHETIC")
    assert all(r["synthetic"] is True for f in ("audit.jsonl", "trace.jsonl") for r in lines(a / f))
    # The committed sample matches a fresh generation.
    assert (ROOT / "sample_session" / "audit.jsonl").read_bytes() == (a / "audit.jsonl").read_bytes()


def nearest_rank(vals, p):
    v = sorted(vals)
    return v[max(0, math.ceil(p / 100 * len(v)) - 1)]


def test_call_stats_match_sample_session(tmp_path, monkeypatch):
    sample.write(tmp_path)
    monkeypatch.setenv("MCP_CALLER_ID", sample.CALLER)
    monkeypatch.delenv("CHINOOK_AUDIT_LOG", raising=False)
    g = Guard(audit_path=tmp_path / "audit.jsonl", trace_path=tmp_path / "trace.jsonl")
    audit, spans = lines(tmp_path / "audit.jsonl"), lines(tmp_path / "trace.jsonl")  # read before call_stats audits itself
    st = g.call_stats()
    assert st.total_calls == len(audit) == 32
    assert st.by_tool["run_query"].outcomes == {"ok": 7, "refused": 6}
    assert st.by_tool["describe_table"].outcomes == {"ok": 6, "refused": 1}
    assert sum(sum(t.outcomes.values()) for t in st.by_tool.values()) == 32
    assert sum(t.outcomes.get("refused", 0) for t in st.by_tool.values()) == 7
    for tool, t in st.by_tool.items():
        d = [(s["end_ns"] - s["start_ns"]) / 1e6 for s in spans if s["name"] == "tool_call" and s["attributes"]["tool"] == tool]
        assert t.p50_ms == round(nearest_rank(d, 50), 3) and t.p95_ms == round(nearest_rank(d, 95), 3)
    fails = g.recent_failures(20).failures
    assert len(fails) == 7
    cross = [f for f in fails if f.detail.startswith("query plan too expensive")]
    assert len(cross) == 3 and len({f.sql_sha256 for f in cross}) == 1  # the repeated expensive query
    assert any(f.detail.startswith("only SELECT") for f in fails)  # the blocked DROP
    tr = g.get_trace(cross[0].trace_id)
    assert "query_execution" not in {s.name for s in tr.spans} and "sql_guard_check" in {s.name for s in tr.spans}


def test_walkthrough_runs_on_a_copy(tmp_path):
    import subprocess, sys
    before = (ROOT / "sample_session" / "audit.jsonl").read_bytes()
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "debug_walkthrough.py")], capture_output=True,
                         text=True, cwd=ROOT, check=True).stdout
    assert "wrong answer" in out and "refused 3 times" in out and "only SELECT" in out
    assert (ROOT / "sample_session" / "audit.jsonl").read_bytes() == before


# -- the new tools go through the shared path ------------------------------------------
@pytest.mark.parametrize(
    "tool,args",
    [("get_trace", {}), ("get_trace", {"trace_id": "not-hex"}), ("explain_refusal", {"trace_id": 5}),
     ("recent_failures", {"n": 21}), ("recent_failures", {"n": 0})],
)
async def test_invalid_input_audited_for_debug_tools(guard, audit_path, tool, args):
    with pytest.raises(Exception):
        await build_server(guard).call_tool(tool, args)
    recs = lines(audit_path)
    assert len(recs) == 1 and recs[0]["tool"] == tool and recs[0]["detail"].startswith("invalid input")


def test_debug_tools_one_line_each_and_rate_limited(audit_path, trace_path):
    g = Guard(audit_path=audit_path, trace_path=trace_path, rate_limit_per_min=4)
    g.call_stats()
    g.recent_failures(3)
    with pytest.raises(Refused):
        g.get_trace("0" * 32)
    g.call_stats()
    with pytest.raises(Refused, match="rate limit"):
        g.recent_failures(1)
    recs = lines(audit_path)
    assert [r["tool"] for r in recs] == ["call_stats", "recent_failures", "get_trace", "call_stats", "recent_failures"]
