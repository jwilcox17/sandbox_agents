"""
Lab Appointment Scheduling Agent — single-file edition.

Reads patient + lab-order data from the SQLite DB populated by
patient_generator.py (patients.db by default), and writes appointment
records back to the same DB. Uses Anthropic's tool-use API to schedule,
confirm, reschedule, and cancel patient lab appointments across Quest
Diagnostics and LabCorp.

    pip install anthropic python-dotenv
    # put ANTHROPIC_API_KEY=sk-ant-... in a .env file next to this script
    python lab_scheduling_agent.py
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import textwrap
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from anthropic import Anthropic

# Load ANTHROPIC_API_KEY (and PATIENTS_DB if you want) from a .env file in the
# current directory. Real shell env vars still take precedence.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional; env vars work without it

# ============================================================
# DB
# ============================================================

DB_PATH = os.environ.get(
    "PATIENTS_DB",
    os.path.join(os.environ.get("SANDBOX_DATA_DIR", ""), "hospital", "patients.db")
)

# This agent owns lab_locations + appointments. The patient tables
# (patients, lab_orders, ...) are owned by patient_generator.py.
# Both files use CREATE TABLE IF NOT EXISTS so either can be run first.
SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_locations (
    lab_id   TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    name     TEXT NOT NULL,
    address  TEXT NOT NULL,
    city     TEXT NOT NULL,
    state    TEXT NOT NULL,
    zip      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS appointments (
    confirmation_number TEXT PRIMARY KEY,
    patient_id    TEXT NOT NULL,
    lab_id        TEXT NOT NULL,
    provider      TEXT NOT NULL,
    datetime      TEXT NOT NULL,
    order_ids     TEXT NOT NULL,           -- JSON array of order_id strings
    status        TEXT NOT NULL DEFAULT 'scheduled',
    cancel_reason TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appts_patient ON appointments(patient_id);
CREATE INDEX IF NOT EXISTS idx_appts_lab     ON appointments(lab_id);
"""

# Seed catalog. Realistic-looking sites spread across major metros.
LAB_LOCATION_SEEDS = [
    # Quest Diagnostics
    ("QD-SF-001", "Quest Diagnostics", "Quest Diagnostics — Mission St",
     "2480 Mission St", "San Francisco", "CA", "94110"),
    ("QD-PA-007", "Quest Diagnostics", "Quest Diagnostics — Menlo Park",
     "1300 El Camino Real", "Menlo Park", "CA", "94025"),
    ("QD-OAK-003", "Quest Diagnostics", "Quest Diagnostics — Oakland Fruitvale",
     "3300 E 14th St", "Oakland", "CA", "94601"),
    ("QD-LA-012", "Quest Diagnostics", "Quest Diagnostics — Downtown LA",
     "800 S Figueroa St", "Los Angeles", "CA", "90017"),
    ("QD-NYC-021", "Quest Diagnostics", "Quest Diagnostics — Midtown East",
     "240 E 38th St", "New York", "NY", "10016"),
    ("QD-CHI-031", "Quest Diagnostics", "Quest Diagnostics — Chicago Loop",
     "30 S Wacker Dr", "Chicago", "IL", "60606"),
    ("QD-DAL-042", "Quest Diagnostics", "Quest Diagnostics — Dallas Uptown",
     "2727 Lemmon Ave", "Dallas", "TX", "75204"),
    ("QD-HOU-049", "Quest Diagnostics", "Quest Diagnostics — Houston Medical Center",
     "6624 Fannin St", "Houston", "TX", "77030"),
    ("QD-MIA-055", "Quest Diagnostics", "Quest Diagnostics — Miami Brickell",
     "1390 S Dixie Hwy", "Miami", "FL", "33146"),
    ("QD-ATL-061", "Quest Diagnostics", "Quest Diagnostics — Midtown Atlanta",
     "1100 Peachtree St NE", "Atlanta", "GA", "30309"),
    ("QD-SEA-073", "Quest Diagnostics", "Quest Diagnostics — Seattle Capitol Hill",
     "1101 Madison St", "Seattle", "WA", "98104"),
    ("QD-PHX-088", "Quest Diagnostics", "Quest Diagnostics — Phoenix Camelback",
     "1300 N Central Ave", "Phoenix", "AZ", "85004"),
    # LabCorp
    ("LC-SF-014", "LabCorp", "LabCorp — Castro",
     "2300 Market St", "San Francisco", "CA", "94114"),
    ("LC-PA-021", "LabCorp", "LabCorp — Palo Alto",
     "795 El Camino Real", "Palo Alto", "CA", "94301"),
    ("LC-OAK-009", "LabCorp", "LabCorp — Downtown Oakland",
     "350 30th St", "Oakland", "CA", "94609"),
    ("LC-LA-018", "LabCorp", "LabCorp — Beverly Hills",
     "8631 W 3rd St", "Los Angeles", "CA", "90048"),
    ("LC-NYC-025", "LabCorp", "LabCorp — Upper East Side",
     "350 E 79th St", "New York", "NY", "10075"),
    ("LC-CHI-036", "LabCorp", "LabCorp — Chicago River North",
     "676 N St Clair St", "Chicago", "IL", "60611"),
    ("LC-DAL-044", "LabCorp", "LabCorp — Dallas Park Cities",
     "8201 Preston Rd", "Dallas", "TX", "75225"),
    ("LC-HOU-052", "LabCorp", "LabCorp — Houston Heights",
     "1631 N Loop W", "Houston", "TX", "77008"),
    ("LC-MIA-057", "LabCorp", "LabCorp — Miami Coral Gables",
     "2333 Ponce de Leon Blvd", "Miami", "FL", "33134"),
    ("LC-ATL-064", "LabCorp", "LabCorp — Buckhead",
     "3193 Howell Mill Rd NW", "Atlanta", "GA", "30327"),
    ("LC-SEA-078", "LabCorp", "LabCorp — Bellevue",
     "1135 116th Ave NE", "Bellevue", "WA", "98004"),
    ("LC-PHX-092", "LabCorp", "LabCorp — Scottsdale",
     "7301 E 2nd St", "Scottsdale", "AZ", "85251"),
]


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


