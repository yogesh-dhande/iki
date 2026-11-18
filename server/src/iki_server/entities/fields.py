"""Model fields that generated plugin models use. Plugin migrations import them, so they are API."""

from collections.abc import Mapping, Sequence
from typing import Any

from django.db import models


class QuantityField(models.FloatField):  # type: ignore[type-arg]
    """A quantity, stored as a double precision number in its canonical UCUM unit.

    The unit is part of the field, so migrations record it, and a change of
    unit is a migration that converts the stored values.
    """

    description = "Quantity"

    def __init__(self, *args: Any, unit: str, **kwargs: Any) -> None:  # noqa: ANN401 - Django's field signature
        self.unit = unit
        super().__init__(*args, **kwargs)

    def deconstruct(self) -> tuple[str, str, Sequence[Any], dict[str, Any]]:
        name, path, args, kwargs = super().deconstruct()
        kwargs["unit"] = self.unit
        return name, path, args, kwargs


class StateField(models.CharField):  # type: ignore[type-arg]
    """A record's state (ADR 0118), in its declaring type's table as ``iki_state``.

    The states are part of the field, so migrations record them: a state that
    disappears is a migration, which moves its records to the state the plugin
    says (``moved``), or is refused.
    """

    description = "State"

    def __init__(
        self,
        *args: Any,  # noqa: ANN401 - Django's field signature
        states: Sequence[str],
        moved: Mapping[str, str] | None = None,
        locking: Sequence[str] = (),
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self.states = tuple(states)
        self.moved = dict(moved or {})
        self.locking = tuple(locking)
        kwargs.setdefault("max_length", 50)
        super().__init__(*args, **kwargs)

    def deconstruct(self) -> tuple[str, str, Sequence[Any], dict[str, Any]]:
        name, path, args, kwargs = super().deconstruct()
        kwargs["states"] = list(self.states)
        if self.moved:
            kwargs["moved"] = dict(sorted(self.moved.items()))
        if self.locking:
            kwargs["locking"] = list(self.locking)
        if kwargs.get("max_length") == 50:
            del kwargs["max_length"]
        return name, path, args, kwargs
