"""
Healthcare Data Validation Reporter
------------------------------------
Validates the patient records in the shared SQLite DB and, separately,
uses an Anthropic-API agent to turn the validation results into a
human-friendly email report.

Key design point (per the spec): VALIDATION IS NOT AI. The checks below
are deterministic Python rules. The AI is only used by the `report` /
`chat` commands to render structured findings into prose.

    pip install anthropic python-dotenv
    # put ANTHROPIC_API_KEY in .env
    python validation_reporter.py validate
    python validation_reporter.py report --recipient "Dr. Patel"
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
import textwrap
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from anthropic import Anthropic

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # optional


# ============================================================
# DB connection (read-only by default; seed-issues opens write)
# ============================================================

DB_PATH = os.environ.get(
    "PATIENTS_DB",
    os.path.join(os.environ.get("SANDBOX_DATA_DIR", ""), "hospital", "patients.db")
)


@contextmanager
def connect(readonly: bool = True):
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"No DB found at {DB_PATH}. Run patient_generator.py first to "
            "populate the shared database.")
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        if not readonly:
            conn.rollback()
        raise
    finally:
        conn.close()


# ============================================================
# Validation rules (deterministic — NO AI here)
# ============================================================

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?\d{1,3}[-.\s]?\(?\d{1,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}$")
ZIP_RE   = re.compile(r"^\d{5}(-\d{4})?$")
ICD10_RE = re.compile(r"^[A-Z]\d{2}(\.\d{1,4})?$")
STATE_RE = re.compile(r"^[A-Z]{2}$")

# Medication -> required condition keyword. If a patient is on this med and
# none of their condition descriptions contain the keyword (case-insensitive),
# that's a clinical inconsistency.
MED_REQUIRES_CONDITION = {
    "metformin":      ["diabet", "prediabet"],
    "glipizide":      ["diabet"],
    "semaglutide":    ["diabet", "obes"],
    "empagliflozin":  ["diabet"],
    "tirzepatide":    ["diabet", "obes"],
    "lisinopril":     ["hypertens"],
    "amlodipine":     ["hypertens"],
    "losartan":       ["hypertens"],
    "hydrochlorothiazide": ["hypertens"],
    "levothyroxine":  ["hypothyroid"],
    "atorvastatin":   ["lipid", "hyperlipid"],
    "rosuvastatin":   ["lipid", "hyperlipid"],
    "simvastatin":    ["lipid", "hyperlipid"],
    "albuterol":      ["asthma"],
    "fluticasone":    ["asthma"],
    "montelukast":    ["asthma"],
    "omeprazole":     ["gerd", "reflux"],
    "pantoprazole":   ["gerd", "reflux"],
    "sertraline":     ["depress", "anx"],
    "fluoxetine":     ["depress"],
    "escitalopram":   ["depress", "anx"],
    "bupropion":      ["depress"],
    "buspirone":      ["anx"],
}


def _finding(severity: str, code: str, message: str,
             patient_id: str | None = None, details: dict | None = None) -> dict:
    f = {"severity": severity, "code": code, "message": message,
         "patient_id": patient_id}
    if details:
        f["details"] = details
    return f


def _check_required_fields(conn) -> list[dict]:
    out: list[dict] = []
    rows = conn.execute(
        "SELECT patient_id, first_name, last_name, date_of_birth, sex, "
        "       phone_mobile, email, street_address, city, state, zip "
        "FROM patients").fetchall()
    for r in rows:
        pid = r["patient_id"]
        if not r["first_name"] or not r["last_name"]:
            out.append(_finding("CRITICAL", "MISSING_NAME",
                                "Patient missing first or last name", pid))
        if not r["date_of_birth"]:
            out.append(_finding("CRITICAL", "MISSING_DOB",
                                "Patient has no date of birth on file", pid))
        if not r["sex"]:
            out.append(_finding("WARNING", "MISSING_SEX",
                                "Patient has no sex on file", pid))
        if not r["phone_mobile"] and not r["email"]:
            out.append(_finding("CRITICAL", "NO_CONTACT",
                                "Patient has no phone or email", pid))
        if not r["street_address"] or not r["city"] or not r["zip"]:
            out.append(_finding("WARNING", "INCOMPLETE_ADDRESS",
                                "Address missing street, city, or ZIP", pid))
    return out


def _check_formats(conn) -> list[dict]:
    out: list[dict] = []
    rows = conn.execute(
        "SELECT patient_id, email, phone_mobile, zip, state, date_of_birth "
        "FROM patients").fetchall()
    today = date.today()
    for r in rows:
        pid = r["patient_id"]
        if r["email"] and not EMAIL_RE.match(r["email"]):
            out.append(_finding("CRITICAL", "INVALID_EMAIL",
                                f"Malformed email address: {r['email']!r}", pid))
        if r["phone_mobile"] and not PHONE_RE.match(r["phone_mobile"]):
            out.append(_finding("CRITICAL", "INVALID_PHONE",
                                f"Malformed phone number: {r['phone_mobile']!r}", pid))
        if r["zip"] and not ZIP_RE.match(r["zip"]):
            out.append(_finding("CRITICAL", "INVALID_ZIP",
                                f"Malformed ZIP code: {r['zip']!r}", pid))
        if r["state"] and not STATE_RE.match(r["state"]):
            out.append(_finding("WARNING", "INVALID_STATE",
                                f"State should be a 2-letter code: {r['state']!r}", pid))
        if r["date_of_birth"]:
            try:
                dob = date.fromisoformat(r["date_of_birth"])
                if dob > today:
                    out.append(_finding("CRITICAL", "DOB_IN_FUTURE",
                                        f"DOB is in the future: {dob}", pid))
                elif (today.year - dob.year) > 120:
                    out.append(_finding("WARNING", "DOB_TOO_OLD",
                                        f"DOB implies age over 120: {dob}", pid))
            except ValueError:
                out.append(_finding("CRITICAL", "INVALID_DOB",
                                    f"Unparseable DOB: {r['date_of_birth']!r}", pid))
    return out


def _check_duplicates(conn) -> list[dict]:
    out: list[dict] = []
    dupe_emails = conn.execute(
        "SELECT email, COUNT(*) AS n, GROUP_CONCAT(patient_id) AS pids "
        "FROM patients WHERE email IS NOT NULL AND email != '' "
        "GROUP BY LOWER(email) HAVING n > 1").fetchall()
    for r in dupe_emails:
        out.append(_finding("WARNING", "DUPLICATE_EMAIL",
                            f"Email {r['email']!r} appears on {r['n']} patients",
                            details={"patient_ids": r["pids"].split(","),
                                     "email": r["email"]}))
    dupe_phones = conn.execute(
        "SELECT phone_mobile, COUNT(*) AS n, GROUP_CONCAT(patient_id) AS pids "
        "FROM patients WHERE phone_mobile IS NOT NULL AND phone_mobile != '' "
        "GROUP BY phone_mobile HAVING n > 1").fetchall()
    for r in dupe_phones:
        out.append(_finding("WARNING", "DUPLICATE_PHONE",
                            f"Phone {r['phone_mobile']!r} appears on {r['n']} patients",
                            details={"patient_ids": r["pids"].split(","),
                                     "phone": r["phone_mobile"]}))
    return out


def _check_clinical_consistency(conn) -> list[dict]:
    out: list[dict] = []
    pids = [r[0] for r in conn.execute("SELECT patient_id FROM patients")]
    for pid in pids:
        meds = conn.execute(
            "SELECT name, prescribed_for FROM medications "
            "WHERE patient_id = ? AND status = 'active'", (pid,)).fetchall()
        if not meds:
            continue
        condition_text = " | ".join(
            (r[0] or "").lower()
            for r in conn.execute(
                "SELECT description FROM conditions WHERE patient_id = ? "
                "AND status = 'active'", (pid,))
        )
        for m in meds:
            med_lower = (m["name"] or "").lower()
            # Look up the first matching trigger word
            for trigger, required in MED_REQUIRES_CONDITION.items():
                if trigger in med_lower:
                    if not any(req in condition_text for req in required):
                        out.append(_finding(
                            "WARNING", "MED_WITHOUT_CONDITION",
                            f"On {m['name']} but no matching condition "
                            f"(expected one of: {', '.join(required)})",
                            pid,
                            details={"medication": m["name"],
                                     "prescribed_for": m["prescribed_for"]}))
                    break
    return out


def _check_bmi_consistency(conn) -> list[dict]:
    out: list[dict] = []
    rows = conn.execute(
        "SELECT patient_id, height_cm, weight_kg, bmi FROM patients "
        "WHERE height_cm IS NOT NULL AND weight_kg IS NOT NULL "
        "AND bmi IS NOT NULL").fetchall()
    for r in rows:
        if r["height_cm"] <= 0:
            continue
        computed = r["weight_kg"] / ((r["height_cm"] / 100) ** 2)
        if abs(computed - r["bmi"]) > 1.0:
            out.append(_finding(
                "WARNING", "BMI_INCONSISTENT",
                f"Stored BMI {r['bmi']} differs from height/weight-derived "
                f"value {round(computed, 1)} by more than 1.0",
                r["patient_id"],
                details={"stored_bmi": r["bmi"],
                         "computed_bmi": round(computed, 1),
                         "height_cm": r["height_cm"],
                         "weight_kg": r["weight_kg"]}))
    return out


def _check_temporal(conn) -> list[dict]:
    out: list[dict] = []
    today = date.today()
    # Stale pending lab orders (>90 days)
    cutoff = (today - timedelta(days=90)).isoformat()
    rows = conn.execute(
        "SELECT order_id, patient_id, panel, ordered_on FROM lab_orders "
        "WHERE status = 'pending' AND ordered_on < ?", (cutoff,)).fetchall()
    for r in rows:
        days = (today - date.fromisoformat(r["ordered_on"])).days
        out.append(_finding(
            "WARNING", "STALE_LAB_ORDER",
            f"Pending lab order {r['order_id']} is {days} days old",
            r["patient_id"],
            details={"order_id": r["order_id"], "panel": r["panel"],
                     "ordered_on": r["ordered_on"], "days_old": days}))
    # Diagnoses before DOB or in the future
    rows = conn.execute(
        "SELECT c.patient_id, c.icd10_code, c.diagnosed_date, p.date_of_birth "
        "FROM conditions c JOIN patients p ON p.patient_id = c.patient_id "
        "WHERE c.diagnosed_date IS NOT NULL").fetchall()
    for r in rows:
        try:
            dx = date.fromisoformat(r["diagnosed_date"])
        except (ValueError, TypeError):
            continue
        if dx > today:
            out.append(_finding(
                "WARNING", "DIAGNOSIS_IN_FUTURE",
                f"Diagnosis date {dx} for {r['icd10_code']} is in the future",
                r["patient_id"]))
        if r["date_of_birth"]:
            try:
                dob = date.fromisoformat(r["date_of_birth"])
                if dx < dob:
                    out.append(_finding(
                        "WARNING", "DIAGNOSIS_BEFORE_DOB",
                        f"Diagnosis date {dx} for {r['icd10_code']} predates DOB",
                        r["patient_id"]))
            except ValueError:
                pass
    return out


def _check_completeness(conn) -> list[dict]:
    out: list[dict] = []
    rows = conn.execute(
        "SELECT patient_id, primary_care_physician, emergency_contact_name "
        "FROM patients").fetchall()
    for r in rows:
        pid = r["patient_id"]
        if not r["primary_care_physician"]:
            out.append(_finding("INFO", "MISSING_PCP",
                                "No primary care physician on file", pid))
        if not r["emergency_contact_name"]:
            out.append(_finding("INFO", "MISSING_EMERGENCY_CONTACT",
                                "No emergency contact on file", pid))

    pid_with_ins = {r[0] for r in conn.execute(
        "SELECT DISTINCT patient_id FROM insurance")}
    pid_with_imm = {r[0] for r in conn.execute(
        "SELECT DISTINCT patient_id FROM immunizations")}
    for pid in (r[0] for r in conn.execute("SELECT patient_id FROM patients")):
        if pid not in pid_with_ins:
            out.append(_finding("INFO", "NO_INSURANCE",
                                "No insurance records on file", pid))
        if pid not in pid_with_imm:
            out.append(_finding("INFO", "NO_IMMUNIZATIONS",
                                "No immunizations on file", pid))
    return out


def _check_referential_integrity(conn) -> list[dict]:
    out: list[dict] = []
    valid_pids = {r[0] for r in conn.execute("SELECT patient_id FROM patients")}
    # Orphan lab_orders
    rows = conn.execute(
        "SELECT order_id, patient_id FROM lab_orders").fetchall()
    for r in rows:
        if r["patient_id"] not in valid_pids:
            out.append(_finding(
                "CRITICAL", "ORPHAN_LAB_ORDER",
                f"Lab order {r['order_id']} references missing patient "
                f"{r['patient_id']}",
                details={"order_id": r["order_id"]}))
    # ICD-10 format check
    rows = conn.execute(
        "SELECT patient_id, icd10_code FROM conditions WHERE icd10_code IS NOT NULL"
    ).fetchall()
    for r in rows:
        if not ICD10_RE.match(r["icd10_code"]):
            out.append(_finding(
                "WARNING", "INVALID_ICD10",
                f"ICD-10 code {r['icd10_code']!r} doesn't match the expected format",
                r["patient_id"]))
    return out


# Ordered for stable report output. Severity sort happens later.
ALL_CHECKS = [
    ("required_fields",       _check_required_fields),
    ("formats",               _check_formats),
    ("duplicates",            _check_duplicates),
    ("clinical_consistency",  _check_clinical_consistency),
    ("bmi_consistency",       _check_bmi_consistency),
    ("temporal",              _check_temporal),
    ("completeness",          _check_completeness),
    ("referential_integrity", _check_referential_integrity),
]

SEVERITY_ORDER = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}


def run_all_checks(patient_id: str | None = None) -> list[dict]:
    """Run every validation rule. If patient_id is given, filter results to it."""
    with connect(readonly=True) as conn:
        findings: list[dict] = []
        for _, fn in ALL_CHECKS:
            findings.extend(fn(conn))
    if patient_id:
        findings = [f for f in findings if f.get("patient_id") == patient_id]
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 99),
                                  f["code"], f.get("patient_id") or ""))
    return findings


def summarize(findings: list[dict]) -> dict:
    by_severity: dict[str, int] = {}
    by_code: dict[str, int] = {}
    affected: set[str] = set()
    for f in findings:
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
        by_code[f["code"]] = by_code.get(f["code"], 0) + 1
        if f.get("patient_id"):
            affected.add(f["patient_id"])
    with connect(readonly=True) as conn:
        total_patients = conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    return {
        "total_findings": len(findings),
        "by_severity": by_severity,
        "by_code": dict(sorted(by_code.items(),
                                key=lambda kv: -kv[1])),
        "patients_affected": len(affected),
        "patients_total": total_patients,
    }


# ============================================================
# Tools (validation = deterministic, exposed to chat agent as data sources)
# ============================================================

def t_run_validation(patient_id: str | None = None) -> dict:
    findings = run_all_checks(patient_id=patient_id)
    return {"summary": summarize(findings), "findings": findings}


def t_get_validation_summary() -> dict:
    return summarize(run_all_checks())


def t_get_patient_record(patient_id: str) -> dict:
    """Read-only: pull the patient row + linked records so the chat agent can
    explain a finding in context."""
    with connect(readonly=True) as conn:
        p = conn.execute("SELECT * FROM patients WHERE patient_id = ?",
                          (patient_id,)).fetchone()
        if not p:
            return {"error": f"No patient with id {patient_id}"}
        def fetch(t):
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM {t} WHERE patient_id = ?", (patient_id,))]
        return {"patient": dict(p), "conditions": fetch("conditions"),
                "medications": fetch("medications"),
                "lab_orders": fetch("lab_orders")}


TOOLS = {
    "run_validation": t_run_validation,
    "get_validation_summary": t_get_validation_summary,
    "get_patient_record": t_get_patient_record,
}

TOOL_SCHEMAS = [
    {"name": "run_validation",
     "description": ("Run all deterministic validation rules and return the "
                     "findings. Pass patient_id to scope to one patient. "
                     "Validation is rule-based; do not invent findings."),
     "input_schema": {"type": "object",
                      "properties": {"patient_id": {"type": "string"}}}},
    {"name": "get_validation_summary",
     "description": "Just the counts (by severity, by code, patients affected).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_patient_record",
     "description": "Read-only fetch of a patient's record for context when explaining a finding.",
     "input_schema": {"type": "object",
                      "properties": {"patient_id": {"type": "string"}},
                      "required": ["patient_id"]}},
]


# ============================================================
# AI report generation (this is the only AI step in the pipeline)
# ============================================================

MODEL = "claude-haiku-4-5"

REPORT_SYSTEM_PROMPT = """You are a healthcare data quality reporter for
Twin Health. You receive structured validation findings produced by a
deterministic rule engine. Your only job is to render those findings into
a professional email to a healthcare provider.

