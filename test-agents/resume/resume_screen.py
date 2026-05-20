#!/usr/bin/env python3
"""
Resume Screening Agent
======================
Scores an applicant's resume against a job description using Claude,
saves the result to SQLite, and flags boundary scores for human review.

Usage:
    python screen.py <resume_path> <job_id> [--cover-letter PATH] [--references PATH]
    python screen.py resumes/jane_doe.pdf senior_python_dev
    python screen.py --review
    python screen.py --resolve jane@example.com senior_python_dev approved "Alice Smith"
    python screen.py --scores [job_id]
"""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import pdfplumber
from docx import Document as DocxDocument
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
JOB_DESC_DIR = BASE_DIR / "job_descriptions"
DB_PATH = BASE_DIR / "data" / "applicants.db"

SCORING_MODEL = os.getenv("SCORING_MODEL", "claude-sonnet-4-5-20250929")

REJECT_CEILING = 70
FORWARD_FLOOR = 90
REVIEW_LOW = 65
REVIEW_HIGH = 92


# ---------------------------------------------------------------------------
# Document parsing
# ---------------------------------------------------------------------------

def parse_document(file_path: str) -> str:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    ext = path.suffix.lower()
    if ext == ".pdf":
        return _parse_pdf(path)
    elif ext == ".docx":
        return _parse_docx(path)
    raise ValueError(f"Unsupported file type: '{ext}'. Use .pdf or .docx")


def _parse_pdf(path: Path) -> str:
    pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
            for table in page.extract_tables():
                for row in table:
                    cells = [c.strip() if c else "" for c in row]
                    pages.append(" | ".join(cells))
    text = "\n\n".join(pages).strip()
    if not text:
        raise ValueError(f"No text extracted from {path}. May be scanned — needs OCR.")
    return text


def _parse_docx(path: Path) -> str:
    doc = DocxDocument(str(path))
    parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n".join(parts).strip()
    if not text:
        raise ValueError(f"No text extracted from {path}")
    return text


# ---------------------------------------------------------------------------
# Job description
# ---------------------------------------------------------------------------

def load_job_description(job_id: str) -> dict:
    path = JOB_DESC_DIR / f"{job_id}.json"
    if not path.exists():
        available = [f.stem for f in JOB_DESC_DIR.glob("*.json")]
        raise FileNotFoundError(f"Job '{job_id}' not found. Available: {available}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# LLM scoring + extraction
# ---------------------------------------------------------------------------

async def score_resume(client: anthropic.AsyncAnthropic, jd: dict,
                       resume_text: str, cover_letter: str | None = None,
                       references: str | None = None) -> dict:
    """Score resume against JD and extract applicant name/email from the resume."""
    title = jd.get("title", "Unknown")
    description = jd.get("description", "")
    requirements = jd.get("requirements", [])
    preferred = jd.get("preferred_qualifications", [])

    req_list = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(requirements)) or "(none)"
    pref_list = "\n".join(f"  {i+1}. {p}" for i, p in enumerate(preferred)) or "(none)"

    optional = ""
    if cover_letter:
        optional += f"\n\nCOVER LETTER:\n{cover_letter}"
    if references:
        optional += f"\n\nREFERENCE LETTERS:\n{references}"

    prompt = f"""You are an expert resume screener. You have two tasks:

TASK 1: Extract the applicant's full name and email address from the resume.
If either cannot be found, use "Unknown" for name and "unknown@unknown.com" for email.

TASK 2: Score this applicant's qualifications against the job description on a scale of 0 to 100.

SCORING CRITERIA:
- 90-100: Meets ALL required qualifications AND MOST preferred qualifications.
- 70-89:  Meets MOST required qualifications AND SOME preferred qualifications.
- Below 70: Meets FEW or NONE of the required qualifications.

Score based on evidence in the documents only, not assumptions.

---

JOB: {title}

DESCRIPTION:
{description}

REQUIRED QUALIFICATIONS:
{req_list}

PREFERRED QUALIFICATIONS:
{pref_list}

---

APPLICANT RESUME:
{resume_text}{optional}

---

Respond with a single JSON object, no markdown fences, no preamble:
{{
  "applicant_name": "<full name extracted from resume>",
  "applicant_email": "<email extracted from resume>",
  "score": <int 0-100>,
  "justification": "<2-4 paragraph explanation>",
  "requirements_met": ["<requirement text>", ...],
  "requirements_missing": ["<requirement text>", ...],
  "preferred_met": ["<preferred qual text>", ...],
  "preferred_missing": ["<preferred qual text>", ...]
}}"""

    response = await client.messages.create(
        model=SCORING_MODEL,
        max_tokens=2000,
        temperature=0.2,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()

    result = json.loads(raw)
    result["score"] = max(0, min(100, int(result["score"])))
    result.setdefault("applicant_name", "Unknown")
    result.setdefault("applicant_email", "unknown@unknown.com")
    return result


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS applicant_scores (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            applicant_name       TEXT NOT NULL,
            applicant_email      TEXT NOT NULL,
            job_id               TEXT NOT NULL,
            job_title            TEXT NOT NULL,
            score                INTEGER NOT NULL,
            tier                 TEXT NOT NULL,
            justification        TEXT NOT NULL,
            requirements_met     TEXT DEFAULT '[]',
            requirements_missing TEXT DEFAULT '[]',
            preferred_met        TEXT DEFAULT '[]',
            preferred_missing    TEXT DEFAULT '[]',
            flagged_for_review   INTEGER DEFAULT 0,
            review_status        TEXT DEFAULT 'pending',
            reviewed_by          TEXT,
            reviewed_at          TEXT,
            created_at           TEXT NOT NULL,
            UNIQUE(applicant_email, job_id)
        );
    """)
    conn.commit()
    return conn


def save_score(conn, *, applicant_name, applicant_email, job_id, job_title,
               score, tier, justification, requirements_met, requirements_missing,
               preferred_met, preferred_missing, flagged):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO applicant_scores (
            applicant_name, applicant_email, job_id, job_title,
            score, tier, justification,
            requirements_met, requirements_missing, preferred_met, preferred_missing,
            flagged_for_review, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(applicant_email, job_id) DO UPDATE SET
            score=excluded.score, tier=excluded.tier,
            justification=excluded.justification,
            requirements_met=excluded.requirements_met,
            requirements_missing=excluded.requirements_missing,
            preferred_met=excluded.preferred_met,
            preferred_missing=excluded.preferred_missing,
            flagged_for_review=excluded.flagged_for_review,
            review_status='pending', reviewed_by=NULL, reviewed_at=NULL,
            created_at=excluded.created_at
    """, (
        applicant_name, applicant_email, job_id, job_title,
        score, tier, justification,
        json.dumps(requirements_met), json.dumps(requirements_missing),
        json.dumps(preferred_met), json.dumps(preferred_missing),
        int(flagged), now,
    ))
    conn.commit()


