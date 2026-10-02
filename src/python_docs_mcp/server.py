"""MCP server exposing CPython stdlib docs and PyPI package info tools.

Tool set:

- ``health_check()`` — trivial liveness probe (server name + version).
- ``python_docs(identifier, topic=None, max_tokens=8000)`` — fetch a stdlib
  module page from docs.python.org and return clean markdown.
  Identifier resolution order:

  1. dotted names (e.g. ``xml.etree.ElementTree``) → fetched directly; the
     docs site only has lowercase filenames, so on 404 the fetcher retries
     with the lowercased name;
  2. plain lowercase module paths (e.g. ``json``) → fetched directly;
  3. other plain names (e.g. ``ElementTree``) → resolved through the search
     index: the unique entry whose full name or last dotted segment matches
     case-insensitively is used; ambiguous or missing matches return an error
     dict suggesting ``python_search``.

  Optional ``topic`` keeps only the matching heading section (plus title and
  description); ``max_tokens`` truncates the final markdown to a token budget
  (estimated as ``len(text) // 4``) cut at a line boundary.
- ``python_search(query, limit=8)`` — rank stdlib modules by name and
  description from the cached search index.
- ``pypi_package(name, version=None)`` — PyPI metadata + README for a package
  (optionally version-pinned), cached 1 day keyed by name+version.
- ``python_status()`` — real health check over the search index, the local
  cache and light GET probes of docs.python.org and pypi.org.

Design rules (hard requirements):

- No tool ever calls or delegates to another tool; each one does its own work
  via :mod:`python_docs_mcp.fetchers`, :mod:`python_docs_mcp.search` and
  :class:`~python_docs_mcp.cache.DocCache`.
- Every tool returns a plain dict. On ANY failure the result is
  ``{"ok": False, "error": "<short message>", "suggestion": "<what to try
  instead>"}``; no exception ever escapes a tool and nothing recurses.
- Fetched content is cached in :class:`~python_docs_mcp.cache.DocCache`:
  module docs TTL 7 days (key ``doc:{module}``), PyPI metadata TTL 1 day
  (key ``pypi:{name}[:{version}]``). Failures are never cached.
"""

from __future__ import annotations

import json
import re

import httpx
from mcp.server.fastmcp import FastMCP

from . import __version__
from .cache import DocCache
from .fetchers import (
    DOCS_BASE_URL,
    PYPI_JSON_BASE_URL,
    USER_AGENT,
    fetch_pypi_package,
    fetch_python_module,
)
from .search import get_index, search_modules

mcp = FastMCP("python-docs")

#: Cache TTLs (seconds). Module docs: one week; PyPI metadata: one day.
DOC_TTL = 604800
PYPI_META_TTL = 86400

#: A plain single-segment module path: a lowercase identifier (docs.python.org
#: serves module pages only under lowercase filenames).
_PLAIN_MODULE_RE = re.compile(r"^[a-z0-9_]+$")

#: Light GET probes for python_status (name, url).
_ENDPOINT_PROBES = (
    ("docs_python_org", f"{DOCS_BASE_URL}json.html"),
    ("pypi_org", f"{PYPI_JSON_BASE_URL}/mcp/json"),
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


# ---------------------------------------------------------------------------
# Small shared helpers (not exposed as tools)
# ---------------------------------------------------------------------------

def _cache() -> DocCache | None:
    """A fresh DocCache, or ``None`` when the cache is unavailable."""
    try:
        return DocCache()
    except Exception:
        return None


def _cache_get(key: str) -> dict | None:
    """Return the cached doc dict for ``key`` (fresh, valid JSON), else None."""
    cache = _cache()
    if cache is None:
        return None
    try:
        raw = cache.get(key)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("markdown"), str):
        return data
    return None


def _cache_set(key: str, value: dict, ttl_seconds: int) -> None:
    """Store a doc dict under ``key``; cache-write failures are ignored."""
    cache = _cache()
    if cache is None:
        return
    try:
        cache.set(key, json.dumps(value), ttl_seconds)
    except Exception:
        pass


def _module_url(module_name: str) -> str:
    """Source URL for a module page (mirrors the fetcher's first candidate)."""
    return f"{DOCS_BASE_URL}{module_name}.html"


