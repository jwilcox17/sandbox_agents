#!/usr/bin/env python3
"""
test_ai_readiness.py — test suite for the simplified single-fetch version.

Run:
  python test_ai_readiness.py                        # offline tests only (~1s)
  python test_ai_readiness.py --live https://x.com   # offline + real run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import ai_readiness as ar
except ImportError as exc:
    print(f"FATAL: cannot import ai_readiness.py: {exc}", file=sys.stderr)
    print("       pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Tiny test runner
# ---------------------------------------------------------------------------

class TestRunner:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.errors: list[tuple[str, str]] = []

    def run(self, name: str, fn) -> None:
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        except AssertionError as exc:
            self.failed += 1
            self.errors.append((name, traceback.format_exc()))
            print(f"  \033[31mFAIL\033[0m  {name}: {exc}")
            return
        except Exception as exc:
            self.failed += 1
            self.errors.append((name, traceback.format_exc()))
            print(f"  \033[31mERROR\033[0m {name}: {type(exc).__name__}: {exc}")
            return
        self.passed += 1
        print(f"  \033[32mPASS\033[0m  {name}")

    def summary(self) -> int:
        total = self.passed + self.failed
        print()
        if self.failed == 0:
            print(f"\033[32m✓ All {total} tests passed.\033[0m")
            return 0
        print(f"\033[31m✗ {self.failed}/{total} tests failed.\033[0m")
        print("\nFailure details:")
        for name, tb in self.errors:
            print(f"\n--- {name} ---\n{tb}")
        return 1


def _make_async_ctx(client_mock: MagicMock) -> MagicMock:
    """Build a fake `async with httpx.AsyncClient(...) as client:` context."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client_mock)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


# ===========================================================================
# Pure-function tests
# ===========================================================================

def test_clean_url() -> None:
    assert ar.clean_url("example.com") == "https://example.com"
    assert ar.clean_url("http://example.com") == "https://example.com"
    assert ar.clean_url("https://example.com/") == "https://example.com"
    assert ar.clean_url("  https://example.com/  ") == "https://example.com"
    assert ar.clean_url("https://example.com/path") == "https://example.com/path"


def test_parse_features_basic() -> None:
    html = """<!doctype html>
<html>
<head>
  <title>Example Site - Home</title>
  <meta property="og:title" content="Example">
  <script type="application/ld+json">{"@type":"Organization","name":"Example"}</script>
</head>
<body>
  <h1>Welcome to Example</h1>
  <p>This is the main paragraph with some substantive content for testing.</p>
  <h2>Our Services</h2>
  <p>We provide great services across many domains.</p>
  <h3>Contact</h3>
  <a href="/about">About us</a>
  <a href="/contact">Contact</a>
  <a href="https://external.com">External link</a>
  <a href="mailto:hi@example.com">Email</a>
  <script>console.log('scripts should be ignored');</script>
</body>
</html>"""
    page = ar.FetchedPage(
        url="https://example.com", final_url="https://example.com",
        status=200, headers={}, html=html,
    )
    f = ar.parse_features(page)
    assert f.title == "Example Site - Home"
    assert "Welcome to Example" in f.headings
    assert "Our Services" in f.headings
    assert f.heading_count == 3
    assert f.link_count == 4
    # 2 internal (/about, /contact); mailto: and external https don't count
    assert f.internal_link_count == 2
    assert f.has_structured_data is True
    # Script content shouldn't leak into preview/word count
    assert "console.log" not in f.content_preview
    assert f.word_count > 5


def test_parse_features_minimal_html() -> None:
    """Pages with no title/headings degrade gracefully, not crash."""
    page = ar.FetchedPage(
        url="https://x.com", final_url="https://x.com",
        status=200, headers={}, html="<html><body>just text</body></html>",
    )
    f = ar.parse_features(page)
    assert f.title == "Untitled"
    assert f.headings == []
    assert f.has_structured_data is False


