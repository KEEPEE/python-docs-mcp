"""Wiring tests: do this repo's HTTP paths really go through the politeness layer?

:mod:`tests.test_politeness` proves the layer itself is correct.  These tests
prove the *integration* — that ``fetch_python_module``, ``fetch_pypi_package``,
the search-index build and ``python_status`` cannot bypass it.  Everything is
offline: the only HTTP client in play is ``httpx.MockTransport``; time and sleep
are injected.

They also pin the decisions that are specific to THIS repo:

1. the default PyPI path never touches ``/pypi/<name>/json`` — the endpoint
   PyPI's robots.txt disallows — not even when the project page fails (§ 2),
2. the politeness layer itself blocks that endpoint, so the violation cannot
   come back through another door (§ 2),
3. the JSON API is reachable only through ``PYTHON_DOCS_MCP_PYPI_JSON_OPT_IN``,
   and then the result says so in a ``notice`` (§ 2),
4. a mixed-case module lookup costs ONE request, not two (§ 3),
5. docs.python.org requests are throttled to the layer's delay instead of the
   7 ms gap the audit measured (§ 4),
6. a spent request budget yields a *partial* index, never an exception, and is
   never cached (§ 5).

Seams used (all module-level on purpose):

- ``httpx.Client``              → same client, MockTransport swapped in
- ``fetchers.set_politeness``   → a layer with an injected clock / RNG
- ``fetchers.DOCS_BASE_URL``    → an older docs line, to test the robots gate
- ``search._fetch_library_index_html`` → keep the index build out of a fetch test
"""

from __future__ import annotations

import random
from pathlib import Path

import httpx
import pytest

import python_docs_mcp.fetchers as fetchers_mod
import python_docs_mcp.search as search_mod
import python_docs_mcp.server as server_mod
from python_docs_mcp.cache import DocCache
from python_docs_mcp.fetchers import (
    ALLOWED_HOSTS,
    BOT_CHALLENGE_ERROR,
    FETCH_BUDGET_LIMIT,
    PYPI_JSON_OPT_IN_ENV_VAR,
    PYPI_JSON_OPT_IN_NOTICE,
    fetch_pypi_package,
    fetch_python_module,
    pypi_json_url,
    pypi_project_url,
    tool_budget,
)
from python_docs_mcp.politeness import Politeness
from python_docs_mcp.search import INDEX_KEY, LIBRARY_INDEX_URL

FIXTURES = Path(__file__).parent / "fixtures"

JSON_HTML = (FIXTURES / "py_json_module.html").read_bytes()
LIBRARY_INDEX_HTML = (FIXTURES / "py_library_index.html").read_bytes()
ATTRS_HTML = (FIXTURES / "pypi_attrs_project.html").read_bytes()
BOT_CHALLENGE_HTML = (FIXTURES / "pypi_bot_challenge.html").read_bytes()

JSON_PATH = "/3/library/json.html"
LIBRARY_INDEX_PATH = "/3/library/index.html"
ATTRS_PATH = "/project/attrs/"

#: Verbatim from https://pypi.org/robots.txt (copy kept at
#: mcp-politeness-audit/robots/pypi.org.txt).  The two wildcard rules are the
#: whole point: ``urllib.robotparser`` on Python 3.12 reads them as literal
#: prefixes and allows the JSON API; an RFC 9309 matcher blocks it.
PYPI_ROBOTS = b"""Sitemap: https://pypi.org/sitemap.xml

User-agent: *
Disallow: /simple/
Disallow: /packages/
Disallow: /_includes/authed/
Disallow: /project/*/submit-malware-report/
Disallow: /pypi/*/json
Disallow: /pypi/*/*/json
Disallow: /pypi*?
Disallow: /search*
Disallow: /_/
Disallow: /account/
Disallow: /admin/
"""

#: Trimmed from https://docs.python.org/robots.txt — the ``*`` group and the
#: disallowed old-version lines (a real gate we can trip by changing the base).
DOCS_ROBOTS = b"""Sitemap: https://docs.python.org/sitemap.xml

# Prevent development and old documentation from showing up in search results.
User-agent: *
Disallow: /dev
Disallow: /release

# Disallow EOL versions
Disallow: /2/
Disallow: /3.10/
"""

