"""Loop guard: warn at the 2nd identical refused query, block from the 3rd, reset, isolation, expiry."""
import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from chinook_mcp.guard import LOOP_DETAIL, Guard, QueryInput, Refused, sql_hash
from chinook_mcp.server import build_server

CROSS = "SELECT count(*) FROM Track a, Track b"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def lg(audit_path, trace_path, clock):
    return Guard(audit_path=audit_path, trace_path=trace_path, clock=clock, rate_limit_per_min=1000)


def refuse(g, sql=CROSS):
    with pytest.raises(Refused) as ei:
        g.run_query(QueryInput(sql=sql))
    return ei.value


def test_first_refusal_is_normal_second_warns_third_blocks(lg, audit_path):
    first = refuse(lg)
    assert "too expensive" in first.reason and "already refused once" not in first.reason
    second = refuse(lg)
    assert "too expensive" in second.reason and "change it instead of retrying" in second.reason
    assert "explain_refusal" in second.reason and second.trace_id in second.reason
    third = refuse(lg)
    assert third.reason.startswith("repeated refused query (loop guard)")
    for word in ("Stop retrying", "explain_refusal", "search_tracks", "sample_rows", "explain_query"):
        assert word in third.reason
    assert refuse(lg).reason.startswith("repeated refused query")  # and later ones
    recs = lines(audit_path)
    assert len(recs) == 4  # one audit line per call, blocked or not
    assert recs[2]["detail"] == LOOP_DETAIL == recs[3]["detail"] and recs[2]["outcome"] == "refused"
    assert "already refused once" not in recs[1]["detail"]  # audit keeps the plain reason
    assert len({r["sql_sha256"] for r in recs}) == 1


def test_blocked_call_does_not_touch_the_database(lg, monkeypatch):
    refuse(lg)
    refuse(lg)
    def boom(*a, **k):
        raise AssertionError("database was opened for a blocked call")
    monkeypatch.setattr(lg, "_connect", boom)
    assert refuse(lg).reason.startswith("repeated refused query")


def test_blocked_call_is_audited_and_traced(lg, audit_path, trace_path):
    refuse(lg)
    refuse(lg)
    err = refuse(lg)
    rec = lines(audit_path)[-1]
    assert rec["trace_id"] == err.trace_id and rec["detail"] == LOOP_DETAIL
    names = [s["name"] for s in lines(trace_path) if s["trace_id"] == err.trace_id]
    assert "loop_guard_check" in names and "tool_call" in names
    assert "sql_guard_check" not in names and "query_execution" not in names  # checks were not re-run


def test_changed_query_resets_the_streak(lg):
    refuse(lg)
    refuse(lg)
    assert lg.run_query(QueryInput(sql="SELECT 1")).row_count == 1  # an allowed, different query
    again = refuse(lg)  # back to a first refusal
    assert "already refused once" not in again.reason and "loop guard" not in again.reason


def test_different_refused_query_starts_its_own_streak(lg):
    refuse(lg)
    refuse(lg)
    other = refuse(lg, "SELECT count(*) FROM Track x, Track y, Track z")
    assert "loop guard" not in other.reason and "already refused once" not in other.reason


def test_identical_query_that_is_now_allowed_resets(audit_path, trace_path, clock):
    g = Guard(audit_path=audit_path, trace_path=trace_path, clock=clock, max_scan_rows=1000, rate_limit_per_min=1000)
    sql = "SELECT * FROM Track"
    refuse(g, sql)
    g.max_scan_rows = 10**9  # the same text is now allowed
    assert g.run_query(QueryInput(sql=sql)).row_count > 0
    g.max_scan_rows = 1000
    assert "already refused once" not in refuse(g, sql).reason  # counter was reset by the success


def test_per_caller_isolation(lg, monkeypatch):
    monkeypatch.setenv("MCP_CALLER_ID", "alice")
    refuse(lg)
    refuse(lg)
    assert refuse(lg).reason.startswith("repeated refused query")
    monkeypatch.setenv("MCP_CALLER_ID", "bob")
    first = refuse(lg)
    assert "loop guard" not in first.reason and "already refused once" not in first.reason


def test_window_expiry_uses_injected_clock(lg, clock):
    refuse(lg)
    refuse(lg)
    assert refuse(lg).reason.startswith("repeated refused query")
    clock.t += 121  # past the 120 second window
    after = refuse(lg)
    assert "loop guard" not in after.reason and "already refused once" not in after.reason


def test_thresholds_are_configurable(audit_path, trace_path, clock, monkeypatch):
    g = Guard(audit_path=audit_path, trace_path=trace_path, clock=clock, loop_warn=3, loop_block=5, rate_limit_per_min=1000)
    for _ in range(3):
        refuse(g)
    assert all("loop guard" not in refuse(g).reason for _ in range(1))  # 4th still runs
    assert refuse(g).reason.startswith("repeated refused query")  # 5th blocked
    monkeypatch.setenv("CHINOOK_LOOP_WINDOW", "5")
    monkeypatch.setenv("CHINOOK_LOOP_BLOCK", "4")
    e = Guard(audit_path=audit_path, trace_path=trace_path)
    assert (e.loop_window, e.loop_block) == (5.0, 4)


def test_counters_hold_hashes_only(lg):
    refuse(lg)
    dumped = repr(lg._loops)
    assert sql_hash(CROSS) in dumped and "Track" not in dumped


def test_other_tools_in_between_do_not_reset(lg):
    refuse(lg)
    lg.explain_query(CROSS)
    lg.audit_tail(3)
    second = refuse(lg)
    assert "already refused once" in second.reason


def test_blocked_reason_explained_and_visible_in_debug_tools(lg):
    refuse(lg), refuse(lg)
    err = refuse(lg)
    ex = lg.explain_refusal(err.trace_id)
    assert ex.known_reason and "loop guard" in ex.explanation.lower()
    f = lg.recent_failures(5).failures
    assert f[-1].detail == LOOP_DETAIL and f[-1].trace_id == err.trace_id
    assert lg.call_stats().by_tool["run_query"].outcomes == {"refused": 3}


def test_no_new_tool_added():
    # The loop guard adds no tool: the tool list is unchanged at 13 (the over-privilege test checks the details).
    import asyncio
    assert len(asyncio.run(build_server().list_tools())) == 13


async def test_stdio_loop_path(tmp_path):
    audit = tmp_path / "a.jsonl"
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "chinook_mcp.server"],
        env={"MCP_CALLER_ID": "loopy", "CHINOOK_AUDIT_LOG": str(audit), "CHINOOK_TRACE_LOG": str(tmp_path / "t.jsonl")},
        cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
    )
    texts = []
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for _ in range(3):
                res = await s.call_tool("run_query", {"sql": CROSS})
                assert res.is_error
                texts.append(res.content[0].text)
            import re
            tid = re.search(r"trace_id=([0-9a-f]{32})", texts[2]).group(1)
            why = await s.call_tool("explain_refusal", {"trace_id": tid})
            assert not why.is_error and why.structured_content["known_reason"] is True
            ok = await s.call_tool("search_tracks", {"text": "dawn", "limit": 2})
            assert not ok.is_error
    assert "too expensive" in texts[0] and "already refused once" in texts[1]
    assert "repeated refused query (loop guard)" in texts[2]
    recs = lines(audit)
    assert [x["tool"] for x in recs] == ["run_query"] * 3 + ["explain_refusal", "search_tracks"]
    assert recs[2]["detail"] == LOOP_DETAIL
