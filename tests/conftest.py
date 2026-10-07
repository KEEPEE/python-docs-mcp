"""Shared offline fixtures for the whole test suite.

Two things must be true for every test in this repo, and both are easy to
forget, so they are autouse here rather than repeated per module:

1. **No real cache directory.** ``PYTHON_DOCS_MCP_CACHE_DIR`` points at a
   per-test temp dir, so tests never read or write the developer's
   ``~/.cache/python-docs-mcp`` (a warm real cache makes tests order-dependent).
   The legacy ``FLUTTER_DOCS_MCP_CACHE_DIR`` / ``JAVA_SPRING_MCP_CACHE_DIR``
   fallbacks that ``cache.default_db_path`` still honours are cleared for the
   same reason.  ``PYTHON_DOCS_MCP_PYPI_JSON_OPT_IN`` is cleared too: the
   default path must never touch the robots-disallowed JSON endpoint, and an
   opt-in left set in a developer's shell would make that untestable.
2. **A fresh politeness layer.** The process-wide singleton is replaced before
   each test with one backed by an in-memory robots store, zero base delay and a
   seeded RNG — no real sleeping, no robots state leaking between tests — and
   reset afterwards so production code can never see a test instance.

Nothing in this file performs network I/O.
"""

from __future__ import annotations

import random

import pytest

from python_docs_mcp import fetchers as fetchers_mod
from python_docs_mcp.politeness import DEFAULT_DISABLE_ENV_VAR, Politeness


@pytest.fixture(autouse=True)
def isolated_cache_dir(tmp_path, monkeypatch):
    """Keep every test out of the real ``~/.cache/python-docs-mcp``."""
    monkeypatch.setenv("PYTHON_DOCS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("FLUTTER_DOCS_MCP_CACHE_DIR", raising=False)
    monkeypatch.delenv("JAVA_SPRING_MCP_CACHE_DIR", raising=False)
    # An opt-out or a PyPI opt-in set in a developer's shell must not silently
    # change what the suite proves. Tests that want either set it themselves.
    monkeypatch.delenv(DEFAULT_DISABLE_ENV_VAR, raising=False)
    monkeypatch.delenv(fetchers_mod.PYPI_JSON_OPT_IN_ENV_VAR, raising=False)
    yield


@pytest.fixture(autouse=True)
def politeness_layer():
    """Fresh, fast, deterministic politeness layer for the duration of a test.

    ``cache_path=":memory:"`` keeps the robots cache in-process: a SQLite file
    per test costs seconds across the suite and buys nothing here. File-backed
    persistence is covered explicitly in ``test_politeness_wiring.py``.

    ``challenge_detector`` is the production one (:func:`fetchers.pypi_challenge_detector`,
    wired by :func:`fetchers.get_politeness`), so the wiring tests exercise the
    same anti-bot path the running server has — including the single retry the
    layer performs before it reports a challenge.
    """
    layer = Politeness(
        fetchers_mod.USER_AGENT,
        cache_path=":memory:",
        base_delay=(0.0, 0.0),  # no throttling sleeps in wiring tests
        rng=random.Random(1234),
        challenge_detector=fetchers_mod.pypi_challenge_detector,
    )
    fetchers_mod.set_politeness(layer)
    try:
        yield layer
    finally:
        fetchers_mod.set_politeness(None)
        layer.close()
