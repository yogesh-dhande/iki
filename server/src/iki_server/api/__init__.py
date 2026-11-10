"""The REST API at ``/api/v1`` (ADR 0027).

Built with django-ninja. Conventions every endpoint follows:

- **Errors** are RFC 9457 problem details (``application/problem+json``),
  with a stable ``code`` and, for invalid input, the offending fields.
- **Lists** are paginated with an opaque ``cursor`` and a ``limit``.
- **Rate limits** apply per caller and are reported in ``RateLimit`` and
  ``RateLimit-Policy`` headers.
- **Authorization** goes through :mod:`iki_server.org.permissions`, as in the
  web pages. Things a caller can't see answer 404; things they can see but
  not change answer 403.
- **Audit:** changes are recorded with ``via=api``.
"""
