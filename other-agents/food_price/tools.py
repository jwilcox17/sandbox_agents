"""Tool implementations for the food delivery comparison agent.

Three tools exposed to Claude:
    1. get_delivery_quote(url)  -- fetch a page and extract structured pricing
    2. compute_total(quote, tip_percent)  -- final out-the-door price (deterministic)
    3. present_comparison(totals)  -- formatted side-by-side summary

The first tool uses a sub-call to Claude Haiku as a parser, since each delivery
service renders its pages differently. The other two are pure Python.
"""

from __future__ import annotations

import json
from typing import Optional

import httpx
from anthropic import Anthropic
from pydantic import BaseModel, Field, ValidationError


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class Fees(BaseModel):
    delivery_fee: float = 0.0
    service_fee: float = 0.0
    small_order_fee: float = 0.0
    other_fees: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.delivery_fee
            + self.service_fee
            + self.small_order_fee
            + self.other_fees
        )


class DeliveryQuote(BaseModel):
    service: str = Field(..., description="DoorDash, Uber Eats, Grubhub, etc.")
    restaurant: str
    item_name: str
    item_price: float
    fees: Fees = Field(default_factory=Fees)
    estimated_tax: float = 0.0
    eta_minutes: Optional[int] = None
    url: str


# ---------------------------------------------------------------------------
# Anthropic client (lazy singleton, used by the parser sub-call)
# ---------------------------------------------------------------------------

_client: Optional[Anthropic] = None


def _client_instance() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic()
    return _client


# ---------------------------------------------------------------------------
# Internal: fetch a URL
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_MAX_FETCH_CHARS = 80_000  # cap to keep parser sub-call cheap


def _fetch(url: str) -> str:
    """Fetch a URL and return text content, truncated."""
    with httpx.Client(follow_redirects=True, timeout=20.0, headers=_HEADERS) as c:
        resp = c.get(url)
        resp.raise_for_status()
    text = resp.text
    return text[:_MAX_FETCH_CHARS] if len(text) > _MAX_FETCH_CHARS else text


# ---------------------------------------------------------------------------
# Internal: parser sub-call (Claude Haiku does the messy HTML extraction)
# ---------------------------------------------------------------------------

_PARSER_SYSTEM = """You extract structured pricing data from food-delivery web pages.

Given HTML or text content from DoorDash, Uber Eats, Grubhub, or a restaurant's
own ordering page, find a representative menu item and ALL fees the user would
pay. Return ONLY a JSON object matching this exact schema, with no commentary
and no markdown fences:

{
  "service": "<DoorDash | Uber Eats | Grubhub | restaurant name>",
  "restaurant": "<restaurant name>",
  "item_name": "<one representative item, e.g. 'Pad See Ew'>",
  "item_price": <float>,
  "fees": {
    "delivery_fee": <float>,
    "service_fee": <float>,
    "small_order_fee": <float>,
    "other_fees": <float>
  },
  "estimated_tax": <float>,
  "eta_minutes": <int or null>
}

Rules:
- Use 0.0 for any fee you cannot find on the page.
- If the page is a login wall, empty SPA shell, or contains no pricing data,
  return: {"error": "no pricing data", "reason": "<short explanation>"}.
- Do not invent prices. Only report what is visible in the content provided.
"""


def _parse_with_claude(content: str, url: str) -> dict:
    """Send the page content to Haiku and parse the JSON response."""
    resp = _client_instance().messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        system=_PARSER_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"URL: {url}\n\nPAGE CONTENT:\n{content}",
        }],
    )

    text = "".join(b.text for b in resp.content if b.type == "text").strip()

    # Strip ```json fences if the model adds them anyway
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": "parser returned invalid JSON", "raw": text[:300]}


# ---------------------------------------------------------------------------
# Tool 1: get_delivery_quote
# ---------------------------------------------------------------------------

def get_delivery_quote(url: str) -> dict:
    """Fetch a delivery URL and extract structured pricing data."""
    try:
        content = _fetch(url)
    except httpx.HTTPError as e:
        return {"error": "fetch failed", "reason": str(e), "url": url}

    parsed = _parse_with_claude(content, url)
    if "error" in parsed:
        parsed["url"] = url
        return parsed

    parsed["url"] = url
    try:
        quote = DeliveryQuote(**parsed)
    except ValidationError as e:
        return {"error": "parsed data failed validation", "reason": str(e), "url": url}

    return quote.model_dump()


# ---------------------------------------------------------------------------
# Tool 2: compute_total
# ---------------------------------------------------------------------------