ATTRS_JSON = b'{"info": {"name": "attrs", "version": "24.2.0", "summary": "x"}}'


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class FakeClock:
    """Monotonic clock + a sleep that advances it (no real waiting in tests)."""

    def __init__(self, start: float = 1_000.0, wall: float = 1_700_000_000.0) -> None:
        self.t = start
        self.wall = wall
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def wall_now(self) -> float:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Recorder:
    """MockTransport handler: canned routes + a record of every request."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        action = self.routes.get(path, self.routes.get("*"))
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action(request, self)
        if isinstance(action, int):
            return httpx.Response(action, content=b"nope")
        headers: dict[str, str] = {}
        if path != "/robots.txt":
            headers = {
                "etag": '"v1"',
                "last-modified": "Wed, 21 Oct 2015 07:28:00 GMT",
            }
        return httpx.Response(200, content=action or b"", headers=headers)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def hits(self, path: str) -> int:
        return self.paths().count(path)

    def page_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path != "/robots.txt"]


_REAL_HTTPX_CLIENT = httpx.Client


def use_transport(monkeypatch, recorder: Recorder) -> Recorder:
    """Swap only the *transport* of every ``httpx.Client`` built in the repo.

    Patching the class rather than ``fetchers._client`` keeps the production
    client factory in play, so the real headers (our UA), timeout and redirect
    policy are what reach the wire — and the ``python_status`` probes, which
    build their own client, are covered by the same seam.
    """

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(recorder)
        return _REAL_HTTPX_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    return recorder


def fast_layer(clock: FakeClock | None = None, **kw) -> Politeness:
    """Install a politeness layer with no throttling sleeps and return it."""
    kw.setdefault("base_delay", (0.0, 0.0))
    kw.setdefault("rng", random.Random(1234))
    kw.setdefault("allowed_hosts", ALLOWED_HOSTS)
    if clock is not None:
        kw["clock"] = clock.now
        kw["sleep"] = clock.sleep
        kw["wall_clock"] = clock.wall_now
    layer = Politeness(fetchers_mod.USER_AGENT, cache_path=None, **kw)
    fetchers_mod.set_politeness(layer)
    return layer


@pytest.fixture
def no_index_build(monkeypatch):
    """Keep the search-index build out of a PyPI tool test.

    The autouse fixtures already isolate the cache dir; this only stubs the
    index so a pypi_package test measures exactly the requests it cares about.
    """
    monkeypatch.setattr(
        server_mod,
        "get_index",
        lambda: {"modules": [], "count": 0, "built_at": "2026-01-01T00:00:00+00:00"},
    )
    return None


def assert_no_json_endpoint(recorder: Recorder) -> None:
    """No request may hit a robots-disallowed PyPI path.

    Used by the default-path tests: this is the regression guard for the
    violation the audit found (``Disallow: /pypi/*/json``).
    """
    offenders = [
        p
        for p in recorder.paths()
        if p.startswith("/pypi/") or p.startswith("/simple/") or p.endswith("/json")
    ]
    assert not offenders, f"disallowed PyPI path requested: {offenders}"


# ---------------------------------------------------------------------------
# 1. fetch_python_module goes through the layer
# ---------------------------------------------------------------------------
def test_fetch_python_module_goes_through_the_layer(monkeypatch):
    rec = use_transport(
        monkeypatch, Recorder({"/robots.txt": DOCS_ROBOTS, JSON_PATH: JSON_HTML})
    )

    out = fetch_python_module("json")

    assert out["ok"] is True
    assert "dumps" in out["markdown"]
    paths = rec.paths()
    # robots.txt is consulted *before* the page — that is the whole point.
    assert paths.index("/robots.txt") < paths.index(JSON_PATH)
    assert rec.hits(JSON_PATH) == 1


def test_polite_user_agent_is_the_one_on_the_wire(monkeypatch):
    rec = use_transport(
        monkeypatch, Recorder({"/robots.txt": DOCS_ROBOTS, JSON_PATH: JSON_HTML})
    )
    fetch_python_module("json")

    for request in rec.requests:
        assert request.headers["user-agent"] == fetchers_mod.USER_AGENT
    # The old UA spoofed Chrome 126; a robots file can only match a token it sees.
    assert "Mozilla" not in fetchers_mod.USER_AGENT
    assert "python-docs-mcp/" in fetchers_mod.USER_AGENT


def test_robots_txt_is_fetched_once_per_host_across_calls(monkeypatch):
    rec = use_transport(monkeypatch, Recorder({"/robots.txt": DOCS_ROBOTS, "*": JSON_HTML}))
    fetch_python_module("json")
    fetch_python_module("os")
    assert rec.hits("/robots.txt") == 1


def test_disallowed_docs_line_is_blocked_with_readable_error(monkeypatch):
    """docs.python.org disallows EOL versions; a base pointing there must fail."""
    monkeypatch.setattr(fetchers_mod, "DOCS_BASE_URL", "https://docs.python.org/3.10/library/")
    rec = use_transport(monkeypatch, Recorder({"/robots.txt": DOCS_ROBOTS, "*": JSON_HTML}))

    out = fetch_python_module("json")

    assert out["ok"] is False
    assert "robots.txt" in out["error"]
    assert rec.hits("/3.10/library/json.html") == 0


def test_allowlist_refuses_foreign_hosts(monkeypatch):
    use_transport(monkeypatch, Recorder({"*": JSON_HTML}))
    layer = fast_layer()
    assert layer.can_fetch("https://evil.example.internal/x") is False
    response = fetchers_mod._http_get("https://evil.example.internal/x")
    assert response.error and "not in ALLOWED_HOSTS" in response.error


# ---------------------------------------------------------------------------
# 2. PyPI: the robots.txt violation, fixed and guarded
# ---------------------------------------------------------------------------
def test_default_pypi_path_never_touches_the_json_endpoint(monkeypatch):
    """REQUIRED guard: the default path must not request /pypi/<name>/json.

    The JSON route is present in the transport and would *raise* if it were
    ever hit, so this test fails loudly rather than quietly returning data
    fetched from a disallowed URL.
    """
    rec = use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": PYPI_ROBOTS,
                ATTRS_PATH: ATTRS_HTML,
                "/pypi/attrs/json": AssertionError("the disallowed JSON endpoint was requested"),
            }
        ),
    )

    out = fetch_pypi_package("attrs")

    assert out["ok"] is True, out
    assert out["name"] == "attrs"
    assert out["version"] == "26.1.0"
    assert out["summary"] == "Classes Without Boilerplate"
    assert out["requires_python"] == ">=3.9"
    assert out["license"] == "MIT"
    assert out["homepage"] == "https://github.com/python-attrs/attrs"
    assert out["readme_markdown"]
    assert out["url"] == "https://pypi.org/project/attrs/26.1.0/"
    assert "notice" not in out  # the notice belongs to the opt-in path only
    assert rec.hits(ATTRS_PATH) == 1
    assert_no_json_endpoint(rec)


def test_politeness_layer_blocks_the_pypi_json_urls(monkeypatch):
    """REQUIRED double insurance: the layer itself calls those URLs blocked.

    Even if some future code path asked for the JSON API, the layer would
    refuse it — the wildcard rules are what stdlib ``robotparser`` gets wrong.
    """
    rec = use_transport(monkeypatch, Recorder({"/robots.txt": PYPI_ROBOTS}))
    layer = fast_layer()

    assert layer.can_fetch(pypi_json_url("attrs")) is False
    assert layer.can_fetch(pypi_json_url("attrs", "24.2.0")) is False
    assert layer.can_fetch("https://pypi.org/simple/attrs/") is False
    assert layer.can_fetch("https://pypi.org/search/?q=attrs") is False
    # …and the replacement source is allowed.
    assert layer.can_fetch(pypi_project_url("attrs")) is True
    assert layer.can_fetch(pypi_project_url("attrs", "24.2.0")) is True

    with httpx.Client() as client:
        blocked = layer.get(client, pypi_json_url("attrs"))
    assert blocked.blocked_by_robots is True
    assert "/pypi/*/json" in (blocked.error or "")
    assert blocked.status_code is None
    # Nothing but robots.txt went on the wire.
    assert rec.paths() == ["/robots.txt"] * len(rec.paths())


def test_json_endpoint_is_not_requested_even_when_the_project_page_fails(monkeypatch):
    """No silent fallback: a failing HTML page must not unlock the JSON API."""
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": PYPI_ROBOTS, ATTRS_PATH: 500}),
    )

    out = fetch_pypi_package("attrs")

    assert out["ok"] is False
    assert "HTTP 500" in out["error"]
    assert_no_json_endpoint(rec)


