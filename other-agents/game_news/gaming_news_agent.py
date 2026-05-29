"""
Simple gaming news agent.

The agent uses Claude's tool-use loop:
  1. Claude decides which RSS feeds to pull from.
  2. The `fetch_news` tool grabs articles via feedparser.
  3. Claude reads them and writes a markdown report.
  4. The function returns the report as a string (no storage).

Setup:
    pip install anthropic feedparser
    export ANTHROPIC_API_KEY=sk-ant-...

Usage:
    python gaming_news_agent.py
"""

import json
import feedparser
import anthropic

MODEL = "claude-haiku-4-5"
# RSS feeds the agent is allowed to fetch from.
GAMING_FEEDS = {
    "ign":              "https://feeds.feedburner.com/ign/all",
    "kotaku":           "https://kotaku.com/rss",
    "pcgamer":          "https://www.pcgamer.com/rss/",
    "eurogamer":        "https://www.eurogamer.net/feed",
    "polygon":          "https://www.polygon.com/rss/index.xml",
    "rockpapershotgun": "https://www.rockpapershotgun.com/feed",
    "gamespot":         "https://www.gamespot.com/feeds/news/",
}

TOOLS = [
    {
        "name": "fetch_news",
        "description": (
            "Fetch the latest articles from one or more gaming news sources. "
            f"Available sources: {', '.join(GAMING_FEEDS)}. "
            "Returns a list of recent articles with title, link, summary, and publish date."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names of sources to fetch from.",
                },
                "limit_per_source": {
                    "type": "integer",
                    "description": "Max articles per source (default 10).",
                    "default": 10,
                },
            },
            "required": ["sources"],
        },
    }
]


def fetch_news(sources, limit_per_source=10):
    """Pull articles from each requested RSS feed."""
    articles = []
    for src in sources:
        url = GAMING_FEEDS.get(src.lower())
        if not url:
            continue
        feed = feedparser.parse(url)
        for entry in feed.entries[:limit_per_source]:
            articles.append({
                "source":    src,
                "title":     entry.get("title", ""),
                "link":      entry.get("link", ""),
                "summary":   entry.get("summary", "")[:400],  # keep prompt small
                "published": entry.get("published", ""),
            })
    return articles


SYSTEM_PROMPT = (
    "You are a gaming news analyst. When asked for a news report, use the "
    "fetch_news tool to gather articles from multiple sources, then write a "
    "concise markdown report. Group items by theme (Releases, Industry, "
    "Hardware, Reviews, etc.), keep each bullet short, and include the source "
    "name for every item. End with a one-sentence overall takeaway."
)


def run_agent(user_request: str, max_iterations: int = 5) -> str:
    """Run the tool-use loop and return Claude's final report text."""
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": user_request}]

    for _ in range(max_iterations):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        # Claude is finished — extract and return the text.
        if response.stop_reason != "tool_use":
            return "".join(b.text for b in response.content if b.type == "text")

        # Otherwise, run the requested tools and feed results back in.
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use" and block.name == "fetch_news":
                result = fetch_news(**block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

        messages.append({"role": "user", "content": tool_results})

    return "Agent hit max iterations without producing a final report."


if __name__ == "__main__":
    report = run_agent(
        "Give me a summary report of today's gaming news. "
        "Pull from at least 3 different sources."
    )
    print(report)
