"""
Fake Patient Generator Agent — single-file edition.

Populates a SQLite DB with realistic, medically coherent fake patients
for use as test data. Companion to lab_scheduling_agent.py, which reads
from the same DB (patients.db by default).

    pip install anthropic faker python-dotenv
    # put ANTHROPIC_API_KEY=sk-ant-... in a .env file next to this script
    python patient_generator.py
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import textwrap
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any

from anthropic import Anthropic
from faker import Faker

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

DB_PATH = os.environ.get("PATIENTS_DB", "/logs/patients.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    patient_id TEXT PRIMARY KEY, mrn TEXT UNIQUE NOT NULL,
    first_name TEXT NOT NULL, middle_name TEXT, last_name TEXT NOT NULL,
    date_of_birth TEXT NOT NULL, sex TEXT, gender_identity TEXT,
    race TEXT, ethnicity TEXT, preferred_language TEXT, marital_status TEXT,
    street_address TEXT, city TEXT, state TEXT, zip TEXT,
    phone_mobile TEXT, phone_home TEXT, email TEXT,
    emergency_contact_name TEXT, emergency_contact_phone TEXT,
    emergency_contact_relationship TEXT, blood_type TEXT,
    height_cm REAL, weight_kg REAL, bmi REAL,
    primary_care_physician TEXT, pharmacy TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS insurance (
    id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL,
    provider TEXT, plan_name TEXT, policy_number TEXT, group_number TEXT,
    subscriber_name TEXT, is_primary INTEGER DEFAULT 1,
    FOREIGN KEY (patient_id) REFERENCES patients(patient_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS allergies (
    id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL,
    allergen TEXT NOT NULL, reaction TEXT, severity TEXT,
    FOREIGN KEY (patient_id) REFERENCES patients(patient_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS conditions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL,
    icd10_code TEXT, description TEXT NOT NULL,
    diagnosed_date TEXT, status TEXT DEFAULT 'active',
    FOREIGN KEY (patient_id) REFERENCES patients(patient_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS medications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL,
    name TEXT NOT NULL, dose TEXT, frequency TEXT, prescribed_for TEXT,
    started_date TEXT, status TEXT DEFAULT 'active',
    FOREIGN KEY (patient_id) REFERENCES patients(patient_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS vitals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL,
    measured_at TEXT NOT NULL, systolic_bp INTEGER, diastolic_bp INTEGER,
    heart_rate INTEGER, respiratory_rate INTEGER, temperature_f REAL, spo2 INTEGER,
    FOREIGN KEY (patient_id) REFERENCES patients(patient_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS immunizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL,
    vaccine TEXT NOT NULL, administered_date TEXT,
    FOREIGN KEY (patient_id) REFERENCES patients(patient_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS lab_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT UNIQUE NOT NULL,
    patient_id TEXT NOT NULL, panel TEXT NOT NULL,
    fasting_required INTEGER DEFAULT 0, ordered_by TEXT, ordered_on TEXT,
    status TEXT DEFAULT 'pending',
    FOREIGN KEY (patient_id) REFERENCES patients(patient_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_conditions_patient  ON conditions(patient_id);
CREATE INDEX IF NOT EXISTS idx_medications_patient ON medications(patient_id);
CREATE INDEX IF NOT EXISTS idx_lab_orders_patient  ON lab_orders(patient_id);
"""


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


def init_db(reset: bool = False) -> None:
    if reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    with connect() as conn:
        conn.executescript(SCHEMA)


# ============================================================
# Reference data
# ============================================================

