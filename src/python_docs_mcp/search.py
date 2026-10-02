"""Search index over CPython stdlib modules.

Builds a flat list of searchable entries from the "Python Standard Library"
index page (https://docs.python.org/3/library/index.html): every module listed
in its toctree becomes ``{"module", "description", "url"}`` where ``module`` is
the canonical lowercase name derived from the link's href (e.g.
``xml.etree.elementtree``) and ``url`` is the absolute docs page URL.

Public entry points:

- ``build_index() -> dict`` — fetch + parse; persists the result in the
  :class:`~python_docs_mcp.cache.DocCache` under key ``search-index`` with a
  7-day TTL. On network/parse failure it falls back to the cached copy
  (fresh first, then stale via ``peek``) and only raises when nothing is
  available.
- ``get_index(cache=None) -> dict`` — cached-or-build accessor used by the
  server; never raises (mirrors the java-spring-mcp ``load_index`` semantics).
- ``search_modules(query, limit=8) -> list[dict]`` — offline multi-token
  ranking over module name + description.

Index shape: ``{"modules": [...], "count": int, "built_at": <iso8601 UTC>}``
(a failed ``get_index`` adds ``"error"``; a stale fallback adds ``"stale"``).

No network access happens at import time.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .cache import DocCache
from .fetchers import _client, _fetch_url

__all__ = [
    "INDEX_KEY",
    "LIBRARY_INDEX_URL",
    "DEFAULT_MAX_AGE_SECONDS",
    "parse_library_index_html",
    "build_index",
    "get_index",
    "search_modules",
]

INDEX_KEY = "search-index"
LIBRARY_INDEX_URL = "https://docs.python.org/3/library/index.html"
DEFAULT_MAX_AGE_SECONDS = 604800  # one week (7 days)

#: A canonical module name is a dotted lowercase Python identifier.  This
#: filters out non-module pages that still carry the code/span.pre pattern,
#: e.g. ``unittest.mock-examples.html`` (an examples page with a hyphen).
_MODULE_NAME_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")


# ---------------------------------------------------------------------------
# Parsing (pure / offline)
# ---------------------------------------------------------------------------

def _module_from_href(href: str) -> tuple[str, str] | None:
    """Derive the canonical module name from a toctree href.

    Two shapes occur on the library index page:

    - ``json.html`` → module ``json``;
    - ``curses.html#module-curses.textpad`` → module ``curses.textpad``
      (submodules documented on a parent page carry a ``#module-<name>``
      fragment, which is the canonical lowercase name).

    Returns ``(module_name, href)`` or ``None`` when the href is not a module
    page link (relative nav links like ``../index.html``, absolute paths, or
    fragments that are not ``#module-*`` references on a ``.html`` page).
    """
    page, sep, fragment = href.partition("#")
    if not page.endswith(".html"):
        return None
    if sep and fragment.startswith("module-") and len(fragment) > len("module-"):
        module = fragment[len("module-"):]
    else:
        module = page[: -len(".html")]
    module = module.lower()
    if not module or "/" in module or module.startswith(".."):
        return None
    return module, href


def _description(anchor, code) -> str:
    """Text of the anchor after the leading module name, minus separators.

    Module entries render as ``<code>name</code> — description``; section and
    example pages start with prose instead (and are filtered out before this
    is called).  Returns ``""`` when there is no description.
    """
    full = anchor.get_text(" ", strip=True)
    code_text = code.get_text(" ", strip=True)
    if full.startswith(code_text):
        rest = full[len(code_text):]
    else:  # defensive: name appears later in the text
        idx = full.find(code_text)
        rest = full[idx + len(code_text):] if idx >= 0 else ""
    rest = rest.strip()
    while rest and rest[0] in "\u2014\u2013-":  # "—", "–", "-"
        rest = rest[1:].strip()
    return rest


def parse_library_index_html(html: str, base_url: str = LIBRARY_INDEX_URL) -> list[dict]:
    """Parse the docs.python.org library index page into module entries.

    Each entry is ``{"module", "description", "url"}`` where ``module`` is the
    canonical lowercase name derived from the href and ``url`` is the absolute
    documentation page URL.  Only genuine module entries are kept: the anchor
    must link to a ``.html`` page, carry a leading ``<code><span class="pre">``
    name, and derive a valid dotted module name.  Section pages ("The
    compression package"), example pages (``sys_path_init.html``,
    ``unittest.mock-examples.html``) and nav links are all excluded.

    Raises ``ValueError`` when the document yields no entries.
    """
    soup = BeautifulSoup(html, "lxml")

    entries: list[dict] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a"):
        code = anchor.find("code", recursive=False)
        if code is None:
            continue
        span = code.find("span", class_="pre")
        if span is None:
            continue

        # The module name must LEAD the anchor text ("<code>name</code> — …");
        # prose-first anchors are section/example pages with an inline xref.
        full_text = anchor.get_text(" ", strip=True)
        code_text = code.get_text(" ", strip=True)
        if not full_text.startswith(code_text):
            continue

        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("/", "..")):
            continue
        derived = _module_from_href(href)
        if derived is None:
            continue
        module, page_href = derived
        if not _MODULE_NAME_RE.match(module):
            continue
        if module in seen:  # defensive: the page has one toctree today
            continue
        seen.add(module)

        entries.append(
            {
                "module": module,
                "description": _description(anchor, code),
                "url": urljoin(base_url, page_href),
            }
        )

    if not entries:
        raise ValueError("not a library index page (no module entries found)")
    return entries


# ---------------------------------------------------------------------------
# Index building / loading
# ---------------------------------------------------------------------------

def _open_cache() -> DocCache | None:
    try:
        return DocCache()
    except Exception:
        return None  # cache unavailable; degrade to build-only behaviour


def _valid_index(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("modules"), list)


def _read_cache(cache: DocCache | None, fresh_only: bool = True) -> dict | None:
    """Return a valid cached index or ``None``.

    With ``fresh_only=False`` an expired row is still returned (via
    ``cache.peek``) so callers can serve it as a stale fallback.
    """
    if cache is None:
        return None
    try:
        raw = cache.get(INDEX_KEY)
        if raw is None and not fresh_only:
            raw = cache.peek(INDEX_KEY)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None  # corrupt cache value
    return data if _valid_index(data) else None


def _fetch_library_index_html() -> str:
    """GET the library index page; raises ``RuntimeError`` on any failure."""
    with _client() as client:
        response, transport_error = _fetch_url(client, LIBRARY_INDEX_URL)
    if response is None:
        raise RuntimeError(f"transport error fetching library index: {transport_error}")
    if response.status_code >= 400:
        raise RuntimeError(
            f"library index request failed with HTTP {response.status_code}"
        )
    return response.text


def _build_index(cache: DocCache | None) -> dict:
    """Fetch + parse + persist.  May raise (network or parse failures)."""
    html = _fetch_library_index_html()
    modules = parse_library_index_html(html, base_url=LIBRARY_INDEX_URL)
    index = {
        "modules": modules,
        "count": len(modules),
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    if cache is not None:
        try:
            cache.set(INDEX_KEY, json.dumps(index), DEFAULT_MAX_AGE_SECONDS)
        except Exception:
            pass  # a cache-write failure must not break the returned index
    return index


def build_index() -> dict:
    """Fetch the library index page and build the full index.

    Returns ``{"modules": [...], "count": int, "built_at": <iso8601 UTC>}``
    and persists it in the :class:`DocCache` under key ``search-index`` with a
    7-day TTL.  On network or parse failure it falls back to the cached copy —
    fresh first (``cache.get``), then stale (``cache.peek``, returned with
    ``"stale": True``) — and only raises when no usable cache exists.
    """
    cache = _open_cache()
    try:
        return _build_index(cache)
    except Exception as exc:
        cached = _read_cache(cache, fresh_only=True)
        if cached is not None:
            return cached
        stale = _read_cache(cache, fresh_only=False)
        if stale is not None:
            stale = dict(stale)
            stale["stale"] = True
            return stale
        raise RuntimeError(f"index build failed: {type(exc).__name__}: {exc}") from exc


def get_index(cache: DocCache | None = None) -> dict:
    """Return the search index, using the :class:`DocCache` under key
    ``search-index``.  Never raises (mirrors java-spring-mcp's load_index).

    - Fresh (unexpired) cache hit: returned immediately, no network.
    - Miss: ``build_index()`` runs; on success the result is cached with a
      7-day TTL and returned.
    - Build failure with an old cached copy present: the old copy is returned
      with ``"stale": True``.
    - Build failure with no usable cache: returns
      ``{"modules": [], "count": 0, "built_at": None, "error": ...}``.

    ``cache`` may be passed explicitly (tests, custom storage); when omitted a
    default :class:`DocCache` is opened.
    """
    if cache is None:
        cache = _open_cache()

    cached = _read_cache(cache, fresh_only=True)
    if cached is not None:
        return cached

    try:
        index = build_index()
    except Exception as exc:
        stale = _read_cache(cache, fresh_only=False)
        if stale is not None:
            stale = dict(stale)
            stale["stale"] = True
            return stale
        return {
            "modules": [],
            "count": 0,
            "built_at": None,
            "error": f"index build failed: {type(exc).__name__}: {exc}",
        }

    # Persist to *this* cache when the (possibly monkeypatched) build did not.
    if cache is not None and index.get("built_at") is not None and not index.get("stale"):
        try:
            cache.set(INDEX_KEY, json.dumps(index), DEFAULT_MAX_AGE_SECONDS)
        except Exception:
            pass
    return index


# ---------------------------------------------------------------------------
# Search / ranking (pure / offline)
# ---------------------------------------------------------------------------

#: Per-token score tiers.  Exact name > startswith > substring in name >
#: substring in description; a token that matches neither field excludes the
#: entry from the results entirely.
_EXACT_SCORE = 10.0
_STARTSWITH_SCORE = 8.0
_SUBSTRING_NAME_SCORE = 6.0
_SUBSTRING_DESC_SCORE = 3.0


def _token_score(token: str, module_lower: str, description_lower: str) -> float:
    """Score one lowercase query token against a module entry."""
    if token == module_lower:
        return _EXACT_SCORE
    if module_lower.startswith(token):
        return _STARTSWITH_SCORE
    if token in module_lower:
        return _SUBSTRING_NAME_SCORE
    if description_lower and token in description_lower:
        return _SUBSTRING_DESC_SCORE
    return 0.0


def search_modules(query: str | None, limit: int = 8) -> list[dict]:
    """Rank stdlib modules by how well their name/description match ``query``.

    Case-insensitive.  The query is split into whitespace-separated tokens; a
    module must match **every** token (exact name > startswith > substring in
    name > substring in description) and its score is the sum of the per-token
    scores.  Results are ``{"module", "description", "score"}`` dicts sorted
    best-first (ties broken by module name).

    The index comes from :func:`get_index`, so a bare call works without
    touching the network on cache hits.  Empty/None queries return ``[]``.
    """
    if query is None:
        return []
    text = str(query).strip().lower()
    tokens = text.split()
    if not tokens:
        return []

    index = get_index()
    modules = index.get("modules") or []

    results: list[dict] = []
    for entry in modules:
        module_lower = str(entry.get("module") or "").lower()
        description_lower = str(entry.get("description") or "").lower()
        score = 0.0
        matched = True
        for token in tokens:
            token_score = _token_score(token, module_lower, description_lower)
            if token_score <= 0.0:
                matched = False
                break
            score += token_score
        if matched and score > 0.0:
            results.append(
                {
                    "module": entry.get("module", ""),
                    "description": entry.get("description", ""),
                    "score": round(score, 3),
                }
            )

    results.sort(key=lambda r: (-r["score"], r["module"].lower()))
    if limit is not None:
        results = results[: max(0, int(limit))]
    return results
