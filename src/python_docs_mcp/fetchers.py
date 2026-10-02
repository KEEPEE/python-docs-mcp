"""Fetchers for CPython stdlib documentation and PyPI package info.

Two public entry points (later phases build MCP tools on top of these):

- :func:`fetch_python_module` — fetches a module page from
  https://docs.python.org/3/library/{module}.html and converts the Sphinx
  HTML to clean markdown.
- :func:`fetch_pypi_package` — fetches https://pypi.org/pypi/{name}/json
  (optionally pinned to a version) and extracts metadata + README.

Both functions are total: they never raise, they return ``{"ok": False,
"error": ...}`` dicts on failure.

The HTTP layer is deliberately thin so that the parsing logic lives in pure
functions (:func:`parse_python_module_html`, :func:`parse_pypi_json`) that
the offline test-suite can exercise against downloaded fixtures.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

__all__ = [
    "fetch_python_module",
    "fetch_pypi_package",
    "parse_python_module_html",
    "parse_pypi_json",
    "_module_url_candidates",
]

DOCS_BASE_URL = "https://docs.python.org/3/library/"
PYPI_JSON_BASE_URL = "https://pypi.org/pypi"
REQUEST_TIMEOUT = 20.0
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 python-docs-mcp/0.1.0"
)

# Trailing "— Python 3.14.8 documentation" (or unversioned variant) appended to
# every docs.python.org <title>.
_TITLE_SUFFIX_RE = re.compile(r"\s*—\s*Python\s+[\d.]*\s*documentation\s*$", re.IGNORECASE)


def _client() -> httpx.Client:
    """A browser-ish client: 20 s timeout, redirects followed."""
    return httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _fetch_url(client: httpx.Client, url: str) -> tuple[httpx.Response | None, str | None]:
    """GET ``url`` with one retry on transport errors.

    Returns ``(response, None)`` on success, or ``(None, error_message)`` when
    both attempts fail at the transport level (timeout, connection reset, ...).
    HTTP error statuses (404/500/...) are returned as normal responses — the
    caller decides how to interpret them.
    """
    last_error = "unknown transport error"
    for _attempt in range(2):  # initial attempt + one retry
        try:
            return client.get(url), None
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    return None, last_error


def _module_url_candidates(module_name: str) -> list[str]:
    """URLs to try for ``module_name``, in order.

    docs.python.org serves module pages only under LOWERCASE filenames, so a
    dotted name with mixed case (e.g. ``xml.etree.ElementTree``) needs a
    lowercased fallback: the first candidate is the name as given, the second
    (only when different) is the fully lowercased variant.
    """
    names = [module_name]
    if module_name.lower() != module_name:
        names.append(module_name.lower())
    return [f"{DOCS_BASE_URL}{name}.html" for name in names]


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
    site only has lowercase filenames, so on 404 we retry with the lowercased
    name before giving up.

    Success: ``{"ok": True, "url", "title", "module", "markdown"}``
    Failure: ``{"ok": False, "error": "<short message>"}`` — never raises.
    """
    if not module_name or not module_name.strip():
        return {"ok": False, "error": "module name is empty"}
    module_name = module_name.strip()

    try:
        with _client() as client:
            for url in _module_url_candidates(module_name):
                response, transport_error = _fetch_url(client, url)
                if response is None:
                    return {
                        "ok": False,
                        "error": f"transport error fetching docs: {transport_error}",
                    }
                if response.status_code == 404:
                    continue  # try the next candidate (lowercase fallback)
                if response.status_code != 200:
                    return {
                        "ok": False,
                        "error": f"docs.python.org returned HTTP {response.status_code}",
                    }
                parsed = parse_python_module_html(response.text, url)
                if not parsed.get("ok"):
                    return {"ok": False, "error": parsed.get("error", "failed to parse module page")}
                return {
                    "ok": True,
                    "url": url,
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
# PyPI parsing
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
    :func:`fetch_pypi_package` adds that) or ``{"ok": False, "error": ...}`` on
    malformed input. Never raises.

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


def fetch_pypi_package(name: str, version: str | None = None) -> dict:
    """Fetch metadata + README for a PyPI package (optionally version-pinned).

    Success: ``{"ok": True, "name", "version" (resolved), "summary",
    "requires_python", "license", "homepage", "project_urls",
    "readme_markdown", "url"}`` where ``url`` is the human-facing project page.
    Not found / unknown version: ``{"ok": False, "error": "package not found on
    PyPI", "suggestion": ...}``. Never raises.
    """
    if not name or not name.strip():
        return {"ok": False, "error": "package name is empty"}
    name = name.strip()

    url = f"{PYPI_JSON_BASE_URL}/{name}/json"
    if version:
        url = f"{PYPI_JSON_BASE_URL}/{name}/{version}/json"

    try:
        with _client() as client:
            response, transport_error = _fetch_url(client, url)
            if response is None:
                return {
                    "ok": False,
                    "error": f"transport error fetching PyPI: {transport_error}",
                }
            if response.status_code == 404:
                return {
                    "ok": False,
                    "error": "package not found on PyPI",
                    "suggestion": "check the name (case-sensitive) or drop the version pin",
                }
            if response.status_code != 200:
                return {"ok": False, "error": f"pypi.org returned HTTP {response.status_code}"}
            try:
                data = response.json()
            except ValueError:
                return {"ok": False, "error": "pypi.org returned invalid JSON"}

            parsed = parse_pypi_json(data)
            if not parsed.get("ok"):
                return parsed
            parsed["url"] = f"https://pypi.org/project/{name}/{parsed['version']}/"
            return parsed
    except Exception as exc:  # absolute last resort — never raise out of here
        return {"ok": False, "error": f"unexpected error fetching PyPI data: {exc}"}
