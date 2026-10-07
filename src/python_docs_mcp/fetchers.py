"""Fetchers for CPython stdlib documentation and PyPI package info.

Two public entry points (later phases build MCP tools on top of these):

- :func:`fetch_python_module` — fetches a module page from
  https://docs.python.org/3/library/{module}.html and converts the Sphinx
  HTML to clean markdown.
- :func:`fetch_pypi_package` — fetches the PyPI **project page**
  (https://pypi.org/project/{name}/[{version}/]) and extracts metadata +
  README from its HTML.

Both functions are total: they never raise, they return ``{"ok": False,
"error": ...}`` dicts on failure.

The HTTP layer is deliberately thin so that the parsing logic lives in pure
functions (:func:`parse_python_module_html`, :func:`parse_pypi_project_html`,
:func:`parse_pypi_json`) that the offline test-suite can exercise against
downloaded fixtures.

Why the PyPI **HTML page** and not the JSON API
-----------------------------------------------
``https://pypi.org/robots.txt`` says, for ``User-agent: *``::

    Disallow: /pypi/*/json
    Disallow: /pypi/*/*/json
    Disallow: /pypi*?
    Disallow: /simple/

i.e. the JSON API this module used to call (``/pypi/{name}/json``) is
explicitly disallowed, and the Simple API (``/simple/``) is too.  The project
page ``/project/{name}/`` is allowed, so it is now the **only** default
source.  ``urllib.robotparser`` on Python 3.12 does not understand ``*`` in
rules, which is why the old code never noticed (A2 §1.2).

The JSON API is reachable only through an explicit opt-in
(``PYTHON_DOCS_MCP_PYPI_JSON_OPT_IN=1``).  When it is on, the result carries a
``"notice"`` field spelling out that PyPI's robots.txt forbids that endpoint
and that using it is the user's responsibility — and the opt-in path
deliberately does **not** go through the politeness layer's robots decision
(which blocks that URL; see ``tests/test_politeness_wiring.py``).  When the
opt-in is off, the JSON endpoint is never requested — **not even as a fallback
when the HTML page fails**.

Politeness
----------
Every request goes through :mod:`python_docs_mcp.politeness`: robots.txt rules
with an RFC 9309 wildcard matcher, per-host throttle (the audit measured a
7 ms gap between two docs.python.org requests — A2 §4.1), ``429``/``503``/
``504`` handling with ``Retry-After``, stall detection, conditional GET
(``ETag`` / ``Last-Modified`` → ``304``) and a per-tool-call request budget.
The layer never raises and never changes the return shape.  Opt out with
``PYTHON_DOCS_MCP_POLITENESS_DISABLED=1`` — at the user's own risk.
"""

from __future__ import annotations

import json as _json
import os
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

from .cache import DocCache
from .politeness import Politeness, default_robots_db_path

__all__ = [
    "fetch_python_module",
    "fetch_pypi_package",
    "parse_python_module_html",
    "parse_pypi_json",
    "parse_pypi_project_html",
    "pypi_project_url",
    "pypi_json_url",
    "pypi_json_opt_in_enabled",
    "pypi_challenge_detector",
    "_module_url_candidates",
    "get_politeness",
    "set_politeness",
    "tool_budget",
    "current_budget",
]

DOCS_BASE_URL = "https://docs.python.org/3/library/"
#: Robots-DISALLOWED (``Disallow: /pypi/*/json``).  Kept only for the explicit
#: opt-in path and so that ``server`` can prove it never probes it.
PYPI_JSON_BASE_URL = "https://pypi.org/pypi"
#: Robots-allowed and the default source since the politeness work landed.
PYPI_PROJECT_BASE_URL = "https://pypi.org/project/"

#: connect 5 s / read 10 s / total 15 s — the values A2 §4.3 settled on (the
#: module used to allow a bare 20 s with no retry discipline around it).
REQUEST_TIMEOUT = 15.0
TIMEOUTS = (5.0, 10.0, REQUEST_TIMEOUT)

#: Honest, contactable UA (A1 §6 / A2 §4.3).  This module used to send a
#: Chrome 126 spoof: it buys nothing, it defeats ``User-agent:``-specific
#: robots rules (a robots file can only match a token it can see) and it hides
#: who is knocking.
USER_AGENT = (
    "python-docs-mcp/0.1 (+https://github.com/KEEPEE/python-docs-mcp)"
)

#: The only hosts this module ever contacts.  Handed to the politeness layer as
#: an allowlist so an unexpected redirect or a malformed identifier can never
#: turn a docs lookup into a request to somebody else's site.
ALLOWED_HOSTS = frozenset({"docs.python.org", "pypi.org"})

#: Hard cap on requests one MCP tool call may make (A2 §4.3).  Since A8 F3 a
#: unit is **one request on the wire**, so the robots.txt fetch of a cold host
#: pays too.  Measured live (A10 smoke): cold ``python_docs("ElementTree")`` =
#: robots + library-index page + module page = **3**; cold ``python_docs("json")``
#: = robots + page = **2**; the error path (unknown module) is bounded by the
#: candidate loop at robots + index + ≤2 pages = **4**, whatever the limit is.
#: 6 therefore leaves 3 units of headroom for a ``429`` retry or a redirect
#: chain on the legitimate path, while an unknown name still cannot spend more
#: than 4 requests.
FETCH_BUDGET_LIMIT = 6

#: TTL for the raw body + validators the fetcher keeps for conditional GET.
#: Matches the server's docs TTL so a revalidation window always exists.
REVALIDATION_TTL_SECONDS = 7 * 24 * 3600

