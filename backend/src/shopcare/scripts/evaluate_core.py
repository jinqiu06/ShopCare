"""Fixed core acceptance: offline lexical baseline or budgeted real retrieval/Agent runs."""

import argparse
import hashlib
import json
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from shopcare.agent.agent import run_conversation_turn
from shopcare.agent.checkpoints import initialize_agent_checkpoint, open_agent_checkpointer
from shopcare.core.config import Settings
from shopcare.db import build_engine, init_db
from shopcare.db.models import PolicyChunkRecord, PolicyEmbeddingRecord
from shopcare.services.conversations import create_conversation
from shopcare.services.embeddings import build_embedding_client
from shopcare.services.llm import build_chat_client
from shopcare.services.policies.chunker import split_policy
from shopcare.services.policies.embeddings import load_policy_embeddings
from shopcare.services.policies.lexical_retriever import list_effective_chunks, search_policy_chunks
from shopcare.services.policies.loader import load_policies
from shopcare.services.policies.semantic_retriever import search_semantic_policy_chunks
from shopcare.services.policies.store import replace_policy_chunks


@dataclass
class RequestBudget:
    max_chat: int = 30
    max_embedding: int = 50
    chat: int = 0
    embedding: int = 0

    def on_request(self, request: httpx.Request) -> None:
        # Count actual SDK requests, including attempts that fail; never log headers/bodies.
        kind = "embedding" if request.url.path.endswith("/embeddings") else "chat"
        count = getattr(self, kind)
        if count >= getattr(self, f"max_{kind}"):
            raise ValueError(f"{kind} request budget exhausted")
        setattr(self, kind, count + 1)


def score_ranking(ranking: list[str], relevant: list[str], k: int) -> dict:
    unique = list(dict.fromkeys(ranking))[:k]
    gold = set(relevant)
    matched = [i + 1 for i, item in enumerate(unique) if item in gold]
    return {
        "ranking": unique,
        "hit": float(bool(matched)) if gold else None,
        "precision": len(matched) / k if gold else None,
        "recall": len(matched) / len(gold) if gold else None,
        "reciprocal_rank": 1 / matched[0] if matched else 0.0 if gold else None,
        "correct_empty": not unique if not gold else None,
    }


