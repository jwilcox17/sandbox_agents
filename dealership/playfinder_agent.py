"""
playfinder_agent.py — CLI agent that reads the SQLite cache produced by
dealership_agent.py, generates synthetic customer data via Claude (on first
run or with --regen), then finds sales 'plays' and writes a salesperson-ready
report via Claude.

Usage:
    export ANTHROPIC_API_KEY=...
    python playfinder_agent.py --cache inv.db
    python playfinder_agent.py --cache inv.db --regen
    python playfinder_agent.py --cache inv.db --num-customers 50

Pipeline:
    cache.db (from agent 1)
        |-- generate customers via Claude (once, or with --regen)
        |-- ensure customers / service_appointments / customer_wants tables
        |-- find candidate plays in Python (4 types)
        |-- build prompt -> Claude -> markdown report on stdout

Plays found:
    1. service_time_test_drive — customer's service appointment is upcoming
       AND their dealership has the same model in a newer year
    2. want_in_stock — customer's wishlist matches inventory at their dealership
    3. cross_dealer_transfer — customer's wishlist matches inventory at a
       different dealership in the cache
    4. trade_in_match — customer A wants car X; customer B already owns car X
       at reasonable mileage (potential trade-in source)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from anthropic import Anthropic

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = "claude-haiku-4-5"
MAX_TOKENS_GENERATE = 8000
MAX_TOKENS_REPORT = 8000
DEFAULT_NUM_CUSTOMERS_PER_DEALER = 30
SERVICE_LOOKAHEAD_DAYS = 30
TRADE_IN_MAX_MILEAGE = 80000
MAX_PLAYS_PER_TYPE_IN_PROMPT = 30  # cap so prompt doesn't blow up

# Match agent 1's cache table-name regex (defense in depth: we interpolate
# the table name into SQL since it's chosen, not user input).
_TABLE_NAME_RE = re.compile(r"^cars_[a-z0-9_]+$")

log = logging.getLogger("playfinder_agent")


# ---------------------------------------------------------------------------
# Schema setup
# ---------------------------------------------------------------------------


def ensure_customer_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            dealership_domain TEXT NOT NULL,
            current_year INTEGER,
            current_make TEXT,
            current_model TEXT,
            current_mileage INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_customers_dealership
            ON customers(dealership_domain);

        CREATE TABLE IF NOT EXISTS service_appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            scheduled_date TEXT NOT NULL,
            service_type TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );

        CREATE TABLE IF NOT EXISTS customer_wants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            wanted_make TEXT,
            wanted_model TEXT,
            wanted_year_min INTEGER,
            max_price INTEGER,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );
        """
    )
    conn.commit()


def list_dealership_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name LIKE 'cars_%'"
    ).fetchall()
    # Filter through the same regex agent 1 uses, just to be safe
    return [r["name"] for r in rows if _TABLE_NAME_RE.fullmatch(r["name"])]


def map_tables_to_domains(conn: sqlite3.Connection) -> dict[str, str]:
    """{table_name: dealership_domain} — read from a sample row of each table.
    Avoids any lossy reverse of domain_to_table_name."""
    out: dict[str, str] = {}
    for t in list_dealership_tables(conn):
        row = conn.execute(
            f"SELECT dealership_domain FROM {t} "
            "WHERE dealership_domain IS NOT NULL LIMIT 1"
        ).fetchone()
        if row:
            out[t] = row["dealership_domain"]
    return out


def domain_to_table(conn: sqlite3.Connection, domain: str) -> Optional[str]:
    for table, dom in map_tables_to_domains(conn).items():
        if dom == domain:
            return table
    return None


# ---------------------------------------------------------------------------
# Customer data generation (Claude)
# ---------------------------------------------------------------------------


_GENERATE_SYSTEM_PROMPT = (
    "You are generating realistic synthetic customer data for an automotive "
    "dealership simulation. Output ONLY a single JSON object matching the "
    "schema specified — no prose, no markdown, no code fences."
)


def _sample_inventory_lines(rows: list[sqlite3.Row], limit: int = 10) -> str:
    lines = []
    for r in rows[:limit]:
        price = r["price"] if r["price"] not in (None, "", "N/A") else "?"
        lines.append(
            f"- {r['year']} {r['make']} {r['model']} {r['trim']} — ${price}"
        )
    return "\n".join(lines)


