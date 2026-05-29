"""
service_followup_agent.py — CLI agent that simulates post-service customer
satisfaction follow-ups for an automotive dealership. Reads/writes the same
SQLite cache used by dealership_agent.py, playfinder_agent.py, and
plate_notifier_agent.py.

Usage:
    # Option A: shell export
    export ANTHROPIC_API_KEY=...
    python service_followup_agent.py --cache inv.db

    # Option B: .env file in the current directory
    echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
    python service_followup_agent.py --cache inv.db

    python service_followup_agent.py --cache inv.db --today 2026-06-15
    python service_followup_agent.py --cache inv.db --regen-history

Pipeline (per invocation):
    1. Ensure schema; on first run (or --regen-history), generate ~20 past-
       dated service records per dealer via Claude, spread across the past
       ~120 days so the touchpoint cadence has something to fire on.
    2. Identify records due for their next touchpoint based on --today.
    3. Claude call #1: generate outgoing follow-up messages.
    4. Claude call #2: simulate customer responses to those messages,
       including occasional issue reports.
    5. For each touchpoint, write one or two .txt files in ./service_followups/
       (outgoing_*.txt always; incoming_*.txt only if customer responded).
    6. If any responses flagged as issues, mark the record and append to
       issues_flagged.txt. Flagged records are dropped from the cadence.

Touchpoint cadence (days after service completion):
    1 -> +1   initial satisfaction check
    2 -> +7   early issue catch
    3 -> +30  1-month quality check
    4 -> +90  long-term loyalty / next-service nudge
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

DEFAULT_RECORDS_PER_DEALER = 20
RECORDS_HISTORY_SPAN_DAYS = 120  # spread completed_dates over past N days
FOLLOWUPS_DIR = Path(os.environ["SANDBOX_DATA_DIR"]) / "dealership" / "service_followups"
ISSUES_FILE = FOLLOWUPS_DIR / "issues_flagged.txt"

TOUCHPOINT_OFFSETS = {1: 1, 2: 7, 3: 30, 4: 90}
MAX_TOUCHPOINTS = 4

# Cap how many touchpoints go into a single Claude call to keep prompts bounded
MAX_TOUCHPOINTS_PER_CALL = 50

_TABLE_NAME_RE = re.compile(r"^cars_[a-z0-9_]+$")

log = logging.getLogger("service_followup_agent")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS service_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            dealership_domain TEXT NOT NULL,
            vehicle_description TEXT NOT NULL,
            service_type TEXT NOT NULL,
            completed_date TEXT NOT NULL,
            issue_flagged INTEGER NOT NULL DEFAULT 0,
            issue_flagged_date TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_service_records_dealership
            ON service_records(dealership_domain);
        CREATE INDEX IF NOT EXISTS idx_service_records_flagged
            ON service_records(issue_flagged);

        CREATE TABLE IF NOT EXISTS service_followups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_record_id INTEGER NOT NULL,
            touchpoint_number INTEGER NOT NULL,
            sent_date TEXT NOT NULL,
            outgoing_subject TEXT,
            outgoing_body TEXT,
            customer_response TEXT,
            response_sentiment TEXT,  -- 'positive' | 'neutral' | 'issue' | 'no_response'
            FOREIGN KEY (service_record_id) REFERENCES service_records(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_service_followups_unique
            ON service_followups(service_record_id, touchpoint_number);
        """
    )
    conn.commit()


def list_dealership_domains(conn: sqlite3.Connection) -> list[str]:
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


def sample_vehicles(conn: sqlite3.Connection, domain: str, n: int) -> list[str]:
    """Random sample of `YYYY Make Model` strings from this dealer's inventory."""
    table = None
    for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'cars_%'"
    ).fetchall():
        if not _TABLE_NAME_RE.fullmatch(r["name"]):
            continue
        s = conn.execute(
            f"SELECT dealership_domain FROM {r['name']} LIMIT 1"
        ).fetchone()
        if s and s["dealership_domain"] == domain:
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
# Bootstrap: generate synthetic service records via Claude
# ---------------------------------------------------------------------------


_GEN_SYSTEM = (
    "You are generating realistic synthetic service-history records for a "
    "dealership simulation. Output ONLY a single JSON object matching the "
    "schema specified — no prose, no markdown, no code fences."
)