Hard rules:
- Do NOT invent findings, codes, severities, or patient_ids. If something
  isn't in the structured input, do not mention it.
- Do NOT diagnose patients or give clinical advice. You describe data
  quality issues only.
- Keep the tone calm, professional, and actionable.

Email format:
- Begin with `Subject: ...` on its own line
- Then a `To: ...` line and `From: ...` line
- Blank line, then the body
- Greeting (use the recipient name provided; otherwise "Care Team")
- 1-2 sentence executive summary using the actual counts from the input
- Findings grouped by severity (CRITICAL > WARNING > INFO). For each
  severity, list the top issue codes with their counts and 1-3 example
  patient_ids. Do not enumerate every patient.
- A short "Recommended next steps" section with 2-4 concrete actions
- Sign-off as "Twin Health Data Quality Team"
"""


def generate_report(recipient: str | None = None) -> str:
    findings = run_all_checks()
    summary = summarize(findings)

    # Trim what we send the model — we don't need every finding, just enough
    # examples to write accurately about the top issues.
    examples_per_code: dict[str, list[dict]] = {}
    for f in findings:
        examples_per_code.setdefault(f["code"], [])
        if len(examples_per_code[f["code"]]) < 5:
            examples_per_code[f["code"]].append(
                {"patient_id": f.get("patient_id"),
                 "severity": f["severity"],
                 "message": f["message"]})

    payload = {
        "summary": summary,
        "examples_by_code": examples_per_code,
    }

    user_message = (
        f"Recipient name: {recipient or 'Care Team'}\n"
        f"Today's date: {date.today().isoformat()}\n\n"
        f"Validation results from the rule engine:\n"
        f"```json\n{json.dumps(payload, indent=2)}\n```\n\n"
        f"Write the email."
    )

    client = Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=REPORT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()


# ============================================================
# Optional: seed realistic data-quality issues into the DB
# ============================================================

def seed_issues(count: int = 8, seed: int | None = None) -> dict:
    rng = random.Random(seed)
    actions: list[str] = []
    with connect(readonly=False) as conn:
        pids = [r[0] for r in conn.execute(
            "SELECT patient_id FROM patients ORDER BY RANDOM() LIMIT ?", (count,))]
        if not pids:
            return {"error": "No patients in the DB to corrupt. Run patient_generator first."}

        corruptions = [
            "null_dob", "null_phone", "bad_email", "bad_zip",
            "dup_email", "stale_order", "med_without_condition",
            "bmi_mismatch", "diagnosis_future", "null_pcp",
        ]
        for pid in pids:
            kind = rng.choice(corruptions)
            if kind == "null_dob":
                conn.execute("UPDATE patients SET date_of_birth = '' WHERE patient_id = ?", (pid,))
                actions.append(f"{pid}: cleared date_of_birth")
            elif kind == "null_phone":
                conn.execute("UPDATE patients SET phone_mobile = NULL, email = NULL WHERE patient_id = ?", (pid,))
                actions.append(f"{pid}: cleared phone + email (no contact)")
            elif kind == "bad_email":
                conn.execute("UPDATE patients SET email = 'not-an-email' WHERE patient_id = ?", (pid,))
                actions.append(f"{pid}: set malformed email")
            elif kind == "bad_zip":
                conn.execute("UPDATE patients SET zip = 'ABCDE' WHERE patient_id = ?", (pid,))
                actions.append(f"{pid}: set malformed ZIP")
            elif kind == "dup_email":
                other = conn.execute(
                    "SELECT email FROM patients WHERE patient_id != ? AND email IS NOT NULL "
                    "ORDER BY RANDOM() LIMIT 1", (pid,)).fetchone()
                if other:
                    conn.execute("UPDATE patients SET email = ? WHERE patient_id = ?",
                                  (other[0], pid))
                    actions.append(f"{pid}: copied email from another patient (dup)")
            elif kind == "stale_order":
                stale_date = (date.today() - timedelta(days=rng.randint(120, 400))).isoformat()
                conn.execute(
                    "INSERT INTO lab_orders (order_id, patient_id, panel, fasting_required, "
                    "ordered_by, ordered_on, status) VALUES (?, ?, ?, 0, ?, ?, 'pending')",
                    (f"ORD-STALE-{rng.randint(10000, 99999)}", pid,
                     "HbA1c", "Dr. Patel", stale_date))
                actions.append(f"{pid}: added stale pending lab order ({stale_date})")
            elif kind == "med_without_condition":
                conn.execute(
                    "INSERT INTO medications (patient_id, name, dose, frequency, "
                    "prescribed_for, started_date, status) VALUES "
                    "(?, 'Metformin', '500mg', 'twice daily', 'Mystery indication', "
                    "?, 'active')",
                    (pid, date.today().isoformat()))
                actions.append(f"{pid}: added Metformin without diabetes diagnosis")
            elif kind == "bmi_mismatch":
                conn.execute("UPDATE patients SET bmi = 99.9 WHERE patient_id = ?", (pid,))
                actions.append(f"{pid}: set BMI to 99.9 (mismatched)")
            elif kind == "diagnosis_future":
                future = (date.today() + timedelta(days=rng.randint(30, 365))).isoformat()
                conn.execute(
                    "INSERT INTO conditions (patient_id, icd10_code, description, "
                    "diagnosed_date, status) VALUES (?, 'E11.9', "
                    "'Type 2 diabetes (test future-dated)', ?, 'active')",
                    (pid, future))
                actions.append(f"{pid}: added diagnosis dated in the future ({future})")
            elif kind == "null_pcp":
                conn.execute("UPDATE patients SET primary_care_physician = NULL WHERE patient_id = ?", (pid,))
                actions.append(f"{pid}: cleared primary_care_physician")
    return {"seeded": len(actions), "actions": actions}


# ============================================================
# Chat agent loop (uses the validation tools as read-only data sources)
# ============================================================

CHAT_SYSTEM_PROMPT = """You are the Healthcare Data Quality Assistant for
Twin Health. You answer questions about patient-data quality issues found
by a deterministic rule engine. Use the available tools to look up the
real findings — never invent them.

