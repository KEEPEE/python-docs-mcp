"""Offline tests for python_docs_mcp.search (parser, ranking, cache logic).

The parser runs against the real downloaded fixture
``tests/fixtures/py_library_index.html``; the ranking and get_index tests use
a throwaway DocCache pre-populated from that fixture plus monkeypatched
build/fetch layers, so nothing here touches the network.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from python_docs_mcp import search as search_mod
from python_docs_mcp.cache import DocCache
from python_docs_mcp.search import (
    INDEX_KEY,
    LIBRARY_INDEX_URL,
    build_index,
    get_index,
    parse_library_index_html,
    search_modules,
)

FIXTURE = Path(__file__).parent / "fixtures" / "py_library_index.html"


def _fixture_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Parser tests on the real fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def parsed_entries() -> list[dict]:
    return parse_library_index_html(_fixture_html())


def test_parser_entry_count(parsed_entries):
    assert len(parsed_entries) > 150
    # sanity band from the task: a few hundred stdlib modules
    assert len(parsed_entries) < 800


def test_json_entry(parsed_entries):
    by_module = {e["module"]: e for e in parsed_entries}
    entry = by_module.get("json")
    assert entry is not None, "json missing from index"
    assert "JSON" in entry["description"]
    assert entry["url"] == "https://docs.python.org/3/library/json.html"


def test_dotted_module_is_lowercase(parsed_entries):
    by_module = {e["module"]: e for e in parsed_entries}
    entry = by_module.get("xml.etree.elementtree")
    assert entry is not None, "xml.etree.elementtree missing from index"
    assert entry["module"] == "xml.etree.elementtree"  # canonical lowercase
    assert entry["url"].endswith("xml.etree.elementtree.html")


def test_no_relative_or_path_modules(parsed_entries):
    for entry in parsed_entries:
        module = entry["module"]
        assert not module.startswith(".."), entry
        assert "/" not in module, entry
        assert module == module.lower(), entry
        assert entry["url"].startswith("https://docs.python.org/3/library/")


def test_entry_shape(parsed_entries):
    for entry in parsed_entries:
        assert set(entry) == {"module", "description", "url"}, entry
        assert isinstance(entry["description"], str)


def test_section_and_example_pages_excluded(parsed_entries):
    by_module = {e["module"]: e for e in parsed_entries}
    # Prose-first toctree entries (section/example pages) must not be modules.
    assert "sys_path_init" not in by_module
    assert "unittest.mock-examples" not in by_module
    # ...while the real modules they reference are present.
    assert "unittest.mock" in by_module


def test_submodule_fragment_href(parsed_entries):
    """Submodules documented on a parent page use #module-<name> fragments."""
    by_module = {e["module"]: e for e in parsed_entries}
    entry = by_module.get("curses.textpad")
    assert entry is not None, "curses.textpad missing from index"
    assert entry["url"] == (
        "https://docs.python.org/3/library/curses.html#module-curses.textpad"
    )


def test_parser_rejects_non_index_html():
    with pytest.raises(ValueError):
        parse_library_index_html("<html><body><p>nothing here</p></body></html>")


# ---------------------------------------------------------------------------
# Ranking tests on the real fixture (offline, via a pre-populated cache)
# ---------------------------------------------------------------------------

