"""A service's choice of how its requests' deliverables are reviewed (ADR 0305), as its
Admin makes it before it takes requests: a request is pinned to it as it arrives."""

from iki_server.accounts.models import User
from iki_server.org.models import Project
from iki_server.reviews import choices

CORE = "iki/core.result_review@1"
REQUEST = "iki/requests.Request"


def serve(admin: User, service: Project, flow: str = "accepted", type_name: str = REQUEST) -> None:
    """``service`` reviews the deliverables of requests sent to it from now on under core's
    result review's ``flow``, its parts played by the shared lab roles."""
    choices.choose(admin, project=service, type_name=type_name, procedure=CORE, flow=flow)