def test_pypi_404_is_reported_as_not_found(monkeypatch):
    rec = use_transport(
        monkeypatch, Recorder({"/robots.txt": PYPI_ROBOTS, ATTRS_PATH: 404})
    )
    out = fetch_pypi_package("attrs")
    assert out["ok"] is False
    assert out["error"] == "package not found on PyPI"
    assert out["suggestion"]
    assert_no_json_endpoint(rec)


def test_bot_challenge_page_is_reported_not_parsed(monkeypatch):
    """PyPI answers some cold URLs with a JS challenge (HTTP 200).

    It must be named as what it is — never parsed into a fake package, and
    never answered by falling back to the disallowed JSON endpoint.
    """
    rec = use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": PYPI_ROBOTS,
                ATTRS_PATH: BOT_CHALLENGE_HTML,
                "/pypi/attrs/json": AssertionError("JSON fallback used after a challenge page"),
            }
        ),
    )

    out = fetch_pypi_package("attrs")

    assert out["ok"] is False
    assert out["error"] == BOT_CHALLENGE_ERROR
    assert out["bot_challenge"] is True
    assert PYPI_JSON_OPT_IN_ENV_VAR in out["suggestion"]
    assert_no_json_endpoint(rec)


# -- A8 F4: the layer's challenge hook, wired to this repo's detector -------- #