def _build_generate_prompt(
    domain: str, sample_inventory: list[sqlite3.Row], num_customers: int
) -> str:
    today_iso = date.today().isoformat()
    cutoff_iso = (date.today() + timedelta(days=60)).isoformat()
    return f"""Generate {num_customers} realistic customers for the dealership at {domain}.

The dealership's current inventory looks like (sample of {len(sample_inventory)} cars):
{_sample_inventory_lines(sample_inventory)}

For each customer, produce:
- name: realistic first + last name
- current_year: integer year of the car they currently own (typically 3-12 years older than the newest in inventory)
- current_make: brand of car they own. Most should match the dealership's primary brand; ~15% can be other brands.
- current_model: model name
- current_mileage: realistic for the year (~10-15k miles per year of age)
- service_appointment (OPTIONAL — include for ~25% of customers):
    - scheduled_date: YYYY-MM-DD between {today_iso} and {cutoff_iso}
    - service_type: e.g. "30,000 mile service", "Oil change", "Brake inspection"
- want (OPTIONAL — include for ~30% of customers):
    - wanted_make, wanted_model: usually similar to or an upgrade from their current car. Often match the dealership's inventory above so plays exist.
    - wanted_year_min: integer, typically newer than the customer's current car
    - max_price: integer dollars, realistic for the wanted model/year

Output ONLY this JSON shape (no surrounding prose, no code fence):

{{
  "customers": [
    {{
      "name": "Sarah Chen",
      "current_year": 2019,
      "current_make": "Volvo",
      "current_model": "XC60",
      "current_mileage": 62000,
      "service_appointment": {{"scheduled_date": "{today_iso}", "service_type": "30,000 mile service"}},
      "want": {{"wanted_make": "Volvo", "wanted_model": "XC90", "wanted_year_min": 2023, "max_price": 70000}}
    }}
  ]
}}

If a customer has no service_appointment or no want, OMIT the field entirely (do not set to null).
Generate exactly {num_customers} customers.
"""


def _parse_json_loose(text: str) -> dict:
    """Parse JSON, tolerating ```json fences and surrounding prose."""
    s = text.strip()
    # Strip code fences
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # Fallback: grab the outermost {...}
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


def _validate_customer(c: dict) -> bool:
    """Cheap shape check. Drop bad records rather than fail the whole batch."""
    required = ("name", "current_year", "current_make", "current_model", "current_mileage")
    for k in required:
        if c.get(k) in (None, ""):
            return False
    try:
        int(c["current_year"])
        int(c["current_mileage"])
    except (TypeError, ValueError):
        return False
    return True


def generate_customers_for_dealership(
    client: Anthropic,
    domain: str,
    sample_inventory: list[sqlite3.Row],
    num_customers: int,
) -> list[dict]:
    log.info("[%s] generating %d customers via Claude...", domain, num_customers)
    prompt = _build_generate_prompt(domain, sample_inventory, num_customers)
    msg = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS_GENERATE,
        system=_GENERATE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")

    try:
        parsed = _parse_json_loose(text)
    except json.JSONDecodeError as e:
        log.error("[%s] could not parse Claude's JSON: %s", domain, e)
        log.debug("[%s] raw response: %s", domain, text[:500])
        return []

    raw = parsed.get("customers") or []
    valid = [c for c in raw if _validate_customer(c)]
    if len(valid) < len(raw):
        log.warning(
            "[%s] dropped %d/%d invalid customer records",
            domain, len(raw) - len(valid), len(raw),
        )
    return valid