# ---------------------------------------------------------------------------
# Tier / flag logic
# ---------------------------------------------------------------------------

def determine_tier(score: int) -> str:
    if score >= FORWARD_FLOOR:
        return "forward_to_manager"
    elif score > REJECT_CEILING:
        return "keep_on_file"
    return "reject"


def is_flagged(score: int) -> bool:
    return (REVIEW_LOW <= score <= REJECT_CEILING) or (FORWARD_FLOOR <= score <= REVIEW_HIGH)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

async def cmd_screen(args):
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set. Add it to .env", file=sys.stderr)
        sys.exit(1)

    jd = load_job_description(args.job_id)
    print(f"Job: {jd['title']}")

    print(f"Parsing resume: {args.resume_path}")
    resume_text = parse_document(args.resume_path)
    print(f"  Extracted {len(resume_text)} chars")

    cover_letter = None
    if args.cover_letter:
        print(f"Parsing cover letter: {args.cover_letter}")
        cover_letter = parse_document(args.cover_letter)

    references = None
    if args.references:
        print(f"Parsing references: {args.references}")
        references = parse_document(args.references)

    print(f"Scoring against {jd['title']}...")
    client = anthropic.AsyncAnthropic(api_key=api_key)
    result = await score_resume(client, jd, resume_text, cover_letter, references)

    score = result["score"]
    tier = determine_tier(score)
    flagged = is_flagged(score)
    name = result["applicant_name"]
    email = result["applicant_email"]

    conn = get_db()
    save_score(
        conn,
        applicant_name=name, applicant_email=email,
        job_id=args.job_id, job_title=jd["title"],
        score=score, tier=tier, justification=result["justification"],
        requirements_met=result.get("requirements_met", []),
        requirements_missing=result.get("requirements_missing", []),
        preferred_met=result.get("preferred_met", []),
        preferred_missing=result.get("preferred_missing", []),
        flagged=flagged,
    )
    conn.close()

    print("\n" + "=" * 60)
    print(f"APPLICANT:  {name} ({email})")
    print(f"POSITION:   {jd['title']}")
    print(f"SCORE:      {score}/100")
    print(f"TIER:       {tier}")
    if flagged:
        print("⚠ FLAGGED FOR HUMAN REVIEW (boundary score)")
    print(f"\n{result['justification']}")

    if result.get("requirements_met"):
        print("\nRequirements met:")
        for r in result["requirements_met"]:
            print(f"  ✓ {r}")
    if result.get("requirements_missing"):
        print("\nRequirements missing:")
        for r in result["requirements_missing"]:
            print(f"  ✗ {r}")
    if result.get("preferred_met"):
        print("\nPreferred qualifications met:")
        for p in result["preferred_met"]:
            print(f"  ✓ {p}")
    if result.get("preferred_missing"):
        print("\nPreferred qualifications missing:")
        for p in result["preferred_missing"]:
            print(f"  ✗ {p}")

    print("=" * 60)
    print(f"Saved to {DB_PATH}")


