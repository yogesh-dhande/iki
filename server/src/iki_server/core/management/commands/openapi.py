"""``iki-server openapi``: prints the /api/v1 OpenAPI document, or with ``--worker``,
/worker/v1's.

The TypeScript client (``web/packages/sdk``) and the worker's protocol types
(``worker/crates/iki-protocol``) are generated from them; CI checks that the
committed copies match the code.
"""

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandParser


class Command(BaseCommand):
    help = "Print the /api/v1 OpenAPI document as JSON."
    requires_system_checks = []  # noqa: RUF012 - Django reads it per class

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--worker", action="store_true", help="The /worker/v1 API's.")

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        if options["worker"]:
            from iki_server.api.worker import api
        else:
            from iki_server.api.v1 import api

        schema = api.get_openapi_schema()
        self.stdout.write(json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False))