def insert_customers(
    conn: sqlite3.Connection, domain: str, customers: list[dict]
) -> None:
    cur = conn.cursor()
    for c in customers:
        cur.execute(
            """INSERT INTO customers
               (name, dealership_domain, current_year, current_make,
                current_model, current_mileage)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                c["name"],
                domain,
                int(c["current_year"]),
                c["current_make"],
                c["current_model"],
                int(c["current_mileage"]),
            ),
        )
        cust_id = cur.lastrowid

        appt = c.get("service_appointment")
        if isinstance(appt, dict) and appt.get("scheduled_date"):
            cur.execute(
                """INSERT INTO service_appointments
                   (customer_id, scheduled_date, service_type)
                   VALUES (?, ?, ?)""",
                (cust_id, appt["scheduled_date"], appt.get("service_type")),
            )

        want = c.get("want")
        if isinstance(want, dict) and want.get("wanted_make"):
            try:
                year_min = int(want["wanted_year_min"]) if want.get("wanted_year_min") else None
            except (TypeError, ValueError):
                year_min = None
            try:
                max_price = int(want["max_price"]) if want.get("max_price") else None
            except (TypeError, ValueError):
                max_price = None
            cur.execute(
                """INSERT INTO customer_wants
                   (customer_id, wanted_make, wanted_model, wanted_year_min, max_price)
                   VALUES (?, ?, ?, ?, ?)""",
                (cust_id, want["wanted_make"], want.get("wanted_model"), year_min, max_price),
            )
    conn.commit()


def clear_customer_tables(conn: sqlite3.Connection) -> None:
    log.info("clearing existing customer / appointments / wants tables")
    conn.executescript(
        "DELETE FROM customer_wants; "
        "DELETE FROM service_appointments; "
        "DELETE FROM customers;"
    )
    conn.commit()


def existing_customer_domains(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT dealership_domain FROM customers"
    ).fetchall()
    return {r["dealership_domain"] for r in rows}


# ---------------------------------------------------------------------------
# Play finders (deterministic SQL)
# ---------------------------------------------------------------------------


@dataclass
class Play:
    play_type: str
    customer_name: str
    customer_dealer: str
    details: dict


def _read_inventory_matches(
    conn: sqlite3.Connection,
    table: str,
    *,
    make: str,
    model: str,
    year_min: int,
    max_price: Optional[int],
    limit: int = 3,
) -> list[dict]:
    """Find inventory rows matching a customer want at a specific dealership."""
    if not _TABLE_NAME_RE.fullmatch(table):
        return []
    sql = (
        f"SELECT vin, year, make, model, trim, price, exterior_color "
        f"FROM {table} "
        f"WHERE make = ? AND model = ? "
        f"AND year IS NOT NULL AND year >= ? "
    )
    params: list = [make, model, year_min]
    if max_price is not None:
        # SQLite's CAST is forgiving — non-numeric prices become 0, which
        # always passes <= max_price. Filter to numeric prices > 0.
        sql += "AND CAST(price AS INTEGER) > 0 AND CAST(price AS INTEGER) <= ? "
        params.append(max_price)
    sql += "ORDER BY year DESC, CAST(price AS INTEGER) ASC LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        log.debug("inventory query on %s failed: %s", table, e)
        return []
    return [dict(r) for r in rows]


def find_service_time_plays(conn: sqlite3.Connection) -> list[Play]:
    today = date.today()
    cutoff = today + timedelta(days=SERVICE_LOOKAHEAD_DAYS)
    tables_by_domain = {v: k for k, v in map_tables_to_domains(conn).items()}

    rows = conn.execute(
        """SELECT c.name, c.dealership_domain, c.current_year, c.current_make,
                  c.current_model, c.current_mileage,
                  a.scheduled_date, a.service_type
           FROM customers c
           JOIN service_appointments a ON a.customer_id = c.id
           WHERE a.scheduled_date BETWEEN ? AND ?""",
        (today.isoformat(), cutoff.isoformat()),
    ).fetchall()

    plays: list[Play] = []
    for r in rows:
        table = tables_by_domain.get(r["dealership_domain"])
        if not table:
            continue
        matches = _read_inventory_matches(
            conn, table,
            make=r["current_make"], model=r["current_model"],
            year_min=(r["current_year"] or 0) + 1,
            max_price=None,
            limit=3,
        )
        if matches:
            plays.append(Play(
                play_type="service_time_test_drive",
                customer_name=r["name"],
                customer_dealer=r["dealership_domain"],
                details={
                    "current_car": (
                        f"{r['current_year']} {r['current_make']} {r['current_model']}"
                    ),
                    "current_mileage": r["current_mileage"],
                    "service_date": r["scheduled_date"],
                    "service_type": r["service_type"],
                    "matching_inventory": matches,
                },
            ))
    return plays


def find_want_in_stock_plays(conn: sqlite3.Connection) -> list[Play]:
    tables_by_domain = {v: k for k, v in map_tables_to_domains(conn).items()}
    rows = conn.execute(
        """SELECT c.name, c.dealership_domain,
                  w.wanted_make, w.wanted_model, w.wanted_year_min, w.max_price
           FROM customers c
           JOIN customer_wants w ON w.customer_id = c.id
           WHERE w.wanted_make IS NOT NULL AND w.wanted_model IS NOT NULL"""
    ).fetchall()

    plays: list[Play] = []
    for r in rows:
        table = tables_by_domain.get(r["dealership_domain"])
        if not table:
            continue
        matches = _read_inventory_matches(
            conn, table,
            make=r["wanted_make"], model=r["wanted_model"],
            year_min=r["wanted_year_min"] or 0,
            max_price=r["max_price"],
            limit=3,
        )
        if matches:
            plays.append(Play(
                play_type="want_in_stock",
                customer_name=r["name"],
                customer_dealer=r["dealership_domain"],
                details={
                    "wants": (
                        f"{r['wanted_year_min'] or '?'}+ "
                        f"{r['wanted_make']} {r['wanted_model']}"
                    ),
                    "max_price": r["max_price"],
                    "matching_inventory": matches,
                },
            ))
    return plays


def find_cross_dealer_plays(conn: sqlite3.Connection) -> list[Play]:
    tables_by_domain = {v: k for k, v in map_tables_to_domains(conn).items()}
    if len(tables_by_domain) < 2:
        return []  # nothing to cross-shop

    rows = conn.execute(
        """SELECT c.name, c.dealership_domain,
                  w.wanted_make, w.wanted_model, w.wanted_year_min, w.max_price
           FROM customers c
           JOIN customer_wants w ON w.customer_id = c.id
           WHERE w.wanted_make IS NOT NULL AND w.wanted_model IS NOT NULL"""
    ).fetchall()

    plays: list[Play] = []
    for r in rows:
        own_table = tables_by_domain.get(r["dealership_domain"])
        for domain, table in tables_by_domain.items():
            if table == own_table:
                continue
            matches = _read_inventory_matches(
                conn, table,
                make=r["wanted_make"], model=r["wanted_model"],
                year_min=r["wanted_year_min"] or 0,
                max_price=r["max_price"],
                limit=2,
            )
            if matches:
                plays.append(Play(
                    play_type="cross_dealer_transfer",
                    customer_name=r["name"],
                    customer_dealer=r["dealership_domain"],
                    details={
                        "wants": (
                            f"{r['wanted_year_min'] or '?'}+ "
                            f"{r['wanted_make']} {r['wanted_model']}"
                        ),
                        "max_price": r["max_price"],
                        "available_at": domain,
                        "matching_inventory": matches,
                    },
                ))
    return plays


def find_trade_in_plays(conn: sqlite3.Connection) -> list[Play]:
    wants = conn.execute(
        """SELECT c.id, c.name, c.dealership_domain,
                  w.wanted_make, w.wanted_model, w.wanted_year_min
           FROM customers c
           JOIN customer_wants w ON w.customer_id = c.id
           WHERE w.wanted_make IS NOT NULL AND w.wanted_model IS NOT NULL"""
    ).fetchall()

    owners = conn.execute(
        """SELECT id, name, dealership_domain, current_year, current_make,
                  current_model, current_mileage
           FROM customers
           WHERE current_year IS NOT NULL AND current_make IS NOT NULL"""
    ).fetchall()

    plays: list[Play] = []
    for w in wants:
        year_min = w["wanted_year_min"] or 0
        for o in owners:
            if o["id"] == w["id"]:
                continue
            if (
                o["current_make"] == w["wanted_make"]
                and o["current_model"] == w["wanted_model"]
                and (o["current_year"] or 0) >= year_min
                and (o["current_mileage"] or 0) <= TRADE_IN_MAX_MILEAGE
            ):
                plays.append(Play(
                    play_type="trade_in_match",
                    customer_name=w["name"],
                    customer_dealer=w["dealership_domain"],
                    details={
                        "wants": (
                            f"{year_min}+ {w['wanted_make']} {w['wanted_model']}"
                        ),
                        "owner_name": o["name"],
                        "owner_dealer": o["dealership_domain"],
                        "owner_car": (
                            f"{o['current_year']} {o['current_make']} {o['current_model']}"
                        ),
                        "owner_mileage": o["current_mileage"],
                    },
                ))
                break  # one match per want is enough to surface the play
    return plays


# ---------------------------------------------------------------------------
# Report prompt + Claude call
# ---------------------------------------------------------------------------


def build_report_prompt(plays: list[Play]) -> str:
    by_type: dict[str, list[Play]] = {}
    for p in plays:
        by_type.setdefault(p.play_type, []).append(p)

    out: list[str] = [
        "You are an automotive sales operations analyst. Below is a list of "
        "candidate sales 'plays' identified across our dealership network.",
        "",
        f"## Total candidate plays: {len(plays)}",
        "",
    ]
    for play_type, items in by_type.items():
        out.append(f"### {play_type} ({len(items)})")
        out.append("")
        for p in items[:MAX_PLAYS_PER_TYPE_IN_PROMPT]:
            out.append(
                f"- **{p.customer_name}** at {p.customer_dealer} — "
                f"{json.dumps(p.details, default=str)}"
            )
        if len(items) > MAX_PLAYS_PER_TYPE_IN_PROMPT:
            out.append(
                f"  …(+{len(items) - MAX_PLAYS_PER_TYPE_IN_PROMPT} more plays of this type)"
            )
        out.append("")

    out += [
        "## Your task",
        "",
        "Produce a salesperson-ready report in markdown that includes:",
        "",
        "1. **Top 10 priority plays** — ranked by likely revenue impact and ease of execution. "
        "For each: customer name, dealership, play type, two or three short suggested talking "
        "points, and the recommended next action.",
        "",
        "2. **Plays by type — summary statistics**: how many of each type, and a one-line "
        "characterization of the pattern (e.g., 'most service-time plays are XC60 owners "
        "ready for an XC90 upgrade').",
        "",
        "3. **Cross-dealer opportunities** — if any cross_dealer_transfer plays exist, "
        "highlight the most actionable transfers.",
        "",
        "Be specific. Use real customer names, real cars, real prices from the data above.",
    ]
    return "\n".join(out)


def generate_report(client: Anthropic, prompt: str) -> str:
    log.info("calling Claude for play report (%s)...", MODEL)
    msg = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS_REPORT,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Find sales 'plays' in the dealership inventory cache produced by "
            "dealership_agent.py. Generates synthetic customer/service data "
            "via Claude on first run."
        ),
    )
    parser.add_argument(
        "--cache", required=True, metavar="PATH",
        help="Path to the SQLite cache from dealership_agent.py",
    )
    parser.add_argument(
        "--regen", action="store_true",
        help="Regenerate customer data even if it already exists",
    )
    parser.add_argument(
        "--num-customers", type=int, default=DEFAULT_NUM_CUSTOMERS_PER_DEALER,
        help=f"Customers per dealership (default {DEFAULT_NUM_CUSTOMERS_PER_DEALER})",
    )
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
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY environment variable is required.")
        return 2

    with sqlite3.connect(args.cache) as conn:
        conn.row_factory = sqlite3.Row
        ensure_customer_tables(conn)

        tables_by_domain = map_tables_to_domains(conn)
        if not tables_by_domain:
            log.error(
                "no dealership tables in cache. Run dealership_agent.py first "
                "with --cache %s to populate.", args.cache
            )
            return 1
        log.info(
            "cache has %d dealership(s): %s",
            len(tables_by_domain), ", ".join(tables_by_domain.values()),
        )

        # Generate customer data per-dealership (skip if already present, unless --regen)
        if args.regen:
            clear_customer_tables(conn)
        present = existing_customer_domains(conn)

        domains_needing_gen = [
            d for d in tables_by_domain.values() if d not in present
        ]
        if domains_needing_gen:
            client = Anthropic()
            for domain in domains_needing_gen:
                table = next(
                    t for t, dom in tables_by_domain.items() if dom == domain
                )
                sample = conn.execute(
                    f"SELECT year, make, model, trim, price FROM {table} "
                    "WHERE year IS NOT NULL ORDER BY RANDOM() LIMIT 10"
                ).fetchall()
                if not sample:
                    log.warning("[%s] no inventory in cache, skipping", domain)
                    continue
                customers = generate_customers_for_dealership(
                    client, domain, sample, args.num_customers
                )
                if customers:
                    insert_customers(conn, domain, customers)
                    log.info("[%s] inserted %d customers", domain, len(customers))
        else:
            log.info("customer data already present; skipping generation (use --regen to refresh)")

        # Find plays
        log.info("finding candidate plays...")
        plays: list[Play] = []
        plays += find_service_time_plays(conn)
        plays += find_want_in_stock_plays(conn)
        plays += find_cross_dealer_plays(conn)
        plays += find_trade_in_plays(conn)

        if not plays:
            log.warning(
                "no candidate plays found across the cache. "
                "Try --regen to regenerate customer data, or run dealership_agent.py "
                "for more dealerships."
            )
            return 0
        log.info("found %d candidate plays:", len(plays))
        by_type: dict[str, int] = {}
        for p in plays:
            by_type[p.play_type] = by_type.get(p.play_type, 0) + 1
        for t, n in by_type.items():
            log.info("  %s: %d", t, n)

        # Generate the salesperson-ready report
        prompt = build_report_prompt(plays)
        log.info("report prompt: %d chars", len(prompt))
        client = Anthropic()
        try:
            report = generate_report(client, prompt)
        except Exception as e:
            log.error("Claude call failed: %s", e)
            return 1

        print(report)
        return 0


if __name__ == "__main__":
    sys.exit(main())
