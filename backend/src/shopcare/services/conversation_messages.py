import logging
import sqlite3
from contextlib import ExitStack

from httpx import Client
from openai import APITimeoutError, OpenAIError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.agent.agent import run_conversation_turn
from shopcare.agent.checkpoints import open_agent_checkpointer
from shopcare.core.config import Settings
from shopcare.schemas.answers import PolicyAnswer
from shopcare.services.conversations import (
    ConversationNotFoundError,
    ConversationTurnConflictError,
    get_conversation_for_user,
)
from shopcare.services.embeddings import build_embedding_client
from shopcare.services.llm import build_chat_client

logger = logging.getLogger(__name__)


class ConversationMessageError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def get_ready_conversation_thread(engine: Engine, user_id: str, conversation_id: str) -> str:
    with Session(engine) as session:
        conversation = get_conversation_for_user(session, user_id, conversation_id)
        if conversation is None:
            raise ConversationMessageError("CONVERSATION_NOT_FOUND")
        if conversation.status != "ready":
            raise ConversationMessageError("CONVERSATION_NOT_READY")
        return conversation.thread_id


def send_message(
    engine: Engine,
    settings: Settings,
    user_id: str,
    conversation_id: str,
    query: str,
) -> PolicyAnswer:
    try:
        return _send_message(engine, settings, user_id, conversation_id, query)
    except ConversationNotFoundError as error:
        raise ConversationMessageError("CONVERSATION_NOT_FOUND") from error
    except ConversationTurnConflictError as error:
        raise ConversationMessageError("CONVERSATION_NOT_READY") from error
    except (SQLAlchemyError, sqlite3.Error) as error:
        raise ConversationMessageError("CONVERSATION_UNAVAILABLE") from error
    except (APITimeoutError, TimeoutError) as error:
        raise ConversationMessageError("AGENT_TIMEOUT") from error
    except OpenAIError as error:
        logger.warning(
            "Conversation model request failed: type=%s status=%s cause=%s",
            type(error).__name__,
            getattr(error, "status_code", None),
            type(error.__cause__).__name__ if error.__cause__ is not None else None,
        )
        raise ConversationMessageError("AGENT_UPSTREAM_ERROR") from error


def _send_message(
    engine: Engine,
    settings: Settings,
    user_id: str,
    conversation_id: str,
    query: str,
) -> PolicyAnswer:
    thread_id = get_ready_conversation_thread(engine, user_id, conversation_id)
    with ExitStack() as resources:
        try:
            checkpointer = resources.enter_context(
                open_agent_checkpointer(engine, settings.agent_checkpoint_db)
            )
        except FileNotFoundError as error:
            raise ConversationMessageError("CONVERSATION_CHECKPOINT_MISSING") from error
        except (sqlite3.Error, OSError, ValueError) as error:
            raise ConversationMessageError("CONVERSATION_UNAVAILABLE") from error

        try:
            checkpoint = checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
        except sqlite3.Error as error:
            raise ConversationMessageError("CONVERSATION_UNAVAILABLE") from error
        if checkpoint is None:
            raise ConversationMessageError("CONVERSATION_CHECKPOINT_MISSING")

        http_client = resources.enter_context(Client(timeout=30))
        try:
            chat_client = build_chat_client(settings, http_client=http_client)
            embedding_client = build_embedding_client(settings, http_client=http_client)
        except ValueError as error:
            raise ConversationMessageError("AGENT_NOT_CONFIGURED") from error

        return run_conversation_turn(
            engine,
            chat_client,
            embedding_client,
            checkpointer,
            user_id,
            conversation_id,
            query,
        )