@pytest.fixture()
def fixture_cache(tmp_path, monkeypatch):
    """Point DocCache at a throwaway dir and pre-populate it from the fixture."""
    monkeypatch.setenv("PYTHON_DOCS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    cache = DocCache()
    modules = parse_library_index_html(_fixture_html())
    index = {
        "modules": modules,
        "count": len(modules),
        "built_at": "2026-01-01T00:00:00+00:00",
    }
    cache.set(INDEX_KEY, json.dumps(index), 604800)
    return cache


def test_search_json_exact_first(fixture_cache):
    results = search_modules("json")
    assert results, "expected hits for 'json'"
    assert results[0]["module"] == "json"
    assert all(set(r) == {"module", "description", "score"} for r in results)


def test_search_email_mime_multi_token(fixture_cache):
    """Both tokens must match: 'email' in the name, 'mime' in its description."""
    results = search_modules("email mime")
    assert any(r["module"] == "email" for r in results), results
    # the exact-name entry outranks description-only matches
    assert results[0]["module"] == "email"


def test_search_nonexistent_returns_empty(fixture_cache):
    assert search_modules("zzz_nonexistent") == []


def test_search_limit_respected(fixture_cache):
    default = search_modules("html")
    limited = search_modules("html", limit=3)
    assert len(limited) <= 3
    assert [r["module"] for r in limited] == [r["module"] for r in default[:3]]
    assert search_modules("html", limit=0) == []


def test_search_is_case_insensitive(fixture_cache):
    results = search_modules("JSON")
    assert results and results[0]["module"] == "json"


def test_search_scores_descending(fixture_cache):
    results = search_modules("email")
    scores = [r["score"] for r in results]
    assert all(a >= b for a, b in zip(scores, scores[1:]))


# ---------------------------------------------------------------------------
# get_index / build_index cache behaviour (monkeypatched, tmp cache dir)
# ---------------------------------------------------------------------------

def _canned() -> dict:
    return {
        "built_at": "2026-01-01T00:00:00+00:00",
        "count": 1,
        "modules": [
            {"module": "json", "description": "JSON encoder and decoder",
             "url": "https://docs.python.org/3/library/json.html"},
        ],
    }


def _expire_cache_row(db_path: Path) -> None:
    """Rewrite the kv row so its expiry lies in the past (simulate TTL lapse)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE kv SET expires_at = ? WHERE key = ?",
            (time.time() - 10, INDEX_KEY),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    """Point DocCache at a throwaway directory and stub out build_index."""
    monkeypatch.setenv("PYTHON_DOCS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    calls = {"count": 0}

    def fake_build():
        calls["count"] += 1
        return _canned()

    monkeypatch.setattr(search_mod, "build_index", fake_build)
    return calls


def test_get_index_fresh_write_then_cache_hit(isolated_cache):
    first = get_index()
    assert first == _canned()
    assert first.get("stale") is not True
    assert isolated_cache["count"] == 1

    # Second call must be served from the cache: no rebuild, same content.
    second = get_index()
    assert second == _canned()
    assert isolated_cache["count"] == 1


def test_get_index_stale_fallback_on_network_failure(tmp_path, monkeypatch):
    """When the network fails and the cached copy is expired, serve it stale."""
    monkeypatch.setenv("PYTHON_DOCS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    state = {"fail": False}

    def fake_fetch():
        if state["fail"]:
            raise RuntimeError("network down")
        return _fixture_html()  # real parse of the fixture, no network

    monkeypatch.setattr(search_mod, "_fetch_library_index_html", fake_fetch)

    # First build succeeds and persists a fresh index.
    fresh = get_index()
    assert fresh.get("stale") is not True
    assert fresh["count"] == len(fresh["modules"]) > 150

    # Expire the cached row, then kill the network: the stale copy must come back.
    _expire_cache_row(Path(tmp_path / "cache" / "cache.db"))
    state["fail"] = True
    stale = get_index()
    assert stale.get("stale") is True
    assert stale["count"] == fresh["count"]
    assert stale["modules"] == fresh["modules"]
    assert stale["built_at"] == fresh["built_at"]


def test_build_index_stale_fallback_on_network_failure(tmp_path, monkeypatch):
    """build_index itself falls back: fresh cache first, then stale via peek."""
    monkeypatch.setenv("PYTHON_DOCS_MCP_CACHE_DIR", str(tmp_path / "cache"))

    def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(search_mod, "_fetch_library_index_html", boom)

    # No cache at all -> build_index raises (nothing to fall back on).
    with pytest.raises(RuntimeError, match="network down"):
        build_index()

    # Seed an expired cached copy: the stale fallback must return it.
    cache = DocCache()
    cache.set(INDEX_KEY, json.dumps(_canned()), 604800)
    _expire_cache_row(Path(tmp_path / "cache" / "cache.db"))
    result = build_index()
    assert result.get("stale") is True
    assert result["modules"] == _canned()["modules"]


def test_get_index_build_fails_with_no_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHON_DOCS_MCP_CACHE_DIR", str(tmp_path / "cache"))

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(search_mod, "_fetch_library_index_html", boom)
    result = get_index()  # must not raise
    assert result["modules"] == []
    assert result["count"] == 0
    assert result["built_at"] is None
    assert "boom" in result["error"]


def test_get_index_accepts_explicit_cache(tmp_path, monkeypatch):
    """An explicitly passed DocCache is used for both read and write."""
    # build_index() opens its own default cache too; keep that in tmp as well.
    monkeypatch.setenv("PYTHON_DOCS_MCP_CACHE_DIR", str(tmp_path / "default"))
    monkeypatch.setattr(search_mod, "_fetch_library_index_html", _fixture_html)
    cache = DocCache(str(tmp_path / "explicit" / "cache.db"))

    first = get_index(cache=cache)
    assert first["count"] > 150
    # The explicit cache must now hold the index (fresh hit, no rebuild).
    cached_raw = cache.get(INDEX_KEY)
    assert json.loads(cached_raw)["modules"] == first["modules"]

    calls = {"n": 0}

    def counting_fetch():
        calls["n"] += 1
        return _fixture_html()

    monkeypatch.setattr(search_mod, "_fetch_library_index_html", counting_fetch)
    second = get_index(cache=cache)
    assert second == first
    assert calls["n"] == 0  # served from the explicit cache


def test_search_modules_uses_get_index(monkeypatch):
    """search_modules must rank over whatever get_index returns (no network)."""
    monkeypatch.setattr(
        search_mod, "get_index", lambda: _canned()
    )
    results = search_modules("json")
    assert [r["module"] for r in results] == ["json"]
    assert results[0]["score"] == 10.0


def test_constants():
    assert INDEX_KEY == "search-index"
    assert LIBRARY_INDEX_URL == "https://docs.python.org/3/library/index.html"
