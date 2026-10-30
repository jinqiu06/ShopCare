"""Build stored policy vectors using the existing core embedding services."""

import argparse
import json
import sys
from pathlib import Path

from openai import OpenAIError
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.core.config import Settings
from shopcare.db import build_engine, init_db
from shopcare.db.models import PolicyChunkRecord
from shopcare.services.embeddings import build_embedding_client
from shopcare.services.policy_embeddings import embed_policy_chunks, save_policy_embeddings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量生成并保存政策向量（正式运行会调用模型）")
    parser.add_argument("--database-url", help="覆盖配置中的本地 SQLite 地址")
    parser.add_argument(
        "--dry-run", action="store_true", help="只预览片段数，不调用模型或写入数据库"
    )
    args = parser.parse_args(argv)
    engine = None
    try:
        settings = Settings()
        url = make_url(args.database_url or settings.database_url)
        if url.get_backend_name() != "sqlite":
            parser.error("当前命令仅支持本地 SQLite")
        if not url.database or url.database == ":memory:":
            raise ValueError("需要已导入政策的文件数据库")
        database_path = Path(url.database).expanduser().resolve()
        if not database_path.is_file():
            raise ValueError("数据库不存在，请先导入政策")
        url = url.set(database=str(database_path))
        engine = build_engine(url.render_as_string(hide_password=False))
        statement = select(PolicyChunkRecord).order_by(PolicyChunkRecord.chunk_id)
        with Session(engine) as session:
            chunks = list(session.scalars(statement).all())
            # Save every source column to detect changes while the remote call runs.
            original = [
                tuple(getattr(chunk, column.name) for column in PolicyChunkRecord.__table__.columns)
                for chunk in chunks
            ]
        if not chunks:
            raise ValueError("没有政策片段，请先导入政策")
        summary = {
            "chunks": len(chunks),
            "model_id": settings.embedding_model_id,
            "dimensions": settings.embedding_dimensions,
        }
        if args.dry_run:
            print(json.dumps({**summary, "dry_run": True}, ensure_ascii=False))
            return 0
        client = build_embedding_client(settings)
        print(f"将为 {len(chunks)} 个片段调用 embedding 模型，可能产生 API 用量。", file=sys.stderr)
        # No database session is held during remote requests. Failed batches do not overwrite data.
        vectors = embed_policy_chunks(client, chunks)
        init_db(engine)
        with Session(engine) as session, session.begin():
            current = [
                tuple(getattr(chunk, column.name) for column in PolicyChunkRecord.__table__.columns)
                for chunk in session.scalars(statement)
            ]
            if current != original:
                raise ValueError("政策片段已变化，请重新运行建索引")
            written = save_policy_embeddings(
                session,
                chunks,
                vectors,
                settings.embedding_model_id,
                settings.embedding_dimensions,
            )
        print(json.dumps({**summary, "vectors_written": written}, ensure_ascii=False))
        return 0
    except ValueError:
        print(
            "索引构建失败：请检查配置、片段和向量；片段变化时请重跑。旧向量未提交替换。",
            file=sys.stderr,
        )
        return 1
    except (OSError, SQLAlchemyError, OpenAIError, TimeoutError):
        print(
            "索引构建失败：请检查数据库、网络、API 权限或额度。旧向量未提交替换。", file=sys.stderr
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
