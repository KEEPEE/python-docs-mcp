# python-docs-mcp

Real-time **CPython stdlib documentation** (docs.python.org/3) and **PyPI package info** as an MCP (Model Context Protocol) server. Built so AI agents write Python code against real, current APIs instead of hallucinated or deprecated ones.

- **Stdlib modules** — every module page scraped live from [docs.python.org/3/library](https://docs.python.org/3/library/) and converted to clean markdown
- **PyPI packages** — metadata + README (RST→markdown) via the official [JSON API](https://pypi.org/help/api/#package-information), optionally pinned to a version
- **Local search index** over all 229 stdlib modules (name + description recorded, so `ElementTree` resolves to `xml.etree.elementtree` automatically), rebuilt at most every 7 days with stale-fallback
- **SQLite TTL cache** so repeated lookups are instant

## Tools

| Tool | What it does |
|---|---|
| `python_docs` | Fetch a stdlib module page as markdown. Identifier forms: `json` (plain module), `xml.etree.ElementTree` (dotted — the fetcher lowercases on 404 fallback), or plain names like `ElementTree` (resolved via the index to the unique best match). Optional `topic` filter (a heading section) and `max_tokens` truncation. |
| `python_search` | Multi-token ranked search over the stdlib module index (name + description; every token must match). Returns `{module, description, score}` results — call `python_docs` with the chosen name. |
| `pypi_package` | PyPI package metadata: resolved version, summary, requires_python, license, homepage, project URLs and the README as markdown. Optional pinned `version`. |
| `python_status` | Real health check: index size/age/staleness, cache stats, live probes of docs.python.org and pypi.org (with curl fallback for edges that stall Python TLS). |
| `health_check` | Server liveness + version. |

All tools return plain dicts; failures come back as `{"ok": false, "error": ..., "suggestion": ...}` — the server never crashes on a bad lookup.

## Requirements

- Python 3.10+ (tested on 3.12)
- The MCP Python SDK **1.x** is pinned (`mcp>=1.2.0,<2.0`) — this package does not support the mcp 2.x rename of FastMCP.
- `curl` on PATH (fallback transport for hosts that throttle Python TLS clients)

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

`~/.cache/python-docs-mcp/cache.db` (override with `PYTHON_DOCS_MCP_CACHE_DIR`). Module docs cached 7 days, PyPI metadata 1 day. Delete the file to force a full refresh.

## Development

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q                 # offline unit tests (fixtures in tests/fixtures/)
.venv/bin/python scripts/e2e_mcp_test.py      # live end-to-end: spawns the server, exercises every tool
```

## Design notes

Same clean-room architecture as [java-spring-mcp](https://github.com/KEEPEE/java-spring-mcp): fetchers → pure parsers → TTL cache → search index → tools, with no circular delegation, a pinned 1.x MCP SDK, error-dict failures, and an offline test suite backed by real page fixtures (a docs.python.org module page, the full library index, PyPI JSON).

## License

MIT