def test_challenge_page_costs_exactly_one_retry_on_the_same_url(monkeypatch):
    """The layer retries a challenge **once**, on the same URL, after a wait.

    This is the F4 path with the production detector: the challenge is HTTP
    200, so only the body shape says "this is not the document".  The retry is
    the same URL (never another endpoint), it is spaced by the escalated
    per-host delay (never an immediate re-hit), and when it is still a
    challenge the tool reports both possible truths.
    """
    clock = FakeClock()
    layer = fast_layer(clock, challenge_detector=fetchers_mod.pypi_challenge_detector)
    rec = use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": PYPI_ROBOTS,
                ATTRS_PATH: BOT_CHALLENGE_HTML,
                "/pypi/attrs/json": AssertionError("JSON fallback used after a challenge page"),
            }
        ),
    )

    out = fetch_pypi_package("attrs")

    urls = [str(r.url) for r in rec.page_requests()]
    assert urls == [pypi_project_url("attrs")] * 2, urls  # 1 retry, same URL
    assert clock.slept, "the retry must be spaced, not an immediate re-hit"
    st = layer.stats()
    assert st["challenge_detected"] == 2  # both attempts were challenges
    assert st["challenge_retries"] == 1   # exactly one retry
    assert st["requests"] == 2 and st["robots_requests"] == 1

    assert out["ok"] is False
    assert out["error"] == BOT_CHALLENGE_ERROR
    assert out["bot_challenge"] is True
    # Honest wording: a challenge is not proof the package exists.
    assert "not a 404" in out["error"]
    assert "does not exist" in out["error"]
    assert_no_json_endpoint(rec)
    # A challenge body is never stored for conditional GET.
    assert fetchers_mod._revalidation_for(pypi_project_url("attrs")) == (None, None)


def test_real_project_page_is_fetched_once_with_the_detector_active(monkeypatch):
    """No retry, no challenge counters: the detector must not eat real pages."""
    clock = FakeClock()
    layer = fast_layer(clock, challenge_detector=fetchers_mod.pypi_challenge_detector)
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": PYPI_ROBOTS, ATTRS_PATH: ATTRS_HTML}),
    )

    out = fetch_pypi_package("attrs")

    assert out["ok"] is True and out["name"] == "attrs"
    assert rec.hits(ATTRS_PATH) == 1, "a real page must never be re-requested"
    st = layer.stats()
    assert st["challenge_detected"] == 0 and st["challenge_retries"] == 0


def test_production_layer_has_the_pypi_detector_wired(monkeypatch):
    """Guard: :func:`get_politeness` must pass this repo's detector.

    A detector that exists but is never handed to the layer is the failure mode
    A8 F4 was written for — the challenge would then be parsed as a page.
    """
    fetchers_mod.set_politeness(None)  # drop the test instance, build the real one
    layer = fetchers_mod.get_politeness()
    try:
        assert layer.challenge_detector is fetchers_mod.pypi_challenge_detector
        assert layer.max_retry_on_challenge == 1
    finally:
        layer.close()


def test_opt_in_json_path_is_used_and_carries_a_notice(monkeypatch):
    monkeypatch.setenv(PYPI_JSON_OPT_IN_ENV_VAR, "1")
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": PYPI_ROBOTS, "/pypi/attrs/json": ATTRS_JSON, ATTRS_PATH: ATTRS_HTML}),
    )

    out = fetch_pypi_package("attrs")

    assert out["ok"] is True
    assert out["version"] == "24.2.0"
    assert rec.hits("/pypi/attrs/json") == 1
    assert rec.hits(ATTRS_PATH) == 0  # opt-in does not fetch the page as well
    assert out["notice"] == PYPI_JSON_OPT_IN_NOTICE
    assert "robots.txt" in out["notice"]
    assert PYPI_JSON_OPT_IN_ENV_VAR in out["notice"]


def test_opt_in_notice_is_present_on_failures_too(monkeypatch):
    monkeypatch.setenv(PYPI_JSON_OPT_IN_ENV_VAR, "true")
    use_transport(monkeypatch, Recorder({"/pypi/attrs/json": 404}))
    out = fetch_pypi_package("attrs")
    assert out["ok"] is False
    assert out["notice"] == PYPI_JSON_OPT_IN_NOTICE


def test_opt_in_off_by_default_even_with_odd_values(monkeypatch):
    for value in ("0", "false", "", "nope"):
        monkeypatch.setenv(PYPI_JSON_OPT_IN_ENV_VAR, value)
        rec = use_transport(
            monkeypatch,
            Recorder({"/robots.txt": PYPI_ROBOTS, ATTRS_PATH: ATTRS_HTML, "/pypi/attrs/json": ATTRS_JSON}),
        )
        out = fetch_pypi_package("attrs")
        assert out["ok"] is True
        assert "notice" not in out
        assert_no_json_endpoint(rec)


