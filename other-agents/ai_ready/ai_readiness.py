#!/usr/bin/env python3
"""
ai_readiness.py — Simple CLI port of the n8n "ai_readiness_reporter" workflow.

Generates an AI-Readiness Report for a URL using a single HTTP fetch (no
JavaScript rendering, no headless browser) and Claude for scoring. Trades
multi-page coverage for a zero-dependency setup that runs cleanly on small
VMs (t2.small etc.).

Pipeline:
  1. Fetch the URL with httpx (single GET, no JS)
  2. Parse the HTML with BeautifulSoup → structured features
  3. Two parallel Claude calls:
       a. Readability scoring on the parsed features
       b. Accessibility scoring on the raw HTML + response headers
  4. Deterministic combine: readability * 0.6 + accessibility * 0.4
  5. Claude generates the final markdown report

Required:
  ANTHROPIC_API_KEY (env var, or paste into the CONFIG block below)

Optional:
  ANTHROPIC_MODEL (default: claude-sonnet-4-5-20250929)

Usage:
  python ai_readiness.py https://example.com
  python ai_readiness.py https://example.com -o report.md
  python ai_readiness.py https://example.com --json data.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from anthropic import Anthropic
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIG — paste your key here, or leave blank to use the env var.
# ---------------------------------------------------------------------------
# WARNING: if you commit this file to git, your key goes with it.
ANTHROPIC_API_KEY = "REDACTED"   # e.g. "sk-ant-api03-..."

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
RAW_HTML_CHAR_LIMIT = 80_000  # cap what we send to Claude
RAW_FETCH_HEADERS = {
    "User-Agent": "ai-readiness-cli/1.0 (+https://github.com/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
NOISY_HEADERS = {"set-cookie", "date", "expires", "age", "cache-control", "etag"}


def _resolve(hardcoded: str, env_var: str) -> str | None:
    """Hardcoded value wins if set; otherwise fall back to the env var."""
    return hardcoded.strip() or os.getenv(env_var) or None


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

READABILITY_SYSTEM = """You are evaluating a single web page for AI-readability — i.e. how well an AI agent can parse, navigate, and extract information from this page's content and structure.

You will receive structured features extracted from the page:
- Title and main headings
- Body content preview (first 200 chars of visible text)
- Word count, heading count, link counts, internal link count
- Whether structured data (schema.org / JSON-LD / microdata) was detected

Score on these four dimensions:

1. **Content Structure** (0-3 points)
   - Clear heading hierarchy (H1, H2, H3...)
   - Logical organization, semantic structure
   - Headings actually convey what their sections contain

2. **Information Density** (0-3 points)
   - Substantive content (not thin or boilerplate)
   - Balance of meaningful content vs navigation/footer cruft
   - Clear topic focus

3. **Link Structure** (0-2 points)
   - Reasonable internal linking (suggests a navigable site)
   - Not so many links that the page is mostly nav
   - Anchor text appears descriptive (judge from headings)

4. **Metadata Quality** (0-2 points)
   - Meaningful, descriptive page title
   - Headings are descriptive, not generic
   - Structured data presence (schema.org / JSON-LD)

**Scoring guidelines:**
- High (8-10): excellent structure, would be easy for an AI to parse
- Medium (5-7): decent but with notable gaps
- Low (0-4): poor structure, difficult for AI to use

Output ONLY a JSON object — no prose, no markdown fences — matching this schema exactly:

{
  "summary": "string",
  "score": 0.0,
  "breakdown": {
    "content_structure": 0.0,
    "information_density": 0.0,
    "link_structure": 0.0,
    "metadata_quality": 0.0
  },
  "strengths": ["string", ...],
  "weaknesses": ["string", ...],
  "recommendations": ["string", ...]
}"""


ACCESSIBILITY_SYSTEM = """You are evaluating a website's accessibility and usability for AI agents, web scrapers, and automation tools. Your analysis should work for ANY type of website.

You will be given:
  • The URL
  • HTTP status code and response headers from a fetch with NO JavaScript executed
  • The raw HTML body returned by that fetch (possibly truncated)

This represents what a basic bot, search crawler, or non-JS agent sees.

**Evaluation Tasks:**

1. **Information Discovery & Clarity** (0-3 points)
   - Can you find the site's primary purpose and offerings from the raw HTML?
   - Are contact details, location, or key action points present in the markup?
   - Are titles, descriptions, and headings descriptive enough to identify the page?

