#!/usr/bin/env python3
"""
setup.py — one-shot setup for sandbox_agents

Run immediately after cloning to:
  1. Verify prerequisites
  2. Create a Python virtualenv
  3. Install dependencies
  4. Create .env with your credentials
  5. Scaffold data directories
  6. Seed starter databases
  7. Render crontab with real paths
  8. Optionally install crontab

Usage:
    python setup.py                     # interactive
    python setup.py --help              # show all options
    python setup.py --yes --anthropic-api-key sk-ant-... --no-install-cron
"""

import argparse
import getpass
import os
import pathlib
import re
import shutil
import subprocess
import sys
import textwrap

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).resolve().parent

# This is set to True in main() when --yes / --non-interactive is passed.
# Helper functions read this flag rather than receiving it as a parameter
# so the call sites stay clean.
NON_INTERACTIVE = False


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def fatal(msg: str, code: int = 2) -> None:
    """Print an error message and exit."""
    print(f"[error] {msg}")
    sys.exit(code)


def info(msg: str) -> None:
    print(f"[ok] {msg}")


def warn(msg: str) -> None:
    print(f"[warn] {msg}")


def skip(msg: str) -> None:
    print(f"[skip] {msg}")


def confirm(prompt: str, default_yes: bool = True) -> bool:
    """
    Ask the user a yes/no question.

    In non-interactive mode, returns the default without prompting.
    default_yes=True  → default answer is Y (Enter = yes)
    default_yes=False → default answer is N (Enter = no)
    """
    if NON_INTERACTIVE:
        return default_yes
    hint = "[Y/n]" if default_yes else "[y/N]"
    while True:
        answer = input(f"{prompt} {hint} ").strip().lower()
        if answer == "":
            return default_yes
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer y or n.")


def prompt_secret(label: str) -> str:
    """
    Prompt for a secret value (masked input) and require a non-empty response.
    Re-prompts once on empty, then aborts.
    """
    for attempt in range(2):
        value = getpass.getpass(f"  {label}: ").strip()
        if value:
            return value
        if attempt == 0:
            print("  (Value cannot be empty — please try again.)")
    fatal(f"Required variable {label!r} cannot be empty. Aborting.", code=3)


def prompt_value(label: str, default: str = None) -> str:
    """
    Prompt for a plain-text value.  Re-prompts once on empty, then aborts.
    If `default` is provided, pressing Enter accepts the default.
    """
    hint = f" [{default}]" if default else ""
    for attempt in range(2):
        raw = input(f"  {label}{hint}: ").strip()
        if raw:
            return raw
        if default:
            return default
        if attempt == 0:
            print("  (Value cannot be empty — please try again.)")
    fatal(f"Required variable {label!r} cannot be empty. Aborting.", code=3)


