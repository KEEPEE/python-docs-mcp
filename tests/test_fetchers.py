"""Offline tests for the fetcher parsers, using real downloaded fixtures.

Fixtures (downloaded once, committed):
- ``tests/fixtures/py_json_module.html``    — https://docs.python.org/3/library/json.html
- ``tests/fixtures/pypi_django.json``       — https://pypi.org/pypi/django/json
  (kept for the opt-in JSON parser only — that endpoint is disallowed by
  PyPI's robots.txt, so nothing in the default path fetches it)
- ``tests/fixtures/pypi_attrs_project.html`` — https://pypi.org/project/attrs/
- ``tests/fixtures/pypi_six_pinned.html``   — https://pypi.org/project/six/1.17.0/
- ``tests/fixtures/pypi_bot_challenge.html` — the JS "Client Challenge" page
  PyPI's edge returns for some cache-miss URLs (HTTP 200, ~3 KB)

No test in this module touches the network.
"""

import json
from pathlib import Path

import httpx
import pytest

from python_docs_mcp.fetchers import (
    PYPI_JSON_OPT_IN_ENV_VAR,
    _module_url_candidates,
    parse_pypi_json,
    parse_pypi_project_html,
    parse_python_module_html,
    pypi_challenge_detector,
    pypi_json_opt_in_enabled,
    pypi_json_url,
    pypi_project_url,
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

def test_module_url_candidates_lowercase_first():
    """docs.python.org only has LOWERCASE filenames → lowercase is tried first.

    The old order (name as given, then lowercase) made a guaranteed 404 request
    per mixed-case lookup (A2 §4.1 measured exactly that).  The name as given is
    kept only as a fallback, so a mixed-case lookup still tries both URLs.
    """
    urls = _module_url_candidates("xml.etree.ElementTree")
    assert urls == [
        "https://docs.python.org/3/library/xml.etree.elementtree.html",
        "https://docs.python.org/3/library/xml.etree.ElementTree.html",
    ]


def test_module_url_candidates_simple_name_no_duplicate():
    assert _module_url_candidates("json") == ["https://docs.python.org/3/library/json.html"]


def test_module_url_candidates_all_lowercase_names_have_one_candidate():
    """No regression for already-lowercase names: exactly one candidate, no dupes."""
    for name in ("json", "xml.etree.elementtree", "os.path", "zlib"):
        urls = _module_url_candidates(name)
        assert len(urls) == 1, name
        assert urls[0] == f"https://docs.python.org/3/library/{name}.html"


# ---------------------------------------------------------------------------
# PyPI project-page (HTML) parsing — the default, robots-allowed source
# ---------------------------------------------------------------------------

def test_parse_pypi_project_html_attrs_fixture():
    result = parse_pypi_project_html(
        _fixture_text("pypi_attrs_project.html"),
        "https://pypi.org/project/attrs/",
    )
    assert result["ok"] is True
    assert result["name"] == "attrs"
    assert result["version"] == "26.1.0"
    assert result["summary"] == "Classes Without Boilerplate"
    assert result["requires_python"] == ">=3.9"
    assert result["license"] == "MIT"  # "License expression" sidebar block
    assert result["homepage"] == "https://github.com/python-attrs/attrs"
    assert result["project_urls"]["Documentation"] == "https://www.attrs.org/"
    assert len(result["readme_markdown"]) > 1000


def test_parse_pypi_project_html_pinned_fixture_uses_the_pin():
    """A pinned page states the version twice (h1 and the pip command)."""
    result = parse_pypi_project_html(
        _fixture_text("pypi_six_pinned.html"),
        "https://pypi.org/project/six/1.17.0/",
    )
    assert result["ok"] is True
    assert result["name"] == "six"
    assert result["version"] == "1.17.0"
    assert result["requires_python"] == "!=3.0.*, !=3.1.*, !=3.2.*, >=2.7"
    assert result["license"] == "MIT License (MIT)"  # legacy "License" block
    assert result["homepage"] == "https://github.com/benjaminp/six"


def test_parse_pypi_project_html_same_keys_as_json_parser():
    """Both sources must yield the SAME payload keys — callers must not notice.

    ``server.pypi_package`` and ``scripts/e2e_mcp_test.py`` read these keys, so
    switching the source may not change the shape.
    """
    html_result = parse_pypi_project_html(
        _fixture_text("pypi_attrs_project.html"), "https://pypi.org/project/attrs/"
    )
    json_result = parse_pypi_json(json.loads(_fixture_text("pypi_django.json")))
    assert html_result["ok"] and json_result["ok"]
    assert set(html_result) == set(json_result)
    assert set(html_result) == {
        "ok",
        "name",
        "version",
        "summary",
        "requires_python",
        "license",
        "homepage",
        "project_urls",
        "readme_markdown",
    }


def test_parse_pypi_project_html_detects_bot_challenge_page():
    """PyPI's JS challenge is HTTP 200 — it must never be parsed as a package."""
    result = parse_pypi_project_html(
        _fixture_text("pypi_bot_challenge.html"),
        "https://pypi.org/project/whatever/",
    )
    assert result["ok"] is False
    assert result.get("bot_challenge") is True
    assert "bot-verification" in result["error"]


def test_parse_pypi_project_html_rejects_non_project_pages():
    for html in (
        "<html><body><p>nothing</p></body></html>",
        "<html><head><title>PyPI</title></head><body><h1>404</h1></body></html>",
    ):
        result = parse_pypi_project_html(html, "https://pypi.org/project/nope/")
        assert result["ok"] is False
        assert result.get("error")


def test_parse_pypi_project_html_garbage_does_not_raise():
    for bad in ("", None, 42, "<html"):  # type: ignore[arg-type]
        result = parse_pypi_project_html(bad, "https://pypi.org/project/x/")  # type: ignore[arg-type]
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# PyPI source selection (default HTML vs opt-in JSON)
# ---------------------------------------------------------------------------

def test_pypi_url_helpers():
    assert pypi_project_url("attrs") == "https://pypi.org/project/attrs/"
    assert pypi_project_url("attrs", "24.2.0") == "https://pypi.org/project/attrs/24.2.0/"
    assert pypi_json_url("attrs") == "https://pypi.org/pypi/attrs/json"
    assert pypi_json_url("attrs", "24.2.0") == "https://pypi.org/pypi/attrs/24.2.0/json"


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("YES", True), ("on", True), ("", False),
     ("0", False), ("false", False), ("maybe", False), (None, False)],
)
def test_pypi_json_opt_in_env_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(PYPI_JSON_OPT_IN_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(PYPI_JSON_OPT_IN_ENV_VAR, value)
    assert pypi_json_opt_in_enabled() is expected


# ---------------------------------------------------------------------------
# A8 F4: the PyPI anti-bot detector handed to the politeness layer
# ---------------------------------------------------------------------------

def _hdr(content_type: str | None = "text/html; charset=utf-8") -> "httpx.Headers":
    return httpx.Headers({"content-type": content_type}) if content_type else httpx.Headers()


def test_pypi_challenge_detector_flags_the_real_challenge_fixture():
    """The committed page is what PyPI's edge actually sent: 3 038 B, HTTP 200."""
    body = (FIXTURES / "pypi_bot_challenge.html").read_bytes()
    assert len(body) < 8 * 1024
    assert pypi_challenge_detector(body, _hdr()) is True


def test_pypi_challenge_detector_passes_every_real_page_fixture():
    """Precision: no real artifact in this repo may be called a challenge."""
    for name in (
        "pypi_attrs_project.html",
        "pypi_six_pinned.html",
        "py_json_module.html",
        "py_library_index.html",
        "pypi_django.json",
    ):
        body = (FIXTURES / name).read_bytes()
        assert pypi_challenge_detector(body, _hdr()) is False, name


def test_pypi_challenge_detector_conditions_are_independent():
    challenge = (FIXTURES / "pypi_bot_challenge.html").read_bytes()

    # 1. size gate — a big body is never a challenge, markers or not
    assert pypi_challenge_detector(challenge + b"<!--fill-->" * 2000, _hdr()) is False
    # 2. content-type veto — a JSON artifact is not an HTML challenge
    assert pypi_challenge_detector(challenge, _hdr("application/json")) is False
    # a missing content-type must not make the verdict *less* safe
    assert pypi_challenge_detector(challenge, httpx.Headers()) is True
    # 3. a real page that merely mentions the marker is vetoed by its own header
    page = (
        b"<!DOCTYPE html><html><body>"
        b'<h1 class="project-header__name">attrs 24.2.0</h1>'
        b'<a href="/_fs-ch-x/assets/a.css">x</a></body></html>'
    )
    assert pypi_challenge_detector(page, _hdr()) is False
    # no marker at all -> not a challenge (a plain error page, for instance)
    assert pypi_challenge_detector(b"<!DOCTYPE html><html><body>nope</body></html>", _hdr()) is False
