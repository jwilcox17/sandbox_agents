#!/usr/bin/env python3
"""
review_agent.py — A CLI agent that generates fake customer reviews using
Claude (via the Anthropic API) and stores them in a local SQLite database.

Examples:
    python review_agent.py --create 7 "portable vacuum cleaner"
    python review_agent.py --create-many 5 9 "wireless earbuds"
    python review_agent.py --list
    python review_agent.py --list --item "portable vacuum cleaner"

Requirements:
    pip install anthropic
    export ANTHROPIC_API_KEY=sk-ant-...

The script is built around a small agent loop: we describe a `save_review`
tool to Claude, ask it to invent a review, and Claude responds by *calling*
that tool with structured arguments (username, score, description). Our
code then actually performs the database insert. This is the standard
"tool use" / "function calling" pattern, and it is what turns one-shot
text generation into an agent — Claude decides what to do, our code
executes the side effect.
"""

import argparse
import os
import random
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone

import anthropic  # pip install anthropic


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Haiku 4.5 is plenty smart for short creative generation like product
# reviews, and it keeps the cost-per-review low. Swap to "claude-sonnet-4-6"
# (mid-tier) or "claude-opus-4-7" (top-tier) for richer prose at higher cost.
MODEL = "claude-haiku-4-5"
DB_PATH = "/logs/reviews.db"
ANTHROPIC_API_KEY = "REDACTED"

# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------
def init_db(path: str = DB_PATH) -> None:
    """Create the `reviews` table if it does not yet exist.

    We call this every time the script runs — it is a no-op once the
    table exists, so it serves as both first-run setup and a safety net.
    The CHECK constraint on `score` is a belt-and-braces measure: even if
    something bypassed our Python validation, the DB would still reject
    out-of-range scores.
    """
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reviews (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT    NOT NULL,
                score       INTEGER NOT NULL CHECK (score BETWEEN 1 AND 10),
                description TEXT    NOT NULL,
                item        TEXT    NOT NULL,
                created_at  TEXT    NOT NULL
            )
            """
        )
        conn.commit()


def insert_review(username: str, score: int, description: str, item: str,
                  path: str = DB_PATH) -> int:
    """Insert one review and return its new row id.

    We use parameterised queries (the `?` placeholders) rather than string
    formatting. This is non-negotiable: it prevents SQL injection, which
    matters here because the `description` field comes from a language
    model whose output we do not fully control.
    """
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute(
            """
            INSERT INTO reviews (username, score, description, item, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                username,
                score,
                description,
                item,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return cur.lastrowid


def fetch_reviews(item: str | None = None,
                  limit: int | None = None,
                  path: str = DB_PATH) -> list[tuple]:
    """Return reviews, optionally filtered to a single item and/or limited
    to the most recent N (newest first when limit is set)."""
    with closing(sqlite3.connect(path)) as conn:
        if limit is not None:
            # Newest first, capped at `limit`. We sort DESC by id because
            # id is autoincrement, so highest id == most recently inserted.
            if item is not None:
                return conn.execute(
                    "SELECT id, username, score, item, description, created_at "
                    "FROM reviews WHERE item = ? ORDER BY id DESC LIMIT ?",
                    (item, limit),
                ).fetchall()
            return conn.execute(
                "SELECT id, username, score, item, description, created_at "
                "FROM reviews ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        if item is not None:
            return conn.execute(
                "SELECT id, username, score, item, description, created_at "
                "FROM reviews WHERE item = ? ORDER BY id",
                (item,),
            ).fetchall()
        return conn.execute(
            "SELECT id, username, score, item, description, created_at "
            "FROM reviews ORDER BY id"
        ).fetchall()


# ---------------------------------------------------------------------------
# Agent layer (Anthropic API + tool use)
# ---------------------------------------------------------------------------
# This dictionary is the JSON-schema description of our tool. Claude reads
# the `description` field to decide *when* to use the tool, and it reads
# `input_schema` to decide *how* to fill in arguments. Putting clear,
# specific guidance here is the single biggest lever you have over output
# quality — it is essentially a tiny system prompt for the tool itself.
SAVE_REVIEW_TOOL = {
    "name": "save_review",
    "description": (
        "Persist a single fictitious customer review to the local database. "
        "Call this exactly once per request, with realistic, varied details. "
        "Do not include any preamble or commentary — just call the tool."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "username": {
                "type": "string",
                "description": (
                    "A plausible online username/handle for the reviewer "
                    "(for example: 'maple_42', 'JennyR', 'tech_dad_77', "
                    "'sarah.b'). Vary the style across calls. Avoid the "
                    "names of real public figures."
                ),
            },
            "score": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": (
                    "Star rating out of 10. Use the value supplied in the "
                    "user request — do not invent a different score."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "The review text itself: 1 to 4 sentences, written in "
                    "the casual voice of a real customer. The tone must "
                    "match the score: 1-3 is harsh, 4-6 is mixed/lukewarm, "
                    "7-8 is positive with small gripes, 9-10 is glowing."
                ),
            },
        },
        "required": ["username", "score", "description"],
    },
}