def _resolve_plain_name(ident: str, index: dict | None) -> list[dict]:
    """Index entries whose full name or last dotted segment matches ``ident``.

    Case-insensitive exact match on the canonical lowercase module name
    (mirrors the java-spring-mcp plain-name lookup semantics); returns every
    matching entry so callers can detect ambiguity.
    """
    target = str(ident).strip().lower()
    if not target:
        return []
    entries = (index or {}).get("modules") or []
    matches: list[dict] = []
    for entry in entries:
        module = str(entry.get("module") or "").lower()
        if not module:
            continue
        last_segment = module.rsplit(".", 1)[-1]
        if module == target or last_segment == target:
            matches.append(entry)
    return matches


def _extract_topic_section(markdown: str, topic: str) -> tuple[str | None, list[str]]:
    """Extract the first heading section whose text contains ``topic``.

    Returns ``(section_markdown_or_None, all_heading_texts)``.  The section
    runs from the matched heading up to (excluding) the next heading of the
    same or a higher level.
    """
    lines = markdown.splitlines()
    headings: list[str] = []
    target: tuple[int, int] | None = None  # (line index, heading level)
    for i, line in enumerate(lines):
        match = _HEADING_RE.match(line.strip())
        if not match:
            continue
        text = match.group(2).strip()
        headings.append(text)
        if target is None and topic.lower() in text.lower():
            target = (i, len(match.group(1)))
    if target is None:
        return None, headings
    idx, level = target
    end = len(lines)
    for j in range(idx + 1, len(lines)):
        match = _HEADING_RE.match(lines[j].strip())
        if match and len(match.group(1)) <= level:
            end = j
            break
    return "\n".join(lines[idx:end]).rstrip(), headings


def _title_and_description(markdown: str) -> str:
    """Everything before the first level-2 heading (title + description)."""
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        match = _HEADING_RE.match(line.strip())
        if match and len(match.group(1)) == 2:
            return "\n".join(lines[:i]).strip()
    # No level-2 heading: keep only the title line to avoid duplicating body.
    return lines[0].strip() if lines else ""


def _apply_topic(markdown: str, topic: str | None) -> tuple[str, str | None]:
    """Apply optional topic filtering; returns (content, note_or_None)."""
    if not topic:
        return markdown, None
    section, headings = _extract_topic_section(markdown, str(topic))
    if section is None:
        listing = ", ".join(headings[:25]) or "(none)"
        return markdown, (
            f"no section matching {topic!r}; available headings: {listing}"
        )
    preface = _title_and_description(markdown)
    content = f"{preface}\n\n{section}".strip() if preface else section
    return content, None


def _truncate_markdown(markdown: str, max_tokens: int | None) -> tuple[str, bool]:
    """Truncate to ~max_tokens tokens (len//4 estimate) at a line boundary."""
    if not max_tokens or max_tokens <= 0:
        return markdown, False
    total = len(markdown) // 4
    if total <= int(max_tokens):
        return markdown, False
    budget = int(max_tokens) * 4
    cut = markdown[:budget]
    newline = cut.rfind("\n")
    if newline > 0:
        cut = cut[:newline]
    shown = len(cut) // 4
    note = f"\n\n[truncated: showing ~{shown} of ~{total} estimated tokens]"
    return cut.rstrip() + note, True


def _doc_payload(
    *,
    identifier: str,
    url: str,
    title: str | None,
    markdown: str,
    topic: str | None,
    max_tokens: int,
    cached: bool,
    note: str | None = None,
) -> dict:
    """Assemble the python_docs success shape (topic filter + truncation)."""
    content, topic_note = _apply_topic(markdown, topic)
    if topic_note and note:
        note = f"{note}; {topic_note}"
    elif topic_note:
        note = topic_note
    content, truncated = _truncate_markdown(content, max_tokens)
    payload: dict = {
        "ok": True,
        "identifier": identifier,
        "url": url,
        "markdown": content,
        "truncated": truncated,
        "cached": cached,
    }
    if title:
        payload["title"] = title
    if note:
        payload["note"] = note
    return payload


def _error_from(fetch_result: dict, context: str) -> dict:
    """Convert a fetcher failure dict into the tool-level error shape."""
    out: dict = {"ok": False, "error": f"{context}: {fetch_result.get('error', 'unknown failure')}"}
    if fetch_result.get("suggestion"):
        out["suggestion"] = fetch_result["suggestion"]
    else:
        out["suggestion"] = "retry, or use python_search() to find the right module"
    return out