def test_server_pypi_package_passes_the_notice_through_and_caches_it(no_index_build, monkeypatch):
    monkeypatch.setenv(PYPI_JSON_OPT_IN_ENV_VAR, "1")
    use_transport(monkeypatch, Recorder({"/pypi/attrs/json": ATTRS_JSON}))

    first = server_mod.pypi_package("attrs")
    assert first["ok"] is True
    assert first["notice"] == PYPI_JSON_OPT_IN_NOTICE

    second = server_mod.pypi_package("attrs")
    assert second["cached"] is True
    assert second["notice"] == PYPI_JSON_OPT_IN_NOTICE


def test_server_pypi_package_default_shape_is_unchanged(no_index_build, monkeypatch):
    """The keys consumers (e2e script, README) rely on must not change."""
    use_transport(
        monkeypatch, Recorder({"/robots.txt": PYPI_ROBOTS, ATTRS_PATH: ATTRS_HTML})
    )
    out = server_mod.pypi_package("attrs")
    assert out["ok"] is True
    assert out["cached"] is False
    assert {
        "ok",
        "name",
        "version",
        "summary",
        "requires_python",
        "license",
        "homepage",
        "project_urls",
        "readme_markdown",
        "url",
        "cached",
    } <= set(out)
    assert "notice" not in out


# ---------------------------------------------------------------------------
# 3. lowercase-first module lookup (one request instead of two)
# ---------------------------------------------------------------------------
def test_mixed_case_lookup_makes_one_request(monkeypatch):
    """The audit measured a guaranteed 404 per mixed-case lookup; it is gone."""
    rec = use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": DOCS_ROBOTS,
                "/3/library/xml.etree.elementtree.html": JSON_HTML,
                "/3/library/xml.etree.ElementTree.html": 404,
            }
        ),
    )

    out = fetch_python_module("xml.etree.ElementTree")

    assert out["ok"] is True
    pages = [r.url.path for r in rec.page_requests()]
    assert pages == ["/3/library/xml.etree.elementtree.html"]
    assert rec.hits("/3/library/xml.etree.ElementTree.html") == 0


def test_lowercase_first_still_falls_back_to_the_given_name(monkeypatch):
    """If the lowercase page is missing, the name as given is still tried."""
    rec = use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": DOCS_ROBOTS,
                "/3/library/xml.etree.elementtree.html": 404,
                "/3/library/xml.etree.ElementTree.html": JSON_HTML,
            }
        ),
    )
    out = fetch_python_module("xml.etree.ElementTree")
    assert out["ok"] is True
    assert rec.hits("/3/library/xml.etree.ElementTree.html") == 1


def test_both_candidates_missing_reports_not_found(monkeypatch):
    rec = use_transport(monkeypatch, Recorder({"/robots.txt": DOCS_ROBOTS, "*": 404}))
    out = fetch_python_module("nope_xyz_module")
    assert out["ok"] is False
    assert "not found in the Python 3 documentation" in out["error"]
    assert rec.hits("/3/library/nope_xyz_module.html") == 1


# ---------------------------------------------------------------------------
# 4. throttle: the 7 ms gap the audit measured cannot come back
# ---------------------------------------------------------------------------
def test_docs_python_org_requests_are_throttled(monkeypatch):
    """The audit measured a **7 ms** gap between docs.python.org requests (A2 §4.1).

    With an injected clock, the layer's ``base_delay`` must be what separates
    the two page requests — not 7 ms — and the wait is a recorded fake sleep,
    never a real one.
    """
    clock = FakeClock()
    layer = fast_layer(clock, base_delay=(0.35, 0.9))
    stamps: list[tuple[str, float]] = []

    def handler(request, recorder):
        stamps.append((request.url.path, clock.now()))
        return httpx.Response(200, content=JSON_HTML)

    def robots_handler(request, recorder):
        stamps.append(("/robots.txt", clock.now()))
        return httpx.Response(200, content=DOCS_ROBOTS)

    use_transport(monkeypatch, Recorder({"/robots.txt": robots_handler, "*": handler}))

    assert fetch_python_module("json")["ok"] is True
    assert fetch_python_module("os")["ok"] is True

    pages = [t for path, t in stamps if path != "/robots.txt"]
    assert len(pages) == 2
    gap = pages[1] - pages[0]
    assert gap >= 0.35, f"requests only {gap * 1000:.0f} ms apart"
    assert gap <= 0.9
    # A8 F1: the robots.txt fetch now queues in the same per-host line, so it
    # reserves a slot and the page request behind it waits as well — two waits
    # where the pre-A8 layer had one, and *every* gap on the host (including
    # robots→page) is at least base_delay.  That is exactly the pair A7 measured
    # 12–15 ms apart in the old copy.
    ordered = [t for _path, t in stamps]
    assert len(ordered) == 3  # robots, page, page
    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    assert all(0.35 <= g <= 0.9 for g in gaps), gaps
    st = layer.stats()
    assert st["throttle_waits"] == 2
    assert st["robots_throttle_waits"] == 0  # robots was first on the host: no wait
    assert st["throttle_sleep_s"] == pytest.approx(sum(gaps))


