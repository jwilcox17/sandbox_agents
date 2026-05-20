"""Food delivery price comparison agent.

Run:
    python agent.py                    # paste URLs interactively
    python agent.py URL1 URL2 [URL3]   # pass URLs as args
    python agent.py -v URL1 URL2       # verbose: show each tool call
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv

from tools import TOOL_SCHEMAS, dispatch

load_dotenv()

MODEL = "claude-opus-4-7"
MAX_ITERATIONS = 25

SYSTEM_PROMPT = """You are a food-delivery price comparison agent.

The user will give you 2+ URLs to food-delivery pages (DoorDash, Uber Eats,
Grubhub, or a restaurant's own ordering page) for the same or similar item.
Your job is to tell them which is cheapest out-the-door.

For each URL:
  1. Call get_delivery_quote to fetch and parse it.
  2. Call compute_total on the parsed quote (use 15% tip unless the user
     specifies a different tip).

Once you have totals for every URL, call present_comparison once with the
full list. Then give the user a one- or two-sentence summary noting the
cheapest option, the price gap, and any URLs that failed to parse.

Be efficient: don't fetch the same URL twice. If a URL fails, mention it but
continue with the rest.
"""


def run_agent(user_message: str, verbose: bool = False) -> str:
    """Run the tool-use loop until the model finishes or hits the iteration cap."""
    client = Anthropic()
    messages: list[dict] = [{"role": "user", "content": user_message}]

    for iteration in range(MAX_ITERATIONS):
        if verbose:
            print(f"\n--- iteration {iteration + 1} ---", file=sys.stderr)

        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )

        # Append the assistant turn verbatim (may contain text + tool_use blocks)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Final text response
            text_parts = [b.text for b in response.content if b.type == "text"]
            return "\n".join(text_parts) if text_parts else "(no text in final response)"

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if verbose:
                    keys = list(block.input.keys())
                    print(f"  -> {block.name}({', '.join(keys)})", file=sys.stderr)
                result = dispatch(block.name, block.input)
                if verbose:
                    preview = result[:160].replace("\n", " ")
                    print(f"  <- {preview}{'...' if len(result) > 160 else ''}",
                          file=sys.stderr)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason (max_tokens, pause_turn, etc.)
        return f"(stopped early: stop_reason={response.stop_reason})"

    return f"(hit max iterations of {MAX_ITERATIONS})"


def _read_urls_from_stdin() -> list[str]:
    print("Paste food-delivery URLs (one per line), then Ctrl+D:", file=sys.stderr)
    raw = sys.stdin.read()
    return [line.strip() for line in raw.splitlines() if line.strip()]


def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print(
            "Error: ANTHROPIC_API_KEY not set. "
            "Copy .env.example to .env and add your key.",
            file=sys.stderr,
        )
        sys.exit(1)

    args = sys.argv[1:]
    verbose = False
    if "-v" in args:
        verbose = True
        args.remove("-v")
    if "--verbose" in args:
        verbose = True
        args.remove("--verbose")

    urls = args if args else _read_urls_from_stdin()

    if len(urls) < 2:
        print("Need at least 2 URLs to compare.", file=sys.stderr)
        sys.exit(1)

    user_message = (
        "Compare these food delivery options and tell me which is cheapest:\n"
        + "\n".join(urls)
    )

    print("\nRunning agent...\n", file=sys.stderr)
    result = run_agent(user_message, verbose=verbose)
    print(result)


if __name__ == "__main__":
    main()