#: Stable prefix of the layer's budget message (``Politeness.get``); callers
#: use it to stop a candidate loop instead of burning the remaining attempts on
#: requests that will never be made.
_BUDGET_ERROR_PREFIX = "request budget exhausted"

#: Opt-in switch for the robots-disallowed PyPI JSON API.
PYPI_JSON_OPT_IN_ENV_VAR = "PYTHON_DOCS_MCP_PYPI_JSON_OPT_IN"
_TRUE_VALUES = ("1", "true", "yes", "on")

#: Shipped verbatim in the result whenever the opt-in path is used.
PYPI_JSON_OPT_IN_NOTICE = (
    "PyPI's robots.txt disallows /pypi/*/json and /pypi/*/*/json for every "
    "user-agent. The JSON API was used because "
    f"{PYPI_JSON_OPT_IN_ENV_VAR}=1 is set; that is a deliberate, "
    "user-authorised bypass of the politeness layer and it is the user's "
    "responsibility."
)

#: PyPI's edge (Fastly) answers some cache-miss URLs with a JavaScript
#: "Client Challenge" page instead of the project page — HTTP 200, ~3 KB.
#: Detected explicitly so the tool reports the truth instead of parsing junk
#: or quietly falling back to the disallowed JSON API.
#:
#: The wording is deliberately two-sided.  A challenge page is what PyPI's edge
#: returns for *both* a cold-URL cache miss **and** a package that does not
#: exist (measured live: an unknown name came back as HTTP 200 + the same
#: ~3 KB challenge, not as a 404), so claiming "this package is behind a bot
#: wall" would be a guess.  The honest statement is "either of the two".
BOT_CHALLENGE_ERROR = (
    "pypi.org answered with a JavaScript bot-verification page (HTTP 200, not a "
    "404) instead of the project page. Either PyPI is serving its anti-bot "
    "challenge for this URL, or the package does not exist — PyPI also answers "
    "unknown package names with that page, so a challenge is not proof the name "
    "is valid"
)
BOT_CHALLENGE_SUGGESTION = (
    "retry later (PyPI serves that page for some cold URLs), check the name "
    "(case-sensitive) or drop the version pin, or use the "
    f"{PYPI_JSON_OPT_IN_ENV_VAR}=1 opt-in knowing that PyPI's robots.txt "
    "disallows the JSON API"
)

# -- the layer's anti-bot hook (A8 F4), PyPI-specific and repo-local --------- #

#: Largest body the challenge page ever measured was (3 038 B on 2026-10-06);
#: anything bigger is a real page, and the marker strings below also occur in
#: the <script> of large pages, so size is the first and cheapest filter.
_PYPI_CHALLENGE_MAX_BYTES = 8 * 1024

#: Strings that only the challenge page carries.  ``_fs-ch-`` is the asset path
#: of Fastly's challenge script; the other two are its title/labels.
_PYPI_CHALLENGE_MARKERS = (b"_fs-ch-", b"client challenge", b"js-challenge")

#: Strings a *real* project page always carries.  Their presence vetoes the
#: verdict, which is what keeps the detector from eating a genuine page that
#: merely embeds a challenge-ish script.
_PYPI_PAGE_MARKERS = (b"project-header__name", b"package-header__name", b"pip-command")


def pypi_challenge_detector(body: bytes, headers: httpx.Headers) -> bool:
    """True only for PyPI's JS "Client Challenge" page (A8 F4 hook).

    Passed to :class:`~python_docs_mcp.politeness.Politeness` as
    ``challenge_detector``; the reference module deliberately ships no
    site-specific heuristic, so the heuristic lives here.  Three independent
    conditions, cheapest first:

    1. the body is small (the challenge measured 3 038 B; a real project page
       measured 130 507 B),
    2. it is an HTML document (a JSON or gzip artifact is never a challenge),
    3. it carries a challenge marker **and** none of the markers a real project
       page always has.

    Contract of the hook: cheap, no network I/O, never raises (the layer also
    guards a raising detector).  ``headers`` is used only for the content-type
    veto; a missing content type never makes the verdict *less* safe.
    """
    if len(body) > _PYPI_CHALLENGE_MAX_BYTES:
        return False
    content_type = (headers.get("content-type") or "").lower()
    if content_type and "html" not in content_type:
        return False
    low = body.lower()
    if any(marker in low for marker in _PYPI_PAGE_MARKERS):
        return False
    return b"<!doctype html" in low and any(
        marker in low for marker in _PYPI_CHALLENGE_MARKERS
    )

