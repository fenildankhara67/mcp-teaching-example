"""End to end: launch the server as a subprocess over stdio, list tools and resources, call every tool once."""
import json
import re
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
TOOLS = {
    "list_tables", "describe_table", "run_query", "explain_query", "audit_tail",
    "search_tracks", "sample_rows", "customer_lookup", "my_invoices",
    "get_trace", "recent_failures", "explain_refusal", "call_stats",
}


async def test_stdio_end_to_end(tmp_path):
    audit = tmp_path / "a.jsonl"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "chinook_mcp.server"],
        cwd=str(ROOT),
        env={"MCP_CALLER_ID": "alice", "CHINOOK_AUDIT_LOG": str(audit),
             "CHINOOK_TRACE_LOG": str(tmp_path / "t.jsonl")},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            assert {t.name for t in (await session.list_tools()).tools} == TOOLS
            resources = (await session.list_resources()).resources
            assert [str(r.uri) for r in resources] == ["chinook://schema"]
            body = (await session.read_resource("chinook://schema")).contents[0].text
            schema = json.loads(body)
            assert "Track" in schema["tables"] and "TrackNote" in schema["tables"]
            assert "Body" in {c["name"] for c in schema["tables"]["TrackNote"]}
            assert "Princess" not in body  # schema only, no rows

            assert not (await session.call_tool("list_tables", {})).is_error
            assert not (await session.call_tool("describe_table", {"table": "Album"})).is_error
            unknown = await session.call_tool("describe_table", {"table": "Nope"})
            assert unknown.is_error
            ok = await session.call_tool("run_query", {"sql": "SELECT count(*) AS n FROM Artist"})
            assert not ok.is_error
            bad = await session.call_tool("run_query", {"sql": "DROP TABLE Album"})
            assert bad.is_error
            ex = await session.call_tool("explain_query", {"sql": "SELECT count(*) FROM Track a, Track b"})
            assert not ex.is_error and ex.structured_content["allowed"] is False
            assert not (await session.call_tool("search_tracks", {"text": "dawn", "limit": 3})).is_error
            assert not (await session.call_tool("sample_rows", {"table": "Genre", "n": 2})).is_error
            cust = await session.call_tool("customer_lookup", {"customer_id": 1})
            assert not cust.is_error and "luisg@embraer" not in str(cust.structured_content)
            assert not (await session.call_tool("my_invoices", {"limit": 2})).is_error
            good_tid = ok.structured_content["trace_id"]
            bad_tid = re.search(r"trace_id=([0-9a-f]{32})", bad.content[0].text).group(1)
            tr = await session.call_tool("get_trace", {"trace_id": good_tid})
            assert not tr.is_error and tr.structured_content["entry"]["tool"] == "run_query"
            assert not (await session.call_tool("recent_failures", {"n": 5})).is_error
            why = await session.call_tool("explain_refusal", {"trace_id": bad_tid})
            assert not why.is_error and why.structured_content["known_reason"] is True
            assert not (await session.call_tool("call_stats", {})).is_error
            tail = await session.call_tool("audit_tail", {"n": 50})
            assert not tail.is_error
            entries = tail.structured_content["entries"]
            assert 0 < len(entries) <= 20
            assert all("sql" not in e and "detail" not in e for e in entries)

    lines = [json.loads(x) for x in audit.read_text().splitlines()]
    # Every tool call and the schema resource read: one line each, in call order.
    assert [r["tool"] for r in lines] == [
        "schema_resource", "list_tables", "describe_table", "describe_table",
        "run_query", "run_query", "explain_query",
        "search_tracks", "sample_rows", "customer_lookup", "my_invoices",
        "get_trace", "recent_failures", "explain_refusal", "call_stats", "audit_tail",
    ]


async def test_stdio_every_call_one_audit_line_including_malformed(tmp_path):
    """14 calls over stdio (valid, refused and malformed) give exactly 14 audit lines."""
    audit = tmp_path / "a.jsonl"
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "chinook_mcp.server"], cwd=str(ROOT),
        env={"MCP_CALLER_ID": "stdio-14", "CHINOOK_AUDIT_LOG": str(audit),
             "CHINOOK_TRACE_LOG": str(tmp_path / "t.jsonl")},
    )
    calls = [
        ("list_tables", {}),
        ("describe_table", {"table": "Album"}),
        ("describe_table", {"table": "Nope"}),
        ("describe_table", {}),
        ("run_query", {"sql": "SELECT 1"}),
        ("run_query", {"sql": "DROP TABLE Album"}),
        ("run_query", {"sql": ""}),
        ("run_query", {"sql": "y" * 2001}),
        ("run_query", {}),
        ("run_query", {"sql": 42}),
        ("explain_query", {"sql": "SELECT 1"}),
        ("explain_query", {}),
        ("audit_tail", {"n": 5}),
        ("audit_tail", {"n": "x"}),
    ]
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for name, args in calls:
                await session.call_tool(name, args)
    recs = [json.loads(x) for x in audit.read_text().splitlines()]
    assert len(recs) == 14
    assert [r["tool"] for r in recs] == [c[0] for c in calls]
    assert all(r["caller_id"] == "stdio-14" and r["trace_id"] for r in recs)
    assert sum(r["detail"].startswith("invalid input") for r in recs) == 7
    assert "yyyyyyyyyy" not in audit.read_text()
