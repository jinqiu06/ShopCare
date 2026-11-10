from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from shopcare.agent.checkpoints import (
    initialize_agent_checkpoint as initialize_agent_checkpoint,
)
from shopcare.agent.checkpoints import (
    open_agent_checkpointer as open_agent_checkpointer,
)


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session


def get_current_user_id() -> str:
    return "U001"