2. **Structured Data & Machine Readability** (0-3 points)
   - Does the HTML contain schema.org markup, JSON-LD blocks, or microdata?
   - Are there Open Graph or Twitter Card meta tags?
   - Is contact info structured (vCard, schema.org/Organization, mailto/tel links)?

3. **Automation & Bot Friendliness** (0-2 points)
   - Does the raw HTML contain real page content, or is it just a JavaScript shell?
   - Are there CAPTCHA, Cloudflare-challenge, or anti-bot indicators in the HTML or headers?
   - Do response headers (X-Robots-Tag, restrictive CSP) limit automated access?

4. **Overall Usability for AI Agents** (0-2 points)
   - Could an AI answer user questions about this site from the markup alone?
   - Are there major obstacles (JS-only content, missing semantic tags) to AI comprehension?

**CRITICAL: Output Format Requirements**

You MUST include a breakdown section with EXACT numeric scores in this format:

Breakdown:

Information Discovery & Clarity: X.X/3
Structured Data & Machine Readability: X.X/3
Automation & Bot Friendliness: X.X/2
Overall Usability for AI Agents: X.X/2

Where X.X is your numeric score (e.g., 2.5, 3.0, 1.5).

Then provide:
- Strengths: what makes this site accessible to AI agents
- Challenges: what obstacles exist
- Recommendations: specific, actionable improvements"""


REPORT_SYSTEM_TEMPLATE = """Create a professional AI-Readiness Report in markdown.

**CRITICAL: Use these EXACT scores (already calculated):**
- Overall: {overall}/10
- Readability: {readability}/10 (60% weight)
- AI Accessibility: {accessibility}/10 (40% weight)
- Show formula: ({readability} × 0.6) + ({accessibility} × 0.4) = {overall}/10

**Structure:**
1. Executive Summary (3-4 sentences)
2. Overall Score + Breakdown
3. Readability Analysis (score, strengths, weaknesses, breakdown table)
4. AI Accessibility Analysis (score, strengths, weaknesses, breakdown table)
5. Recommendations (High/Medium/Low priority - 2-3 per section)
6. Technical Details

**Adapt to site type** (e-commerce, services, SaaS, etc.) and focus
recommendations on lowest-scoring dimensions. Note that this analysis is
based on a single page (the URL provided), not a full crawl.

**IMPORTANT:** Complete the entire report. Do not truncate."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FetchedPage:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    html: str


@dataclass
class PageFeatures:
    url: str
    title: str
    headings: list[str]
    content_preview: str
    word_count: int
    heading_count: int
    link_count: int
    internal_link_count: int
    has_structured_data: bool


@dataclass
class ReadabilityResult:
    summary: str
    score: float
    breakdown: dict[str, float]
    strengths: list[str]
    weaknesses: list[str]
    recommendations: list[str]


@dataclass
class AccessibilityResult:
    score: float
    breakdown: dict[str, float]
    raw_analysis: str
    flags: dict[str, bool] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# URL cleaning
# ---------------------------------------------------------------------------

def clean_url(raw: str) -> str:
    """Normalize to https:// form, stripping trailing slash."""
    raw = raw.strip()
    if raw.startswith("http://"):
        base = raw[7:]
    elif raw.startswith("https://"):
        base = raw[8:]
    else:
        base = raw
    return f"https://{base.rstrip('/')}"


# ---------------------------------------------------------------------------
# Single-page fetch
# ---------------------------------------------------------------------------

async def fetch_page(url: str) -> FetchedPage:
    """One HTTP GET, no JS rendering. The whole 'crawl' is now this."""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers=RAW_FETCH_HEADERS)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc

    if resp.status_code >= 400:
        raise RuntimeError(
            f"Fetched {url} but got HTTP {resp.status_code}. "
            f"Cannot analyze — the site is returning an error response."
        )

    headers = {k: v for k, v in resp.headers.items() if k.lower() not in NOISY_HEADERS}

    return FetchedPage(
        url=url,
        final_url=str(resp.url),
        status=resp.status_code,
        headers=headers,
        html=resp.text or "",
    )


# ---------------------------------------------------------------------------
# HTML feature extraction (BeautifulSoup, no browser)
# ---------------------------------------------------------------------------

