"""Django's PostgreSQL backend, giving pooled connections back however a thread ends.

Django returns a thread's connection to psycopg's pool when a request finishes. A thread
that ends holding one (asgiref ends each request's thread once it's done) would take
its place in the pool with it for good: psycopg counts the connection as in use, so
after enough of them every request waits out the pool's timeout while the database has
room. Here the connection goes back when its wrapper is collected, and a warning says
which request and thread opened it, so the code that kept it can be put right.
"""

from __future__ import annotations

import threading
from typing import Any

import structlog
from django.db.backends.postgresql import base

logger = structlog.get_logger("iki.db")


class DatabaseWrapper(base.DatabaseWrapper):
    def get_new_connection(self, conn_params: dict[str, Any]) -> Any:  # noqa: ANN401 - psycopg's
        connection = super().get_new_connection(conn_params)
        context = structlog.contextvars.get_contextvars()
        self._opened_by = {
            "thread": threading.current_thread().name,
            # The request's log line has the same ID, and says what it was.
            "request_id": context.get("request_id"),
            "task_id": context.get("task_id"),
        }
        return connection

    def __del__(self) -> None:
        connection = self.__dict__.get("connection")
        # A pooled connection knows its pool while it's out of it, and only then.
        pool = getattr(connection, "_pool", None)
        if pool is None:
            return
        try:
            pool.putconn(connection)
        except Exception:
            # A finalizer mustn't raise; the pool discards a connection it can't take back.
            return
        logger.warning("db.connection_dropped", **self.__dict__.get("_opened_by", {}))
