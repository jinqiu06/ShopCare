"""Run the developer's fixed order-answer pipeline as a local demo."""

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from time import perf_counter

from openai import OpenAIError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.api.dependencies import get_current_user_id
from shopcare.core.config import Settings
from shopcare.db import build_engine
from shopcare.services.embeddings import build_embedding_client
from shopcare.services.llm import build_chat_client
from shopcare.services.order_answers import answer_order_question, get_order_answer_context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ShopCare 单次订单咨询演示，默认只预览")
    parser.add_argument("--order-id", default="O004")
    parser.add_argument("--item-id", default="O004-I01")
    parser.add_argument("--query", default="耳机左边没有声音，能申请换货吗？")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 11, 2))
    parser.add_argument("--run", action="store_true", help="真实调用 embedding 和对话模型")
    parser.add_argument("--output", help="可选：将本次成功结果保存为 JSON")
    args = parser.parse_args(argv)
    engine = None
    try:
        if not args.query.strip():
            raise ValueError("问题不能为空")
        settings = Settings()
        url = make_url(settings.database_url)
        if (
            url.get_backend_name() != "sqlite"
            or not url.database
            or not Path(url.database).is_file()
        ):
            raise ValueError("需要已有本地 SQLite 文件")
        engine = build_engine(settings.database_url)
        user_id = get_current_user_id()
        with Session(engine) as session:
            if not args.run:
                order, item = get_order_answer_context(
                    session, user_id, args.order_id, args.item_id
                )
                print(
                    json.dumps(
                        {
                            "dry_run": True,
                            "demo_user_id": user_id,
                            "order_id": order.id,
                            "item_id": item.id,
                            "category": item.category,
                            "as_of": args.as_of.isoformat(),
                            "query": args.query,
                            "max_embedding_calls": 1,
                            "max_chat_calls": 1,
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
            embedding = build_embedding_client(settings)
            chat = build_chat_client(settings)
            print("正式演示：最多一次问题向量和一次对话模型调用；不修改数据库。", file=sys.stderr)
            started = datetime.now(timezone.utc).isoformat()
            before = perf_counter()
            answer, response = answer_order_question(
                session,
                embedding,
                chat,
                user_id,
                args.order_id,
                args.item_id,
                args.query,
                args.as_of,
            )
            report = {
                "started_at_utc": started,
                "demo_user_id": user_id,
                "order_id": args.order_id,
                "item_id": args.item_id,
                "query": args.query,
                "as_of": args.as_of.isoformat(),
                "configured_chat_model": settings.llm_model_id,
                "returned_chat_model": response.response_metadata.get("model_name"),
                "embedding_model": settings.embedding_model_id,
                "embedding_dimensions": settings.embedding_dimensions,
                "elapsed_ms": round((perf_counter() - before) * 1000, 2),
                "answer": answer.model_dump(mode="json"),
                "usage_metadata": response.usage_metadata,
                "usage_note": "对话模型返回的 usage；空值表示不可用，不包含 embedding 用量。",
            }
        encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(encoded + "\n", encoding="utf-8")
        print(encoded)
        return 0
    except LookupError:
        print("订单或商品不存在或不可访问；模型未调用。", file=sys.stderr)
        return 1
    except (OSError, ValueError, SQLAlchemyError, OpenAIError, TimeoutError):
        print("回答失败：请检查配置、索引、模型返回或网络；不作为正常答案返回。", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
