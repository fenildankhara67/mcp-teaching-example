"""The skill file, and the repeated-review demo (deterministic, cursor, repeated-hash flagging)."""
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "tool-call-review" / "SKILL.md"


def load_demo():
    spec = importlib.util.spec_from_file_location("repeat_review_demo", ROOT / "scripts" / "repeat_review_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def frontmatter(text):
    assert text.startswith("---\n"), "frontmatter must start on the first line"
    head, _, body = text[4:].partition("\n---\n")
    fields = {}
    for line in head.splitlines():
        m = re.match(r"^([a-z-]+):\s*(.*)$", line)
        assert m, line
        fields[m.group(1)] = m.group(2)
    return fields, body


def test_skill_frontmatter_matches_verified_format():
    fields, _ = frontmatter(SKILL.read_text())
    # agentskills.io/specification: name and description required; name rules; description 1-1024 chars.
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", fields["name"]) and len(fields["name"]) <= 64
    assert fields["name"] == SKILL.parent.name
    assert 1 <= len(fields["description"]) <= 1024
    assert len(fields.get("compatibility", "x")) <= 500
    assert set(fields) <= {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


def test_skill_mentions_each_required_step_and_rule():
    text = SKILL.read_text()
    low = text.lower()
    for word in ("call_stats", "recent_failures", "sql_sha256", "explain_refusal", "search_tracks", "sample_rows",
                 "never retry an unchanged refused query", "read-only", "caller-scoped", "personal data", "report"):
        assert word in low, word
    assert "two or more times" in low and "change approach" in low
    assert len(text.splitlines()) <= 60


def test_demo_is_deterministic():
    runs = [subprocess.run([sys.executable, str(ROOT / "scripts" / "repeat_review_demo.py")], capture_output=True,
                           text=True, cwd=ROOT, check=True).stdout for _ in range(2)]
    assert runs[0] == runs[1] and "Round 4" in runs[0] and "SYNTHETIC" in runs[0]


def test_cursor_never_rereports_and_repeats_are_flagged(tmp_path):
    results = load_demo().run(tmp_path, emit=lambda *_: None)
    assert [r["round"] for r in results] == [1, 2, 3, 4]
    seen = set()
    for r in results:
        assert not (seen & set(r["trace_ids"])), f"round {r['round']} re-reported an earlier entry"
        seen |= set(r["trace_ids"])
    assert [r["new_calls"] for r in results] == [7, 6, 5, 5]  # every call counted once
    assert [r["refused"] for r in results] == [2, 4, 2, 0]
    assert results[0]["flagged"] == []  # first sight is not a repeat
    assert len(results[1]["flagged"]) == 1 and len(results[2]["flagged"]) == 2
    assert results[3]["trace_ids"] == [] and results[3]["flagged"] == []  # clean report
    assert (tmp_path / "state.json").exists()