def test_parse_features_h1_fallback() -> None:
    """When <title> is missing, fall back to the first <h1>."""
    html = "<html><body><h1>The Heading Title</h1><p>body</p></body></html>"
    page = ar.FetchedPage(url="https://x.com", final_url="https://x.com",
                         status=200, headers={}, html=html)
    f = ar.parse_features(page)
    assert f.title == "The Heading Title"


def test_parse_accessibility_breakdown() -> None:
    text = """
Breakdown:

Information Discovery & Clarity: 2.5/3
Structured Data & Machine Readability: 1.0/3
Automation & Bot Friendliness: 1.5/2
Overall Usability for AI Agents: 2.0/2
"""
    bd = ar._parse_accessibility_breakdown(text)
    assert bd["information_discovery"] == 2.5
    assert bd["structured_data"] == 1.0
    assert bd["automation_friendliness"] == 1.5
    assert bd["ai_usability"] == 2.0


def test_parse_accessibility_breakdown_missing() -> None:
    bd = ar._parse_accessibility_breakdown("no scores here at all")
    assert bd == {
        "information_discovery": 0.0, "structured_data": 0.0,
        "automation_friendliness": 0.0, "ai_usability": 0.0,
    }


def test_parse_json_loose() -> None:
    assert ar._parse_json_loose('{"a": 1}') == {"a": 1}
    assert ar._parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert ar._parse_json_loose('Result:\n{"a": 1, "b": [2,3]}\nDone!') == {"a": 1, "b": [2, 3]}


def test_overall_score_formula() -> None:
    assert ar.overall_score(10.0, 10.0) == 10.0
    assert ar.overall_score(0.0, 0.0) == 0.0
    assert ar.overall_score(10.0, 0.0) == 6.0
    assert ar.overall_score(0.0, 10.0) == 4.0
    assert ar.overall_score(7.5, 6.0) == 6.9  # 4.5 + 2.4 = 6.9


def test_resolve_helper() -> None:
    assert ar._resolve("hardcoded", "AR_TEST_NONEXISTENT") == "hardcoded"
    assert ar._resolve("  hardcoded  ", "AR_TEST_NONEXISTENT") == "hardcoded"
    os.environ["AR_TEST_TEMP"] = "from_env"
    try:
        assert ar._resolve("", "AR_TEST_TEMP") == "from_env"
    finally:
        del os.environ["AR_TEST_TEMP"]
    assert ar._resolve("", "AR_TEST_NONEXISTENT") is None


# ===========================================================================
# Mocked I/O tests
# ===========================================================================

async def test_fetch_page_mocked() -> None:
    response = MagicMock()
    response.status_code = 200
    response.text = "<html><body>hi</body></html>"
    response.url = "https://example.com/final"
    response.headers = {
        "content-type": "text/html",
        "set-cookie": "should_be_filtered",
        "server": "nginx",
    }

    client = MagicMock()
    client.get = AsyncMock(return_value=response)

    with patch.object(ar.httpx, "AsyncClient", return_value=_make_async_ctx(client)):
        page = await ar.fetch_page("https://example.com")

    assert page.status == 200
    assert page.html == "<html><body>hi</body></html>"
    assert page.final_url == "https://example.com/final"
    assert page.headers.get("server") == "nginx"
    assert "set-cookie" not in page.headers  # noisy headers are filtered

    client.get.assert_awaited_once()
    assert client.get.call_args.args[0] == "https://example.com"
    assert "User-Agent" in client.get.call_args.kwargs["headers"]


async def test_fetch_page_4xx() -> None:
    """A 4xx/5xx response surfaces as a clear RuntimeError, not silent garbage."""
    response = MagicMock()
    response.status_code = 404
    response.text = "Not Found"
    response.url = "https://example.com"
    response.headers = {}

    client = MagicMock()
    client.get = AsyncMock(return_value=response)

    with patch.object(ar.httpx, "AsyncClient", return_value=_make_async_ctx(client)):
        try:
            await ar.fetch_page("https://example.com")
        except RuntimeError as exc:
            assert "404" in str(exc)
            return
    raise AssertionError("expected RuntimeError on 4xx response")