def init_db() -> None:
    """Create this agent's tables and seed lab_locations if empty."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        existing = conn.execute("SELECT COUNT(*) FROM lab_locations").fetchone()[0]
        if existing == 0:
            conn.executemany(
                "INSERT INTO lab_locations (lab_id, provider, name, address, "
                "city, state, zip) VALUES (?, ?, ?, ?, ?, ?, ?)",
                LAB_LOCATION_SEEDS,
            )


# ============================================================
# Helpers
# ============================================================

def _zip_proximity_score(patient_zip: str, lab_zip: str) -> int:
    """Crude proximity: longest matching ZIP prefix (5 = exact, 0 = nothing)."""
    if not patient_zip or not lab_zip:
        return 0
    for i in range(min(len(patient_zip), len(lab_zip)), 0, -1):
        if patient_zip[:i] == lab_zip[:i]:
            return i
    return 0


def _generate_slots(lab_id: str, days_ahead: int = 7) -> list[str]:
    """Deterministic mock slot generator. Same lab + same day -> same slots."""
    rng = random.Random(f"{lab_id}:{datetime.now().date().isoformat()}")
    slots: list[str] = []
    base = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    for offset in range(days_ahead):
        day = base + timedelta(days=offset)
        if day.weekday() == 6:  # closed Sundays
            continue
        possible = list(range(7, 16))
        rng.shuffle(possible)
        for hour in sorted(possible[:rng.randint(3, 5)]):
            minute = rng.choice([0, 15, 30, 45])
            slots.append(day.replace(hour=hour, minute=minute).isoformat(timespec="minutes"))
    return slots


def _patient_row(conn, patient_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM patients WHERE patient_id = ?", (patient_id,)).fetchone()


# ============================================================
# Tools exposed to Claude
# ============================================================

def t_get_patient_info(patient_id: str) -> dict:
    with connect() as conn:
        row = _patient_row(conn, patient_id)
        if not row:
            return {"error": f"No patient found with id {patient_id}. "
                             "Patients are generated by patient_generator.py."}
        p = dict(row)
        return {
            "patient_id": p["patient_id"],
            "name": f"{p['first_name']} {p['last_name']}",
            "date_of_birth": p["date_of_birth"],
            "phone_mobile": p["phone_mobile"],
            "email": p["email"],
            "address": f"{p['street_address']}, {p['city']}, {p['state']} {p['zip']}",
            "city": p["city"], "state": p["state"], "zip": p["zip"],
            "primary_care_physician": p["primary_care_physician"],
            "preferred_language": p["preferred_language"],
        }


def t_get_pending_lab_orders(patient_id: str) -> dict:
    with connect() as conn:
        if not _patient_row(conn, patient_id):
            return {"error": f"No patient found with id {patient_id}"}
        rows = conn.execute(
            "SELECT order_id, panel, fasting_required, ordered_by, ordered_on, status "
            "FROM lab_orders WHERE patient_id = ? AND status = 'pending'",
            (patient_id,)).fetchall()
        orders = [{**dict(r), "fasting_required": bool(r["fasting_required"])} for r in rows]
        return {"patient_id": patient_id, "pending_orders": orders, "count": len(orders)}


def t_search_lab_locations(zip_code: str | None = None,
                            state: str | None = None,
                            provider: str | None = None,
                            patient_id: str | None = None,
                            max_results: int = 5) -> dict:
    """If patient_id is given, the patient's ZIP/state are used automatically."""
    with connect() as conn:
        if patient_id and not (zip_code or state):
            pr = _patient_row(conn, patient_id)
            if not pr:
                return {"error": f"No patient with id {patient_id}"}
            zip_code = pr["zip"]; state = pr["state"]

        sql = "SELECT * FROM lab_locations"
        clauses, params = [], []
        if provider:
            clauses.append("provider = ?"); params.append(provider)
        if state:
            clauses.append("state = ?"); params.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

        # Rank by ZIP proximity (longest matching prefix), then by ID for stability
        if zip_code:
            rows.sort(key=lambda r: (-_zip_proximity_score(zip_code, r["zip"]), r["lab_id"]))
        return {"locations": rows[:max_results], "count": min(len(rows), max_results),
                "ranked_by_zip": zip_code}


