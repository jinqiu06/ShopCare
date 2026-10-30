"""Compare existing lexical and semantic services on the same fixed cases."""

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from time import perf_counter

from openai import OpenAIError
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.core.config import Settings
from shopcare.db import build_engine
from shopcare.db.models import PolicyChunkRecord, PolicyEmbeddingRecord
from shopcare.services.embeddings import build_embedding_client
from shopcare.services.policy_embeddings import load_policy_embeddings
from shopcare.services.policy_retriever import list_effective_chunks, search_policy_chunks
from shopcare.services.semantic_retriever import search_semantic_policy_chunks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同库十题词法/语义检索对照；默认仅预览")
    parser.add_argument("--run", action="store_true", help="执行真实模型评测，产生 API 用量")
    parser.add_argument("--cases", default="data/eval/retrieval_baseline.json")
    parser.add_argument("--output", default="docs/retrieval-comparison.md")
    parser.add_argument("--max-model-calls", type=int, default=10)
    args = parser.parse_args(argv)
    engine = None
    try:
        raw_cases = Path(args.cases).read_bytes()
        cases = json.loads(raw_cases)
        if not cases or args.max_model_calls < 1 or len(cases) > args.max_model_calls:
            raise ValueError("案例数超出调用预算或输入为空")
        # Validate all required fields/dates before any provider request.
        for case in cases:
            date.fromisoformat(case["as_of"])
            for field in ["id", "query", "category", "expected_policy_id", "expected_version"]:
                case[field]
        preview = {"cases": len(cases), "max_query_calls": len(cases), "top_k": 3}
        if not args.run:
            print(json.dumps({**preview, "dry_run": True}))
            return 0
        settings = Settings()
        url = make_url(settings.database_url)
        if (
            url.get_backend_name() != "sqlite"
            or not url.database
            or not Path(url.database).is_file()
        ):
            raise ValueError("需要已经构建索引的本地 SQLite 文件")
        engine = build_engine(settings.database_url)
        rows = []
        calls = 0
        started = datetime.now(timezone.utc).isoformat()
        with Session(engine) as session:
            # Preflight every case before spending any model budget.
            for case in cases:
                chunks = list_effective_chunks(
                    session, case["category"], date.fromisoformat(case["as_of"])
                )
                load_policy_embeddings(
                    session, chunks, settings.embedding_model_id, settings.embedding_dimensions
                )
            snapshot = {}
            for model in [PolicyChunkRecord, PolicyEmbeddingRecord]:
                snapshot[model.__tablename__] = [
                    {
                        column.name: getattr(record, column.name)
                        for column in model.__table__.columns
                    }
                    for record in session.scalars(select(model).order_by(model.chunk_id))
                ]
            index_hash = hashlib.sha256(
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str).encode()
            ).hexdigest()
            client = build_embedding_client(settings)
            print(f"开始 {len(cases)} 题对照，最多 {len(cases)} 次问题向量调用。", file=sys.stderr)
            for case in cases:
                params = dict(
                    query=case["query"],
                    category=case["category"],
                    as_of=date.fromisoformat(case["as_of"]),
                    top_k=3,
                )
                row = {"case": case}
                for mode in ["lexical", "semantic"]:
                    before = perf_counter()
                    if mode == "lexical":
                        hits = search_policy_chunks(session, **params)
                    else:
                        if case["query"].strip() and list_effective_chunks(
                            session, params["category"], params["as_of"]
                        ):
                            calls += 1
                        hits = search_semantic_policy_chunks(session, client, **params)
                    rank = next(
                        (
                            i + 1
                            for i, hit in enumerate(hits)
                            if (hit.chunk.policy_id, hit.chunk.version)
                            == (case["expected_policy_id"], case["expected_version"])
                        ),
                        None,
                    )
                    row[mode] = {
                        "rank": rank,
                        "elapsed_ms": round((perf_counter() - before) * 1000, 2),
                        "hits": [hit.model_dump(mode="json") for hit in hits],
                    }
                rows.append(row)
                print(f"完成 {case['id']}", file=sys.stderr)
        positive = [row for row in rows if row["case"]["expected_policy_id"] is not None]
        negative = [row for row in rows if row["case"]["expected_policy_id"] is None]
        summary = {
            mode: {
                "positive_cases": len(positive),
                "hit_at_1": sum(row[mode]["rank"] == 1 for row in positive),
                "hit_at_3": sum(row[mode]["rank"] is not None for row in positive),
                "negative_cases": len(negative),
                "correct_empty": sum(not row[mode]["hits"] for row in negative),
            }
            for mode in ["lexical", "semantic"]
        }
        report = {
            "started_at_utc": started,
            "model_id": settings.embedding_model_id,
            "dimensions": settings.embedding_dimensions,
            "base_url": settings.embedding_base_url or settings.llm_base_url,
            "cases_sha256": hashlib.sha256(raw_cases).hexdigest(),
            "index_sha256": index_hash,
            "query_model_calls": calls,
            "top_k": 3,
            "summary": summary,
            "rows": rows,
        }
        lines = [
            "# 词法与语义检索对照",
            "",
            f"运行时间（UTC）：{started}",
            f"模型：{settings.embedding_model_id}；维度：{settings.embedding_dimensions}；top_k=3。",
            f"问题向量调用 {calls} 次；不重建政策向量，不修改数据库。",
            "",
            "同一本地数据库、同一组开发问题、相同类别和日期过滤，直接调用服务层；未经过 HTTP。",
            "语义检索未设置相关性阈值，因此无关问题也可能返回候选。",
            "",
            "| 模式 | 正例 Hit@1 | 正例 Hit@3 | 负例正确空返回 |",
            "|---|---|---|---|",
        ]
        for mode, item in summary.items():
            lines.append(
                f"| {mode} | {item['hit_at_1']}/{len(positive)} | "
                f"{item['hit_at_3']}/{len(positive)} | "
                f"{item['correct_empty']}/{len(negative)} |"
            )
        lines += [
            "",
            "| 案例 | 问题 | 目标 | 词法排名 | 语义排名 | 语义首条（分数） |",
            "|---|---|---|---|---|---|",
        ]
        for row in rows:
            case = row["case"]
            first = row["semantic"]["hits"][:1]
            top = (
                (
                    f"{first[0]['chunk']['policy_id']}:{first[0]['chunk']['version']} "
                    f"({first[0]['score']:.4f})"
                )
                if first
                else "空"
            )
            target = (
                f"{case['expected_policy_id']}:{case['expected_version']}"
                if case["expected_policy_id"]
                else "应为空"
            )
            lines.append(
                f"| {case['id']} | {case['query'].replace('|', '/')} | {target} | "
                f"{row['lexical']['rank'] or '未命中'} | "
                f"{row['semantic']['rank'] or '未命中'} | {top} |"
            )
        lines += [
            "",
            "排名对负例不适用，负例依据是否为空单独统计。",
            "这是十个人工开发案例，不是独立保留集，不代表真实业务准确率。",
            "两种分数含义不同，不能直接比较；耗时是单次顺序运行，不能作为性能基准。",
            "逐题完整候选、分数、耗时和数据指纹见同名 JSON 文件。",
            "本轮不依据这十题调相关性阈值或增加混合排序。",
        ]
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.with_suffix(".json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        output.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {"summary": summary, "query_model_calls": calls, "report": str(output)},
                ensure_ascii=False,
            )
        )
        return 0
    except (OSError, ValueError, KeyError, TypeError, SQLAlchemyError, OpenAIError, TimeoutError):
        print(
            "对照失败：请检查案例、完整向量索引、模型配置或网络；失败不计为检索未命中。",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