def parse_features(page: FetchedPage) -> PageFeatures:
    """Pull structured features out of the raw HTML for the readability pass."""
    soup = BeautifulSoup(page.html, "html.parser")

    title_tag = soup.find("title")
    h1_tag = soup.find("h1")
    title = (
        (title_tag.get_text(strip=True) if title_tag else None)
        or (h1_tag.get_text(strip=True) if h1_tag else None)
        or "Untitled"
    )

    heading_tags = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    headings = [h.get_text(strip=True) for h in heading_tags if h.get_text(strip=True)]

    # Strip non-content elements before extracting visible text
    body_soup = BeautifulSoup(page.html, "html.parser")
    for tag in body_soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = body_soup.get_text(separator=" ", strip=True)
    words = text.split()
    content_preview = text[:200]

    all_links = soup.find_all("a", href=True)
    internal_links = [
        a for a in all_links
        if not a["href"].startswith(("http://", "https://", "//", "mailto:", "tel:"))
    ]

    has_structured = bool(
        soup.find("script", attrs={"type": "application/ld+json"})
        or soup.find(attrs={"itemscope": True})
        or "schema.org" in page.html.lower()
    )

    return PageFeatures(
        url=page.url,
        title=title,
        headings=headings[:5],
        content_preview=content_preview,
        word_count=len(words),
        heading_count=len(headings),
        link_count=len(all_links),
        internal_link_count=len(internal_links),
        has_structured_data=has_structured,
    )


# ---------------------------------------------------------------------------
# Claude scoring: readability
# ---------------------------------------------------------------------------

def analyze_readability(client: Anthropic, features: PageFeatures) -> ReadabilityResult:
    user = (
        f"Analyze this page's features for AI-readability:\n\n"
        f"{json.dumps(features.__dict__, indent=2)}"
    )

    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        system=READABILITY_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    parsed = _parse_json_loose(raw)

    breakdown = parsed.get("breakdown") or {}
    # Score is recomputed deterministically from breakdown values
    score = round(
        float(breakdown.get("content_structure", 0))
        + float(breakdown.get("information_density", 0))
        + float(breakdown.get("link_structure", 0))
        + float(breakdown.get("metadata_quality", 0)),
        2,
    )

    return ReadabilityResult(
        summary=str(parsed.get("summary", "")),
        score=score,
        breakdown={k: float(v) for k, v in breakdown.items()},
        strengths=list(parsed.get("strengths", [])),
        weaknesses=list(parsed.get("weaknesses", [])),
        recommendations=list(parsed.get("recommendations", [])),
    )


