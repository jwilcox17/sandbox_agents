"""
survey_reminder_agent.py — CLI agent that simulates post-purchase customer
survey campaigns with reminder sequences. Reads/writes the same SQLite cache
used by the other agents in this suite.

Usage:
    # Option A: shell export
    export ANTHROPIC_API_KEY=...
    python survey_reminder_agent.py --cache inv.db

    # Option B: .env file in the current directory
    echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
    python survey_reminder_agent.py --cache inv.db

    python survey_reminder_agent.py --cache inv.db --today 2026-07-01
    python survey_reminder_agent.py --cache inv.db --regen-invites

Pipeline (per invocation):
    1. Ensure schema; on first run (or --regen-invites), generate ~25 past-
       dated purchase records per dealer via Claude so the cadence has
       something to fire on.
    2. Identify invites due for their next touchpoint based on --today.
    3. Claude call #1: generate outgoing survey/reminder messages.
    4. Claude call #2: simulate customer responses (mostly no_response;
       some completions with NPS + feedback; a few opt-outs).
    5. Write outgoing_*.txt always; incoming_*.txt when customer responded.
       Surveys that get completed or opted-out have their cadence stopped.

Touchpoint cadence (days after purchase):
    0 -> +3   initial survey invite
    1 -> +8   reminder 1 (gentle)
    2 -> +15  reminder 2 (still polite)
    3 -> +24  reminder 3 (final — explicitly notes it's the last one)
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8000

DEFAULT_INVITES_PER_DEALER = 25
INVITES_HISTORY_SPAN_DAYS = 30
SURVEYS_DIR = Path("/logs/surveys")

TOUCHPOINT_OFFSETS = {0: 3, 1: 8, 2: 15, 3: 24}  # days after purchase_date
MAX_TOUCHPOINTS = 4  # touchpoints 0..3 (initial + 3 reminders)

MAX_TOUCHPOINTS_PER_CALL = 50

_TABLE_NAME_RE = re.compile(r"^cars_[a-z0-9_]+$")

log = logging.getLogger("survey_reminder_agent")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS survey_invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            dealership_domain TEXT NOT NULL,
            vehicle_purchased TEXT NOT NULL,
            purchase_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending | completed | opted_out
            completed_date TEXT,
            nps_score INTEGER,
            feedback_summary TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_survey_invites_status
            ON survey_invites(status);
        CREATE INDEX IF NOT EXISTS idx_survey_invites_dealership
            ON survey_invites(dealership_domain);

        CREATE TABLE IF NOT EXISTS survey_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            survey_invite_id INTEGER NOT NULL,
            touchpoint_number INTEGER NOT NULL,
            sent_date TEXT NOT NULL,
            outgoing_subject TEXT,
            outgoing_body TEXT,
            customer_response TEXT,
            response_outcome TEXT,  -- 'no_response' | 'completed' | 'opted_out'
            FOREIGN KEY (survey_invite_id) REFERENCES survey_invites(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_survey_reminders_unique
            ON survey_reminders(survey_invite_id, touchpoint_number);
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
# Bootstrap: generate synthetic purchase records via Claude
# ---------------------------------------------------------------------------


_GEN_SYSTEM = (
    "You are generating realistic synthetic vehicle-purchase records for a "
    "dealership simulation. Output ONLY a single JSON object matching the "
    "schema specified — no prose, no markdown, no code fences."
)


def _build_invites_prompt(
    domain: str, vehicles: list[str], num: int, today: date
) -> str:
    earliest = (today - timedelta(days=INVITES_HISTORY_SPAN_DAYS)).isoformat()
    yesterday = (today - timedelta(days=1)).isoformat()
    return f"""Generate {num} realistic vehicle-purchase records for the dealership at {domain}.

Each represents a customer who recently purchased a vehicle and will receive a satisfaction survey.

Vehicles this dealership sells (sample of {len(vehicles)}):
{chr(10).join('- ' + v for v in vehicles)}

For each record, produce:
- customer_name: realistic first + last name
- vehicle_purchased: pick from the list above OR a similar realistic vehicle
- purchase_date: YYYY-MM-DD between {earliest} and {yesterday}. Distribute evenly across that range so the survey cadence (+3, +8, +15, +24 days) has touchpoints to fire at different stages.

Output ONLY this JSON shape (no surrounding prose, no code fence):

