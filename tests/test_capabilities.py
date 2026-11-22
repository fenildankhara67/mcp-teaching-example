"""One audit line per call, cost estimate wording, explain_query, audit_tail, rate limit, schema resource."""
import json

import pytest

from chinook_mcp.guard import Guard, QueryInput, Refused
from chinook_mcp.server import build_server

CTE = "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c) SELECT count(*) FROM c"
CROSS = "SELECT count(*) FROM Track a, Track b"


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


# -- one audit line per call -------------------------------------------------
def test_describe_table_one_line_ok(guard, audit_path, trace_path):
    res = guard.describe_table("Album")
    recs = lines(audit_path)
    assert len(recs) == 1 and recs[0]["outcome"] == "ok" and recs[0]["tool"] == "describe_table"
    assert recs[0]["trace_id"] == res.trace_id
    assert any(s["trace_id"] == res.trace_id for s in lines(trace_path))


def test_describe_table_one_line_unknown_table(guard, audit_path, trace_path):
    with pytest.raises(Refused, match="unknown table") as ei:
        guard.describe_table("NoSuchTable")
    recs = lines(audit_path)
    assert len(recs) == 1 and recs[0]["outcome"] == "refused" and recs[0]["detail"] == "unknown table: NoSuchTable"
    assert recs[0]["trace_id"] == ei.value.trace_id
    assert any(s["trace_id"] == ei.value.trace_id for s in lines(trace_path))


def test_every_tool_writes_exactly_one_line(guard, audit_path):
    calls = [
        lambda: guard.list_tables(),
        lambda: guard.run_query(QueryInput(sql="SELECT 1")),
        lambda: guard.explain_query("SELECT 1"),
        lambda: guard.audit_tail(5),
        lambda: guard.schema_overview(),
    ]
    for i, fn in enumerate(calls, 1):
        fn()
        assert len(lines(audit_path)) == i


# -- cost estimate -------------------------------------------------------------
def test_recursive_cte_says_unbounded_not_a_number(guard, audit_path):
    with pytest.raises(Refused) as ei:
        guard.run_query(QueryInput(sql=CTE))
    msg = str(ei.value)
    assert "unbounded or unknown size" in msg
    assert "75,951,225" not in msg and "about" not in msg
    assert lines(audit_path)[-1]["outcome"] == "refused"


def test_cross_join_still_refused_with_a_number(guard):
    with pytest.raises(Refused, match=r"about [\d,]+ rows") as ei:
        guard.run_query(QueryInput(sql=CROSS))
    assert "12,271,009" in str(ei.value)  # 3503 x 3503: the alias resolved to Track


def test_alias_without_index_resolves_to_real_table(guard):
    a = guard.explain_query("SELECT * FROM Genre g")
    assert a.allowed and a.estimated_rows == 25


# -- explain_query -------------------------------------------------------------
def test_explain_allowed_does_not_execute(guard, audit_path):
    r = guard.explain_query("SELECT Name FROM Track WHERE TrackId = 5")
    assert r.allowed and r.statement_type == "select" and r.plan and r.estimated_rows == 0
    assert r.trace_id == lines(audit_path)[-1]["trace_id"]


def test_explain_refusals_reported_not_raised(guard, audit_path):
    cross = guard.explain_query(CROSS)
    assert not cross.allowed and cross.estimated_rows == 12_271_009 and "too expensive" in cross.reason
    cte = guard.explain_query(CTE)
    assert not cte.allowed and cte.estimated_rows is None and "unbounded or unknown size" in cte.reason
    drop = guard.explain_query("DROP TABLE Album")
    assert not drop.allowed and drop.statement_type == "drop"
    multi = guard.explain_query("SELECT 1; DROP TABLE Album")
    assert not multi.allowed
    assert len(lines(audit_path)) == 4  # one line per call


# -- audit_tail ----------------------------------------------------------------
def test_audit_tail_only_own_entries(guard, monkeypatch):
    monkeypatch.setenv("MCP_CALLER_ID", "alice")
    guard.run_query(QueryInput(sql="SELECT 'alice-secret-query'"))
    monkeypatch.setenv("MCP_CALLER_ID", "bob")
    guard.run_query(QueryInput(sql="SELECT 2"))
    guard.list_tables()
    tail = guard.audit_tail(20)
    assert tail.caller_id == "bob" and [e.tool for e in tail.entries] == ["run_query", "list_tables"]
    monkeypatch.setenv("MCP_CALLER_ID", "alice")
    a = guard.audit_tail(20)
    assert [e.tool for e in a.entries] == ["run_query"]
    assert "bob" not in a.model_dump_json()