def _parse_json_loose(text: str) -> dict[str, Any]:
    """Strip optional ```json fences and parse, tolerating prose around the JSON."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


# ---------------------------------------------------------------------------
# Claude scoring: accessibility
# ---------------------------------------------------------------------------

def analyze_accessibility(client: Anthropic, page: FetchedPage) -> AccessibilityResult:
    truncated = len(page.html) > RAW_HTML_CHAR_LIMIT
    html_for_prompt = page.html[:RAW_HTML_CHAR_LIMIT]

    user = (
        f"Analyze this website's accessibility for AI agents and automation tools.\n\n"
        f"URL: {page.url}\n"
        f"HTTP status: {page.status}\n"
        f"Final URL after redirects: {page.final_url}\n"
        f"Response headers: {json.dumps(page.headers, default=str)}\n\n"
        f"Raw HTML body{' (truncated)' if truncated else ''}:\n"
        f"```html\n{html_for_prompt}\n```\n\n"
        f"Provide a comprehensive assessment with exact numeric scores for "
        f"all four dimensions. Adapt your analysis to the site's specific "
        f"type and purpose."
    )

    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        system=ACCESSIBILITY_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()

    breakdown = _parse_accessibility_breakdown(text)
    score = round(sum(breakdown.values()), 2)

    lower = text.lower()
    flags = {
        "information_clear": "clear" in lower or "easy" in lower,
        "structured_data_present": "schema" in lower or "json-ld" in lower,
        "automation_friendly": "accessible" in lower or "friendly" in lower,
        "ai_optimized": "ai" in lower or "agent" in lower,
    }

    return AccessibilityResult(score=score, breakdown=breakdown, raw_analysis=text, flags=flags)


def _parse_accessibility_breakdown(text: str) -> dict[str, float]:
    patterns = {
        "information_discovery": r"Information Discovery & Clarity:\s*([\d.]+)\s*/\s*3",
        "structured_data": r"Structured Data & Machine Readability:\s*([\d.]+)\s*/\s*3",
        "automation_friendliness": r"Automation & Bot Friendliness:\s*([\d.]+)\s*/\s*2",
        "ai_usability": r"Overall Usability for AI Agents:\s*([\d.]+)\s*/\s*2",
    }
    out: dict[str, float] = {}
    for key, pat in patterns.items():
        m = re.search(pat, text, re.IGNORECASE)
        out[key] = float(m.group(1)) if m else 0.0
    return out


# ---------------------------------------------------------------------------
# Combine scores + final report
# ---------------------------------------------------------------------------

def overall_score(readability: float, accessibility: float) -> float:
    return round(readability * 0.6 + accessibility * 0.4, 1)


def generate_report(
    client: Anthropic,
    url: str,
    readability: ReadabilityResult,
    accessibility: AccessibilityResult,
    overall: float,
) -> str:
    system = REPORT_SYSTEM_TEMPLATE.format(
        overall=overall,
        readability=readability.score,
        accessibility=accessibility.score,
    )
    payload = {
        "url": url,
        "overall_score": overall,
        "readability": {
            "summary": readability.summary,
            "score": readability.score,
            "breakdown": readability.breakdown,
            "strengths": readability.strengths,
            "weaknesses": readability.weaknesses,
            "recommendations": readability.recommendations,
        },
        "ai_accessibility": {
            "score": accessibility.score,
            "breakdown": accessibility.breakdown,
            "ai_accessibility": accessibility.flags,
            "analysis": accessibility.raw_analysis,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    user = (
        f"Create a comprehensive AI-Readiness Report using this data:\n\n"
        f"{json.dumps(payload, indent=2)}"
    )

    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=8192,
        temperature=0.4,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    anthropic_key = _resolve(ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY")
    if not anthropic_key:
        print(
            f"Error: missing ANTHROPIC_API_KEY.\n"
            f"Either paste it into the CONFIG block at the top of "
            f"{Path(__file__).name}, or export it as an env var.",
            file=sys.stderr,
        )
        return 2

    url = clean_url(args.url)
    log = lambda msg: print(f"[ai-readiness] {msg}", file=sys.stderr)
    log(f"target: {url}")

    anthropic_client = Anthropic(api_key=anthropic_key)

    # 1. Single fetch.
    log("fetching page...")
    try:
        page = await fetch_page(url)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    log(f"got HTTP {page.status}, {len(page.html)} bytes of HTML")

    # 2. Parse features (cheap, sync).
    features = parse_features(page)
    log(f"parsed: title={features.title!r}, {features.heading_count} headings, "
        f"{features.link_count} links, structured_data={features.has_structured_data}")

    # 3. Two Claude calls, in parallel.
    log("scoring readability + accessibility in parallel (Claude)...")
    task_a = asyncio.create_task(asyncio.to_thread(analyze_readability, anthropic_client, features))
    task_b = asyncio.create_task(asyncio.to_thread(analyze_accessibility, anthropic_client, page))
    try:
        readability, accessibility = await asyncio.gather(task_a, task_b)
    except BaseException as exc:
        for t in (task_a, task_b):
            if not t.done():
                t.cancel()
        await asyncio.gather(task_a, task_b, return_exceptions=True)
        if isinstance(exc, RuntimeError):
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        raise

    log(f"readability: {readability.score}/10")
    log(f"accessibility: {accessibility.score}/10")

    overall = overall_score(readability.score, accessibility.score)
    log(f"overall: {overall}/10")

    # 4. Final report.
    log("generating final report...")
    report = generate_report(anthropic_client, url, readability, accessibility, overall)

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        log(f"wrote report to {args.output}")
    else:
        print(report)

    if args.json:
        Path(args.json).write_text(json.dumps({
            "url": url,
            "overall_score": overall,
            "readability": readability.__dict__,
            "ai_accessibility": {
                "score": accessibility.score,
                "breakdown": accessibility.breakdown,
                "flags": accessibility.flags,
                "analysis": accessibility.raw_analysis,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, indent=2), encoding="utf-8")
        log(f"wrote raw JSON to {args.json}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate an AI-Readiness Report for a single URL.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("url", help="Target website URL")
    parser.add_argument("-o", "--output", help="Write the markdown report to this file (otherwise stdout)")
    parser.add_argument("--json", help="Also write raw analysis data as JSON to this file")
    args = parser.parse_args()

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())