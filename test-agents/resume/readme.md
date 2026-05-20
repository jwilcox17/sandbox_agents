# Resume Screening Agent

A command-line tool that scores job applicant resumes against a job description using Claude. It parses the resume, extracts the applicant's name and email, scores their qualifications on a 0–100 scale, saves everything to a local database, and flags borderline scores for human review.

## Prerequisites

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/)

## Setup

```bash
git clone <your-repo-url> resume-screener
cd resume-screener

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
```

Open `.env` and replace `your-api-key-here` with your Anthropic API key.

## Quick Start

Drop a resume (PDF or DOCX) into the `resumes/` folder and run:

```bash
python screen.py resumes/jane_doe.pdf senior_python_dev
```

That's it. The agent will:

1. Load the job description from `job_descriptions/senior_python_dev.json`
2. Extract text from the resume
3. Send it to Claude, which scores the resume and extracts the applicant's name and email
4. Save the result to a local SQLite database at `data/applicants.db`
5. Print a full report to the terminal

You can also include a cover letter or reference letters:

```bash
python screen.py resumes/jane_doe.pdf senior_python_dev \
  --cover-letter resumes/jane_cl.pdf \
  --references resumes/jane_refs.docx
```

## Scoring

The agent scores resumes on a 0–100 scale based on how closely the applicant's qualifications match the job description.

| Score    | Tier               | Meaning                                                    |
|----------|--------------------|------------------------------------------------------------|
| 90–100   | forward_to_manager | Meets all requirements and most preferred qualifications   |
| 71–89    | keep_on_file       | Meets most requirements and some preferred qualifications  |
| 0–70     | reject             | Meets few or none of the requirements                      |

Scores near the tier boundaries (65–70 and 90–92) are automatically flagged for human review because the automated decision is less certain in those ranges.

## Viewing Results

Show all scored applicants, grouped by position:

```bash
python screen.py --scores
```

Filter to a specific job:

```bash
python screen.py --scores senior_python_dev
```

Example output:

```
===============================================================================================
  Senior Python Developer
===============================================================================================
  Name                      Email                          Score  Tier                 Status
  -------------------------------------------------------------------------------------------
  Jane Doe                  jane@example.com                  92  forward_to_manager   ⚠ review pending
  John Smith                john@example.com                  78  keep_on_file
  Bob Jones                 bob@example.com                   45  reject
```

## Human Review

Show all applicants flagged for review:

```bash
python screen.py --review
```

Approve or reject a flagged applicant:

```bash
python screen.py --resolve jane@example.com senior_python_dev approved "Alice Smith"
python screen.py --resolve bob@example.com senior_python_dev rejected "Alice Smith"
```

The decision and reviewer name are recorded in the database.

## Job Descriptions

Job descriptions are JSON files in the `job_descriptions/` folder. The filename (without `.json`) is the job ID you pass to the CLI.

A job description has four fields:

```json
{
  "title": "Senior Python Developer",
  "description": "A short paragraph describing the role.",
  "requirements": [
    "Requirement 1",
    "Requirement 2"
  ],
  "preferred_qualifications": [
    "Nice-to-have 1",
    "Nice-to-have 2"
  ]
}
```

To add a new position, create a new JSON file in that folder. It's available immediately:

```bash
python screen.py resumes/someone.pdf my_new_job_id
```

Two sample job descriptions are included: `senior_python_dev` and `new_grad_verification_eng`.

## Project Structure

```
resume-screener/
├── screen.py                  # the entire agent
├── requirements.txt
├── .env.example
├── job_descriptions/          # one JSON file per open position
│   ├── senior_python_dev.json
│   └── new_grad_verification_eng.json
├── resumes/                   # drop applicant PDFs/DOCXs here
└── data/
    └── applicants.db          # SQLite database (auto-created on first run)
```

## Configuration

All configuration is in `.env`:

| Variable           | Required | Default                        | Description                          |
|--------------------|----------|--------------------------------|--------------------------------------|
| `ANTHROPIC_API_KEY`| Yes      | —                              | Your Anthropic API key               |
| `SCORING_MODEL`    | No       | `claude-sonnet-4-5-20250929`   | Claude model used for scoring        |

## Database

Results are stored in a SQLite database at `data/applicants.db`, created automatically on first run. Each row is a unique (applicant email, job ID) pair, so the same person can be scored against multiple positions without conflict.

You can query it directly if you want:

```bash
sqlite3 data/applicants.db "SELECT applicant_name, score, tier FROM applicant_scores"
```

If you rescren the same resume against the same job, the existing row is updated (upsert), not duplicated.

## Email Notifications (Not Yet Active)

The code includes a Gmail OAuth integration for sending applicant notification emails, but it is not wired into the screening pipeline yet. When enabled, it will send a tier-appropriate email to the applicant after scoring (rejection, keep on file, or forwarded to manager).

To set it up for when it's ready:

1. Create OAuth credentials in the [Google Cloud Console](https://console.cloud.google.com/apis/credentials) (Desktop app type)
2. Enable the Gmail API for your project
3. Download the client secret JSON and save it as `gmail_credentials.json` in the project root
4. Add to `.env`:
   ```
   GMAIL_SENDER=your-email@gmail.com
   GMAIL_CREDENTIALS_PATH=gmail_credentials.json
   ```
5. Install the additional dependencies:
   ```bash
   pip install google-auth-oauthlib google-api-python-client
   ```

The first run will open a browser for OAuth consent. After that, a token is cached locally and reused.

## Command Reference

```
# Screen a resume
python screen.py <resume_path> <job_id> [--cover-letter PATH] [--references PATH]

# View all scores (grouped by position)
python screen.py --scores [job_id]

# View flagged applicants pending human review
python screen.py --review

# Approve or reject a flagged applicant
python screen.py --resolve <email> <job_id> <approved|rejected> <reviewer_name>
```
