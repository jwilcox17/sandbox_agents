# Implementation Plan: One-Shot Setup Script

**Type:** Developer Tooling
**Date:** 2026-05-21
**Author:** Agent Architect

---

## Goal

A single `setup.py` at the repo root that a developer can run immediately after cloning. It handles venv creation, dependency installation, `.env` generation, data directory scaffolding, database seeding, crontab rendering, and crontab installation — in that order — so the business simulation agents are running under cron with one command.

**In scope:** fresh-clone happy path on Linux/macOS, interactive-by-default with `--yes` for CI, all 9 ordered phases below.
**Out of scope:** existing-instance migration (README), Docker, subset agent selection, email/MCP server setup.

---

## Overview

The script does exactly these six things in order:

- Preflight: verify Python ≥ 3.9, confirm repo looks right
- Venv: create `./venv` (or a user-supplied path), skip if present
- Deps: `pip install -r requirements.txt` inside the venv
- Env: copy `.env.example` to `.env`, prompt for each required var, mask secrets
- Data dirs: `mkdir -p` the full `$SANDBOX_DATA_DIR` subdirectory tree
- Seed: run hospital and dealership generators; copy `fantasy_league.json` template
- Crontab: render `crontab.txt.example` with real paths; optionally install it

---

## CLI Surface

All flags mirror their corresponding interactive prompt so CI can pass everything on the command line.

| Flag | Default | What it does |
|------|---------|--------------|
| `--yes` / `--non-interactive` | off | Skip all yes/no prompts; accept defaults |
| `--venv-path PATH` | `<repo-root>/venv` | Where to create the virtualenv |
| `--recreate-venv` | off | Delete and recreate venv even if it exists |
| `--sandbox-data-dir PATH` | `~/sandbox-data` | Value to write for `SANDBOX_DATA_DIR` in `.env` |
| `--anthropic-api-key KEY` | (no default, always prompted) | `ANTHROPIC_API_KEY` value; avoids interactive prompt |
| `--email-address EMAIL` | (no default, always prompted) | `EMAIL_ADDRESS` value |
| `--app-password PASS` | (no default, always prompted) | `APP_PASSWORD` value |
| `--overwrite-env` | off | Overwrite `.env` even if it already exists |
| `--skip-seed` | off | Skip database seeding entirely |
| `--install-cron` | off | Install crontab without asking |
| `--no-install-cron` | off | Skip crontab install without asking |

When `--yes` is set without `--anthropic-api-key`, the script must still prompt for the key (it has no safe default). The script must never silently write an empty key.

Exit codes: `0` success, `1` preflight failure, `2` install/step failure, `3` user aborted.

---

## Phase-by-Phase Breakdown

---

### Phase 1: Preflight Checks

**What it does:** Verify the environment is ready before touching anything.