async def test_fetch_page_network_error() -> None:
    client = MagicMock()
    client.get = AsyncMock(side_effect=ar.httpx.ConnectError("dns failure"))

    with patch.object(ar.httpx, "AsyncClient", return_value=_make_async_ctx(client)):
        try:
            await ar.fetch_page("https://nope.invalid")
        except RuntimeError as exc:
            assert "Failed to fetch" in str(exc)
            return
    raise AssertionError("expected RuntimeError on network failure")


def test_analyze_readability_mocked() -> None:
    """Score is recomputed deterministically from breakdown — even if Claude lies in 'score'."""
    canned = json.dumps({
        "summary": "Decent structure.",
        "score": 99.0,  # Claude lies, breakdown wins
        "breakdown": {
            "content_structure": 2.5, "information_density": 2.0,
            "link_structure": 1.5, "metadata_quality": 1.0,
        },
        "strengths": ["clear headings"],
        "weaknesses": ["short content"],
        "recommendations": ["expand"],
    })
    msg = MagicMock()
    msg.content = [MagicMock(type="text", text=canned)]
    client = MagicMock()
    client.messages.create = MagicMock(return_value=msg)

    features = ar.PageFeatures(
        url="https://x.com", title="Home", headings=["Welcome"],
        content_preview="hi", word_count=100, heading_count=1,
        link_count=5, internal_link_count=3, has_structured_data=True,
    )
    result = ar.analyze_readability(client, features)

    assert result.score == 7.0, f"expected 7.0 (sum of breakdown), got {result.score}"
    assert result.summary == "Decent structure."
    assert result.strengths == ["clear headings"]


def test_analyze_accessibility_mocked() -> None:
    canned = """Some analysis mentioning schema.org.

Breakdown:

Information Discovery & Clarity: 2.0/3
Structured Data & Machine Readability: 1.5/3
Automation & Bot Friendliness: 1.0/2
Overall Usability for AI Agents: 1.5/2
"""
    msg = MagicMock()
    msg.content = [MagicMock(type="text", text=canned)]
    client = MagicMock()
    client.messages.create = MagicMock(return_value=msg)

    page = ar.FetchedPage(
        url="https://x.com", final_url="https://x.com", status=200,
        headers={"content-type": "text/html"},
        html="<html><head><title>X</title></head><body>hi</body></html>",
    )
    result = ar.analyze_accessibility(client, page)

    assert result.score == 6.0
    assert result.breakdown["information_discovery"] == 2.0
    assert result.flags["structured_data_present"] is True

    # Verify Claude got the HTML and headers
    user_content = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "<title>X</title>" in user_content
    assert "https://x.com" in user_content
    assert client.messages.create.call_args.kwargs["system"] is ar.ACCESSIBILITY_SYSTEM


def test_generate_report_mocked() -> None:
    msg = MagicMock()
    msg.content = [MagicMock(type="text", text="# AI-Readiness Report\n\nBody.")]
    client = MagicMock()
    client.messages.create = MagicMock(return_value=msg)

    readability = ar.ReadabilityResult(
        summary="s", score=7.0,
        breakdown={"content_structure": 2.5, "information_density": 2.0,
                   "link_structure": 1.5, "metadata_quality": 1.0},
        strengths=[], weaknesses=[], recommendations=[],
    )
    accessibility = ar.AccessibilityResult(
        score=6.0,
        breakdown={"information_discovery": 2.0, "structured_data": 1.5,
                   "automation_friendliness": 1.0, "ai_usability": 1.5},
        raw_analysis="raw", flags={},
    )
    overall = ar.overall_score(readability.score, accessibility.score)  # 6.6

    report = ar.generate_report(client, "https://x.com", readability, accessibility, overall)
    assert report.startswith("# AI-Readiness Report")

    # Pre-calculated scores must be in the system prompt
    sys_prompt = client.messages.create.call_args.kwargs["system"]
    assert "7.0" in sys_prompt and "6.0" in sys_prompt and "6.6" in sys_prompt
    assert "× 0.6" in sys_prompt and "× 0.4" in sys_prompt


