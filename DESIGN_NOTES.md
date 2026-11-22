# Design and verification notes (2026-11-21)

## SDK (verified by running it)
- Installed `mcp` 2.3.0 from PyPI into `.venv` (Python 3.11.9); pydantic 2.14.4.
- Server import that works: `from mcp.server.mcpserver import MCPServer` (VERIFIED, class `mcp.server.mcpserver.server.MCPServer`).
- The v2 rename of `FastMCP` to `MCPServer`: the new import works (VERIFIED). Old `mcp.server.fastmcp` import fails: now VERIFIED, see Additions below.
- Tool registration: `@mcp.tool(annotations=ToolAnnotations(...))`; sync functions work; Pydantic return models are accepted.
- `ToolAnnotations` fields are snake_case in Python (`read_only_hint`, `destructive_hint`); reading `readOnlyHint` as an attribute fails. The initialize response on the wire used camelCase (`listChanged`, `protocolVersion`).
- Run over stdio: `MCPServer.run("stdio")` (default transport), i.e. `python -m chinook_mcp.server`.
- Testing: (1) a manual JSON-RPC `initialize` piped to stdin returned a valid result; (2) `tests/test_stdio_client.py` launches the server as a subprocess with `mcp.client.stdio.stdio_client` + `ClientSession`, lists tools, calls `run_query`; a refused call comes back with `is_error` true. `await server.list_tools()` is used for the in-process tool-enumeration test.

## Chinook
- Licence file read from the public repository (raw.githubusercontent): MIT, (c) 2008-2024 Luis Rocha (VERIFIED). To stay cautious the DB is downloaded by `scripts/download_db.sh` (release v1.4.5, `Chinook_Sqlite.sqlite`) rather than committed.

## Not verified
- The OWASP MCP Security Cheat Sheet is reviewed in the next section (read 2026-11-21). Audit fields (timestamp, caller id, tool, SQL hash, outcome) follow it only partly.
- The timeout uses SQLite's progress handler; it stops long queries between VM steps, not a blocked disk read. Behaviour under other SQLite builds is UNVERIFIED.
- Not production safe; teaching example only.

# Additions: SDK import check, OWASP review, design notes

## Old import test (VERIFIED by running it)
`import mcp.server.fastmcp` on mcp 2.3.0 fails with `ModuleNotFoundError: No module named 'mcp.server.fastmcp'. This is mcp 2.x, where FastMCP was renamed to MCPServer ... or pin 'mcp<2'`. The new `from mcp.server.mcpserver import MCPServer` works. This replaces the earlier UNVERIFIED line above.

## OWASP MCP Security Cheat Sheet (opened and read 2026-11-21 via page fetch; summary below is from that fetch)
https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html has 12 sections.

Followed:
- 1 Least privilege: server has only read tools and a read-only DB connection; no credentials exist.
- 5 Input validation: SQL is untrusted; single SELECT only, authorizer, `mode=ro`; no path, shell or URL arguments (tested). Schema is not strict (no `additionalProperties: false`, no `pattern`): partial.
- 6 Timeouts: per-query timeout and row cap exist. Authentication, rate limits, TLS are not done (stdio only).
- 10 Logging: timestamps, user context (caller id) and outcome are logged.
- 12 Tool return values: text from the database is wrapped and labelled untrusted; the server takes no follow-up actions from data. Sheet says tag-based labelling is supplementary; ours is not proof of safety.
- Annotations: read-only hints set. Sheet says annotations are hints, not enforcement; enforcement here is the `mode=ro` connection and authorizer.

Not followed:
- 2 Pinning tool definitions with hashes / `mcp-scan`; strict JSON Schema.
- 3 Sandboxing the server process (filesystem and network restriction).
- 4 Human-in-the-loop: no confirmation step (nothing destructive exists, but the sheet says choose controls by risk, not only the read/write label; a read of customer data is still a risk).
- 6 Authentication, OAuth 2.1, TLS, rate limits per session, Origin/Host validation.
- 7, 8 Message integrity, multi-server isolation: out of scope.
- 9 Supply chain: versions pinned but no hashes (`pip --require-hashes`) and no vulnerability scan.
- 10 SIEM, alerting, redaction review.
- 11 Consent and installation: out of scope.

