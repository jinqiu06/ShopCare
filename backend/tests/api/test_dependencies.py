import sqlite3
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from shopcare.agent.graph import build_agent_graph
from shopcare.api.dependencies import initialize_agent_checkpoint, open_agent_checkpointer
from shopcare.db import build_engine


@pytest.fixture
def business_engine(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'business.db').as_posix()}")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.parametrize("fail_in_body", [False, True])
def test_checkpointer_closes_on_success_or_body_failure(business_engine, tmp_path, fail_in_body):
    checkpoint = tmp_path / "private" / "checkpoints.db"
    try:
        with open_agent_checkpointer(business_engine, str(checkpoint), create=True) as saver:
            connection = saver.conn
            assert saver.get_tuple({"configurable": {"thread_id": "demo"}}) is None
            if fail_in_body:
                raise RuntimeError("simulated request failure")
    except RuntimeError as error:
        assert fail_in_body
        assert str(error) == "simulated request failure"
    assert checkpoint.is_file()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    # 默认模式可以打开已有保存器，并同样管理连接释放。
    with open_agent_checkpointer(business_engine, str(checkpoint)) as saver:
        reopened = saver.conn
        assert reopened.execute("SELECT 1").fetchone() == (1,)
    with pytest.raises(sqlite3.ProgrammingError):
        reopened.execute("SELECT 1")


@pytest.mark.parametrize("path", ["", " \t\n "])
def test_checkpointer_rejects_blank_path(business_engine, path):
    with pytest.raises(ValueError, match="路径不能为空"):
        with open_agent_checkpointer(business_engine, path, create=True):
            pytest.fail("blank path must not open a connection")


def test_checkpointer_missing_file_does_not_create_directories(business_engine, tmp_path):
    checkpoint = tmp_path / "not-created" / "checkpoints.db"
    with pytest.raises(FileNotFoundError, match="检查点数据库不存在"):
        with open_agent_checkpointer(business_engine, str(checkpoint)):
            pytest.fail("missing checkpoint must not open")
    assert not checkpoint.parent.exists()


@pytest.mark.parametrize("create", [False, True])
def test_checkpointer_rejects_business_database_alias_before_opening(
    business_engine, tmp_path, monkeypatch, create
):
    business = tmp_path / "business.db"
    business.write_bytes(b"business data must remain unchanged")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="不能与业务数据库相同"):
        with open_agent_checkpointer(business_engine, "./business.db", create=create):
            pytest.fail("must not open the business database as a checkpointer")
    assert business.read_bytes() == b"business data must remain unchanged"


def test_checkpointer_rejects_same_business_file_through_hard_link(business_engine, tmp_path):
    business = tmp_path / "business.db"
    business.write_bytes(b"original business content")
    alias = tmp_path / "alias.db"
    try:
        alias.hardlink_to(business)
    except OSError:
        pytest.skip("filesystem does not support hard links")
    with pytest.raises(ValueError, match="不能与业务数据库相同"):
        with open_agent_checkpointer(business_engine, str(alias), create=True):
            pytest.fail("file aliases must not bypass database isolation")
    assert business.read_bytes() == b"original business content"


def test_checkpointer_allows_separate_file_with_in_memory_business_database(tmp_path):
    engine = build_engine("sqlite:///:memory:")
    try:
        checkpoint = tmp_path / "checkpoints.db"
        with open_agent_checkpointer(engine, str(checkpoint), create=True) as saver:
            assert saver.get_tuple({"configurable": {"thread_id": "demo"}}) is None
        assert checkpoint.is_file()
    finally:
        engine.dispose()


def test_initialize_checkpoint_persists_marker_and_keeps_other_threads_independent(
    business_engine, tmp_path
):
    path = str(tmp_path / "checkpoints.db")
    config = {"configurable": {"thread_id": "first", "checkpoint_ns": ""}}
    with open_agent_checkpointer(business_engine, path, create=True) as saver:
        assert initialize_agent_checkpoint(saver, "first") is None
    with open_agent_checkpointer(business_engine, path) as saver:
        saved = saver.get_tuple(config)
        assert saved is not None
        assert saved.checkpoint["channel_values"] == {}
        assert saved.metadata["shopcare_initialized"] is True
        assert saved.metadata["source"] == "input"
        assert saved.metadata["step"] == -1
        first_checkpoint_id = saved.checkpoint["id"]
        assert saver.get_tuple({"configurable": {"thread_id": "second"}}) is None
        initialize_agent_checkpoint(saver, "second")
        assert saver.get_tuple(config).checkpoint["id"] == first_checkpoint_id


def test_initialize_checkpoint_refuses_to_replace_existing_graph_history(business_engine, tmp_path):
    path = str(tmp_path / "checkpoints.db")
    config = {"configurable": {"thread_id": "existing"}}
    with open_agent_checkpointer(business_engine, path, create=True) as saver:
        initialize_agent_checkpoint(saver, "existing")
        client = Mock()
        client.bind_tools.return_value.invoke.return_value = AIMessage(content="请补充订单号。")
        graph = build_agent_graph(client, [], checkpointer=saver)
        initial = graph.get_state(config)
        assert initial.values == {"messages": []}
        assert initial.metadata["shopcare_initialized"] is True
        graph.invoke(
            {
                "messages": [HumanMessage(content="耳机坏了")],
                "model_calls": 0,
                "tool_calls": 0,
                "stop_reason": "running",
            },
            config=config,
        )
        previous = saver.get_tuple(config)
        with pytest.raises(ValueError, match="不能重复初始化"):
            initialize_agent_checkpoint(saver, "existing")
        current = saver.get_tuple(config)
        assert current.checkpoint["id"] == previous.checkpoint["id"]
        assert [m.content for m in current.checkpoint["channel_values"]["messages"]] == [
            "耳机坏了",
            "请补充订单号。",
        ]


@pytest.mark.parametrize("thread_id", ["", " \t\n "])
def test_initialize_checkpoint_rejects_blank_thread_id(business_engine, tmp_path, thread_id):
    with open_agent_checkpointer(
        business_engine, str(tmp_path / "checkpoints.db"), create=True
    ) as saver:
        with pytest.raises(ValueError, match="不可为空"):
            initialize_agent_checkpoint(saver, thread_id)
        assert list(saver.list(None)) == []
