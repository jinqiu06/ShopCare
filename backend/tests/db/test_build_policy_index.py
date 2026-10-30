import json
from datetime import date
from unittest.mock import Mock

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from shopcare.core.config import Settings
from shopcare.db import build_engine, init_db
from shopcare.db.models import PolicyChunkRecord, PolicyEmbeddingRecord
from shopcare.scripts import build_policy_index


@pytest.fixture
def setup_index(tmp_path, monkeypatch):
    database = tmp_path / "policies.db"
    url = f"sqlite:///{database.as_posix()}"
    engine = build_engine(url)
    init_db(engine)
    with Session(engine) as session, session.begin():
        for name in ["B", "A"]:
            session.add(
                PolicyChunkRecord(
                    chunk_id=name,
                    policy_id=name,
                    version="v1",
                    title=name,
                    category="数码",
                    effective_from=date(2026, 10, 1),
                    text="测试",
                    start_char=0,
                    end_char=2,
                )
            )
        session.flush()
        session.add(
            PolicyEmbeddingRecord(
                chunk_id="A",
                model_id="old-model",
                dimensions=2,
                vector=[-1.0, 0.0],
            )
        )
    settings = Settings(
        _env_file=None,
        database_url=url,
        embedding_api_key="test-only-key",
        embedding_base_url="https://example.com/v1",
        embedding_model_id="test-model",
        embedding_dimensions=2,
    )
    monkeypatch.setattr(build_policy_index, "Settings", lambda: settings)
    client = Mock()
    client.dimensions = 2
    client.embed_documents.return_value = [[1.0, 0.0], [0.0, 1.0]]
    factory = Mock(return_value=client)
    monkeypatch.setattr(build_policy_index, "build_embedding_client", factory)
    try:
        yield engine, client, factory
    finally:
        engine.dispose()


def snapshot(engine):
    with Session(engine) as session:
        return [
            (row.chunk_id, row.model_id, row.vector)
            for row in session.scalars(
                select(PolicyEmbeddingRecord).order_by(PolicyEmbeddingRecord.chunk_id)
            )
        ]


def test_dry_run_does_not_call_provider_or_write(setup_index, capsys):
    engine, client, factory = setup_index
    before = snapshot(engine)
    assert build_policy_index.main(["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "chunks": 2,
        "model_id": "test-model",
        "dimensions": 2,
        "dry_run": True,
    }
    factory.assert_not_called()
    client.embed_documents.assert_not_called()
    assert snapshot(engine) == before


def test_build_and_repeat_reuse_core_services(setup_index, capsys):
    engine, client, _ = setup_index
    for _ in range(2):
        assert build_policy_index.main([]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["vectors_written"] == 2
        assert snapshot(engine) == [
            ("A", "test-model", [1.0, 0.0]),
            ("B", "test-model", [0.0, 1.0]),
        ]
    client.embed_documents.assert_called_with(["A\n测试", "B\n测试"])


@pytest.mark.parametrize("mode", ["timeout", "invalid_vector"])
def test_provider_failure_preserves_existing_vectors(setup_index, capsys, mode):
    engine, client, _ = setup_index
    before = snapshot(engine)
    if mode == "timeout":
        client.embed_documents.side_effect = TimeoutError("simulated timeout")
    else:
        client.embed_documents.return_value = [[0.0, 0.0], [0.0, 1.0]]
    assert build_policy_index.main([]) == 1
    assert capsys.readouterr().out == ""
    assert snapshot(engine) == before


def test_changed_source_is_rejected_before_saving(setup_index, capsys):
    engine, client, _ = setup_index
    before = snapshot(engine)

    def change_source(texts):
        with Session(engine) as session, session.begin():
            session.execute(
                update(PolicyChunkRecord)
                .where(PolicyChunkRecord.chunk_id == "A")
                .values(title="已修改")
            )
        return [[1.0, 0.0], [0.0, 1.0]]

    client.embed_documents.side_effect = change_source
    assert build_policy_index.main([]) == 1
    assert capsys.readouterr().out == ""
    assert snapshot(engine) == before