def test_audit_tail_no_sql_and_capped(guard, audit_path):
    for i in range(25):
        guard.run_query(QueryInput(sql=f"SELECT {i}"))
    with pytest.raises(Refused):
        guard.run_query(QueryInput(sql="DROP TABLE Album"))
    tail = guard.audit_tail(1000)
    assert len(tail.entries) == 20
    dumped = tail.model_dump_json()
    assert "DROP" not in dumped and "SELECT" not in dumped
    assert all(len(e.sql_sha256) == 64 for e in tail.entries[:-1] if e.sql_sha256)
    assert len(guard.audit_tail(0).entries) == 1  # clamped to at least 1


# -- rate limit ------------------------------------------------------------------
def test_rate_limit_refuses_audits_and_traces(audit_path, trace_path):
    g = Guard(audit_path=audit_path, trace_path=trace_path, rate_limit_per_min=3)
    for _ in range(3):
        g.list_tables()
    with pytest.raises(Refused, match="rate limit exceeded") as ei:
        g.list_tables()
    last = lines(audit_path)[-1]
    assert last["outcome"] == "refused" and "rate limit" in last["detail"]
    assert last["trace_id"] == ei.value.trace_id
    names = {s["name"] for s in lines(trace_path) if s["trace_id"] == ei.value.trace_id}
    assert {"tool_call", "rate_limit_check"} <= names
    assert len(lines(audit_path)) == 4


def test_rate_limit_is_per_caller(audit_path, trace_path, monkeypatch):
    g = Guard(audit_path=audit_path, trace_path=trace_path, rate_limit_per_min=1)
    monkeypatch.setenv("MCP_CALLER_ID", "a")
    g.list_tables()
    monkeypatch.setenv("MCP_CALLER_ID", "b")
    g.list_tables()  # b has its own budget
    monkeypatch.setenv("MCP_CALLER_ID", "a")
    with pytest.raises(Refused, match="rate limit"):
        g.list_tables()


def test_rate_limit_env_var(audit_path, trace_path, monkeypatch):
    monkeypatch.setenv("CHINOOK_RATE_LIMIT_PER_MIN", "2")
    assert Guard(audit_path=audit_path, trace_path=trace_path).rate_limit == 2


# -- schema resource ---------------------------------------------------------------
async def test_schema_resource_listed_and_read_in_process(guard):
    server = build_server(guard)
    assert [str(r.uri) for r in await server.list_resources()] == ["chinook://schema"]
    contents = await server.read_resource("chinook://schema")
    text = contents[0].content if hasattr(contents[0], "content") else str(contents)
    schema = json.loads(text)
    assert "Album" in schema["tables"]


# -- input validation failures are audited too -----------------
@pytest.mark.parametrize(
    "tool,args",
    [
        ("run_query", {"sql": ""}),
        ("run_query", {"sql": "x" * 2001}),
        ("run_query", {}),
        ("run_query", {"sql": 123}),
        ("describe_table", {}),
        ("explain_query", {}),
        ("audit_tail", {"n": "many"}),
    ],
)
async def test_invalid_input_one_audit_line_and_trace(guard, audit_path, trace_path, tool, args):
    server = build_server(guard)
    with pytest.raises(Exception):
        await server.call_tool(tool, args)
    recs = lines(audit_path)
    assert len(recs) == 1
    r = recs[0]
    assert r["tool"] == tool and r["outcome"] == "refused" and r["detail"].startswith("invalid input")
    assert r["caller_id"] and r["rows"] == 0
    assert "x" * 50 not in audit_path.read_text()
    assert any(s["trace_id"] == r["trace_id"] and s["name"] == "tool_call" for s in lines(trace_path))
    if "sql" in args and isinstance(args["sql"], str):
        assert len(r["sql_sha256"]) == 64
    else:
        assert r["sql_sha256"] is None


async def test_invalid_calls_are_rate_limited(audit_path, trace_path):
    g = Guard(audit_path=audit_path, trace_path=trace_path, rate_limit_per_min=2)
    server = build_server(g)
    for _ in range(4):
        with pytest.raises(Exception):
            await server.call_tool("run_query", {"sql": ""})
    recs = lines(audit_path)
    assert len(recs) == 4  # still one line per call
    assert [r["detail"].startswith("invalid input") for r in recs] == [True, True, False, False]
    assert "rate limit" in recs[-1]["detail"]
