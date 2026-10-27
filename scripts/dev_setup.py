"""Generate separate local API secrets without loading or replacing any env file."""

import argparse
import os
import secrets
import stat
from pathlib import Path
from urllib.parse import quote


def create_env_file(path: Path) -> bool:
    """Create a private file atomically; return False without touching existing files."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    admin_password = secrets.token_hex(32)
    app_password = secrets.token_hex(32)
    jwt_secret = secrets.token_hex(48)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(
            "# Generated local API credentials. Keep this file private and untracked.\n"
            f"DEV_ADMIN_PASSWORD={admin_password}\n"
            f"DEV_APP_PASSWORD={app_password}\n"
            f"SCHEDULER_JWT_SECRET={jwt_secret}\n"
            "SCHEDULER_DATABASE_URL=postgresql+psycopg://scheduler_app:"
            f"{app_password}@127.0.0.1:55432/voice_agent\n"
            "SCHEDULER_MIGRATION_DATABASE_URL=postgresql+psycopg://postgres:"
            f"{admin_password}@127.0.0.1:55432/voice_agent\n"
        )
    return True


def enable_brain(path: Path) -> bool:
    """Append only missing brain keys to the API file under an exclusive lock."""
    import fcntl

    try:
        descriptor = os.open(path, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError("Brain setup requires a regular private API env file") from error
    with os.fdopen(descriptor, "r+", encoding="utf-8") as output:
        if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
            raise ValueError("Brain setup requires a regular API env file")
        fcntl.flock(output, fcntl.LOCK_EX)
        existing = output.read()
        values = {}
        for line in existing.splitlines():
            key, separator, value = line.partition("=")
            if separator and not key.lstrip().startswith("#"):
                values[key.strip()] = value.strip().strip("\"'")
        password = values.get("DEV_VALKEY_PASSWORD", secrets.token_hex(32))
        candidates = {
            "DEV_VALKEY_PASSWORD": password,
            "SCHEDULER_RUNTIME_SECRET": secrets.token_hex(48),
            "SCHEDULER_VALKEY_URL": f"valkey://:{quote(password, safe='')}@127.0.0.1:56380/0",
        }
        missing = {key: value for key, value in candidates.items() if key not in values}
        if not missing:
            return False
        output.seek(0, os.SEEK_END)
        if existing and not existing.endswith("\n"):
            output.write("\n")
        output.writelines(f"{key}={value}\n" for key, value in missing.items())
        output.flush()
        os.fsync(output.fileno())
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-brain", action="store_true")
    args = parser.parse_args()
    path = Path(__file__).resolve().parents[1] / ".env.api"
    created = create_env_file(path)
    print("Created private .env.api." if created else "Preserved existing .env.api.")
    if args.enable_brain:
        enable_brain(path)
        print("Brain configuration ready; existing credentials preserved.")
    print("Start development: docker compose --env-file .env.api up --build -d api")


if __name__ == "__main__":
    main()
