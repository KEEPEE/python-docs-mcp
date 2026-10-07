"""Offline tests for :mod:`python_docs_mcp.cache` — schema migration + validators.

The politeness layer's conditional GET depends on three columns this repo did
not have before (``etag``, ``last_modified``, ``body``).  Real users have a
warm ``cache.db`` from the previous version, so the migration must be additive,
idempotent and data-preserving — that is what most of these tests pin down.
"""

from __future__ import annotations

import os
import sqlite3
import urllib.request

import pytest

from python_docs_mcp.cache import DocCache, default_db_path


def _legacy_db(path: str) -> None:
    """Create the pre-politeness schema: key / value / expires_at only."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT, expires_at REAL)"
    )
    conn.execute("INSERT INTO kv VALUES (?, ?, ?)", ("doc:json", "OLD-VALUE", 9e9))
    conn.commit()
    conn.close()


def _columns(path: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [row[1] for row in conn.execute("PRAGMA table_info(kv)")]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------
def test_migration_adds_the_validator_columns(tmp_path):
    path = str(tmp_path / "cache.db")
    _legacy_db(path)
    assert _columns(path) == ["key", "value", "expires_at"]

    DocCache(path)  # opening is enough: _connect() migrates

    assert _columns(path) == ["key", "value", "expires_at", "etag", "last_modified", "body"]


def test_migration_keeps_existing_rows(tmp_path):
    path = str(tmp_path / "cache.db")
    _legacy_db(path)
    cache = DocCache(path)
    assert cache.get("doc:json") == "OLD-VALUE"


def test_migration_is_idempotent(tmp_path):
    """Opening the same file repeatedly must not error or duplicate columns."""
    path = str(tmp_path / "cache.db")
    _legacy_db(path)
    for _ in range(3):
        DocCache(path)
    assert _columns(path).count("etag") == 1
    assert DocCache(path).get("doc:json") == "OLD-VALUE"


def test_fresh_database_has_the_full_schema(tmp_path):
    path = str(tmp_path / "fresh.db")
    DocCache(path)
    assert _columns(path) == ["key", "value", "expires_at", "etag", "last_modified", "body"]


# ---------------------------------------------------------------------------
# value vs. validators: the two writers must not clobber each other
# ---------------------------------------------------------------------------
def test_set_stores_value_and_validators_together(tmp_path):
    cache = DocCache(str(tmp_path / "cache.db"))
    cache.set("u", "VALUE", 60, etag='"v1"', last_modified="Wed, 21 Oct 2015 07:28:00 GMT", body="RAW")
    entry = cache.get_entry("u")
    assert entry == {
        "value": "VALUE",
        "expired": False,
        "etag": '"v1"',
        "last_modified": "Wed, 21 Oct 2015 07:28:00 GMT",
        "body": "RAW",
    }


def test_set_value_does_not_clobber_validators(tmp_path):
    """The server writes parsed results; the fetcher owns the validators.

    This is the bug the split exists to prevent: a plain ``set`` from the
    server would NULL the ``etag`` and silently disable conditional GET.
    """
    cache = DocCache(str(tmp_path / "cache.db"))
    cache.set_validators("u", etag='"v1"', body="RAW", ttl_seconds=60)
    cache.set_value("u", "PARSED", 60)

    entry = cache.get_entry("u")
    assert entry["value"] == "PARSED"
    assert entry["etag"] == '"v1"'
    assert entry["body"] == "RAW"
    assert cache.get("u") == "PARSED"


def test_set_validators_keeps_value_and_expiry(tmp_path):
    cache = DocCache(str(tmp_path / "cache.db"))
    cache.set_value("u", "PARSED", 3600)
    cache.set_validators("u", etag='"v2"', body="RAW2")  # no ttl → expiry untouched

    entry = cache.get_entry("u")
    assert entry["value"] == "PARSED"
    assert entry["etag"] == '"v2"'
    assert entry["expired"] is False
    assert cache.get("u") == "PARSED"


def test_validators_only_row_never_answers_get(tmp_path):
    """A row with no ``value`` can still be revalidated but is not a cache hit."""
    cache = DocCache(str(tmp_path / "cache.db"))
    cache.set_validators("u", etag='"v1"', body="RAW")
    assert cache.get("u") is None
    entry = cache.get_entry("u")
    assert entry["value"] is None and entry["etag"] == '"v1"'


# ---------------------------------------------------------------------------
# expiry
# ---------------------------------------------------------------------------
def test_expired_row_visible_only_with_include_expired(tmp_path):
    cache = DocCache(str(tmp_path / "cache.db"))
    cache.set("u", "V", -10, etag='"v1"', body="RAW")

    assert cache.get("u") is None
    assert cache.peek("u") == "V"
    entry = cache.get_entry("u", include_expired=True)
    assert entry["expired"] is True
    assert entry["etag"] == '"v1"'  # exactly what conditional GET needs
    assert cache.get_entry("u") is None


def test_stats_counts_entries_and_expired(tmp_path):
    cache = DocCache(str(tmp_path / "cache.db"))
    cache.set("fresh", "A", 3600)
    cache.set("stale", "B", -10)
    stats = cache.stats()
    assert stats["entries"] == 2
    assert stats["expired"] == 1


# ---------------------------------------------------------------------------
# default path
# ---------------------------------------------------------------------------
def test_default_db_path_prefers_this_repos_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHON_DOCS_MCP_CACHE_DIR", str(tmp_path / "a"))
    monkeypatch.setenv("FLUTTER_DOCS_MCP_CACHE_DIR", str(tmp_path / "b"))
    monkeypatch.setenv("JAVA_SPRING_MCP_CACHE_DIR", str(tmp_path / "c"))
    assert default_db_path() == str(tmp_path / "a" / "cache.db")


def test_default_db_path_falls_back_to_legacy_env_vars(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTHON_DOCS_MCP_CACHE_DIR", raising=False)
    monkeypatch.setenv("FLUTTER_DOCS_MCP_CACHE_DIR", str(tmp_path / "b"))
    monkeypatch.delenv("JAVA_SPRING_MCP_CACHE_DIR", raising=False)
    assert default_db_path() == str(tmp_path / "b" / "cache.db")
    monkeypatch.delenv("FLUTTER_DOCS_MCP_CACHE_DIR", raising=False)
    monkeypatch.setenv("JAVA_SPRING_MCP_CACHE_DIR", str(tmp_path / "c"))
    assert default_db_path() == str(tmp_path / "c" / "cache.db")


def test_default_db_path_without_any_env_var(monkeypatch):
    for var in ("PYTHON_DOCS_MCP_CACHE_DIR", "FLUTTER_DOCS_MCP_CACHE_DIR", "JAVA_SPRING_MCP_CACHE_DIR"):
        monkeypatch.delenv(var, raising=False)
    path = default_db_path()
    assert path.endswith("/.cache/python-docs-mcp/cache.db")


# ---------------------------------------------------------------------------
# A13 B3 — the migration runs once; an unusable cache address fails upstream
# ---------------------------------------------------------------------------

def test_schema_migration_runs_once_per_cache(tmp_path, monkeypatch):
    """A12 F-A12-3: ``_ensure_schema`` used to run on *every* connection.

    Functionally fine, but it put a ``PRAGMA table_info`` and a possible
    ``ALTER TABLE`` in front of every single read — and that was exactly the
    line that blew up (``attempt to write a readonly database``) for a cache
    directory the user cannot write.  It now runs once, at construction.
    """
    calls: list[str] = []
    original = DocCache._ensure_schema.__func__

    def counting(cls, conn):
        calls.append("run")
        return original(cls, conn)

    monkeypatch.setattr(DocCache, "_ensure_schema", classmethod(counting))

    cache = DocCache(db_path=str(tmp_path / "cache.db"))
    for i in range(10):
        cache.set(f"k{i}", "v", ttl_seconds=60)
        cache.get(f"k{i}")
        cache.get_entry(f"k{i}", include_expired=True)
        cache.set_value(f"k{i}", "w", ttl_seconds=60)
        cache.set_validators(f"k{i}", etag='"x"', last_modified=None, body=None, ttl_seconds=60)
        cache.peek(f"k{i}")
        cache.stats()

    assert calls == ["run"], f"_ensure_schema ran {len(calls)} times, expected exactly once"


def test_one_shot_migration_is_still_idempotent(tmp_path):
    """Idempotence is kept on purpose: a re-run must be a harmless no-op."""
    db = str(tmp_path / "cache.db")
    cache = DocCache(db_path=db)
    for _ in range(2):
        conn = sqlite3.connect(db)
        try:
            DocCache._ensure_schema(conn)
        finally:
            conn.close()
    cache.set("k", "v", ttl_seconds=60)
    assert cache.get("k") == "v"


def test_unusable_cache_address_raises_at_construction(tmp_path):
    """The contract all four repos rely on: fail **fast**, in ``__init__``.

    Every repo wraps ``DocCache()`` in try/except and degrades to no cache, so
    the failure has to surface here — not later, inside a tool call, on the
    first read that happened to need a migration (A12 F-A12-2).
    """
    with pytest.raises(sqlite3.OperationalError):
        DocCache(db_path=str(tmp_path))  # a directory is not a writable database


# ---------------------------------------------------------------------------
# P5 — a read-only database degrades the cache, it must never break a tool
# ---------------------------------------------------------------------------
# Why the two ``chmod`` tests below are skipped for root: root bypasses the DAC
# permission bits, so ``chmod 0444`` does not produce a read-only database for
# it and the test would go green without ever entering the read-only code path.
# That masking is exactly what hid this bug — the suite was green locally (DSH
# runs as root) while GitHub's non-root runner failed on the schema migration
# with ``sqlite3.OperationalError: attempt to write a readonly database``.
# ``test_read_only_mode_is_forced_even_for_root`` forces the same path with the
# uid taken out of the equation, so both kinds of runner are covered.


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_read_only_database_is_still_readable(tmp_path):
    """A root-owned or 0444 ``cache.db`` stays readable; writes become no-ops."""
    db = str(tmp_path / "cache.db")
    _legacy_db(db)
    os.chmod(db, 0o444)
    try:
        cache = DocCache(db_path=db)
        assert cache.read_only is True
        assert cache.read_only_reason  # spelled out for ``*_status()``
        assert cache.get("doc:json") == "OLD-VALUE"
        # The additive migration was skipped because nothing can be written, so
        # the columns it would have added read as NULL instead of raising
        # ``no such column: etag``.
        entry = cache.get_entry("doc:json", include_expired=True)
        assert entry is not None and entry["etag"] is None and entry["body"] is None
        assert set(_columns(db)) == {"key", "value", "expires_at"}  # the file is untouched
        # Writes are a silent no-op returning False — the cache is an
        # optimisation, never a required dependency.
        assert cache.set("k", "v", ttl_seconds=60) is False
        assert cache.set_value("k", "v", ttl_seconds=60) is False
        assert cache.set_validators("k", etag='"e1"') is False
        assert cache.get("k") is None and cache.peek("k") is None
        assert cache.stats() == {"entries": 1, "expired": 0}
    finally:
        os.chmod(db, 0o644)  # let tmp_path cleanup work


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_read_only_directory_is_still_readable(tmp_path):
    """The journal lives next to the DB, so an unwritable dir blocks writes too."""
    cache_dir = tmp_path / "cache-dir"
    cache_dir.mkdir()
    db = str(cache_dir / "cache.db")
    warm = DocCache(db_path=db)
    assert warm.set("k", "v", ttl_seconds=3600) is True

    os.chmod(cache_dir, 0o555)
    try:
        cache = DocCache(db_path=db)
        assert cache.read_only is True
        assert cache.get("k") == "v"
        assert cache.set("k2", "v2", ttl_seconds=60) is False
        assert cache.get("k2") is None
        assert cache.stats() == {"entries": 1, "expired": 0}
    finally:
        os.chmod(cache_dir, 0o755)


def test_read_only_mode_is_forced_even_for_root(tmp_path, monkeypatch):
    """Deterministic version of the chmod test — it must pass whatever the uid.

    ``chmod`` is meaningless for root, so the migration itself is made to raise
    the exact error SQLite raises for a database the process cannot write.
    """
    db = str(tmp_path / "cache.db")
    _legacy_db(db)

    def refusing_migration(cls, conn):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(DocCache, "_ensure_schema", classmethod(refusing_migration))

    cache = DocCache(db_path=db)
    assert cache.read_only is True
    assert "readonly" in (cache.read_only_reason or "")
    # reads still work …
    assert cache.get("doc:json") == "OLD-VALUE"
    # … the columns the skipped migration would have added read as NULL …
    entry = cache.get_entry("doc:json", include_expired=True)
    assert entry is not None and entry["etag"] is None and entry["body"] is None
    # … and every write is a no-op, never an exception.
    assert cache.set("k", "v", ttl_seconds=60) is False
    assert cache.set_value("k", "v", ttl_seconds=60) is False
    assert cache.set_validators("k", etag='"e1"') is False
    assert cache.get("k") is None and cache.peek("k") is None
    assert cache.stats() == {"entries": 1, "expired": 0}
    # every later connection really is opened through the ``mode=ro`` URI:
    # SQLite itself refuses the write, whatever the uid of the process.
    conn = cache._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO kv (key, value) VALUES ('x', 'y')")
    finally:
        conn.close()


def test_unwritable_directory_is_detected_without_chmod(tmp_path, monkeypatch):
    """Same as the read-only-directory test, forced so that root covers it.

    The up-front ``os.access`` hint is stubbed to report what it reports on a
    non-root runner: the directory cannot hold SQLite's journal.
    """
    db = str(tmp_path / "cache.db")
    warm = DocCache(db_path=db)
    assert warm.read_only is False
    assert warm.set("k", "v", ttl_seconds=3600) is True

    monkeypatch.setattr(DocCache, "_write_possible", lambda self: False)
    cache = DocCache(db_path=db)
    assert cache.read_only is True
    assert cache.get("k") == "v"
    assert cache.set("k2", "v2", ttl_seconds=60) is False
    assert cache.get("k2") is None


def test_refused_write_downgrades_the_cache_to_read_only(tmp_path, monkeypatch):
    """Safety net for what the up-front check cannot see.

    Permissions can change after construction, and ``os.access`` is a lie for
    root.  The first write SQLite refuses flips the cache to read-only instead
    of raising out of the tool that merely wanted to cache something.
    """
    db = str(tmp_path / "cache.db")
    cache = DocCache(db_path=db)
    assert cache.set("k", "v", ttl_seconds=3600) is True

    # A ``mode=ro`` connection refuses writes for every uid, exactly like a
    # database the process has no write permission for.
    monkeypatch.setattr(
        DocCache,
        "_connect",
        lambda self: sqlite3.connect(
            f"file:{urllib.request.pathname2url(self.db_path)}?mode=ro", uri=True
        ),
    )
    assert cache.set("k2", "v2", ttl_seconds=60) is False
    assert cache.read_only is True
    assert "readonly" in (cache.read_only_reason or "")
    assert cache.get("k") == "v"  # reads are unaffected
    # later writes are skipped up front, without touching SQLite again
    assert cache.set("k3", "v3", ttl_seconds=60) is False
    assert cache.get("k3") is None


def test_read_only_database_without_a_table_reads_as_empty(tmp_path, monkeypatch):
    """A 0-byte ``cache.db`` that cannot be migrated has no ``kv`` table at all.

    That is still not an error: every read is a miss and every write a no-op.
    """
    db = tmp_path / "cache.db"
    db.write_bytes(b"")
    monkeypatch.setattr(DocCache, "_write_possible", lambda self: False)

    cache = DocCache(db_path=str(db))
    assert cache.read_only is True
    assert cache.get("k") is None
    assert cache.get_entry("k", include_expired=True) is None
    assert cache.peek("k") is None
    assert cache.stats() == {"entries": 0, "expired": 0}
    assert cache.set("k", "v", ttl_seconds=60) is False


def test_forced_migration_failure_still_raises_when_nothing_is_readable(tmp_path, monkeypatch):
    """Read-only mode needs a database that *can* be read.

    A path that is not a database at all has nothing to fall back to, so the
    error still propagates from ``__init__`` and the caller drops the cache —
    the A13 B3 contract every repo's ``try/except DocCache()`` relies on.
    """
    def refusing_migration(cls, conn):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(DocCache, "_ensure_schema", classmethod(refusing_migration))
    # A directory is not a database: nothing can be opened, read or written.
    with pytest.raises(sqlite3.OperationalError):
        DocCache(db_path=str(tmp_path))