US_STATES = ["CA","TX","FL","NY","PA","IL","OH","GA","NC","MI","NJ","VA","WA","AZ","MA","TN","IN","MO","MD","WI"]
RACES = ["White","Black or African American","Asian","American Indian or Alaska Native","Native Hawaiian or Other Pacific Islander","Other","Declined"]
ETHNICITIES = ["Hispanic or Latino","Not Hispanic or Latino","Declined"]
LANGUAGES = ["English","English","English","Spanish","Mandarin","Tagalog","Vietnamese","Arabic","Russian"]
MARITAL = ["Single","Married","Divorced","Widowed","Partnered","Separated"]
SEXES = ["Male","Female"]
BLOOD_TYPES = ["O+","O+","O+","A+","A+","B+","AB+","O-","A-","B-","AB-"]
RELATIONSHIPS = ["Spouse","Parent","Sibling","Child","Friend","Partner"]
INSURANCE_PROVIDERS = [
    ("Blue Cross Blue Shield", ["PPO Gold","HMO Silver","PPO Platinum"]),
    ("UnitedHealthcare", ["Choice Plus","Navigate","Select"]),
    ("Aetna", ["Open Access","HMO","POS II"]),
    ("Cigna", ["Open Access Plus","LocalPlus"]),
    ("Kaiser Permanente", ["HMO Gold","HMO Silver"]),
    ("Humana", ["ChoiceCare PPO","Gold Plus HMO"]),
    ("Medicare", ["Part B"]),
    ("Medicaid", ["State Plan"]),
]
PHARMACIES = ["CVS Pharmacy","Walgreens","Rite Aid","Walmart Pharmacy","Costco Pharmacy","Kroger Pharmacy","Safeway Pharmacy"]
PHYSICIANS = ["Dr. Patel","Dr. Nguyen","Dr. Okafor","Dr. Goldstein","Dr. Martinez","Dr. Chen","Dr. Williams","Dr. Singh","Dr. Anderson","Dr. Hassan"]
COMMON_ALLERGIES = [
    ("Penicillin","Hives","Moderate"),("Sulfa drugs","Rash","Mild"),
    ("Peanuts","Anaphylaxis","Severe"),("Latex","Contact dermatitis","Mild"),
    ("Shellfish","Swelling","Moderate"),("Aspirin","GI upset","Mild"),
    ("Iodine contrast","Hives","Moderate"),("Bee stings","Anaphylaxis","Severe"),
]
STANDARD_IMMUNIZATIONS = ["Tdap","Influenza (annual)","COVID-19","MMR","Varicella","Hepatitis B","HPV","Pneumococcal","Shingles (Shingrix)"]

CONDITION_PROFILES: dict[str, dict] = {
    "type_2_diabetes": {
        "icd10": "E11.9", "description": "Type 2 diabetes mellitus without complications", "min_age": 30,
        "medications": [("Metformin","500mg","twice daily"),("Metformin","1000mg","twice daily"),
                        ("Glipizide","5mg","once daily"),("Semaglutide","0.5mg","weekly injection"),
                        ("Empagliflozin","10mg","once daily")],
        "lab_orders": [("Comprehensive Metabolic Panel + HbA1c + Lipid Panel", True),("HbA1c", False)]},
    "prediabetes": {
        "icd10": "R73.03", "description": "Prediabetes", "min_age": 25,
        "medications": [("Metformin","500mg","once daily")],
        "lab_orders": [("HbA1c + Fasting Glucose", True)]},
    "hypertension": {
        "icd10": "I10", "description": "Essential (primary) hypertension", "min_age": 30,
        "medications": [("Lisinopril","10mg","once daily"),("Lisinopril","20mg","once daily"),
                        ("Amlodipine","5mg","once daily"),("Losartan","50mg","once daily"),
                        ("Hydrochlorothiazide","25mg","once daily")],
        "lab_orders": [("Basic Metabolic Panel", False)]},
    "hyperlipidemia": {
        "icd10": "E78.5", "description": "Hyperlipidemia, unspecified", "min_age": 35,
        "medications": [("Atorvastatin","20mg","once daily at bedtime"),
                        ("Rosuvastatin","10mg","once daily"),
                        ("Simvastatin","40mg","once daily at bedtime")],
        "lab_orders": [("Lipid Panel", True)]},
    "hypothyroidism": {
        "icd10": "E03.9", "description": "Hypothyroidism, unspecified", "min_age": 18,
        "medications": [("Levothyroxine","50mcg","once daily"),("Levothyroxine","75mcg","once daily"),
                        ("Levothyroxine","100mcg","once daily")],
        "lab_orders": [("Thyroid Panel (TSH, Free T4)", False)]},
    "asthma": {
        "icd10": "J45.909", "description": "Unspecified asthma, uncomplicated", "min_age": 5,
        "medications": [("Albuterol HFA","90mcg","as needed"),
                        ("Fluticasone (Flovent)","110mcg","twice daily"),
                        ("Montelukast","10mg","once daily at bedtime")],
        "lab_orders": []},
    "gerd": {
        "icd10": "K21.9", "description": "Gastro-esophageal reflux disease without esophagitis", "min_age": 25,
        "medications": [("Omeprazole","20mg","once daily"),("Pantoprazole","40mg","once daily")],
        "lab_orders": []},
    "depression": {
        "icd10": "F33.1", "description": "Major depressive disorder, recurrent, moderate", "min_age": 16,
        "medications": [("Sertraline","50mg","once daily"),("Fluoxetine","20mg","once daily"),
                        ("Escitalopram","10mg","once daily"),("Bupropion XL","150mg","once daily")],
        "lab_orders": []},
    "anxiety": {
        "icd10": "F41.1", "description": "Generalized anxiety disorder", "min_age": 16,
        "medications": [("Escitalopram","10mg","once daily"),("Buspirone","10mg","twice daily")],
        "lab_orders": []},
    "obesity": {
        "icd10": "E66.9", "description": "Obesity, unspecified", "min_age": 18,
        "medications": [("Semaglutide (Wegovy)","1.0mg","weekly injection"),
                        ("Tirzepatide (Zepbound)","5mg","weekly injection")],
        "lab_orders": [("Comprehensive Metabolic Panel", True)]},
    "ckd_stage_3": {
        "icd10": "N18.3", "description": "Chronic kidney disease, stage 3 (moderate)", "min_age": 50,
        "medications": [],
        "lab_orders": [("Basic Metabolic Panel + Urinalysis", False)]},
}


