# python-docs-mcp

An [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that gives AI coding agents **live CPython standard-library documentation and PyPI package information** instead of whatever the model happens to remember. Module pages are scraped from docs.python.org at request time, cached locally, and returned as clean markdown — so generated Python targets real, current APIs rather than deprecated or invented ones.

- **Stdlib modules** — every module page fetched live from [docs.python.org/3/library](https://docs.python.org/3/library/) and converted to markdown (the `/3/` line is whatever CPython currently documents — measured `3.14.8`)
- **PyPI packages** — metadata + README read from the package's robots-allowed [project page](https://pypi.org/project/<name>/), optionally pinned to a version (see [PyPI source](#pypi-source-and-robotstxt))
- **Local search index** over all 229 stdlib modules (name + description recorded, so `ElementTree` resolves to `xml.etree.elementtree` on its own), rebuilt at most every 7 days with a stale fallback when the network fails
- **SQLite TTL cache** so repeated lookups are instant
- **Politeness layer** — robots.txt (RFC 9309), per-host throttle, `Retry-After`, conditional GET and request budgets ([details](#caching--politeness))

## The five tools

| Tool | What it does |
|---|---|
| `python_docs` | Resolve one identifier (`json`, `xml.etree.ElementTree`, `ElementTree`) to a single stdlib module page as markdown. |
| `python_search` | Ranked search over the local index of all 229 stdlib modules (name **and** description). |
| `pypi_package` | PyPI package metadata + README: resolved version, summary, `Requires-Python`, license, homepage, project URLs. |
| `python_status` | Real health check: index size/age, cache stats, live probes of docs.python.org and pypi.org, and politeness counters. |
| `health_check` | Server liveness and version. |

Every tool returns a plain dict. Failures come back as `{"ok": false, "error": …, "suggestion": …}` — a bad lookup never raises into the MCP layer, and no tool delegates to another tool.

## Requirements

- Python 3.10 or newer (developed and tested on 3.12)
- [`uv`](https://docs.astral.sh/uv/) for the one-command install below (`uvx` ships with it)
- The MCP Python SDK **1.x** is pinned (`mcp>=1.2.0,<2.0`); this package does not support the mcp 2.x rename of `FastMCP`

## Install & run

### One command — no checkout, no token

```bash
uvx --from git+https://github.com/KEEPEE/python-docs-mcp.git python-docs
```

This builds the package in an isolated environment and starts the stdio MCP server. It prints nothing on purpose: stdout is the protocol channel. Stop it with `Ctrl-C`.

After the repository is updated, force `uv` to re-resolve the commit:

```bash
uvx --refresh --from git+https://github.com/KEEPEE/python-docs-mcp.git python-docs
```

### Local checkout — fastest startup, editable while developing

```bash
git clone https://github.com/KEEPEE/python-docs-mcp.git
cd python-docs-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python-docs        # stdio MCP server
```

## MCP client configuration

### Generic stdio client (Claude Desktop, Cursor, Cline, …)

```json
{
  "mcpServers": {
    "python-docs": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/KEEPEE/python-docs-mcp.git",
        "python-docs"
      ]
    }
  }
}
```

With an explicit cache directory (any `env` you set is passed straight through to the server):

```json
{
  "mcpServers": {
    "python-docs": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/KEEPEE/python-docs-mcp.git",
        "python-docs"
      ],
      "env": {
        "PYTHON_DOCS_MCP_CACHE_DIR": "/tmp/python-docs-cache"
      }
    }
  }
}
```

### DeepSeek Harness (`cordis`-style plugin list)

```yaml
- id: mcp-python-docs
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: python-docs
    transport: stdio
    command: uvx
    args:
      [
        '--from',
        'git+https://github.com/KEEPEE/python-docs-mcp.git',
        'python-docs'
      ]
```

To skip the build at client start, point `command` at the console script of a checkout instead — `command: /path/to/python-docs-mcp/.venv/bin/python-docs` with `args: []`.

## Tools reference

### `python_docs(identifier, topic=None, max_tokens=8000)`

Resolves an identifier to one module page. Identifier forms, tried in this order:

| Form | Meaning |
|---|---|
| `xml.etree.ElementTree` (any dotted name) | fetched directly; the docs site only has **lowercase** filenames, so the lowercased name is tried first and the spelling you gave only as a fallback |
| `json`, `zipfile` (plain lowercase path) | fetched directly |
| `ElementTree` (any other plain name) | resolved through the local index: the unique entry whose full name or last dotted segment matches case-insensitively is fetched; ambiguous or missing matches return an error dict pointing at `python_search` |

`topic` keeps only the section whose heading matches it (`Basic Usage`, `Command-line interface`, …) plus the page title and description; if nothing matches, the full page is returned with a `note` listing the available headings. `max_tokens` is a rough budget (1 token ≈ 4 characters): the markdown is cut at a line boundary and `truncated` is set.

Returns `{"ok", "identifier", "url", "title", "markdown", "truncated", "cached"}`, plus an optional `note`.

```jsonc
// python_docs({ "identifier": "xml.etree.ElementTree", "max_tokens": 250 })
{
  "ok": true,
  "identifier": "xml.etree.ElementTree",
  "url": "https://docs.python.org/3/library/xml.etree.elementtree.html",
  "markdown": "# `xml.etree.ElementTree` — The ElementTree XML API  **Source code:** [Lib/xml/etree/ElementTree.py](https://github.com/python/cpython/tree/3.14/Lib/xml/etree/ElementTree.py)  ---  The `xml.etree.ElementTree` module implements a simple and efficient API for pa … [truncated]",
  "truncated": true,
  "cached": false,
  "title": "xml.etree.ElementTree — The ElementTree XML API"
}
```

A bare name is resolved through the index — same page, no guessing:

```jsonc
// python_docs({ "identifier": "ElementTree", "max_tokens": 160 })
{
  "ok": true,
  "identifier": "ElementTree",
  "url": "https://docs.python.org/3/library/xml.etree.elementtree.html",
  "title": "xml.etree.ElementTree — The ElementTree XML API",
  "…": "…"
}
```

A `topic` that matches nothing tells you what the page does have:

```jsonc
// python_docs({ "identifier": "json", "topic": "nonexistent-section", "max_tokens": 120 })
{
  "ok": true,
  "identifier": "json",
  "url": "https://docs.python.org/3/library/json.html",
  "title": "json — JSON encoder and decoder",
  "…": "…",
  "note": "no section matching 'nonexistent-section'; available headings: `json` — JSON encoder and decoder, Basic Usage, Encoders and Decoders, Let the base class default method raise the TypeError, Exceptions, Standard Compliance and Interoperability, Character Encodings, Infinite and NaN Number Values, Repeated Names Within an Object, Top-level Non-Object, Non-Array Values, Implementation Limitations, Command-line interface, Command-line options"
}
```

A name that is not a module is an error dict, not an empty page:

```jsonc
// python_docs({ "identifier": "nope-not-a-real-module-xyz" })
{
  "ok": false,
  "error": "no stdlib module matching 'nope-not-a-real-module-xyz' found in the search index",
  "suggestion": "use python_search('nope-not-a-real-module-xyz') to find the right module name"
}
```

### `python_search(query, limit=8)`

Multi-token ranked search over the local index: **every** token must match, and both the module name and its one-line description are searched. Returns `{"ok", "query", "results": [{module, description, score}], "index_count", "stale"}`.

```jsonc
// python_search({ "query": "zip file", "limit": 3 })
{
  "ok": true,
  "query": "zip file",
  "results": [
    { "module": "zipfile", "description": "Work with ZIP archives", "score": 14.0 },
    { "module": "gzip",    "description": "Support for gzip files", "score": 9.0  }
  ],
  "index_count": 229,
  "stale": false
}
```

Description matching is what makes this useful when you know the job, not the name:

```jsonc
// python_search({ "query": "element tree", "limit": 3 })
{ "results": [ { "module": "xml.etree.elementtree", "description": "The ElementTree XML API", "score": 12.0 } ], "…": "…" }

// python_search({ "query": "tokeniz", "limit": 3 })   // prefix match
{ "results": [ { "module": "tokenize", "description": "Tokenizer for Python source", "score": 8.0 } ], "…": "…" }
```

`python_search` costs no requests while the cached index is fresh; if the index is stale the call rebuilds it (one robots fetch + one library-index page) inside the same budget.

### `pypi_package(name, version=None)`

PyPI metadata + README. `name` is the distribution name; `version` pins one release, which is how you read the README a specific version shipped. Returns `{"ok", "name", "version" (resolved), "summary", "requires_python", "license", "homepage", "project_urls", "readme_markdown", "url"}` — `url` is always the human-facing project page. Cached 1 day, keyed by name + version.

```jsonc
// pypi_package({ "name": "attrs" })
{
  "ok": true,
  "name": "attrs",
  "version": "26.1.0",
  "summary": "Classes Without Boilerplate",
  "requires_python": ">=3.9",
  "license": "MIT",
  "homepage": "https://github.com/python-attrs/attrs",
  "project_urls": {
    "GitHub": "https://github.com/python-attrs/attrs",
    "Changelog": "https://www.attrs.org/en/stable/changelog.html",
    "Documentation": "https://www.attrs.org/",
    "Funding": "https://github.com/sponsors/hynek",
    "Tidelift": "https://tidelift.com/subscription/pkg/pypi-attrs?utm_source=pypi-attrs&utm_medium=pypi"
  },
  "readme_markdown": "[![attrs](https://pypi-camo.freetls.fastly.net/5bfa3846…)] …",
  "url": "https://pypi.org/project/attrs/26.1.0/",
  "cached": false
}
```

A pinned version resolves to exactly that release:

```jsonc
// pypi_package({ "name": "six", "version": "1.16.0" })
{
  "ok": true,
  "name": "six",
  "version": "1.16.0",
  "summary": "Python 2 and 3 compatibility utilities",
  "requires_python": ">=2.7,  !=3.0.*,  !=3.1.*,  !=3.2.*",
  "license": "MIT License (MIT)",
  "homepage": "https://github.com/benjaminp/six",
  "url": "https://pypi.org/project/six/1.16.0/",
  "…": "…"
}
```

An unknown name is an error dict — and on PyPI the honest error message is two-sided, because PyPI does not answer unknown names with a plain 404 (see [the challenge note](#known-trade-off-the-js-client-challenge)):

```jsonc
// pypi_package({ "name": "nope-not-a-real-package-xyz" })
{
  "ok": false,
  "error": "pypi.org answered with a JavaScript bot-verification page (HTTP 200, not a 404) instead of the project page. Either PyPI is serving its anti-bot challenge for this URL, or the package does not exist — PyPI also answers unknown package names with that page, so a challenge is not proof the name is valid",
  "suggestion": "retry later (PyPI serves that page for some cold URLs), check the name (case-sensitive) or drop the version pin, or use the PYTHON_DOCS_MCP_PYPI_JSON_OPT_IN=1 opt-in knowing that PyPI's robots.txt disallows the JSON API"
}
```

### `python_status()`

Probes docs.python.org and pypi.org with a light GET (both on robots-**allowed** paths) and reports the index and cache state. `overall` is `ok`, `degraded` or `error`. The `politeness` block is diagnostics only and never changes `overall`.

```jsonc
// python_status()
{
  "server": "python-docs-mcp",
  "version": "0.2.0",
  "checks": {
    "search_index":    { "status": "ok", "entries": 229, "built_at": "2026-10-07T08:04:39.802131+00:00", "stale": false },
    "cache":           { "status": "ok", "entries": 11, "expired": 0 },
    "docs_python_org": { "status": "ok", "http_status": 200 },
    "pypi_org":        { "status": "ok", "http_status": 200 }
  },
  "politeness": {
    "status": "ok", "disabled": false,
    "requests": 10, "robots_requests": 2, "robots_rows": 2, "robots_fetches": 2, "robots_cache_hits": 7,
    "blocked_by_robots": 0, "throttle_waits": 8, "throttle_sleep_s": 3.626,
    "challenge_detected": 2, "challenge_retries": 1,
    "host_delays": { "docs.python.org": 0.0, "pypi.org": 0.913 },
    "budgets": { "tool:pypi_package": [2, 6], "tool:python_status": [2, 8] },
    "budget_denied": 0, "conditional": 1, "conditional_skipped": 0, "revalidated_304": 1,
    "retries_429": 0, "retries_transport": 0, "stalls": 0
  },
  "overall": "ok"
}
```

### `health_check()`

`{"status": "ok", "server": "python-docs-mcp", "version": "0.2.0"}` — no network, no cache; safe as a liveness probe.

## PyPI source (and robots.txt)

This is the one place where this server deliberately does **not** use the API you might expect, and it is worth understanding before you change it.

PyPI's `robots.txt` (325 bytes, 12 `Disallow` lines for `User-agent: *`) says:

```
Disallow: /simple/
Disallow: /packages/
Disallow: /_includes/authed/
Disallow: /project/*/submit-malware-report/
Disallow: /pypi/*/json
Disallow: /pypi/*/*/json
Disallow: /pypi*?
Disallow: /search*
Disallow: /_/
Disallow: /integrity/
Disallow: /account/
Disallow: /admin/
```

**`pypi_package()` reads its metadata from the robots-allowed project page**, `https://pypi.org/project/<name>/` (or `/project/<name>/<version>/`), because `Disallow: /pypi/*/json` and `Disallow: /pypi/*/*/json` forbid the JSON API this package used to call. `/simple/` is not a way around it — it is disallowed too, and it carries no README or project URLs. (Python's own `urllib.robotparser` did not catch the original violation: on Python < 3.14 it reads `/pypi/*/json` as a literal prefix and reports that URL as *allowed*. The RFC 9309 matcher in `politeness.py` reports it as blocked — verified live: `can_fetch("https://pypi.org/pypi/attrs/json")` → `False`, `can_fetch("https://pypi.org/project/attrs/")` → `True`.)

The page parser pulls the same fields out of the rendered HTML — name, resolved version, summary, `Requires-Python`, license (both the modern "License expression" block and the legacy "License" sidebar entry), homepage, project URLs and the README (RST → markdown) — and `pypi_package` returns **exactly the same keys as before**, so nothing downstream changed.

### The JSON API is opt-in only

```bash
export PYTHON_DOCS_MCP_PYPI_JSON_OPT_IN=1
```

With that set, `pypi_package` uses `https://pypi.org/pypi/<name>/json` again. That endpoint is disallowed by robots.txt, so using it is **your** responsibility, and every result produced that way carries a `notice` field saying so — verbatim:

```json
{
  "…": "…",
  "notice": "PyPI's robots.txt disallows /pypi/*/json and /pypi/*/*/json for every user-agent. The JSON API was used because PYTHON_DOCS_MCP_PYPI_JSON_OPT_IN=1 is set; that is a deliberate, user-authorised bypass of the politeness layer and it is the user's responsibility."
}
```

With the opt-in off (the default) the JSON endpoint is **never** requested — not even as a fallback when the project page fails, and the politeness layer blocks that URL independently, so the violation cannot come back through another door. With the opt-in on, the project page is never requested. The two sources never fall back into one another.

### Known trade-off: the JS "Client Challenge"

PyPI's CDN (Fastly) answers some project-page URLs with a ~3 KB JavaScript **Client Challenge** instead of the page — **HTTP 200, not a 404**. Measured on this repo's fixtures: the challenge page is 3,038 bytes, a real `attrs` project page is 130,507 bytes.

This is a real trade-off of reading the allowed HTML page instead of the JSON API, and it has a sharp edge you should know about:

> **A package that does not exist can look exactly like a bot challenge.** PyPI answers unknown package names with that same challenge page — reproduced live on 2026-10-07: `pypi.org/project/nope-not-a-real-package-xyz/` returned HTTP 200 with the challenge body. A challenge is therefore *not* proof the name is valid, and a 404 is not the only way a name can be wrong.

The tool reports both possibilities instead of guessing:

```jsonc
// the error text, verbatim
"pypi.org answered with a JavaScript bot-verification page (HTTP 200, not a 404) instead of the project page. Either PyPI is serving its anti-bot challenge for this URL, or the package does not exist — PyPI also answers unknown package names with that page, so a challenge is not proof the name is valid"
```

What the code does about it:

- the politeness layer's optional `challenge_detector` hook is wired to a PyPI-specific predicate (`fetchers.pypi_challenge_detector`): small body + HTML + a challenge marker (`_fs-ch-`, "Client Challenge") + **none** of the markers a real project page always carries (`project-header__name`, `package-header__name`, `pip-command`);
- when it fires, the layer waits (escalated per-host delay) and retries **that same URL once** — never another endpoint. Measured: `challenge_detected: 2`, `challenge_retries: 1`, and the whole lookup spends 3 of its 6 budget units;
- if the answer is still a challenge, the body is **never parsed into a fake package**, **never cached**, and **never answered with a fallback to the robots-disallowed JSON endpoint**. A genuine page is fetched exactly once — the detector does not add a request for it (covered by `tests/test_politeness_wiring.py`).

## Caching & politeness

### Cache

Everything lives in one directory: `~/.cache/python-docs-mcp` by default, overridable with `PYTHON_DOCS_MCP_CACHE_DIR`.

| File | Contents | TTL |
|---|---|---|
| `cache.db` | module pages (parsed result + raw body + `ETag` / `Last-Modified`), the search index, PyPI metadata | module docs 7 days, search index 7 days, PyPI metadata 1 day |
| `robots.db` | the politeness layer's robots.txt cache | 7 days per host |

Delete the files to force a full refresh. A cache problem is never a tool failure: if the directory is unwritable the server simply runs without a cache and says so in `python_status().checks.cache`.

### Politeness

Every outbound request — `python_docs`, `python_search` (its index build), `pypi_package` and the `python_status` probes — goes through one small internal module, [`src/python_docs_mcp/politeness.py`](src/python_docs_mcp/politeness.py) (stdlib + `httpx`, no extra dependency):

- **robots.txt is read and obeyed.** Rules are matched per RFC 9309 (`*`, trailing `$`, `%2A`/`%24` literals, merged `User-agent` groups, most-specific match wins, `Allow` beats `Disallow` on a tie). `urllib.robotparser` is deliberately not used: on Python < 3.14 it ignores wildcards, which is exactly how this server once violated PyPI (see [PyPI source](#pypi-source-and-robotstxt)). `docs.python.org` publishes 22 `Disallow` lines in a 517-byte file — `/dev`, `/release` and the EOL version lines (`/2/` … `/3.10/`) — and this server fetches `/3/library/…`; if an older URL ever reached the layer the answer is `{"ok": false, "error": "… blocked by robots.txt …"}` with **no request made** (verified live: `can_fetch("https://docs.python.org/3.10/library/json.html")` → `False`). Files are cached 7 days per host, including negative results (404 / 403 / 5xx), so a host costs one robots request per week.
- **Per-host throttle.** Sequential requests to one host are spaced 0.35–0.9 s by default, or by the site's own `Crawl-delay` when it declares one — neither `docs.python.org` nor `pypi.org` declares a `Crawl-delay` today (checked against both live robots.txt files), so the default window applies. An audit of this server measured a **7 ms** minimum gap between docs.python.org requests during a cold index build; with the layer the same build waits out the full delay (measured cold run: 2 requests on the wire, `throttle_sleep_s: 0.784`).
- **`429` / `503` / `504` are handled.** `Retry-After` is honoured to the second; without it the per-host delay is escalated and the request retried once. A response that stalls past 10 s escalates the delay instead of being retried blindly.
- **Conditional GET.** `ETag` / `Last-Modified` and the raw body are stored next to the cached entry, so refreshing an expired page costs a `304` instead of re-downloading it. Verified against live headers: docs.python.org module pages send **both** `ETag` and `Last-Modified`; `pypi.org` project pages send an `ETag` and no `Last-Modified`. A warm run revalidated one entry with a `304` (`revalidated_304: 1`).
- **Request budgets — one unit is one request on the wire.** One tool call makes at most **6** requests; the robots.txt fetch of a cold host, a `429` retry, a transport retry and every redirect hop each pay a unit. Measured live with a cold cache: `python_docs("ElementTree")` = **3** (robots + library-index page + module page), `python_docs("json")` = **2**, `python_docs("xml.etree.ElementTree")` = **2** (the lowercase page is tried first, so no wasted 404), `python_docs("xml.etree.NopeXYZ")` = **3** (robots + both candidate spellings), an unknown plain name = **2** (the index answers before any page is fetched), `pypi_package("attrs")` = **2**, a challenge-hit PyPI lookup = **3**. `python_status` has its own cap of **8** (measured cold **5**, warm **2**) so its probes plus a stale-index rebuild always fit — a probe refused by the budget would report `error` and drag `overall` to `"degraded"` for a purely internal reason. The search-index build shares the tool call's budget; a build that hits its cap stops early and returns a **partial** index (`"partial": true`) instead of raising, and a partial index is never cached.
- **Redirects are resolved by the layer, not by httpx.** Requests go out with `follow_redirects=False`, every 3xx target is re-checked against that host's robots rules before its body is used, the chain is capped at 5 hops and reported as an error (never an exception), and the `url` a tool returns is the **final** URL of the chain.
- **Host allowlist.** Only `docs.python.org` and `pypi.org` can be contacted, so a malformed identifier or an unexpected redirect cannot turn a docs lookup into a request somewhere else.
- **One transport, no `curl` fallback.** An earlier version shelled out to `curl` when `httpx` failed. That second transport is gone and stays gone: it bypassed robots.txt, the throttle, the budget and the stored validators, so a fallback request was an *unpolite* request; it doubled the worst case; and the premise it was written for — that some edges throttle Python's TLS fingerprint — did not reproduce under measurement.

The layer never raises and never changes a tool's return shape. `python_status` reports its counters under the top-level `politeness` key.

### Opt-out

```bash
export PYTHON_DOCS_MCP_POLITENESS_DISABLED=1
```

This single switch turns off robots.txt, the throttle, retries, conditional GET and the host allowlist at once. **Use it at your own risk:** you take over responsibility for respecting each site's crawling rules, and you are far more likely to be rate-limited or blocked. There is no partial opt-out.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

.venv/bin/python -m pytest -q              # 191 offline tests; fixtures in tests/fixtures/
.venv/bin/python scripts/politeness_smoke.py /tmp/python-docs-smoke-cache
.venv/bin/python scripts/e2e_mcp_test.py
```

- `pytest` is fully offline: HTTP is simulated with `httpx.MockTransport` and time/jitter are injected, so the suite is deterministic and runs in seconds. `tests/fixtures/` holds real captured pages: a docs.python.org module page (112 KB), the full library index (73 KB), two real PyPI project pages (130 KB and 89 KB), the JS challenge page PyPI's CDN returns for some URLs (3 KB) and a PyPI JSON-API payload (605 KB) used to test the opt-in parser.
- `scripts/politeness_smoke.py <cache-dir>` is a **live measurement**, not a test: it drives the real tools against the real docs sites and prints what the politeness layer did (requests, robots, throttle, conditional GET, challenge detection, budgets).
- `scripts/e2e_mcp_test.py` spawns the installed `python-docs` console script and speaks newline-delimited JSON-RPC to it (`initialize` → `tools/list` → every tool → a negative case → `health_check`). Exit code 0 means every check passed. It resolves the server command as `PYTHON_DOCS_MCP_E2E_CMD` (env override) → this checkout's `.venv/bin/python-docs` → `python -m python_docs_mcp.server`.

## Troubleshooting

**The first `python_docs` call is slower than the rest.** That is the cold search-index build: the library-index page plus a robots fetch, spaced by the per-host throttle, followed by the module page you actually asked for. It happens once every 7 days; later calls hit the cached index. Delete `cache.db` and you pay for it again.

**`request budget exhausted for scope 'tool:python_docs'`.** One tool call hit its cap of 6 requests. `python_search` first — it is offline and free — and pass the exact dotted module path. `python_status().politeness.budgets` shows how much each scope spent.

**`"partial": true`, or fewer search results than expected.** An index build ran out of its budget and stopped early, returning a partial index with a `partial_reason`. A partial index is **not cached**, so the next run rebuilds it. `python_search` still works — it just knows fewer modules — and `python_status().checks.search_index.entries` tells you how many it has (a complete index is 229 modules).

**`"stale": true` in search results.** The rebuild failed (offline, DNS failure, 5xx) and the server fell back to the older cached index instead of failing the lookup.

**Nothing is cached and `overall` is `degraded`.** Read `python_status().checks.cache.error` — an unwritable `PYTHON_DOCS_MCP_CACHE_DIR` (read-only mount, missing permission) is the usual cause. Tools keep working without a cache; they are just slower and noisier on the network.

**A lookup returns `blocked by robots.txt`.** The site's rules disallow that path for this user agent, and the request was not sent. For docs.python.org that normally means an EOL version line (`/2/` … `/3.10/`) or `/dev`; for PyPI it means the JSON API or `/simple/`. Fetch the page yourself, or accept the consequences of the opt-out switch above.

**`pypi_package` says "JavaScript bot-verification page".** That is PyPI's CDN, not a bug in this server: the URL came back HTTP 200 with the challenge body. Retry later, check the name's case, drop the version pin — and remember that an unknown name can produce the same page ([why](#known-trade-off-the-js-client-challenge)).

**A mixed-case module name can cost two requests.** docs.python.org serves module pages only under lowercase filenames, so `xml.etree.ElementTree` is fetched as `xml.etree.elementtree.html` first and the normal case costs one page request; only when the lowercase page genuinely does not exist does the fetcher spend a second request on the spelling you gave.

## License & attribution

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Michal Gaspierik.

The **design** of the politeness layer was inspired by [Crawl4AI](https://github.com/unclecode/crawl4ai) (© Unclecode, Apache-2.0): a TTL-cached robots store, the wildcard rule translation, and a per-domain rate limiter with escalating backoff. The implementation is a clean-room rewrite in this project's own synchronous stdlib-plus-`httpx` style — no line was transcribed, translated or mechanically adapted from Crawl4AI, and Crawl4AI is not a dependency of this package. Three defects of the original design are fixed (robots `fetched_at` refresh, negative-result caching, `Crawl-delay` support), and the RFC 9309 matcher is what makes the PyPI rules above actually bind — `robotparser` would have let the JSON API through. The GPL-3.0 part of Crawl4AI — its vendored `html2text` fork — is deliberately excluded: no code, data or dependency from that tree is used or shipped here; HTML→markdown stays `markdownify`'s job. The full statement is in [NOTICE](NOTICE).

Same clean-room architecture as [flutter-mcp](https://github.com/KEEPEE/flutter-mcp) and [java-spring-mcp](https://github.com/KEEPEE/java-spring-mcp): fetchers → pure parsers → TTL cache → search index → tools, with no circular delegation, a pinned 1.x MCP SDK, error-dict failures and an offline test suite.
