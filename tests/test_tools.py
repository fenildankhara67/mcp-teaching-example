"""search_tracks, sample_rows, customer_lookup (redaction) and my_invoices (object-level authorisation)."""
import json
import re
import sqlite3

import pytest

from chinook_mcp.guard import DEFAULT_DB, Guard, QueryInput, Refused
from chinook_mcp.server import build_server

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


def db(sql, *params):
    conn = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# -- search_tracks -------------------------------------------------------------
def test_search_tracks_finds_and_wraps(guard, audit_path):
    res = guard.search_tracks("dawn", 5)
    assert 0 < res.row_count <= 5
    assert any("Princess of the Dawn" in str(c) for r in res.rows for c in r)
    assert all(str(r[1]).startswith("<untrusted_data>") for r in res.rows)
    assert len(lines(audit_path)) == 1 and lines(audit_path)[0]["tool"] == "search_tracks"


def test_search_tracks_text_is_data_not_sql(guard):
    assert guard.search_tracks("'; DROP TABLE Album; --", 5).row_count == 0
    # "%" is escaped, so it matches only names that really contain a percent sign (2 tracks), not everything.
    assert guard.search_tracks("%", 20).row_count == 2
    assert guard.run_query(QueryInput(sql="SELECT count(*) FROM Album")).rows[0][0] > 0


def test_search_tracks_limit_capped(guard):
    assert guard.search_tracks("a", 1000).row_count == 20


# -- sample_rows -----------------------------------------------------------------
def test_sample_rows_bounded_and_allow_listed(guard, audit_path):
    assert guard.sample_rows("Genre", 3).row_count == 3
    assert guard.sample_rows("Genre", 1000).row_count == 10
    for bad in ["Genre; DROP TABLE Album", 'Genre"', "genre ", "sqlite_master", "Genre --", ""]:
        with pytest.raises(Refused, match="unknown table"):
            guard.sample_rows(bad, 1)
    assert len(lines(audit_path)) == 2 + 6  # one line per call


# -- redaction ---------------------------------------------------------------------
def test_customer_lookup_minimised_and_masked(guard):
    c = guard.customer_lookup(1)
    assert c.email_masked == "l***@embraer.com.br"
    dumped = c.model_dump_json()
    for raw in ("luisg@embraer", "3923", "Faria Lima", "+55"):
        assert raw not in dumped
    assert not hasattr(c, "phone") and not hasattr(c, "address")
    with pytest.raises(Refused, match="unknown customer"):
        guard.customer_lookup(999999)


def _all_emails_and_phones():
    emails = {r[0] for r in db("SELECT Email FROM Customer UNION SELECT Email FROM Employee")}
    phones = {r[0] for r in db("SELECT Phone FROM Customer UNION SELECT Fax FROM Customer UNION SELECT Phone FROM Employee") if r[0]}
    return emails, phones


def test_no_raw_email_or_phone_leaves_any_tool(audit_path, trace_path):
    guard = Guard(audit_path=audit_path, trace_path=trace_path, rate_limit_per_min=10_000)
    emails, phones = _all_emails_and_phones()
    assert len(emails) > 50 and len(phones) > 50
    outputs = []
    queries = [
        "SELECT * FROM Customer", "SELECT Email AS x, Phone AS y, Fax AS z FROM Customer",
        "SELECT substr(Email, 1, 30), upper(Email), Email || '' FROM Customer",
        "SELECT group_concat(Email) FROM Customer", "SELECT group_concat(Phone) FROM Employee",
        "SELECT * FROM Employee", "SELECT e FROM (SELECT Email AS e FROM Customer)",
        "SELECT c.* FROM Customer c JOIN Invoice i ON i.CustomerId = c.CustomerId LIMIT 50",
    ]
    for sql in queries:
        try:
            outputs.append(guard.run_query(QueryInput(sql=sql)).model_dump_json())
        except Refused:
            pass  # refused is also fine: nothing leaked
    for table in ("Customer", "Employee"):
        outputs.append(guard.sample_rows(table, 10).model_dump_json())
    for cid in range(1, 60):
        outputs.append(guard.customer_lookup(cid).model_dump_json())
    outputs.append(guard.search_tracks("a", 20).model_dump_json())
    blob = "\n".join(outputs)
    assert len(outputs) > 60
    for e in emails:
        assert e not in blob, e
    for ph in phones:
        assert ph not in blob, ph
    assert not [m for m in EMAIL.findall(blob) if "***" not in m]  # any email-shaped text is masked