Audit field alignment: the sheet recommends full tool invocation parameters, user context and timestamps. Ours has timestamp, caller id, trace id, tool, outcome, detail and row count. It stores a SHA-256 of the SQL, not the parameters: deliberate (avoids storing sensitive values) but a difference. Partially aligned. The sheet's redaction advice is consistent with hashing.

## Design notes
- Cost guard: estimate = product of row counts of full `SCAN` steps in `EXPLAIN QUERY PLAN`; limit 100,000. Aliases in the plan show as the alias only, so an alias is assumed to be the largest table (conservative). On Chinook no single table exceeds the limit, so single-scan refusal is tested with a lowered limit. Behaviour on large databases and other SQLite versions: UNVERIFIED (tested on SQLite 3.51.0 only).
- Tracing: one private `TracerProvider` with `SimpleSpanProcessor` and a JSONL exporter; not set as the global provider.
- Planted row lives in `scripts/seed_local.py` (`TrackNote` table, a new table; Chinook has no natural notes column). The original download is never written.
- Not production safe; teaching example only.

# Additions: audit, cost estimate, new capabilities
- Audit: exactly one line per client call (tools and the schema resource read); internal lookups write none. Test: `tests/test_capabilities.py`.
- Cost estimate (supersedes the alias note above): aliases are resolved to real tables via the index name in the plan or a regex over the FROM clause; recursive CTEs and untraceable sources give "unbounded or unknown size" with no number. Tested on SQLite 3.51.0 only; regex alias parsing is not a SQL parser (UNVERIFIED on unusual SQL).
- Resource API (VERIFIED by running mcp 2.3.0): `@mcp.resource("chinook://schema", ...)` registers it; `list_resources` and `read_resource` work over stdio (`tests/test_stdio_client.py`).
- OWASP mapping (only against sections I read on 2026-11-21, see above): `audit_tail` and the single audit line support section 10 (logging, user context) for the caller's own view only; the per-caller rate limit is a partial step on section 6 (rate limits; in-process, unauthenticated caller id, so not real session limiting); `explain_query` and the schema resource add no write capability, consistent with section 1 (least privilege). Nothing here addresses sections 2, 3, 4 or 6 authentication. No claim of full alignment.
- Not production safe; teaching example only.
- Input validation: the SDK validates arguments before tool code runs, so `AuditedMCPServer.call_tool` in `chinook_mcp/server.py` catches the validation error and calls `Guard.audit_invalid`, which writes one audit line (`refused`, detail "invalid input", argument names only, SHA-256 of the text if a string) and one trace, after the caller check and rate limit. Unknown tool names are not audited (UNVERIFIED whether that matters).

# Additions: purpose-built tools and redaction
OWASP mapping is only to sections of the cheat sheet I read earlier (see above); nothing is claimed beyond that.
- `search_tracks`: section 5 (input validation): the model supplies only a bounded string, bound as a parameter with LIKE wildcards escaped; no SQL text from the model. Partial: schema has length limits, no `pattern`.
- `sample_rows`: section 5 and section 1 (least privilege): table name allow-listed against the real table list, row count capped at 10; no free-form identifier reaches SQL.
- `customer_lookup`: section 12 / data minimisation (sheet's tool-output handling) and section 10 (redaction of logs is separate; our audit stores only a hash). Email masked, address, phone and fax never selected. Redaction is by column name (authorizer returns NULL) plus an output regex backstop; a differently named column holding personal data would not be caught (UNVERIFIED beyond Chinook's schema).
- `my_invoices`: section 1 (least privilege) in spirit. The sheet text I read does not name object-level authorisation, so I do not claim a mapping. The caller id is an unauthenticated environment variable, so this isolates callers only as far as that id is trusted; it is not real authorisation.
- All four go through the same caller check, rate limit, single audit line, trace and validation auditing as the earlier tools. Not production safe; teaching example only.

# Additions: debugging tools and sample session
- Defect found and fixed while testing over stdio: `Refused` was a plain `Exception`, so the SDK returned only "Error executing tool ..." to the client and logged a traceback; the reason and trace id never reached the client. `Refused` now subclasses the SDK's `ToolError`, so the client sees the reason and `[trace_id=...]` (checked by the stdio test, VERIFIED on mcp 2.3.0).
- `get_trace`, `recent_failures`, `explain_refusal`, `call_stats`: caller-scoped reads of the local audit and span files; sections 10 (logging with user context) only in the sense that the caller can see their own records; the cheat sheet text I read does not describe caller-facing debugging tools, so no further mapping is claimed. Unknown and foreign trace ids return one identical message so an id's existence is not revealed (my design choice, not an OWASP item I checked).
- `detail` text is returned by `recent_failures`, `get_trace` and `explain_refusal` (cut to 300 chars). Sqlite error messages could in principle include SQL fragments (UNVERIFIED for all cases); refusal details written by this server contain no SQL text.
- The sample session is SYNTHETIC (fixed seed, invented timings); it is not evidence about real behaviour. Not production safe; teaching example only.

