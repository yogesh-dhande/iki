"""Iki's test plugins stand in for its own (``acme/protein`` for ``iki/protein``) and take
the same HRID prefixes, so here they aren't warned for it (``reserved-hrid-prefix``,
ADR 0033). A test of that warning is marked ``reserved_hrid_prefixes`` and gets Iki's list
as it ships."""

from __future__ import annotations

import pytest

from iki_spec import hrid_prefixes


@pytest.fixture(autouse=True)
def _test_plugins_share_ikis_prefixes(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.node.get_closest_marker("reserved_hrid_prefixes") is None:
        shipped = hrid_prefixes.reserved()
        only_core = hrid_prefixes.Reserved(core=shipped.core, plugins={}, planned={})
        monkeypatch.setattr(hrid_prefixes, "reserved", lambda: only_core)