# Trailing "— Python 3.14.8 documentation" (or unversioned variant) appended to
# every docs.python.org <title>.
_TITLE_SUFFIX_RE = re.compile(r"\s*—\s*Python\s+[\d.]*\s*documentation\s*$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Politeness layer (process-wide singleton)
# ---------------------------------------------------------------------------

_politeness: Politeness | None = None
_politeness_lock = threading.Lock()


def get_politeness() -> Politeness:
    """Process-wide politeness layer (created on first use).

    The robots cache is a SQLite file next to ``cache.db`` so it survives
    restarts (1 robots request per host per 7 days, not per call).  If that
    directory is not writable the layer falls back to a per-process in-memory
    cache — politeness still applies, it just forgets across restarts.  A
    missing cache must never take a tool down.  Tests replace the instance
    through :func:`set_politeness`.

    ``challenge_detector`` wires this repo's :func:`pypi_challenge_detector`
    into the layer (A8 F4): a PyPI "Client Challenge" page is HTTP 200, so
    nothing in the HTTP status says "this is not the document".  The layer then
    waits (escalated per-host delay) and retries that **same URL once**; if it
    is still a challenge the fetch comes back as an error with
    ``bot_challenge=True`` and the body is never cached.  There is no fallback
    to another endpoint — ``/pypi/<name>/json`` is disallowed by PyPI's
    robots.txt and is never requested from this path.
    """
    global _politeness
    if _politeness is None:
        with _politeness_lock:
            if _politeness is None:
                kwargs: dict = dict(
                    timeouts=TIMEOUTS,
                    allowed_hosts=ALLOWED_HOSTS,
                    challenge_detector=pypi_challenge_detector,
                )
                try:
                    _politeness = Politeness(
                        USER_AGENT, cache_path=default_robots_db_path(), **kwargs
                    )
                except Exception:
                    _politeness = Politeness(USER_AGENT, cache_path=None, **kwargs)
    return _politeness


def set_politeness(politeness: Politeness | None) -> None:
    """Replace (or with ``None`` reset) the process-wide layer. Test seam."""
    global _politeness
    with _politeness_lock:
        _politeness = politeness


#: Budget bound by the enclosing :func:`tool_budget` (``(scope, limit)``).
#: A ContextVar, not a module global, so a tool call running in a worker
#: thread can never spend another call's budget.
_current_budget: ContextVar[tuple[str, int] | None] = ContextVar(
    "python_docs_mcp_request_budget", default=None
)


@contextmanager
def tool_budget(name: str, limit: int = FETCH_BUDGET_LIMIT):
    """Bind one request budget to everything fetched inside this block.

    A single ``python_docs("ElementTree")`` call can reach the network three
    times (library-index build, then the module page), and a wrong name used to
    be retried candidate after candidate. The budget caps that: one tool call,
    at most ``limit`` requests, index build included. The scope name is stable
    per tool and reset on entry, so ``Politeness._budgets`` cannot grow without
    bound.
    """
    scope = f"tool:{name}"
    try:
        get_politeness().reset_budget(scope)
    except Exception:
        pass  # a broken budget layer must not stop a tool
    token = _current_budget.set((scope, int(limit)))
    try:
        yield scope
    finally:
        _current_budget.reset(token)


def current_budget() -> tuple[str, int] | None:
    """The budget bound by the enclosing :func:`tool_budget`, if any."""
    return _current_budget.get()


# ---------------------------------------------------------------------------
# Revalidation data (validators + raw body) live in DocCache
# ---------------------------------------------------------------------------

def _doc_cache() -> DocCache | None:
    """DocCache for revalidation data, or ``None`` if it cannot be opened.

    A cache problem must never turn into a fetch failure.
    """
    try:
        return DocCache()
    except Exception:
        return None


def _revalidation_for(url: str) -> tuple[dict | None, bytes | None]:
    """``(validators, cached_body)`` stored for ``url`` by an earlier fetch.

    Both are returned together or not at all: a conditional GET without a body
    to serve would throw away the ``304`` (A3 §5.4).
    """
    cache = _doc_cache()
    if cache is None:
        return None, None
    try:
        entry = cache.get_entry(url, include_expired=True)
    except Exception:
        return None, None
    if not entry or not entry.get("body"):
        return None, None
    validators = {
        k: v
        for k, v in (
            ("etag", entry.get("etag")),
            ("last_modified", entry.get("last_modified")),
        )
        if v
    }
    if not validators:
        return None, None
    return validators, str(entry["body"]).encode("utf-8", "replace")


def _remember_revalidation(url: str, validators: dict, body: str) -> None:
    """Persist validators + raw body so the next fetch can send a conditional GET."""
    if not validators:
        return
    cache = _doc_cache()
    if cache is None:
        return
    try:
        cache.set_validators(
            url,
            etag=validators.get("etag"),
            last_modified=validators.get("last_modified"),
            body=body,
            ttl_seconds=REVALIDATION_TTL_SECONDS,
        )
    except Exception:
        pass  # never break a successful fetch over cache bookkeeping


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class _Response:
    """Minimal response shim shared by every fetch path in this module.

    Exactly one of ``text`` / ``error`` is meaningful.  ``status_code`` is
    *truthful*: ``304`` with ``from_cache=True`` when a conditional GET
    revalidated a stored body, and ``None`` when no HTTP response was obtained
    at all (robots block, budget exhausted, transport failure after retry).
    """

    def __init__(
        self,
        status_code: int | None,
        text: str,
        url: str,
        *,
        error: str | None = None,
        from_cache: bool = False,
        blocked_by_robots: bool = False,
        budget_exhausted: bool = False,
        validators: dict | None = None,
        bot_challenge: bool = False,
    ):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.error = error
        self.from_cache = from_cache
        self.blocked_by_robots = blocked_by_robots
        self.budget_exhausted = budget_exhausted
        self.validators = validators or {}
        #: Set when the layer's ``challenge_detector`` (A8 F4) judged the body
        #: an anti-bot page.  ``status_code`` stays truthful (200) — which is
        #: exactly why the caller must look at this flag, not at the status.
        self.bot_challenge = bot_challenge

    def json(self):
        return _json.loads(self.text)


def _client() -> httpx.Client:
    """The one place an ``httpx.Client`` is built in this module.

    The politeness layer sets its own honest UA per request, so the client
    header is only a default for the one path that bypasses the layer (the
    opt-in PyPI JSON call).
    """
    return httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _budget_for(url: str) -> tuple[str, int]:
    """Budget a request made outside any :func:`tool_budget` should spend."""
    bound = current_budget()
    if bound is not None:
        return bound
    host = (urlparse(url).netloc or "").lower()
    scope = f"fetch:{host}"
    try:
        get_politeness().reset_budget(scope)
    except Exception:
        pass
    return scope, FETCH_BUDGET_LIMIT


def _http_get(
    url: str,
    *,
    budget_scope: str | None = None,
    budget_limit: int = FETCH_BUDGET_LIMIT,
    revalidate: bool = True,
) -> _Response:
    """GET ``url`` through the politeness layer.  Never raises.

    Order inside the layer: robots → budget → throttle (incl. ``Crawl-delay``)
    → conditional GET → send → ``429``/``503``/``504`` with ``Retry-After`` →
    stall detection.  The old version of this function called ``client.get``
    twice back-to-back with no delay and no robots check at all.

    Unless ``revalidate`` is false, the validators and raw body of an earlier
    response for the same URL are read from :class:`DocCache` and offered as
    ``If-None-Match`` / ``If-Modified-Since``, and a fresh ``200`` writes them
    back.  Both are passed together or not at all (A3 §5.4).
    """
    if budget_scope is None:
        budget_scope, budget_limit = _budget_for(url)

    validators: dict | None = None
    cached_body: bytes | None = None
    if revalidate:
        validators, cached_body = _revalidation_for(url)

    with _client() as client:
        response = get_politeness().get(
            client,
            url,
            validators=validators,
            cached_body=cached_body,
            budget_scope=budget_scope,
            budget_limit=budget_limit,
        )

    final_url = response.url or url
    if response.blocked_by_robots or response.error:
        error = response.error or f"request to {url} failed"
        return _Response(
            response.status_code,
            "",
            final_url,
            error=error,
            blocked_by_robots=response.blocked_by_robots,
            budget_exhausted=error.startswith(_BUDGET_ERROR_PREFIX),
            validators=response.validators or {},
            bot_challenge=bool(getattr(response, "bot_challenge", False)),
        )

    if response.from_cache:
        # 304: the body is the one we already had, nothing was re-downloaded.
        # The response carries *fresh* validators, so write them back — keeping
        # the ones we sent would offer a stale ETag on the next revalidation.
        if revalidate:
            _remember_revalidation(url, response.validators or {}, response.text)
        return _Response(
            response.status_code,
            response.text,
            final_url,
            from_cache=True,
            validators=response.validators or {},
        )

    if response.status_code is None or response.status_code >= 400:
        return _Response(
            response.status_code,
            response.text,
            final_url,
            error=f"HTTP {response.status_code} from {url}",
            validators=response.validators or {},
        )

    if revalidate:
        _remember_revalidation(url, response.validators or {}, response.text)
    return _Response(
        response.status_code,
        response.text,
        final_url,
        validators=response.validators or {},
    )


def _layer_error_text(response: _Response, host_label: str) -> str:
    """Human-readable failure text that keeps this module's old wording."""
    if response.blocked_by_robots or response.budget_exhausted:
        return response.error or "request blocked"
    if response.status_code is not None and (response.error or "").startswith("HTTP "):
        return f"{host_label} returned HTTP {response.status_code}"
    return response.error or "unknown transport error"


def _module_url_candidates(module_name: str) -> list[str]:
    """URLs to try for ``module_name``, in order — LOWERCASE FIRST.

    docs.python.org serves module pages only under lowercase filenames, so the
    old order (name as given, then lowercase) guaranteed one wasted 404 per
    mixed-case lookup — measured in A2 §4.1:
    ``xml.etree.ElementTree.html`` → 404, then ``xml.etree.elementtree.html`` →
    200.  The lowercased name now comes first and the name as given is kept
    only as a fallback, which costs a request in the rare case where the
    lowercase page does not exist.  For an already-lowercase name there is
    exactly one candidate, exactly as before.
    """
    names: list[str] = []
    lowered = module_name.lower()
    if lowered != module_name:
        names.append(lowered)
    names.append(module_name)
    unique: list[str] = []
    for name in names:
        if name not in unique:  # dedup: a lowercase name yields one candidate
            unique.append(name)
    return [f"{DOCS_BASE_URL}{name}.html" for name in unique]


# ---------------------------------------------------------------------------
# CPython docs page parsing
# ---------------------------------------------------------------------------

def _extract_title(soup: BeautifulSoup, h1: Any) -> str:
    """Module title + one-line description, e.g. 'json — JSON encoder and decoder'."""
    raw = soup.title.get_text(" ", strip=True) if soup.title else ""
    title = _TITLE_SUFFIX_RE.sub("", raw).strip()
    if not title and h1 is not None:
        # Fallback: the <h1> (minus its "¶" headerlink anchor text).
        clone = BeautifulSoup(str(h1), "lxml")
        for a in clone.select("a.headerlink"):
            a.decompose()
        title = clone.get_text(" ", strip=True)
    return title or "unknown module"


def _to_markdown(element: Any) -> str:
    """Convert a BeautifulSoup element to clean ATX markdown."""
    converter = MarkdownConverter(heading_style="ATX", bullets="-")
    md = converter.convert(str(element))
    # Collapse runs of blank lines left over from div/dl boundaries.
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


def parse_python_module_html(html: str, url: str) -> dict:
    """Parse a Sphinx module page into ``{"ok": True, "title", "markdown"}``.

    ``url`` is only used for context; the function never raises. On failure it
    returns ``{"ok": False, "error": "<short message>"}`` — including for the
    plain "Page not found" body that docs.python.org serves for unknown modules.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:  # pragma: no cover - lxml is very forgiving
        return {"ok": False, "error": f"failed to parse HTML: {exc}"}

    main = soup.find("div", role="main")
    if main is None:
        return {"ok": False, "error": "module not found (no main content on page)"}

    h1 = main.find("h1")
    if h1 is not None and "page not found" in h1.get_text(strip=True).lower():
        return {"ok": False, "error": "module not found"}

    title = _extract_title(soup, h1)

    # The whole page body lives in a single <section> inside role="main".
    section = main.find("section") or main

    # Strip navigation/anchor junk: per-heading "¶" links and clearfix divs.
    for junk in section.select("a.headerlink, div.clearer"):
        junk.decompose()

    markdown = _to_markdown(section)
    if not markdown.strip():
        return {"ok": False, "error": "module page had no extractable content"}

    return {"ok": True, "title": title, "markdown": markdown}


def fetch_python_module(module_name: str) -> dict:
    """Fetch a stdlib module's documentation page as clean markdown.

    ``module_name`` may be dotted (e.g. ``xml.etree.ElementTree``); the docs
    site only has lowercase filenames, so the lowercased name is tried FIRST
    and the name as given only as a fallback (one request saved per mixed-case
    lookup).

    Success: ``{"ok": True, "url", "title", "module", "markdown"}``
    Failure: ``{"ok": False, "error": "<short message>"}`` — never raises.
    """
    if not module_name or not module_name.strip():
        return {"ok": False, "error": "module name is empty"}
    module_name = module_name.strip()

    try:
        for url in _module_url_candidates(module_name):
            response = _http_get(url)
            if response.status_code == 404:
                continue  # try the next candidate (lowercase first, then as given)
            if response.error:
                return {"ok": False, "error": _layer_error_text(response, "docs.python.org")}
            parsed = parse_python_module_html(response.text, response.url or url)
            if not parsed.get("ok"):
                return {"ok": False, "error": parsed.get("error", "failed to parse module page")}
            return {
                "ok": True,
                "url": response.url or url,
                "title": parsed["title"],
                "module": module_name,
                "markdown": parsed["markdown"],
            }
        return {
            "ok": False,
            "error": f"module '{module_name}' not found in the Python 3 documentation",
        }
    except Exception as exc:  # absolute last resort — never raise out of here
        return {"ok": False, "error": f"unexpected error fetching module docs: {exc}"}


# ---------------------------------------------------------------------------
# PyPI parsing — project page HTML (default) and JSON API (opt-in only)
# ---------------------------------------------------------------------------

_RST_HEADING_LEVELS = {"=": "#", "-": "##", "^": "###", "~": "###", "*": "####", "#": "#####"}


def _rst_title_marker(line: str) -> str | None:
    """Return the marker char if ``line`` is an RST title underline/overline."""
    s = line.strip()
    if len(s) >= 3 and s[0] in "=~-^*#" and set(s) == {s[0]}:
        return s[0]
    return None


def _rst_to_markdown(text: str) -> str:
    """A deliberately small RST→Markdown pass.

    Handles what real-world PyPI READMEs mostly contain: section titles
    (underline and overline styles), bullet lists, ``inline code``, roles such
    as :func:`x`, and indented literal blocks (become fenced code). Everything
    else is passed through — good enough for agents to read, not a full RST
    parser.

    Only the opt-in JSON path needs it: the project page ships the README
    already rendered to HTML, which ``markdownify`` converts directly.
    """
    lines = text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Overline + title + underline (document title).
        marker = _rst_title_marker(line)
        if (
            marker is not None
            and i + 2 < n
            and lines[i + 1].strip()
            and _rst_title_marker(lines[i + 2]) == marker
        ):
            out.append(f"{_RST_HEADING_LEVELS[marker]} {lines[i + 1].strip()}")
            i += 3
            continue

        # Text line followed by an underline of the same-or-greater length.
        if (
            stripped
            and _rst_title_marker(line) is None
            and i + 1 < n
            and (m := _rst_title_marker(lines[i + 1])) is not None
            and len(lines[i + 1].strip()) >= max(3, len(stripped))
        ):
            out.append(f"{_RST_HEADING_LEVELS[m]} {stripped}")
            i += 2
            continue

        # Blank line followed by indented lines -> literal (code) block.
        if not stripped and i + 1 < n and lines[i + 1][:1] in (" ", "\t"):
            block: list[str] = []
            j = i + 1
            while j < n and (not lines[j].strip() or lines[j][:1] in (" ", "\t")):
                block.append(lines[j])
                j += 1
            indents = [len(l) - len(l.lstrip()) for l in block if l.strip()]
            pad = min(indents) if indents else 4
            code = "\n".join((l[pad:] if len(l) >= pad else "").rstrip() for l in block).rstrip("\n")
            out.append(f"```\n{code}\n```")
            i = j
            continue

        # Bullet lists: "- ", "* ", "+ " items.
        bullet = re.match(r"^(\s*)[-+*]\s+(.*)$", line)
        if bullet and _rst_title_marker(line) is None:
            out.append(f"{bullet.group(1)}- {bullet.group(2)}")
            i += 1
            continue

        # Inline RST -> Markdown: :role:`target` -> `target`, ``code`` -> `code`.
        converted = re.sub(r":[a-zA-Z0-9_]+:`([^`]*)`", r"`\1`", line)
        converted = re.sub(r"``(.*?)``", r"`\1`", converted)
        out.append(converted)
        i += 1

    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def _rst_like(content_type: str | None) -> bool:
    ct = (content_type or "").lower()
    return "rst" in ct or "restructuredtext" in ct


def _first_project_url(project_urls: dict[str, Any], *preferred: str) -> str | None:
    """Case-insensitive lookup of a project URL by label."""
    lowered = {str(k).strip().lower(): v for k, v in project_urls.items()}
    for label in preferred:
        value = lowered.get(label.lower())
        if value:
            return str(value)
    return None


def parse_pypi_json(data: Any) -> dict:
    """Extract the success payload from a PyPI JSON API response.

    Returns ``{"ok": True, "name", "version", "summary", "requires_python",
    "license", "homepage", "project_urls", "readme_markdown"}`` (no ``url`` —
    the caller adds that) or ``{"ok": False, "error": ...}`` on malformed
    input. Never raises.

    Reached only by the **opt-in** JSON path (``PYTHON_DOCS_MCP_PYPI_JSON_OPT_IN``)
    — the endpoint itself is disallowed by PyPI's robots.txt.  The payload shape
    is deliberately identical to :func:`parse_pypi_project_html`, so the two
    sources stay interchangeable for callers.

    Notes:
    - The README lives in ``info.description`` (the top-level ``description``
      key is empty); RST content is converted with a small pass to markdown.
    - ``license`` falls back to ``license_expression`` when the legacy
      ``license`` field is absent (common for modern packages).
    """
    try:
        if not isinstance(data, dict):
            return {"ok": False, "error": "malformed PyPI response: expected a JSON object"}
        info = data.get("info")
        if not isinstance(info, dict):
            return {"ok": False, "error": "malformed PyPI response: missing 'info' object"}

        name = info.get("name")
        version = info.get("version")
        if not isinstance(name, str) or not name.strip():
            return {"ok": False, "error": "malformed PyPI response: missing package name"}
        if not isinstance(version, str) or not version.strip():
            return {"ok": False, "error": "malformed PyPI response: missing package version"}

        project_urls = info.get("project_urls")
        if not isinstance(project_urls, dict):
            project_urls = None
        elif not project_urls:
            project_urls = None

        homepage = _first_project_url(project_urls or {}, "Homepage", "Source")
        if homepage is None and info.get("home_page"):
            homepage = str(info["home_page"])

        description = info.get("description") or ""
        if _rst_like(info.get("description_content_type")):
            readme_markdown = _rst_to_markdown(description)
        else:
            readme_markdown = description.strip() + ("\n" if description.strip() else "")

        license_value = info.get("license") or info.get("license_expression")

        return {
            "ok": True,
            "name": name,
            "version": version,
            "summary": info.get("summary") or "",
            "requires_python": info.get("requires_python"),
            "license": license_value,
            "homepage": homepage,
            "project_urls": project_urls,
            "readme_markdown": readme_markdown,
        }
    except Exception as exc:  # never raise out of the parser
        return {"ok": False, "error": f"failed to parse PyPI data: {exc}"}


# -- project page (the default, robots-allowed source) ----------------------

#: ``h1.project-header__name`` is "<name> <version>" (e.g. "attrs 26.1.0").
#: Neither a PyPI name nor a version contains whitespace.
_PACKAGE_HEADER_RE = re.compile(r"^(?P<name>\S+)(?:\s+(?P<version>\S+))?$")
#: ``#pip-command`` is "pip install <name>" or "pip install <name>==<version>".
_PIP_COMMAND_RE = re.compile(r"pip install (?P<name>\S+)(?:==(?P<version>\S+))?", re.IGNORECASE)


def _looks_like_bot_challenge(soup: BeautifulSoup, html: str) -> bool:
    """True for PyPI's JS "Client Challenge" page (HTTP 200, ~3 KB).

    Observed live on 2026-10-06: PyPI edge cache MISSes for this client came
    back as ``<title>Client Challenge</title>`` with ``/_fs-ch-…`` asset links
    and no package header, while cache HITs served the real page.  It is not an
    HTTP error, so it has to be recognised by shape — and never answered with a
    silent retry on the disallowed JSON endpoint.
    """
    if soup.select_one(".project-header__name, .package-header__name"):
        return False
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    return "client challenge" in title or "_fs-ch-" in html[:4000]


def _sidebar_sections(soup: BeautifulSoup) -> dict[str, Any]:
    """Sidebar blocks by title, first occurrence only.

    The page renders the sidebar twice (two column layouts); keeping the first
    occurrence per title makes the extraction order-independent and stops
    "Project links" from being read twice.
    """
    sections: dict[str, Any] = {}
    for sec in soup.select(".sidebar-section"):
        title_el = sec.find(["h2", "h3"])
        if title_el is None:
            continue
        title = title_el.get_text(" ", strip=True).strip()
        if title and title not in sections:
            sections[title] = sec
    return sections


def _sidebar_text(sec: Any) -> str:
    """Visible text of a sidebar block, minus its heading and any links."""
    clone = BeautifulSoup(str(sec), "lxml")
    heading = clone.find(["h2", "h3"])
    if heading is not None:
        heading.decompose()
    for a in clone.find_all("a"):
        a.decompose()
    return clone.get_text(" ", strip=True)


def _project_urls_from(sec: Any) -> dict[str, str]:
    """``{label: href}`` from a "Project links" sidebar block (deduplicated)."""
    urls: dict[str, str] = {}
    for anchor in sec.select("a.sidebar-links__link"):
        href = str(anchor.get("href") or "").strip()
        if not href:
            continue
        li = anchor.find_parent("li")
        label = (li.get_text(" ", strip=True) if li else "").strip()
        if not label:
            label = anchor.get_text(" ", strip=True).strip() or "Link"
        if label not in urls:
            urls[label] = href
    return urls


def _requires_python_from(sec: Any) -> str | None:
    """Specifier from the "Requires" block: ``<p><strong>Python</strong> >=3.9</p>``."""
    for para in sec.find_all("p"):
        strong = para.find("strong")
        if strong is None or strong.get_text(" ", strip=True).lower() != "python":
            continue
        head = strong.get_text(" ", strip=True)
        rest = para.get_text(" ", strip=True)
        if rest.lower().startswith(head.lower()):
            rest = rest[len(head):]
        return rest.strip() or None
    text = _sidebar_text(sec)
    if not text:
        return None
    return text.split(":", 1)[1].strip() if ":" in text else text


def parse_pypi_project_html(html: str, url: str = "") -> dict:
    """Parse a PyPI **project page** into the same payload as :func:`parse_pypi_json`.

    Returns ``{"ok": True, "name", "version", "summary", "requires_python",
    "license", "homepage", "project_urls", "readme_markdown"}`` (no ``url`` —
    :func:`fetch_pypi_package` adds that) or ``{"ok": False, "error": ...}``.
    Never raises.

    What is read (markup as served in 2026; the pre-2026 class names are kept
    as fallbacks):

    - ``div.project-header`` → ``h1.project-header__name`` = "attrs 26.1.0" and
      ``p.project-header__summary``; ``#pip-command`` ("pip install
      six==1.17.0") is the cross-check that also pins the version.
    - sidebar ``Project links`` → ``project_urls`` (labels as PyPI shows them),
      ``License expression`` / ``License`` → ``license``,
      ``Requires`` → ``requires_python``.
    - ``div.project-description`` → the README, already rendered to HTML by
      PyPI, converted to markdown with ``markdownify`` (no RST pass needed).
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:  # pragma: no cover - lxml is very forgiving
        return {"ok": False, "error": f"failed to parse HTML: {exc}"}

    try:
        if _looks_like_bot_challenge(soup, html):
            return {"ok": False, "error": BOT_CHALLENGE_ERROR, "bot_challenge": True}

        header = soup.select_one(".project-header__name") or soup.select_one(
            ".package-header__name"
        )
        if header is None:
            return {"ok": False, "error": "not a PyPI project page (no package header on page)"}

        name: str | None = None
        version: str | None = None
        match = _PACKAGE_HEADER_RE.match(header.get_text(" ", strip=True))
        if match:
            name = match.group("name")
            version = match.group("version")

        pip = soup.select_one("#pip-command")
        if pip is not None:
            pip_match = _PIP_COMMAND_RE.search(pip.get_text(" ", strip=True))
            if pip_match:
                name = name or pip_match.group("name")
                # A pinned page states the pin explicitly; trust it over the h1.
                if pip_match.group("version"):
                    version = pip_match.group("version")

        if not name:
            return {"ok": False, "error": "not a PyPI project page (no package name)"}
        if not version:
            return {"ok": False, "error": "PyPI project page had no readable version"}

        summary_el = soup.select_one(".project-header__summary") or soup.select_one(
            ".package-header__num"
        )
        summary = summary_el.get_text(" ", strip=True) if summary_el else ""
        if not summary:
            meta = soup.find("meta", attrs={"name": "description"})
            summary = str(meta.get("content") or "").strip() if meta else ""

        sections = _sidebar_sections(soup)

        license_value: str | None = None
        for title in ("License expression", "License", "Licenses"):
            sec = sections.get(title)
            if sec is not None:
                value = _sidebar_text(sec)
                if value:
                    license_value = value
                    break

        requires_python: str | None = None
        for title in ("Requires-Python", "Requires"):
            sec = sections.get(title)
            if sec is not None:
                value = _requires_python_from(sec)
                if value:
                    requires_python = value
                    break

        project_urls: dict[str, str] | None = None
        links_sec = sections.get("Project links")
        if links_sec is not None:
            found = _project_urls_from(links_sec)
            if found:
                project_urls = found

        homepage = _first_project_url(project_urls or {}, "Homepage", "Source")
        if homepage is None and project_urls:
            homepage = next(iter(project_urls.values()))

        readme_el = soup.select_one(".project-description__content") or soup.select_one(
            ".project-description"
        )
        readme_markdown = _to_markdown(readme_el) if readme_el is not None else ""

        return {
            "ok": True,
            "name": name,
            "version": version,
            "summary": summary or "",
            "requires_python": requires_python,
            "license": license_value,
            "homepage": homepage,
            "project_urls": project_urls,
            "readme_markdown": readme_markdown,
        }
    except Exception as exc:  # never raise out of the parser
        return {"ok": False, "error": f"failed to parse PyPI project page: {exc}"}


def pypi_project_url(name: str, version: str | None = None) -> str:
    """Robots-ALLOWED project page URL for ``name`` (optionally pinned)."""
    if version:
        return f"{PYPI_PROJECT_BASE_URL}{name}/{version}/"
    return f"{PYPI_PROJECT_BASE_URL}{name}/"


def pypi_json_url(name: str, version: str | None = None) -> str:
    """Robots-DISALLOWED JSON API URL — reachable only via the explicit opt-in."""
    if version:
        return f"{PYPI_JSON_BASE_URL}/{name}/{version}/json"
    return f"{PYPI_JSON_BASE_URL}/{name}/json"


def pypi_json_opt_in_enabled() -> bool:
    """True only when ``PYTHON_DOCS_MCP_PYPI_JSON_OPT_IN`` is 1/true/yes/on."""
    return (os.environ.get(PYPI_JSON_OPT_IN_ENV_VAR) or "").strip().lower() in _TRUE_VALUES


def _pypi_not_found() -> dict:
    return {
        "ok": False,
        "error": "package not found on PyPI",
        "suggestion": "check the name (case-sensitive) or drop the version pin",
    }


def _fetch_pypi_via_project_page(name: str, version: str | None) -> dict:
    """Default path: the robots-allowed HTML project page."""
    url = pypi_project_url(name, version)
    response = _http_get(url)
    if response.status_code == 404:
        return _pypi_not_found()
    if response.bot_challenge:
        # A8 F4: the layer already waited (escalated per-host delay) and
        # retried **this same URL** once, and the answer is still a challenge.
        # The body is not parsed and nothing else is requested — in particular
        # the robots-disallowed ``/pypi/<name>/json`` is never used as a
        # fallback, and the message says what is actually unknown.
        return {
            "ok": False,
            "error": BOT_CHALLENGE_ERROR,
            "suggestion": BOT_CHALLENGE_SUGGESTION,
            "bot_challenge": True,
        }
    if response.error:
        out: dict = {"ok": False, "error": _layer_error_text(response, "pypi.org")}
        if response.blocked_by_robots:
            out["suggestion"] = (
                "PyPI's robots.txt disallows this path; use the project page URL, "
                f"or {PYPI_JSON_OPT_IN_ENV_VAR}=1 at your own risk"
            )
        return out

    parsed = parse_pypi_project_html(response.text, url)
    if not parsed.get("ok"):
        error: dict = {"ok": False, "error": parsed.get("error", "failed to parse project page")}
        if parsed.get("bot_challenge"):
            error["suggestion"] = BOT_CHALLENGE_SUGGESTION
            error["bot_challenge"] = True
        else:
            error["suggestion"] = (
                "the PyPI page layout may have changed; retry, or use the "
                f"{PYPI_JSON_OPT_IN_ENV_VAR}=1 opt-in at your own risk"
            )
        return error

    parsed["url"] = f"{PYPI_PROJECT_BASE_URL}{parsed['name']}/{parsed['version']}/"
    return parsed


def _fetch_pypi_via_json_opt_in(name: str, version: str | None) -> dict:
    """Opt-in path: the robots-DISALLOWED JSON API, on the user's explicit request.

    This is the ONLY place in the package that may touch ``/pypi/.../json``, and
    it is reached only when :func:`pypi_json_opt_in_enabled` is true.  It
    deliberately bypasses the politeness layer's robots decision — the layer
    blocks this URL, and ``tests/test_politeness_wiring.py`` asserts both facts.
    Every result carries :data:`PYPI_JSON_OPT_IN_NOTICE` so the bypass is
    visible to whoever reads the tool output.
    """
    url = pypi_json_url(name, version)
    try:
        with _client() as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "error": f"transport error fetching PyPI: {type(exc).__name__}: {exc}",
            "notice": PYPI_JSON_OPT_IN_NOTICE,
        }

    if resp.status_code == 404:
        out = _pypi_not_found()
        out["notice"] = PYPI_JSON_OPT_IN_NOTICE
        return out
    if resp.status_code >= 400:
        return {
            "ok": False,
            "error": f"pypi.org returned HTTP {resp.status_code}",
            "notice": PYPI_JSON_OPT_IN_NOTICE,
        }
    try:
        data = resp.json()
    except ValueError:
        return {
            "ok": False,
            "error": "pypi.org returned invalid JSON",
            "notice": PYPI_JSON_OPT_IN_NOTICE,
        }

    parsed = parse_pypi_json(data)
    if not parsed.get("ok"):
        parsed["notice"] = PYPI_JSON_OPT_IN_NOTICE
        return parsed
    parsed["url"] = f"{PYPI_PROJECT_BASE_URL}{parsed['name']}/{parsed['version']}/"
    parsed["notice"] = PYPI_JSON_OPT_IN_NOTICE
    return parsed


