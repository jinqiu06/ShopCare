"""Preview or run one turn of a persistent demo conversation."""

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from time import perf_counter

from langgraph.checkpoint.sqlite import SqliteSaver
from openai import OpenAIError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.agent.agent import run_conversation_turn
from shopcare.api.dependencies import get_current_user_id
from shopcare.core.config import Settings
from shopcare.db import build_engine, init_db
from shopcare.services.conversations import create_conversation, get_conversation_for_user
from shopcare.services.embeddings import build_embedding_client
from shopcare.services.llm import build_chat_client


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ShopCare持久化会话演示，默认只预览")
    parser.add_argument("--conversation-id", help="继续已有会话；省略则新建")
    parser.add_argument("--query", default="订单O004的O004-I01耳机坏了，能换货吗？")
    parser.add_argument("--as-of", type=date.fromisoformat, help="仅新会话可设，默认2026-11-02")
    parser.add_argument("--checkpoint-db", default="data/private/agent-checkpoints.db")
    parser.add_argument("--run", action="store_true", help="实际执行并保存会话，可能产生模型用量")
    parser.add_argument("--output", help="可选：保存本轮JSON结果；预览不写文件")
    args = parser.parse_args(argv)
    engine = None
    try:
        query = args.query.strip()
        if not query or len(query) > 1000:
            raise ValueError("问题必须为1至1000个字符")
        if args.conversation_id is not None and not args.conversation_id.strip():
            raise ValueError("会话编号不能为空")
        if args.conversation_id and args.as_of is not None:
            raise ValueError("已有会话不能覆盖参照日期")
        settings = Settings()
        url = make_url(settings.database_url)
        if (
            url.get_backend_name() != "sqlite"
            or not url.database
            or not Path(url.database).is_file()
        ):
            raise ValueError("需要已有本地SQLite业务数据库")
        checkpoint_path = Path(args.checkpoint_db).resolve()
        if checkpoint_path == Path(url.database).resolve():
            raise ValueError("检查点数据库与业务数据库必须使用不同文件")
        user_id = get_current_user_id()
        if not args.run:
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "action": "continue" if args.conversation_id else "create",
                        "demo_user_id": user_id,
                        "conversation_id": args.conversation_id,
                        "query": query,
                        "as_of": None
                        if args.conversation_id
                        else (args.as_of or date(2026, 11, 2)).isoformat(),
                        "max_model_calls": 3,
                        "max_tool_calls": 4,
                        "max_embedding_calls": 4,
                        "note": "仅检查输入及文件存在；未检查会话、索引或模型连通性，不写数据。",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        embedding = build_embedding_client(settings)
        chat = build_chat_client(settings)
        engine = build_engine(settings.database_url)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        print(
            "正式执行：最多3次对话、4次工具尝试；保存会话和检查点，不改订单或政策。",
            file=sys.stderr,
        )
        with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            # 为旧演示库补建缺失表，不生成或重置演示订单/政策。
            init_db(engine)
            with Session(engine) as session, session.begin():
                if args.conversation_id:
                    conversation = get_conversation_for_user(
                        session, user_id, args.conversation_id.strip()
                    )
                    if conversation is None:
                        raise LookupError("会话不存在或不可访问")
                else:
                    conversation = create_conversation(
                        session, user_id, args.as_of or date(2026, 11, 2)
                    )
                conversation_id = conversation.id
                as_of = conversation.as_of
                thread_id = conversation.thread_id

            if (
                args.conversation_id
                and saver.get_tuple({"configurable": {"thread_id": thread_id}}) is None
            ):
                raise ValueError("已有会话的检查点不存在，请检查是否使用了原检查点数据库")

            before = perf_counter()
            answer = None
            error_code = None
            try:
                answer = run_conversation_turn(
                    engine,
                    chat,
                    embedding,
                    saver,
                    user_id,
                    conversation_id,
                    query,
                )
            except Exception:
                error_code = "CONVERSATION_RUN_FAILED"

            status = None
            try:
                with Session(engine) as session:
                    record = get_conversation_for_user(session, user_id, conversation_id)
                    if record is not None:
                        status = record.status
            except SQLAlchemyError:
                pass  # 状态不可读取时记为null，不推断已成功收尾。
            report = {
                "dry_run": False,
                "demo_user_id": user_id,
                "conversation_id": conversation_id,
                "conversation_status": status,
                "as_of": as_of.isoformat(),
                "query": query,
                "elapsed_ms": round((perf_counter() - before) * 1000, 2),
                "answer_validated": answer is not None,
                "answer": answer.model_dump(mode="json") if answer is not None else None,
                "error_code": error_code,
            }
        encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(encoded + "\n", encoding="utf-8")
        print(encoded)
        return 0 if answer is not None else 1
    except (
        OSError,
        ValueError,
        LookupError,
        SQLAlchemyError,
        sqlite3.Error,
        OpenAIError,
        TimeoutError,
    ):
        print("会话演示失败：请检查输入、配置、数据库或会话访问权限。", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
