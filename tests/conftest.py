import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("seed_local", ROOT / "scripts" / "seed_local.py")
seed_local = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_local)

if not seed_local.ORIGINAL.exists():
    pytest.exit("Chinook database missing: run scripts/download_db.sh first", returncode=2)
seed_local.seed()  # fresh local copy with the planted row

from chinook_mcp.guard import DEFAULT_DB, Guard  # noqa: E402


def file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def audit_path(tmp_path):
    return tmp_path / "audit.jsonl"


@pytest.fixture
def trace_path(tmp_path):
    return tmp_path / "trace.jsonl"


@pytest.fixture
def guard(audit_path, trace_path):
    return Guard(audit_path=audit_path, trace_path=trace_path)