def index_fingerprint(session: Session, *, include_embeddings: bool = True) -> str:
    records = {}
    models = (
        (PolicyChunkRecord, PolicyEmbeddingRecord) if include_embeddings else (PolicyChunkRecord,)
    )
    for model in models:
        records[model.__tablename__] = [
            {c.name: getattr(row, c.name) for c in model.__table__.columns}
            for row in session.scalars(select(model).order_by(model.chunk_id))
        ]
    return hashlib.sha256(
        json.dumps(records, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def validate_cases(cases: dict, session: Session) -> None:
    if cases.get("schema_version") != 1 or cases.get("annotation_unit") != "policy_id:version":
        raise ValueError("Unsupported case annotation schema")
    ids = []
    for case in cases["retrieval"]:
        ids.append(case["id"])
        if case["split"] not in {"dev", "heldout"} or not case["query"].strip():
            raise ValueError("Invalid split or empty retrieval query")
        eligible = {
            f"{row.policy_id}:{row.version}"
            for row in list_effective_chunks(
                session, case["category"], date.fromisoformat(case["as_of"])
            )
        }
        if (
            len(case["relevant"]) != len(set(case["relevant"]))
            or not set(case["relevant"]) <= eligible
        ):
            raise ValueError(f"Invalid or inapplicable gold policy in {case['id']}")
    for case in cases["agent"]:
        ids.append(case["id"])
        date.fromisoformat(case["as_of"])
        if not case["queries"] or any(not q.strip() or len(q) > 1000 for q in case["queries"]):
            raise ValueError("Invalid Agent queries")
        if not set(case["statuses"]) <= {
            "answered",
            "needs_clarification",
            "insufficient_evidence",
        }:
            raise ValueError("Invalid Agent expected status")
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Case IDs must be unique")


def evaluate_retrieval(session: Session, cases: list[dict], embedding=None) -> list[dict]:
    rows = []
    for case in cases:
        for mode in ("lexical", "semantic") if embedding is not None else ("lexical",):
            before = perf_counter()
            row = {
                "id": case["id"],
                "split": case["split"],
                "tag": case["tag"],
                "mode": mode,
                "query": case["query"],
                "relevant": case["relevant"],
            }
            params = dict(
                query=case["query"],
                category=case["category"],
                as_of=date.fromisoformat(case["as_of"]),
                top_k=3,
            )
            row["eligible_chunk_count"] = len(
                list_effective_chunks(session, case["category"], params["as_of"])
            )
            try:
                if mode == "lexical":
                    hits = search_policy_chunks(session, **params)
                else:
                    hits = search_semantic_policy_chunks(session, embedding, **params)
                row["hits"] = [hit.model_dump(mode="json") for hit in hits]
                ranking = [f"{hit.chunk.policy_id}:{hit.chunk.version}" for hit in hits]
                row["metrics"] = {
                    str(k): score_ranking(ranking, case["relevant"], k) for k in (1, 3)
                }
                row["outcome"] = "completed"
            except Exception as error:
                # A failed experiment is recorded separately, never scored as an empty retrieval.
                row.update(
                    outcome="error",
                    error_type=type(error).__name__,
                    http_status=getattr(error, "status_code", None),
                )
            row["elapsed_ms"] = round((perf_counter() - before) * 1000, 2)
            rows.append(row)
    return rows


def inspect_turn(messages: list, chunk_policies: dict[str, str], answer: dict, case: dict) -> dict:
    calls = [call for m in messages if isinstance(m, AIMessage) for call in m.tool_calls]
    names = {call["name"] for call in calls}
    results = []
    for message in messages:
        if isinstance(message, ToolMessage):
            results.append(json.loads(message.content))
    cited = [chunk_policies.get(cid, "UNKNOWN") for cid in answer["citation_chunk_ids"]]
    checks = {
        "status": answer["status"] in case["statuses"],
        "required_tools": set(case["required_tools"]) <= names,
        "forbidden_tools": not (set(case["forbidden_tools"]) & names),
        "required_any_tool": not case.get("required_any_tool")
        or bool(set(case["required_any_tool"]) & names),
        "citation_relevance": set(cited) <= set(case["relevant"]),
        "inaccessible_data_not_returned": not case.get("deny_successful_tools")
        or not any(result.get("ok") is True for result in results),
    }
    return {
        "checks": checks,
        "automatic_pass": all(checks.values()),
        "tool_calls": calls,
        "tool_results": results,
        "cited_policies": cited,
        "model_usage": [m.usage_metadata for m in messages if isinstance(m, AIMessage)],
        "semantic_review": "pending",
    }


def evaluate_agent(engine, checkpoint_path: str, cases: list[dict], chat, embedding) -> list[dict]:
    rows = []
    with Session(engine) as session:
        chunk_policies = {
            row.chunk_id: f"{row.policy_id}:{row.version}"
            for row in session.scalars(select(PolicyChunkRecord))
        }
    for case in cases:
        row = {"id": case["id"], "tag": case["tag"], "turns": [], "semantic_review": "pending"}
        before = perf_counter()
        try:
            with Session(engine) as session, session.begin():
                conversation = create_conversation(
                    session, "U001", date.fromisoformat(case["as_of"])
                )
                conversation_id, thread_id = conversation.id, conversation.thread_id
            with open_agent_checkpointer(engine, checkpoint_path, create=True) as saver:
                initialize_agent_checkpoint(saver, thread_id)
                for index, query in enumerate(case["queries"]):
                    try:
                        answer = run_conversation_turn(
                            engine, chat, embedding, saver, "U001", conversation_id, query
                        ).model_dump(mode="json")
                    except Exception:
                        saved = saver.get_tuple({"configurable": {"thread_id": thread_id}})
                        if saved is not None:
                            values = saved.checkpoint["channel_values"]
                            row["failed_checkpoint"] = {
                                "stop_reason": values.get("stop_reason"),
                                "model_calls": values.get("model_calls"),
                                "tool_calls": values.get("tool_calls"),
                                "candidate_messages": [
                                    m.content
                                    for m in values.get("messages", [])
                                    if isinstance(m, AIMessage) and not m.tool_calls
                                ],
                                "tool_requests": [
                                    call
                                    for m in values.get("messages", [])
                                    if isinstance(m, AIMessage)
                                    for call in m.tool_calls
                                ],
                            }
                        raise
                    saved = saver.get_tuple({"configurable": {"thread_id": thread_id}})
                    values = saved.checkpoint["channel_values"]
                    messages = values["messages"]
                    start = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
                    expected = {**case}
                    if index == 0 and len(case["queries"]) > 1:
                        expected.update(
                            statuses=[case["first_status"]],
                            required_tools=[],
                            relevant=case.get("first_relevant", []),
                        )
                    assessment = inspect_turn(messages[start:], chunk_policies, answer, expected)
                    row["turns"].append(
                        {
                            "query": query,
                            "answer": answer,
                            **assessment,
                            "model_calls": values["model_calls"],
                            "tool_calls_count": values["tool_calls"],
                        }
                    )
            row["outcome"] = "completed"
            row["automatic_pass"] = all(turn["automatic_pass"] for turn in row["turns"])
        except Exception as error:
            row.update(
                outcome="error",
                automatic_pass=False,
                error_type=type(error).__name__,
                http_status=getattr(error, "status_code", None),
            )
        row["elapsed_ms"] = round((perf_counter() - before) * 1000, 2)
        rows.append(row)
    return rows


def summarize_retrieval(rows: list[dict]) -> list[dict]:
    summaries = []
    for mode, split in sorted({(r["mode"], r["split"]) for r in rows}):
        subset = [r for r in rows if r["mode"] == mode and r["split"] == split]
        completed = [r for r in subset if r["outcome"] == "completed"]
        positives = [r for r in completed if r["relevant"]]
        negatives = [r for r in completed if not r["relevant"]]
        for k in (1, 3):
            item = {
                "mode": mode,
                "split": split,
                "k": k,
                "planned": len(subset),
                "completed": len(completed),
                "errors": len(subset) - len(completed),
                "positive_n": len(positives),
                "negative_n": len(negatives),
            }
            for metric in ("hit", "precision", "recall", "reciprocal_rank"):
                item[metric] = (
                    sum(r["metrics"][str(k)][metric] for r in positives) / len(positives)
                    if positives
                    else None
                )
            item["correct_empty"] = sum(r["metrics"][str(k)]["correct_empty"] for r in negatives)
            summaries.append(item)
    return summaries


def write_report(report: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# Agent/RAG fixed acceptance",
        "",
        f"Mode: {report['execution_mode']}",
        f"Started UTC: {report['started_at_utc']}",
        "",
        "Scoring unit: policy ID + version; deduplicate returned chunks before scoring.",
        "Precision@K uses fixed K (missing slots count as non-relevant). "
        "Positive cases only for ranking metrics.",
        "MRR is truncated at K. Empty-gold cases use correct-empty, not Recall or MRR.",
        "Errors are separate; completed and planned counts must accompany metrics.",
        "Heldout is acceptance only; do not tune using its results. "
        "Manual semantic review is separate.",
        "",
        "| mode | split | K | completed/planned | Hit | Precision | Recall | MRR | empty n |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in report["retrieval_summary"]:
        values = [
            f"{row[key]:.3f}" if row[key] is not None else "N/A"
            for key in ("hit", "precision", "recall", "reciprocal_rank")
        ]
        lines.append(
            f"| {row['mode']} | {row['split']} | {row['k']} | "
            f"{row['completed']}/{row['planned']} | "
            + " | ".join(values)
            + f" | {row['correct_empty']}/{row['negative_n']} |"
        )
    lines += [
        "",
        "## Retrieval failures",
        "",
        "| case | mode | outcome | missing policies / error |",
        "|---|---|---|---|",
    ]
    for row in report["retrieval_rows"]:
        if row["outcome"] == "error":
            detail = row["error_type"]
        else:
            missing = set(row["relevant"]) - set(row["metrics"]["3"]["ranking"])
            if not missing and (row["relevant"] or not row["hits"]):
                continue
            detail = ", ".join(sorted(missing)) or "unrelated candidates returned"
        lines.append(f"| {row['id']} | {row['mode']} | {row['outcome']} | {detail} |")
    lines += [
        "",
        "## Agent automatic checks (not semantic accuracy)",
        "",
        "| case | outcome | automatic pass | failed checks / error |",
        "|---|---|---|---|",
    ]
    for row in report["agent_rows"]:
        failed = [
            key for turn in row["turns"] for key, value in turn["checks"].items() if not value
        ]
        lines.append(
            f"| {row['id']} | {row['outcome']} | {row['automatic_pass']} | "
            f"{row.get('error_type') or ', '.join(failed)} |"
        )
    lines += [
        "",
        "All generated answers require semantic review; "
        "automatic pass does not certify business correctness.",
        "Offline skips semantic retrieval and Agent evaluation; "
        "no simulated semantic score is reported.",
        "Source database is copied read-only; "
        "evaluation writes only to the temporary copy/checkpoints.",
        "JSON contains full case traces, scores, timings, hashes and actual request counts.",
    ]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", action="store_true", help="Call configured embedding/chat providers"
    )
    parser.add_argument("--cases", default="data/eval/core_acceptance.json")
    parser.add_argument("--output", default="data/private/eval/core-acceptance.md")
    parser.add_argument("--max-chat-requests", type=int, default=30)
    parser.add_argument("--max-embedding-requests", type=int, default=50)
    args = parser.parse_args(argv)
    if args.max_chat_requests < 0 or args.max_embedding_requests < 0:
        parser.error("request budgets must not be negative")
    raw = Path(args.cases).read_bytes()
    cases = json.loads(raw)
    budget = RequestBudget(args.max_chat_requests, args.max_embedding_requests)
    report = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "execution_mode": "real" if args.run else "offline_lexical_only",
        "cases_sha256": hashlib.sha256(raw).hexdigest(),
        "cases": cases,
        "agent_rows": [],
        "semantic_review": "pending",
        "top_k": 3,
    }
    sources = sorted(Path("backend/src/shopcare").rglob("*.py"))
    report["source_sha256"] = hashlib.sha256(
        b"".join(p.as_posix().encode() + p.read_bytes() for p in sources)
    ).hexdigest()
    report["git_head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    with TemporaryDirectory(prefix="shopcare-core-eval-") as directory:
        temporary = Path(directory)
        database = temporary / "evaluation.db"
        settings = Settings()
        if args.run:
            url = make_url(settings.database_url)
            if (
                url.get_backend_name() != "sqlite"
                or not url.database
                or not Path(url.database).is_file()
            ):
                raise ValueError("Real mode requires an existing SQLite index")
            uri = Path(url.database).resolve().as_uri() + "?mode=ro"
            with (
                closing(sqlite3.connect(uri, uri=True)) as source,
                closing(sqlite3.connect(database)) as target,
            ):
                source.backup(target)
        engine = build_engine(f"sqlite:///{database.as_posix()}")
        try:
            if not args.run:
                init_db(engine)
                with Session(engine) as session, session.begin():
                    for policy in load_policies(Path("data/policies")):
                        replace_policy_chunks(session, policy, split_policy(policy))
            with Session(engine) as session:
                validate_cases(cases, session)
                report["index_sha256"] = index_fingerprint(session)
                report["corpus_sha256"] = index_fingerprint(session, include_embeddings=False)
                report["chunk_count"] = len(list(session.scalars(select(PolicyChunkRecord))))
                if args.run:
                    # Reject mismatched/incomplete source indexes before any provider request.
                    for case in cases["retrieval"]:
                        chunks = list_effective_chunks(
                            session, case["category"], date.fromisoformat(case["as_of"])
                        )
                        load_policy_embeddings(
                            session,
                            chunks,
                            settings.embedding_model_id,
                            settings.embedding_dimensions,
                        )
            with httpx.Client(
                timeout=30, event_hooks={"request": [budget.on_request]}
            ) as http_client:
                embedding = (
                    build_embedding_client(settings, http_client=http_client) if args.run else None
                )
                chat = build_chat_client(settings, http_client=http_client) if args.run else None
                report["models"] = {
                    "chat": settings.llm_model_id if args.run else None,
                    "embedding": settings.embedding_model_id if args.run else None,
                    "dimensions": settings.embedding_dimensions if args.run else None,
                }
                with Session(engine) as session:
                    report["retrieval_rows"] = evaluate_retrieval(
                        session, cases["retrieval"], embedding
                    )
                if args.run:
                    report["agent_rows"] = evaluate_agent(
                        engine, str(temporary / "checkpoints.db"), cases["agent"], chat, embedding
                    )
        finally:
            engine.dispose()
    report["retrieval_summary"] = summarize_retrieval(report["retrieval_rows"])
    report["requests"] = {
        "chat": budget.chat,
        "embedding": budget.embedding,
        "max_chat": budget.max_chat,
        "max_embedding": budget.max_embedding,
    }
    write_report(report, Path(args.output))
    print(
        json.dumps(
            {
                "report": args.output,
                "mode": report["execution_mode"],
                "requests": report["requests"],
                "retrieval_summary": report["retrieval_summary"],
            },
            ensure_ascii=False,
        )
    )
    return int(
        any(row["outcome"] == "error" for row in report["retrieval_rows"] + report["agent_rows"])
    )


if __name__ == "__main__":
    raise SystemExit(main())
