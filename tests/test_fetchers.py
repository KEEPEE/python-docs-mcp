"""Offline tests for the fetcher parsers, using real downloaded fixtures.

Fixtures (downloaded once, committed):
- ``tests/fixtures/py_json_module.html`` — https://docs.python.org/3/library/json.html
- ``tests/fixtures/pypi_django.json``    — https://pypi.org/pypi/django/json

No test in this module touches the network.
"""

import json
from pathlib import Path

from python_docs_mcp.fetchers import (
    _module_url_candidates,
    parse_pypi_json,
    parse_python_module_html,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# CPython docs page parsing
# ---------------------------------------------------------------------------

def test_parse_python_module_html_json_fixture():
    result = parse_python_module_html(
        _fixture_text("py_json_module.html"),
        "https://docs.python.org/3/library/json.html",
    )
    assert result["ok"] is True
    assert "json" in result["title"].lower()
    markdown = result["markdown"]
    assert len(markdown) > 500
    # Core API signatures must survive the conversion.
    assert "dumps" in markdown
    assert "loads" in markdown


def test_parse_python_module_html_title_strips_docs_suffix():
    result = parse_python_module_html(
        _fixture_text("py_json_module.html"),
        "https://docs.python.org/3/library/json.html",
    )
    # <title> is "json — JSON encoder and decoder — Python 3.14.8 documentation";
    # the versioned suffix must not leak into the title.
    assert "documentation" not in result["title"]


def test_parse_python_module_html_page_not_found_body():
    # The real 404 body from docs.python.org: role="main" present, but it is a
    # plain "Page not found" page with no <section>.
    html = (
        '<html><head><title>Page not found — Python 3.14.8 documentation</title></head>'
        '<body><div class="body" role="main">'
        "<h1>Page not found</h1>"
        "<p>Unfortunately we couldn't find the content you were looking for.</p>"
        '<div class="clearer"></div></div></body></html>'
    )
    result = parse_python_module_html(html, "https://docs.python.org/3/library/nope.html")
    assert result["ok"] is False
    assert "error" in result


def test_parse_python_module_html_no_main_content():
    result = parse_python_module_html("<html><body><p>nothing</p></body></html>", "http://x/y.html")
    assert result["ok"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# PyPI JSON parsing
# ---------------------------------------------------------------------------

def test_parse_pypi_json_django_fixture():
    data = json.loads(_fixture_text("pypi_django.json"))
    result = parse_pypi_json(data)
    assert result["ok"] is True
    assert result["name"].lower() == "django"
    assert result["version"]
    assert result["summary"]
    assert result["readme_markdown"]
    assert result["requires_python"]


def test_parse_pypi_json_django_readme_is_converted():
    data = json.loads(_fixture_text("pypi_django.json"))
    readme = parse_pypi_json(data)["readme_markdown"]
    # The django README is RST with an overlined "Django" title; the simple
    # pass should turn it into an ATX heading and keep the prose readable.
    assert readme.lstrip().startswith("#")
    assert "high-level Python web framework" in readme


def test_parse_pypi_json_garbage_input():
    for bad in ({}, {"info": {}}):
        result = parse_pypi_json(bad)
        assert result["ok"] is False
        assert result.get("error")


def test_parse_pypi_json_non_dict_input():
    for bad in (None, "not a dict", ["list"], 42):
        result = parse_pypi_json(bad)
        assert result["ok"] is False
        assert result.get("error")


# ---------------------------------------------------------------------------
# Lowercase-fallback URL candidates
# ---------------------------------------------------------------------------

def test_module_url_candidates_lowercase_fallback():
    urls = _module_url_candidates("xml.etree.ElementTree")
    assert urls == [
        "https://docs.python.org/3/library/xml.etree.ElementTree.html",
        "https://docs.python.org/3/library/xml.etree.elementtree.html",
    ]


def test_module_url_candidates_simple_name_no_duplicate():
    assert _module_url_candidates("json") == ["https://docs.python.org/3/library/json.html"]