def read_env_file(path: pathlib.Path) -> dict:
    """
    Parse a KEY=value env file into a dict.
    Skips blank lines and comment lines (starting with #).
    Does not handle quoted values or multi-line values.
    """
    result = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def run_step(label: str, cmd: list, cwd: pathlib.Path, env: dict) -> bool:
    """
    Run a subprocess, streaming its output directly to the terminal.
    Returns True on success, False on non-zero exit.
    """
    print(f"\n  > {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, cwd=str(cwd), env=env)
    if result.returncode != 0:
        print(f"[error] Step '{label}' failed (exit {result.returncode}). See output above.")
        return False
    return True


# ---------------------------------------------------------------------------
# Phase 1: Preflight
# ---------------------------------------------------------------------------

def check_preflight(repo_root: pathlib.Path) -> None:
    """Verify Python version, required repo files, and platform."""
    # Python version check
    if sys.version_info < (3, 9):
        v = sys.version_info
        fatal(
            f"Python 3.9+ required (found {v.major}.{v.minor}.{v.micro}). Exiting.",
            code=1,
        )

    # Repo sanity check
    required_files = [
        "requirements.txt",
        ".env.example",
        "crontab.txt.example",
        "logrotate.conf.example",
    ]
    for fname in required_files:
        if not (repo_root / fname).exists():
            fatal(
                f"Required file '{fname}' not found in {repo_root}. "
                "Is this the sandbox_agents repo root?",
                code=1,
            )

    # logrotate binary check (warn-only — operator may install later)
    if not shutil.which("logrotate"):
        warn(
            "logrotate not found on PATH. Log-rotation cron entry will fail until "
            "you install it (apt install logrotate / yum install logrotate)."
        )

    # Platform warning
    if sys.platform == "win32":
        warn(
            "Windows detected. This script is designed for Linux/macOS. "
            "Crontab installation will not work. Proceed with caution."
        )

    info("preflight checks passed.")


# ---------------------------------------------------------------------------
# Phase 2: Create virtualenv
# ---------------------------------------------------------------------------

def create_venv(venv_path: pathlib.Path, recreate: bool) -> None:
    """Create a virtualenv at venv_path, skipping if it already exists."""
    venv_python = venv_path / "bin" / "python"
    already_exists = venv_python.exists()

    if already_exists:
        if recreate:
            print(f"  Removing existing virtualenv at {venv_path} ...")
            shutil.rmtree(venv_path)
        else:
            if NON_INTERACTIVE:
                skip(f"venv already exists at {venv_path}. Use --recreate-venv to rebuild.")
                return
            else:
                if not confirm(f"Virtualenv already exists at {venv_path}. Recreate?", default_yes=False):
                    skip("venv left as-is.")
                    return
                print(f"  Removing existing virtualenv at {venv_path} ...")
                shutil.rmtree(venv_path)

    print(f"  Creating virtualenv at {venv_path} ...")
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr)
        fatal(
            "venv creation failed. Check that python3-venv is installed "
            "(apt install python3-venv on Debian/Ubuntu).",
            code=2,
        )
    info(f"virtualenv created at {venv_path}.")


# ---------------------------------------------------------------------------
# Phase 3: Install dependencies
# ---------------------------------------------------------------------------

