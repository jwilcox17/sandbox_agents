# Implementation Plan: Portability Refactor

**Type:** Infrastructure / Configuration
**Date:** 2026-05-21

## Goal

Make the `sandbox_agents` repo self-documenting and clone-and-runnable for any developer on any EC2 instance. This means eliminating hardcoded paths and credentials, standardizing dependency management and env loading, untracking regenerable binary artifacts, and providing the onboarding templates that currently exist only in the operator's head.

This plan is the portability prerequisite for a future automated setup script.

---

## Requirements

### Functional Requirements

- No agent reads credentials from a hardcoded string literal
- No agent writes to an absolute path that isn't derived from `SANDBOX_DATA_DIR`
- All agents load their `.env` with the same mechanism (`python-dotenv`)
- A single `requirements.txt` at the repo root covers every agent's dependencies
- Tracked `.db` files are removed from git history (regenerable via existing generator scripts)
- A properly encoded `.gitignore` actually ignores what it's supposed to
- `.env.example`, `crontab.txt.example`, and a root `README.md` give a new developer everything they need to get started

### Out-of-scope for this plan

- The future `setup.sh` / `bootstrap.sh` script
- Merging or choosing between `test-agents/fantasy/` and `test-agents/fantnew/` (flag only)
- Rationalizing the four `test-agents/email/` server variants (flag only)
- Standardizing hardcoded model version strings across agents
- Any business-logic changes beyond what is explicitly called out as a bug fix

### Credentials Required (for `.env.example`)

- `ANTHROPIC_API_KEY`
- `SANDBOX_DATA_DIR`
- `EMAIL_ADDRESS`
- `APP_PASSWORD`
- `ANTHROPIC_MODEL` (optional override)
- `FANTASY_BASEBALL_LOG_FILE` (optional override, used in `fantnew/fantasy_server.py`)
- `SCORING_MODEL` (optional override, used in `test-agents/resume/resume_screen.py`)
- `PATIENTS_DB` (optional override, hospital agents — legacy name kept for backward compat)

---

## Implementation Steps

---

### Phase 1: Security & Encoding Hotfixes

These are surgical, self-contained, and must land before anything else because they are either security risks or they block all git tooling from working correctly.

**1.1 — Fix `.gitignore` encoding**

- File: `.gitignore` (repo root)
- Problem: file is UTF-16 LE encoded; git's pattern matcher reads it as raw bytes so no patterns match.
- Change: resave the file as UTF-8 with identical content (no pattern changes yet — patterns are extended in Phase 5).
- Portability (no behavior change).
- Verify: `file .gitignore` should report `ASCII text` or `UTF-8 Unicode text`; `git check-ignore -v dealership/inv.db` should now return a match after Phase 5 adds the pattern.

**1.2 — Fix `other-agents/seo_agent/seo_agent.py` hardcoded API key**

- File: `other-agents/seo_agent/seo_agent.py`, line 34
- Problem: `ANTHROPIC_API_KEY = "REDACTED"` — the script passes this literal to `anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)` at line 38, so every API call fails.
- Change: replace the literal assignment with `ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")` and add an early exit if the key is empty (before the client is constructed), consistent with the pattern in `other-agents/cust_rev/review_agent.py:353-356`.
- Bug fix (functional: the agent currently cannot make any API calls).
- Verify: `python other-agents/seo_agent/seo_agent.py --help` should not raise an exception on import. Running with `ANTHROPIC_API_KEY` unset should print a clear error and exit non-zero.

**1.3 — Fix `other-agents/ai_ready/ai_readiness.py` hardcoded API key**

- File: `other-agents/ai_ready/ai_readiness.py`, line 52
- Problem: `ANTHROPIC_API_KEY = "REDACTED"` — the existing `_resolve()` helper at line 67 falls back to the env var only if the hardcoded string is empty. With `"REDACTED"` the env var is never reached.
- Change: replace with `ANTHROPIC_API_KEY = ""` so `_resolve()` falls through to `os.getenv("ANTHROPIC_API_KEY")` as intended. The existing error handling at line 509-516 is already correct.
- Bug fix (functional: the `_resolve` fallback is broken as long as the placeholder is non-empty).
- Verify: with `ANTHROPIC_API_KEY` unset, script should print its existing error message and exit 2. With key set, normal operation.

**1.4 — Remove hardcoded email credentials**

- Files and lines:
  - `test-agents/email/email_server.py:35-36`
  - `test-agents/email/email_summarizer_server.py:36-37`
  - `test-agents/email/email_mcp_server.py:41-42`
