import json
from datetime import date
from unittest.mock import Mock

from sqlalchemy.orm import Session

from shopcare.core.config import Settings
from shopcare.db import build_engine, init_db
from shopcare.db.models import PolicyChunkRecord, PolicyEmbeddingRecord
from shopcare.scripts import compare_retrieval


def test_preview_never_builds_client(monkeypatch, capsys):
    factory = Mock(side_effect=AssertionError("Must not create provider"))
    monkeypatch.setattr(compare_retrieval, "build_embedding_client", factory)
    assert compare_retrieval.main([]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    factory.assert_not_called()


def test_comparison_uses_real_services_and_writes_report(tmp_path, monkeypatch, capsys):
    url = f"sqlite:///{(tmp_path / 'comparison.db').as_posix()}"
    engine = build_engine(url)
    init_db(engine)
    try:
        with Session(engine) as session, session.begin():
            session.add(
                PolicyChunkRecord(
                    chunk_id="A",
                    policy_id="A",
                    version="v1",
                    title="换货",
                    category="数码",
                    effective_from=date(2026, 1, 1),
                    text="换货政策",
                    start_char=0,
                    end_char=4,
                )
            )
            session.flush()
            session.add(
                PolicyEmbeddingRecord(
                    chunk_id="A",
                    model_id="test-model",
                    dimensions=2,
                    vector=[1.0, 0.0],
                )
            )
        settings = Settings(
            _env_file=None,
            database_url=url,
            embedding_model_id="test-model",
            embedding_dimensions=2,
        )
        monkeypatch.setattr(compare_retrieval, "Settings", lambda: settings)
        client = Mock()
        client.model, client.dimensions = "test-model", 2
        client.embed_query.return_value = [1.0, 0.0]
        monkeypatch.setattr(compare_retrieval, "build_embedding_client", lambda _: client)
        cases = tmp_path / "cases.json"
        cases.write_text(
            json.dumps(
                [
                    {
                        "id": name,
                        "query": query,
                        "category": "数码",
                        "as_of": "2026-10-30",
                        "expected_policy_id": expected,
                        "expected_version": "v1" if expected else None,
                    }
                    for name, query, expected in [("P", "换货", "A"), ("N", "zzzz", None)]
                ]
            ),
            encoding="utf-8",
        )
        output = tmp_path / "report.md"
        assert (
            compare_retrieval.main(["--run", "--cases", str(cases), "--output", str(output)]) == 0
        )
        report = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
        assert report["summary"]["lexical"]["correct_empty"] == 1
        assert report["summary"]["semantic"]["correct_empty"] == 0
        assert report["summary"]["semantic"]["hit_at_1"] == 1
        assert report["query_model_calls"] == 2
        assert len(report["index_sha256"]) == 64
        assert output.read_text(encoding="utf-8").startswith("# 词法与语义检索对照")
        client.embed_query.assert_any_call("换货")
        capsys.readouterr()
    finally:
        engine.dispose()
