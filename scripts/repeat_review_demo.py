#!/usr/bin/env python3
"""Repeated-task demo (about a second, deterministic): run the tool-call-review routine in 4 rounds.

Data is the SYNTHETIC hourly story in sample_session/ (not real). Each round, one more 15-minute window of the
session "has happened": the audit and span files in a temporary folder grow to that point. The round reads only
entries newer than the cursor kept in a small state file in the same temporary folder.

The routine uses the Guard methods that the MCP tools call (call_stats, recent_failures, explain_refusal), not a
stdio client; tests/test_stdio_client.py covers the real stdio path for the same tools.
Run:  .venv/bin/python scripts/repeat_review_demo.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import make_sample_session as sample  # noqa: E402

WINDOWS = [(0, 15), (15, 30), (30, 45), (45, 60)]


def _read(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()]


def run(workdir: Path, emit=print) -> list[dict]:
    """Run the 4 rounds in workdir. Returns one result dict per round."""
    os.environ.pop("CHINOOK_AUDIT_LOG", None)
    os.environ.pop("CHINOOK_TRACE_LOG", None)
    os.environ["MCP_CALLER_ID"] = sample.CALLER
    from chinook_mcp.guard import Guard

    if not (sample.OUT / "hourly_audit.jsonl").exists():
        sample.write()
    audit_all = _read(sample.OUT / "hourly_audit.jsonl")
    spans_all = _read(sample.OUT / "hourly_trace.jsonl")
    state_path = workdir / "state.json"
    state_path.write_text(json.dumps({"cursor": "", "hash_rounds": {}, "outcomes": {}}))
    results = []
    emit("SYNTHETIC session (not real data). Routine: skills/tool-call-review/SKILL.md")

    for rnd, (lo, hi) in enumerate(WINDOWS, 1):
        end = (sample.BASE + timedelta(minutes=hi)).isoformat()
        visible = [r for r in audit_all if r["ts"] <= end]
        ids = {r["trace_id"] for r in visible}
        (workdir / "audit.jsonl").write_text("".join(json.dumps(r) + "\n" for r in visible))
        (workdir / "trace.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans_all if s["trace_id"] in ids))
        guard = Guard(audit_path=workdir / "audit.jsonl", trace_path=workdir / "trace.jsonl", rate_limit_per_min=1000)
        state = json.loads(state_path.read_text())
        cursor = state["cursor"]

        # Step 1: call_stats. New calls = difference from the totals seen at the last checkpoint.
        stats = guard.call_stats()
        now = {t: dict(s.outcomes) for t, s in stats.by_tool.items()}
        new_calls: Counter = Counter()
        new_refused = 0
        for tool, outs in now.items():
            for o, n in outs.items():
                d = n - state["outcomes"].get(tool, {}).get(o, 0)
                new_calls[tool] += d
                new_refused += d if o != "ok" else 0
        # Steps 2 and 3: recent_failures, only entries newer than the cursor, grouped by SQL hash.
        failures = guard.recent_failures(20).failures
        fresh = [f for f in failures if f.ts > cursor]
        total_by_hash = Counter(f.sql_sha256 for f in failures)
        groups: dict[str, list] = {}
        for f in fresh:
            groups.setdefault(f.sql_sha256, []).append(f)

        emit(f"\n--- Round {rnd} (minutes {lo}-{hi}) ---")
        emit("Called: {} new calls ({} refused): {}".format(
            sum(new_calls.values()), new_refused,
            ", ".join(f"{t} x{n}" for t, n in sorted(new_calls.items()) if n)))
        flags = []
        if not groups:
            emit("Refused: none")
        for h, fs in groups.items():
            earlier = state["hash_rounds"].get(h, [])
            label = sample.HOURLY_LABELS.get(h, "")
            emit(f"Refused: {fs[0].tool} x{len(fs)}  sql#{h[:10]}  ({label}; story label, the server only stores the hash)")
            # Step 4: a hash refused two or more times in total gets one explain_refusal, then a change of approach.
            if total_by_hash[h] >= 2:
                ex = guard.explain_refusal(fs[-1].trace_id)
                emit(f"  Why: {ex.explanation}")
                emit(f"  Change: {ex.next_step} Do not send this query again.")
            if earlier:
                flags.append(h)
                emit(f"  FLAG: still retrying the same query (refused in round(s) {', '.join(map(str, earlier))} too)")
            state["hash_rounds"].setdefault(h, []).append(rnd)
        if not groups:
            emit("Report: clean. No refusals since the last checkpoint; the assistant changed approach"
                 if rnd == len(WINDOWS) else "Report: clean this round.")
        # Advance the cursor so the next round never re-reports these entries.
        state["cursor"] = visible[-1]["ts"]
        state["outcomes"] = now
        state_path.write_text(json.dumps(state))
        results.append({
            "round": rnd, "new_calls": sum(new_calls.values()), "refused": new_refused,
            "trace_ids": [f.trace_id for f in fresh], "flagged": flags,
        })
    return results


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="chinook_review_") as d:
        run(Path(d))