When the user asks for a report or an email, structure it like a
professional email with Subject / To / From / body / sign-off as
"Twin Health Data Quality Team". For exploration questions, be concise
and reference patient_ids only when relevant.

You can describe issues and recommend operational fixes (e.g. "request
updated contact info") but you do not give clinical advice and you do
not diagnose patients."""


def run_turn(client, messages):
    for _ in range(8):
        resp = client.messages.create(
            model=MODEL, max_tokens=2048, system=CHAT_SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS, messages=messages)
        for block in resp.content:
            if block.type == "text" and block.text.strip():
                print(f"\n{textwrap.fill(block.text.strip(), 80, initial_indent='Agent: ', subsequent_indent='       ')}")
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return messages
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
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


# ============================================================
# CLI
# ============================================================

def _print_json(result) -> int:
    out = json.dumps(result, indent=2, default=str)
    if isinstance(result, dict) and "error" in result:
        print(out, file=sys.stderr); return 1
    print(out); return 0


def run_chat() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        print("Create a .env file next to this script with:", file=sys.stderr)
        print("    ANTHROPIC_API_KEY=sk-ant-...", file=sys.stderr)
        return 1
    if not os.path.exists(DB_PATH):
        print(f"No DB found at {DB_PATH}. Run patient_generator.py first.",
              file=sys.stderr)
        return 1
    client = Anthropic()
    messages: list = []
    print("\n" + "=" * 64)
    print("  Healthcare Data Validation Reporter")
    print(f"  DB: {DB_PATH}")
    print("=" * 64)
    print("  Commands: /quit, /reset")
    print("  Try: 'What are the most common data quality issues?'\n")
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
        prog="validation_reporter",
        description=("Validate patient records (rule-based) and optionally "
                     "generate an AI-rendered email report. With no "
                     "subcommand, runs interactive chat."))
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("chat", help="Interactive chat agent (default)")

    p_val = sub.add_parser("validate", help="Run validation rules; print findings as JSON")
    p_val.add_argument("--patient", dest="patient_id",
                       help="Scope to one patient")

    sub.add_parser("summary", help="Quick counts (severity, top codes, affected patients)")

    p_rep = sub.add_parser("report", help="Generate an email report (uses Claude)")
    p_rep.add_argument("--recipient", help='e.g. "Dr. Patel"; default "Care Team"')
    p_rep.add_argument("--out", help="Write the email to this file instead of stdout")

    p_seed = sub.add_parser("seed-issues",
                            help="Intentionally corrupt random patients (demo only). REQUIRES write DB.")
    p_seed.add_argument("--count", type=int, default=8,
                        help="Number of corruptions to introduce")
    p_seed.add_argument("--seed", type=int, help="For reproducible output")

    args = parser.parse_args()

    if args.cmd is None or args.cmd == "chat":
        return run_chat()
    if args.cmd == "validate":
        try:
            return _print_json(t_run_validation(args.patient_id))
        except FileNotFoundError as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr); return 1
    if args.cmd == "summary":
        try:
            return _print_json(t_get_validation_summary())
        except FileNotFoundError as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr); return 1
    if args.cmd == "report":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is not set.", file=sys.stderr); return 1
        try:
            email = generate_report(recipient=args.recipient)
        except FileNotFoundError as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr); return 1
        if args.out:
            with open(args.out, "w") as f: f.write(email + "\n")
            print(f"Wrote {args.out}", file=sys.stderr)
        else:
            print(email)
        return 0
    if args.cmd == "seed-issues":
        try:
            return _print_json(seed_issues(count=args.count, seed=args.seed))
        except FileNotFoundError as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr); return 1
    parser.print_help(); return 1


if __name__ == "__main__":
    sys.exit(main())