def t_get_available_slots(lab_id: str, days_ahead: int = 7) -> dict:
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM lab_locations WHERE lab_id = ?",
                             (lab_id,)).fetchone():
            return {"error": f"Unknown lab_id {lab_id}"}
        taken = {r[0] for r in conn.execute(
            "SELECT datetime FROM appointments WHERE lab_id = ? AND status = 'scheduled'",
            (lab_id,))}
    slots = [s for s in _generate_slots(lab_id, days_ahead) if s not in taken]
    return {"lab_id": lab_id, "available_slots": slots, "count": len(slots)}


def t_book_appointment(patient_id: str, lab_id: str, datetime_iso: str,
                       order_ids: list[str]) -> dict:
    with connect() as conn:
        if not _patient_row(conn, patient_id):
            return {"error": f"No patient with id {patient_id}"}
        lab = conn.execute("SELECT * FROM lab_locations WHERE lab_id = ?",
                           (lab_id,)).fetchone()
        if not lab:
            return {"error": f"Unknown lab_id {lab_id}"}
        clash = conn.execute(
            "SELECT 1 FROM appointments WHERE lab_id = ? AND datetime = ? AND status = 'scheduled'",
            (lab_id, datetime_iso)).fetchone()
        if clash:
            return {"error": "That slot was just taken. Pick another."}

        # Validate the order_ids belong to this patient and gather fasting status
        if not order_ids:
            return {"error": "order_ids must contain at least one order."}
        q = ",".join("?" * len(order_ids))
        matched = conn.execute(
            f"SELECT order_id, panel, fasting_required FROM lab_orders "
            f"WHERE patient_id = ? AND order_id IN ({q})",
            (patient_id, *order_ids)).fetchall()
        if not matched:
            return {"error": "None of the supplied order_ids match this patient's orders."}
        fasting = any(bool(r["fasting_required"]) for r in matched)

        conf = "CONF-" + uuid.uuid4().hex[:6].upper()
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO appointments (confirmation_number, patient_id, lab_id, "
            "provider, datetime, order_ids, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?)",
            (conf, patient_id, lab_id, lab["provider"], datetime_iso,
             json.dumps(order_ids), now))

        return {
            "confirmation_number": conf,
            "patient_id": patient_id,
            "lab_id": lab_id,
            "provider": lab["provider"],
            "lab_name": lab["name"],
            "lab_address": f"{lab['address']}, {lab['city']}, {lab['state']} {lab['zip']}",
            "datetime": datetime_iso,
            "order_ids": order_ids,
            "panels": [r["panel"] for r in matched],
            "fasting_required": fasting,
            "fasting_instructions": (
                "Do not eat or drink anything except water for 8-12 hours before your appointment."
                if fasting else None
            ),
            "status": "scheduled",
        }


