"""Database session store that records which user each session belongs to.

Configured with ``SESSION_ENGINE = "iki_server.accounts.sessions"``.
"""

from typing import Any

from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore as DBStore

from iki_server.accounts.models import UserSession


class SessionStore(DBStore):
    @classmethod
    def get_model_class(cls) -> type[UserSession]:
        return UserSession

    def create_model_instance(self, data: dict[str, Any]) -> UserSession:
        obj = super().create_model_instance(data)
        assert isinstance(obj, UserSession)  # noqa: S101 - get_model_class() guarantees it
        obj.user_id = data.get(SESSION_KEY)
        return obj

    async def acreate_model_instance(self, data: dict[str, Any]) -> UserSession:
        obj = await super().acreate_model_instance(data)
        assert isinstance(obj, UserSession)  # noqa: S101 - get_model_class() guarantees it
        obj.user_id = data.get(SESSION_KEY)
        return obj
