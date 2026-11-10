"""Shared infrastructure tables."""

from typing import ClassVar

from django.db import models


class RateLimitCounter(models.Model):
    """Requests counted against one limit in one fixed window, shared by every web pod.

    The table is UNLOGGED: counters are worth losing in a crash rather than
    paying for write-ahead logging on every counted request.
    """

    key = models.CharField(max_length=200)
    window_start = models.DateTimeField()
    count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "iki_rate_limit"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["key", "window_start"], name="iki_rate_limit_window"),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["window_start"], name="iki_rate_limit_age"),
        ]

    def __str__(self) -> str:
        return f"{self.key} @ {self.window_start:%H:%M}: {self.count}"