# Additions: skill and repeated review
## Skill format (docs opened 2026-11-21)
- Spec: https://agentskills.io/specification. A skill is a directory with a required `SKILL.md` (YAML frontmatter then Markdown) and optional `scripts/`, `references/`, `assets/`. Required fields: `name` (max 64 chars, lowercase letters, digits and hyphens, no leading, trailing or double hyphen, must match the directory name) and `description` (1 to 1024 chars, what it does and when to use it). Optional: `license`, `compatibility` (max 500), `metadata` (string map), `allowed-tools` (experimental). Body has no format restrictions; spec suggests under 500 lines and about 5000 tokens.
- Placement (Claude Code docs, https://code.claude.com/docs/en/skills): personal `~/.claude/skills/<name>/SKILL.md`, project `.claude/skills/<name>/SKILL.md`, plugin `<plugin>/skills/<name>/SKILL.md`. That page says all frontmatter is optional with `description` recommended, which is looser than the open spec; I follow the stricter spec. I read only the first 100000 characters of that page.
- UNVERIFIED: that this skill loads or triggers in any product (I did not install or run it in one); that `skills-ref validate` passes (not run); behaviour of `compatibility` text. The skill sits in `skills/tool-call-review/`, not in a client's skills folder, so it is inactive until copied.
- `tests/test_skill.py` checks the frontmatter against the spec rules above by hand (no YAML library).
## Data and sample files in this folder
- `data/Chinook_Sqlite.sqlite`: downloaded Chinook sample (MIT), never modified. `data/Chinook_local.sqlite`: copy plus the planted `TrackNote` table. `data/caller_customers.json`: fake caller to customer id map.
- `sample_session/audit.jsonl`, `trace.jsonl` (32-call synthetic story for the debug walkthrough) and `hourly_audit.jsonl`, `hourly_trace.jsonl` (23-call synthetic story in four windows for the repeated review). `sample_session/README.txt` labels them synthetic.
- Scripts: `seed_local.py` (local copy, planted row, caller map), `make_sample_session.py`, `download_db.sh`, `demo.py`, `debug_walkthrough.py`, `repeat_review_demo.py`.
- Could the repeated review run over anything else here? Yes, over `sample_session/audit.jsonl` (the 32-call story): it also has a refused query repeated three times, but within one minute, so it does not split into four windows without regenerating it. A live `audit.jsonl` from a real server run would also work, but only for entries made by one caller id. No other data exists in the folder.

# Additions: loop guard and talk demo
## Loop guard
- Implementation: `Guard._call` in `chinook_mcp/guard.py`. State is `(caller id, tool) -> [sha256, refusal times]`, hashes only. Warn at the 2nd refusal (extra sentence in the message; the audit detail keeps the plain reason), block from the 3rd call before any check runs (audit detail `repeated refused query (loop guard)`, span `loop_guard_check`). `explain_refusal` has a lookup entry for it; `recent_failures` and `call_stats` show it because they read the audit file (tested).
- Limits: in-process only, lost on server restart, not shared between server processes; keyed on the unauthenticated caller id, so changing the id avoids it; matches exact query text only (add a space and it is a new hash); a different query to the same tool resets the streak, so alternating two refused queries is not caught (UNVERIFIED how an assistant would behave); blocked calls are not counted again, so after the window passes the query runs its checks again; calls rejected by input validation are not part of the loop guard. Not production safe.
- Time is injectable (`Guard(clock=...)`) so the window tests do not sleep. The rate limit uses the same clock.
- No new tool was added (still 13). I did not change the repeated-review demo data: it is an offline story and still reads true.
## Talk demo
- `scripts/talk_demo.py` uses Guard methods, not stdio. Output is deterministic except for trace ids (shortened to 8 characters) and the temp folder name; the test normalises both. Colour only when stdout is a terminal.