def test_crawl_delay_from_robots_is_honoured(monkeypatch):
    """A site that declares ``crawl-delay`` gets waited out, not ignored.

    (docs.python.org publishes no crawl-delay; ``www.javadoc.io`` does — A2 §2.
    The synthetic file here proves the value is read and applied per host.)

    A13 B2 changed the expected count.  The delay learned from a *cold* robots
    fetch now also gates the page request that triggered that fetch, so with
    ``base_delay=(0, 0)`` there are **two** waits of exactly 2 s — robots→page1
    and page1→page2.  Before A13 the robots→page1 gap was 0 ms, which is
    precisely the defect A12 measured live on ``docs.spring.io`` (365–857 ms
    against a declared 1 s).
    """
    clock = FakeClock()
    layer = fast_layer(clock, base_delay=(0.0, 0.0))
    stamps: list[tuple[str, float]] = []

    def page_handler(request, recorder):
        stamps.append((request.url.path, clock.now()))
        return httpx.Response(200, content=JSON_HTML)

    def robots_handler(request, recorder):
        stamps.append(("/robots.txt", clock.now()))
        return httpx.Response(200, content=b"User-agent: *\ncrawl-delay: 2\n")

    use_transport(monkeypatch, Recorder({"/robots.txt": robots_handler, "*": page_handler}))
    fetch_python_module("json")
    fetch_python_module("os")

    assert [p for p, _ in stamps] == ["/robots.txt", JSON_PATH, "/3/library/os.html"], stamps
    # base_delay is (0, 0), so every wait here is the declared crawl-delay.
    assert clock.slept == [pytest.approx(2.0), pytest.approx(2.0)], clock.slept
    gaps = [b - a for (_p1, a), (_p2, b) in zip(stamps, stamps[1:])]
    assert all(g >= 2.0 for g in gaps), gaps
    assert layer.stats()["throttle_waits"] == 2
    assert layer.stats()["crawl_delay_applied"] == 1
    assert layer.stats()["throttle_sleep_s"] == pytest.approx(4.0)


def test_429_is_retried_once_after_retry_after(monkeypatch):
    clock = FakeClock()
    layer = fast_layer(clock)
    state = {"n": 0}

    def flaky(request, recorder):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "3"}, content=b"slow down")
        return httpx.Response(200, content=JSON_HTML)

    rec = use_transport(monkeypatch, Recorder({"/robots.txt": DOCS_ROBOTS, JSON_PATH: flaky}))
    out = fetch_python_module("json")

    assert out["ok"] is True
    assert rec.hits(JSON_PATH) == 2
    assert layer.stats()["retries_429"] == 1
    assert layer.stats()["retry_after_honoured"] == 1
    assert any(s >= 3.0 for s in clock.slept)


# ---------------------------------------------------------------------------
# 5. request budget and the partial index
# ---------------------------------------------------------------------------
def test_tool_budget_caps_requests_for_one_call(monkeypatch):
    layer = fast_layer()
    rec = use_transport(monkeypatch, Recorder({"/robots.txt": DOCS_ROBOTS, "*": 404}))

    with tool_budget("python_docs", limit=2):
        out = fetch_python_module("xml.etree.ElementTree")

    assert out["ok"] is False
    assert "budget exhausted" in out["error"]
    # A8 F3: a budget unit is one **request on the wire**, so on a cold host
    # unit 1 pays for robots.txt and unit 2 for the first candidate; the second
    # candidate is refused.  "limit 2" now means two real requests — before A8
    # the same limit allowed two page requests plus a free robots fetch.
    assert len(rec.page_requests()) == 1
    assert rec.hits("/robots.txt") == 1
    assert layer.stats()["budget_denied"] == 1
    assert layer.stats()["budgets"]["tool:python_docs"] == [2, 2]


def test_index_build_budget_exhaustion_yields_partial_index(monkeypatch):
    """REQUIRED: a spent budget yields a partial index, never an exception."""
    layer = fast_layer()
    use_transport(monkeypatch, Recorder({"/robots.txt": DOCS_ROBOTS, LIBRARY_INDEX_PATH: LIBRARY_INDEX_HTML}))

    with tool_budget("python_docs", limit=1):
        # Spend the only unit on a module page, then try to build the index.
        fetch_python_module("json")
        index = search_mod.build_index()

    assert index["partial"] is True
    assert index["modules"] == []
    assert "budget exhausted" in index["partial_reason"]
    # A8 F3: the single unit went to the robots.txt fetch, so BOTH the module
    # page and the index page were refused — two denials, and zero page
    # requests.  The robots fetch is no longer invisible to the budget.
    assert layer.stats()["budget_denied"] == 2
    assert layer.stats()["requests"] == 0
    assert layer.stats()["robots_requests"] == 1