def _probe_endpoint(url: str) -> dict:
    """Light GET probe (quick httpx, then a short curl fallback); never raises.

    The curl fallback matters for hosts whose edge intermittently stalls
    Python TLS clients while serving curl fine — the same behaviour
    fetchers._fetch_url works around.
    """
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": USER_AGENT})
        if resp.status_code < 400:
            return {"status": "ok", "http_status": resp.status_code}
    except Exception:
        pass
    try:
        import subprocess

        proc = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
             "-L", "--max-time", "10", url],
            capture_output=True, text=True, timeout=20,
        )
        code = int(proc.stdout.strip() or 0)
        if code:
            return {"status": "ok" if code < 400 else "error", "http_status": code}
    except Exception as exc:
        return {"status": "error", "http_status": None, "error": f"{type(exc).__name__}: {exc}"}
    return {"status": "error", "http_status": None, "error": "no response from httpx or curl"}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def health_check() -> dict:
    """Check that the server is up and report its version."""
    return {"status": "ok", "server": "python-docs-mcp", "version": __version__}


@mcp.tool()
def python_docs(identifier: str, topic: str | None = None, max_tokens: int = 8000) -> dict:
    """Fetch a CPython stdlib module's documentation page as markdown.

    ``identifier`` resolution order:
      1. dotted names like "xml.etree.ElementTree" → fetched directly; the
         docs site only has lowercase filenames, so on 404 the fetcher retries
         with the lowercased name;
      2. plain lowercase module paths like "json" → fetched directly;
      3. other plain names like "ElementTree" → resolved through the search
         index: the unique entry whose full name or last dotted segment matches
         case-insensitively is fetched (e.g. "ElementTree" →
         xml.etree.elementtree); ambiguous or missing matches return an error
         dict suggesting python_search().

    ``topic`` (a heading like "Functions") keeps only that heading section plus
    title/description; if no such section exists the full content is returned
    with a "note" listing the available headings. ``max_tokens`` truncates the
    final markdown to roughly that many tokens (estimated as len(text)//4),
    cut at a line boundary, and sets "truncated": true when applied.

    On failure returns {"ok": False, "error", "suggestion"} — try
    python_search() to find the right module name.
    """
    try:
        return _python_docs_impl(identifier, topic, max_tokens)
    except Exception as exc:  # last-resort guard: never let an exception escape
        return {
            "ok": False,
            "error": f"unexpected failure: {type(exc).__name__}: {exc}",
            "suggestion": "retry, or use python_search() to find the right module",
        }


def _python_docs_impl(identifier: str, topic: str | None, max_tokens: int) -> dict:
    ident = str(identifier or "").strip()
    if not ident:
        return {
            "ok": False,
            "error": "identifier is required",
            "suggestion": "pass a module name like 'json' or 'xml.etree.ElementTree'",
        }

    # --- identifier resolution ------------------------------------------------
    if "." in ident or _PLAIN_MODULE_RE.match(ident):
        module_name = ident
    else:
        index = get_index()  # never raises; may carry "error" + empty modules
        matches = _resolve_plain_name(ident, index)
        if not matches:
            detail = ""
            if index.get("error"):
                detail = f" (search index unavailable: {index['error']})"
            return {
                "ok": False,
                "error": f"no stdlib module matching '{ident}' found in the search index{detail}",
                "suggestion": f"use python_search('{ident}') to find the right module name",
            }
        if len(matches) > 1:
            alt_list = ", ".join(str(e.get("module")) for e in matches[:5])
            return {
                "ok": False,
                "error": f"ambiguous name '{ident}': {len(matches)} modules match ({alt_list})",
                "suggestion": "use python_search('...') or pass the full dotted module path",
            }
        module_name = str(matches[0].get("module"))

    # --- fetch (with DocCache) --------------------------------------------------
    key = f"doc:{module_name}"
    cached_doc = _cache_get(key)
    if cached_doc is not None:
        return _doc_payload(
            identifier=ident,
            url=cached_doc.get("url") or _module_url(module_name),
            title=cached_doc.get("title"),
            markdown=cached_doc["markdown"],
            topic=topic,
            max_tokens=max_tokens,
            cached=True,
        )
    result = fetch_python_module(module_name)
    if not result.get("ok"):
        return _error_from(result, f"module '{ident}' fetch failed")
    _cache_set(key, {
        "title": result.get("title"),
        "markdown": result.get("markdown"),
        "url": result.get("url"),
    }, DOC_TTL)
    return _doc_payload(
        identifier=ident,
        url=result.get("url") or _module_url(module_name),
        title=result.get("title"),
        markdown=result.get("markdown", ""),
        topic=topic,
        max_tokens=max_tokens,
        cached=False,
    )


