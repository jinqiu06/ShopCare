import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopcare.db import build_engine
from shopcare.db.models import PolicyChunkRecord

ROOT = Path(__file__).resolve().parents[3]


def run_ingest(database, *args):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "shopcare.scripts.ingest_policies",
            "--database-url",
            f"sqlite:///{database.as_posix()}",
            *args,
        ],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
    )


def stored_count(database):
    engine = build_engine(f"sqlite:///{database.as_posix()}")
    try:
        with Session(engine) as session:
            return session.scalar(select(func.count()).select_from(PolicyChunkRecord))
    finally:
        engine.dispose()


def test_ingest_cli_imports_repeats_and_rechunks(tmp_path):
    database = tmp_path / "nested" / "policy.db"
    first = run_ingest(database)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {"policies_processed": 8, "chunks_written": 8}
    assert stored_count(database) == 8
    smaller = run_ingest(database, "--chunk-size", "40", "--overlap", "5")
    assert smaller.returncode == 0, smaller.stderr
    assert json.loads(smaller.stdout)["chunks_written"] > 8
    for _ in range(2):
        result = run_ingest(database)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"policies_processed": 8, "chunks_written": 8}
        assert stored_count(database) == 8


@pytest.mark.parametrize("args", [("--chunk-size", "0"), ("--overlap", "300")])
def test_ingest_cli_rejects_window_before_database_creation(tmp_path, args):
    database = tmp_path / "policy.db"
    result = run_ingest(database, *args)
    assert result.returncode == 2
    assert not database.exists()


def test_ingest_cli_invalid_input_preserves_existing_data(tmp_path):
    database = tmp_path / "policy.db"
    assert run_ingest(database).returncode == 0
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "bad.json").write_text("{broken", encoding="utf-8")
    result = run_ingest(database, "--policy-dir", str(folder))
    assert result.returncode == 1
    assert stored_count(database) == 8
