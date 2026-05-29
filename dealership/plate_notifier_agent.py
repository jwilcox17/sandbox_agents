"""
plate_notifier_agent.py — CLI agent that simulates a license-plate
notification system. Reads/writes the same SQLite cache used by
dealership_agent.py and playfinder_agent.py.

Usage:
    # Option A: shell export
    export ANTHROPIC_API_KEY=...
    python plate_notifier_agent.py --cache inv.db

    # Option B: .env file in the current directory
    echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
    python plate_notifier_agent.py --cache inv.db

    python plate_notifier_agent.py --cache inv.db --today 2026-05-25
    python plate_notifier_agent.py --cache inv.db --regen-orders

Pipeline (per invocation):
    1. Ensure plate_orders table exists; if empty (or --regen-orders),
       generate ~20 synthetic orders per dealership via Claude.
    2. Deterministically advance order statuses based on --today.
    3. Identify which orders need a notification today (initial or follow-up).
    4. ONE batched Claude call generates the message body for each.
    5. Write one .txt per notification to ./notifications/ and update DB.

Follow-up cadence after a plate becomes 'ready' and is not picked up:
    day 0  -> initial 'plate is ready' notice
    day +3 -> follow-up 1 (gentle reminder)
    day +7 -> follow-up 2 (polite urgency)
    day +14 -> follow-up 3 (firm reminder)
    day +21 -> follow-up 4 (final notice; plate may be returned to DMV)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 8000

DEFAULT_ORDERS_PER_DEALER = 20
NOTIFICATIONS_DIR = Path(os.environ["SANDBOX_DATA_DIR"]) / "dealership" / "notifications"

# Deterministic state machine: how long a plate spends in each pre-ready stage.
# Random within these ranges, seeded by order_id so each run is reproducible.
PROCESSING_DAYS_MIN = 5
PROCESSING_DAYS_MAX = 10
SHIPPING_DAYS_MIN = 2
SHIPPING_DAYS_MAX = 5

# Follow-up schedule (days after 'ready' / 'shipped').
FOLLOW_UP_OFFSETS = [0, 3, 7, 14, 21]  # 0 = initial, then 4 follow-ups
MAX_FOLLOW_UPS = 4

# Cap how many notifications go into a single Claude call to keep prompts bounded
MAX_NOTIFICATIONS_PER_CALL = 50

# Defense-in-depth check for table names we interpolate into SQL
_TABLE_NAME_RE = re.compile(r"^cars_[a-z0-9_]+$")

log = logging.getLogger("plate_notifier_agent")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def ensure_orders_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS plate_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            dealership_domain TEXT NOT NULL,
            vehicle_description TEXT NOT NULL,
            order_date TEXT NOT NULL,
            delivery_method TEXT NOT NULL DEFAULT 'pickup',  -- 'pickup' | 'ship'
            status TEXT NOT NULL DEFAULT 'processing',
            ready_date TEXT,
            pickup_date TEXT,
            follow_up_count INTEGER NOT NULL DEFAULT 0,
            last_notification_date TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_plate_orders_status
            ON plate_orders(status);
        CREATE INDEX IF NOT EXISTS idx_plate_orders_dealership
            ON plate_orders(dealership_domain);
        """
    )
    conn.commit()


def list_dealership_domains(conn: sqlite3.Connection) -> list[str]:
    """Same approach as the play-finder: read one sample row from each
    cars_* table to get the canonical domain string."""
    tables = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name LIKE 'cars_%'"
        ).fetchall()
        if _TABLE_NAME_RE.fullmatch(r["name"])
    ]
    domains: list[str] = []
    for t in tables:
        row = conn.execute(
            f"SELECT dealership_domain FROM {t} "
            "WHERE dealership_domain IS NOT NULL LIMIT 1"
        ).fetchone()
        if row:
            domains.append(row["dealership_domain"])
    return domains