- Problem: real Gmail address and app password appear as `os.getenv(..., "<real-value>")` fallback defaults. Any clone of the repo exposes the credential; it also means the server will silently use the real account if `EMAIL_ADDRESS`/`APP_PASSWORD` are not set.
- Change: replace the fallback defaults with empty strings (`os.getenv("EMAIL_ADDRESS", "")` / `os.getenv("APP_PASSWORD", "")`). Add a startup check (before the server starts listening) that exits with a clear message if either is empty.
- Note: the user should rotate the Gmail app password regardless of this fix.
- `email_monitor_server.py:33-34` already uses placeholder defaults (`"YOUR_EMAIL"` / `"YOUR_APPPASS"`) and does not need the security fix, but should still be updated to the empty-string pattern for consistency.
- Security fix (functional: changes failure mode from silent credential leak to explicit startup error).
- Verify: starting any email server with `EMAIL_ADDRESS` and `APP_PASSWORD` unset should produce a clear error and exit. With env vars set, normal operation.

**1.5 — Fix `other-agents/cust_rev/review_agent.py` hardcoded API key**

- File: `other-agents/cust_rev/review_agent.py`, line 44
- Problem: `ANTHROPIC_API_KEY = "REDACTED"` appears as a module-level constant but is never actually used (the SDK reads `ANTHROPIC_API_KEY` from the environment automatically, as noted in the comment at line 358). The constant is dead code but looks alarming and could mislead a future reader into thinking the key is baked in.
- Change: delete the `ANTHROPIC_API_KEY = "REDACTED"` line entirely. The existing env-var check at lines 353-356 and the implicit SDK pickup are sufficient.
- Portability (removing dead/confusing code, no behavior change).
- Verify: `python other-agents/cust_rev/review_agent.py --list` should work without `ANTHROPIC_API_KEY` set (list reads DB only, no API call); running a create command without the key should hit the existing error check.

---

### Phase 2: Dependency Consolidation

**2.1 — Audit all third-party imports**

- Scan every `.py` file in the repo for non-stdlib imports. The full set found is:
  - `anthropic` — all agents using Claude
  - `anyio` — `test-agents/fantasy/fantasy_server.py`, `test-agents/fantnew/fantasy_server.py`
  - `beautifulsoup4` (import: `bs4`) — `other-agents/ai_ready/ai_readiness.py`
  - `curl-cffi` (import: `curl_cffi`) — `dealership/dealership_agent.py`
  - `faker` — `hospital/patient_generator.py`
  - `fastapi` — `test-agents/fantasy/fantasy_test_client.py` (test/dev only)
  - `feedparser` — `other-agents/game_news/gaming_news_agent.py`
  - `httpx` — `other-agents/ai_ready/ai_readiness.py`, `other-agents/seo_agent/seo_agent.py`, `test-agents/email/email_mcp_server.py`
  - `mcp[cli]` — all MCP agents
  - `pdfplumber` — `test-agents/resume/resume_screen.py`
  - `pydantic` — `other-agents/food_price/tools.py`, `test-agents/email/` servers
  - `python-docx` (import: `docx`) — `test-agents/resume/resume_screen.py`
  - `python-dotenv` (import: `dotenv`) — most agents
  - `requests` — `test-agents/fantasy/fantasy_tools.py`, `test-agents/fantnew/fantasy_tools.py`
  - `trafilatura` — `other-agents/seo_agent/seo_agent.py`
  - `uvicorn` — `test-agents/fantasy/fantasy_test_client.py` (test/dev only)

**2.2 — Create root `requirements.txt`**

- File: `requirements.txt` (repo root, new file)
- Content: one entry per package from the audit above, with loose lower-bound pins matching or exceeding those in `test-agents/resume/requirements.txt` where overlap exists.
- Add a comment block at the top noting that `fastapi` and `uvicorn` are test/dev-only and can be omitted for production deployments.
- Portability.

**2.3 — Consolidate `test-agents/resume/requirements.txt`**

- File: `test-agents/resume/requirements.txt`
- The packages it lists (`mcp[cli]`, `anthropic`, `pdfplumber`, `python-docx`, `python-dotenv`, `pydantic`) are all now in the root file.
- Decision: keep the file but replace its contents with a single comment: `# See root requirements.txt`. This preserves the file so any existing `pip install -r test-agents/resume/requirements.txt` invocations don't break with a "file not found" error; they just become a no-op.
- Portability.
- Verify: `pip install -r requirements.txt` in a fresh venv completes without errors; `python -c "import pdfplumber, docx, curl_cffi, trafilatura, faker, feedparser"` succeeds.

---

### Phase 3: Path Standardization on `SANDBOX_DATA_DIR`

All writeable output moves under `$SANDBOX_DATA_DIR/<subgroup>/`. The convention for subgroup names follows the existing directory structure: `dealership/`, `hospital/`, `fantasy/`, `fantnew/`, `screenwrite/`.

