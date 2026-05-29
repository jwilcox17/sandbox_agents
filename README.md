# sandbox_agents

A collection of Python agents that run on cron to simulate business operations and generate logs for testing. Most agents call the Claude API; a few are MCP servers intended for Claude Desktop. All writeable output (logs, DBs, reports) lives under a single `$SANDBOX_DATA_DIR` so nothing clutters the repo or system paths.

## Quick start

```bash
git clone <repo-url> sandbox_agents
cd sandbox_agents
python3 setup.py
```

`setup.py` does the whole bootstrap: creates a venv, installs dependencies, writes `.env` (prompts for `ANTHROPIC_API_KEY` and friends), creates `$SANDBOX_DATA_DIR`, seeds the patient / dealership / fantasy data, renders `crontab.txt` with your real paths, and offers to install it. Re-running is idempotent.

Unattended use:

```bash
python3 setup.py --yes \
  --anthropic-api-key "$ANTHROPIC_API_KEY" \
  --sandbox-data-dir /home/agentdev/data \
  --no-install-cron
```

`python3 setup.py --help` lists every flag.

## Agents

Cron-driven agents run on the schedule installed by `setup.py`. MCP servers (fantasy, email, screenwriting) are long-running and connect to Claude Desktop separately.

### `dealership/` — automotive sales pipeline

Five agents that share a single `inv.db` SQLite cache. `dealership_agent.py` scrapes first, the others read from the cache.

| Time | Agent | Description |
|------|-------|-------------|
| 04:30 | `dealership_agent.py` | Scrapes new-car inventory from Dealer.com / Team Velocity sites (`smythevolvocars.com`, `volvocarsprinceton.com` by default). Hits real APIs. |
| 04:35 | `playfinder_agent.py` | Reads `inv.db`, generates four sales-play types (test-drive offer, want-in-stock, cross-dealer transfer, trade-in match). Stdout only. |
| 05:00 | `plate_notifier_agent.py` | Simulates a license-plate-order notification cadence (0 / +3 / +7 / +14 / +21 days). Writes outgoing letters under `$SANDBOX_DATA_DIR/dealership/notifications/`. |
| 05:10 | `service_followup_agent.py` | Simulates post-service satisfaction touchpoints (+1 / +7 / +30 / +90 days) including synthetic customer responses. Flags issues to `issues_flagged.txt`. |
| 05:15 | `survey_reminder_agent.py` | Simulates a four-touch post-purchase survey cadence (+3 / +8 / +15 / +24 days). |

### `hospital/` — patient pipeline

Three agents that share `patients.db`.

| Time | Agent | Description |
|------|-------|-------------|
| 06:00 | `patient_generator.py generate` | Uses `faker` + Claude to create medically coherent synthetic patient records (10 per run by default). |
| 06:15 | `lab_scheduling_agent.py generate-labs` | Tool-use agent that assigns patients to lab locations and times. Deterministic, no Claude calls. |
| 06:18 | `validation_reporter.py validate` | Deterministic validation pass over the patient DB; Claude writes the human-readable email summary. |

### `other-agents/` — miscellaneous business sims

| Time | Agent | Description |
|------|-------|-------------|
| every 4h | `cust_rev/review_agent.py --random` | Tool-use agent that generates a synthetic customer review into `reviews.db` against a configured product context. |
| 01:55 | `cust_rev/review_agent.py --list --last 5` | Nightly summary of the last 5 reviews. |
| 02:10 | `game_news/gaming_news_agent.py` | Fetches and summarizes gaming news headlines. |
| 07:00 | `food_price/agent.py` | Tool-use agent that compares menu-item prices across two delivery URLs (e.g. Grubhub vs. restaurant direct). |
| 07:30 | `funder/prospect_finder.py` | Takes a natural-language search query (e.g. "tech philanthropists supporting STEM in California") and returns matching funder prospects. |
| 23:00 | `seo_agent/seo_agent.py` | Async SEO audit of a URL using `httpx` + `trafilatura` + Claude. |
| 23:12 | `ai_ready/test_ai_readiness.py --live` | Scores a URL for AI-readiness (readability, accessibility, content quality). |

### `test-agents/` — cron + MCP mix

| Time | Agent | Description |
|------|-------|-------------|
| 15:54 | `resume/resume_screen.py <pdf> <job>` | Loops over PDFs in `resumes/`, scores each against a job description using `pdfplumber` + Claude. |
| 15:56 | `resume/resume_screen.py --review` | Reviews the scoring run for consistency. |
| 15:57 | `resume/resume_screen.py --scores <job>` | Prints final scoring report. |
| 16:30 | `fantnew/daily_report.py` | Generates a daily fantasy-baseball markdown report into `$SANDBOX_DATA_DIR/fantnew/reports/`. |