**Checks to run:**
1. Python version: `sys.version_info >= (3, 9)`. If not, print `[error] Python 3.9+ required (found X.Y.Z). Exiting.` and `sys.exit(1)`.
2. Repo sanity: verify that `requirements.txt`, `.env.example`, and `crontab.txt.example` all exist relative to the script's own directory. If any are missing, the repo is in a bad state and the script cannot continue safely.
3. Platform check: warn (don't abort) if `sys.platform == 'win32'`.

**Idempotency:** read-only checks, always safe to re-run.

**Failure mode:** clear message naming the missing file or bad Python version, exit 1.

**Required:** yes — non-skippable.

---

### Phase 2: Create Virtualenv

**What it does:** Create a Python virtualenv at `--venv-path` (default: `<repo-root>/venv`) using the stdlib `venv` module.

**Logic:**
- If the venv already exists (test: `os.path.exists(venv_path / "bin" / "python")`):
  - In non-interactive mode with `--recreate-venv`: delete and recreate.
  - In non-interactive mode without `--recreate-venv`: `[skip] venv already exists at <path>. Use --recreate-venv to rebuild.` and continue.
  - In interactive mode: ask "Virtualenv already exists at <path>. Recreate? [y/N]". On N, skip.
- On create: `python -m venv <venv_path>` via `subprocess.run`. Capture stderr; if it fails, print the error and `sys.exit(2)`.
- Print `[ok] virtualenv created at <path>`.

**Idempotency:** skip without touching if venv exists and `--recreate-venv` not set.

**Failure mode:** `[error] venv creation failed. Check that python3-venv is installed (apt install python3-venv on Debian/Ubuntu).` then exit 2.

**Required:** yes.

---

### Phase 3: Install Dependencies

**What it does:** Run `<venv>/bin/pip install -r requirements.txt` from the repo root.

**Logic:**
- Build the pip command as an absolute path: `venv_path / "bin" / "pip"`.
- Run with `subprocess.run([pip, "install", "-r", "requirements.txt"], cwd=repo_root)`. Stream stdout/stderr directly (do not capture) so the user sees progress.
- If return code non-zero: `[error] pip install failed. See output above.` and exit 2.
- Print `[ok] dependencies installed.`

**Idempotency:** pip install is naturally idempotent (already-installed packages are skipped).

**Failure mode:** pip itself will explain what went wrong; the script just needs to relay the exit code.

**Required:** yes.

---

### Phase 4: Create `.env`

**What it does:** Copy `.env.example` to `.env` in the repo root, then substitute real values for each required variable.

**Logic:**
1. If `.env` already exists and `--overwrite-env` is not set:
   - Interactive: ask "`.env` already exists. Overwrite? [y/N]". On N: `[skip] .env left untouched.` and continue (do not abort — the rest of the script can still run).
   - Non-interactive: `[skip] .env already exists. Pass --overwrite-env to replace it.` and continue.
2. Read `.env.example` into memory with `pathlib.Path.read_text()`.
3. Prompt for each required var. Use `getpass.getpass()` for `ANTHROPIC_API_KEY` and `APP_PASSWORD`; use regular `input()` for `EMAIL_ADDRESS` and `SANDBOX_DATA_DIR`.
4. For `SANDBOX_DATA_DIR`, show the default (`~/sandbox-data` expanded to the real path via `os path.expanduser`) and accept Enter to use it.
5. Substitute each `<placeholder>` in the `.env.example` text with the collected value. The four substitutions:
   - `<your-anthropic-api-key>` → collected key
   - `<your-sandbox-data-dir>` → collected path (fully expanded)
   - `<your-gmail-address>` → collected email
   - `<your-gmail-app-password>` → collected password
6. Write the result to `.env` with `pathlib.Path.write_text()`.
7. Print `[ok] .env written.`

**Implementation note:** The `SANDBOX_DATA_DIR` value collected here is also needed for all subsequent phases. Store it as a variable in memory so later phases don't have to re-read `.env`.

**Masking:** Never echo or print secret values. The `getpass` prompts will mask terminal input. In `--yes` / `--non-interactive` mode, values must be supplied via flags; if `ANTHROPIC_API_KEY` is not supplied via `--anthropic-api-key` and `--yes` is set, still prompt for it (there is no safe default).

**Idempotency:** skip by default if `.env` exists; only overwrite on explicit request.

**Failure mode:** if a required var is left empty after prompting (user hit Enter on a required field), re-prompt once more, then abort with `[error] Required variable <VAR> cannot be empty. Aborting.` exit 3.

**Required:** yes (skippable for re-runs when `.env` already exists and is correct).

---

### Phase 5: Create Data Directory Structure

**What it does:** Create the full subdirectory tree under `SANDBOX_DATA_DIR`.

**Directories to create** (all via `pathlib.Path.mkdir(parents=True, exist_ok=True)`):
```
$SANDBOX_DATA_DIR/
  dealership/
    surveys/
    notifications/
    service_followups/
  hospital/
  resume/
  cust_rev/
  fantnew/
    reports/
  fantasy/
  screenwrite/
    projects/
  cron-logs/
```

**Logic:** loop over the list, call `mkdir(parents=True, exist_ok=True)` on each. This is inherently idempotent. Print `[ok] data directories created under <SANDBOX_DATA_DIR>`.

**Idempotency:** `exist_ok=True` means re-running is always safe.

**Failure mode:** `[error] Could not create <path>: <OS error>. Check that the parent directory exists and is writable.` exit 2.

**Required:** yes.

---

### Phase 6: Seed Databases

**What it does:** Populate starter data so the cron jobs have something to work with on first run.

**What to seed:**

**6a. Hospital — patients DB**
- Command: `<venv>/bin/python hospital/patient_generator.py generate --count 10 --seed 14`
- Run from `repo_root` with `SANDBOX_DATA_DIR` explicitly passed in `env=` (see note below).
- This calls Claude ~10 times. Cost estimate: roughly $0.05–0.15 at Sonnet prices.

**6b. Hospital — lab locations**
- Command: `<venv>/bin/python hospital/lab_scheduling_agent.py generate-labs --count 5`
- Same env; run after 6a (labs reference the same DB).
- Does not call Claude (pure faker generation). No additional cost.

**6c. Dealership — inventory DB**
- Command: `<venv>/bin/python dealership/dealership_agent.py smythevolvocars.com volvocarsprinceton.com --cache $SANDBOX_DATA_DIR/dealership/inv.db`
- This scrapes two live HTTP endpoints (smythevolvocars.com, volvocarsprinceton.com) and calls Claude to process inventory. Cost estimate: roughly $0.10–0.30 depending on inventory size.
- Note: these are real public dealership websites. The scrape is read-only and no account is needed.

**6d. Fantasy — copy league config**
- Source: `<repo_root>/test-agents/fantnew/fantasy_league.json`
- Destination: `$SANDBOX_DATA_DIR/fantnew/fantasy_league.json`
- This is not a Claude call — just a file copy. The `fantnew/fantasy_config.py` now reads from `$SANDBOX_DATA_DIR/fantnew/fantasy_league.json` (confirmed post-portability-refactor). If the destination already exists, skip.
- The `test-agents/fantasy/fantasy_league.json` also exists (identical content) but the cron only uses `fantnew/daily_report.py`, so only the fantnew copy needs seeding.

**Cost prompt (shown before any seeding):**
```
[?] Seed starter data? This will:
    - Generate 10 synthetic hospital patients (calls Claude ~10 times, ~$0.05-0.15)
    - Generate 5 fake lab locations (no API calls)
    - Scrape dealership inventory from 2 live websites (calls Claude, ~$0.10-0.30)
    - Copy fantasy_league.json template to your data directory
    Total estimated cost: $0.15–0.45 in Anthropic API credits.
    Seed now? [Y/n]
```
On N (or `--skip-seed`): `[skip] Seeding skipped. Run <commands> manually when ready.` and print the manual commands.

**Environment propagation for subprocess calls:**
Build an explicit `env` dict for each subprocess:
```python
seed_env = os.environ.copy()
seed_env["SANDBOX_DATA_DIR"] = str(sandbox_data_dir)
# ANTHROPIC_API_KEY will already be in the process env if user exported it,
# but since the .env hasn't been sourced, read it back from the .env file we wrote.
```
The script should read `.env` back after writing it (simple line-by-line parse for `KEY=value` lines, skip comments) and inject any missing vars into `seed_env`. This avoids requiring the user to re-source their shell.

**Failure mode:** if any seed subprocess returns non-zero, print `[error] Seeding step "<name>" failed. See output above. You can re-run seeding manually later.` Do not abort the whole setup — log the failure and continue to crontab rendering. The user can seed manually.

**Idempotency:** Patient generator and `generate-labs` insert rows; re-running adds duplicates. The script should skip seeding if `$SANDBOX_DATA_DIR/hospital/patients.db` already exists (check before prompting). Similarly skip dealership seeding if `$SANDBOX_DATA_DIR/dealership/inv.db` already exists. Skip fantasy copy if destination already exists. This makes re-runs safe.

**Required:** optional (default yes; `--skip-seed` to bypass).

---

### Phase 7: Render Crontab

**What it does:** Read `crontab.txt.example`, substitute the two placeholders, and write the result to `$SANDBOX_DATA_DIR/crontab.txt`.

**Substitutions:**
- `<repo-path>` → `str(repo_root)` (absolute path of the repo, derived from `os.path.abspath(__file__).parent`)
- `<venv-path>` → `str(venv_path)` (absolute path of the venv)
- The `SANDBOX_DATA_DIR=/your/data/dir` line in the example → `SANDBOX_DATA_DIR=<actual sandbox_data_dir>`

**Use `re.sub` or simple `str.replace`** — the placeholders are fixed strings, no regex needed.

**Write** result to `sandbox_data_dir / "crontab.txt"`. This keeps the rendered file outside the repo (no risk of accidentally committing it with real paths).

Print `[ok] crontab rendered to <path>.`

**Idempotency:** always overwrite (the rendered crontab is derived from current paths; overwriting is always correct).

**Failure mode:** `[error] Could not write crontab to <path>.` exit 2.

**Required:** yes (always runs; it's just a text substitution — cheap and safe).

---

### Phase 8: Install Crontab

**What it does:** Optionally run `crontab <path-to-rendered-crontab.txt>`.

**Logic:**
- Show the user the rendered crontab file path and a note: "This will overwrite your current user crontab."
- Interactive: ask "Install this crontab now? [Y/n]"
- `--install-cron`: proceed without asking
- `--no-install-cron`: skip without asking
- Default non-interactive (`--yes` without `--install-cron`/`--no-install-cron`): skip (installing crontab is destructive; non-interactive mode should not overwrite crontab unless explicitly told to).

On yes: `subprocess.run(["crontab", str(crontab_path)])`. Check return code.

On no/skip: print the manual install command:
```
[skip] To install crontab later, run:
    crontab <path-to-crontab.txt>
```

**Idempotency:** `crontab` command is idempotent — running it twice with the same file produces the same result.

**Failure mode:** `[error] crontab command failed. Is cron installed? (apt install cron on Debian/Ubuntu)` exit 2.

**Required:** optional (default yes in interactive mode; skippable).

---

### Phase 9: Final Summary

**What it does:** Print a clear success block telling the user what was done and what to do next. Always runs, even if optional steps were skipped.

**Contents of summary:**
1. What was set up (venv path, data dir, whether seeding ran, whether cron was installed)
2. Where cron logs will appear: `$SANDBOX_DATA_DIR/cron-logs/<topic>.log`
3. How to verify cron is running: `crontab -l` and `grep CRON /var/log/syslog` (or `journalctl -u cron` on systemd systems)
4. How to test an agent manually (example command using venv python)
5. Any steps that were skipped with the manual commands to run them
6. Note about MCP servers: "The email, fantasy server, code, and screenwriting agents are MCP servers that run interactively; they are not in the crontab. See test-agents/README.md for setup."

---

## Code Structure

The script lives at `<repo-root>/setup.py`. It is a single flat script with no classes. Suggested function layout:

```
main()                    — orchestrates all phases; parses args; calls phase functions in order
check_preflight(repo_root)
create_venv(venv_path, recreate)
install_deps(venv_path, repo_root)
create_env(repo_root, args)  → returns sandbox_data_dir (str/Path)
create_data_dirs(sandbox_data_dir)
seed_databases(repo_root, venv_path, sandbox_data_dir, env_vars)
render_crontab(repo_root, venv_path, sandbox_data_dir)  → returns crontab_path
install_crontab(crontab_path, args)
print_summary(venv_path, sandbox_data_dir, skipped_steps)

# Helpers
confirm(prompt, default_yes=True)   — single yes/no prompt respecting --yes flag
prompt_secret(prompt)               — wraps getpass, re-prompts on empty
prompt_value(prompt, default=None)  — wraps input with default display
read_env_file(path)                 — parses KEY=value lines, returns dict
run_step(label, cmd, cwd, env)      — runs subprocess, streams output, returns success bool
fatal(msg, code=2)                  — prints [error] msg, sys.exit(code)
```

`confirm()` and the prompt helpers should respect a module-level `NON_INTERACTIVE` flag set from args in `main()`.

The `repo_root` is derived once at module level: `REPO_ROOT = pathlib.Path(__file__).resolve().parent`. This is the only global state needed.

---

## Spot-Check Items for Implementer

These must be verified against the actual repo before finalizing the seeding commands. Most are confirmed from the spot-check done during planning, but the implementer should re-confirm before writing the subprocess calls.

1. **`patient_generator.py generate --count 10 --seed 14`** — confirmed. The `generate` subcommand exists, `--count` is required, `--seed` is optional. `DB_PATH` already reads from `SANDBOX_DATA_DIR` post-portability-refactor (confirmed at line 41-43).

2. **`lab_scheduling_agent.py generate-labs --count 5`** — confirmed. The `generate-labs` subcommand exists (line 859). Uses the same `DB_PATH` as patient_generator (same `SANDBOX_DATA_DIR`-derived default). No Claude calls; faker only.

3. **`dealership_agent.py smythevolvocars.com volvocarsprinceton.com --cache $SANDBOX_DATA_DIR/dealership/inv.db`** — confirmed. `domains` is a positional nargs argument (line 940-944); `--cache` is accepted (line 952). The seeding command exactly matches the production cron command, which is correct.

4. **`fantasy_league.json` template** — confirmed. `test-agents/fantnew/fantasy_league.json` exists in the repo and contains a valid 2-team config. `fantasy_config.py` now reads from `$SANDBOX_DATA_DIR/fantnew/fantasy_league.json` (confirmed post-refactor, line 6-8). The copy destination is `$SANDBOX_DATA_DIR/fantnew/fantasy_league.json`. Also copy `test-agents/fantasy/fantasy_league.json` to `$SANDBOX_DATA_DIR/fantasy/fantasy_league.json` for completeness (that server is not in cron but the data dir exists).

5. **Resume PDFs** — confirmed at `test-agents/resume/resumes/`: `communications_resume25.pdf`, `engineering_resume25.pdf`, `functionalsample.pdf`. The crontab uses `<repo-path>/test-agents/resume/resumes/*.pdf` as an absolute glob, so no setup action is required — the files are in-repo and the rendered crontab will have the real absolute path.

6. **`resume_screen.py` cron loop** — the cron entry runs a `for` loop over the glob inline. This is a shell construct inside the crontab. The rendered `crontab.txt` will have the real `<repo-path>`, so no additional setup is needed.

7. **`daily_report.py` output path** — confirmed. Line 208 of `daily_report.py` already uses `SANDBOX_DATA_DIR / "fantnew" / "reports"` as the default output path. No setup action beyond creating the directory (handled in Phase 5).

8. **`food_price/agent.py` CLI** — confirmed. This agent takes URL strings as positional `sys.argv` args (not argparse subcommands). The two URLs in `crontab.txt.example` are real Starbirdchicken/Grubhub menu item pages. No substitution needed in the crontab template; they carry forward as-is.

9. **`funder/prospect_finder.py` CLI** — confirmed. Takes a positional `query` string. The search query in the crontab ("tech philanthropists supporting STEM in California") carries forward as-is.

10. **`SANDBOX_DATA_DIR` is NOT sourced from `.env` at seed time** — the script must inject it explicitly into the subprocess `env=` dict. Confirmed: all three hospital agents read `SANDBOX_DATA_DIR` from `os.environ.get(...)` at module load, so it must be present in the subprocess environment at import time.

11. **`cust_rev/reviews.db` initial state** — `review_agent.py --list` will create the DB on first access. No seeding needed; the cron job will start generating reviews on its next run. No seed step required.

12. **`validation_reporter.py validate`** — this subcommand reads from the patients DB (it does not generate data). Running it as a seed step would work but would just print a report to stdout and exit. It is already in the cron schedule. It does NOT need to be run during setup — patient_generator.py seeding is sufficient.

---

## Test Plan

**Phase 1 (preflight):**
- Run `python setup.py` from outside the repo directory → should fail with "requirements.txt not found".
- Run with Python 3.8 → should fail with Python version error (test with `python3.8 setup.py` if available, or mock `sys.version_info`).

**Phase 2 (venv):**
- Fresh run: confirm `venv/bin/python` exists after setup.
- Re-run: confirm `[skip] venv already exists` appears without error.
- `--recreate-venv`: confirm venv is recreated (timestamp on `venv/` changes).

**Phase 3 (deps):**
- After setup, `venv/bin/python -c "import anthropic, pdfplumber, curl_cffi, faker, feedparser"` should succeed.

**Phase 4 (.env):**
- Fresh run: confirm `.env` is created with real values substituted (no `<placeholder>` strings remain).
- Re-run without `--overwrite-env`: confirm `[skip] .env left untouched` and `.env` unchanged.
- Re-run with `--overwrite-env`: confirm `.env` is rewritten.
- Run with empty input on `ANTHROPIC_API_KEY`: confirm re-prompt and eventual abort.

**Phase 5 (dirs):**
- After setup, `ls $SANDBOX_DATA_DIR` should show all expected subdirs.
- Re-run: no errors (exist_ok=True).

**Phase 6 (seeding):**
- After setup, `$SANDBOX_DATA_DIR/hospital/patients.db` should exist and contain rows: `venv/bin/python hospital/patient_generator.py query --limit 5`.
- `$SANDBOX_DATA_DIR/dealership/inv.db` should exist.
- `$SANDBOX_DATA_DIR/fantnew/fantasy_league.json` should exist and be valid JSON.
- Re-run without `--skip-seed`: confirm `[skip] patients.db already exists` (no duplicate generation).
- `--skip-seed`: confirm seeding is skipped entirely and manual commands are printed.

**Phase 7 (render crontab):**
- `$SANDBOX_DATA_DIR/crontab.txt` should exist after setup.
- Confirm no `<repo-path>` or `<venv-path>` literals remain: `grep '<repo-path>\|<venv-path>\|/your/data' $SANDBOX_DATA_DIR/crontab.txt` should return nothing.
- Confirm the venv python path in the crontab actually exists on disk.

**Phase 8 (install crontab):**
- Run with `--install-cron`: confirm `crontab -l` shows the expected entries.
- Run with `--no-install-cron`: confirm crontab is not modified and manual command is printed.
- Run interactively and answer N: same result as `--no-install-cron`.

**End-to-end non-interactive test:**
```
python setup.py \
  --yes \
  --anthropic-api-key "$ANTHROPIC_API_KEY" \
  --sandbox-data-dir /tmp/sandbox_test \
  --email-address test@example.com \
  --app-password testpass \
  --no-install-cron
```
Should complete exit 0, create all dirs, `.env`, crontab, and seed data.

---

## Follow-Ups / Out of Scope

- **Healthcheck script** — a separate `healthcheck.py` that verifies cron is running, recent log files exist, and the DBs have recent entries. Useful for monitoring.
- **Docker image** — a `Dockerfile` + `docker-compose.yml` for containerized deployment.
- **Agent subset selection** — `--agents hospital,dealership` to run only a subset of cron jobs.
- **Multi-tenancy** — multiple `SANDBOX_DATA_DIR` instances on the same host.
- **Existing instance migration** — covered by the README AWS migration checklist; no scripting planned.
- **MCP server management** — `fantasy_server.py`, `email_*_server.py`, `coding_server.py`, `screenwriting_agent.py` are all interactive MCP servers. A future `start_mcp_servers.py` script could manage them as background processes.

---

## Changelog

- v1.0 (2026-05-21): Initial plan