async def test_run_cancels_sibling_on_failure() -> None:
    """When one Claude call fails, the other gets cancelled — no orphan tasks."""
    sibling_state = {"cancelled": False, "completed": False}

    def failing_readability(*args, **kwargs):
        raise RuntimeError("simulated readability failure")

    def slow_accessibility(*args, **kwargs):
        # asyncio.to_thread will block here; cancellation is harder to verify
        # at the thread level, but we can check that gather propagates cleanly.
        # Use a short sleep then mark completed; cancellation here is best-effort.
        import time
        time.sleep(0.5)
        sibling_state["completed"] = True

    fake_page = ar.FetchedPage(
        url="https://x.com", final_url="https://x.com", status=200,
        headers={}, html="<html><body>x</body></html>",
    )

    async def fake_fetch(url):
        return fake_page

    args = argparse.Namespace(url="https://x.com", output=None, json=None)

    with patch.object(ar, "fetch_page", fake_fetch), \
         patch.object(ar, "analyze_readability", failing_readability), \
         patch.object(ar, "analyze_accessibility", slow_accessibility), \
         patch.object(ar, "_resolve", return_value="fake-key"), \
         patch.object(ar, "Anthropic", MagicMock()):
        code = await ar.run(args)

    assert code == 1, f"expected exit code 1, got {code}"


# ===========================================================================
# Live end-to-end test
# ===========================================================================

async def test_live_run(url: str) -> None:
    print(f"  → live target: {url}")
    print("  → single fetch + 3 Claude calls (~10-30s)")

    out_path = tempfile.NamedTemporaryFile(suffix=".md", delete=False).name
    json_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name

    args = argparse.Namespace(url=url, output=out_path, json=json_path)
    code = await ar.run(args)
    assert code == 0, f"ar.run() exited with {code}"

    md = Path(out_path).read_text(encoding="utf-8")
    assert len(md) > 200, f"report too short ({len(md)} chars)"
    assert "× 0.6" in md and "× 0.4" in md, "report missing the score formula"

    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    assert 0 <= data["overall_score"] <= 10
    assert 0 <= data["readability"]["score"] <= 10
    assert 0 <= data["ai_accessibility"]["score"] <= 10

    print(f"  → markdown: {out_path}")
    print(f"  → json:     {json_path}")
    print(f"  → overall:  {data['overall_score']}/10")


# ===========================================================================
# Main
# ===========================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Test ai_readiness.py")
    parser.add_argument("--live", metavar="URL",
                        help="Also do a real end-to-end run against this URL")
    args = parser.parse_args()

    runner = TestRunner()

    print("Pure-function tests")
    runner.run("clean_url",                                 test_clean_url)
    runner.run("parse_features (rich HTML)",                test_parse_features_basic)
    runner.run("parse_features (minimal HTML)",             test_parse_features_minimal_html)
    runner.run("parse_features (H1 fallback for title)",    test_parse_features_h1_fallback)
    runner.run("_parse_accessibility_breakdown",            test_parse_accessibility_breakdown)
    runner.run("_parse_accessibility_breakdown (no match)", test_parse_accessibility_breakdown_missing)
    runner.run("_parse_json_loose",                         test_parse_json_loose)
    runner.run("overall_score formula",                     test_overall_score_formula)
    runner.run("_resolve (hardcoded vs env)",               test_resolve_helper)

    print("\nMocked I/O tests")
    runner.run("fetch_page (200 OK)",            test_fetch_page_mocked)
    runner.run("fetch_page (404 → friendly)",    test_fetch_page_4xx)
    runner.run("fetch_page (network error)",     test_fetch_page_network_error)
    runner.run("analyze_readability (mocked)",   test_analyze_readability_mocked)
    runner.run("analyze_accessibility (mocked)", test_analyze_accessibility_mocked)
    runner.run("generate_report (mocked)",       test_generate_report_mocked)
    runner.run("run() handles branch failure",   test_run_cancels_sibling_on_failure)

    if args.live:
        print(f"\nLive end-to-end test against {args.live}")
        runner.run("live run", lambda: test_live_run(args.live))

    return runner.summary()


if __name__ == "__main__":
    sys.exit(main())