def cmd_review(args):
    conn = get_db()
    rows = conn.execute(
        "SELECT applicant_name, applicant_email, job_id, job_title, score, tier, justification "
        "FROM applicant_scores WHERE flagged_for_review=1 AND review_status='pending' "
        "ORDER BY created_at DESC"
    ).fetchall()
    conn.close()

    if not rows:
        print("No applicants pending review.")
        return

    print(f"{len(rows)} applicant(s) flagged for review:\n")
    for r in rows:
        print(f"  {r['applicant_name']} ({r['applicant_email']})")
        print(f"    Job:   {r['job_title']} ({r['job_id']})")
        print(f"    Score: {r['score']}/100  Tier: {r['tier']}")
        print(f"    {r['justification'][:120]}...")
        print()


def cmd_resolve(args):
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE applicant_scores SET review_status=?, reviewed_by=?, reviewed_at=? "
        "WHERE applicant_email=? AND job_id=? AND flagged_for_review=1",
        (args.decision, args.reviewed_by, now, args.applicant_email, args.job_id),
    )
    conn.commit()
    conn.close()

    if cur.rowcount == 0:
        print(f"No pending flagged record found for {args.applicant_email} / {args.job_id}")
    else:
        print(f"Resolved: {args.applicant_email} → {args.decision} (by {args.reviewed_by})")


def cmd_scores(args):
    conn = get_db()
    if args.job_id:
        rows = conn.execute(
            "SELECT applicant_name, applicant_email, job_title, score, tier, flagged_for_review, review_status "
            "FROM applicant_scores WHERE job_id=? ORDER BY score DESC", (args.job_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT applicant_name, applicant_email, job_title, score, tier, flagged_for_review, review_status "
            "FROM applicant_scores ORDER BY job_title, score DESC"
        ).fetchall()
    conn.close()

    if not rows:
        print("No scores recorded yet.")
        return

    current_job = None
    for r in rows:
        if r["job_title"] != current_job:
            current_job = r["job_title"]
            print(f"\n{'=' * 95}")
            print(f"  {current_job}")
            print(f"{'=' * 95}")
            print(f"  {'Name':<25} {'Email':<30} {'Score':>5}  {'Tier':<20} {'Status'}")
            print(f"  {'-' * 91}")
        flag = "⚠ review" if r["flagged_for_review"] else ""
        status = r["review_status"] if r["flagged_for_review"] else ""
        print(f"  {r['applicant_name']:<25} {r['applicant_email']:<30} {r['score']:>5}  {r['tier']:<20} {flag} {status}")
    print()


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Resume Screening Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python screen.py resumes/jane.pdf senior_python_dev
  python screen.py resumes/jane.pdf senior_python_dev --cover-letter resumes/jane_cl.pdf
  python screen.py --review
  python screen.py --resolve jane@example.com senior_python_dev approved "Alice Smith"
  python screen.py --scores
  python screen.py --scores senior_python_dev
        """,
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--review", action="store_true", help="Show flagged applicants pending review")
    group.add_argument("--resolve", nargs=4, metavar=("EMAIL", "JOB_ID", "DECISION", "REVIEWER"),
                       help="Resolve a flagged review: EMAIL JOB_ID approved|rejected REVIEWER_NAME")
    group.add_argument("--scores", nargs="?", const="__all__", metavar="JOB_ID",
                       help="Show all scores, optionally filtered by job ID")

    parser.add_argument("resume_path", nargs="?", help="Path to resume PDF or DOCX")
    parser.add_argument("job_id", nargs="?", help="Job description ID (filename without .json)")
    parser.add_argument("--cover-letter", help="Path to cover letter PDF or DOCX")
    parser.add_argument("--references", help="Path to reference letters PDF or DOCX")

    args = parser.parse_args()

    if args.review:
        cmd_review(args)
    elif args.resolve:
        args.applicant_email, args.job_id, args.decision, args.reviewed_by = args.resolve
        if args.decision not in ("approved", "rejected"):
            parser.error("Decision must be 'approved' or 'rejected'")
        cmd_resolve(args)
    elif args.scores:
        args.job_id = None if args.scores == "__all__" else args.scores
        cmd_scores(args)
    elif args.resume_path and args.job_id:
        asyncio.run(cmd_screen(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()