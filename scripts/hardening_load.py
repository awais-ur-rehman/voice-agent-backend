"""Opt-in ASGI qualification against the dedicated local synthetic fixture services."""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

FIXTURE_ENV = {
    "TEST_DATABASE_URL": "postgresql+psycopg://postgres:phase1_test_only@127.0.0.1:55433/voice_agent_test",
    "TEST_APP_DATABASE_URL": "postgresql+psycopg://scheduler_app:phase1_app_test_only@127.0.0.1:55433/voice_agent_test",
    "TEST_VALKEY_URL": "valkey://:phase2_valkey_test_only@127.0.0.1:56379/0",
}
MAX_CLIENTS = 20
MAX_ROUNDS = 30
TARGET_MS = 150
SCHEMA = "0018"
ROOT = Path(__file__).resolve().parents[1]


class Refusal(Exception):
    """Unsafe or unqualified load environment."""


@dataclass(frozen=True)
class Profile:
    clients: int = MAX_CLIENTS
    rounds: int = MAX_ROUNDS

    @property
    def qualifying(self) -> bool:
        return self.clients == MAX_CLIENTS and self.rounds == MAX_ROUNDS


class Arguments(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's default error would echo an arbitrary rejected value.
        raise Refusal("Use --help for the bounded disposable profile.") from None


def profile_for(argv: list[str], environment: Mapping[str, str]) -> Profile:
    parser = Arguments(description=__doc__, allow_abbrev=False)
    parser.add_argument("--allow-disposable-fixtures", action="store_true")
    parser.add_argument("--clients", type=int, default=MAX_CLIENTS)
    parser.add_argument("--rounds", type=int, default=MAX_ROUNDS)
    args = parser.parse_args(argv)
    if not args.allow_disposable_fixtures:
        raise Refusal("Explicit --allow-disposable-fixtures is required.")
    if not (1 <= args.clients <= MAX_CLIENTS and 1 <= args.rounds <= MAX_ROUNDS):
        raise Refusal("Clients must be 1–20 and rounds must be 1–30.")
    if any(environment.get(name, fixed) != fixed for name, fixed in FIXTURE_ENV.items()):
        raise Refusal("Only the exact loopback synthetic fixture URLs are allowed.")
    return Profile(args.clients, args.rounds)


def require_database(connection, *, schema: bool = True) -> None:
    from sqlalchemy import text

    if connection.scalar(text("SELECT current_database()")) != "voice_agent_test":
        raise Refusal("Connected database is outside the disposable fixture scope.")
    if schema and connection.scalar(text("SELECT version_num FROM alembic_version")) != SCHEMA:
        raise Refusal("The disposable database must already be migrated to 0018.")


def quantile(samples: list[float], fraction: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def source_context() -> dict:
    paths = (
        "scheduler",
        "brain",
        "analytics",
        "knowledge",
        "voice/catalog.json",
        "pyproject.toml",
        "uv.lock",
        "migrations",
        "scripts/hardening_load.py",
        "scripts/hardening_load_workloads.py",
    )
    git = ["/usr/bin/git", "-C", str(ROOT)]
    files = subprocess.check_output([*git, "ls-files", "-z", "--", *paths]).decode().split("\0")
    names = sorted({name for name in files if name} | set(paths[-2:]))
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode() + b"\0" + hashlib.sha256((ROOT / name).read_bytes()).digest())
    return {
        "checkout_head": subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip(),
        "workload_and_runtime_source_sha256": digest.hexdigest(),
        "source_file_count": len(names),
        "host": {
            "os": platform.system(),
            "kernel": platform.release(),
            "arch": platform.machine(),
            "logical_cpus": os.cpu_count(),
            "python": platform.python_version(),
        },
        "packages": {
            name: version(name)
            for name in ("anyio", "fastapi", "httpx2", "psycopg", "sqlalchemy", "valkey")
        },
    }


def run_isolated(profile: Profile) -> dict:
    # The CLI owns its process. Clear PG*, proxies, scheduler, dotenv locations,
    # API keys and all other caller variables before importing application code.
    os.environ.clear()
    os.environ.update({"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"})
    from scripts.hardening_load_workloads import run

    return run(profile)


def main(
    argv: list[str],
    environment: Mapping[str, str],
    runner: Callable[[Profile], dict] = run_isolated,
) -> int:
    try:
        profile = profile_for(argv, environment)
    except Refusal as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        report = runner(profile)
    except Refusal as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("Synthetic load failed during setup, execution, or cleanup.", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return int(not report["correctness"] or report["qualification"] is False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ))
