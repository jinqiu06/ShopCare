from importlib import import_module

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from shopcare.db.base import Base


def build_engine(database_url: str) -> Engine:
    engine = create_engine(database_url)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, connection_record):
            previous_autocommit = connection.autocommit
            connection.autocommit = True

            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()
                connection.autocommit = previous_autocommit

    return engine


def init_db(engine: Engine) -> None:
    # Register all mapped tables before emitting DDL.
    import_module("shopcare.db.models")
    Base.metadata.create_all(engine)