def test_partial_index_is_never_cached(monkeypatch, tmp_path):
    fast_layer()
    use_transport(monkeypatch, Recorder({"/robots.txt": DOCS_ROBOTS, LIBRARY_INDEX_PATH: LIBRARY_INDEX_HTML}))
    cache = DocCache(str(tmp_path / "index.db"))

    with tool_budget("python_docs", limit=1):
        fetch_python_module("json")
        index = search_mod._build_index(cache)

    assert index.get("partial") is True
    assert cache.get(INDEX_KEY) is None


def test_get_index_prefers_a_stale_copy_over_a_partial_build(monkeypatch, tmp_path):
    import json as _json

    cache = DocCache(str(tmp_path / "index.db"))
    cache.set(INDEX_KEY, _json.dumps({"modules": [{"module": "json", "description": "JSON", "url": "u"}], "count": 1, "built_at": "2020-01-01T00:00:00+00:00"}), -1)
    fast_layer()
    use_transport(monkeypatch, Recorder({"/robots.txt": DOCS_ROBOTS, LIBRARY_INDEX_PATH: LIBRARY_INDEX_HTML}))

    with tool_budget("python_docs", limit=1):
        fetch_python_module("json")
        index = search_mod.get_index(cache=cache)

    assert index["stale"] is True
    assert index["count"] == 1


def test_index_build_itself_makes_one_request(monkeypatch):
    """The build is one page; the budget only bites when the call already spent some."""
    fast_layer()
    rec = use_transport(
        monkeypatch, Recorder({"/robots.txt": DOCS_ROBOTS, LIBRARY_INDEX_PATH: LIBRARY_INDEX_HTML})
    )
    index = search_mod.build_index()
    assert index["count"] > 150
    assert rec.hits(LIBRARY_INDEX_PATH) == 1
    assert index.get("partial") is None


# ---------------------------------------------------------------------------
# 6. conditional GET (ETag / If-None-Match → 304)
# ---------------------------------------------------------------------------
def test_conditional_get_reuses_the_stored_body(monkeypatch):
    layer = fast_layer()
    seen: list[dict] = []

    def handler(request, recorder):
        seen.append(dict(request.headers))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=JSON_HTML, headers={"etag": '"v1"'})

    use_transport(monkeypatch, Recorder({"/robots.txt": DOCS_ROBOTS, JSON_PATH: handler}))

    first = fetch_python_module("json")
    second = fetch_python_module("json")

    assert first["ok"] and second["ok"]
    assert "if-none-match" in seen[1]
    assert layer.stats()["revalidated_304"] == 1
    assert layer.stats()["conditional"] == 1


# ---------------------------------------------------------------------------
# 7. python_status: probes on allowed paths, politeness outside checks
# ---------------------------------------------------------------------------
def test_status_probes_only_robots_allowed_urls(monkeypatch):
    """The probe itself used to be ``/pypi/mcp/json`` — a robots violation."""
    use_transport(monkeypatch, Recorder({"/robots.txt": PYPI_ROBOTS}))
    layer = fast_layer()

    for _name, url in server_mod._ENDPOINT_PROBES:
        assert layer.can_fetch(url) is True, url
    assert not any("/pypi/" in url for _name, url in server_mod._ENDPOINT_PROBES)


def test_python_status_reports_politeness_outside_checks(monkeypatch):
    use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": DOCS_ROBOTS,
                LIBRARY_INDEX_PATH: LIBRARY_INDEX_HTML,
                "/3/library/json.html": JSON_HTML,
                "/project/mcp/": b"<html><body>mcp</body></html>",
            }
        ),
    )
    result = server_mod.python_status()

    assert set(result["checks"]) == {"search_index", "cache", "docs_python_org", "pypi_org"}
    assert "politeness" in result
    block = result["politeness"]
    for key in (
        "disabled",
        "requests",
        "robots_rows",
        "blocked_by_robots",
        "throttle_waits",
        "budgets",
        "conditional",
        "revalidated_304",
    ):
        assert key in block, key
    assert block["requests"] >= 1
    assert result["overall"] in ("ok", "degraded")


def test_python_status_never_probes_the_json_endpoint(monkeypatch):
    rec = use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": DOCS_ROBOTS,
                LIBRARY_INDEX_PATH: LIBRARY_INDEX_HTML,
                "/3/library/json.html": JSON_HTML,
                "/project/mcp/": b"<html><body>mcp</body></html>",
                "/pypi/mcp/json": AssertionError("python_status probed a disallowed URL"),
            }
        ),
    )
    server_mod.python_status()
    for path in rec.paths():
        assert not path.startswith("/pypi/"), path


