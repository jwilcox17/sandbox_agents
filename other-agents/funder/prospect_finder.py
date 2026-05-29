"""
Foundation Prospect Finder — simple CLI.

One-shot: give it your foundation's criteria and a search query, it uses
Claude's web search to find prospects, prints a ranked list.

    pip install anthropic python-dotenv
    # put ANTHROPIC_API_KEY in .env

    python prospect_finder.py "tech philanthropists supporting STEM in California"
    python prospect_finder.py "education funders Bay Area" --foundation my.json --count 8
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap

from anthropic import Anthropic

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


DEFAULT_FOUNDATION = {
    "name": "Example Foundation",
    "mission": "Improve educational and economic outcomes for under-resourced youth.",
    "funding_areas": ["K-12 education", "youth development", "workforce development"],
    "geographic_focus": ["California", "Oregon", "Washington"],
    "min_capacity": "individuals/orgs with documented giving of $10k+ per year",
    "avoid": ["active SEC investigations", "convicted of fraud"],
}


SYSTEM_PROMPT = """You are a prospect researcher for a charitable foundation.

You will be given the foundation's profile and a search query. Your job:
1. Run several focused web searches (3-6) to find real, named prospects
   (individuals or organizations) matching the criteria.
2. For each strong match, gather: name, kind, location, what they fund,
   approximate giving capacity, why they fit, and source URLs.
3. Reply with a JSON array of prospects ranked best-fit first. No prose.

Output format — a JSON array only, like:
[
  {
    "name": "...",
    "kind": "individual" | "organization",
    "location": "City, ST or 'National (US)'",
    "sectors": ["...", "..."],
    "capacity": "low" | "mid" | "major" | "ultra",
    "why_fit": "1-2 sentences grounded in what you found",
    "concerns": ["any disqualifier hits, or empty array"],
    "sources": ["https://...", "..."]
  },
  ...
]

Rules:
- Do NOT fabricate names, giving amounts, board roles, or quotes. If you
  can't verify a fact in the search results, leave it out.
- Every prospect MUST have at least one source URL from your searches.
- If a candidate has any disqualifier signal (per the foundation's `avoid`
  list), include it in `concerns` rather than dropping the prospect.
- Reply with the JSON array only — no preamble, no markdown fences.
"""


def find_prospects(query: str, foundation: dict, count: int,
                   max_searches: int, verbose: bool = False) -> list[dict]:
    client = Anthropic()
    user = (
        f"Foundation profile:\n```json\n{json.dumps(foundation, indent=2)}\n```\n\n"
        f"Search query: {query!r}\n\n"
        f"Find up to {count} strong prospects. Reply with the JSON array only."
    )

    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search",
                "max_uses": max_searches}],
        messages=[{"role": "user", "content": user}],
    )

    if verbose:
        for block in resp.content:
            if getattr(block, "type", None) == "server_tool_use" and block.name == "web_search":
                print(f"  ↳ web_search: {block.input.get('query', '?')!r}", file=sys.stderr)

    text = "".join(b.text for b in resp.content
                   if getattr(b, "type", None) == "text").strip()

    # Strip optional markdown fences in case the model hedged.
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rstrip("`").strip()

    try:
        prospects = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Model returned non-JSON output: {exc}\n"
                           f"Raw output:\n{text[:500]}")

    if not isinstance(prospects, list):
        raise RuntimeError(f"Expected a JSON array, got: {type(prospects).__name__}")
    return prospects


def render_table(prospects: list[dict]) -> str:
    if not prospects:
        return "(no prospects found)"
    lines = []
    for i, p in enumerate(prospects, 1):
        concerns = p.get("concerns") or []
        flag = "  ⚠️" if concerns else ""
        lines.append(
            f"{i}. {p.get('name', '?')}{flag}  "
            f"[{p.get('kind', '?')}, {p.get('capacity', '?')} capacity, "
            f"{p.get('location', '?')}]"
        )
        if p.get("sectors"):
            lines.append(f"     sectors: {', '.join(p['sectors'])}")
        if p.get("why_fit"):
            lines.append(textwrap.fill(
                f"why: {p['why_fit']}", 78,
                initial_indent="     ", subsequent_indent="          "))
        if concerns:
            lines.append(f"     concerns: {'; '.join(concerns)}")
        for s in p.get("sources", []) or []:
            lines.append(f"     - {s}")
        lines.append("")
    return "\n".join(lines).rstrip()


def main():
    parser = argparse.ArgumentParser(
        prog="prospect_finder",
        description="Find foundation prospects via web search. One-shot CLI.")
    parser.add_argument("query", help='Search intent, e.g. "education funders in CA"')
    parser.add_argument("--foundation", help="Path to foundation profile JSON "
                        "(default: built-in example)")
    parser.add_argument("--count", type=int, default=5,
                        help="Max prospects to return (default 5)")
    parser.add_argument("--max-searches", type=int, default=6,
                        help="Cap on web_search calls (default 6)")
    parser.add_argument("--json", action="store_true",
                        help="Emit raw JSON instead of the human table")
    parser.add_argument("--out", help="Write output to a file")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show each web_search query as it runs")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set. Put it in .env or export it.",
              file=sys.stderr)
        return 1

    if args.foundation:
        with open(args.foundation) as f:
            foundation = json.load(f)
    else:
        foundation = DEFAULT_FOUNDATION

    try:
        prospects = find_prospects(args.query, foundation,
                                    count=args.count,
                                    max_searches=args.max_searches,
                                    verbose=args.verbose)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    output = json.dumps(prospects, indent=2) if args.json else render_table(prospects)

    if args.out:
        with open(args.out, "w") as f:
            f.write(output + "\n")
        print(f"Wrote {len(prospects)} prospects to {args.out}", file=sys.stderr)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
