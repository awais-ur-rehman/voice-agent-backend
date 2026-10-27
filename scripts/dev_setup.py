"""Generate separate local API secrets without loading or replacing any env file."""

import os
import secrets
from pathlib import Path


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


def main() -> None:
    path = Path(__file__).resolve().parents[1] / ".env.api"
    created = create_env_file(path)
    print("Created private .env.api." if created else "Preserved existing .env.api.")
    print("Start development: docker compose --env-file .env.api up --build -d api")


if __name__ == "__main__":
    main()