**Note on the `PATIENTS_DB` variable:** the hospital agents already support `PATIENTS_DB` as an env var override. After this phase, `PATIENTS_DB` should default to `$SANDBOX_DATA_DIR/hospital/patients.db` instead of the hardcoded fallbacks. The variable name is kept for backward compatibility with existing deployments that already set it.

---

**3.1 — `dealership/survey_reminder_agent.py`**

- Line 61: replace `SURVEYS_DIR = Path("/logs/surveys")` with:
  ```
  SURVEYS_DIR = Path(os.environ["SANDBOX_DATA_DIR"]) / "dealership" / "surveys"
  ```
- Add `import os` if not already present (it is present — line 43).
- Portability.
- Verify: run with `SANDBOX_DATA_DIR=/tmp/test_data` and `--cache dealership/inv.db --today 2026-01-01`; confirm survey output files appear under `/tmp/test_data/dealership/surveys/`.

**3.2 — `dealership/plate_notifier_agent.py`**

- Line 59: replace `NOTIFICATIONS_DIR = Path("/logs/notifications")` with:
  ```
  NOTIFICATIONS_DIR = Path(os.environ["SANDBOX_DATA_DIR"]) / "dealership" / "notifications"
  ```
- Portability.
- Verify: same pattern as 3.1, confirm output under `$SANDBOX_DATA_DIR/dealership/notifications/`.

**3.3 — `dealership/service_followup_agent.py`**

- Line 64: replace `FOLLOWUPS_DIR = Path("/logs/service_followups")` with:
  ```
  FOLLOWUPS_DIR = Path(os.environ["SANDBOX_DATA_DIR"]) / "dealership" / "service_followups"
  ```
- Line 65: `ISSUES_FILE = FOLLOWUPS_DIR / "issues_flagged.txt"` — no change needed; it already derives from `FOLLOWUPS_DIR`.
- Portability.
- Verify: same pattern, confirm output under `$SANDBOX_DATA_DIR/dealership/service_followups/`.

**3.4 — `dealership/dealership_agent.py` and `dealership/playfinder_agent.py`**

- These agents use `--cache` CLI arg to locate `inv.db`. No hardcoded paths. No changes required.
- Note for `crontab.txt.example`: cron invocations should pass `--cache $SANDBOX_DATA_DIR/dealership/inv.db`.

**3.5 — `hospital/patient_generator.py`**

- Line 41: `DB_PATH = os.environ.get("PATIENTS_DB", "/logs/patients.db")` — replace the default with `os.path.join(os.environ.get("SANDBOX_DATA_DIR", "/tmp"), "hospital", "patients.db")`.
- Full replacement:
  ```
  DB_PATH = os.environ.get(
      "PATIENTS_DB",
      os.path.join(os.environ.get("SANDBOX_DATA_DIR", ""), "hospital", "patients.db")
  )
  ```
- Leave `PATIENTS_DB` override in place so existing deployments that pin that variable still work.
- Portability.
- Verify: with `SANDBOX_DATA_DIR=/tmp/test_data` (and `PATIENTS_DB` unset), confirm DB is created at `/tmp/test_data/hospital/patients.db`.

**3.6 — `hospital/lab_scheduling_agent.py`**

- Line 43: `DB_PATH = os.environ.get("PATIENTS_DB", "patients.db")` — replace relative default with the same `SANDBOX_DATA_DIR`-derived path as 3.5.
- Portability.

**3.7 — `hospital/validation_reporter.py`**

- Line 45: same issue and same fix as 3.6.
- Portability.

**3.8 — `other-agents/cust_rev/review_agent.py`**

- Line 43: replace `DB_PATH = "/logs/reviews.db"` with:
  ```
  DB_PATH = os.path.join(os.environ.get("SANDBOX_DATA_DIR", ""), "cust_rev", "reviews.db")
  ```
- Add `import os` if not already present (it is — line 32 implicit via `anthropic` usage; actually confirm with a quick check — `os` is imported at line 30).
- Portability.
- Verify: with `SANDBOX_DATA_DIR=/tmp/test_data`, `python review_agent.py --list` should create/open DB at `/tmp/test_data/cust_rev/reviews.db`.

**3.9 — `test-agents/fantnew/fantasy_server.py`**

- Line 15: replace `DEFAULT_LOG_FILE = "/logs/fantasy_baseball_server.log"` with:
  ```
  DEFAULT_LOG_FILE = os.path.join(
      os.environ.get("SANDBOX_DATA_DIR", ""), "fantnew", "fantasy_baseball_server.log"
  )
  ```
- The existing `FANTASY_BASEBALL_LOG_FILE` env var override at line 662 already provides a per-deployment escape hatch; the default simply becomes sensible.
- Portability.
- Verify: start the server with `SANDBOX_DATA_DIR=/tmp/test_data`; confirm log appears at `/tmp/test_data/fantnew/fantasy_baseball_server.log`.