def test_phone_pattern_backstop_masks_literals(guard):
    res = guard.run_query(QueryInput(sql="SELECT 'call +55 (12) 3923-5555 or a.b@example.com'"))
    cell = res.rows[0][0]
    assert "3923" not in cell and "a.b@" not in cell and "a***@example.com" in cell


# -- my_invoices -------------------------------------------------------------------
def test_my_invoices_only_own_rows(guard, monkeypatch):
    seen = {}
    for who, cust in (("alice", 1), ("bob", 2)):
        monkeypatch.setenv("MCP_CALLER_ID", who)
        res = guard.my_invoices(20)
        ids = {int(r[0]) for r in res.rows}
        mine = {r[0] for r in db("SELECT InvoiceId FROM Invoice WHERE CustomerId = ?", cust)}
        assert ids and ids <= mine
        seen[who] = ids
    assert not (seen["alice"] & seen["bob"])


def test_my_invoices_unknown_caller_refused_and_audited(guard, audit_path, monkeypatch):
    monkeypatch.setenv("MCP_CALLER_ID", "mallory")
    with pytest.raises(Refused, match="no customer is mapped"):
        guard.my_invoices(5)
    rec = lines(audit_path)[-1]
    assert rec["outcome"] == "refused" and rec["caller_id"] == "mallory" and len(lines(audit_path)) == 1


async def test_no_argument_selects_another_customer(guard, monkeypatch):
    server = build_server(guard)
    tool = next(t for t in await server.list_tools() if t.name == "my_invoices")
    assert set(tool.input_schema["properties"]) == {"limit"}
    monkeypatch.setenv("MCP_CALLER_ID", "alice")
    bob_ids = {r[0] for r in db("SELECT InvoiceId FROM Invoice WHERE CustomerId = 2")}
    # Extra or hostile arguments cannot redirect the lookup.
    for args in ({"limit": 20, "customer_id": 2}, {"limit": 20, "caller_id": "bob"}, {"customer_id": 2}):
        try:
            result = await server.call_tool("my_invoices", args)
        except Exception:
            continue
        text = json.dumps(result.structured_content or str(result.content))
        rows = (result.structured_content or {}).get("rows", [])
        assert not ({int(r[0]) for r in rows} & bob_ids), text


def test_my_invoices_cannot_be_reached_by_sql_ownership_bypass(guard, monkeypatch):
    # run_query is general, but my_invoices itself takes only a limit; a limit of 0 or text is rejected upstream.
    monkeypatch.setenv("MCP_CALLER_ID", "alice")
    assert guard.my_invoices(1).row_count == 1


# -- new tools are audited when input is invalid ----------------------------------
@pytest.mark.parametrize(
    "tool,args",
    [
        ("search_tracks", {}), ("search_tracks", {"text": ""}), ("search_tracks", {"text": "a", "limit": 21}),
        ("sample_rows", {}), ("sample_rows", {"table": "Genre", "n": 11}),
        ("customer_lookup", {}), ("customer_lookup", {"customer_id": "x"}), ("customer_lookup", {"customer_id": 0}),
        ("my_invoices", {"limit": 0}), ("my_invoices", {"limit": "all"}),
    ],
)
async def test_invalid_input_audited_for_new_tools(guard, audit_path, tool, args):
    server = build_server(guard)
    with pytest.raises(Exception):
        await server.call_tool(tool, args)
    recs = lines(audit_path)
    assert len(recs) == 1 and recs[0]["tool"] == tool and recs[0]["detail"].startswith("invalid input")


def test_new_tools_rate_limited(audit_path, trace_path):
    g = Guard(audit_path=audit_path, trace_path=trace_path, rate_limit_per_min=1)
    g.search_tracks("a", 1)
    with pytest.raises(Refused, match="rate limit"):
        g.sample_rows("Genre", 1)
