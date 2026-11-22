"""MCP server exposing thirteen read-only tools and one schema resource over the Chinook sample database.

Run over stdio:  python -m chinook_mcp.server
"""
from __future__ import annotations

import json
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from .guard import (
    AuditTail, CallStatsResult, CustomerInfo, FailureList, RefusalExplanation, TraceInfo, ExplainResult, Guard, QueryInput, QueryResult, TableList, TableSchema,
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)


class AuditedMCPServer(MCPServer):
    """MCPServer that audits calls rejected by argument validation.

    The SDK validates arguments before the tool function runs, so the guard never sees a bad call.
    This is the one place that intercepts it: call_tool catches the validation error and asks the
    guard for one audit line and one trace.
    """

    guard: Guard

    async def call_tool(self, name, arguments, context=None):
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            if isinstance(exc.__cause__, ValidationError):
                self.guard.audit_invalid(name, arguments)
            raise


def build_server(guard: Guard | None = None) -> MCPServer:
    guard = guard or Guard()
    mcp = AuditedMCPServer("chinook-readonly")
    mcp.guard = guard

    @mcp.tool(annotations=READ_ONLY)
    def list_tables() -> TableList:
        """List the tables in the music-store database."""
        return guard.list_tables()

    @mcp.tool(annotations=READ_ONLY)
    def describe_table(table: str) -> TableSchema:
        """Show column names and types for one table (schema only, no rows)."""
        return guard.describe_table(table)

    @mcp.tool(annotations=READ_ONLY)
    def run_query(sql: str) -> QueryResult:
        """Run one SELECT statement. Results are capped and time-limited."""
        return guard.run_query(QueryInput(sql=sql))

    @mcp.tool(annotations=READ_ONLY)
    def explain_query(sql: str) -> ExplainResult:
        """Dry run: say whether run_query would allow this SQL and why. Does not execute it."""
        return guard.explain_query(sql)

    @mcp.tool(annotations=READ_ONLY)
    def audit_tail(n: int = 10) -> AuditTail:
        """Your own last N audit entries (at most 20): hash and outcome only, never SQL text."""
        return guard.audit_tail(n)

    @mcp.tool(annotations=READ_ONLY)
    def search_tracks(
        text: Annotated[str, Field(min_length=1, max_length=50)],
        limit: Annotated[int, Field(ge=1, le=20)] = 10,
    ) -> QueryResult:
        """Find tracks by part of a track or artist name (at most 20 results). No SQL needed."""
        return guard.search_tracks(text, limit)

    @mcp.tool(annotations=READ_ONLY)
    def sample_rows(table: str, n: Annotated[int, Field(ge=1, le=10)] = 5) -> QueryResult:
        """Peek at up to 10 rows of one table. The table must be in the real table list."""
        return guard.sample_rows(table, n)

    @mcp.tool(annotations=READ_ONLY)
    def customer_lookup(customer_id: Annotated[int, Field(ge=1)]) -> CustomerInfo:
        """One customer with personal fields minimised: email masked, no address, phone or fax."""
        return guard.customer_lookup(customer_id)

    @mcp.tool(annotations=READ_ONLY)
    def my_invoices(limit: Annotated[int, Field(ge=1, le=20)] = 10) -> QueryResult:
        """Your own invoices only (the customer mapped to your caller id). No argument picks a customer."""
        return guard.my_invoices(limit)

    @mcp.tool(annotations=READ_ONLY)
    def get_trace(trace_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]) -> TraceInfo:
        """Audit entry and spans for one of your own trace ids. Unknown or foreign ids get the same refusal."""
        return guard.get_trace(trace_id)

    @mcp.tool(annotations=READ_ONLY)
    def recent_failures(n: Annotated[int, Field(ge=1, le=20)] = 10) -> FailureList:
        """Your last n refused or failed calls (at most 20): tool, outcome, detail, trace id, SQL hash only."""
        return guard.recent_failures(n)

    @mcp.tool(annotations=READ_ONLY)
    def explain_refusal(trace_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]) -> RefusalExplanation:
        """Plain-language reason and next step for one of your own refused calls."""
        return guard.explain_refusal(trace_id)

    @mcp.tool(annotations=READ_ONLY)
    def call_stats() -> CallStatsResult:
        """Your call counts by tool and outcome, with p50 and p95 duration per tool."""
        return guard.call_stats()

    @mcp.resource("chinook://schema", name="schema", mime_type="application/json",
                  description="Tables and columns of the music-store database. Schema only, no rows.")
    def schema() -> str:
        return json.dumps(guard.schema_overview().model_dump())

    return mcp


if __name__ == "__main__":
    build_server().run("stdio")
