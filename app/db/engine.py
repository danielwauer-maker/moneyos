from typing import Any

from sqlalchemy import Engine, create_engine, event


def build_engine(database_url: str, **kwargs: Any) -> Engine:
    """Create the database engine behind one replaceable backend boundary.

    A future SQLCipher adapter can be introduced here without changing domain or
    service code.
    """
    connect_args = dict(kwargs.pop("connect_args", {}))
    if database_url.startswith("sqlite"):
        connect_args.setdefault("check_same_thread", False)
    engine = create_engine(database_url, connect_args=connect_args, **kwargs)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_safety(dbapi_connection: object, _record: object) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine
