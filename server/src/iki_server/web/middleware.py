"""Security headers Django doesn't set itself.

Django's ``SecurityMiddleware`` covers HSTS, nosniff, Referrer-Policy and
Cross-Origin-Opener-Policy; ``ContentSecurityPolicyMiddleware`` covers CSP.
This adds the rest, and makes responses uncacheable unless a view (or the
static file server) says otherwise.
"""

from django.http import HttpRequest, HttpResponse
from django.utils.cache import patch_cache_control
from django.utils.deprecation import MiddlewareMixin

#: Powerful browser features Iki doesn't use. Each is denied to every origin.
PERMISSIONS_POLICY = ", ".join(
    f"{feature}=()"
    for feature in (
        "accelerometer",
        "autoplay",
        "bluetooth",
        "camera",
        "display-capture",
        "geolocation",
        "gyroscope",
        "hid",
        "identity-credentials-get",
        "magnetometer",
        "microphone",
        "midi",
        "payment",
        "publickey-credentials-get",
        "screen-wake-lock",
        "serial",
        "usb",
        "xr-spatial-tracking",
    )
)

HEADERS = {
    "Permissions-Policy": PERMISSIONS_POLICY,
    # Other sites may not embed or read Iki's responses...
    "Cross-Origin-Resource-Policy": "same-origin",
    # ...and Iki pages load nothing from other origins, which also gives
    # them cross-origin isolation.
    "Cross-Origin-Embedder-Policy": "require-corp",
    "Origin-Agent-Cluster": "?1",
    "X-Permitted-Cross-Domain-Policies": "none",
    # Where browsers send CSP violation reports (the CSP's report-to "csp").
    "Reporting-Endpoints": 'csp="/csp-report/"',
}


class SecurityHeadersMiddleware(MiddlewareMixin):
    def process_response(self, request: HttpRequest, response: HttpResponse) -> HttpResponse:
        for header, value in HEADERS.items():
            response.headers.setdefault(header, value)
        if "Cache-Control" not in response:
            # Pages can hold personal data: never store them in shared or browser caches.
            patch_cache_control(response, no_store=True)
        return response
