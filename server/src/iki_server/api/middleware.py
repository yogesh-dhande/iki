"""Makes every response under ``/api/`` an API response.

- Errors that never reached an endpoint (unknown paths, unsupported
  methods, crashes) become problem details too.
- Responses carry the caller's rate-limit headers.
- 401s say how to authenticate (RFC 9110 requires ``WWW-Authenticate``):
  with a bearer token, and where to find the authorization server (RFC 9728).
"""

from django.http import HttpRequest, HttpResponse
from django.utils.deprecation import MiddlewareMixin

from iki_server.api import problems
from iki_server.api.security import bearer_challenge, rate_limit_headers

PREFIX = "/api/"
#: Headers worth keeping when an error response is replaced.
_KEEP = ("Allow", "Retry-After", "WWW-Authenticate")


class ApiResponseMiddleware(MiddlewareMixin):
    def process_response(self, request: HttpRequest, response: HttpResponse) -> HttpResponse:
        if not request.path.startswith(PREFIX):
            return response
        if response.status_code >= 400 and not response.get("Content-Type", "").startswith(
            problems.CONTENT_TYPE
        ):
            replacement = problems.response(problems.Problem(response.status_code))
            for header in _KEEP:
                if header in response:
                    replacement[header] = response[header]
            # Keep cookies set further in (a refreshed session, the CSRF cookie).
            replacement.cookies = response.cookies
            response = replacement
        decision = getattr(request, "iki_rate_limit", None)
        if decision is not None:
            for header, value in rate_limit_headers(decision).items():
                response[header] = value
        if response.status_code == 401 and "WWW-Authenticate" not in response:
            response["WWW-Authenticate"] = bearer_challenge(request)
        return response
