import json
import time

import pytest

from chinook_mcp.guard import DEFAULT_DB, Guard, QueryInput, Refused
from chinook_mcp.server import build_server
from tests.conftest import file_hash


def q(sql):
    return QueryInput(sql=sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE Album",
        "INSERT INTO Artist (ArtistId, Name) VALUES (9999, 'x')",
        "UPDATE Artist SET Name = 'x'",
        "DELETE FROM Artist",
        "WITH x AS (SELECT 1) DELETE FROM Artist",
        "SELECT 1; DROP TABLE Album",
        "PRAGMA writable_schema = 1",
        "ATTACH DATABASE ':memory:' AS other",
    ],
)
def test_write_attempts_refused_and_file_unchanged(guard, sql):
    before = file_hash(DEFAULT_DB)
    with pytest.raises(Refused):
        guard.run_query(q(sql))
    assert file_hash(DEFAULT_DB) == before


def test_connection_is_really_read_only(guard):
    # Bypass the authorizer: the mode=ro connection alone must still refuse writes.
    import sqlite3

    conn = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM Artist")
    conn.close()


def test_row_limit_enforced(audit_path, trace_path):
    g = Guard(audit_path=audit_path, trace_path=trace_path, row_cap=5)
    res = g.run_query(q("SELECT * FROM Track"))
    assert res.row_count == 5 and res.truncated


def test_timeout_enforced(audit_path, trace_path):
    # Cost guard disabled here so the timeout (the last line of defence) is what stops the query.
    g = Guard(audit_path=audit_path, trace_path=trace_path, timeout=0.3, max_scan_rows=10**30)
    sql = "SELECT count(*) FROM Track a, Track b, Track c, Track d"
    start = time.monotonic()
    with pytest.raises(Refused, match="timed out"):
        g.run_query(q(sql))
    assert time.monotonic() - start < 3


def test_normal_tools_work(guard):
    assert "Album" in guard.list_tables().tables
    assert guard.describe_table("Album").columns[0].name == "AlbumId"
    assert guard.run_query(q("SELECT Name FROM Artist LIMIT 3")).row_count == 3


def test_describe_unknown_table_refused(guard):
    with pytest.raises(Refused):
        guard.describe_table("Album; DROP TABLE Album")


async def test_no_registered_tool_can_mutate():
    tools = await build_server().list_tools()
    assert {t.name for t in tools} == {
        "list_tables", "describe_table", "run_query", "explain_query", "audit_tail",
        "search_tracks", "sample_rows", "customer_lookup", "my_invoices",
        "get_trace", "recent_failures", "explain_refusal", "call_stats",
    }
    forbidden_verbs = ("write", "insert", "update", "delete", "drop", "exec", "create", "alter", "remove", "set")
    forbidden_params = ("path", "file", "filename", "command", "cmd", "shell", "script", "url")
    assert len(tools) == 13
    expected_args = {
        "list_tables": set(), "describe_table": {"table"}, "run_query": {"sql"}, "explain_query": {"sql"},
        "audit_tail": {"n"}, "search_tracks": {"text", "limit"}, "sample_rows": {"table", "n"},
        "customer_lookup": {"customer_id"}, "my_invoices": {"limit"},
        "get_trace": {"trace_id"}, "recent_failures": {"n"}, "explain_refusal": {"trace_id"}, "call_stats": set(),
    }
    for t in tools:
        assert set(t.input_schema.get("properties", {})) == expected_args[t.name]
        assert t.annotations.read_only_hint is True
        assert t.annotations.destructive_hint is False
        assert not any(w in t.name for w in forbidden_verbs)
        props = t.input_schema.get("properties", {})
        for pname, pdef in props.items():
            assert pname.lower() not in forbidden_params, f"{t.name} accepts {pname}"
            assert pdef.get("format") not in ("path", "file-path", "uri")
        assert not any(w in (t.description or "").lower() for w in ("shell command", "file path"))
        # The only free-text argument is SQL, and it is checked by the guard (see write-attempt tests).
        assert set(props) <= {"table", "sql", "n", "text", "limit", "customer_id", "trace_id"}


def read_log(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_audit_records_ok_and_refused_with_caller(guard, audit_path, monkeypatch):
    monkeypatch.setenv("MCP_CALLER_ID", "student-1")
    guard.run_query(q("SELECT 1"))
    with pytest.raises(Refused):
        guard.run_query(q("DROP TABLE Album"))
    ok, refused = read_log(audit_path)
    assert ok["outcome"] == "ok" and refused["outcome"] == "refused"
    for rec in (ok, refused):
        assert rec["caller_id"] == "student-1"
        assert rec["tool"] == "run_query"
        assert len(rec["sql_sha256"]) == 64
    assert "DROP" not in audit_path.read_text()  # only the hash is stored, not the SQL


def test_audit_default_caller(guard, audit_path, monkeypatch):
    monkeypatch.delenv("MCP_CALLER_ID", raising=False)
    guard.list_tables()
    assert read_log(audit_path)[0]["caller_id"] == "anonymous"
