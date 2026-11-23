"""The app and server pages expose their shared stylesheets directly to accessibility tools."""

import re
from pathlib import Path

import pytest
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string


@pytest.mark.parametrize("template", ["_head_links.html", "web/app.html"])
def test_shared_stylesheets_are_external_and_can_be_read_under_the_page_csp(template: str) -> None:
    html = render_to_string(template)
    sheets = re.findall(r'<link rel="stylesheet"[^>]* href="/static/(iki/[^"]+\.css)"', html)
    assert {"iki/tokens.css", "iki/fonts.css", "iki/button.css", "iki/iki.css"} <= set(sheets)
    for name in sheets:
        source = finders.find(name)
        assert isinstance(source, str), name
        # axe reconstructs @import rules in inline styles, which Chromium's CSP rejects.
        assert not re.search(r"^\s*@import\b", Path(source).read_text(), flags=re.MULTILINE), name