**MCP servers** (not cron-driven — connect via Claude Desktop):

- `fantnew/fantasy_server.py` — fantasy baseball league management
- `email/email_*_server.py` — four Gmail IMAP server variants (full, summarizer, monitor, MCP wrapper)
- `screenwrite/screenwriting_agent.py` — interactive screenplay development
- `code/coding_server.py` — zero-dependency coding research server

## Running agents

After `setup.py`, the crontab schedules everything automatically. Logs land in `$SANDBOX_DATA_DIR/cron-logs/<topic>.log`.

Every cron entry is invoked through `bin/run_agent.py <name> <cmd...>`, a thin wrapper that streams stdout/stderr through to the log file unchanged and then writes a heartbeat to `$SANDBOX_DATA_DIR/health/<name>.json` containing the last start/finish time, duration, and exit code. To check the fleet at a glance:

```bash
ls -lt $SANDBOX_DATA_DIR/health/                            # most-recently-run first
jq -r '"\(.agent)\t\(.exit_code)\t\(.finished_at)"' \
   $SANDBOX_DATA_DIR/health/*.json                          # one line per agent
```

If a heartbeat file is older than its scheduled cadence the agent isn't running; if `exit_code` is non-zero it failed on its last run.

### Log retention

Two cron entries handle automatic cleanup so logs and artifacts don't grow forever:

- **00:05 daily — `logrotate`** rotates everything under `$SANDBOX_DATA_DIR/cron-logs/`. Config lives at `$SANDBOX_DATA_DIR/logrotate.conf` (rendered by `setup.py` from `logrotate.conf.example`). Keeps **7 days** of history per log, gzips older days. `copytruncate` mode means agents writing to the log when rotation happens don't lose data.
- **00:10 daily — artifact pruning** runs `find -mtime +30 -delete` against the output-artifact directories (`dealership/notifications/`, `dealership/surveys/`, `dealership/service_followups/`, `fantnew/reports/`). Anything older than **30 days** is removed.

Both jobs go through `run_agent.py` so they show up in the health-file fleet view. Their output goes to `$SANDBOX_DATA_DIR/cron-logs/_meta.log`, which is itself covered by the rotation glob.

To change retention, edit `$SANDBOX_DATA_DIR/logrotate.conf` (the `rotate 7` line) or the `-mtime +30` value in your crontab. No restart needed.

To run an agent manually:

```bash
source venv/bin/activate
python hospital/patient_generator.py generate --count 10 --seed 14
python dealership/dealership_agent.py smythevolvocars.com --cache $SANDBOX_DATA_DIR/dealership/inv.db
python test-agents/resume/resume_screen.py --review
```

Most agents support `--help`.

## MCP servers

The MCP servers under `test-agents/` (fantasy, email, screenwriting) are not cron-driven. Connect them to Claude Desktop by adding entries to `claude_desktop_config.json` pointing at `<venv-path>/bin/python <repo-path>/<server>.py`.

## Manual setup (if you skip setup.py)

1. `python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
2. `cp .env.example .env`, fill in `ANTHROPIC_API_KEY`, `SANDBOX_DATA_DIR`, `EMAIL_ADDRESS`, `APP_PASSWORD`
3. `mkdir -p $SANDBOX_DATA_DIR/{dealership,hospital,cust_rev,fantnew/reports,resume,cron-logs}`
4. Regenerate seed data:
   ```bash
   python hospital/patient_generator.py generate --count 10 --seed 14
   python dealership/dealership_agent.py smythevolvocars.com volvocarsprinceton.com \
     --cache $SANDBOX_DATA_DIR/dealership/inv.db
   ```
5. `cp crontab.txt.example my-crontab.txt`, substitute `<repo-path>` / `<venv-path>` / `SANDBOX_DATA_DIR`, then `crontab my-crontab.txt`

## Migrating an existing AWS instance

If you're upgrading an instance that used the old hardcoded `/logs/...` layout:

```bash
export SANDBOX_DATA_DIR=/home/agentdev/data
mkdir -p $SANDBOX_DATA_DIR/{dealership,hospital,cust_rev,fantnew/reports,resume,cron-logs}
mv /logs/inv.db      $SANDBOX_DATA_DIR/dealership/inv.db
mv /logs/patients.db $SANDBOX_DATA_DIR/hospital/patients.db
mv /logs/reviews.db  $SANDBOX_DATA_DIR/cust_rev/reviews.db
```

Then run `python3 setup.py` — it will skip the seeded DBs (idempotent), refresh deps, and re-render the crontab with the correct paths. Remove any `ANTHROPIC_API_KEY=...` line embedded in the old crontab; the scripts load it from `.env` now.