def build_user_message(score: int, item: str) -> str:
    """Compose the natural-language instruction we send to Claude.

    We restate the score and item, and we explicitly instruct Claude to
    respond by calling the tool. The `tool_choice` parameter on the API
    call below also forces this, but saying it in the prompt as well is
    cheap belt-and-braces.
    """
    return (
        f"Generate one fictional customer review for this product or "
        f"service: '{item}'. The reviewer's rating is {score}/10. "
        f"The tone must reflect that score. Respond by calling the "
        f"save_review tool — do not write any other text."
    )


def generate_and_save_review(client: anthropic.Anthropic,
                             score: int,
                             item: str) -> dict:
    """Run one agent turn: ask Claude, parse the tool call, persist it.

    Returns a dict with the saved fields plus the new row id.
    Raises RuntimeError if the model failed to call the tool (rare, but
    worth handling explicitly so failures are loud).
    """
    # Ask Claude. Three things matter here:
    #   1. `tools=[...]`: tells the model what functions are available.
    #   2. `tool_choice={"type": "tool", "name": "save_review"}`: forces
    #      the model to call this *specific* tool rather than answering
    #      with text. Without this, Claude might write the review as
    #      prose and never call the tool.
    #   3. `max_tokens`: an upper bound on the response length. Tool
    #      arguments count toward this, so give it some headroom.
    response = client.messages.create(
        model=MODEL,
        max_tokens=512,
        tools=[SAVE_REVIEW_TOOL],
        tool_choice={"type": "tool", "name": "save_review"},
        messages=[{"role": "user", "content": build_user_message(score, item)}],
    )

    # A response is a list of content blocks. With forced tool use, we
    # expect exactly one `tool_use` block, but iterating is safer than
    # indexing — the SDK could in principle prepend a text block.
    for block in response.content:
        if block.type == "tool_use" and block.name == "save_review":
            args = block.input  # already a parsed dict, schema-validated

            # Defensive override: if Claude ignored the score we asked
            # for, snap it back to what the user requested. The schema
            # doesn't enforce equality with the user's input, only the
            # 1-10 range, so this is the cheapest way to guarantee it.
            if args["score"] != score:
                args["score"] = score

            row_id = insert_review(
                username=args["username"],
                score=args["score"],
                description=args["description"],
                item=item,
            )
            return {"id": row_id, "item": item, **args}

    # Fallback: surface whatever Claude said so debugging is easy.
    text = "".join(b.text for b in response.content if b.type == "text")
    raise RuntimeError(f"Model did not call save_review. It said: {text!r}")


# ---------------------------------------------------------------------------
# CLI layer
# ---------------------------------------------------------------------------
def parse_score(raw: str) -> int:
    """argparse type-converter for score arguments."""
    try:
        s = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"score must be an integer, got {raw!r}")
    if not 1 <= s <= 10:
        raise argparse.ArgumentTypeError("score must be between 1 and 10")
    return s