{{
  "invites": [
    {{
      "customer_name": "Sarah Chen",
      "vehicle_purchased": "2024 Volvo XC90 T8 Recharge",
      "purchase_date": "{(today - timedelta(days=10)).isoformat()}"
    }}
  ]
}}

Generate exactly {num} records.
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


def generate_invites_for_dealership(
    client: Anthropic, domain: str, vehicles: list[str],
    num: int, today: date,
) -> list[dict]:
    log.info("[%s] generating %d survey invites via Claude...", domain, num)
    prompt = _build_invites_prompt(domain, vehicles, num, today)
    msg = client.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS,
        system=_GEN_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    try:
        parsed = _parse_json_loose(text)
    except json.JSONDecodeError as e:
        log.error("[%s] could not parse JSON: %s", domain, e)
        return []

    valid = []
    for r in parsed.get("invites", []):
        if not all(r.get(k) for k in ("customer_name", "vehicle_purchased", "purchase_date")):
            continue
        try:
            d = datetime.strptime(r["purchase_date"], "%Y-%m-%d").date()
            if d >= today:
                continue
        except (TypeError, ValueError):
            continue
        valid.append(r)
    if len(valid) < len(parsed.get("invites", [])):
        log.warning(
            "[%s] dropped %d invalid records",
            domain, len(parsed.get("invites", [])) - len(valid),
        )
    return valid


