from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy.engine import Engine


@contextmanager
def open_agent_checkpointer(
    engine: Engine,
    checkpoint_db: str,
    *,
    create: bool = False,
) -> Iterator[SqliteSaver]:
    """为调用方管理检查点连接，不允许复用业务数据库。"""
    if not checkpoint_db.strip():
        raise ValueError("检查点数据库路径不能为空")
    checkpoint_path = Path(checkpoint_db).resolve()

    database = engine.url.database
    if engine.url.get_backend_name() == "sqlite" and database and database != ":memory:":
        business_path = Path(database).resolve()
        if checkpoint_path == business_path or (
            checkpoint_path.exists()
            and business_path.exists()
            and checkpoint_path.samefile(business_path)
        ):
            raise ValueError("检查点数据库不能与业务数据库相同")

    if create:
        checkpoint_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
    elif not checkpoint_path.is_file():
        raise FileNotFoundError("检查点数据库不存在")

    with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
        yield saver


def initialize_agent_checkpoint(
    checkpointer: SqliteSaver,
    thread_id: str,
) -> None:
    if not thread_id.strip():
        raise ValueError("thread_id不可为空")

    config = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }
    if checkpointer.get_tuple(config) is not None:
        raise ValueError("会话检查点已存在，不能重复初始化")

    checkpointer.put(
        config,
        empty_checkpoint(),
        {
            "source": "input",
            "step": -1,
            "parents": {},
            "shopcare_initialized": True,
        },
        {},
    )