def sample_vehicles_for_dealership(
    conn: sqlite3.Connection, domain: str, n: int
) -> list[str]:
    """Random sample of `YYYY Make Model` strings from this dealer's inventory."""
    table = None
    for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'cars_%'"
    ).fetchall():
        if not _TABLE_NAME_RE.fullmatch(r["name"]):
            continue
        sample = conn.execute(
            f"SELECT dealership_domain FROM {r['name']} LIMIT 1"
        ).fetchone()
        if sample and sample["dealership_domain"] == domain:
            table = r["name"]
            break
    if not table:
        return []
    rows = conn.execute(
        f"SELECT year, make, model FROM {table} "
        "WHERE year IS NOT NULL ORDER BY RANDOM() LIMIT ?",
        (n,),
    ).fetchall()
    return [f"{r['year']} {r['make']} {r['model']}" for r in rows]


# ---------------------------------------------------------------------------
# Bootstrap: generate synthetic plate orders via Claude
# ---------------------------------------------------------------------------


_GEN_SYSTEM = (
    "You are generating realistic synthetic plate-order records for a "
    "dealership simulation. Output ONLY a single JSON object matching the "
    "schema specified — no prose, no markdown, no code fences."
)


def _build_orders_prompt(
    domain: str, vehicles: list[str], num_orders: int, today: date
) -> str:
    earliest = (today - timedelta(days=35)).isoformat()
    return f"""Generate {num_orders} realistic plate-order records for the dealership at {domain}.

Each record represents a customer who recently bought a vehicle and is awaiting their license plate.

Available vehicles at this dealership (sample of {len(vehicles)}):
{chr(10).join('- ' + v for v in vehicles)}

For each order, produce:
- customer_name: realistic first + last name
- vehicle_description: pick from the list above, OR make up a similar realistic vehicle the dealership might sell
- order_date: YYYY-MM-DD, evenly distributed between {earliest} and {today.isoformat()}
- delivery_method: "pickup" (most common, ~70%) or "ship" (~30%)

Output ONLY this JSON shape (no surrounding prose, no code fence):

{{
  "orders": [
    {{
      "customer_name": "Sarah Chen",
      "vehicle_description": "2024 Volvo XC90 T8 Recharge",
      "order_date": "{(today - timedelta(days=14)).isoformat()}",
      "delivery_method": "pickup"
    }}
  ]
}}

Generate exactly {num_orders} orders. Spread the order dates so the simulation has a mix of plates at different stages.
"""


def _parse_json_loose(text: str) -> dict:
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


def generate_orders_for_dealership(
    client: Anthropic, domain: str, vehicles: list[str],
    num_orders: int, today: date,
) -> list[dict]:
    log.info("[%s] generating %d plate orders via Claude...", domain, num_orders)
    prompt = _build_orders_prompt(domain, vehicles, num_orders, today)
    msg = client.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS,
        system=_GEN_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    try:
        parsed = _parse_json_loose(text)
    except json.JSONDecodeError as e:
        log.error("[%s] could not parse Claude's JSON: %s", domain, e)
        return []

    valid = []
    for o in parsed.get("orders", []):
        if not all(o.get(k) for k in ("customer_name", "vehicle_description", "order_date")):
            continue
        try:
            datetime.strptime(o["order_date"], "%Y-%m-%d")
        except (TypeError, ValueError):
            continue
        if o.get("delivery_method") not in ("pickup", "ship"):
            o["delivery_method"] = "pickup"
        valid.append(o)
    if len(valid) < len(parsed.get("orders", [])):
        log.warning("[%s] dropped %d invalid orders",
                    domain, len(parsed.get("orders", [])) - len(valid))
    return valid


