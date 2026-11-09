from django.http import HttpRequest

from iki_server.setup.models import Installation


def installation(request: HttpRequest) -> dict[str, object]:
    """``organization_name`` for page titles and headers. Read-only: no row is created."""
    name = Installation.objects.filter(pk=1).values_list("organization_name", flat=True).first()
    return {"organization_name": name or ""}