# ============================================================
# Generation
# ============================================================

def _pick_conditions(rng: random.Random, age: int, forced: list[str] | None) -> list[str]:
    if forced:
        return [c for c in forced if age >= CONDITION_PROFILES[c]["min_age"]]
    p_any = min(0.15 + (age / 100) * 0.7, 0.85)
    if rng.random() > p_any:
        return []
    eligible = [k for k, v in CONDITION_PROFILES.items() if age >= v["min_age"]]
    n = min(rng.choices([1, 2, 3], weights=[6, 3, 1])[0], len(eligible))
    return rng.sample(eligible, n)


def _height_weight(rng: random.Random, sex: str, force_obese: bool) -> tuple[float, float]:
    if sex == "Male":
        height, weight = rng.uniform(165, 190), rng.uniform(70, 105)
    else:
        height, weight = rng.uniform(152, 175), rng.uniform(55, 90)
    if force_obese:
        weight = rng.uniform(30.5, 37.0) * ((height / 100) ** 2)
    return round(height, 1), round(weight, 1)


def _vitals_for(rng: random.Random, conditions: list[str], bmi: float) -> dict:
    sys_b, dia_b = 118, 76
    if "hypertension" in conditions: sys_b, dia_b = 138, 86
    if bmi >= 30: sys_b += 4; dia_b += 2
    return {"systolic_bp": sys_b + rng.randint(-8, 10),
            "diastolic_bp": dia_b + rng.randint(-6, 8),
            "heart_rate": rng.randint(62, 88),
            "respiratory_rate": rng.randint(12, 18),
            "temperature_f": round(rng.uniform(97.6, 99.0), 1),
            "spo2": rng.randint(96, 99)}