def cmd_create(client, score: int, item: str) -> None:
    review = generate_and_save_review(client, score, item)
    print(f"\nSaved review #{review['id']}")
    print(f"  Item:        {review['item']}")
    print(f"  User:        {review['username']}")
    print(f"  Score:       {review['score']}/10")
    print(f"  Description: {review['description']}\n")


def cmd_create_many(client, n: int, score: int, item: str) -> None:
    for i in range(1, n + 1):
        review = generate_and_save_review(client, score, item)
        print(f"[{i}/{n}] #{review['id']} {review['username']} "
              f"({review['score']}/10): {review['description']}")


def cmd_list(item: str | None, limit: int | None) -> None:
    rows = fetch_reviews(item=item, limit=limit)
    if not rows:
        print("No reviews found.")
        return
    # When limited, fetch_reviews returns newest-first. Reverse for display
    # so the most recent appears at the bottom (matches a chat-log feel).
    if limit is not None:
        rows = list(reversed(rows))
    for rid, user, score, it, desc, created in rows:
        print(f"#{rid}  [{created}]  {user} — {it} — {score}/10")
        print(f"    {desc}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate fictional customer reviews with Claude and store "
            "them in a local SQLite database."
        )
    )

    # The three subcommands are mutually exclusive — the user picks one.
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--create",
        nargs=2,
        metavar=("SCORE", "ITEM"),
        help="Generate one review (e.g. --create 7 'portable vacuum cleaner').",
    )
    group.add_argument(
        "--create-many",
        nargs=3,
        metavar=("N", "SCORE", "ITEM"),
        help="Generate N independent reviews for the same item and score.",
    )
    group.add_argument(
        "--random",
        nargs="+",
        metavar=("ITEM", "COUNT"),
        help="Generate review(s) with a random score 1-10. "
             "Optional second arg = how many (default 1).",
    )
    group.add_argument(
        "--list",
        action="store_true",
        help="List stored reviews (optionally filter with --item).",
    )

    parser.add_argument(
        "--item",
        metavar="ITEM",
        help="When used with --list, restrict output to this item.",
    )
    parser.add_argument(
        "--last",
        type=int,
        metavar="N",
        help="When used with --list, show only the N most recent reviews.",
    )

    args = parser.parse_args()

    # Always make sure the database and table exist before doing anything.
    init_db()

    # --list does not need the API; the other two do.
    if args.list:
        cmd_list(args.item, args.last)
        return 0

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.",
              file=sys.stderr)
        return 1

    # The SDK reads ANTHROPIC_API_KEY from the environment automatically.
    client = anthropic.Anthropic()

    try:
        if args.create:
            score = parse_score(args.create[0])
            item = args.create[1]
            cmd_create(client, score, item)
        elif args.create_many:
            try:
                n = int(args.create_many[0])
                if n < 1:
                    raise ValueError
            except ValueError:
                print("ERROR: N must be a positive integer.", file=sys.stderr)
                return 1
            score = parse_score(args.create_many[1])
            item = args.create_many[2]
            cmd_create_many(client, n, score, item)
        elif args.random:
            # First arg is the item; optional second arg is a count.
            item = args.random[0]
            count = 1
            if len(args.random) > 1:
                try:
                    count = int(args.random[1])
                    if count < 1:
                        raise ValueError
                except ValueError:
                    print("ERROR: COUNT must be a positive integer.",
                          file=sys.stderr)
                    return 1
            if len(args.random) > 2:
                print("ERROR: --random takes at most 2 arguments "
                      "(ITEM and optional COUNT).", file=sys.stderr)
                return 1
            for i in range(1, count + 1):
                score = random.randint(1, 10)
                if count > 1:
                    print(f"[{i}/{count}] random score: {score}")
                cmd_create(client, score, item)
    except argparse.ArgumentTypeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())