**3.10 — `test-agents/fantnew/daily_report.py`**

- Line 208: the default for `--output` is `BASE_DIR / "reports" / f"report_{...}.md"` (in-tree). Replace with:
  ```
  default=Path(os.environ.get("SANDBOX_DATA_DIR", str(BASE_DIR))) / "fantnew" / "reports" / f"report_{datetime.now().strftime('%Y%m%d')}.md"
  ```
- This means: if `SANDBOX_DATA_DIR` is set, reports go there; if not (local dev run), falls back to the script's own directory as before.
- Portability.
- Verify: with `SANDBOX_DATA_DIR=/tmp/test_data`, confirm report written to `/tmp/test_data/fantnew/reports/`.

**3.11 — `test-agents/resume/resume_screen.py`**

- Line 34: `DB_PATH = BASE_DIR / "data" / "applicants.db"` — replace with:
  ```
  DB_PATH = Path(os.environ.get("SANDBOX_DATA_DIR", str(BASE_DIR / "data"))) / "resume" / "applicants.db"
  ```
  Fallback: if `SANDBOX_DATA_DIR` is not set, the DB stays at `BASE_DIR/data/applicants.db` (current behavior). When set, it moves to `$SANDBOX_DATA_DIR/resume/applicants.db`.
- Portability.
- Verify: with `SANDBOX_DATA_DIR=/tmp/test_data`, confirm `--screen` or `--review` creates DB at `/tmp/test_data/resume/applicants.db`.

**3.12 — `test-agents/screenwrite/screenwriting_agent.py`**

- Line 75: `self.project_dir = Path(f"./projects/{project_name}")` — replace with:
  ```
  base = Path(os.environ.get("SANDBOX_DATA_DIR", ".")) / "screenwrite" / "projects"
  self.project_dir = base / project_name
  ```
- Portability.
- Verify: with `SANDBOX_DATA_DIR=/tmp/test_data` and a project name argument, confirm project files appear under `/tmp/test_data/screenwrite/projects/<project_name>/`.

**3.13 — `test-agents/fantasy/fantasy_server.py`**

- Line 16: `filename='fantasy_baseball_server.log'` (relative, inside `logging.basicConfig`).
- Replace with a path derived from `SANDBOX_DATA_DIR`:
  ```
  filename=os.path.join(os.environ.get("SANDBOX_DATA_DIR", "."), "fantasy", "fantasy_baseball_server.log")
  ```
- Portability.
- Note: `test-agents/fantnew/` is the actively used version; this change is applied to `fantasy/` for completeness, but see the follow-up section.

**3.14 — `test-agents/fantasy/fantasy_config.py` and `test-agents/fantnew/fantasy_config.py`**

- Both have `DEFAULT_CONFIG_PATH = "fantasy_league.json"` (relative).
- `fantasy_league.json` is a per-deployment config file (team names, league ID, etc.), not a regenerable artifact. It should live in `$SANDBOX_DATA_DIR/fantasy/fantasy_league.json` or `$SANDBOX_DATA_DIR/fantnew/fantasy_league.json` respectively.
- Change both files: replace `DEFAULT_CONFIG_PATH = "fantasy_league.json"` with:
  ```
  DEFAULT_CONFIG_PATH = os.path.join(
      os.environ.get("SANDBOX_DATA_DIR", "."), "<subgroup>", "fantasy_league.json"
  )
  ```
  where `<subgroup>` is `"fantasy"` for `fantasy/fantasy_config.py` and `"fantnew"` for `fantnew/fantasy_config.py`.
- Portability.
- Verify: set `SANDBOX_DATA_DIR=/tmp/test_data`, place a valid `fantasy_league.json` at the expected path, and confirm the server loads it correctly.

---

### Phase 4: Env Loading Standardization

**4.1 — Remove custom `.env` loaders from the three dealership agents**

