#!/usr/bin/env python3
"""
SEO Analysis Agent
Usage:
  python seo_agent.py https://example.com
  python seo_agent.py https://example.com --output report.md
  python seo_agent.py https://example.com --no-gsc
"""

import argparse
import asyncio
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

import anthropic
import httpx
import trafilatura
from trafilatura.sitemaps import sitemap_search

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = "REDACTED"
GSC_TOKEN_FILE    = "gsc_token.json"
GSC_SCOPES        = ["https://www.googleapis.com/auth/webmasters.readonly"]

_anthropic = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"  {msg}", file=sys.stderr)


def clean_url(raw: str) -> dict:
    raw = raw.strip().rstrip("/")
    base = raw.removeprefix("https://").removeprefix("http://")
    return {"https": f"https://{base}", "no_protocol": base}


# ── Crawl ─────────────────────────────────────────────────────────────────────

def _fetch_page(url: str) -> dict | None:
    try:
        html = trafilatura.fetch_url(url)
        if not html:
            return None
        text = trafilatura.extract(html, output_format="markdown", include_links=True, include_tables=True)
        if not text or len(text.strip()) < 100:
            return None
        return {"url": url, "markdown": text}
    except Exception:
        return None


async def crawl_site(base_url: str, max_pages: int = 20) -> list[dict]:
    urls = []
    try:
        found = sitemap_search(base_url)
        if found:
            base_domain = urlparse(base_url).netloc
            urls = [u for u in found if urlparse(u).netloc == base_domain][:max_pages]
    except Exception:
        pass

    if base_url not in urls:
        urls.insert(0, base_url)

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = await asyncio.gather(*[loop.run_in_executor(pool, _fetch_page, u) for u in urls])

    return [r for r in results if r is not None]


# ── Google Search Console ─────────────────────────────────────────────────────

def gsc_authorize():
    """
    One-time OAuth setup. Run directly:
      python seo_agent.py --authorize-gsc
    """
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", GSC_SCOPES)
    creds = flow.run_local_server(port=0)
    Path(GSC_TOKEN_FILE).write_text(creds.to_json())
    print(f"✓ GSC token saved to {GSC_TOKEN_FILE}")


def _load_gsc_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if not Path(GSC_TOKEN_FILE).exists():
        raise FileNotFoundError(
            f"No GSC token. Run: python seo_agent.py --authorize-gsc"
        )
    creds = Credentials.from_authorized_user_file(GSC_TOKEN_FILE, GSC_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        Path(GSC_TOKEN_FILE).write_text(creds.to_json())
    return creds


async def fetch_gsc(domain: str) -> dict:
    creds  = _load_gsc_creds()
    today  = date.today()
    week_ago = today - timedelta(days=7)

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"https://www.googleapis.com/webmasters/v3/sites/sc-domain:{domain}/searchAnalytics/query",
            headers={"Authorization": f"Bearer {creds.token}"},
            json={"startDate": str(week_ago), "endDate": str(today), "dimensions": ["query"], "rowLimit": 25},
            timeout=30,
        )
        r.raise_for_status()

    rows = r.json().get("rows", [])
    total_clicks      = sum(r.get("clicks", 0) for r in rows)
    total_impressions = sum(r.get("impressions", 0) for r in rows)

    return {
        "summary": {
            "clicks": total_clicks,
            "impressions": total_impressions,
            "ctr_percent": round(total_clicks / total_impressions * 100, 2) if total_impressions else 0,
            "avg_position": round(sum(r.get("position", 0) for r in rows) / len(rows), 2) if rows else 0,
            "date_range": f"{week_ago} to {today}",
        },
        "top_queries": [
            {"query": r["keys"][0], "clicks": r.get("clicks", 0),
             "impressions": r.get("impressions", 0), "position": round(r.get("position", 0), 1)}
            for r in rows[:10]
        ],
    }


# ── Keyword generation (Haiku) ────────────────────────────────────────────────

async def generate_keyword(markdown: str) -> str:
    msg = await _anthropic.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        tools=[{
            "name": "output_keyword",
            "description": "Output the best SEO keyword phrase for this website",
            "input_schema": {
                "type": "object",
                "properties": {"phrase": {"type": "string"}},
                "required": ["phrase"],
            },
        }],
        tool_choice={"type": "tool", "name": "output_keyword"},
        system=(
            "You are an SEO expert. Analyze the content and return the single best "
            "1-5 word search phrase that represents the site's core topic."
        ),
        messages=[{"role": "user", "content": f"Website content:\n\n{markdown[:8000]}"}],
    )
    tool_block = next(b for b in msg.content if b.type == "tool_use")
    return tool_block.input["phrase"]


