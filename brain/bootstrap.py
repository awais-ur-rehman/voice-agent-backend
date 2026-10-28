"""Explicit local synthetic provisioning, immutable publishing and recovery maintenance."""

import argparse
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from scheduler.bootstrap import ROOT, seed_demo, validate_local_database
from scheduler.config import load_settings
from scheduler.db import tenant_context
from scheduler.models import Agent, AgentVersion, Patient, Provider, Service

from .agent_config import AgentConfig, LegacyAgentConfig, read_config
from .sessions import purge_recovery


def read_demo_credentials(path: Path) -> dict:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError("Demo credentials must be a regular private file") from error
    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("Demo credentials must be a regular private 0600 file")
        return json.load(source)


def update_demo_identifiers(path: Path, summary: list[dict]) -> None:
    """Atomically add identifiers; a failed write cannot truncate saved passwords."""
    import fcntl
    import tempfile

    lock_fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = read_demo_credentials(path)
        by_tenant = {entry["tenant_id"]: entry for entry in summary}
        changed = False
        for clinic in current["clinics"]:
            expected = by_tenant[clinic["tenant_id"]]["agent_id"]
            if "agent_id" in clinic and clinic["agent_id"] != expected:
                raise ValueError("Saved demo agent does not match the synthetic tenant")
            if "agent_id" not in clinic:
                clinic["agent_id"] = expected
                changed = True
        if not changed:
            return
        descriptor, temporary = tempfile.mkstemp(prefix=".demo-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(current, output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def publish_agent(
    db: Session, tenant_id: UUID, agent_id: UUID, config: AgentConfig | LegacyAgentConfig
) -> UUID:
    """Publish a fresh immutable version; existing sessions keep their original version."""
    tenant_context(db, tenant_id)
    agent = db.scalar(
        select(Agent)
        .where(
            Agent.id == agent_id,
            Agent.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    if agent is None:
        raise ValueError("Agent does not belong to this tenant")
    number = (
        db.scalar(
            select(func.max(AgentVersion.number)).where(
                AgentVersion.agent_id == agent.id, AgentVersion.purpose == "production"
            )
        )
        or 0
    ) + 1
    version = AgentVersion(
        number=number,
        id=uuid4(),
        tenant_id=tenant_id,
        agent_id=agent.id,
        config=config.model_dump(),
    )
    db.add(version)
    db.flush()
    agent.active_version_id = version.id
    db.flush()
    return version.id


def seed_brain_demo(admin_url: str, credentials_path: Path) -> list[dict]:
    """Extend Phase 1 synthetic clinics without replacing their accounts or passwords."""
    validate_local_database(admin_url)
    seed_demo(admin_url, credentials_path)
    payload = read_demo_credentials(credentials_path)
    engine = create_engine(admin_url, hide_parameters=True)
    summary = []
    try:
        with Session(engine) as db, db.begin():
            for clinic in payload["clinics"]:
                tenant_id = UUID(clinic["tenant_id"])
                tenant_context(db, tenant_id)
                agent_id = uuid5(tenant_id, "phase-2-synthetic-receptionist")
                agent = db.get(Agent, agent_id)
                if agent is None:
                    agent = Agent(id=agent_id, tenant_id=tenant_id, name="Synthetic Receptionist")
                    db.add(agent)
                    db.flush()
                    publish_agent(db, tenant_id, agent.id, LegacyAgentConfig())
                for model, key, name_ur in (
                    (Provider, "provider_id", "ڈاکٹر مصنوعی"),
                    (Service, "service_id", "مشاورت"),
                    (Patient, "patient_id", "مصنوعی مریض"),
                ):
                    row = cast(
                        Provider | Service | Patient | None, db.get(model, UUID(clinic[key]))
                    )
                    if row is None or row.tenant_id != tenant_id:
                        raise ValueError("Synthetic demo record does not match tenant")
                    # These explicit synthetic pronunciations still need native owner review.
                    if row.name_ur is None:
                        row.name_ur = name_ur
                summary.append(
                    {
                        "tenant_id": str(tenant_id),
                        "agent_id": str(agent_id),
                        "version_id": str(agent.active_version_id),
                    }
                )
                clinic.setdefault("agent_id", str(agent_id))
            db.flush()
        update_demo_identifiers(credentials_path, summary)
        return summary
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--demo", action="store_true")
    choice.add_argument("--publish", action="store_true")
    choice.add_argument("--purge-recovery", action="store_true")
    parser.add_argument("--tenant-id", type=UUID)
    parser.add_argument("--agent-id", type=UUID)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--limit", type=int, default=500, choices=range(1, 1001), metavar="1..1000")
    args = parser.parse_args()
    if not args.demo and not args.tenant_id:
        parser.error("--tenant-id is required for publishing or maintenance")
    if args.publish and not (args.agent_id and args.config):
        parser.error("--publish requires --agent-id and --config")
    try:
        settings = load_settings()
        if args.demo:
            # The migration credential lives only in the dedicated API env file.
            from dotenv import dotenv_values

            config = dotenv_values(ROOT / ".env.api", interpolate=False)
            url = config.get("SCHEDULER_MIGRATION_DATABASE_URL")
            if not url:
                raise ValueError("Missing migration URL")
            print(json.dumps(seed_brain_demo(url, ROOT / ".local/phase1-demo-credentials.json")))
            return
        engine = create_engine(settings.database_url.get_secret_value(), hide_parameters=True)
        try:
            with Session(engine) as db, db.begin():
                tenant_context(db, args.tenant_id)
                if args.publish:
                    agent_config = read_config(json.loads(args.config.read_text(encoding="utf-8")))
                    print(publish_agent(db, args.tenant_id, args.agent_id, agent_config))
                else:
                    print(json.dumps(purge_recovery(db, datetime.now(UTC), args.limit)))
        finally:
            engine.dispose()
    except Exception:
        raise SystemExit("Local brain operation failed; verify configuration, tenant and database.")


if __name__ == "__main__":
    main()