def generate_and_insert(count: int, conditions=None, age_min=None, age_max=None,
                         state=None, seed=None) -> list[dict]:
    fake = Faker("en_US")
    if seed is not None:
        Faker.seed(seed); rng = random.Random(seed)
    else:
        rng = random.Random()

    if conditions:
        unknown = [c for c in conditions if c not in CONDITION_PROFILES]
        if unknown:
            raise ValueError(f"Unknown condition keys: {unknown}. "
                             f"Valid: {list(CONDITION_PROFILES)}")

    age_min = 18 if age_min is None else age_min
    age_max = 85 if age_max is None else age_max
    if age_min > age_max:
        raise ValueError("age_min must be <= age_max")

    summaries: list[dict] = []
    with connect() as conn:
        used = {r[0] for r in conn.execute("SELECT patient_id FROM patients")}
        for _ in range(count):
            for _attempt in range(200):
                pid = f"P-{rng.randint(1000, 9999)}"
                if pid not in used:
                    used.add(pid); break
            else:
                raise RuntimeError("Could not allocate unique patient_id")

            sex = rng.choice(SEXES)
            first = fake.first_name_male() if sex == "Male" else fake.first_name_female()
            last = fake.last_name()
            age = rng.randint(age_min, age_max)
            dob = date.today() - timedelta(days=age * 365 + rng.randint(0, 364))
            patient_conditions = _pick_conditions(rng, age, conditions)
            force_obese = "obesity" in patient_conditions
            height_cm, weight_kg = _height_weight(rng, sex, force_obese)
            bmi = round(weight_kg / ((height_cm / 100) ** 2), 1)
            pstate = state or rng.choice(US_STATES)

            row = {
                "patient_id": pid,
                "mrn": f"MRN-{fake.random_number(digits=8, fix_len=True)}",
                "first_name": first,
                "middle_name": fake.first_name() if rng.random() < 0.4 else None,
                "last_name": last, "date_of_birth": dob.isoformat(),
                "sex": sex, "gender_identity": sex,
                "race": rng.choice(RACES), "ethnicity": rng.choice(ETHNICITIES),
                "preferred_language": rng.choice(LANGUAGES),
                "marital_status": rng.choice(MARITAL),
                "street_address": fake.street_address(),
                "city": fake.city(), "state": pstate, "zip": fake.zipcode(),
                "phone_mobile": fake.numerify("+1-###-555-####"),
                "phone_home": fake.numerify("+1-###-555-####") if rng.random() < 0.5 else None,
                "email": f"{first.lower()}.{last.lower()}@example.com",
                "emergency_contact_name": fake.name(),
                "emergency_contact_phone": fake.numerify("+1-###-555-####"),
                "emergency_contact_relationship": rng.choice(RELATIONSHIPS),
                "blood_type": rng.choice(BLOOD_TYPES),
                "height_cm": height_cm, "weight_kg": weight_kg, "bmi": bmi,
                "primary_care_physician": rng.choice(PHYSICIANS),
                "pharmacy": rng.choice(PHARMACIES),
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            cols = ", ".join(row); ph = ", ".join("?" * len(row))
            conn.execute(f"INSERT INTO patients ({cols}) VALUES ({ph})", list(row.values()))

            # Insurance
            for i in range(2 if rng.random() < 0.12 else 1):
                prov, plans = rng.choice(INSURANCE_PROVIDERS)
                conn.execute("INSERT INTO insurance (patient_id, provider, plan_name, "
                             "policy_number, group_number, subscriber_name, is_primary) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (pid, prov, rng.choice(plans), fake.numerify("###########"),
                              fake.numerify("GRP######"), f"{first} {last}", 1 if i == 0 else 0))
            # Allergies
            if rng.random() < 0.3:
                for a, r, s in rng.sample(COMMON_ALLERGIES, rng.choices([1, 2], weights=[4, 1])[0]):
                    conn.execute("INSERT INTO allergies (patient_id, allergen, reaction, severity) "
                                 "VALUES (?, ?, ?, ?)", (pid, a, r, s))
            # Conditions + linked meds + linked orders
            for cond_key in patient_conditions:
                prof = CONDITION_PROFILES[cond_key]
                dx = dob + timedelta(days=(prof["min_age"] + rng.randint(0, max(1, age - prof["min_age"]))) * 365)
                if dx > date.today():
                    dx = date.today() - timedelta(days=rng.randint(30, 1500))
                conn.execute("INSERT INTO conditions (patient_id, icd10_code, description, "
                             "diagnosed_date, status) VALUES (?, ?, ?, ?, 'active')",
                             (pid, prof["icd10"], prof["description"], dx.isoformat()))
                for med, dose, freq in prof["medications"]:
                    if rng.random() < 0.55:
                        conn.execute("INSERT INTO medications (patient_id, name, dose, "
                                     "frequency, prescribed_for, started_date, status) "
                                     "VALUES (?, ?, ?, ?, ?, ?, 'active')",
                                     (pid, med, dose, freq, prof["description"],
                                      (date.today() - timedelta(days=rng.randint(30, 1200))).isoformat()))
                for panel, fasting in prof["lab_orders"]:
                    if rng.random() < 0.6:
                        order_id = f"ORD-{rng.randint(50000, 99999)}"
                        conn.execute("INSERT INTO lab_orders (order_id, patient_id, panel, "
                                     "fasting_required, ordered_by, ordered_on, status) "
                                     "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                                     (order_id, pid, panel, int(fasting),
                                      row["primary_care_physician"],
                                      (date.today() - timedelta(days=rng.randint(1, 30))).isoformat()))
            # Vitals
            v = _vitals_for(rng, patient_conditions, bmi)
            conn.execute("INSERT INTO vitals (patient_id, measured_at, systolic_bp, diastolic_bp, "
                         "heart_rate, respiratory_rate, temperature_f, spo2) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (pid, datetime.now().isoformat(timespec="seconds"),
                          v["systolic_bp"], v["diastolic_bp"], v["heart_rate"],
                          v["respiratory_rate"], v["temperature_f"], v["spo2"]))
            # Immunizations
            for vax in rng.sample(STANDARD_IMMUNIZATIONS, rng.randint(2, 5)):
                conn.execute("INSERT INTO immunizations (patient_id, vaccine, administered_date) "
                             "VALUES (?, ?, ?)",
                             (pid, vax, (date.today() - timedelta(days=rng.randint(30, 1800))).isoformat()))

            summaries.append({"patient_id": pid, "name": f"{first} {last}", "age": age,
                              "state": pstate,
                              "conditions": [CONDITION_PROFILES[c]["description"] for c in patient_conditions] or ["None"]})
    return summaries


