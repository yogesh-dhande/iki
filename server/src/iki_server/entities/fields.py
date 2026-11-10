"""Model fields that generated plugin models use. Plugin migrations import them, so they are API."""

from collections.abc import Sequence
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