def _build_records_prompt(
    domain: str, vehicles: list[str], num_records: int, today: date
) -> str:
    earliest = (today - timedelta(days=RECORDS_HISTORY_SPAN_DAYS)).isoformat()
    yesterday = (today - timedelta(days=1)).isoformat()
    return f"""Generate {num_records} realistic completed service-history records for the dealership at {domain}.

Each record represents a service visit that has already been completed. The follow-up agent will use these to send post-service satisfaction touchpoints.

Vehicles this dealership services (sample of {len(vehicles)}):
{chr(10).join('- ' + v for v in vehicles)}

For each record, produce:
- customer_name: realistic first + last name
- vehicle_description: pick from the list above, OR make up a similar realistic one
- service_type: realistic dealership service. Examples: "Oil change & multi-point inspection", "30,000 mile service", "Brake pad replacement", "Battery replacement", "Tire rotation & alignment", "Transmission fluid service", "Cabin air filter replacement", "Annual inspection", "Recall remedy — software update"
- completed_date: YYYY-MM-DD between {earliest} and {yesterday}. Distribute these evenly across that range so the follow-up cadence (+1, +7, +30, +90 days) has touchpoints to fire across many records.

Output ONLY this JSON shape (no surrounding prose, no code fence):

{{
  "records": [
    {{
      "customer_name": "Sarah Chen",
      "vehicle_description": "2022 Volvo XC60 Plus",
      "service_type": "30,000 mile service",
      "completed_date": "{(today - timedelta(days=45)).isoformat()}"
    }}
  ]
}}

Generate exactly {num_records} records.
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


def generate_records_for_dealership(
    client: Anthropic, domain: str, vehicles: list[str],
    num_records: int, today: date,
) -> list[dict]:
    log.info("[%s] generating %d service records via Claude...", domain, num_records)
    prompt = _build_records_prompt(domain, vehicles, num_records, today)
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
    for r in parsed.get("records", []):
        if not all(r.get(k) for k in
                   ("customer_name", "vehicle_description", "service_type", "completed_date")):
            continue
        try:
            d = datetime.strptime(r["completed_date"], "%Y-%m-%d").date()
            if d >= today:
                continue  # must be past-dated
        except (TypeError, ValueError):
            continue
        valid.append(r)
    if len(valid) < len(parsed.get("records", [])):
        log.warning(
            "[%s] dropped %d invalid records",
            domain, len(parsed.get("records", [])) - len(valid),
        )
    return valid


def insert_records(conn: sqlite3.Connection, domain: str, records: list[dict]) -> None:
    conn.executemany(
        """INSERT INTO service_records
           (customer_name, dealership_domain, vehicle_description,
            service_type, completed_date)
           VALUES (?, ?, ?, ?, ?)""",
        [(r["customer_name"], domain, r["vehicle_description"],
          r["service_type"], r["completed_date"]) for r in records],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Find records due for their next touchpoint
# ---------------------------------------------------------------------------


@dataclass
class Touchpoint:
    record_id: int
    customer_name: str
    dealership_domain: str
    vehicle_description: str
    service_type: str
    completed_date: str
    touchpoint_number: int
    days_since_service: int


def find_due_touchpoints(conn: sqlite3.Connection, today: date) -> list[Touchpoint]:
    rows = conn.execute(
        """SELECT sr.id, sr.customer_name, sr.dealership_domain,
                  sr.vehicle_description, sr.service_type, sr.completed_date,
                  COUNT(sf.id) AS touchpoints_sent,
                  MAX(sf.sent_date) AS last_sent_date
           FROM service_records sr
           LEFT JOIN service_followups sf ON sf.service_record_id = sr.id
           WHERE sr.issue_flagged = 0
           GROUP BY sr.id"""
    ).fetchall()

    out: list[Touchpoint] = []
    today_iso = today.isoformat()
    for r in rows:
        sent = int(r["touchpoints_sent"] or 0)
        if sent >= MAX_TOUCHPOINTS:
            continue
        # Don't fire twice on the same simulated day
        if r["last_sent_date"] == today_iso:
            continue

        next_n = sent + 1
        target_offset = TOUCHPOINT_OFFSETS[next_n]
        completed = datetime.strptime(r["completed_date"], "%Y-%m-%d").date()
        days_since = (today - completed).days
        if days_since < target_offset:
            continue

        out.append(Touchpoint(
            record_id=r["id"],
            customer_name=r["customer_name"],
            dealership_domain=r["dealership_domain"],
            vehicle_description=r["vehicle_description"],
            service_type=r["service_type"],
            completed_date=r["completed_date"],
            touchpoint_number=next_n,
            days_since_service=days_since,
        ))
    return out


# ---------------------------------------------------------------------------
# Claude call #1: generate outgoing messages
# ---------------------------------------------------------------------------


def _tone_for_touchpoint(n: int) -> str:
    return {
        1: "warm and brief; thank them for their visit yesterday; one clear question about how everything went",
        2: "friendly check-in one week later; ask if everything's still running well; short and easy to reply to",
        3: "more formal one-month milestone; acknowledge the time elapsed; ask whether they've noticed anything since",
        4: "loyalty-focused three-month touchpoint; thank them, note that a next service may be coming up, no hard sell",
    }[n]


def _build_outgoing_prompt(touchpoints: list[Touchpoint], today: date) -> str:
    out = [
        "You are an automotive service-department customer-communication writer. "
        "For each touchpoint below, produce a short, professional email body "
        "(no greeting line, no signature — those will be added by the system).",
        "",
        f"Today's date: {today.isoformat()}",
        "",
        "Tone guide by touchpoint_number:",
        "  1 = " + _tone_for_touchpoint(1),
        "  2 = " + _tone_for_touchpoint(2),
        "  3 = " + _tone_for_touchpoint(3),
        "  4 = " + _tone_for_touchpoint(4),
        "",
        f"## {len(touchpoints)} touchpoints to write",
        "",
    ]
    for t in touchpoints:
        out.append(
            f"- record_id={t.record_id}, customer={t.customer_name!r}, "
            f"vehicle={t.vehicle_description!r}, service={t.service_type!r}, "
            f"dealer={t.dealership_domain}, "
            f"touchpoint_number={t.touchpoint_number}, "
            f"days_since_service={t.days_since_service}"
        )
    out += [
        "",
        "Output ONLY a JSON object of this exact shape (no code fence, no prose):",
        "",
        '{',
        '  "messages": [',
        '    {"record_id": <int>, "subject": "<email subject line>", "body": "<email body>"}',
        '  ]',
        '}',
        "",
        "Rules:",
        "- 'body' is 2–4 short sentences. Plain text, no markdown.",
        "- Reference the specific vehicle and service performed.",
        "- Make it easy for the customer to reply with concerns.",
        "- Match the tone guide for the touchpoint_number.",
        f"- Produce exactly {len(touchpoints)} messages, one per record_id listed above.",
    ]
    return "\n".join(out)


def generate_outgoing_messages(
    client: Anthropic, touchpoints: list[Touchpoint], today: date
) -> dict[int, dict]:
    log.info("calling Claude to write %d outgoing message(s)...", len(touchpoints))
    prompt = _build_outgoing_prompt(touchpoints, today)
    msg = client.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    try:
        parsed = _parse_json_loose(text)
    except json.JSONDecodeError as e:
        log.error("could not parse outgoing-messages JSON: %s", e)
        return {}

    by_id: dict[int, dict] = {}
    for m in parsed.get("messages", []):
        rid = m.get("record_id")
        if not isinstance(rid, int) or not m.get("subject") or not m.get("body"):
            continue
        by_id[rid] = {"subject": m["subject"], "body": m["body"]}
    return by_id


# ---------------------------------------------------------------------------
# Claude call #2: simulate customer responses
# ---------------------------------------------------------------------------


_SENTIMENT_VALUES = {"positive", "neutral", "issue", "no_response"}


def _build_responses_prompt(
    touchpoints: list[Touchpoint], outgoing: dict[int, dict], today: date
) -> str:
    out = [
        "You are simulating realistic customer responses to post-service "
        "follow-up emails from a car dealership. For each outgoing message "
        "below, generate the customer's reply.",
        "",
        f"Today's date: {today.isoformat()}",
        "",
        "Distribution target across all responses (approximate):",
        "  ~50% no_response  — customer simply doesn't reply (sentiment='no_response', response='')",
        "  ~35% positive     — short, satisfied reply",
        "  ~10% neutral      — non-committal acknowledgment, no concerns",
        "  ~5%  issue        — customer reports a real problem with the service",
        "",
        "When sentiment='issue', make it a specific, plausible automotive issue tied to the service",
        "(e.g., 'brake noise has returned after the pad replacement', 'oil leak noticed in driveway",
        "two days later', 'check engine light came on a week after the software update').",
        "",
        "When sentiment='no_response', set response to an empty string.",
        "",
        f"## {len(touchpoints)} outgoing messages to respond to",
        "",
    ]
    for t in touchpoints:
        m = outgoing.get(t.record_id, {})
        out.append(
            f"- record_id={t.record_id}, customer={t.customer_name!r}, "
            f"vehicle={t.vehicle_description!r}, service={t.service_type!r}, "
            f"touchpoint_number={t.touchpoint_number}, "
            f"days_since_service={t.days_since_service}"
        )
        if m:
            subj = m["subject"].replace("\n", " ")
            body = m["body"].replace("\n", " ")
            out.append(f"    outgoing_subject: {subj}")
            out.append(f"    outgoing_body: {body}")
    out += [
        "",
        "Output ONLY a JSON object of this exact shape (no code fence, no prose):",
        "",
        '{',
        '  "responses": [',
        '    {"record_id": <int>, "sentiment": "positive|neutral|issue|no_response", "response": "<reply text or empty>"}',
        '  ]',
        '}',
        "",
        f"Produce exactly {len(touchpoints)} responses, one per record_id listed above.",
        "Keep responses brief (1-3 sentences) and conversational, like a real email reply.",
    ]
    return "\n".join(out)


def generate_responses(
    client: Anthropic, touchpoints: list[Touchpoint],
    outgoing: dict[int, dict], today: date,
) -> dict[int, dict]:
    log.info("calling Claude to simulate %d customer response(s)...", len(touchpoints))
    prompt = _build_responses_prompt(touchpoints, outgoing, today)
    msg = client.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    try:
        parsed = _parse_json_loose(text)
    except json.JSONDecodeError as e:
        log.error("could not parse responses JSON: %s", e)
        return {}

    by_id: dict[int, dict] = {}
    for r in parsed.get("responses", []):
        rid = r.get("record_id")
        sentiment = r.get("sentiment")
        if not isinstance(rid, int) or sentiment not in _SENTIMENT_VALUES:
            continue
        by_id[rid] = {
            "sentiment": sentiment,
            "response": (r.get("response") or "").strip(),
        }
    return by_id


# ---------------------------------------------------------------------------
# Writing files + recording in DB
# ---------------------------------------------------------------------------


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


def write_outgoing_file(
    out_dir: Path, t: Touchpoint, subject: str, body: str, today: date
) -> Path:
    fn = (
        f"outgoing_{today.isoformat()}_record{t.record_id:04d}"
        f"_t{t.touchpoint_number}_{_safe_name(t.customer_name)}.txt"
    )
    path = out_dir / fn
    path.write_text(
        f"From: {t.dealership_domain} <service@{t.dealership_domain}>\n"
        f"To: {t.customer_name}\n"
        f"Date: {today.isoformat()}\n"
        f"Subject: {subject}\n"
        f"X-Touchpoint: {t.touchpoint_number} of {MAX_TOUCHPOINTS}\n"
        f"X-Record-Id: {t.record_id}\n"
        f"X-Vehicle: {t.vehicle_description}\n"
        f"X-Service: {t.service_type}\n"
        f"X-Days-Since-Service: {t.days_since_service}\n"
        "\n"
        f"Dear {t.customer_name},\n\n"
        f"{body}\n\n"
        "Best regards,\n"
        f"The service team at {t.dealership_domain}\n"
    )
    return path


def write_incoming_file(
    out_dir: Path, t: Touchpoint, response: str, sentiment: str, today: date
) -> Path:
    fn = (
        f"incoming_{today.isoformat()}_record{t.record_id:04d}"
        f"_t{t.touchpoint_number}_{_safe_name(t.customer_name)}.txt"
    )
    path = out_dir / fn
    path.write_text(
        f"From: {t.customer_name}\n"
        f"To: {t.dealership_domain} <service@{t.dealership_domain}>\n"
        f"Date: {today.isoformat()}\n"
        f"Subject: Re: (follow-up touchpoint {t.touchpoint_number})\n"
        f"X-Record-Id: {t.record_id}\n"
        f"X-Sentiment: {sentiment}\n"
        "\n"
        f"{response}\n"
    )
    return path


def append_issue_summary(
    t: Touchpoint, response: str, today: date
) -> None:
    FOLLOWUPS_DIR.mkdir(exist_ok=True)
    with ISSUES_FILE.open("a", encoding="utf-8") as f:
        f.write(
            f"[{today.isoformat()}] record {t.record_id} "
            f"({t.customer_name} @ {t.dealership_domain})\n"
            f"  Vehicle: {t.vehicle_description}\n"
            f"  Service: {t.service_type} (completed {t.completed_date}, "
            f"{t.days_since_service} days ago)\n"
            f"  Touchpoint: {t.touchpoint_number}\n"
            f"  Customer report: {response}\n"
            f"  Action: cadence stopped — needs human follow-up.\n\n"
        )


def record_touchpoint(
    conn: sqlite3.Connection, t: Touchpoint,
    subject: str, body: str,
    response: str, sentiment: str, today: date,
) -> None:
    conn.execute(
        """INSERT INTO service_followups
           (service_record_id, touchpoint_number, sent_date,
            outgoing_subject, outgoing_body,
            customer_response, response_sentiment)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (t.record_id, t.touchpoint_number, today.isoformat(),
         subject, body, response or None, sentiment),
    )
    if sentiment == "issue":
        conn.execute(
            "UPDATE service_records SET issue_flagged=1, issue_flagged_date=? "
            "WHERE id=?",
            (today.isoformat(), t.record_id),
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
            "Simulate post-service customer satisfaction follow-ups over the "
            "SQLite cache produced by dealership_agent.py."
        ),
    )
    parser.add_argument("--cache", required=True, metavar="PATH",
                        help="Path to the SQLite cache from dealership_agent.py")
    parser.add_argument("--today", metavar="YYYY-MM-DD",
                        help="Override the simulated current date (default: real today)")
    parser.add_argument("--regen-history", action="store_true",
                        help="Wipe service_records + service_followups and regenerate")
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
        ensure_tables(conn)

        domains = list_dealership_domains(conn)
        if not domains:
            log.error(
                "no dealership tables in cache. Run dealership_agent.py first "
                "with --cache %s to populate.", args.cache
            )
            return 1

        if args.regen_history:
            log.info("wiping existing service_records + service_followups")
            conn.executescript(
                "DELETE FROM service_followups; DELETE FROM service_records;"
            )
            conn.commit()

        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM service_records"
        ).fetchone()["n"]

        if existing == 0:
            client = Anthropic()
            for domain in domains:
                vehicles = sample_vehicles(conn, domain, 10)
                if not vehicles:
                    log.warning("[%s] no inventory in cache, skipping", domain)
                    continue
                records = generate_records_for_dealership(
                    client, domain, vehicles, DEFAULT_RECORDS_PER_DEALER, today
                )
                if records:
                    insert_records(conn, domain, records)
                    log.info("[%s] inserted %d service records", domain, len(records))
        else:
            log.info(
                "service_records already populated (%d rows); use --regen-history to refresh",
                existing,
            )

        due = find_due_touchpoints(conn, today)
        if not due:
            log.info("no touchpoints due today.")
            return 0

        if len(due) > MAX_TOUCHPOINTS_PER_CALL:
            log.warning(
                "%d touchpoints due; capping at %d. Re-run to process the rest.",
                len(due), MAX_TOUCHPOINTS_PER_CALL,
            )
            due = due[:MAX_TOUCHPOINTS_PER_CALL]
        log.info("%d touchpoint(s) due", len(due))
        # Per-touchpoint breakdown for visibility
        by_n: dict[int, int] = {}
        for t in due:
            by_n[t.touchpoint_number] = by_n.get(t.touchpoint_number, 0) + 1
        for n, count in sorted(by_n.items()):
            log.info("  touchpoint %d: %d", n, count)

        client = Anthropic()
        try:
            outgoing = generate_outgoing_messages(client, due, today)
        except Exception as e:
            log.error("outgoing-message Claude call failed: %s", e)
            return 1

        try:
            responses = generate_responses(client, due, outgoing, today)
        except Exception as e:
            log.error("response-simulation Claude call failed: %s", e)
            return 1

        FOLLOWUPS_DIR.mkdir(exist_ok=True)
        outgoing_count = 0
        incoming_count = 0
        issues_count = 0
        skipped = 0

        for t in due:
            m = outgoing.get(t.record_id)
            if not m:
                log.warning(
                    "[record %d] no outgoing message returned by Claude, skipping",
                    t.record_id,
                )
                skipped += 1
                continue

            r = responses.get(t.record_id) or {"sentiment": "no_response", "response": ""}
            sentiment = r["sentiment"]
            response_text = r["response"] if sentiment != "no_response" else ""

            path_out = write_outgoing_file(
                FOLLOWUPS_DIR, t, m["subject"], m["body"], today
            )
            outgoing_count += 1
            print(path_out)

            if sentiment != "no_response" and response_text:
                path_in = write_incoming_file(
                    FOLLOWUPS_DIR, t, response_text, sentiment, today
                )
                incoming_count += 1
                print(path_in)

            if sentiment == "issue":
                append_issue_summary(t, response_text, today)
                issues_count += 1

            record_touchpoint(
                conn, t, m["subject"], m["body"], response_text, sentiment, today
            )

        conn.commit()

        log.info(
            "wrote %d outgoing + %d incoming file(s) to %s/ (skipped %d, flagged %d issue(s))",
            outgoing_count, incoming_count, FOLLOWUPS_DIR, skipped, issues_count,
        )
        if issues_count:
            log.info("issues summary appended to: %s", ISSUES_FILE)
        return 0


if __name__ == "__main__":
    sys.exit(main())