# ============================================================
# Tools exposed to Claude
# ============================================================

def t_initialize_database(reset: bool = False) -> dict:
    init_db(reset=reset)
    return {"status": "initialized", "reset": reset, "db_path": DB_PATH}


def t_list_supported_conditions() -> dict:
    return {"conditions": [{"key": k, "icd10": v["icd10"],
                            "description": v["description"], "min_age": v["min_age"]}
                           for k, v in CONDITION_PROFILES.items()]}


def t_generate_patients(count, conditions=None, age_min=None, age_max=None,
                        state=None, seed=None) -> dict:
    if count < 1 or count > 500:
        return {"error": "count must be between 1 and 500"}
    init_db()  # ensure tables exist
    summaries = generate_and_insert(count=count, conditions=conditions,
                                    age_min=age_min, age_max=age_max,
                                    state=state, seed=seed)
    return {"generated": len(summaries), "patients": summaries}


def t_get_patient_details(patient_id: str) -> dict:
    with connect() as conn:
        p = conn.execute("SELECT * FROM patients WHERE patient_id = ?", (patient_id,)).fetchone()
        if not p:
            return {"error": f"No patient with id {patient_id}"}
        def fetch(t):
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM {t} WHERE patient_id = ?", (patient_id,))]
        return {"patient": dict(p), "insurance": fetch("insurance"),
                "allergies": fetch("allergies"), "conditions": fetch("conditions"),
                "medications": fetch("medications"), "vitals": fetch("vitals"),
                "immunizations": fetch("immunizations"), "lab_orders": fetch("lab_orders")}


def t_query_patients(state=None, condition_icd10=None, min_age=None, max_age=None, limit=20) -> dict:
    yr = date.today().year
    clauses, params = [], []
    if state: clauses.append("p.state = ?"); params.append(state)
    if min_age is not None:
        clauses.append("CAST(strftime('%Y', p.date_of_birth) AS INTEGER) <= ?"); params.append(yr - min_age)
    if max_age is not None:
        clauses.append("CAST(strftime('%Y', p.date_of_birth) AS INTEGER) >= ?"); params.append(yr - max_age)
    if condition_icd10:
        clauses.append("EXISTS (SELECT 1 FROM conditions c WHERE c.patient_id = p.patient_id AND c.icd10_code = ?)")
        params.append(condition_icd10)
    sql = "SELECT p.patient_id, p.first_name, p.last_name, p.date_of_birth, p.sex, p.state, p.bmi FROM patients p"
    if clauses: sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY p.created_at DESC LIMIT ?"; params.append(int(limit))
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        return {"count": len(rows), "patients": [dict(r) for r in rows]}


