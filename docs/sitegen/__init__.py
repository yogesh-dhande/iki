"""What Iki's docs sites are built from that isn't written for them, and the check of what
they built.

- ``examples``: runs the example plugins' own code on their public and synthetic data
  (pUC19's public nanopore reads, the plate assay's synthetic MARS export), so the
  worked examples on ikisci.org show the values the plugins compute, never ones copied
  by hand.
- ``calls``: which Python SDK method and MCP tool make each REST call, read from their
  code, for the API reference.

``python -m sitegen generate`` writes both as JSON for the site's build.
- ``check``: checks a built static site as a reader's browser meets it: every link and
  anchor goes somewhere, nothing loads from another origin, and every page carries a
  Content Security Policy before anything it governs. It takes the built directory, so
  any docs site in the repository (ikisci.org in ``docs/site``, or another) can use it.

``make docs`` and ``make check-docs`` run them.
"""
