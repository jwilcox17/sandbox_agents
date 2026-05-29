#!/usr/bin/env python3
"""
run_agent.py — cron wrapper that records a heartbeat for the wrapped command.

Writes $SANDBOX_DATA_DIR/health/<name>.json after the command finishes,
containing the last start/finish time, duration, and exit code. The wrapped
command's stdout/stderr pass through unchanged, so existing cron log
redirection (``>> .../cron-logs/<topic>.log 2>&1``) keeps working.

Usage:
    run_agent.py <name> <cmd> [args...]

If SANDBOX_DATA_DIR is unset, the command still runs but no heartbeat is
written — that keeps manual dev invocations working outside cron.
"""

import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone


def _write_atomic(path: pathlib.Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, path)


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: run_agent.py <name> <cmd> [args...]", file=sys.stderr)
        return 64

    name = sys.argv[1]
    cmd = sys.argv[2:]

    data_dir = os.environ.get("SANDBOX_DATA_DIR")
    health_path: pathlib.Path | None = None
    if data_dir:
        health_dir = pathlib.Path(data_dir) / "health"
        try:
            health_dir.mkdir(parents=True, exist_ok=True)
            health_path = health_dir / f"{name}.json"
        except OSError as e:
            # If we can't create the health dir, run the command anyway —
            # observability is best-effort, the agent itself is the job.
            print(f"[run_agent] could not create {health_dir}: {e}", file=sys.stderr)

    started_at = datetime.now(timezone.utc)
    start = time.monotonic()
    exit_code = 1
    error: str | None = None

    try:
        result = subprocess.run(cmd)
        exit_code = result.returncode
    except FileNotFoundError as e:
        exit_code = 127
        error = f"command not found: {e}"
        print(f"[run_agent] {error}", file=sys.stderr)
    except KeyboardInterrupt:
        exit_code = 128 + signal.SIGINT
        error = "interrupted"
    except Exception as e:  # noqa: BLE001 — wrapper must not crash silently
        exit_code = 1
        error = f"{type(e).__name__}: {e}"
        print(f"[run_agent] wrapper error: {error}", file=sys.stderr)

    duration = time.monotonic() - start
    finished_at = datetime.now(timezone.utc)

    if health_path is not None:
        payload = {
            "agent": name,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_s": round(duration, 3),
            "exit_code": exit_code,
            "command": cmd,
        }
        if error:
            payload["error"] = error
        try:
            _write_atomic(health_path, payload)
        except OSError as e:
            print(f"[run_agent] could not write {health_path}: {e}", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