def insert_orders(conn: sqlite3.Connection, domain: str, orders: list[dict]) -> None:
    conn.executemany(
        """INSERT INTO plate_orders
           (customer_name, dealership_domain, vehicle_description,
            order_date, delivery_method)
           VALUES (?, ?, ?, ?, ?)""",
        [
            (o["customer_name"], domain, o["vehicle_description"],
             o["order_date"], o["delivery_method"])
            for o in orders
        ],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Status state machine (deterministic, seeded by order id)
# ---------------------------------------------------------------------------


def _stage_durations(order_id: int) -> tuple[int, int]:
    """Per-order processing & shipping durations. Stable across runs."""
    rng = random.Random(f"plate-{order_id}")
    return (
        rng.randint(PROCESSING_DAYS_MIN, PROCESSING_DAYS_MAX),
        rng.randint(SHIPPING_DAYS_MIN, SHIPPING_DAYS_MAX),
    )


def advance_statuses(conn: sqlite3.Connection, today: date) -> int:
    """Update plate_orders.status based on elapsed days. Returns count changed.

    Pickup-method flow: processing -> ready
    Ship-method flow:   processing -> shipped (acts like 'ready' for follow-ups)
    Picked-up orders are terminal."""
    changes = 0
    rows = conn.execute(
        "SELECT id, order_date, delivery_method, status, ready_date, pickup_date "
        "FROM plate_orders "
        "WHERE status NOT IN ('picked_up', 'returned')"
    ).fetchall()

    for r in rows:
        order_date = datetime.strptime(r["order_date"], "%Y-%m-%d").date()
        days_since = (today - order_date).days
        proc_days, ship_days = _stage_durations(r["id"])
        new_status = None
        new_ready_date = r["ready_date"]

        if r["delivery_method"] == "pickup":
            if days_since >= proc_days and r["status"] == "processing":
                new_status = "ready"
                new_ready_date = (order_date + timedelta(days=proc_days)).isoformat()
        else:  # 'ship'
            if days_since >= proc_days + ship_days and r["status"] in ("processing", "shipped"):
                # Counts as 'ready' for notification purposes once it's in transit
                if r["status"] == "processing":
                    new_status = "shipped"
                    new_ready_date = (order_date + timedelta(days=proc_days)).isoformat()
            elif days_since >= proc_days and r["status"] == "processing":
                new_status = "shipped"
                new_ready_date = (order_date + timedelta(days=proc_days)).isoformat()

        if new_status:
            conn.execute(
                "UPDATE plate_orders SET status=?, ready_date=? WHERE id=?",
                (new_status, new_ready_date, r["id"]),
            )
            changes += 1

    conn.commit()
    return changes


# ---------------------------------------------------------------------------
# Decide who gets notified today
# ---------------------------------------------------------------------------


@dataclass
class Notification:
    order_id: int
    customer_name: str
    vehicle_description: str
    dealership_domain: str
    delivery_method: str  # 'pickup' | 'ship'
    follow_up_number: int  # 0 = initial, 1..4 = follow-up
    days_since_ready: int


def find_due_notifications(conn: sqlite3.Connection, today: date) -> list[Notification]:
    rows = conn.execute(
        "SELECT id, customer_name, dealership_domain, vehicle_description, "
        "       delivery_method, status, ready_date, follow_up_count, "
        "       last_notification_date "
        "FROM plate_orders "
        "WHERE status IN ('ready', 'shipped')"
    ).fetchall()

    out: list[Notification] = []
    today_iso = today.isoformat()
    for r in rows:
        # Don't re-notify on the same simulated day
        if r["last_notification_date"] == today_iso:
            continue
        if r["follow_up_count"] >= MAX_FOLLOW_UPS and r["last_notification_date"] is not None:
            # Already exhausted follow-ups
            continue
        if not r["ready_date"]:
            continue

        ready = datetime.strptime(r["ready_date"], "%Y-%m-%d").date()
        days_since_ready = (today - ready).days
        if days_since_ready < 0:
            continue

        next_follow_up = (
            0 if r["last_notification_date"] is None else r["follow_up_count"] + 1
        )
        if next_follow_up > MAX_FOLLOW_UPS:
            continue
        target_offset = FOLLOW_UP_OFFSETS[next_follow_up]
        if days_since_ready >= target_offset:
            out.append(Notification(
                order_id=r["id"],
                customer_name=r["customer_name"],
                vehicle_description=r["vehicle_description"],
                dealership_domain=r["dealership_domain"],
                delivery_method=r["delivery_method"],
                follow_up_number=next_follow_up,
                days_since_ready=days_since_ready,
            ))
    return out


# ---------------------------------------------------------------------------
# Generate notification message bodies (one batched Claude call)
# ---------------------------------------------------------------------------


def _tone_for_follow_up(n: int) -> str:
    return {
        0: "warm and welcoming; this is the first notification",
        1: "friendly reminder; polite and brief",
        2: "slightly more urgent; emphasize the convenience of picking up soon",
        3: "firm but courteous; note that the plate has been waiting for two weeks",
        4: "final notice; professional and clear that the plate may be returned to the DMV if not collected, but never threatening",
    }[n]


def _build_notify_prompt(notifications: list[Notification], today: date) -> str:
    out = [
        "You are an automotive customer-communication writer. For each plate-order "
        "notification below, produce a short, professional email body (no greeting "
        "block, no signature — those will be added by the system).",
        "",
        f"Today's date: {today.isoformat()}",
        "",
        "Tone guide by follow-up number:",
        "  0 = " + _tone_for_follow_up(0),
        "  1 = " + _tone_for_follow_up(1),
        "  2 = " + _tone_for_follow_up(2),
        "  3 = " + _tone_for_follow_up(3),
        "  4 = " + _tone_for_follow_up(4),
        "",
        f"## {len(notifications)} notifications to write",
        "",
    ]
    for n in notifications:
        out.append(
            f"- order_id={n.order_id}, customer={n.customer_name!r}, "
            f"vehicle={n.vehicle_description!r}, dealer={n.dealership_domain}, "
            f"delivery={n.delivery_method}, follow_up_number={n.follow_up_number}, "
            f"days_since_ready={n.days_since_ready}"
        )
    out += [
        "",
        "Output ONLY a JSON object of this exact shape (no code fence, no prose):",
        "",
        '{',
        '  "messages": [',
        '    {"order_id": <int>, "subject": "<email subject line>", "body": "<email body>"}',
        '  ]',
        '}',
        "",
        "Rules:",
        "- 'body' is 2–4 short sentences. Plain text, no markdown.",
        "- Reference the specific vehicle by name.",
        "- For delivery_method='ship', say the plate has shipped; for 'pickup', say it's ready at the dealership.",
        "- Match the tone guide for the follow_up_number.",
        "- The 'subject' should be appropriate to the follow-up number (e.g. 'Your plate is ready' for 0, 'Final notice: plate awaiting pickup' for 4).",
        f"- Produce exactly {len(notifications)} messages, one per order_id listed above.",
    ]
    return "\n".join(out)


def generate_messages(
    client: Anthropic, notifications: list[Notification], today: date
) -> dict[int, dict]:
    log.info("calling Claude to write %d message(s)...", len(notifications))
    prompt = _build_notify_prompt(notifications, today)
    msg = client.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    try:
        parsed = _parse_json_loose(text)
    except json.JSONDecodeError as e:
        log.error("could not parse Claude's notification JSON: %s", e)
        return {}

    by_id: dict[int, dict] = {}
    for m in parsed.get("messages", []):
        oid = m.get("order_id")
        if not isinstance(oid, int):
            continue
        if not m.get("subject") or not m.get("body"):
            continue
        by_id[oid] = {"subject": m["subject"], "body": m["body"]}
    return by_id


# ---------------------------------------------------------------------------
# Write notification files + update DB
# ---------------------------------------------------------------------------


def write_notification_file(
    out_dir: Path, n: Notification, subject: str, body: str, today: date
) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", n.customer_name).strip("_")
    fn = (
        f"{today.isoformat()}_order{n.order_id:04d}"
        f"_fu{n.follow_up_number}_{safe_name}.txt"
    )
    path = out_dir / fn
    label = "INITIAL" if n.follow_up_number == 0 else f"FOLLOW-UP {n.follow_up_number}"
    path.write_text(
        f"From: {n.dealership_domain} <noreply@{n.dealership_domain}>\n"
        f"To: {n.customer_name}\n"
        f"Date: {today.isoformat()}\n"
        f"Subject: {subject}\n"
        f"X-Notification-Type: {label}\n"
        f"X-Order-Id: {n.order_id}\n"
        f"X-Vehicle: {n.vehicle_description}\n"
        f"X-Delivery-Method: {n.delivery_method}\n"
        "\n"
        f"Dear {n.customer_name},\n\n"
        f"{body}\n\n"
        "Best regards,\n"
        f"The team at {n.dealership_domain}\n"
    )
    return path


def record_notification(
    conn: sqlite3.Connection, n: Notification, today: date
) -> None:
    conn.execute(
        "UPDATE plate_orders SET follow_up_count=?, last_notification_date=? WHERE id=?",
        (n.follow_up_number, today.isoformat(), n.order_id),
    )


# ---------------------------------------------------------------------------
# .env loader (stdlib-only, no python-dotenv dependency)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Simulate a license-plate notification system over the SQLite "
            "cache produced by dealership_agent.py."
        ),
    )
    parser.add_argument("--cache", required=True, metavar="PATH",
                        help="Path to the SQLite cache from dealership_agent.py")
    parser.add_argument("--today", metavar="YYYY-MM-DD",
                        help="Override the simulated current date (default: real today)")
    parser.add_argument("--regen-orders", action="store_true",
                        help="Wipe plate_orders and regenerate via Claude")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    if not os.path.exists(args.cache):
        log.error("cache file not found: %s", args.cache)
        return 2

    # Load .env from cwd if present (shell env still wins)
    load_dotenv()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error(
            "ANTHROPIC_API_KEY not found. Either `export ANTHROPIC_API_KEY=...` "
            "in your shell, or put `ANTHROPIC_API_KEY=...` in a .env file in "
            "the current directory."
        )
        return 2

    if args.today:
        try:
            today = datetime.strptime(args.today, "%Y-%m-%d").date()
        except ValueError:
            log.error("--today must be YYYY-MM-DD")
            return 2
    else:
        today = date.today()
    log.info("simulated today: %s", today.isoformat())

    with sqlite3.connect(args.cache) as conn:
        conn.row_factory = sqlite3.Row
        ensure_orders_table(conn)

        domains = list_dealership_domains(conn)
        if not domains:
            log.error(
                "no dealership tables in cache. Run dealership_agent.py first "
                "with --cache %s to populate.", args.cache
            )
            return 1

        if args.regen_orders:
            log.info("wiping existing plate_orders")
            conn.execute("DELETE FROM plate_orders")
            conn.commit()

        existing_count = conn.execute(
            "SELECT COUNT(*) AS n FROM plate_orders"
        ).fetchone()["n"]

        if existing_count == 0:
            client = Anthropic()
            for domain in domains:
                vehicles = sample_vehicles_for_dealership(conn, domain, 10)
                if not vehicles:
                    log.warning("[%s] no inventory in cache, skipping", domain)
                    continue
                orders = generate_orders_for_dealership(
                    client, domain, vehicles, DEFAULT_ORDERS_PER_DEALER, today
                )
                if orders:
                    insert_orders(conn, domain, orders)
                    log.info("[%s] inserted %d orders", domain, len(orders))
        else:
            log.info("plate_orders already populated (%d rows); "
                     "use --regen-orders to refresh", existing_count)

        changed = advance_statuses(conn, today)
        if changed:
            log.info("advanced status on %d order(s)", changed)

        due = find_due_notifications(conn, today)
        if not due:
            log.info("no notifications due today.")
            return 0

        # Cap the batch and process the rest next run if needed
        if len(due) > MAX_NOTIFICATIONS_PER_CALL:
            log.warning(
                "%d notifications due; capping at %d. Re-run to process the rest.",
                len(due), MAX_NOTIFICATIONS_PER_CALL,
            )
            due = due[:MAX_NOTIFICATIONS_PER_CALL]
        log.info("%d notification(s) due", len(due))

        client = Anthropic()
        try:
            messages = generate_messages(client, due, today)
        except Exception as e:
            log.error("Claude call failed: %s", e)
            return 1

        NOTIFICATIONS_DIR.mkdir(exist_ok=True)
        written = 0
        for n in due:
            m = messages.get(n.order_id)
            if not m:
                log.warning("[order %d] no message returned by Claude, skipping",
                            n.order_id)
                continue
            path = write_notification_file(
                NOTIFICATIONS_DIR, n, m["subject"], m["body"], today
            )
            record_notification(conn, n, today)
            written += 1
            print(path)
        conn.commit()

        log.info("wrote %d notification file(s) to %s/", written, NOTIFICATIONS_DIR)
        return 0


if __name__ == "__main__":
    sys.exit(main())