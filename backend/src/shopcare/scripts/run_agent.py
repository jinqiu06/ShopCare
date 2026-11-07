"""Run the bounded read-only Agent demo; preview unless --run is supplied."""

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from time import perf_counter

from langchain_core.messages import AIMessage
from openai import OpenAIError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.agent.graph import build_agent_graph
from shopcare.agent.prompts import build_agent_messages
from shopcare.agent.results import parse_agent_answer
from shopcare.api.dependencies import get_current_user_id
from shopcare.core.config import Settings
from shopcare.db import build_engine
from shopcare.services.embeddings import build_embedding_client
from shopcare.services.llm import build_chat_client
from shopcare.tools.orders import build_order_tools
from shopcare.tools.policy import build_policy_tools


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ShopCare 单次只读Agent演示，默认只预览")
    parser.add_argument("--query", default="订单O004的O004-I01耳机坏了，能换货吗？")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 11, 2))
    parser.add_argument("--max-model-calls", type=int, default=3)
    parser.add_argument("--max-tool-calls", type=int, default=4)
    parser.add_argument("--max-output-chars", type=int, default=12000)
    parser.add_argument("--run", action="store_true", help="实际运行，可能调用对话和embedding服务")
    parser.add_argument("--output", help="可选：保存图运行后的成功或失败JSON报告")
    args = parser.parse_args(argv)
    engine = None
    try:
        query = args.query.strip()
        if not query or len(query) > 1000:
            raise ValueError("问题必须为1至1000个字符")
        limits = {
            "max_model_calls": args.max_model_calls,
            "max_tool_calls": args.max_tool_calls,
            "max_output_chars": args.max_output_chars,
        }
        if any(value <= 0 for value in limits.values()):
            raise ValueError("调用预算和输出限制必须为正整数")

        settings = Settings()
        url = make_url(settings.database_url)
        if (
            url.get_backend_name() != "sqlite"
            or not url.database
            or not Path(url.database).is_file()
        ):
            raise ValueError("需要已有本地SQLite文件")
        user_id = get_current_user_id()
        preview = {
            "dry_run": True,
            "demo_user_id": user_id,
            "query": query,
            "as_of": args.as_of.isoformat(),
            "limits": limits,
            "max_embedding_calls": args.max_tool_calls,
            "tools": ["lookup_order", "search_order_policies"],
            "note": "仅检查输入及本地数据库文件存在；未检查订单、索引或模型连通性。",
        }
        if not args.run:
            print(json.dumps(preview, ensure_ascii=False, indent=2))
            return 0

        print(
            f"正式演示：最多{args.max_model_calls}次对话调用尝试、"
            f"{args.max_tool_calls}次工具派发尝试；"
            f"问题向量调用至多{args.max_tool_calls}次；数据库只读。",
            file=sys.stderr,
        )
        embedding = build_embedding_client(settings)
        chat = build_chat_client(settings)
        engine = build_engine(settings.database_url)
        started = datetime.now(timezone.utc).isoformat()
        before = perf_counter()
        with Session(engine) as session:
            session.execute(text("PRAGMA query_only=ON"))
            tools = build_order_tools(session, user_id, args.as_of) + build_policy_tools(
                session, embedding, user_id, args.as_of
            )
            graph = build_agent_graph(chat, tools, **limits, validate_answers=True)
            result = graph.invoke(
                {
                    "messages": build_agent_messages(query, args.as_of),
                    "model_calls": 0,
                    "tool_calls": 0,
                    "stop_reason": "running",
                }
            )
            answer = None
            error_code = None
            if result["stop_reason"] != "completed":
                error_code = "AGENT_RUN_FAILED"
            else:
                try:
                    answer = parse_agent_answer(result)
                except ValueError:
                    error_code = "ANSWER_INVALID"

        responses = [m for m in result["messages"] if isinstance(m, AIMessage)]
        report = {
            "dry_run": False,
            "started_at_utc": started,
            "demo_user_id": user_id,
            "query": query,
            "as_of": args.as_of.isoformat(),
            "configured_chat_model": settings.llm_model_id,
            "embedding_model": settings.embedding_model_id,
            "embedding_dimensions": settings.embedding_dimensions,
            "limits": limits,
            "stop_reason": result["stop_reason"],
            "model_calls": result["model_calls"],
            "tool_calls": result["tool_calls"],
            "elapsed_ms": round((perf_counter() - before) * 1000, 2),
            "answer_validated": answer is not None,
            "answer": answer.model_dump(mode="json") if answer is not None else None,
            "error_code": error_code,
            "tool_requests": [
                {"id": call["id"], "name": call["name"]}
                for message in responses
                for call in message.tool_calls
            ],
            "model_responses": [
                {
                    "returned_model": message.response_metadata.get("model_name"),
                    "usage_metadata": message.usage_metadata,
                }
                for message in responses
            ],
            "usage_note": "仅含已保留对话响应的usage；null为不可用，不含失败尝试及embedding用量。",
            "validation_note": "通过表示格式与引用来源有效，仍需审查政策解释、事实和能力边界。",
        }
        encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(encoded + "\n", encoding="utf-8")
        print(encoded)
        return 0 if answer is not None else 1
    except (OSError, ValueError, SQLAlchemyError, OpenAIError, TimeoutError):
        print(
            "Agent演示失败：请检查输入、配置、数据库或模型服务；未返回有效答案。", file=sys.stderr
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
