"""No screen may scroll sideways.

A phone held one-handed at a counter can't drag a table around. This is a
standing rule rather than a one-off cleanup, so it is enforced here: the shapes
that cause horizontal scrolling are simply not allowed in a template.
"""

import pathlib

import pytest

TEMPLATES = sorted(pathlib.Path("src/lottery_tracker/web/templates").glob("*.html"))


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_no_data_tables(path):
    """Tables are what forced the sideways drag. Stack the rows instead."""
    assert "<table" not in path.read_text(), (
        f"{path.name} has a table — build it as stacked rows so it fits a phone"
    )


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_nothing_opts_into_horizontal_scrolling(path):
    text = path.read_text()
    assert "overflow-x:auto" not in text.replace(" ", "")
    assert "overflow-x: scroll" not in text


def test_the_page_itself_cannot_scroll_sideways():
    """The last line of defence, in the layout every page inherits."""
    base = (pathlib.Path("src/lottery_tracker/web/templates/base.html")
            .read_text().replace(" ", ""))
    assert "max-width:100%" in base and "overflow-x:hidden" in base
