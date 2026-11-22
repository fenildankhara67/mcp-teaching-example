"""The main demo: six stages in order, deterministic output (apart from trace ids and the temp path)."""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = str(ROOT / "scripts" / "talk_demo.py")


def run(*args, stdin="", env=None):
    import os
    e = {**os.environ, **(env or {})}
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, cwd=ROOT, input=stdin, env=e)


def normalise(text):
    text = re.sub(r"(trace(?: id)?) [0-9a-f]{8}\b", r"\1 TID", text)
    text = re.sub(r"logs: \S+", "logs: PATH", text)
    return text


def test_two_runs_identical_and_all_stages():
    a, b = run(), run()
    assert a.returncode == 0 and b.returncode == 0 and a.stderr == "" and "Traceback" not in a.stdout
    assert normalise(a.stdout) == normalise(b.stdout)
    out = a.stdout
    heads = re.findall(r"^== (\d)\. ", out, re.M)
    assert heads == ["1", "2", "3", "4", "5", "6"]
    for word in ("Legend:", "OK", "REFUSED", "BLOCKED", "== Summary ==", "calls  ok  refused  blocked  traces"):
        assert word in out
    assert "\033[" not in out  # piped output has no colour codes


def test_demo_content_matches_the_stage_outline():
    out = run().stdout
    assert "13 tools, 13 read-only" in out                       # 4a too much power
    assert "<untrusted_data>" in out and "nothing acted on it" in out  # 4b hidden instruction
    assert "unbounded or unknown size" in out                     # 4c costly query
    assert "already refused once" in out and "BLOCKED  attempt 3" in out  # 5 loop: warning then block
    assert "explain_refusal" in out and "tracks per artist" in out        # 5 recovery
    assert "sql#" in out and "SKILL.md" in out                    # 6 review
    assert re.search(r"trace [0-9a-f]{8}\]", out) and not re.search(r"[0-9a-f]{32}", out)  # ids shortened
    nums = re.search(r"^\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", out, re.M).groups()
    calls, ok, refused, blocked, traces = map(int, nums)
    assert calls == ok + refused + blocked == traces and blocked == 1


def test_pause_mode_waits_for_enter_and_matches_fast():
    paused = run("--pause", stdin="\n" * 6)
    assert paused.returncode == 0
    assert normalise(paused.stdout).count("[Enter for the next stage]") == 5
    stripped = normalise(paused.stdout).replace("   [Enter for the next stage] ", "")
    assert stripped == normalise(run("--fast").stdout)
    assert run("--pause", "--fast").returncode == 0


def test_logs_can_be_pointed_at_a_visible_folder(tmp_path):
    env = {"CHINOOK_AUDIT_LOG": str(tmp_path / "audit.jsonl"), "CHINOOK_TRACE_LOG": str(tmp_path / "trace.jsonl")}
    r = run(env=env)
    assert r.returncode == 0
    audit = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(audit) == 18 and sum(a["detail"] == "repeated refused query (loop guard)" for a in audit) == 1
    assert (tmp_path / "trace.jsonl").exists() and str(tmp_path) in r.stdout
    again = run(env=env)  # a second run appends; its own summary counts only its own calls
    assert "      18  12" in again.stdout