def t_run_sql(query: str) -> dict:
    q = query.strip().lower()
    bad = ("insert","update","delete","drop","alter","truncate","replace","attach","create")
    if any(q.startswith(w) or f" {w} " in q for w in bad):
        return {"error": "Only read-only SELECT/WITH/EXPLAIN queries are allowed."}
    if not (q.startswith("select") or q.startswith("with") or q.startswith("explain")):
        return {"error": "Query must start with SELECT, WITH, or EXPLAIN."}
    with connect() as conn:
        try:
            rows = conn.execute(query).fetchall()
        except Exception as exc:
            return {"error": f"SQL error: {exc}"}
        capped = rows[:100]
        return {"row_count": len(rows), "rows": [dict(r) for r in capped],
                "truncated": len(rows) > 100}


def t_get_database_stats() -> dict:
    with connect() as conn:
        def n(t): return conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        cond = conn.execute("SELECT description, COUNT(*) AS n FROM conditions GROUP BY description ORDER BY n DESC").fetchall()
        st = conn.execute("SELECT state, COUNT(*) AS n FROM patients GROUP BY state ORDER BY n DESC").fetchall()
        return {"patients": n("patients"), "conditions": n("conditions"),
                "medications": n("medications"), "lab_orders": n("lab_orders"),
                "allergies": n("allergies"), "immunizations": n("immunizations"),
                "by_condition": [dict(r) for r in cond],
                "by_state": [dict(r) for r in st]}


TOOLS = {
    "initialize_database": t_initialize_database,
    "list_supported_conditions": t_list_supported_conditions,
    "generate_patients": t_generate_patients,
    "get_patient_details": t_get_patient_details,
    "query_patients": t_query_patients,
    "run_sql": t_run_sql,
    "get_database_stats": t_get_database_stats,
}

TOOL_SCHEMAS = [
    {"name": "initialize_database",
     "description": "Create the SQLite tables. Pass reset=true to drop existing data first.",
     "input_schema": {"type": "object",
                      "properties": {"reset": {"type": "boolean", "default": False}}}},
    {"name": "list_supported_conditions",
     "description": "List the condition keys the generator understands. Call this if you need exact keys for the `conditions` filter.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "generate_patients",
     "description": ("Generate `count` realistic fake patients and insert them. Each gets "
                     "demographics, insurance, vitals, immunizations, and (by age) plausible "
                     "conditions with linked medications and pending lab orders. Use "
                     "`conditions` to force a diagnosis, `seed` for reproducibility."),
     "input_schema": {"type": "object",
                      "properties": {
                          "count":      {"type": "integer", "description": "1-500"},
                          "conditions": {"type": "array", "items": {"type": "string"}},
                          "age_min":    {"type": "integer"},
                          "age_max":    {"type": "integer"},
                          "state":      {"type": "string", "description": "Two-letter US state code"},
                          "seed":       {"type": "integer"}},
                      "required": ["count"]}},
    {"name": "get_patient_details",
     "description": "Full chart for one patient: demographics + insurance + allergies + conditions + meds + vitals + immunizations + lab orders.",
     "input_schema": {"type": "object",
                      "properties": {"patient_id": {"type": "string"}},
                      "required": ["patient_id"]}},
    {"name": "query_patients",
     "description": "Filter patients by state, age range, or ICD-10 condition.",
     "input_schema": {"type": "object",
                      "properties": {"state": {"type": "string"},
                                     "condition_icd10": {"type": "string"},
                                     "min_age": {"type": "integer"},
                                     "max_age": {"type": "integer"},
                                     "limit": {"type": "integer", "default": 20}}}},
    {"name": "run_sql",
     "description": "Read-only SQL (SELECT/WITH/EXPLAIN). Results capped at 100 rows.",
     "input_schema": {"type": "object",
                      "properties": {"query": {"type": "string"}},
                      "required": ["query"]}},
    {"name": "get_database_stats",
     "description": "Counts per table + breakdowns by condition and state.",
     "input_schema": {"type": "object", "properties": {}}},
]


