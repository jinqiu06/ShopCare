import json
from datetime import date
from unittest.mock import Mock

import httpx
import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopcare.core.config import Settings
from shopcare.db import build_engine, init_db
from shopcare.db.models import Conversation, PolicyChunkRecord, PolicyEmbeddingRecord
from shopcare.scripts import evaluate_core
from shopcare.scripts.seed import seed_demo_data


def test_ranking_metrics_deduplicate_policies_and_use_complete_gold():
    result = evaluate_core.score_ranking(["A", "A", "X", "B"], ["A", "B"], 3)
    assert result["ranking"] == ["A", "X", "B"]
    assert result["precision"] == pytest.approx(2 / 3)
    assert result["recall"] == 1
    assert result["reciprocal_rank"] == 1
    result = evaluate_core.score_ranking(["X", "Y", "A"], ["A", "B"], 3)
    assert result["precision"] == pytest.approx(1 / 3)
    assert result["recall"] == 0.5
    assert result["reciprocal_rank"] == pytest.approx(1 / 3)
    assert evaluate_core.score_ranking(["X", "Y", "A"], ["A"], 1)["recall"] == 0


def test_empty_gold_and_missing_hits_have_different_metric_denominators():
    negative = evaluate_core.score_ranking([], [], 3)
    assert negative["correct_empty"] is True
    assert all(negative[key] is None for key in ("hit", "precision", "recall", "reciprocal_rank"))
    assert evaluate_core.score_ranking(["X"], [], 3)["correct_empty"] is False
    positive = evaluate_core.score_ranking([], ["A"], 3)
    assert positive["hit"] == positive["precision"] == positive["recall"] == 0
    assert positive["reciprocal_rank"] == 0


def test_failed_retrieval_is_not_scored_as_correct_empty():
    rows = [
        {"mode": "semantic", "split": "dev", "outcome": "error", "relevant": []},
        {
            "mode": "semantic",
            "split": "dev",
            "outcome": "completed",
            "relevant": ["A"],
            "metrics": {str(k): evaluate_core.score_ranking(["A"], ["A"], k) for k in (1, 3)},
        },
    ]
    summary = evaluate_core.summarize_retrieval(rows)[1]
    assert (summary["planned"], summary["completed"], summary["errors"]) == (2, 1, 1)
    assert summary["negative_n"] == summary["correct_empty"] == 0
    assert summary["positive_n"] == 1


def test_http_budget_stops_before_an_extra_request_is_sent():
    budget = evaluate_core.RequestBudget(max_chat=1, max_embedding=1)
    transport = Mock(return_value=httpx.Response(200, json={}))
    with httpx.Client(
        transport=httpx.MockTransport(transport), event_hooks={"request": [budget.on_request]}
    ) as client:
        client.post("https://example.invalid/v1/chat/completions")
        with pytest.raises(ValueError, match="chat request budget exhausted"):
            client.post("https://example.invalid/v1/chat/completions")
        client.post("https://example.invalid/v1/embeddings")
        with pytest.raises(ValueError, match="embedding request budget exhausted"):
            client.post("https://example.invalid/v1/embeddings")
    assert transport.call_count == 2
    assert (budget.chat, budget.embedding) == (1, 1)


def test_offline_evaluation_never_constructs_provider_clients(tmp_path, monkeypatch, capsys):
    factory = Mock(side_effect=AssertionError("offline mode must not build model clients"))
    monkeypatch.setattr(evaluate_core, "build_chat_client", factory)
    monkeypatch.setattr(evaluate_core, "build_embedding_client", factory)
    output = tmp_path / "offline.md"
    assert (
        evaluate_core.main(
            ["--output", str(output), "--max-chat-requests", "0", "--max-embedding-requests", "0"]
        )
        == 0
    )
    report = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    assert report["execution_mode"] == "offline_lexical_only"
    assert len(report["retrieval_rows"]) == 30
    assert report["agent_rows"] == []
    assert {row["mode"] for row in report["retrieval_rows"]} == {"lexical"}
    assert report["requests"]["chat"] == report["requests"]["embedding"] == 0
    assert report["chunk_count"] == 8
    factory.assert_not_called()
    capsys.readouterr()


def test_real_mode_harness_runs_graph_on_temporary_copy_only(tmp_path, monkeypatch, capsys):
    database = tmp_path / "source.db"
    url = f"sqlite:///{database.as_posix()}"
    engine = build_engine(url)
    try:
        init_db(engine)
        with Session(engine) as session, session.begin():
            seed_demo_data(session)
            session.add(
                PolicyChunkRecord(
                    chunk_id="P:v1:000",
                    policy_id="P",
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
                    chunk_id="P:v1:000",
                    model_id="test-embedding",
                    dimensions=2,
                    vector=[1.0, 0.0],
                )
            )
        original = database.read_bytes()
        settings = Settings(
            _env_file=None,
            database_url=url,
            llm_model_id="mock-chat",
            embedding_model_id="test-embedding",
            embedding_dimensions=2,
        )
        monkeypatch.setattr(evaluate_core, "Settings", lambda: settings)
        embedding = Mock()
        embedding.model, embedding.dimensions = "test-embedding", 2
        embedding.embed_query.return_value = [1.0, 0.0]
        model = Mock()
        model.bind_tools.return_value.invoke.return_value = AIMessage(
            content=json.dumps(
                {
                    "status": "needs_clarification",
                    "answer": "请补充订单和商品编号。",
                    "citation_chunk_ids": [],
                    "limitations": [],
                    "policy_quotes": [],
                }
            )
        )
        monkeypatch.setattr(evaluate_core, "build_chat_client", Mock(return_value=model))
        monkeypatch.setattr(evaluate_core, "build_embedding_client", Mock(return_value=embedding))
        cases = {
            "schema_version": 1,
            "annotation_unit": "policy_id:version",
            "retrieval": [
                {
                    "id": "R1",
                    "split": "dev",
                    "tag": "positive",
                    "query": "换货",
                    "category": "数码",
                    "as_of": "2026-11-02",
                    "relevant": ["P:v1"],
                }
            ],
            "agent": [
                {
                    "id": "A1",
                    "tag": "clarification",
                    "as_of": "2026-11-02",
                    "queries": ["耳机坏了"],
                    "statuses": ["needs_clarification"],
                    "required_tools": [],
                    "forbidden_tools": [],
                    "relevant": [],
                }
            ],
        }
        case_path = tmp_path / "cases.json"
        case_path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
        output = tmp_path / "real-mode-with-mock-clients.md"
        assert (
            evaluate_core.main(["--run", "--cases", str(case_path), "--output", str(output)]) == 0
        )
        report = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
        assert report["agent_rows"][0]["automatic_pass"] is True
        assert report["agent_rows"][0]["turns"][0]["model_calls"] == 1
        assert report["agent_rows"][0]["semantic_review"] == "pending"
        assert len(report["retrieval_rows"]) == 2
        model.bind_tools.return_value.invoke.assert_called_once()
        assert database.read_bytes() == original
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(Conversation)) == 0
        capsys.readouterr()
    finally:
        engine.dispose()