def install_deps(venv_path: pathlib.Path, repo_root: pathlib.Path) -> None:
    """Run pip install -r requirements.txt inside the venv."""
    pip = venv_path / "bin" / "pip"
    if not pip.exists():
        fatal(
            f"pip not found at {pip}. The virtualenv may be incomplete. "
            "Try --recreate-venv.",
            code=2,
        )
    print("  Installing dependencies (this may take a minute) ...")
    result = subprocess.run(
        [str(pip), "install", "-r", "requirements.txt"],
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        fatal("pip install failed. See output above.", code=2)
    info("dependencies installed.")


# ---------------------------------------------------------------------------
# Phase 4: Create .env
# ---------------------------------------------------------------------------

def create_env(repo_root: pathlib.Path, args: argparse.Namespace) -> pathlib.Path:
    """
    Copy .env.example to .env, substituting real values.
    Returns the resolved SANDBOX_DATA_DIR path.
    """
    env_path = repo_root / ".env"

    if env_path.exists() and not args.overwrite_env:
        if NON_INTERACTIVE:
            skip(".env already exists. Pass --overwrite-env to replace it.")
        else:
            if not confirm(".env already exists. Overwrite?", default_yes=False):
                skip(".env left untouched.")
        # Still need to return the sandbox_data_dir from the existing file.
        existing = read_env_file(env_path)
        sdd = existing.get("SANDBOX_DATA_DIR", "")
        if sdd:
            return pathlib.Path(os.path.expanduser(sdd)).resolve()
        # Fall through to re-prompt if the existing .env has no SANDBOX_DATA_DIR
        warn(".env exists but SANDBOX_DATA_DIR is not set; will prompt for it now.")

    template = (repo_root / ".env.example").read_text()

    # --- Collect values ---

    # ANTHROPIC_API_KEY — always prompt even in --yes mode (no safe default)
    if args.anthropic_api_key:
        api_key = args.anthropic_api_key
    else:
        print("\n  Enter your Anthropic API key (input hidden).")
        api_key = prompt_secret("ANTHROPIC_API_KEY")

    # SANDBOX_DATA_DIR
    if args.sandbox_data_dir:
        sandbox_data_dir = pathlib.Path(os.path.expanduser(args.sandbox_data_dir)).resolve()
    else:
        default_sdd = str(pathlib.Path("~/sandbox-data").expanduser().resolve())
        if NON_INTERACTIVE:
            sandbox_data_dir = pathlib.Path(default_sdd)
        else:
            print(f"\n  Where should agent data (DBs, logs, reports) be stored?")
            raw = prompt_value("SANDBOX_DATA_DIR", default=default_sdd)
            sandbox_data_dir = pathlib.Path(os.path.expanduser(raw)).resolve()

    # EMAIL_ADDRESS
    if args.email_address:
        email = args.email_address
    elif NON_INTERACTIVE:
        # --yes without --email-address: use a placeholder but warn
        warn(
            "No --email-address provided; writing placeholder to .env. "
            "Update it before using email agents."
        )
        email = "<your-gmail-address>"
    else:
        print("\n  Gmail address for the email agents (can be filled in later).")
        email = prompt_value("EMAIL_ADDRESS", default="<your-gmail-address>")

    # APP_PASSWORD
    if args.app_password:
        app_password = args.app_password
    elif NON_INTERACTIVE:
        warn(
            "No --app-password provided; writing placeholder to .env. "
            "Update it before using email agents."
        )
        app_password = "<your-gmail-app-password>"
    else:
        print("\n  Gmail App Password (Settings > Security > App passwords).")
        app_password = prompt_secret("APP_PASSWORD")

    # --- Substitute placeholders ---
    content = template
    content = content.replace("<your-anthropic-api-key>", api_key)
    content = content.replace("<your-sandbox-data-dir>", str(sandbox_data_dir))
    content = content.replace("<your-gmail-address>", email)
    content = content.replace("<your-gmail-app-password>", app_password)

    env_path.write_text(content)
    info(".env written.")
    return sandbox_data_dir


# ---------------------------------------------------------------------------
# Phase 5: Create data directories
# ---------------------------------------------------------------------------

def create_data_dirs(sandbox_data_dir: pathlib.Path) -> None:
    """Create the full subdirectory tree under SANDBOX_DATA_DIR."""
    subdirs = [
        "dealership/surveys",
        "dealership/notifications",
        "dealership/service_followups",
        "hospital",
        "resume",
        "cust_rev",
        "fantnew/reports",
        "fantasy",
        "screenwrite/projects",
        "cron-logs",
        "health",
    ]
    try:
        for subdir in subdirs:
            (sandbox_data_dir / subdir).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        fatal(
            f"Could not create {e.filename}: {e.strerror}. "
            "Check that the parent directory exists and is writable.",
            code=2,
        )
    info(f"data directories created under {sandbox_data_dir}.")


# ---------------------------------------------------------------------------
# Phase 6: Seed databases
# ---------------------------------------------------------------------------

def seed_databases(
    repo_root: pathlib.Path,
    venv_path: pathlib.Path,
    sandbox_data_dir: pathlib.Path,
    skip_seed: bool,
) -> list:
    """
    Seed starter data.  Returns a list of step names that were skipped.
    """
    skipped = []

    if skip_seed:
        skip(
            "Seeding skipped. Run the following commands manually when ready:\n"
            + _seed_manual_commands(repo_root, venv_path, sandbox_data_dir)
        )
        return ["seeding (--skip-seed)"]

    # Cost prompt
    print(
        textwrap.dedent(
            f"""
            [?] Seed starter data? This will:
                - Generate 10 synthetic hospital patients (calls Claude ~10 times, ~$0.05-0.15)
                - Generate 5 fake lab locations (no API calls)
                - Scrape dealership inventory from 2 live websites (calls Claude, ~$0.10-0.30)
                - Copy fantasy_league.json template to your data directory
                Total estimated cost: $0.15-0.45 in Anthropic API credits.
            """
        ).rstrip()
    )

    if not confirm("Seed now?", default_yes=True):
        skip(
            "Seeding skipped. Run manually:\n"
            + _seed_manual_commands(repo_root, venv_path, sandbox_data_dir)
        )
        return ["seeding (user declined)"]

    # Build env for subprocesses — inject SANDBOX_DATA_DIR and any vars from .env
    seed_env = _build_seed_env(repo_root, sandbox_data_dir)
    venv_python = str(venv_path / "bin" / "python")

    # --- 6a. Hospital patients ---
    patients_db = sandbox_data_dir / "hospital" / "patients.db"
    if patients_db.exists():
        skip(f"patients.db already exists at {patients_db}; skipping hospital patient generation.")
        skipped.append("hospital patient generation (DB exists)")
    else:
        ok = run_step(
            "hospital patient generation",
            [venv_python, "hospital/patient_generator.py", "generate", "--count", "10", "--seed", "14"],
            cwd=repo_root,
            env=seed_env,
        )
        if not ok:
            warn("Hospital patient generation failed. You can re-run it manually later.")
            skipped.append("hospital patient generation (failed)")

    # --- 6b. Hospital lab locations ---
    ok = run_step(
        "hospital lab locations",
        [venv_python, "hospital/lab_scheduling_agent.py", "generate-labs", "--count", "5"],
        cwd=repo_root,
        env=seed_env,
    )
    if not ok:
        warn("Lab location generation failed. You can re-run it manually later.")
        skipped.append("hospital lab locations (failed)")

    # --- 6c. Dealership inventory ---
    inv_db = sandbox_data_dir / "dealership" / "inv.db"
    if inv_db.exists():
        skip(f"inv.db already exists at {inv_db}; skipping dealership scrape.")
        skipped.append("dealership inventory scrape (DB exists)")
    else:
        ok = run_step(
            "dealership inventory scrape",
            [
                venv_python,
                "dealership/dealership_agent.py",
                "smythevolvocars.com",
                "volvocarsprinceton.com",
                "--cache",
                str(inv_db),
            ],
            cwd=repo_root,
            env=seed_env,
        )
        if not ok:
            warn("Dealership inventory scrape failed. You can re-run it manually later.")
            skipped.append("dealership inventory scrape (failed)")

    # --- 6d. Fantasy league config copy ---
    fantnew_src = repo_root / "test-agents" / "fantnew" / "fantasy_league.json"
    fantnew_dst = sandbox_data_dir / "fantnew" / "fantasy_league.json"
    if fantnew_dst.exists():
        skip(f"fantasy_league.json already exists at {fantnew_dst}; skipping copy.")
        skipped.append("fantasy_league.json copy (already exists)")
    else:
        if fantnew_src.exists():
            shutil.copy2(str(fantnew_src), str(fantnew_dst))
            info(f"fantasy_league.json copied to {fantnew_dst}.")
        else:
            warn(f"Source not found: {fantnew_src}. Skipping fantasy config copy.")
            skipped.append("fantasy_league.json copy (source missing)")

    # Also copy the test-agents/fantasy/fantasy_league.json for completeness
    fantasy_src = repo_root / "test-agents" / "fantasy" / "fantasy_league.json"
    fantasy_dst = sandbox_data_dir / "fantasy" / "fantasy_league.json"
    if fantasy_dst.exists():
        skip(f"fantasy/fantasy_league.json already exists; skipping.")
    elif fantasy_src.exists():
        shutil.copy2(str(fantasy_src), str(fantasy_dst))
        info(f"fantasy/fantasy_league.json copied to {fantasy_dst}.")

    return skipped


def _build_seed_env(repo_root: pathlib.Path, sandbox_data_dir: pathlib.Path) -> dict:
    """
    Build an env dict for seed subprocesses.
    Starts from the current process env, overlays vars from .env, then
    explicitly sets SANDBOX_DATA_DIR to the value we collected interactively.
    """
    env = os.environ.copy()
    env_file = repo_root / ".env"
    if env_file.exists():
        for key, value in read_env_file(env_file).items():
            if key not in env:  # don't override already-exported vars
                env[key] = value
    # Always use the value we collected — it's authoritative for this run.
    env["SANDBOX_DATA_DIR"] = str(sandbox_data_dir)
    return env


def _seed_manual_commands(
    repo_root: pathlib.Path,
    venv_path: pathlib.Path,
    sandbox_data_dir: pathlib.Path,
) -> str:
    py = venv_path / "bin" / "python"
    inv_db = sandbox_data_dir / "dealership" / "inv.db"
    lines = [
        f"    {py} {repo_root}/hospital/patient_generator.py generate --count 10 --seed 14",
        f"    {py} {repo_root}/hospital/lab_scheduling_agent.py generate-labs --count 5",
        f"    {py} {repo_root}/dealership/dealership_agent.py smythevolvocars.com volvocarsprinceton.com --cache {inv_db}",
        f"    cp {repo_root}/test-agents/fantnew/fantasy_league.json {sandbox_data_dir}/fantnew/fantasy_league.json",
    ]
    return "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 7: Render crontab
# ---------------------------------------------------------------------------

def render_crontab(
    repo_root: pathlib.Path,
    venv_path: pathlib.Path,
    sandbox_data_dir: pathlib.Path,
) -> pathlib.Path:
    """Substitute placeholders in crontab.txt.example and write to SANDBOX_DATA_DIR/crontab.txt."""
    template_path = repo_root / "crontab.txt.example"
    content = template_path.read_text()

    # logrotate path: discovered or placeholder for the operator to fix later.
    logrotate_bin = shutil.which("logrotate") or "/usr/sbin/logrotate"

    content = content.replace("<repo-path>", str(repo_root))
    content = content.replace("<venv-path>", str(venv_path))
    content = content.replace("<logrotate-path>", logrotate_bin)
    # Replace the SANDBOX_DATA_DIR placeholder line in the crontab header
    content = re.sub(
        r"^SANDBOX_DATA_DIR=.*$",
        f"SANDBOX_DATA_DIR={sandbox_data_dir}",
        content,
        flags=re.MULTILINE,
    )

    crontab_path = sandbox_data_dir / "crontab.txt"
    try:
        crontab_path.write_text(content)
    except OSError as e:
        fatal(f"Could not write crontab to {crontab_path}: {e.strerror}", code=2)

    info(f"crontab rendered to {crontab_path}.")
    return crontab_path


def render_logrotate_conf(
    repo_root: pathlib.Path,
    sandbox_data_dir: pathlib.Path,
) -> pathlib.Path:
    """Substitute <sandbox-data-dir> in logrotate.conf.example and write to SANDBOX_DATA_DIR/logrotate.conf."""
    template_path = repo_root / "logrotate.conf.example"
    content = template_path.read_text()
    content = content.replace("<sandbox-data-dir>", str(sandbox_data_dir))

    conf_path = sandbox_data_dir / "logrotate.conf"
    try:
        conf_path.write_text(content)
    except OSError as e:
        fatal(f"Could not write logrotate.conf to {conf_path}: {e.strerror}", code=2)

    info(f"logrotate.conf rendered to {conf_path}.")
    return conf_path


# ---------------------------------------------------------------------------
# Phase 8: Install crontab
# ---------------------------------------------------------------------------

def install_crontab(crontab_path: pathlib.Path, args: argparse.Namespace) -> bool:
    """Optionally install the rendered crontab. Returns True if installed."""
    print(f"\n  Rendered crontab: {crontab_path}")
    print("  Note: This will OVERWRITE your current user crontab.")

    # Determine whether to install
    if args.no_install_cron:
        _print_cron_skip(crontab_path)
        return False

    if args.install_cron:
        do_install = True
    elif NON_INTERACTIVE:
        # --yes without --install-cron: skip (installing crontab is destructive)
        _print_cron_skip(crontab_path)
        return False
    else:
        do_install = confirm("Install this crontab now?", default_yes=True)

    if not do_install:
        _print_cron_skip(crontab_path)
        return False

    result = subprocess.run(["crontab", str(crontab_path)])
    if result.returncode != 0:
        fatal(
            "crontab command failed. Is cron installed? "
            "(apt install cron on Debian/Ubuntu)",
            code=2,
        )
    info("crontab installed.")
    return True


def _print_cron_skip(crontab_path: pathlib.Path) -> None:
    skip(
        f"Crontab not installed. To install later, run:\n"
        f"    crontab {crontab_path}"
    )


# ---------------------------------------------------------------------------
# Phase 9: Final summary
# ---------------------------------------------------------------------------

def print_summary(
    venv_path: pathlib.Path,
    sandbox_data_dir: pathlib.Path,
    crontab_path: pathlib.Path,
    seed_ran: bool,
    cron_installed: bool,
    skipped_steps: list,
) -> None:
    """Print a clear summary of what was done and what to do next."""
    venv_python = venv_path / "bin" / "python"
    summary = textwrap.dedent(
        f"""
        ============================================================
        Setup complete!
        ============================================================

        What was set up:
          Virtualenv      : {venv_path}
          Data directory  : {sandbox_data_dir}
          .env            : {REPO_ROOT / '.env'}
          Crontab file    : {crontab_path}
          Seeding ran     : {'yes' if seed_ran else 'no (skipped)'}
          Crontab active  : {'yes — cron jobs are now scheduled' if cron_installed else 'no — not yet installed'}

        Cron logs will appear in:
          {sandbox_data_dir}/cron-logs/<topic>.log

        Verify cron is running:
          crontab -l
          grep CRON /var/log/syslog          # Debian/Ubuntu with syslog
          journalctl -u cron                 # systemd systems

        Test an agent manually (example):
          {venv_python} {REPO_ROOT}/hospital/patient_generator.py query --limit 5

        MCP servers (interactive, not in crontab):
          The email, fantasy, code, and screenwriting agents are MCP servers
          that run interactively. See test-agents/README.md for setup.
        """
    )

    if skipped_steps:
        manual = "\n  Skipped steps (run manually when ready):"
        for step in skipped_steps:
            manual += f"\n    - {step}"
        summary += manual + "\n"

    if not cron_installed:
        summary += f"\n  Install crontab:\n    crontab {crontab_path}\n"

    summary += "\n============================================================\n"
    print(summary)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="setup.py",
        description=(
            "One-shot setup for sandbox_agents: "
            "venv, deps, .env, data dirs, seed, crontab."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Exit codes:
              0  success
              1  preflight failure (bad Python version, missing repo files)
              2  step failure (venv, pip, OS error, crontab)
              3  user aborted (empty required value)

            Examples:
              python setup.py
              python setup.py --yes --anthropic-api-key sk-ant-xxx --no-install-cron
              python setup.py --yes --anthropic-api-key sk-ant-xxx \\
                              --sandbox-data-dir /tmp/sandbox_test \\
                              --email-address test@example.com \\
                              --app-password testpass \\
                              --no-install-cron
            """
        ),
    )

    p.add_argument(
        "--yes", "--non-interactive",
        dest="yes",
        action="store_true",
        default=False,
        help="Skip all yes/no prompts; accept defaults. "
             "ANTHROPIC_API_KEY must still be supplied via --anthropic-api-key.",
    )
    p.add_argument(
        "--venv-path",
        dest="venv_path",
        metavar="PATH",
        default=None,
        help=f"Where to create the virtualenv (default: {REPO_ROOT / 'venv'})",
    )
    p.add_argument(
        "--recreate-venv",
        dest="recreate_venv",
        action="store_true",
        default=False,
        help="Delete and recreate the venv even if it already exists.",
    )
    p.add_argument(
        "--sandbox-data-dir",
        dest="sandbox_data_dir",
        metavar="PATH",
        default=None,
        help="SANDBOX_DATA_DIR value to write to .env (default: ~/sandbox-data).",
    )
    p.add_argument(
        "--anthropic-api-key",
        dest="anthropic_api_key",
        metavar="KEY",
        default=None,
        help="ANTHROPIC_API_KEY value; avoids interactive prompt.",
    )
    p.add_argument(
        "--email-address",
        dest="email_address",
        metavar="EMAIL",
        default=None,
        help="EMAIL_ADDRESS value for email agents.",
    )
    p.add_argument(
        "--app-password",
        dest="app_password",
        metavar="PASS",
        default=None,
        help="APP_PASSWORD (Gmail app password) value for email agents.",
    )
    p.add_argument(
        "--overwrite-env",
        dest="overwrite_env",
        action="store_true",
        default=False,
        help="Overwrite .env even if it already exists.",
    )
    p.add_argument(
        "--skip-seed",
        dest="skip_seed",
        action="store_true",
        default=False,
        help="Skip database seeding entirely.",
    )

    cron_group = p.add_mutually_exclusive_group()
    cron_group.add_argument(
        "--install-cron",
        dest="install_cron",
        action="store_true",
        default=False,
        help="Install crontab without asking.",
    )
    cron_group.add_argument(
        "--no-install-cron",
        dest="no_install_cron",
        action="store_true",
        default=False,
        help="Skip crontab installation without asking.",
    )

    return p


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def main() -> None:
    global NON_INTERACTIVE

    parser = build_parser()
    args = parser.parse_args()

    NON_INTERACTIVE = args.yes

    print("=" * 60)
    print("sandbox_agents setup")
    print("=" * 60)

    # Resolve paths early so all phases use consistent absolute paths.
    venv_path = (
        pathlib.Path(os.path.expanduser(args.venv_path)).resolve()
        if args.venv_path
        else REPO_ROOT / "venv"
    )

    # Phase 1: Preflight
    print("\n[phase 1] Preflight checks")
    check_preflight(REPO_ROOT)

    # Phase 2: Virtualenv
    print("\n[phase 2] Virtualenv")
    create_venv(venv_path, recreate=args.recreate_venv)

    # Phase 3: Dependencies
    print("\n[phase 3] Dependencies")
    install_deps(venv_path, REPO_ROOT)

    # Phase 4: .env
    print("\n[phase 4] Environment file (.env)")
    sandbox_data_dir = create_env(REPO_ROOT, args)

    # Phase 5: Data directories
    print("\n[phase 5] Data directories")
    create_data_dirs(sandbox_data_dir)

    # Phase 6: Seed databases
    print("\n[phase 6] Seed databases")
    skipped_steps = seed_databases(
        REPO_ROOT,
        venv_path,
        sandbox_data_dir,
        skip_seed=args.skip_seed,
    )
    seed_ran = not args.skip_seed and not any(
        "user declined" in s or "--skip-seed" in s for s in skipped_steps
    )

    # Phase 7: Render crontab + logrotate.conf
    print("\n[phase 7] Render crontab and logrotate.conf")
    crontab_path = render_crontab(REPO_ROOT, venv_path, sandbox_data_dir)
    render_logrotate_conf(REPO_ROOT, sandbox_data_dir)

    # Phase 8: Install crontab
    print("\n[phase 8] Install crontab")
    cron_installed = install_crontab(crontab_path, args)

    # Phase 9: Summary
    print_summary(
        venv_path=venv_path,
        sandbox_data_dir=sandbox_data_dir,
        crontab_path=crontab_path,
        seed_ran=seed_ran,
        cron_installed=cron_installed,
        skipped_steps=skipped_steps,
    )


if __name__ == "__main__":
    main()
