#!/usr/bin/env python3
"""
Daily Fantasy Baseball Report
==============================
Cron-friendly wrapper that fetches:
  - League leaders (HR, AVG, ERA, strikeouts)
  - Stats for Yankees players on your roster (Judge, Volpe, Dominguez)
  - Stats for Giants players on your roster (Chapman, Jung Hoo Lee, Webb, Adames)

Saves a dated markdown report to ./reports/ and prints to stdout.

Usage:
    python daily_report.py
    python daily_report.py --output /custom/path/report.md

Cron example (runs at 8am daily):
    0 8 * * * cd /path/to/mcp && python daily_report.py >> reports/cron.log 2>&1
"""

import asyncio
import argparse
import os
import sys
from datetime import datetime, timezone
from contextlib import AsyncExitStack
from pathlib import Path

from anthropic import Anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
SERVER_SCRIPT = BASE_DIR / "fantasy_server.py"

# Yankees players currently on your fantasy roster
YANKEES_PLAYERS = [
    "Aaron Judge",
    "Anthony Volpe",
    "Jasson Dominguez",
]

# Giants players currently on your fantasy roster
GIANTS_PLAYERS = [
    "Matt Chapman",
    "Jung Hoo Lee",
    "Logan Webb",
    "Willy Adames",
]

STAT_CATEGORIES = ["homeRuns", "battingAverage", "earnedRunAverage", "strikeOuts"]

SYSTEM_PROMPT = """
You are a fantasy baseball analyst generating a concise daily report.

Workflow:
1. Call get_league_leaders once for each requested stat category.
2. Call search_multiple_players with all player names in a single call to resolve their IDs.
3. Call get_multiple_players_profiles with all resolved IDs in a single call to fetch stats.
4. Write a clean markdown report organized into clear sections.

Rules:
- Always include actual numerical stats — never skip a player or stat category.
- Use markdown tables for stat comparisons.
- Keep analysis brief and actionable.
- Do not ask clarifying questions — generate the full report from the data available.
"""

QUERY = f"""
Generate today's fantasy baseball report dated {datetime.now(timezone.utc).strftime('%B %d, %Y')}.

## Section 1 — League Leaders
Get the top 10 leaders for each of these categories: {', '.join(STAT_CATEGORIES)}.

## Section 2 — Yankees Watch ({', '.join(YANKEES_PLAYERS)})
Search for and retrieve full 2026 stats for these Yankees players on my roster.
Present their key batting stats (AVG, HR, RBI, OBP, SLG) in a table.

## Section 3 — Giants Watch ({', '.join(GIANTS_PLAYERS)})
Search for and retrieve full 2026 stats for these Giants players on my roster.
For hitters present batting stats (AVG, HR, RBI, OBP, SLG).
For Logan Webb present pitching stats (ERA, WHIP, W, K, IP).

End with a brief 2-3 sentence fantasy takeaway for each group.
"""


class ReportClient:
    def __init__(self):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY not set in .env", file=sys.stderr)
            sys.exit(1)

        self.anthropic = Anthropic(api_key=api_key)
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        self.max_tokens = int(os.getenv("MAX_TOKENS", "8000"))
        self.session = None
        self.exit_stack = AsyncExitStack()
        self.available_tools = []

    async def connect(self):
        if not SERVER_SCRIPT.exists():
            print(f"ERROR: Server script not found: {SERVER_SCRIPT}", file=sys.stderr)
            sys.exit(1)

        python = sys.executable  # uses the same interpreter running this script
        server_params = StdioServerParameters(
            command=python, args=[str(SERVER_SCRIPT)], env=None
        )
        stdio_transport = await self.exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        stdio, write = stdio_transport
        self.session = await self.exit_stack.enter_async_context(
            ClientSession(stdio, write)
        )
        await self.session.initialize()

        response = await self.session.list_tools()
        self.available_tools = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.inputSchema,
            }
            for t in response.tools
        ]
        print(f"Connected. {len(self.available_tools)} tools available.", file=sys.stderr)

    async def run_report(self) -> str:
        messages = [{"role": "user", "content": QUERY}]
        final_text = []

        while True:
            response = self.anthropic.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=self.available_tools,
            )
            messages.append({"role": "assistant", "content": response.content})

            tool_uses = []
            for block in response.content:
                if block.type == "text":
                    final_text.append(block.text)
                elif block.type == "tool_use":
                    tool_uses.append(block)
                    print(f"  -> {block.name}({list(block.input.keys())})", file=sys.stderr)

            if not tool_uses:
                break

            # Execute all tool calls in parallel
            results = await asyncio.gather(
                *[self.session.call_tool(t.name, t.input) for t in tool_uses]
            )

            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": t.id,
                        "content": r.content,
                    }
                    for t, r in zip(tool_uses, results)
                ],
            })

        return "\n".join(final_text)

    async def close(self):
        await self.exit_stack.aclose()


async def main(output_path: Path):
    client = ReportClient()
    try:
        print("Connecting to fantasy server...", file=sys.stderr)
        await client.connect()

        print("Running report...", file=sys.stderr)
        report = await client.run_report()

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")

        print(f"Saved: {output_path}", file=sys.stderr)
        print(report)

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily Fantasy Baseball Report")
    parser.add_argument(
        "--output",
        type=Path,
        default=BASE_DIR / "reports" / f"report_{datetime.now().strftime('%Y%m%d')}.md",
        help="Output file path (default: reports/report_YYYYMMDD.md)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.output))