def insert_invites(conn: sqlite3.Connection, domain: str, records: list[dict]) -> None:
    conn.executemany(
        """INSERT INTO survey_invites
           (customer_name, dealership_domain, vehicle_purchased, purchase_date)
           VALUES (?, ?, ?, ?)""",
        [(r["customer_name"], domain, r["vehicle_purchased"], r["purchase_date"])
         for r in records],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Find invites due for their next touchpoint
# ---------------------------------------------------------------------------


@dataclass
class Touchpoint:
    invite_id: int
    customer_name: str
    dealership_domain: str
    vehicle_purchased: str
    purchase_date: str
    touchpoint_number: int  # 0 = initial, 1..3 = reminders
    days_since_purchase: int


def find_due_touchpoints(conn: sqlite3.Connection, today: date) -> list[Touchpoint]:
    rows = conn.execute(
        """SELECT si.id, si.customer_name, si.dealership_domain,
                  si.vehicle_purchased, si.purchase_date,
                  COUNT(sr.id) AS touchpoints_sent,
                  MAX(sr.sent_date) AS last_sent_date
           FROM survey_invites si
           LEFT JOIN survey_reminders sr ON sr.survey_invite_id = si.id
           WHERE si.status = 'pending'
           GROUP BY si.id"""
    ).fetchall()

    out: list[Touchpoint] = []
    today_iso = today.isoformat()
    for r in rows:
        sent = int(r["touchpoints_sent"] or 0)
        if sent >= MAX_TOUCHPOINTS:
            continue
        if r["last_sent_date"] == today_iso:
            continue

        next_n = sent  # 0 if none sent yet -> initial; 1 if one sent -> reminder 1; etc.
        target_offset = TOUCHPOINT_OFFSETS[next_n]
        purchase = datetime.strptime(r["purchase_date"], "%Y-%m-%d").date()
        days_since = (today - purchase).days
        if days_since < target_offset:
            continue

        out.append(Touchpoint(
            invite_id=r["id"],
            customer_name=r["customer_name"],
            dealership_domain=r["dealership_domain"],
            vehicle_purchased=r["vehicle_purchased"],
            purchase_date=r["purchase_date"],
            touchpoint_number=next_n,
            days_since_purchase=days_since,
        ))
    return out


# ---------------------------------------------------------------------------
# Claude call #1: outgoing messages
# ---------------------------------------------------------------------------


def _tone_for_touchpoint(n: int) -> str:
    return {
        0: "warm and brief; thank them for their recent purchase and invite them to take a short satisfaction survey; one clear call-to-action",
        1: "gentle reminder; acknowledge they're busy; keep it short; remind them the survey takes only a few minutes",
        2: "still respectful and patient; emphasize that their feedback genuinely shapes how the dealership improves",
        3: "this is the final reminder — explicitly state it is the last one and that they will not be contacted again about this survey; thank them regardless of whether they respond",
    }[n]


def _build_outgoing_prompt(touchpoints: list[Touchpoint], today: date) -> str:
    out = [
        "You are an automotive customer-experience writer. For each touchpoint "
        "below, produce a short, respectful email body (no greeting line, no "
        "signature — the system adds those).",
        "",
        f"Today's date: {today.isoformat()}",
        "",
        "Tone guide by touchpoint_number:",
        "  0 = " + _tone_for_touchpoint(0),
        "  1 = " + _tone_for_touchpoint(1),
        "  2 = " + _tone_for_touchpoint(2),
        "  3 = " + _tone_for_touchpoint(3),
        "",
        f"## {len(touchpoints)} touchpoints to write",
        "",
    ]
    for t in touchpoints:
        out.append(
            f"- invite_id={t.invite_id}, customer={t.customer_name!r}, "
            f"vehicle={t.vehicle_purchased!r}, dealer={t.dealership_domain}, "
            f"touchpoint_number={t.touchpoint_number}, "
            f"days_since_purchase={t.days_since_purchase}"
        )
    out += [
        "",
        "Output ONLY a JSON object of this exact shape (no code fence, no prose):",
        "",
        '{',
        '  "messages": [',
        '    {"invite_id": <int>, "subject": "<email subject>", "body": "<email body>"}',
        '  ]',
        '}',
        "",
        "Rules:",
        "- 'body' is 2–4 short sentences. Plain text, no markdown.",
        "- Reference the specific vehicle.",
        "- Include a clear (fictitious) survey link, e.g. 'https://survey.<dealer-domain>/<invite_id>'.",
        "- Always provide an unsubscribe option ('reply STOP to opt out of further surveys').",
        "- Match the tone guide for the touchpoint_number. For touchpoint 3, make the 'this is our last reminder' message unmistakable.",
        f"- Produce exactly {len(touchpoints)} messages, one per invite_id listed above.",
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
        iid = m.get("invite_id")
        if not isinstance(iid, int) or not m.get("subject") or not m.get("body"):
            continue
        by_id[iid] = {"subject": m["subject"], "body": m["body"]}
    return by_id


# ---------------------------------------------------------------------------
# Claude call #2: simulate customer responses
# ---------------------------------------------------------------------------


_OUTCOME_VALUES = {"no_response", "completed", "opted_out"}


def _build_responses_prompt(
    touchpoints: list[Touchpoint], outgoing: dict[int, dict], today: date
) -> str:
    out = [
        "You are simulating realistic customer responses to post-purchase "
        "satisfaction-survey reminders from a car dealership. For each "
        "outgoing message below, decide whether the customer responds and "
        "how.",
        "",
        f"Today's date: {today.isoformat()}",
        "",
        "Distribution targets:",
        "  Initial invite (touchpoint 0): ~75% no_response, ~22% completed, ~3% opted_out",
        "  Reminder 1 (touchpoint 1):     ~85% no_response, ~12% completed, ~3% opted_out",
        "  Reminder 2 (touchpoint 2):     ~90% no_response, ~7% completed, ~3% opted_out",
        "  Reminder 3 (touchpoint 3):     ~93% no_response, ~5% completed, ~2% opted_out",
        "",
        "When outcome='completed', also produce:",
        "  - nps_score: integer 0..10 (most customers in the 7-10 range; a few critics in 0-6)",
        "  - feedback_summary: one short sentence summarizing what they said (e.g.,",
        "    'Loves the car, smooth purchase process.' or 'Disappointed with the financing wait.').",
        "  - response: 1-3 sentence email reply (the body they actually sent).",
        "",
        "When outcome='opted_out': response is a brief polite opt-out reply like",
        "  'Please stop sending these — thanks.' nps_score is null.",
        "",
        "When outcome='no_response': response is empty, nps_score is null.",
        "",
        f"## {len(touchpoints)} outgoing messages to respond to",
        "",
    ]
    for t in touchpoints:
        m = outgoing.get(t.invite_id, {})
        out.append(
            f"- invite_id={t.invite_id}, customer={t.customer_name!r}, "
            f"vehicle={t.vehicle_purchased!r}, "
            f"touchpoint_number={t.touchpoint_number}, "
            f"days_since_purchase={t.days_since_purchase}"
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
        '    {"invite_id": <int>, "outcome": "no_response|completed|opted_out",',
        '     "response": "<reply text or empty>",',
        '     "nps_score": <int 0-10 or null>,',
        '     "feedback_summary": "<short summary or null>"}',
        '  ]',
        '}',
        "",
        f"Produce exactly {len(touchpoints)} responses, one per invite_id listed above.",
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
        iid = r.get("invite_id")
        outcome = r.get("outcome")
        if not isinstance(iid, int) or outcome not in _OUTCOME_VALUES:
            continue
        nps = r.get("nps_score")
        try:
            nps = int(nps) if nps is not None else None
            if nps is not None and not (0 <= nps <= 10):
                nps = None
        except (TypeError, ValueError):
            nps = None
        by_id[iid] = {
            "outcome": outcome,
            "response": (r.get("response") or "").strip(),
            "nps_score": nps,
            "feedback_summary": (r.get("feedback_summary") or "").strip() or None,
        }
    return by_id


# ---------------------------------------------------------------------------
# Writing files + recording in DB
# ---------------------------------------------------------------------------


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


_LABEL = {0: "initial", 1: "reminder1", 2: "reminder2", 3: "reminder3_final"}


def write_outgoing_file(
    out_dir: Path, t: Touchpoint, subject: str, body: str, today: date
) -> Path:
    fn = (
        f"outgoing_{today.isoformat()}_invite{t.invite_id:04d}"
        f"_{_LABEL[t.touchpoint_number]}_{_safe_name(t.customer_name)}.txt"
    )
    path = out_dir / fn
    path.write_text(
        f"From: {t.dealership_domain} <surveys@{t.dealership_domain}>\n"
        f"To: {t.customer_name}\n"
        f"Date: {today.isoformat()}\n"
        f"Subject: {subject}\n"
        f"X-Touchpoint: {_LABEL[t.touchpoint_number]} ({t.touchpoint_number}/{MAX_TOUCHPOINTS - 1})\n"
        f"X-Invite-Id: {t.invite_id}\n"
        f"X-Vehicle: {t.vehicle_purchased}\n"
        f"X-Days-Since-Purchase: {t.days_since_purchase}\n"
        "\n"
        f"Dear {t.customer_name},\n\n"
        f"{body}\n\n"
        "Thank you,\n"
        f"The team at {t.dealership_domain}\n"
    )
    return path


def write_incoming_file(
    out_dir: Path, t: Touchpoint, response: str, outcome: str,
    nps: Optional[int], summary: Optional[str], today: date,
) -> Path:
    fn = (
        f"incoming_{today.isoformat()}_invite{t.invite_id:04d}"
        f"_{outcome}_{_safe_name(t.customer_name)}.txt"
    )
    path = out_dir / fn
    extra = ""
    if outcome == "completed":
        extra = (
            f"X-NPS-Score: {nps if nps is not None else 'N/A'}\n"
            f"X-Feedback-Summary: {summary or 'N/A'}\n"
        )
    path.write_text(
        f"From: {t.customer_name}\n"
        f"To: {t.dealership_domain} <surveys@{t.dealership_domain}>\n"
        f"Date: {today.isoformat()}\n"
        f"Subject: Re: {_LABEL[t.touchpoint_number]} (invite {t.invite_id})\n"
        f"X-Invite-Id: {t.invite_id}\n"
        f"X-Outcome: {outcome}\n"
        f"{extra}"
        "\n"
        f"{response}\n"
    )
    return path


def record_touchpoint(
    conn: sqlite3.Connection, t: Touchpoint,
    subject: str, body: str,
    response: str, outcome: str,
    nps: Optional[int], summary: Optional[str], today: date,
) -> None:
    conn.execute(
        """INSERT INTO survey_reminders
           (survey_invite_id, touchpoint_number, sent_date,
            outgoing_subject, outgoing_body,
            customer_response, response_outcome)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (t.invite_id, t.touchpoint_number, today.isoformat(),
         subject, body, response or None, outcome),
    )
    if outcome == "completed":
        conn.execute(
            """UPDATE survey_invites
               SET status='completed', completed_date=?, nps_score=?, feedback_summary=?
               WHERE id=?""",
            (today.isoformat(), nps, summary, t.invite_id),
        )
    elif outcome == "opted_out":
        conn.execute(
            "UPDATE survey_invites SET status='opted_out' WHERE id=?",
            (t.invite_id,),
        )


# ---------------------------------------------------------------------------
# .env loader (stdlib-only, no python-dotenv dependency)
# ---------------------------------------------------------------------------


def load_dotenv(path: str = ".env") -> int:
    """Load KEY=value lines from a .env file into os.environ.

    Already-set process-environment values win (shell export takes precedence
    over the file). Supports unquoted, "double-quoted", and 'single-quoted'
    values; ignores blank lines and `#` comments. Returns the number of
    variables actually loaded (skipped ones don't count).
    """
    if not os.path.exists(path):
        return 0
    loaded = 0
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if (len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"')):
                value = value[1:-1]
            if not key or key in os.environ:
                continue
            os.environ[key] = value
            loaded += 1
    return loaded


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Simulate post-purchase customer survey reminder campaigns over "
            "the SQLite cache produced by dealership_agent.py."
        ),
    )
    parser.add_argument("--cache", required=True, metavar="PATH",
                        help="Path to the SQLite cache from dealership_agent.py")
    parser.add_argument("--today", metavar="YYYY-MM-DD",
                        help="Override the simulated current date (default: real today)")
    parser.add_argument("--regen-invites", action="store_true",
                        help="Wipe survey_invites + survey_reminders and regenerate")
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
    loaded = load_dotenv()
    if loaded:
        log.info("loaded %d variable(s) from .env", loaded)

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

        if args.regen_invites:
            log.info("wiping existing survey_invites + survey_reminders")
            conn.executescript(
                "DELETE FROM survey_reminders; DELETE FROM survey_invites;"
            )
            conn.commit()

        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM survey_invites"
        ).fetchone()["n"]

        if existing == 0:
            client = Anthropic()
            for domain in domains:
                vehicles = sample_vehicles(conn, domain, 10)
                if not vehicles:
                    log.warning("[%s] no inventory in cache, skipping", domain)
                    continue
                invites = generate_invites_for_dealership(
                    client, domain, vehicles, DEFAULT_INVITES_PER_DEALER, today
                )
                if invites:
                    insert_invites(conn, domain, invites)
                    log.info("[%s] inserted %d invites", domain, len(invites))
        else:
            log.info(
                "survey_invites already populated (%d rows); use --regen-invites to refresh",
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
        by_n: dict[int, int] = {}
        for t in due:
            by_n[t.touchpoint_number] = by_n.get(t.touchpoint_number, 0) + 1
        for n, count in sorted(by_n.items()):
            log.info("  %s: %d", _LABEL[n], count)

        client = Anthropic()
        try:
            outgoing = generate_outgoing_messages(client, due, today)
        except Exception as e:
            log.error("outgoing Claude call failed: %s", e)
            return 1

        try:
            responses = generate_responses(client, due, outgoing, today)
        except Exception as e:
            log.error("response-simulation Claude call failed: %s", e)
            return 1

        SURVEYS_DIR.mkdir(exist_ok=True)
        out_count = 0
        in_count = 0
        completed_count = 0
        optout_count = 0

        for t in due:
            m = outgoing.get(t.invite_id)
            if not m:
                log.warning(
                    "[invite %d] no outgoing message returned by Claude, skipping",
                    t.invite_id,
                )
                continue

            r = responses.get(t.invite_id) or {
                "outcome": "no_response", "response": "",
                "nps_score": None, "feedback_summary": None,
            }
            outcome = r["outcome"]
            response = r["response"] if outcome != "no_response" else ""

            path_out = write_outgoing_file(
                SURVEYS_DIR, t, m["subject"], m["body"], today
            )
            out_count += 1
            print(path_out)

            if outcome != "no_response" and response:
                path_in = write_incoming_file(
                    SURVEYS_DIR, t, response, outcome,
                    r["nps_score"], r["feedback_summary"], today,
                )
                in_count += 1
                print(path_in)

            if outcome == "completed":
                completed_count += 1
            elif outcome == "opted_out":
                optout_count += 1

            record_touchpoint(
                conn, t, m["subject"], m["body"], response,
                outcome, r["nps_score"], r["feedback_summary"], today,
            )

        conn.commit()

        log.info(
            "wrote %d outgoing + %d incoming file(s) to %s/ "
            "(completed: %d, opted_out: %d)",
            out_count, in_count, SURVEYS_DIR, completed_count, optout_count,
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