@mcp.tool()
def python_search(query: str, limit: int = 8) -> dict:
    """Search CPython stdlib modules by name and description.

    Ranks entries of the cached search index (every query token must match;
    exact name > startswith > substring in name > substring in description).
    Returns {"ok": True, "query", "results": [{module, description, score}],
    "index_count", "stale"}. When the index is unavailable returns
    {"ok": False, "error", "suggestion"} — retry later.
    """
    try:
        index = get_index()
        modules = index.get("modules") or []
        if not modules and index.get("error"):
            return {
                "ok": False,
                "error": f"search index unavailable: {index['error']}",
                "suggestion": "retry python_search() later (the index rebuilds automatically)",
            }
        results = search_modules(query, limit=limit)
        return {
            "ok": True,
            "query": query,
            "results": results,
            "index_count": int(index.get("count") or len(modules)),
            "stale": bool(index.get("stale")),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"search failed: {type(exc).__name__}: {exc}",
            "suggestion": "retry python_search() later",
        }


@mcp.tool()
def pypi_package(name: str, version: str | None = None) -> dict:
    """Look up a package on PyPI (metadata + README).

    Returns name, resolved version, summary, requires_python, license,
    homepage, project URLs and the README converted to markdown. When
    ``version`` is given the lookup is pinned to that release; otherwise the
    latest version is used. Metadata is cached for 1 day keyed by name+version;
    failures are never cached.

    On failure returns {"ok": False, "error", "suggestion"}.
    """
    try:
        pkg = str(name or "").strip()
        if not pkg:
            return {
                "ok": False,
                "error": "package name is required",
                "suggestion": "pass e.g. name='requests' (optionally version)",
            }
        ver = str(version).strip() if version else None

        key = f"pypi:{pkg}" + (f":{ver}" if ver else "")
        cache = _cache()
        if cache is not None:
            try:
                raw = cache.get(key)
            except Exception:
                raw = None
            if raw is not None:
                try:
                    data = json.loads(raw)
                except (ValueError, TypeError):
                    data = None
                if isinstance(data, dict):
                    out = dict(data)
                    out["cached"] = True
                    return out

        result = fetch_pypi_package(pkg, ver)
        if not result.get("ok"):
            return _error_from(result, "pypi lookup failed")

        data: dict = {
            "ok": True,
            "name": result.get("name"),
            "version": result.get("version"),
            "summary": result.get("summary"),
            "requires_python": result.get("requires_python"),
            "license": result.get("license"),
            "homepage": result.get("homepage"),
            "project_urls": result.get("project_urls"),
            "readme_markdown": result.get("readme_markdown"),
            "url": result.get("url"),
        }
        _cache_set(key, data, PYPI_META_TTL)
        data["cached"] = False
        return data
    except Exception as exc:
        return {
            "ok": False,
            "error": f"pypi lookup failed: {type(exc).__name__}: {exc}",
            "suggestion": "retry, or verify the package name",
        }


@mcp.tool()
def python_status() -> dict:
    """Real health check: search index, local cache and upstream endpoints.

    Probes docs.python.org and pypi.org with a light GET (8s timeout each,
    curl fallback). Never raises; returns {"server", "version",
    "checks": {...}, "overall": "ok"|"degraded"|"error"} where overall is
    "error" when the search index is unavailable, "degraded" when any check
    fails, and "ok" otherwise.
    """
    try:
        checks: dict = {}

        index = get_index()
        entry_count = len(index.get("modules") or [])
        checks["search_index"] = {
            "status": "ok" if entry_count else "error",
            "entries": entry_count,
            "built_at": index.get("built_at"),
            "stale": bool(index.get("stale")),
        }

        try:
            stats = DocCache().stats()
            checks["cache"] = {
                "status": "ok",
                "entries": int(stats.get("entries", 0)),
                "expired": int(stats.get("expired", 0)),
            }
        except Exception as exc:
            checks["cache"] = {
                "status": "error",
                "entries": 0,
                "expired": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }

        for name, url in _ENDPOINT_PROBES:
            checks[name] = _probe_endpoint(url)

        if checks["search_index"]["status"] == "error":
            overall = "error"
        elif any(c.get("status") != "ok" for c in checks.values()):
            overall = "degraded"
        else:
            overall = "ok"
        return {
            "server": "python-docs-mcp",
            "version": __version__,
            "checks": checks,
            "overall": overall,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"status check failed: {type(exc).__name__}: {exc}",
            "suggestion": "retry python_status()",
        }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