def fetch_pypi_package(name: str, version: str | None = None) -> dict:
    """Fetch metadata + README for a PyPI package (optionally version-pinned).

    Default source: the robots-allowed **project page**
    ``https://pypi.org/project/{name}/[{version}/]``.  The JSON API is used only
    when ``PYTHON_DOCS_MCP_PYPI_JSON_OPT_IN`` is truthy, and then the result
    carries a ``"notice"`` explaining that PyPI's robots.txt disallows it.  The
    two sources never fall back into one another: with the opt-in off the JSON
    endpoint is never requested, with it on the project page never is.

    Success: ``{"ok": True, "name", "version" (resolved), "summary",
    "requires_python", "license", "homepage", "project_urls",
    "readme_markdown", "url"}`` where ``url`` is the human-facing project page
    (plus ``"notice"`` in opt-in mode).
    Not found / unknown version: ``{"ok": False, "error": "package not found on
    PyPI", "suggestion": ...}``. Never raises.
    """
    if not name or not name.strip():
        return {"ok": False, "error": "package name is empty"}
    name = name.strip()

    try:
        if pypi_json_opt_in_enabled():
            return _fetch_pypi_via_json_opt_in(name, version)
        return _fetch_pypi_via_project_page(name, version)
    except Exception as exc:  # absolute last resort — never raise out of here
        return {"ok": False, "error": f"unexpected error fetching PyPI data: {exc}"}
