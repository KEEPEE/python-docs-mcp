# python-docs-mcp

Real-time **CPython stdlib documentation** (docs.python.org/3) and **PyPI package info** as an MCP (Model Context Protocol) server. Built so AI agents write Python code against real, current APIs instead of hallucinated or deprecated ones.

- **Stdlib modules** — every module page scraped live from [docs.python.org/3/library](https://docs.python.org/3/library/) and converted to clean markdown
- **PyPI packages** — metadata + README (RST→markdown) read from the package's [project page](https://pypi.org/project/<name>/), optionally pinned to a version (see [PyPI source](#pypi-source-and-robotstxt))
- **Local search index** over all 229 stdlib modules (name + description recorded, so `ElementTree` resolves to `xml.etree.elementtree` automatically), rebuilt at most every 7 days with stale-fallback
- **SQLite TTL cache** so repeated lookups are instant

## Tools

| Tool | What it does |
|---|---|
| `python_docs` | Fetch a stdlib module page as markdown. Identifier forms: `json` (plain module), `xml.etree.ElementTree` (dotted — the lowercase filename is tried first, the given spelling only as a fallback), or plain names like `ElementTree` (resolved via the index to the unique best match). Optional `topic` filter (a heading section) and `max_tokens` truncation. |
| `python_search` | Multi-token ranked search over the stdlib module index (name + description; every token must match). Returns `{module, description, score}` results — call `python_docs` with the chosen name. |
| `pypi_package` | PyPI package metadata: resolved version, summary, requires_python, license, homepage, project URLs and the README as markdown. Optional pinned `version`. |
| `python_status` | Real health check: index size/age/staleness, cache stats, live probes of docs.python.org and pypi.org (both on robots-allowed paths), plus the politeness layer's counters under a top-level `politeness` key. |
| `health_check` | Server liveness + version. |

All tools return plain dicts; failures come back as `{"ok": false, "error": ..., "suggestion": ...}` — the server never crashes on a bad lookup.

## Requirements

- Python 3.10+ (tested on 3.12)
- The MCP Python SDK **1.x** is pinned (`mcp>=1.2.0,<2.0`) — this package does not support the mcp 2.x rename of FastMCP.

## Run it

### Option A — `uvx` straight from this repo (no manual install)

```bash
# TOKEN = your GitLab personal access token for github.com/KEEPEE
uvx --from "git+https://github.com/KEEPEE/python-docs-mcp.git" python-docs
```

Add `--refresh` to force re-pulling the latest commit after an update.

### Option B — local venv (fastest startup, no token in config)

```bash
git clone https://github.com/KEEPEE/python-docs-mcp.git
cd python-docs-mcp
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e .
.venv/bin/python-docs        # starts the stdio MCP server
```

## MCP client configuration

### DSH (this machine) — in `~/.dsh-home/profiles/web/cordis.patch.yml`

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

### Claude Desktop / any generic MCP client

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

## Cache location

`~/.cache/python-docs-mcp/cache.db` (override with `PYTHON_DOCS_MCP_CACHE_DIR`). Module docs cached 7 days, PyPI metadata 1 day. The same directory holds `robots.db`, the 7-day robots.txt cache — one env var moves both. Delete the files to force a full refresh.

## Politeness

Every outbound request — `python_docs`, `python_search` (its index build), `pypi_package` and the `python_status` probes — goes through one small internal layer, `src/python_docs_mcp/politeness.py` (stdlib + `httpx`, no new dependency):

- **robots.txt is read and obeyed.** Rules are matched per RFC 9309 (`*`, trailing `$`, `%2A`/`%24` literals, merged `User-agent` groups, most-specific match wins, `Allow` beats `Disallow` on a tie). `urllib.robotparser` is deliberately not used: on Python < 3.14 it ignores wildcards, which is exactly how this server ended up violating PyPI (see [PyPI source](#pypi-source-and-robotstxt)). `docs.python.org` disallows `/dev`, `/release` and the EOL lines (`/2/`, `/3.10/`…) — if such a URL ever reaches the fetcher the answer is `{"ok": false, "error": "… blocked by robots.txt …"}` with **no request made**. Files are cached 7 days per host in `robots.db`, including negative results (404/403/5xx), so a host costs one robots request per week.
- **Per-host throttle, including `Crawl-delay`.** Sequential requests to one host are spaced 0.35–0.9 s by default, or the site's own `Crawl-delay` when it declares one. An audit of this server measured a **7 ms** minimum gap between docs.python.org requests during a cold index build; with the layer the same build waits out the full delay.
- **`429` / `503` / `504` are handled.** `Retry-After` is honoured to the second; without it the per-host delay is escalated and the request retried once. A response that stalls past 10 s escalates the delay instead of being retried blindly.
- **Bounded time, one transport.** 5 s connect / 10 s read / 15 s total, per request. The `curl` subprocess fallback `python_status` used to fall back to is gone: it bypassed robots, throttle and budget, and doubled the worst case.
- **Conditional GET.** `ETag` / `Last-Modified` and the raw body are stored next to the cached entry, so refreshing an expired page costs a `304` instead of re-downloading it.
- **Request budgets — one unit is one request on the wire.** One tool call makes at most **6** requests; the robots.txt fetch of a cold host, a `429` retry, a transport retry and every redirect hop each pay a unit. Measured live: a cold `python_docs("ElementTree")` costs 3 (robots + library-index page + module page), a cold `python_docs("json")` costs 2, and an unknown module name cannot exceed 4 (robots + index + at most two candidate pages) whatever the limit. `python_status` has its own cap of **8** so its probes plus a stale-index rebuild always fit — a probe refused by the budget would report `error` and drag `overall` to `"degraded"` for a purely internal reason. The search-index build shares the tool call's budget; an index build that hits its cap stops early and returns a **partial** index (`"partial": true`) instead of raising, and a partial index is never cached.
- **Redirects are resolved by the layer, not by httpx.** Requests go out with `follow_redirects=False`, every 3xx target is re-checked against that host's robots rules before its body is used, the chain is capped at 5 hops and reported as an error (never an exception), and the `url` a tool returns is the **final** URL of the chain.
- **Host allowlist.** Only `docs.python.org` and `pypi.org` can be contacted, so a malformed identifier or an unexpected redirect cannot turn a docs lookup into a request somewhere else.

The layer never raises and never changes a tool's return shape; `python_status` reports its counters under the top-level `politeness` key (robots cache rows, throttle waits, blocks, retries, stalls, budgets) — diagnostics only, never part of `overall`.

**Opt-out** (at your own risk — you become responsible for whatever the site's rules say):

```bash
export PYTHON_DOCS_MCP_POLITENESS_DISABLED=1
```

That turns off robots, throttle, retry, conditional GET and the allowlist in one switch. There is no partial opt-out.

**Attribution:** the layer's design is inspired by [Crawl4AI](https://github.com/unclecode/crawl4ai) (© Unclecode, Apache-2.0) — the robots store, the wildcard translation and the per-domain rate limiter. It is a clean-room rewrite, not a copy, and Crawl4AI is not a dependency here. See [`NOTICE`](NOTICE).

## PyPI source (and robots.txt)

PyPI's `robots.txt` says, for `User-agent: *`:

```
Disallow: /pypi/*/json
Disallow: /pypi/*/*/json
Disallow: /pypi*?
Disallow: /simple/
```

Until now this server read package metadata from `https://pypi.org/pypi/<name>/json` — a path that file forbids. (Python's own `robotparser` did not catch it: it treats `/pypi/*/json` as a literal prefix, so it reports that URL as allowed.) `/simple/` is not a way around it — it is disallowed too, and it carries no README or project URLs.

**The default source is now the project page**, `https://pypi.org/project/<name>/` (or `/project/<name>/<version>/`), which robots.txt allows. The parser pulls the same fields out of the rendered page — name, resolved version, summary, `Requires-Python`, license (both the modern "License expression" block and the legacy "License" sidebar entry), homepage, project URLs and the README — and `pypi_package` returns **exactly the same keys as before**, so nothing downstream changed.

**The JSON API is opt-in only.** Set

```bash
export PYTHON_DOCS_MCP_PYPI_JSON_OPT_IN=1
```

and `pypi_package` will use `https://pypi.org/pypi/<name>/json` again. That endpoint is disallowed by robots.txt, so using it is your responsibility, and every result produced that way carries a `notice` field saying so. With the opt-in off (the default) the JSON endpoint is never requested — not even as a fallback when the project page fails, and the politeness layer blocks that URL independently, so the violation cannot come back through another door.

**Known limitation — the JS "Client Challenge".** PyPI's CDN answers some project pages with a ~3 KB JavaScript "Client Challenge" instead of the page (HTTP 200, measured at 3 038 B). The politeness layer's optional `challenge_detector` hook is wired to a PyPI-specific predicate in `fetchers.pypi_challenge_detector`: small body + HTML + a challenge marker (`_fs-ch-`, "Client Challenge") + none of the markers a real project page always carries. When it fires, the layer waits (escalated per-host delay) and retries **that same URL once** — never another endpoint. If the answer is still a challenge the tool reports it honestly:

```json
{"ok": false,
 "error": "pypi.org answered with a JavaScript bot-verification page (HTTP 200, not a 404) instead of the project page. Either PyPI is serving its anti-bot challenge for this URL, or the package does not exist — PyPI also answers unknown package names with that page, so a challenge is not proof the name is valid",
 "suggestion": "retry later …, check the name (case-sensitive) or drop the version pin, or use the PYTHON_DOCS_MCP_PYPI_JSON_OPT_IN=1 opt-in …"}
```

That two-sided wording is deliberate: a challenge page is what PyPI's edge returns for a cold-URL cache miss **and** for a package that does not exist, so claiming one or the other would be a guess. The body is never parsed into a fake package, never cached, and never answered with a fallback to the robots-disallowed JSON endpoint. A real page is fetched exactly once — the detector does not add a request (covered by `tests/test_politeness_wiring.py`).

## Development

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q                 # offline unit tests (fixtures in tests/fixtures/)
.venv/bin/python scripts/e2e_mcp_test.py      # live end-to-end: spawns the server, exercises every tool
```

## Design notes

Same clean-room architecture as [java-spring-mcp](https://github.com/KEEPEE/java-spring-mcp): fetchers → pure parsers → TTL cache → search index → tools, with no circular delegation, a pinned 1.x MCP SDK, error-dict failures, and an offline test suite backed by real page fixtures (a docs.python.org module page, the full library index, two real PyPI project pages and the JS challenge page PyPI's CDN returns for some URLs).

## License

MIT. The politeness layer's design was inspired by Crawl4AI (Apache-2.0);
the implementation is our own clean-room rewrite — see [`NOTICE`](NOTICE).
