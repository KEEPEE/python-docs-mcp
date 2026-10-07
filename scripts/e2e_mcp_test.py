#!/usr/bin/env python3
"""End-to-end test for the python-docs MCP server over stdio JSON-RPC.

Spawns the installed console script (``python-docs``) as a subprocess and
speaks newline-delimited JSON-RPC to it: initialize → notifications/initialized
→ tools/list → tools/call for every tool (including one negative case) →
health_check at the very end to prove the server is still alive after an error
response.

Stdlib only — no project imports. Exit code 0 only if every check passes.
Per-call timeout is generous (300s for index-dependent first calls): the first
python_status / python_search / python_docs call may trigger a search-index
build plus live fetches against docs.python.org and pypi.org.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

#: Which server process to spawn.  Override with ``PYTHON_DOCS_MCP_E2E_CMD``
#: (a shell-split command); otherwise use this checkout's ``.venv`` if it has
#: the console script, else run the module with the interpreter that started
#: this script (works with any install, editable or not).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOCAL_BIN = os.path.join(_REPO_ROOT, ".venv", "bin", "python-docs")
if os.environ.get("PYTHON_DOCS_MCP_E2E_CMD"):
    SERVER_CMD = os.environ["PYTHON_DOCS_MCP_E2E_CMD"].split()
elif os.path.exists(_LOCAL_BIN):
    SERVER_CMD = [_LOCAL_BIN]
else:
    SERVER_CMD = [sys.executable, "-m", "python_docs_mcp.server"]
PROTOCOL_VERSION = "2025-03-26"
PER_CALL_TIMEOUT = 120.0
FIRST_CALL_TIMEOUT = 300.0

EXPECTED_TOOLS = {
    "health_check",
    "python_docs",
    "python_search",
    "pypi_package",
    "python_status",
}


class McpClient:
    """Minimal newline-delimited JSON-RPC client over a subprocess stdio."""

    def __init__(self, cmd):
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._next_id = 0
        self._lines: "queue.Queue[str]" = queue.Queue()
        self.stderr_chunks: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_chunks.append(line)

    def send_notification(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    def request(self, method: str, params: dict | None = None,
                timeout: float = PER_CALL_TIMEOUT):
        self._next_id += 1
        mid = self._next_id
        msg = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out after {timeout}s waiting for '{method}'")
            line = self._lines.get(timeout=remaining)
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue  # ignore non-JSON noise on stdout
            if data.get("id") != mid:
                continue  # not our response (sequential calls → none expected)
            if "error" in data:
                raise RuntimeError(f"JSON-RPC error for {method}: {data['error']}")
            return data["result"]

    def _write(self, obj) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        finally:
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass


def call_tool(client: McpClient, name: str, arguments: dict,
              timeout: float = PER_CALL_TIMEOUT) -> dict:
    """tools/call → the parsed dict the tool returned.

    Raises on protocol errors or when the tool reports isError; a tool that
    returns its own {"error": ...} dict is NOT an error here (isError stays
    false) and is returned as-is.
    """
    result = client.request("tools/call", {"name": name, "arguments": arguments},
                            timeout=timeout)
    if result.get("isError"):
        raise RuntimeError(f"tool '{name}' reported isError: {_first_text(result)[:300]}")
    return json.loads(_first_text(result))


def _first_text(result) -> str:
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            return item.get("text", "")
    raise RuntimeError(f"no text content in tools/call result: {json.dumps(result)[:300]}")


def main() -> int:
    client = McpClient(SERVER_CMD)
    results: list[tuple[str, bool, str]] = []

    def record(label: str, ok: bool, detail: str) -> None:
        results.append((label, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {label}: {detail}")

    try:
        # 1. initialize --------------------------------------------------------
        init = client.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "python-docs-e2e", "version": "0.1"},
        })
        record(
            "initialize",
            bool(init.get("serverInfo")),
            f"server={init.get('serverInfo', {}).get('name')} protocol={init.get('protocolVersion')}",
        )
        client.send_notification("notifications/initialized")

        # 2. tools/list ----------------------------------------------------------
        listed = client.request("tools/list", {})
        names = {t.get("name") for t in listed.get("tools", [])}
        missing = EXPECTED_TOOLS - names
        record(
            "tools/list",
            not missing,
            f"tools={sorted(names)}" + (f" missing={sorted(missing)}" if missing else ""),
        )

        # 3. python_status (first call may build the search index) -----------------
        data = call_tool(client, "python_status", {}, timeout=FIRST_CALL_TIMEOUT)
        checks = data.get("checks", {})
        ok = (
            isinstance(checks, dict)
            and {"search_index", "cache", "docs_python_org", "pypi_org"} <= set(checks)
            and data.get("overall") in ("ok", "degraded")
        )
        si = checks.get("search_index", {}) if isinstance(checks, dict) else {}
        record(
            'python_status {}',
            ok,
            f"overall={data.get('overall')} index_entries={si.get('entries')} "
            f"stale={si.get('stale')} cache_entries={checks.get('cache', {}).get('entries')} "
            f"docs={checks.get('docs_python_org', {}).get('http_status')} "
            f"pypi={checks.get('pypi_org', {}).get('http_status')}",
        )

        # 4. python_search -----------------------------------------------------------
        data = call_tool(client, "python_search", {"query": "http client", "limit": 5},
                         timeout=FIRST_CALL_TIMEOUT)
        results_list = data.get("results")
        ok = (
            data.get("ok") is True
            and data.get("query") == "http client"
            and isinstance(results_list, list)
            and len(results_list or []) >= 1
            and all(isinstance(r.get("module"), str) for r in (results_list or []))
            and data.get("index_count", 0) >= 1
        )
        top = results_list[0] if results_list else {}
        record(
            'python_search {"query": "http client", "limit": 5}',
            ok,
            f"count={len(results_list or [])} top={top.get('module')} "
            f"score={top.get('score')} index_count={data.get('index_count')} "
            f"stale={data.get('stale')}",
        )

        # 5. python_docs plain module ---------------------------------------------------
        data = call_tool(client, "python_docs", {"identifier": "json"},
                         timeout=FIRST_CALL_TIMEOUT)
        ok = (
            data.get("ok") is True
            and isinstance(data.get("markdown"), str)
            and "dumps" in data["markdown"]
            and "docs.python.org" in (data.get("url") or "")
            and "json" in str(data.get("title") or "")
        )
        record(
            'python_docs {"identifier": "json"}',
            ok,
            f"title={data.get('title')!r} markdown_len={len(data.get('markdown', ''))} "
            f"cached={data.get('cached')} truncated={data.get('truncated')}",
        )

        # 6. python_docs plain name → index resolution -----------------------------------
        data = call_tool(client, "python_docs", {"identifier": "ElementTree"},
                         timeout=FIRST_CALL_TIMEOUT)
        ok = (
            data.get("ok") is True
            and data.get("identifier") == "ElementTree"
            and "xml.etree.elementtree" in (data.get("url") or "")
            and len(data.get("markdown", "")) > 100
        )
        record(
            'python_docs {"identifier": "ElementTree"}',
            ok,
            f"title={data.get('title')!r} url={data.get('url')} "
            f"cached={data.get('cached')} truncated={data.get('truncated')}",
        )

        # 7. pypi_package ------------------------------------------------------------------
        data = call_tool(client, "pypi_package", {"name": "requests"},
                         timeout=FIRST_CALL_TIMEOUT)
        ok = (
            data.get("ok") is True
            and data.get("name") == "requests"
            and bool(data.get("version"))
            and isinstance(data.get("cached"), bool)
            and "error" not in data
        )
        record(
            'pypi_package {"name": "requests"}',
            ok,
            f"name={data.get('name')} version={data.get('version')} "
            f"cached={data.get('cached')} readme={'yes' if data.get('readme_markdown') else 'no'}",
        )

        # 8. negative case -------------------------------------------------------------------
        data = call_tool(client, "pypi_package", {"name": "no-such-package-xyz-12345"},
                         timeout=FIRST_CALL_TIMEOUT)
        ok = (
            isinstance(data, dict)
            and data.get("ok") is False
            and "error" in data
            and "suggestion" in data
        )
        record(
            'pypi_package {"name": "no-such-package-xyz-12345"}',
            ok,
            f"error={str(data.get('error'))[:140]!r}",
        )

        # 9. liveness after the error ---------------------------------------------------------
        data = call_tool(client, "health_check", {})
        ok = data.get("status") == "ok"
        record("health_check (liveness)", ok, f"version={data.get('version')}")
    except Exception as exc:
        record(f"exception during run ({exc.__class__.__name__})", False, str(exc)[:300])
    finally:
        client.close()

    failed = [r for r in results if not r[1]]
    print()
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        stderr_tail = "".join(client.stderr_chunks[-20:])
        if stderr_tail.strip():
            print("--- server stderr (tail) ---")
            print(stderr_tail, end="")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
