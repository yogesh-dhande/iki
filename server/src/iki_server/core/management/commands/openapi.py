"""``iki-server openapi``: prints the /api/v1 OpenAPI document.

The TypeScript client (``web/packages/sdk``) is generated from it; CI checks
that the committed copy matches the code.
"""

import json
from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Print the /api/v1 OpenAPI document as JSON."
    requires_system_checks = []  # noqa: RUF012 - Django reads it per class

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        from iki_server.api.v1 import api

        schema = api.get_openapi_schema()
        self.stdout.write(json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False))