# ── Keyword research (Sonnet + web search) ────────────────────────────────────

async def research_keyword(keyword: str) -> str:
    msg = await _anthropic.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2048,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        system=(
            "You are an SEO research analyst. Search the web to understand the "
            "competitive landscape for the given keyword: who ranks, what content "
            "performs well, what the search intent is, and what content gaps exist."
        ),
        messages=[{
            "role": "user",
            "content": (
                f'Research the keyword: "{keyword}"\n\n'
                "Find: top ranking sites, content types that perform best, "
                "key subtopics, search intent, and exploitable content gaps."
            ),
        }],
    )
    return "\n".join(b.text for b in msg.content if hasattr(b, "text"))


# ── Report generation (Sonnet 4) ──────────────────────────────────────────────

REPORT_SYSTEM = """You are an expert SEO Strategist. Generate a comprehensive markdown SEO strategy report.

Structure:
# [Site] SEO Strategy Report
## Executive Summary
## Current Performance  (use GSC data; note if unavailable)
## Website Content Analysis
## Keyword & Market Opportunity
## Competitive Landscape  (from web research)
## SEO Recommendations
  ### Technical SEO
  ### Content Strategy
  ### Keyword Targeting
## 90-Day Action Plan
## Success Metrics & KPIs

Use **bold** for metrics, bullet lists for recommendations, > for key callouts.
Be specific and data-driven — reference actual numbers and findings from the data provided."""


async def generate_report(url, keyword, pages, gsc_data, research) -> str:
    pages_summary = [
        {"url": p["url"], "words": len(p["markdown"].split()), "preview": p["markdown"][:300]}
        for p in pages
    ]

    prompt = f"""Generate an SEO strategy report for: {url}
Target keyword: "{keyword}"

--- Google Search Console ---
{json.dumps(gsc_data, indent=2) if gsc_data else "Not available"}

--- Crawled Pages ({len(pages)} found) ---
{json.dumps(pages_summary, indent=2)}

--- Homepage Content Sample ---
{pages[0]["markdown"][:3000] if pages else "N/A"}

--- Keyword Research ---
{research or "Not available"}

Write the full report now."""

    msg = await _anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        system=REPORT_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# ── Pipeline ──────────────────────────────────────────────────────────────────

async def run(url: str, use_gsc: bool, max_pages: int) -> str:
    urls = clean_url(url)

    log("⏳ Crawling website...")
    pages = await crawl_site(urls["https"], max_pages=max_pages)
    if not pages:
        print("❌ No content found. Check the URL.", file=sys.stderr)
        raise SystemExit(1)
    log(f"✓ Crawled {len(pages)} pages")

    log("⏳ Generating target keyword...")
    combined = "\n\n".join(f"## {p['url']}\n{p['markdown']}" for p in pages)
    keyword = await generate_keyword(combined)
    log(f"✓ Target keyword: '{keyword}'")

    log("⏳ Running research and fetching GSC data...")

    async def _safe_gsc():
        try:
            return await fetch_gsc(urls["no_protocol"])
        except Exception as e:
            log(f"⚠ GSC skipped: {e}")
            return None

    gsc_data, research = await asyncio.gather(
        _safe_gsc() if use_gsc else asyncio.sleep(0, result=None),
        research_keyword(keyword),
    )

    if gsc_data:
        log("✓ GSC data fetched")
    log("✓ Keyword research complete")

    log("⏳ Generating report...")
    report = await generate_report(url, keyword, pages, gsc_data, research)
    log("✓ Done\n")
    return report


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SEO Analysis Agent")
    parser.add_argument("url", nargs="?", help="Target website URL")
    parser.add_argument("--output", "-o", help="Save report to file (default: stdout)")
    parser.add_argument("--no-gsc", action="store_true", help="Skip Google Search Console")
    parser.add_argument("--max-pages", type=int, default=20, help="Max pages to crawl")
    parser.add_argument("--authorize-gsc", action="store_true", help="Run GSC OAuth setup")
    args = parser.parse_args()

    if args.authorize_gsc:
        gsc_authorize()
        return

    if not args.url:
        parser.print_help()
        raise SystemExit(1)

    print(f"\n🔍 Analyzing: {args.url}\n", file=sys.stderr)
    report = asyncio.run(run(args.url, use_gsc=not args.no_gsc, max_pages=args.max_pages))

    if args.output:
        Path(args.output).write_text(report)
        print(f"✅ Report saved to: {args.output}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()