def compute_total(quote: dict, tip_percent: float = 15.0) -> dict:
    """Sum item price + fees + tax + tip for a parsed quote."""
    if "error" in quote:
        return {"error": "cannot compute total on errored quote", "upstream": quote}

    try:
        q = DeliveryQuote(**quote)
    except ValidationError as e:
        return {"error": "invalid quote shape", "reason": str(e)}

    fees_total = q.fees.total
    tip = q.item_price * (tip_percent / 100.0)
    grand_total = q.item_price + fees_total + q.estimated_tax + tip

    return {
        "service": q.service,
        "restaurant": q.restaurant,
        "item": q.item_name,
        "subtotal": round(q.item_price, 2),
        "fees_total": round(fees_total, 2),
        "tax": round(q.estimated_tax, 2),
        "tip": round(tip, 2),
        "tip_percent": tip_percent,
        "grand_total": round(grand_total, 2),
        "eta_minutes": q.eta_minutes,
        "url": q.url,
    }


# ---------------------------------------------------------------------------
# Tool 3: present_comparison
# ---------------------------------------------------------------------------

def present_comparison(totals: list[dict]) -> str:
    """Format a side-by-side comparison, sorted cheapest first."""
    valid = [t for t in totals if "error" not in t]
    errors = [t for t in totals if "error" in t]

    if not valid:
        return "No valid quotes to compare.\n" + "\n".join(
            f"  - {e.get('error')}: {e.get('reason', '')}" for e in errors
        )

    valid.sort(key=lambda x: x["grand_total"])
    cheapest = valid[0]["grand_total"]

    lines = [f"\n=== Comparison ({len(valid)} option{'s' if len(valid) != 1 else ''}) ===\n"]
    for i, t in enumerate(valid):
        delta = t["grand_total"] - cheapest
        marker = "  *** CHEAPEST ***" if i == 0 else ""
        delta_str = f"  (+${delta:.2f})" if delta > 0 else ""
        eta = f"  |  ETA {t['eta_minutes']} min" if t.get("eta_minutes") else ""

        lines.append(f"{t['service']} - {t['restaurant']}{marker}")
        lines.append(f"  Item:   {t['item']:<30} ${t['subtotal']:>7.2f}")
        lines.append(f"  Fees:                                  ${t['fees_total']:>7.2f}")
        lines.append(f"  Tax:                                   ${t['tax']:>7.2f}")
        lines.append(f"  Tip ({t['tip_percent']:.0f}%):                              ${t['tip']:>7.2f}")
        lines.append(f"  TOTAL:                                 ${t['grand_total']:>7.2f}{delta_str}{eta}")
        lines.append("")

    if errors:
        lines.append(f"{len(errors)} URL(s) could not be processed:")
        for e in errors:
            lines.append(f"  - {e.get('url', '?')}: {e.get('error')} ({e.get('reason', '')})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool schemas (sent to Claude)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "get_delivery_quote",
        "description": (
            "Fetch a food-delivery URL (DoorDash, Uber Eats, Grubhub, or a "
            "restaurant's own ordering page) and extract structured pricing data: "
            "service, restaurant, a representative item with price, all fees, "
            "estimated tax, and ETA. Call once per URL the user provides."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch and parse"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "compute_total",
        "description": (
            "Compute the final out-the-door price for a parsed quote, including "
            "tip. Call once per successful quote. Default tip is 15%."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "quote": {
                    "type": "object",
                    "description": "A quote dict returned by get_delivery_quote",
                },
                "tip_percent": {
                    "type": "number",
                    "description": "Tip percentage (default 15.0)",
                },
            },
            "required": ["quote"],
        },
    },
    {
        "name": "present_comparison",
        "description": (
            "Format a side-by-side comparison of all computed totals, sorted "
            "cheapest first. Call once at the end with all totals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "totals": {
                    "type": "array",
                    "description": "List of dicts from compute_total",
                    "items": {"type": "object"},
                },
            },
            "required": ["totals"],
        },
    },
]


# ---------------------------------------------------------------------------
# Dispatcher: agent.py calls this with the tool name + input from Claude
# ---------------------------------------------------------------------------

def dispatch(tool_name: str, tool_input: dict) -> str:
    """Route a tool call to its implementation. Always returns a JSON string."""
    try:
        if tool_name == "get_delivery_quote":
            result = get_delivery_quote(tool_input["url"])
        elif tool_name == "compute_total":
            result = compute_total(
                tool_input["quote"],
                tool_input.get("tip_percent", 15.0),
            )
        elif tool_name == "present_comparison":
            # Already a string; wrap for consistent return type
            return present_comparison(tool_input["totals"])
        else:
            result = {"error": f"unknown tool: {tool_name}"}
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": "tool execution failed", "reason": str(e)})