def test_status_probes_spend_the_tool_budget(monkeypatch):
    """The probes are politeness-gated *and* budgeted — no unbudgeted side path.

    ``_probe_endpoint`` builds its own short-timeout client (a status probe
    should not inherit the 15 s page timeout), but it must still spend the tool
    call's budget; otherwise ``python_status`` adds requests behind the cap.
    """
    layer = fast_layer()
    use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": DOCS_ROBOTS,
                LIBRARY_INDEX_PATH: LIBRARY_INDEX_HTML,
                "/3/library/json.html": JSON_HTML,
                "/project/mcp/": b"<html><body>mcp</body></html>",
            }
        ),
    )
    server_mod.python_status()

    used, limit = layer.stats()["budgets"]["tool:python_status"]
    # A8 F3: the robots fetches are paid for now.  docs.python.org robots (1) +
    # library index page (1) + json probe (1, robots already cached) + pypi.org
    # robots (1) + pypi probe (1) = 5 units for 5 real requests.
    assert used == 5
    assert limit == server_mod.STATUS_BUDGET_LIMIT
    st = layer.stats()
    assert st["requests"] == 3 and st["robots_requests"] == 2
    # A7 §4 warning: a probe that hits the cap reports ``error`` and drags
    # ``overall`` to "degraded" for a purely internal reason.  The limit must
    # leave headroom, so nothing here may be denied.
    assert st["budget_denied"] == 0
    assert used < limit


# ---------------------------------------------------------------------------
# 8. structural guards
# ---------------------------------------------------------------------------
def test_no_subprocess_or_curl_transport_left_in_the_sources():
    """A subprocess transport would bypass robots, throttle and budget entirely.

    The words may appear in comments explaining why the fallback was removed;
    the *code* must not contain one.
    """
    src_dir = Path(fetchers_mod.__file__).parent
    for name in ("fetchers.py", "server.py", "search.py", "cache.py"):
        text = (src_dir / name).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        assert "import subprocess" not in code, name
        assert "subprocess." not in code, name
        assert '["curl"' not in code, name


def test_fetchers_never_build_a_client_that_bypasses_the_layer():
    """Only the documented opt-in path may call ``client.get`` directly."""
    src = Path(fetchers_mod.__file__).read_text(encoding="utf-8")
    assert src.count("client.get(") == 1, "exactly one direct client.get: the opt-in JSON path"
    assert "get_politeness().get(" in src


def test_politeness_report_keys_are_real_counters():
    """Guard against the drift that makes a counter permanently read 0.

    Every reported counter name must exist in ``Politeness.stats()`` — a typo
    like ``robots_fetched`` (vs ``robots_requests``) would otherwise report a
    silent zero forever, which is how a new counter (F1/F2/F4) can look wired
    while never moving.
    """
    stats = Politeness("ua", cache_path=":memory:").stats()
    report = server_mod._politeness_report()
    derived = {"status", "robots_rows", "host_delays", "budgets"}
    reported = [key for key in report if key not in derived]
    assert reported, "report is empty"
    for key in reported:
        assert key in stats, key
    # The counters A8 added must be reported, not just counted internally.
    for key in (
        "robots_requests",
        "robots_throttle_waits",
        "robots_throttle_sleep_s",
        "redirect_hops",
        "challenge_detected",
        "challenge_retries",
    ):
        assert key in report, key


# ---------------------------------------------------------------------------
# A13 B3 — an unwritable cache directory must never take a tool down
# ---------------------------------------------------------------------------
def test_pypi_package_and_status_survive_an_unwritable_cache_dir(monkeypatch, tmp_path):
    """A12 F-A12-2: the cache is optional end-to-end in this repo too.

    A bad cache directory must degrade ``pypi_package`` to "no cache", never to
    an exception, and ``python_status`` must say so.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where a directory should be")
    monkeypatch.setenv("PYTHON_DOCS_MCP_CACHE_DIR", str(blocker / "cache"))

    use_transport(monkeypatch, Recorder({"/robots.txt": PYPI_ROBOTS, ATTRS_PATH: ATTRS_HTML}))
    out = server_mod.pypi_package("attrs")
    assert out.get("ok") is True, out
    assert out.get("name") == "attrs"

    monkeypatch.setattr(
        server_mod, "get_index",
        lambda: {"modules": [{"name": "json"}], "count": 1, "built_at": "x"},
    )
    use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": DOCS_ROBOTS,
                LIBRARY_INDEX_PATH: LIBRARY_INDEX_HTML,
                "/3/library/json.html": JSON_HTML,
                "/project/mcp/": b"<html><body>mcp</body></html>",
            }
        ),
    )
    status = server_mod.python_status()
    assert status["checks"]["cache"]["status"] == "error", status["checks"]["cache"]
    assert "error" in status["checks"]["cache"]     # the reason is spelled out
    assert status["overall"] == "degraded"          # announced, not hidden
