"""Offline tests for the MCP tools in python_docs_mcp.server.

All fetchers, the search index and endpoint probes are monkeypatched — nothing
here touches the network. The cache is exercised for real against a temporary
SQLite file via PYTHON_DOCS_MCP_CACHE_DIR=tmp_path.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from python_docs_mcp import server as server_mod
from python_docs_mcp.cache import DocCache


DOCS_BASE = "https://docs.python.org/3/library"

FAKE_INDEX = {
    "built_at": "2026-01-01T00:00:00+00:00",
    "count": 5,
    "modules": [
        {
            "module": "json",
            "description": "JSON encoder and decoder",
            "url": f"{DOCS_BASE}/json.html",
        },
        {
            "module": "xml.etree.elementtree",
            "description": "The ElementTree XML API",
            "url": f"{DOCS_BASE}/xml.etree.elementtree.html",
        },
        {
            "module": "http.client",
            "description": "HTTP/1.1 client",
            "url": f"{DOCS_BASE}/http.client.html",
        },
        {
            "module": "a.timer",
            "description": "First timer module",
            "url": f"{DOCS_BASE}/a.timer.html",
        },
        {
            "module": "b.timer",
            "description": "Second timer module",
            "url": f"{DOCS_BASE}/b.timer.html",
        },
    ],
}

JSON_MD = (
    "# json — JSON encoder and decoder\n\n"
    "The ``json`` module provides a familiar API with the main functions:\n\n"
    "## Functions\n\n"
    "- `dumps(obj)` — Serialize *obj* as a JSON string.\n"
    "- `loads(s)` — Deserialize *s* as JSON.\n\n"
    "## Examples\n\n"
    "```python\njson.dumps({'a': 1})\n```\n"
)

TIMER_MD = (
    "# timer Module\n\n"
    "A simple timer.\n\n"
    "## Methods\n\n"
    "- `start()` — Starts it.\n\n"
    "## Fields\n\n"
    "- `count` — An int.\n"
)


@pytest.fixture
def fake_cache_dir(tmp_path, monkeypatch):
    """Point DocCache at a fresh temp dir so tests never share state."""
    monkeypatch.setenv("PYTHON_DOCS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path


def _patch_index(monkeypatch, index: dict) -> None:
    def fake_get_index(cache=None) -> dict:
        return dict(index)

    monkeypatch.setattr(server_mod, "get_index", fake_get_index)


def _patch_fetchers(monkeypatch, module=None, pypi=None):
    """Install recording fakes for the two fetchers; returns call log."""
    calls = {"module": [], "pypi": []}

    def fake_module(name):
        calls["module"].append({"name": name})
        if module is not None:
            return module(name)
        return {"ok": False, "error": f"module '{name}' not found in the Python 3 documentation"}

    def fake_pypi(name, version=None):
        calls["pypi"].append({"name": name, "version": version})
        if pypi is not None:
            return pypi(name, version)
        return {
            "ok": False,
            "error": "package not found on PyPI",
            "suggestion": "check the name (case-sensitive) or drop the version pin",
        }

    monkeypatch.setattr(server_mod, "fetch_python_module", fake_module)
    monkeypatch.setattr(server_mod, "fetch_pypi_package", fake_pypi)
    return calls


def _ok_module(name: str, title: str, markdown: str) -> dict:
    return {
        "ok": True,
        "url": f"{DOCS_BASE}/{name}.html",
        "title": title,
        "module": name,
        "markdown": markdown,
    }


# ---------------------------------------------------------------------------
# python_docs — identifier resolution
# ---------------------------------------------------------------------------

def test_plain_lowercase_module_fetched_directly(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(
        monkeypatch,
        module=lambda n: _ok_module(n, "json — JSON encoder and decoder", JSON_MD)
        if n == "json"
        else {"ok": False, "error": f"module '{n}' not found"},
    )
    result = server_mod.python_docs("json")
    assert calls["module"] == [{"name": "json"}]
    assert result["ok"] is True
    assert result["identifier"] == "json"
    assert result["url"].endswith("/json.html")
    assert "json" in result["title"]
    assert "dumps" in result["markdown"]
    assert result["cached"] is False and result["truncated"] is False


def test_dotted_mixed_case_fetched_directly(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(
        monkeypatch,
        module=lambda n: (
            _ok_module("xml.etree.elementtree", "xml.etree.ElementTree — XML toolkit", "# ET\n\ntext\n")
            if n == "xml.etree.ElementTree"
            else {"ok": False, "error": f"module '{n}' not found"}
        ),
    )
    result = server_mod.python_docs("xml.etree.ElementTree")
    # the fetcher itself performs the lowercase-on-404 fallback
    assert calls["module"] == [{"name": "xml.etree.ElementTree"}]
    assert result["ok"] is True
    assert "elementtree" in result["url"]


def test_plain_name_resolved_via_index(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)
    calls = _patch_fetchers(
        monkeypatch,
        module=lambda n: (
            _ok_module(n, "xml.etree.ElementTree — XML toolkit", "# ET\n\ntext\n")
            if n == "xml.etree.elementtree"
            else {"ok": False, "error": f"module '{n}' not found"}
        ),
    )
    result = server_mod.python_docs("ElementTree")
    assert calls["module"] == [{"name": "xml.etree.elementtree"}]
    assert result["ok"] is True
    assert result["identifier"] == "ElementTree"
    assert "elementtree" in result["url"]


def test_plain_name_ambiguous_returns_error(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)  # a.timer and b.timer both match "Timer"
    calls = _patch_fetchers(monkeypatch)
    result = server_mod.python_docs("Timer")
    assert result["ok"] is False
    assert "ambiguous" in result["error"]
    assert "a.timer" in result["error"] and "b.timer" in result["error"]
    assert "python_search" in result["suggestion"]
    assert calls["module"] == []  # ambiguous → no fetch attempt


def test_plain_name_no_match_returns_error(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)  # no SuchModule entry
    calls = _patch_fetchers(monkeypatch)
    result = server_mod.python_docs("TotallyBogusXYZ")
    assert result["ok"] is False
    assert "no stdlib module matching 'TotallyBogusXYZ'" in result["error"]
    assert "python_search" in result["suggestion"]
    assert calls["module"] == []  # no index match → no fetch attempt


def test_plain_name_with_broken_index_mentions_unavailable(fake_cache_dir, monkeypatch):
    _patch_index(
        monkeypatch,
        {"modules": [], "count": 0, "built_at": None, "error": "index build failed: boom"},
    )
    calls = _patch_fetchers(monkeypatch)
    result = server_mod.python_docs("ElementTree")
    assert result["ok"] is False
    assert "unavailable" in result["error"]
    assert "python_search" in result["suggestion"]
    assert calls["module"] == []


def test_empty_identifier_returns_error(fake_cache_dir, monkeypatch):
    _patch_fetchers(monkeypatch)
    result = server_mod.python_docs("   ")
    assert result["ok"] is False
    assert "error" in result and "suggestion" in result


def test_module_fetch_failure_returns_error_dict_and_is_not_cached(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(monkeypatch)  # every module fetch fails
    first = server_mod.python_docs("json")
    assert first["ok"] is False
    assert "error" in first and "suggestion" in first
    second = server_mod.python_docs("json")
    assert second["ok"] is False  # failures are never cached → fetcher called again
    assert len(calls["module"]) == 2


# ---------------------------------------------------------------------------
# python_docs — topic filter + truncation
# ---------------------------------------------------------------------------

def test_topic_filter_keeps_only_matching_section(fake_cache_dir, monkeypatch):
    _patch_fetchers(
        monkeypatch,
        module=lambda n: _ok_module(n, "json — JSON encoder and decoder", JSON_MD)
        if n == "json"
        else {"ok": False, "error": "404"},
    )
    result = server_mod.python_docs("json", topic="functions")
    assert result["markdown"].startswith("# json")
    assert "The ``json`` module provides" in result["markdown"]  # description kept
    assert "## Functions" in result["markdown"]
    assert "dumps(obj)" in result["markdown"]
    assert "## Examples" not in result["markdown"]
    assert "note" not in result


def test_topic_without_match_returns_full_content_with_note(fake_cache_dir, monkeypatch):
    _patch_fetchers(
        monkeypatch,
        module=lambda n: _ok_module(n, "json — JSON encoder and decoder", JSON_MD)
        if n == "json"
        else {"ok": False, "error": "404"},
    )
    result = server_mod.python_docs("json", topic="classes")
    assert "## Functions" in result["markdown"] and "## Examples" in result["markdown"]
    assert "no section matching 'classes'" in result["note"]
    assert "Functions" in result["note"] and "Examples" in result["note"]


def test_truncation_cuts_at_line_boundary_with_note(fake_cache_dir, monkeypatch):
    long_md = "# Big Module\n\n" + "\n".join(f"line {i} " + "x" * 40 for i in range(600))
    _patch_fetchers(
        monkeypatch,
        module=lambda n: _ok_module(n, "Big Module", long_md) if n == "json" else {"ok": False, "error": "404"},
    )
    result = server_mod.python_docs("json", max_tokens=100)
    assert result["truncated"] is True
    last_line = result["markdown"].splitlines()[-1]
    assert last_line.startswith("[truncated: showing ~")
    assert last_line.endswith("estimated tokens]")
    match = re.search(r"showing ~(\d+) of ~(\d+) estimated tokens\]", last_line)
    assert match is not None
    shown, total = int(match.group(1)), int(match.group(2))
    assert shown <= 100 and total > 100
    # cut at a line boundary: no partial "line N xxx..." tail before the note
    body_lines = result["markdown"].splitlines()[:-1]
    assert all(l.startswith("line ") or l.startswith("#") for l in body_lines if l)


def test_no_truncation_when_within_budget(fake_cache_dir, monkeypatch):
    _patch_fetchers(
        monkeypatch,
        module=lambda n: _ok_module(n, "json — JSON encoder and decoder", JSON_MD)
        if n == "json"
        else {"ok": False, "error": "404"},
    )
    result = server_mod.python_docs("json", max_tokens=8000)
    assert result["truncated"] is False
    assert "[truncated:" not in result["markdown"]


# ---------------------------------------------------------------------------
# python_docs — caching (real DocCache against a temp dir)
# ---------------------------------------------------------------------------

def test_module_doc_cached_on_second_call(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)
    calls = _patch_fetchers(
        monkeypatch,
        module=lambda n: (
            _ok_module(n, "json — JSON encoder and decoder", JSON_MD)
            if n == "json"
            else {"ok": False, "error": "404"}
        ),
    )
    first = server_mod.python_docs("json")
    second = server_mod.python_docs("json")
    assert first["cached"] is False and second["cached"] is True
    assert len(calls["module"]) == 1
    assert second["markdown"] == first["markdown"]
    assert second["url"] == first["url"]


def test_index_resolved_doc_cached_under_resolved_module(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)
    calls = _patch_fetchers(
        monkeypatch,
        module=lambda n: (
            _ok_module(n, "xml.etree.ElementTree — XML toolkit", "# ET\n\ntext\n")
            if n == "xml.etree.elementtree"
            else {"ok": False, "error": f"module '{n}' not found"}
        ),
    )
    by_name = server_mod.python_docs("ElementTree")
    assert by_name["cached"] is False
    # a direct call with the resolved lowercase path hits the same cache entry
    by_path = server_mod.python_docs("xml.etree.elementtree")
    assert by_path["cached"] is True
    assert len(calls["module"]) == 1


# ---------------------------------------------------------------------------
# python_search
# ---------------------------------------------------------------------------

def test_python_search_shape(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)
    monkeypatch.setattr(
        server_mod,
        "search_modules",
        lambda q, limit=8: [
            {
                "module": "http.client",
                "description": "HTTP/1.1 client",
                "score": 14.0,
            }
        ][: max(0, limit)],
    )
    result = server_mod.python_search("http client", limit=3)
    assert result["ok"] is True
    assert result["query"] == "http client"
    assert result["index_count"] == len(FAKE_INDEX["modules"])
    assert result["stale"] is False
    assert result["results"][0]["module"] == "http.client"


def test_python_search_stale_index_still_ok(fake_cache_dir, monkeypatch):
    stale = dict(FAKE_INDEX, stale=True)
    _patch_index(monkeypatch, stale)
    monkeypatch.setattr(server_mod, "search_modules", lambda q, limit=8: [])
    result = server_mod.python_search("http client")
    assert result["ok"] is True
    assert result["stale"] is True
    assert result["results"] == []


def test_python_search_reports_index_error(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, {"modules": [], "count": 0, "built_at": None, "error": "boom"})
    monkeypatch.setattr(server_mod, "search_modules", lambda q, limit=8: [])
    result = server_mod.python_search("http client")
    assert result["ok"] is False
    assert "search index unavailable" in result["error"]
    assert "retry" in result["suggestion"].lower()


# ---------------------------------------------------------------------------
# pypi_package
# ---------------------------------------------------------------------------

def _ok_pypi(name: str, version: str) -> dict:
    return {
        "ok": True,
        "name": name,
        "version": version,
        "summary": "A test package",
        "requires_python": ">=3.8",
        "license": "Apache-2.0",
        "homepage": f"https://example.com/{name}",
        "project_urls": {"Homepage": f"https://example.com/{name}"},
        "readme_markdown": f"# {name}\n\nReadme body.\n",
        "url": f"https://pypi.org/project/{name}/{version}/",
    }


def test_pypi_package_success_and_cache(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(
        monkeypatch,
        pypi=lambda n, v=None: _ok_pypi(n, "2.31.0" if v is None else v),
    )
    first = server_mod.pypi_package("requests")
    assert first["ok"] is True
    assert first["cached"] is False
    assert first["name"] == "requests"
    assert first["version"] == "2.31.0"
    assert first["readme_markdown"].startswith("# requests")

    second = server_mod.pypi_package("requests")
    assert second["ok"] is True
    assert second["cached"] is True
    assert second["version"] == "2.31.0"
    assert len(calls["pypi"]) == 1  # fetcher hit only once


def test_pypi_package_version_pin_cached_separately(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(
        monkeypatch,
        pypi=lambda n, v=None: _ok_pypi(n, v or "2.31.0"),
    )
    pinned = server_mod.pypi_package("requests", version="2.30.0")
    assert pinned["version"] == "2.30.0" and pinned["cached"] is False
    latest = server_mod.pypi_package("requests")
    assert latest["version"] == "2.31.0" and latest["cached"] is False
    assert [c["version"] for c in calls["pypi"]] == ["2.30.0", None]


def test_pypi_package_failure_shape_and_no_cache(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(monkeypatch)  # every pypi fetch fails
    first = server_mod.pypi_package("nope-xyz")
    assert first["ok"] is False
    assert first["error"] == "pypi lookup failed: package not found on PyPI"
    assert "suggestion" in first
    second = server_mod.pypi_package("nope-xyz")
    assert second["ok"] is False  # failures are never cached → fetcher called again
    assert len(calls["pypi"]) == 2


def test_pypi_package_requires_name(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(monkeypatch)
    result = server_mod.pypi_package("  ")
    assert result["ok"] is False
    assert "error" in result and "suggestion" in result
    assert calls["pypi"] == []


# ---------------------------------------------------------------------------
# python_status
# ---------------------------------------------------------------------------

def test_python_status_all_ok(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)
    monkeypatch.setattr(
        server_mod, "_probe_endpoint", lambda url: {"status": "ok", "http_status": 200}
    )
    result = server_mod.python_status()
    assert result["server"] == "python-docs-mcp"
    assert result["overall"] == "ok"
    assert set(result["checks"]) == {
        "search_index",
        "cache",
        "docs_python_org",
        "pypi_org",
    }
    si = result["checks"]["search_index"]
    assert si["status"] == "ok" and si["entries"] == len(FAKE_INDEX["modules"])
    assert si["stale"] is False and si["built_at"] == FAKE_INDEX["built_at"]
    assert result["checks"]["cache"]["status"] == "ok"
    for probe in ("docs_python_org", "pypi_org"):
        assert result["checks"][probe] == {"status": "ok", "http_status": 200}


def test_python_status_degraded_when_probe_fails(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)

    def probe(url):
        if "pypi.org" in url:
            return {"status": "error", "http_status": None, "error": "timeout"}
        return {"status": "ok", "http_status": 200}

    monkeypatch.setattr(server_mod, "_probe_endpoint", probe)
    result = server_mod.python_status()
    assert result["overall"] == "degraded"
    assert result["checks"]["pypi_org"]["status"] == "error"


def test_python_status_error_when_index_broken(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, {"modules": [], "count": 0, "built_at": None, "error": "boom"})
    monkeypatch.setattr(
        server_mod, "_probe_endpoint", lambda url: {"status": "ok", "http_status": 200}
    )
    result = server_mod.python_status()
    assert result["overall"] == "error"
    assert result["checks"]["search_index"]["status"] == "error"


def test_python_status_reports_read_only_cache_without_changing_overall(fake_cache_dir, monkeypatch):
    """P5: an unwritable cache is *announced* — and stays an "ok" check.

    The migration is forced to fail the way it does on a machine where the
    cache file cannot be written, so this is deterministic for root too.
    """
    _patch_index(monkeypatch, FAKE_INDEX)
    monkeypatch.setattr(
        server_mod, "_probe_endpoint", lambda url: {"status": "ok", "http_status": 200}
    )

    def refusing_migration(cls, conn):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(DocCache, "_ensure_schema", classmethod(refusing_migration))
    result = server_mod.python_status()
    cache_check = result["checks"]["cache"]
    assert cache_check["status"] == "ok", cache_check
    assert cache_check["read_only"] is True
    assert "readonly" in cache_check["read_only_reason"]
    assert result["overall"] == "ok"