- Files: `dealership/survey_reminder_agent.py`, `dealership/plate_notifier_agent.py`, `dealership/service_followup_agent.py`
- Problem: each contains a ~30-line hand-rolled `load_dotenv()` function (approximately lines 611-642, 520-551, 606-637 respectively). This is maintenance burden and behaves subtly differently from `python-dotenv`.
- Change: in each file, delete the hand-rolled `load_dotenv` function and its call in `main()`. Add `from dotenv import load_dotenv` to the imports and call `load_dotenv()` near the top of `main()` (before any env var reads), matching the pattern in `dealership/dealership_agent.py` which already uses `python-dotenv` cleanly. Confirm the "shell env wins" behavior is preserved (it is: `python-dotenv`'s default `override=False` gives the same precedence).
- Portability (behavior-equivalent; see note about precedence).
- Verify: place a `.env` with `ANTHROPIC_API_KEY=test` in the `dealership/` directory; run each agent with `--help` and confirm it starts without error. Then `export ANTHROPIC_API_KEY=other` and confirm the shell value wins.

**4.2 — Make `other-agents/food_price/agent.py` dotenv-optional**

- File: `other-agents/food_price/agent.py`, lines 16 and 20
- Problem: `from dotenv import load_dotenv` is an unconditional hard import; if `python-dotenv` is not installed the agent fails to import even if env vars are already set.
- Change: wrap in a try/except identical to the hospital pattern:
  ```python
  try:
      from dotenv import load_dotenv
      load_dotenv()
  except ImportError:
      pass
  ```
- Portability (after Phase 2, `python-dotenv` will always be in the venv; this change is defensive for dev environments that install a subset of deps).
- Verify: `python agent.py --help` works with and without `python-dotenv` installed.

**4.3 — Fix `other-agents/seo_agent/seo_agent.py` to load `.env`**

- File: `other-agents/seo_agent/seo_agent.py`
- The file already conditionally imports `load_dotenv` (lines 26-27) but this was for a different library. After Phase 1.2 fixes the API key read, ensure the file also calls `load_dotenv()` early (before the `ANTHROPIC_API_KEY` read) using the try/except optional pattern.
- Portability.
- Verify: place `ANTHROPIC_API_KEY=test` in a `.env` in `other-agents/seo_agent/`; confirm `python seo_agent.py --help` reads the key.

---

### Phase 5: Untrack Regenerable Artifacts & Fix `.gitignore`

**5.1 — Untrack database files**

- Files to untrack:
  - `dealership/inv.db`
  - `hospital/patients.db`
  - `test-agents/resume/data/applicants.db`
- Command: `git rm --cached dealership/inv.db hospital/patients.db test-agents/resume/data/applicants.db`
- This removes them from git's index (stops tracking) without deleting the files on disk. Existing deployments keep their data intact; new clones just won't have stale seed data.
- Portability.
- Verify: `git status` shows the files as untracked (not deleted). `git ls-files dealership/inv.db` returns nothing.

**5.2 — Update `.gitignore`**

- File: `.gitignore` (already saved as UTF-8 in Phase 1.1)
- Add patterns:
  ```
  # Regenerable databases
  *.db

  # Output data (in case SANDBOX_DATA_DIR is pointed inside the repo)
  /dealership/surveys/
  /dealership/notifications/
  /dealership/service_followups/
  /hospital/
  /cust_rev/reviews.db
  /fantnew/reports/
  /fantnew/fantasy_baseball_server.log
  /fantasy/fantasy_baseball_server.log
  /screenwrite/projects/
  /resume/

  # Common Python artifacts
  __pycache__/
  *.pyc
  *.pyo
  .venv/
  venv/
  *.egg-info/

  # Env files
  .env
  ```
- Note: `fantasy_league.json` is not ignored because it is a required deployment config (not regenerable). Developers should copy it from `.env.example` guidance in the README.
- Portability.
- Verify: `git check-ignore -v dealership/inv.db` returns a match. `git check-ignore -v .env` returns a match.

---

### Phase 6: Templates & Documentation

**6.1 — Create `.env.example`**

- File: `.env.example` (repo root, new file)
- Create the file with the exact content below. Every var has a one-line comment and a `<your-...>` placeholder; no real values appear anywhere.

```
# ── Required ─────────────────────────────────────────────────────────────────

# Anthropic API key — all agents that call Claude
ANTHROPIC_API_KEY=<your-anthropic-api-key>

# Absolute path to the writable data directory (logs, DBs, reports, etc.)
# Example: /home/agentdev/data  or  /tmp/sandbox_test
SANDBOX_DATA_DIR=<your-sandbox-data-dir>

# ── Email agents (test-agents/email/) ────────────────────────────────────────

# Gmail address used by the email MCP servers
EMAIL_ADDRESS=<your-gmail-address>

# Gmail app password (Settings > Security > App passwords — NOT your login password)
# IMAP must be enabled in Gmail settings.
APP_PASSWORD=<your-gmail-app-password>

# ── Optional overrides ────────────────────────────────────────────────────────

# Override the Claude model used by most agents (default varies per agent)
# Example: claude-sonnet-4-6  or  claude-opus-4-5
# ANTHROPIC_MODEL=<model-id>

# Override the model used for resume scoring (test-agents/resume/resume_screen.py)
# SCORING_MODEL=<model-id>

# Override the hospital DB path (hospital/ agents).
# Defaults to $SANDBOX_DATA_DIR/hospital/patients.db when SANDBOX_DATA_DIR is set.
# PATIENTS_DB=<absolute-path-to-patients.db>

# Override the fantasy baseball server log path (test-agents/fantnew/fantasy_server.py)
# Defaults to $SANDBOX_DATA_DIR/fantnew/fantasy_baseball_server.log
# FANTASY_BASEBALL_LOG_FILE=<absolute-path-to-log>
```

- Note on API key loading: scripts call `load_dotenv()` via `python-dotenv`, which loads the `.env` at or above the script's working directory. The crontab does NOT embed `ANTHROPIC_API_KEY` inline; all agents pick it up from the `.env` file. Shell environment variables take precedence over `.env` values (python-dotenv default behavior, `override=False`).
- Portability.
- Verify: file exists; `grep -v '^#' .env.example | grep -v '^$' | bash -n` passes (valid shell assignment syntax).

**6.2 — Create `crontab.txt.example`**

- File: `crontab.txt.example` (repo root, new file)
- This file represents the full production crontab after the portability refactor: 19 jobs, all paths parameterized, one consistent log convention, no embedded credentials.
- Create the file with the exact content below.

```
# ============================================================================
# sandbox_agents — example crontab
# ============================================================================
#
# HOW TO USE
# ----------
# 1. Copy this file somewhere and fill in <repo-path> and <venv-path>.
# 2. Install:  crontab /path/to/your-filled-in-crontab.txt
#
# PLACEHOLDER LEGEND
# ------------------
#   <repo-path>   — absolute path to the sandbox_agents repo checkout
#                   e.g. /home/agentdev/sandbox_agents
#   <venv-path>   — absolute path to the shared Python virtualenv
#                   e.g. /home/agentdev/venv
#
# ENV / CREDENTIALS
# -----------------
# Scripts load credentials via python-dotenv from a .env file in the repo root.
# Do NOT embed ANTHROPIC_API_KEY or other secrets here.
# SANDBOX_DATA_DIR must be set as a cron environment variable (see below) OR
# you can rely on it being exported from your shell profile — but cron does not
# source shell profiles, so the explicit assignment below is safest.
#
SANDBOX_DATA_DIR=/your/data/dir

# ============================================================================
# Resume screening
# ============================================================================

# Screen all PDFs in the resumes directory (runs daily at 15:54)
54 15 * * * for f in <repo-path>/test-agents/resume/resumes/*.pdf; do <venv-path>/bin/python -u <repo-path>/test-agents/resume/resume_screen.py "$f" senior_python_dev; done >> $SANDBOX_DATA_DIR/cron-logs/resume.log 2>&1

# Show flagged applicants pending review (15:56)
56 15 * * * <venv-path>/bin/python <repo-path>/test-agents/resume/resume_screen.py --review >> $SANDBOX_DATA_DIR/cron-logs/resume.log 2>&1

# Print scores for a job (15:57)
57 15 * * * <venv-path>/bin/python <repo-path>/test-agents/resume/resume_screen.py --scores senior_python_dev >> $SANDBOX_DATA_DIR/cron-logs/resume.log 2>&1

# ============================================================================
# Fantasy baseball
# ============================================================================

# Daily fantasy baseball report (16:30)
30 16 * * * <venv-path>/bin/python <repo-path>/test-agents/fantnew/daily_report.py >> $SANDBOX_DATA_DIR/cron-logs/ball_report.log 2>&1

# ============================================================================
# Customer reviews
# ============================================================================

# Generate a random review every 4 hours
0 */4 * * * <venv-path>/bin/python <repo-path>/other-agents/cust_rev/review_agent.py --random "Enterprise system for AI companies and developers called Aurite" >> $SANDBOX_DATA_DIR/cron-logs/customer.log 2>&1

# List the 5 most recent reviews nightly (01:55)
55 1 * * * <venv-path>/bin/python <repo-path>/other-agents/cust_rev/review_agent.py --list --last 5 >> $SANDBOX_DATA_DIR/cron-logs/customer.log 2>&1

# ============================================================================
# Gaming news
# ============================================================================

# Nightly gaming news digest (02:10)
10 2 * * * <venv-path>/bin/python <repo-path>/other-agents/game_news/gaming_news_agent.py >> $SANDBOX_DATA_DIR/cron-logs/game_news.log 2>&1

# ============================================================================
# AI readiness
# ============================================================================

# Nightly AI-readiness test against a live URL (23:12)
12 23 * * * <venv-path>/bin/python <repo-path>/other-agents/ai_ready/test_ai_readiness.py --live https://en.wikipedia.org/wiki/Mission:_Impossible_2 >> $SANDBOX_DATA_DIR/cron-logs/ai_ready.log 2>&1

# ============================================================================
# SEO agent
# ============================================================================

# Nightly SEO analysis (23:00)
0 23 * * * <venv-path>/bin/python <repo-path>/other-agents/seo_agent/seo_agent.py https://www.tankathon.com/ >> $SANDBOX_DATA_DIR/cron-logs/seo_agent.log 2>&1

# ============================================================================
# Dealership pipeline  (sequential; all share the same inv.db cache)
# ============================================================================

# 1. Scrape inventory (04:30)
30 4 * * * <venv-path>/bin/python <repo-path>/dealership/dealership_agent.py smythevolvocars.com volvocarsprinceton.com --cache $SANDBOX_DATA_DIR/dealership/inv.db >> $SANDBOX_DATA_DIR/cron-logs/dealership.log 2>&1

# 2. Find play-finder matches (04:35)
35 4 * * * <venv-path>/bin/python <repo-path>/dealership/playfinder_agent.py --cache $SANDBOX_DATA_DIR/dealership/inv.db >> $SANDBOX_DATA_DIR/cron-logs/dealership.log 2>&1

# 3. Plate notifications (05:00)
0  5 * * * <venv-path>/bin/python <repo-path>/dealership/plate_notifier_agent.py --cache $SANDBOX_DATA_DIR/dealership/inv.db >> $SANDBOX_DATA_DIR/cron-logs/dealership.log 2>&1

# 4. Service follow-ups (05:10)
10 5 * * * <venv-path>/bin/python <repo-path>/dealership/service_followup_agent.py --cache $SANDBOX_DATA_DIR/dealership/inv.db >> $SANDBOX_DATA_DIR/cron-logs/dealership.log 2>&1

# 5. Survey reminders (05:15)
15 5 * * * <venv-path>/bin/python <repo-path>/dealership/survey_reminder_agent.py --cache $SANDBOX_DATA_DIR/dealership/inv.db >> $SANDBOX_DATA_DIR/cron-logs/dealership.log 2>&1

# ============================================================================
# Hospital pipeline  (sequential)
# ============================================================================

# 1. Generate synthetic patients (06:00)
0  6 * * * <venv-path>/bin/python <repo-path>/hospital/patient_generator.py generate --count 10 --seed 14 >> $SANDBOX_DATA_DIR/cron-logs/hospital.log 2>&1

# 2. Generate lab locations (06:15)
15 6 * * * <venv-path>/bin/python <repo-path>/hospital/lab_scheduling_agent.py generate-labs --count 5 >> $SANDBOX_DATA_DIR/cron-logs/hospital.log 2>&1

# 3. Validate and report (06:18)
18 6 * * * <venv-path>/bin/python <repo-path>/hospital/validation_reporter.py validate >> $SANDBOX_DATA_DIR/cron-logs/hospital.log 2>&1

# ============================================================================
# Food price comparison
# ============================================================================

# Daily food price comparison (07:00)
0  7 * * * <venv-path>/bin/python <repo-path>/other-agents/food_price/agent.py 'https://www.grubhub.com/restaurant/starbird-chicken-1241-w-el-camino-real-sunnyvale/549122/menu-item/324278795776?menu-item-options=2_6_7_8_9_10' 'https://order.starbirdchicken.com/venue/?id=3212&order-type=6&time=IIZWAUg&screen=menu' >> $SANDBOX_DATA_DIR/cron-logs/food.log 2>&1

# ============================================================================
# Funder prospects
# ============================================================================

# Daily funder prospect search (07:30)
30 7 * * * <venv-path>/bin/python <repo-path>/other-agents/funder/prospect_finder.py "tech philanthropists supporting STEM in California" >> $SANDBOX_DATA_DIR/cron-logs/funder.log 2>&1
```

- Portability.
- Verify: file exists; every line is either a comment, blank, a cron env assignment, or a valid crontab entry (5 time fields followed by a command).

**6.3 — Create root `README.md`**

- File: `README.md` (repo root, new file)
- Content:
  1. What this repo is (one paragraph)
  2. Agent inventory: a table listing each agent, its subdirectory, entry point, and one-line description
  3. Setup instructions: clone, create venv, `pip install -r requirements.txt`, copy `.env.example` to `.env` and fill in values, set `SANDBOX_DATA_DIR`
  4. Running agents: reference `crontab.txt.example`; note that `SANDBOX_DATA_DIR` must be set and the directory must be writable before any agent runs
  5. Regenerating seed data: how to run `patient_generator.py generate --count N` and `dealership_agent.py --cache $SANDBOX_DATA_DIR/dealership/inv.db` to recreate the DBs from scratch on a fresh instance
  6. Notes on MCP servers: the `test-agents/` agents are MCP servers intended to be connected to a Claude Desktop or compatible client
  7. **Migrating an existing AWS instance** — a brief numbered checklist:
     - Set `SANDBOX_DATA_DIR` to the desired data root (e.g. `/home/agentdev/data`) and export it in the cron env assignment at the top of the crontab
     - Move existing data files: `mv /logs/inv.db $SANDBOX_DATA_DIR/dealership/inv.db`, `mv /logs/patients.db $SANDBOX_DATA_DIR/hospital/patients.db`, `mv /logs/reviews.db $SANDBOX_DATA_DIR/cust_rev/reviews.db`; create any subdirectories that don't exist yet (`mkdir -p $SANDBOX_DATA_DIR/{dealership,hospital,cust_rev,fantnew/reports,resume,cron-logs}`)
     - If the repo was cloned as `~/jon/` or any other name, no rename is required — `<repo-path>` in the crontab template is a placeholder for whatever the actual path is
     - Copy `.env.example` to `.env` in the repo root and fill in all required values; remove any `ANTHROPIC_API_KEY=...` line from the crontab (the scripts now load it from `.env`)
     - Install or confirm the shared venv has all deps: `pip install -r requirements.txt`
     - Populate `crontab.txt.example` with the real `<repo-path>` and `<venv-path>` values and install: `crontab /path/to/filled-in-crontab.txt`
- Portability.
- Verify: file exists and renders correctly as markdown.

---

## Testing Strategy

Each phase has inline verification steps. The overall integration test after all phases:

1. Create a fresh directory: `export SANDBOX_DATA_DIR=/tmp/sandbox_test && mkdir -p $SANDBOX_DATA_DIR`
2. Create a `.env` at the repo root with real values for `ANTHROPIC_API_KEY`, `SANDBOX_DATA_DIR`, `EMAIL_ADDRESS`, `APP_PASSWORD`
3. Create a fresh venv and `pip install -r requirements.txt`
4. Run one agent from each subgroup to confirm it reads the env, writes to the correct output path, and exits cleanly:
   - `python hospital/patient_generator.py --count 5` → DB appears at `$SANDBOX_DATA_DIR/hospital/patients.db`
   - `python hospital/validation_reporter.py` → reads from that DB, prints report
   - `python dealership/dealership_agent.py --cache $SANDBOX_DATA_DIR/dealership/inv.db` → creates the cache DB
   - `python dealership/survey_reminder_agent.py --cache $SANDBOX_DATA_DIR/dealership/inv.db --today 2026-01-01` → survey files in `$SANDBOX_DATA_DIR/dealership/surveys/`
   - `python other-agents/cust_rev/review_agent.py --list` → opens DB at `$SANDBOX_DATA_DIR/cust_rev/reviews.db`
   - `python test-agents/fantnew/daily_report.py` → report appears in `$SANDBOX_DATA_DIR/fantnew/reports/`
5. Confirm `git status` shows no tracked `.db` files and no unexpected staged changes.

---

## Follow-up / Out of Scope

**Duplicate fantasy directories**
`test-agents/fantasy/` and `test-agents/fantnew/` appear to be successive versions of the same MCP server. They have identical tool schemas and identical `fantasy_tools.py`/`fantasy_config.py` structures. A follow-up task should pick one as canonical, redirect cron entries, and delete the other. The plan does not make this choice.

**Four email server variants**
`test-agents/email/` contains `email_server.py`, `email_mcp_server.py`, `email_monitor_server.py`, and `email_summarizer_server.py`. It is unclear which, if any, are actively used in cron. A follow-up task should determine which are deployed and remove the dead variants.

**Hardcoded model version strings**
Several agents hardcode specific model version strings (e.g., `claude-sonnet-4-6`, `claude-opus-4-5-20251101`, `claude-sonnet-4-5-20250929`). The `ANTHROPIC_MODEL` / `SCORING_MODEL` env var pattern in `ai_readiness.py` and `resume_screen.py` is a good pattern worth spreading. Standardizing this is out of scope for portability but is a good follow-up.

**Future setup script**
This plan is the prerequisite. Once complete, a `setup.sh` can be written that: creates the venv, installs deps, prompts for env vars and writes `.env`, creates `SANDBOX_DATA_DIR`, and installs cron from `crontab.txt.example`.

---

## Changelog

- v1.0 (2026-05-21): Initial plan
- v1.1 (2026-05-21): Phase 6 expanded with concrete file contents for `.env.example` and `crontab.txt.example` (19 production jobs, parameterized paths, standardized `$SANDBOX_DATA_DIR/cron-logs/` log convention, no embedded API key). README outline updated with AWS migration checklist. Phase 3 spot-checked against production crontab: resume `--review` reads DB only (no new directories); hospital subcommands (`generate`, `generate-labs`, `validate`) confirmed against actual argparse wiring; `--cache` flag on all dealership jobs confirmed clean. Log path inconsistencies in the original crontab (mixed `/home/agentdev/logs/` and `/home/agentdev/myscript.log`) resolved to a single `$SANDBOX_DATA_DIR/cron-logs/<topic>.log` convention.
