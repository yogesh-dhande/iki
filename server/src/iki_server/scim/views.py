"""The SCIM 2.0 API at ``/scim/v2/`` that identity providers call.

Authenticated with the bearer token an Org Admin creates on the sign-in
settings page. Requests without a valid token get 401 and are rate limited
per client address. Every change is audited through the SCIM channel.
"""

import json
from collections.abc import Callable
from functools import wraps
from typing import Any

import structlog
from django.db.models import QuerySet
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.audit.models import Via
from iki_server.scim import protocol, services, tokens
from iki_server.scim.models import ScimGroup
from iki_server.scim.services import ScimError
from iki_server.web.ratelimit import RateLimiter, client_key

logger = structlog.get_logger("iki.scim")

#: Requests with a missing or wrong token one client address can make per minute.
unauthorized_limiter = RateLimiter(limit_per_minute=30)

CONTENT_TYPE = "application/scim+json"
USER_FILTERS = {"username", "externalid", "id", "emails.value"}
GROUP_FILTERS = {"displayname", "externalid", "id", "members.value"}

View = Callable[..., HttpResponse]


def _json(body: Any, status: int = 200) -> JsonResponse:  # noqa: ANN401 - JSON
    return JsonResponse(body, status=status, content_type=CONTENT_TYPE, safe=False)


def _error(status: int, detail: str, scim_type: str | None = None) -> JsonResponse:
    return _json(protocol.error(status, detail, scim_type), status=status)


def _base(request: HttpRequest) -> str:
    return f"{request.scheme}://{request.get_host()}"


def scim_view(*methods: str) -> Callable[[View], View]:
    """Bearer-token auth, SCIM errors, the SCIM audit channel, and allowed methods."""

    def decorate(view: View) -> View:
        @wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:  # noqa: ANN401
            token = tokens.authenticate(request.headers.get("Authorization", ""))
            if token is None:
                if not unauthorized_limiter.allow(client_key(request)):
                    return _error(429, "Too many requests without a valid token.")
                logger.warning("scim_unauthorized", path=request.path)
                response = _error(401, "A valid SCIM bearer token is required.")
                response["WWW-Authenticate"] = 'Bearer realm="iki-scim"'
                return response
            if request.method not in methods:
                return _error(405, f"{request.method} isn't supported here.")
            context = audit.Context(
                via=Via.SCIM,
                client_ip=request.META.get("REMOTE_ADDR") or None,
                actor_label=str(token),
            )
            try:
                with audit.using(context):
                    return view(request, *args, **kwargs)
            except ScimError as error:
                return _error(error.status, error.detail, error.scim_type)
            except Http404:
                return _error(404, "No such resource.")

        return never_cache(csrf_exempt(wrapper))

    return decorate


def _document(request: HttpRequest) -> dict[str, Any]:
    try:
        document = json.loads(request.body or b"{}")
    except ValueError:
        raise ScimError(400, "The request body isn't JSON.", "invalidSyntax") from None
    if not isinstance(document, dict):
        raise ScimError(400, "The request body must be a JSON object.", "invalidSyntax")
    return document


def _page(request: HttpRequest) -> tuple[int, int]:
    try:
        start = max(1, int(request.GET.get("startIndex", 1)))
        count = min(protocol.MAX_PAGE, max(0, int(request.GET.get("count", 100))))
    except ValueError:
        raise ScimError(400, "startIndex and count must be integers.", "invalidValue") from None
    return start, count


def _filtered(
    queryset: QuerySet[Any], request: HttpRequest, allowed: set[str], fields: dict[str, str]
) -> QuerySet[Any]:
    text = request.GET.get("filter")
    if not text:
        return queryset
    for clause in protocol.parse_filter(text, allowed):
        if fields[clause.attribute] == "pk":
            value = protocol.uuid(clause.value)
            if value is None:
                return queryset.none()
            queryset = queryset.filter(pk=value)
        else:
            queryset = queryset.filter(**{fields[clause.attribute]: clause.value})
    return queryset


# --- Discovery ---------------------------------------------------------------------


@scim_view("GET")
def service_provider_config(request: HttpRequest) -> HttpResponse:
    return _json(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": protocol.MAX_PAGE},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "Bearer token",
                    "description": "The SCIM token from Iki's sign-in settings page.",
                    "primary": True,
                }
            ],
        }
    )


_RESOURCE_TYPES = [
    {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "User",
        "name": "User",
        "endpoint": "/Users",
        "schema": protocol.USER_SCHEMA,
    },
    {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "Group",
        "name": "Group",
        "endpoint": "/Groups",
        "schema": protocol.GROUP_SCHEMA,
    },
]


@scim_view("GET")
def resource_types(request: HttpRequest) -> HttpResponse:
    return _json(protocol.list_response(_RESOURCE_TYPES, len(_RESOURCE_TYPES), 1))