def _appt_with_lab(conn, conf: str) -> dict | None:
    row = conn.execute(
        "SELECT a.*, l.name AS lab_name, l.address, l.city, l.state AS lab_state, l.zip AS lab_zip "
        "FROM appointments a LEFT JOIN lab_locations l ON l.lab_id = a.lab_id "
        "WHERE a.confirmation_number = ?", (conf,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("order_ids"):
        try: d["order_ids"] = json.loads(d["order_ids"])
        except Exception: pass
    return d


def t_lookup_appointment(confirmation_number: str) -> dict:
    with connect() as conn:
        d = _appt_with_lab(conn, confirmation_number)
        return d if d else {"error": f"No appointment found with confirmation {confirmation_number}"}


def t_list_appointments_for_patient(patient_id: str) -> dict:
    with connect() as conn:
        if not _patient_row(conn, patient_id):
            return {"error": f"No patient with id {patient_id}"}
        rows = conn.execute(
            "SELECT * FROM appointments WHERE patient_id = ? ORDER BY datetime DESC",
            (patient_id,)).fetchall()
        appts = []
        for r in rows:
            d = dict(r)
            if d.get("order_ids"):
                try: d["order_ids"] = json.loads(d["order_ids"])
                except Exception: pass
            appts.append(d)
        return {"patient_id": patient_id, "appointments": appts, "count": len(appts)}


def t_reschedule_appointment(confirmation_number: str, new_datetime_iso: str) -> dict:
    with connect() as conn:
        appt = conn.execute("SELECT * FROM appointments WHERE confirmation_number = ?",
                             (confirmation_number,)).fetchone()
        if not appt:
            return {"error": f"No appointment found with confirmation {confirmation_number}"}
        if appt["status"] != "scheduled":
            return {"error": f"Appointment is {appt['status']}, cannot reschedule."}
        clash = conn.execute(
            "SELECT 1 FROM appointments WHERE lab_id = ? AND datetime = ? "
            "AND confirmation_number != ? AND status = 'scheduled'",
            (appt["lab_id"], new_datetime_iso, confirmation_number)).fetchone()
        if clash:
            return {"error": "That slot is already taken at this lab."}
        conn.execute("UPDATE appointments SET datetime = ? WHERE confirmation_number = ?",
                     (new_datetime_iso, confirmation_number))
        return {"confirmation_number": confirmation_number,
                "previous_datetime": appt["datetime"],
                "new_datetime": new_datetime_iso, "status": "rescheduled"}


def t_cancel_appointment(confirmation_number: str, reason: str | None = None) -> dict:
    with connect() as conn:
        appt = conn.execute("SELECT * FROM appointments WHERE confirmation_number = ?",
                             (confirmation_number,)).fetchone()
        if not appt:
            return {"error": f"No appointment found with confirmation {confirmation_number}"}
        conn.execute(
            "UPDATE appointments SET status = 'cancelled', cancel_reason = ? "
            "WHERE confirmation_number = ?", (reason, confirmation_number))
        return {"confirmation_number": confirmation_number, "status": "cancelled",
                "reason": reason}


def t_send_reminder(confirmation_number: str, channel: str = "sms") -> dict:
    if channel not in {"sms", "email"}:
        return {"error": "channel must be 'sms' or 'email'"}
    with connect() as conn:
        d = _appt_with_lab(conn, confirmation_number)
        if not d:
            return {"error": f"No appointment found with confirmation {confirmation_number}"}
        p = _patient_row(conn, d["patient_id"])
        if not p:
            return {"error": "Patient record missing for this appointment."}
        destination = p["phone_mobile"] if channel == "sms" else p["email"]
        return {
            "confirmation_number": confirmation_number,
            "channel": channel,
            "sent_to": destination,
            "patient_name": f"{p['first_name']} {p['last_name']}",
            "status": "queued",
            "message_preview": (
                f"Reminder: lab appointment on {d['datetime']} at {d['lab_name']}, "
                f"{d['address']}, {d['city']}."
            ),
        }


# ============================================================
# Lab location management
# ============================================================

# Anchor cities used when generate-labs picks somewhere to drop a fake lab.
# Each entry is (city, state, ZIP prefix used as a realistic seed).
_FAKE_LAB_CITIES = [
    ("San Francisco", "CA", "941"), ("Los Angeles", "CA", "900"),
    ("San Diego", "CA", "921"), ("Sacramento", "CA", "958"),
    ("Phoenix", "AZ", "850"), ("Las Vegas", "NV", "891"),
    ("Denver", "CO", "802"), ("Seattle", "WA", "981"),
    ("Portland", "OR", "972"), ("Austin", "TX", "787"),
    ("Dallas", "TX", "752"), ("Houston", "TX", "770"),
    ("Chicago", "IL", "606"), ("Minneapolis", "MN", "554"),
    ("Atlanta", "GA", "303"), ("Miami", "FL", "331"),
    ("Orlando", "FL", "328"), ("Boston", "MA", "021"),
    ("New York", "NY", "100"), ("Philadelphia", "PA", "191"),
    ("Washington", "DC", "200"), ("Charlotte", "NC", "282"),
    ("Nashville", "TN", "372"), ("Detroit", "MI", "482"),
]

_LAB_NAME_SUFFIXES = [
    "Downtown", "Westside", "Eastside", "North", "South", "Midtown",
    "Medical Center", "Heights", "Plaza", "Crossing", "Square",
    "Park", "Hills", "Village", "Commons",
]


def t_add_lab(lab_id: str, provider: str, name: str, address: str,
              city: str, state: str, zip: str) -> dict:
    """Insert a single, fully-specified lab location."""
    if provider not in ("Quest Diagnostics", "LabCorp"):
        return {"error": "provider must be 'Quest Diagnostics' or 'LabCorp'"}
    with connect() as conn:
        if conn.execute("SELECT 1 FROM lab_locations WHERE lab_id = ?",
                         (lab_id,)).fetchone():
            return {"error": f"lab_id {lab_id} already exists"}
        conn.execute(
            "INSERT INTO lab_locations (lab_id, provider, name, address, "
            "city, state, zip) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lab_id, provider, name, address, city, state, zip))
    return {"status": "added", "lab_id": lab_id, "provider": provider,
            "name": name, "address": address, "city": city,
            "state": state, "zip": zip}


def t_list_labs(provider: str | None = None, state: str | None = None,
                limit: int = 50) -> dict:
    """List labs, optionally filtered by provider and/or state."""
    sql = "SELECT * FROM lab_locations"
    clauses, params = [], []
    if provider:
        clauses.append("provider = ?"); params.append(provider)
    if state:
        clauses.append("state = ?"); params.append(state)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY state, city, lab_id LIMIT ?"; params.append(int(limit))
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return {"count": len(rows), "locations": rows}


def t_generate_labs(count: int, provider: str | None = None,
                    state: str | None = None, seed: int | None = None) -> dict:
    """Bulk-generate plausible fake lab locations using Faker."""
    if count < 1 or count > 500:
        return {"error": "count must be between 1 and 500"}
    try:
        from faker import Faker
    except ImportError:
        return {"error": "generate-labs requires Faker. Install with: pip install faker"}

    fake = Faker("en_US")
    if seed is not None:
        Faker.seed(seed); rng = random.Random(seed)
    else:
        rng = random.Random()

    # Pool of cities respecting any state filter.
    cities = _FAKE_LAB_CITIES
    if state:
        cities = [c for c in cities if c[1] == state.upper()]
        if not cities:
            # State requested but no anchor city for it — fall back to a synthetic one.
            cities = [(fake.city(), state.upper(), fake.zipcode()[:3])]

    providers = [provider] if provider else None

    created: list[dict] = []
    with connect() as conn:
        used_ids = {r[0] for r in conn.execute("SELECT lab_id FROM lab_locations")}
        for _ in range(count):
            prov = (providers[0] if providers else rng.choice(["Quest Diagnostics", "LabCorp"]))
            city, st, zip_prefix = rng.choice(cities)
            zip_full = f"{zip_prefix}{rng.randint(0, 99):02d}"
            prefix = "QD" if prov == "Quest Diagnostics" else "LC"
            city_code = "".join(c for c in city if c.isalpha()).upper()[:3]
            # Allocate a unique lab_id
            for _attempt in range(200):
                cand = f"{prefix}-{city_code}-{rng.randint(100, 999)}"
                if cand not in used_ids:
                    used_ids.add(cand); lab_id = cand; break
            else:
                return {"error": "Could not allocate a unique lab_id; try a smaller count."}

            short_city = city.split(",")[0]
            name = f"{prov} — {short_city} {rng.choice(_LAB_NAME_SUFFIXES)}"
            address = fake.street_address()
            conn.execute(
                "INSERT INTO lab_locations (lab_id, provider, name, address, "
                "city, state, zip) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lab_id, prov, name, address, city, st, zip_full))
            created.append({"lab_id": lab_id, "provider": prov, "name": name,
                            "city": city, "state": st, "zip": zip_full})
    return {"generated": len(created), "locations": created}


def t_delete_lab(lab_id: str, confirm: bool = False) -> dict:
    """Remove a single lab. Blocked if it has any scheduled appointments
    unless confirm=True, in which case those appointments are cancelled."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM lab_locations WHERE lab_id = ?",
                            (lab_id,)).fetchone()
        if not row:
            return {"error": f"No lab with lab_id {lab_id}"}
        appt_count = conn.execute(
            "SELECT COUNT(*) FROM appointments WHERE lab_id = ? AND status = 'scheduled'",
            (lab_id,)).fetchone()[0]
        if appt_count > 0 and not confirm:
            return {"error": f"Lab has {appt_count} scheduled appointments. "
                             "Pass --confirm to delete and auto-cancel them."}
        if appt_count > 0:
            conn.execute(
                "UPDATE appointments SET status = 'cancelled', "
                "cancel_reason = 'Lab location removed' "
                "WHERE lab_id = ? AND status = 'scheduled'", (lab_id,))
        conn.execute("DELETE FROM lab_locations WHERE lab_id = ?", (lab_id,))
    return {"status": "deleted", "lab_id": lab_id,
            "appointments_cancelled": appt_count}


TOOLS = {
    "get_patient_info": t_get_patient_info,
    "get_pending_lab_orders": t_get_pending_lab_orders,
    "search_lab_locations": t_search_lab_locations,
    "get_available_slots": t_get_available_slots,
    "book_appointment": t_book_appointment,
    "lookup_appointment": t_lookup_appointment,
    "list_appointments_for_patient": t_list_appointments_for_patient,
    "reschedule_appointment": t_reschedule_appointment,
    "cancel_appointment": t_cancel_appointment,
    "send_reminder": t_send_reminder,
    "list_labs": t_list_labs,
    "add_lab": t_add_lab,
    "generate_labs": t_generate_labs,
    "delete_lab": t_delete_lab,
}

TOOL_SCHEMAS = [
    {"name": "get_patient_info",
     "description": "Look up a patient's identity, contact info, and address by patient_id (format P-####).",
     "input_schema": {"type": "object",
                      "properties": {"patient_id": {"type": "string"}},
                      "required": ["patient_id"]}},
    {"name": "get_pending_lab_orders",
     "description": "List unfulfilled lab orders placed by the patient's care team. Always check this before booking so you book the right panel.",
     "input_schema": {"type": "object",
                      "properties": {"patient_id": {"type": "string"}},
                      "required": ["patient_id"]}},
    {"name": "search_lab_locations",
     "description": ("Find Quest Diagnostics or LabCorp draw sites. If you pass "
                     "patient_id, the patient's ZIP and state are used automatically "
                     "to rank by proximity. Otherwise pass zip_code and/or state."),
     "input_schema": {"type": "object",
                      "properties": {
                          "patient_id": {"type": "string"},
                          "zip_code":   {"type": "string"},
                          "state":      {"type": "string"},
                          "provider":   {"type": "string", "enum": ["Quest Diagnostics", "LabCorp"]},
                          "max_results": {"type": "integer", "default": 5}}}},
    {"name": "get_available_slots",
     "description": "Open ISO 8601 datetimes at a specific lab over the next N days.",
     "input_schema": {"type": "object",
                      "properties": {"lab_id":     {"type": "string"},
                                     "days_ahead": {"type": "integer", "default": 7}},
                      "required": ["lab_id"]}},
    {"name": "book_appointment",
     "description": ("Book an appointment. Returns confirmation number, lab address, "
                     "and fasting instructions if any of the orders require fasting."),
     "input_schema": {"type": "object",
                      "properties": {
                          "patient_id":   {"type": "string"},
                          "lab_id":       {"type": "string"},
                          "datetime_iso": {"type": "string", "description": "e.g. 2026-05-14T09:30"},
                          "order_ids":    {"type": "array", "items": {"type": "string"}}},
                      "required": ["patient_id", "lab_id", "datetime_iso", "order_ids"]}},
    {"name": "lookup_appointment",
     "description": "Look up an existing appointment by confirmation number.",
     "input_schema": {"type": "object",
                      "properties": {"confirmation_number": {"type": "string"}},
                      "required": ["confirmation_number"]}},
    {"name": "list_appointments_for_patient",
     "description": "Every appointment (any status) for a patient.",
     "input_schema": {"type": "object",
                      "properties": {"patient_id": {"type": "string"}},
                      "required": ["patient_id"]}},
    {"name": "reschedule_appointment",
     "description": "Move a scheduled appointment to a new datetime at the same lab.",
     "input_schema": {"type": "object",
                      "properties": {"confirmation_number": {"type": "string"},
                                     "new_datetime_iso":    {"type": "string"}},
                      "required": ["confirmation_number", "new_datetime_iso"]}},
    {"name": "cancel_appointment",
     "description": "Cancel an existing appointment.",
     "input_schema": {"type": "object",
                      "properties": {"confirmation_number": {"type": "string"},
                                     "reason": {"type": "string"}},
                      "required": ["confirmation_number"]}},
    {"name": "send_reminder",
     "description": "Send an SMS or email reminder for an appointment.",
     "input_schema": {"type": "object",
                      "properties": {"confirmation_number": {"type": "string"},
                                     "channel": {"type": "string", "enum": ["sms", "email"], "default": "sms"}},
                      "required": ["confirmation_number"]}},
    {"name": "list_labs",
     "description": "List lab locations currently in the system, optionally filtered.",
     "input_schema": {"type": "object",
                      "properties": {"provider": {"type": "string", "enum": ["Quest Diagnostics", "LabCorp"]},
                                     "state":    {"type": "string"},
                                     "limit":    {"type": "integer", "default": 50}}}},
    {"name": "add_lab",
     "description": "Add a single specific lab location to the database.",
     "input_schema": {"type": "object",
                      "properties": {"lab_id":   {"type": "string"},
                                     "provider": {"type": "string", "enum": ["Quest Diagnostics", "LabCorp"]},
                                     "name":     {"type": "string"},
                                     "address":  {"type": "string"},
                                     "city":     {"type": "string"},
                                     "state":    {"type": "string"},
                                     "zip":      {"type": "string"}},
                      "required": ["lab_id", "provider", "name", "address", "city", "state", "zip"]}},
    {"name": "generate_labs",
     "description": "Bulk-generate plausible fake Quest/LabCorp lab locations.",
     "input_schema": {"type": "object",
                      "properties": {"count":    {"type": "integer", "description": "1-500"},
                                     "provider": {"type": "string", "enum": ["Quest Diagnostics", "LabCorp"]},
                                     "state":    {"type": "string"},
                                     "seed":     {"type": "integer"}},
                      "required": ["count"]}},
    {"name": "delete_lab",
     "description": "Remove a lab location. Blocked by scheduled appointments unless confirm=true (which cancels them).",
     "input_schema": {"type": "object",
                      "properties": {"lab_id":  {"type": "string"},
                                     "confirm": {"type": "boolean"}},
                      "required": ["lab_id"]}},
]


# ============================================================
# Agent loop
# ============================================================

MODEL = "claude-haiku-4-5"
SYSTEM_PROMPT = """You are the Lab Appointment Scheduling Agent for Twin Health.
You help patients book, confirm, reschedule, and cancel diagnostic lab
appointments at Quest Diagnostics and LabCorp facilities, and keep them on
track with the lab orders their care team has placed.

How you operate:
- Greet the patient warmly. If you don't have a patient_id yet, ask for one.
  (IDs look like P-1234.)
- Before suggesting any booking, always check pending lab orders so you book
  the right panel.
- Use search_lab_locations with patient_id to automatically find labs near
  the patient's home — the tool will use their ZIP and state on file.
- Use tools rather than guessing. Never invent confirmation numbers,
  addresses, or available slots.
- When offering slot options, summarize clearly (date/time, lab, address) and
  let the patient choose.
- After any booking/reschedule/cancel, confirm in plain language and offer a
  reminder. If fasting is required, mention the fasting instructions.
- Keep responses concise and friendly. Avoid medical advice beyond logistics."""


def run_turn(client, messages):
    for _ in range(10):
        resp = client.messages.create(model=MODEL, max_tokens=2048,
                                      system=SYSTEM_PROMPT, tools=TOOL_SCHEMAS,
                                      messages=messages)
        for block in resp.content:
            if block.type == "text" and block.text.strip():
                print(f"\n{textwrap.fill(block.text.strip(), 80, initial_indent='Agent: ', subsequent_indent='       ')}")
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return messages
        results = []
        for block in resp.content:
            if block.type != "tool_use": continue
            preview = json.dumps(block.input, separators=(", ", "="))
            if len(preview) > 200: preview = preview[:197] + "..."
            print(f"  ↳ tool: {block.name}({preview})")
            try:
                out = TOOLS[block.name](**block.input)
                out_str = json.dumps(out, indent=2, default=str); err = False
            except Exception as exc:
                out_str = f"Tool error: {exc}"; err = True
            display = out_str if len(out_str) <= 1200 else out_str[:1200] + "\n... (truncated for display)"
            print(("  ↳ error:\n" if err else "  ↳ result:\n") + textwrap.indent(display, "      "))
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": out_str, "is_error": err})
        messages.append({"role": "user", "content": results})
    print("\n[Reached max tool-loop iterations.]")
    return messages


def _print_result(result) -> int:
    """Print a tool result as pretty JSON. Exit code 1 if it carries an error."""
    out = json.dumps(result, indent=2, default=str)
    if isinstance(result, dict) and "error" in result:
        print(out, file=sys.stderr)
        return 1
    print(out)
    return 0


def run_chat() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        print("Create a .env file next to this script with:", file=sys.stderr)
        print("    ANTHROPIC_API_KEY=sk-ant-...", file=sys.stderr)
        print("Or export it in your shell.", file=sys.stderr)
        return 1
    if not os.path.exists(DB_PATH):
        print(f"No DB found at {DB_PATH}. Run patient_generator.py first to populate "
              "patient data, or this agent will only be able to book against patients "
              "you create separately.")
    init_db()
    client = Anthropic()
    messages: list = []
    print("\n" + "=" * 64)
    print("  Lab Appointment Scheduling Agent  ·  Twin Health")
    print("  Quest Diagnostics & LabCorp integration")
    print(f"  DB: {DB_PATH}")
    print("=" * 64)
    print("  Commands: /quit, /reset")
    print("  Try: \"Hi, I'm patient P-1234 and I need to book my labs.\"\n")
    while True:
        try:
            user = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); break
        if not user: continue
        if user.lower() in {"/quit", "/exit", "quit", "exit"}:
            print("Goodbye."); break
        if user.lower() == "/reset":
            messages = []; print("\n[Conversation reset.]"); continue
        messages.append({"role": "user", "content": user})
        try:
            messages = run_turn(client, messages)
        except Exception as exc:
            print(f"\n[API error: {exc}]"); messages.pop()
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="lab_scheduling_agent",
        description="Schedule, look up, reschedule, and cancel lab appointments. "
                    "With no subcommand, launches the interactive agent.",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("chat", help="Run the interactive agent (default)")
    sub.add_parser("init", help="Create lab_locations + appointments tables and seed labs")

    p_pat = sub.add_parser("patient", help="Look up patient info")
    p_pat.add_argument("patient_id")

    p_ord = sub.add_parser("orders", help="List a patient's pending lab orders")
    p_ord.add_argument("patient_id")

    p_labs = sub.add_parser("labs", help="Search Quest/LabCorp draw sites")
    p_labs.add_argument("--patient", dest="patient_id",
                        help="Use this patient's ZIP/state automatically")
    p_labs.add_argument("--zip", dest="zip_code")
    p_labs.add_argument("--state")
    p_labs.add_argument("--provider", choices=["Quest Diagnostics", "LabCorp"])
    p_labs.add_argument("--max", dest="max_results", type=int, default=5)

    p_slots = sub.add_parser("slots", help="Open slots at a lab")
    p_slots.add_argument("lab_id")
    p_slots.add_argument("--days", dest="days_ahead", type=int, default=7)

    p_book = sub.add_parser("book", help="Book an appointment")
    p_book.add_argument("--patient", dest="patient_id", required=True)
    p_book.add_argument("--lab",     dest="lab_id",     required=True)
    p_book.add_argument("--datetime", dest="datetime_iso", required=True,
                        help="ISO 8601, e.g. 2026-05-16T09:30")
    p_book.add_argument("--orders", dest="order_ids", nargs="+", required=True,
                        metavar="ORD",
                        help="One or more order_ids belonging to the patient")

    p_look = sub.add_parser("lookup", help="Look up an appointment by confirmation #")
    p_look.add_argument("confirmation_number")

    p_list = sub.add_parser("list", help="List a patient's appointments")
    p_list.add_argument("patient_id")

    p_resch = sub.add_parser("reschedule", help="Move an appointment to a new time")
    p_resch.add_argument("confirmation_number")
    p_resch.add_argument("--datetime", dest="new_datetime_iso", required=True)

    p_cancel = sub.add_parser("cancel", help="Cancel an appointment")
    p_cancel.add_argument("confirmation_number")
    p_cancel.add_argument("--reason")

    p_rem = sub.add_parser("remind", help="Send an appointment reminder")
    p_rem.add_argument("confirmation_number")
    p_rem.add_argument("--channel", choices=["sms", "email"], default="sms")

    p_list_labs = sub.add_parser("list-labs", help="List lab locations in the DB")
    p_list_labs.add_argument("--provider", choices=["Quest Diagnostics", "LabCorp"])
    p_list_labs.add_argument("--state")
    p_list_labs.add_argument("--limit", type=int, default=50)

    p_add_lab = sub.add_parser("add-lab", help="Manually add one lab location")
    p_add_lab.add_argument("--lab-id",   dest="lab_id",   required=True,
                           help="Unique ID, e.g. QD-SF-099")
    p_add_lab.add_argument("--provider", required=True,
                           choices=["Quest Diagnostics", "LabCorp"])
    p_add_lab.add_argument("--name",     required=True,
                           help='e.g. "Quest Diagnostics — Mission St"')
    p_add_lab.add_argument("--address",  required=True)
    p_add_lab.add_argument("--city",     required=True)
    p_add_lab.add_argument("--state",    required=True, help="Two-letter code")
    p_add_lab.add_argument("--zip",      required=True)

    p_gen_labs = sub.add_parser("generate-labs",
                                 help="Bulk-generate fake Quest/LabCorp labs (needs faker)")
    p_gen_labs.add_argument("--count", type=int, required=True, help="1-500")
    p_gen_labs.add_argument("--provider", choices=["Quest Diagnostics", "LabCorp"],
                            help="Restrict to one provider")
    p_gen_labs.add_argument("--state", help="Two-letter state code")
    p_gen_labs.add_argument("--seed", type=int, help="For reproducible output")

    p_del_lab = sub.add_parser("delete-lab", help="Remove a lab location")
    p_del_lab.add_argument("lab_id")
    p_del_lab.add_argument("--confirm", action="store_true",
                           help="Required if the lab has scheduled appointments")

    args = parser.parse_args()

    # All direct-CLI commands need the tables present; chat mode handles its own init.
    if args.cmd not in (None, "chat"):
        init_db()

    if args.cmd is None or args.cmd == "chat":
        return run_chat()
    if args.cmd == "init":
        return _print_result({"status": "initialized", "db_path": DB_PATH})
    if args.cmd == "patient":
        return _print_result(t_get_patient_info(args.patient_id))
    if args.cmd == "orders":
        return _print_result(t_get_pending_lab_orders(args.patient_id))
    if args.cmd == "labs":
        return _print_result(t_search_lab_locations(
            zip_code=args.zip_code, state=args.state, provider=args.provider,
            patient_id=args.patient_id, max_results=args.max_results))
    if args.cmd == "slots":
        return _print_result(t_get_available_slots(args.lab_id, args.days_ahead))
    if args.cmd == "book":
        return _print_result(t_book_appointment(
            patient_id=args.patient_id, lab_id=args.lab_id,
            datetime_iso=args.datetime_iso, order_ids=args.order_ids))
    if args.cmd == "lookup":
        return _print_result(t_lookup_appointment(args.confirmation_number))
    if args.cmd == "list":
        return _print_result(t_list_appointments_for_patient(args.patient_id))
    if args.cmd == "reschedule":
        return _print_result(t_reschedule_appointment(
            args.confirmation_number, args.new_datetime_iso))
    if args.cmd == "cancel":
        return _print_result(t_cancel_appointment(args.confirmation_number, args.reason))
    if args.cmd == "remind":
        return _print_result(t_send_reminder(args.confirmation_number, args.channel))
    if args.cmd == "list-labs":
        return _print_result(t_list_labs(provider=args.provider,
                                          state=args.state, limit=args.limit))
    if args.cmd == "add-lab":
        return _print_result(t_add_lab(
            lab_id=args.lab_id, provider=args.provider, name=args.name,
            address=args.address, city=args.city, state=args.state, zip=args.zip))
    if args.cmd == "generate-labs":
        return _print_result(t_generate_labs(
            count=args.count, provider=args.provider,
            state=args.state, seed=args.seed))
    if args.cmd == "delete-lab":
        return _print_result(t_delete_lab(args.lab_id, confirm=args.confirm))
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())