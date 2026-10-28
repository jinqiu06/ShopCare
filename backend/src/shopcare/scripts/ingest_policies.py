"""Import all policy versions using the developer's validated pipeline."""

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.core.config import Settings
from shopcare.db import build_engine, init_db
from shopcare.services.policy_chunker import split_policy
from shopcare.services.policy_loader import load_policies
from shopcare.services.policy_store import replace_policy_chunks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入 ShopCare 模拟政策片段")
    parser.add_argument("--policy-dir", default="data/policies")
    parser.add_argument("--database-url", help="覆盖配置中的本地 SQLite 地址")
    parser.add_argument("--chunk-size", type=int, default=300)
    parser.add_argument("--overlap", type=int, default=50)
    args = parser.parse_args(argv)
    if args.chunk_size <= 0 or not 0 <= args.overlap < args.chunk_size:
        parser.error("必须满足 chunk-size > 0 且 0 <= overlap < chunk-size")

    engine = None
    try:
        policies = load_policies(Path(args.policy_dir))
        if not policies:
            raise ValueError("政策目录为空")
        # Validate the entire input batch before writing any policy.
        batches = [
            (policy, split_policy(policy, args.chunk_size, args.overlap)) for policy in policies
        ]
        url = make_url(args.database_url or Settings().database_url)
        if url.get_backend_name() != "sqlite":
            parser.error("当前导入命令仅支持本地 SQLite")
        if url.database and url.database != ":memory:":
            database_path = Path(url.database).expanduser().resolve()
            database_path.parent.mkdir(parents=True, exist_ok=True)
            url = url.set(database=str(database_path))
        engine = build_engine(url.render_as_string(hide_password=False))
        init_db(engine)
        with Session(engine) as session, session.begin():
            written = sum(
                replace_policy_chunks(session, policy, chunks) for policy, chunks in batches
            )
        print(json.dumps({"policies_processed": len(policies), "chunks_written": written}))
        return 0
    except (OSError, ValueError, SQLAlchemyError):
        print("政策导入失败，请检查文件格式、窗口参数或数据库状态", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