def _attribute(name: str, kind: str = "string", **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "name": name,
        "type": kind,
        "multiValued": False,
        "required": False,
        "mutability": "readWrite",
        "returned": "default",
        **extra,
    }


_SCHEMAS = [
    {
        "id": protocol.USER_SCHEMA,
        "name": "User",
        "attributes": [
            _attribute("userName", required=True, uniqueness="server"),
            _attribute("externalId"),
            _attribute("displayName"),
            _attribute("name", "complex"),
            _attribute("emails", "complex", multiValued=True),
            _attribute("active", "boolean"),
            _attribute("groups", "complex", multiValued=True, mutability="readOnly"),
        ],
    },
    {
        "id": protocol.GROUP_SCHEMA,
        "name": "Group",
        "attributes": [
            _attribute("displayName", required=True, uniqueness="server"),
            _attribute("externalId"),
            _attribute("members", "complex", multiValued=True),
        ],
    },
]


@scim_view("GET")
def schemas(request: HttpRequest) -> HttpResponse:
    return _json(protocol.list_response(_SCHEMAS, len(_SCHEMAS), 1))


# --- Users -------------------------------------------------------------------------


def _provisioned() -> QuerySet[User]:
    return User.objects.filter(provisioned_at__isnull=False).order_by("date_joined", "pk")


def _user_or_404(resource_id: str) -> User:
    return get_object_or_404(_provisioned(), pk=resource_id)


def _user_body(request: HttpRequest, user: User) -> dict[str, Any]:
    groups = list(ScimGroup.objects.filter(memberships__user=user).order_by("display_name"))
    return protocol.user_resource(user, _base(request), groups=groups)


@scim_view("GET", "POST")
def users(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        user = services.provision(protocol.profile(_document(request)))
        response = _json(_user_body(request, user), status=201)
        response["Location"] = _user_body(request, user)["meta"]["location"]
        return response
    queryset = _filtered(
        _provisioned(),
        request,
        USER_FILTERS,
        {
            "username": "scim_user_name",
            "externalid": "scim_external_id",
            "id": "pk",
            "emails.value": "email",
        },
    )
    start, count = _page(request)
    total = queryset.count()
    page = list(queryset[start - 1 : start - 1 + count])
    return _json(protocol.list_response([_user_body(request, u) for u in page], total, start))


@scim_view("GET", "PUT", "PATCH", "DELETE")
def user(request: HttpRequest, resource_id: str) -> HttpResponse:
    person = _user_or_404(resource_id)
    if request.method == "DELETE":
        services.deprovision(person)
        return HttpResponse(status=204)
    if request.method == "PUT":
        person = services.update(person, protocol.profile(_document(request)))
    elif request.method == "PATCH":
        person = services.update(person, protocol.user_patch(_document(request)))
    return _json(_user_body(request, person))


# --- Groups ------------------------------------------------------------------------


def _groups() -> QuerySet[ScimGroup]:
    return ScimGroup.objects.order_by("created_at", "pk")


def _group_body(request: HttpRequest, group: ScimGroup) -> dict[str, Any]:
    excluded = {a.strip().lower() for a in request.GET.get("excludedAttributes", "").split(",")}
    return protocol.group_resource(group, _base(request), members="members" not in excluded)


@scim_view("GET", "POST")
def groups(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        document = _document(request)
        group = services.create_group(
            protocol.string(document.get("displayName"), "displayName"),
            protocol.string(document.get("externalId"), "externalId"),
            protocol.member_ids(document.get("members")),
        )
        body = protocol.group_resource(group, _base(request))
        response = _json(body, status=201)
        response["Location"] = body["meta"]["location"]
        return response
    queryset = _filtered(
        _groups(),
        request,
        GROUP_FILTERS,
        {
            "displayname": "display_name",
            "externalid": "external_id",
            "id": "pk",
            "members.value": "memberships__user_id",
        },
    )
    start, count = _page(request)
    total = queryset.count()
    page = list(queryset[start - 1 : start - 1 + count])
    return _json(protocol.list_response([_group_body(request, g) for g in page], total, start))


@scim_view("GET", "PUT", "PATCH", "DELETE")
def group(request: HttpRequest, resource_id: str) -> HttpResponse:
    found = get_object_or_404(_groups(), pk=resource_id)
    if request.method == "DELETE":
        services.delete_group(found)
        return HttpResponse(status=204)
    if request.method == "PUT":
        document = _document(request)
        found = services.replace_group(
            found,
            protocol.string(document.get("displayName"), "displayName"),
            protocol.string(document["externalId"], "externalId")
            if "externalId" in document
            else None,
            protocol.member_ids(document.get("members")),
        )
    elif request.method == "PATCH":
        found = services.patch_group(found, protocol.group_patch(_document(request)))
        # Entra and Okta don't read the body of a PATCH; 204 spares sending big groups.
        return HttpResponse(status=204)
    return _json(_group_body(request, found))


@csrf_exempt
def not_found(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:  # noqa: ANN401
    return _error(404, "No such resource.")