# ============================================================
# Agent loop
# ============================================================

MODEL = "claude-sonnet-4-5"
SYSTEM_PROMPT = """You are a fake-patient data generator agent. You help an
engineer populate a SQLite test database with realistic, medically coherent
patient records for development and testing of healthcare software (in
particular, a lab-appointment-scheduling agent that reads from the same DB).

How you operate:
- All data is fake test data. Generate freely.
- When the user asks for "some patients" without specifics, default to ~10.
- When they mention a clinical concept ("diabetics", "older hypertensives in
  Texas"), translate to the right tool arguments. If unsure of condition keys,
  call list_supported_conditions first.
- After generating, show a short readable summary of who was created.
- For analytical questions, prefer get_database_stats or run_sql over loading
  every record into context.
- If the DB doesn't exist yet, call initialize_database before the first insert.
- Be friendly and concise."""


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
    client = Anthropic()
    messages: list = []
    print("\n" + "=" * 64)
    print("  Fake Patient Generator Agent")
    print(f"  DB: {DB_PATH}")
    print("=" * 64)
    print("  Commands: /quit, /reset")
    print("  Try: 'Generate 20 patients, half diabetic, ages 40-70'\n")
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
        prog="patient_generator",
        description="Generate fake patient data, or run the agentic chat.",
        epilog="With no subcommand (or with `chat`), launches the interactive agent.",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("chat", help="Run the interactive agent (default)")

    p_init = sub.add_parser("init", help="Create the SQLite tables")
    p_init.add_argument("--reset", action="store_true",
                        help="Drop existing DB file before creating tables")

    sub.add_parser("conditions",
                   help="List supported condition keys and their ICD-10 codes")

    p_gen = sub.add_parser("generate", help="Generate fake patients")
    p_gen.add_argument("--count", type=int, required=True, help="Number to generate (1-500)")
    p_gen.add_argument("--conditions", nargs="+", metavar="KEY",
                       help="Force every patient to have these conditions "
                            "(see `conditions` for valid keys)")
    p_gen.add_argument("--age-min", type=int, dest="age_min")
    p_gen.add_argument("--age-max", type=int, dest="age_max")
    p_gen.add_argument("--state", help="Two-letter US state, e.g. CA")
    p_gen.add_argument("--seed", type=int, help="For reproducible output")

    p_det = sub.add_parser("details", help="Full chart for a patient")
    p_det.add_argument("patient_id")

    p_q = sub.add_parser("query", help="Filter patients (lightweight summaries)")
    p_q.add_argument("--state")
    p_q.add_argument("--icd10", dest="condition_icd10",
                     help="e.g. E11.9 for Type 2 diabetes")
    p_q.add_argument("--min-age", type=int, dest="min_age")
    p_q.add_argument("--max-age", type=int, dest="max_age")
    p_q.add_argument("--limit", type=int, default=20)

    sub.add_parser("stats", help="Counts and breakdowns")

    p_sql = sub.add_parser("sql", help="Run a read-only SQL query")
    p_sql.add_argument("query", help='e.g. "SELECT COUNT(*) FROM patients"')

    args = parser.parse_args()

    if args.cmd is None or args.cmd == "chat":
        return run_chat()
    if args.cmd == "init":
        return _print_result(t_initialize_database(reset=args.reset))
    if args.cmd == "conditions":
        return _print_result(t_list_supported_conditions())
    if args.cmd == "generate":
        return _print_result(t_generate_patients(
            count=args.count, conditions=args.conditions,
            age_min=args.age_min, age_max=args.age_max,
            state=args.state, seed=args.seed))
    if args.cmd == "details":
        return _print_result(t_get_patient_details(args.patient_id))
    if args.cmd == "query":
        return _print_result(t_query_patients(
            state=args.state, condition_icd10=args.condition_icd10,
            min_age=args.min_age, max_age=args.max_age, limit=args.limit))
    if args.cmd == "stats":
        return _print_result(t_get_database_stats())
    if args.cmd == "sql":
        return _print_result(t_run_sql(args.query))
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())