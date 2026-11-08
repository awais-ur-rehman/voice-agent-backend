"""Disabled startup has no DB connection; enabled startup qualifies a narrow role."""

import argparse
import json
import signal
import threading
from datetime import UTC, datetime

from .settings import Settings


def qualify(engine):
    from sqlalchemy import text

    with engine.connect() as db:
        role = db.execute(
            text(
                "SELECT current_user AS name,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,rolinherit,rolreplication FROM pg_roles WHERE rolname=current_user"
            )
        ).one()
        unsafe = db.scalar(
            text("""SELECT EXISTS(SELECT 1 FROM pg_class WHERE relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)
          AND relnamespace='public'::regnamespace) OR EXISTS(SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=current_user))""")
        )
        if role.name != "analytics_worker" or any(role[1:]) or unsafe:
            raise RuntimeError("analytics_worker_role_required")
        for table in [
            "patients",
            "appointments",
            "users",
            "refresh_tokens",
            "agent_sessions",
            "voice_sessions",
            "command_receipts",
            "knowledge_sources",
            "knowledge_chunks",
            "knowledge_jobs",
            "knowledge_query_events",
        ]:
            if db.scalar(
                text(
                    "SELECT has_any_column_privilege(current_user,:table,'SELECT,INSERT,UPDATE,REFERENCES') OR has_table_privilege(current_user,:table,'DELETE,TRUNCATE,TRIGGER')"
                ),
                {"table": table},
            ):
                raise RuntimeError("analytics_worker_grants_unsafe")
        if db.scalar(
            text(
                "SELECT has_column_privilege(current_user,'analytics_transcripts','text','SELECT') OR has_table_privilege(current_user,'analytics_transcripts','INSERT,UPDATE')"
            )
        ):
            raise RuntimeError("analytics_worker_grants_unsafe")
        if not db.scalar(
            text(
                "SELECT has_function_privilege(current_user,'analytics_discover_tenants(uuid,integer)','EXECUTE') AND has_table_privilege(current_user,'analytics_daily_rollups','SELECT,INSERT,UPDATE,DELETE')"
            )
        ):
            raise RuntimeError("analytics_worker_migration_required")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings(_env_file=None)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if not settings.enabled:
        print(json.dumps({"status": "disabled"}), flush=True)
        while not args.once and not stop.wait(30):
            pass
        return
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import sessionmaker

    from analytics.aggregation import run_once

    url = settings.database_url.get_secret_value()
    if not url or make_url(url).username != "analytics_worker":
        raise SystemExit("analytics_worker_database_required")
    engine = create_engine(
        url,
        hide_parameters=True,
        pool_size=2,
        max_overflow=0,
        connect_args={
            "connect_timeout": 5,
            "options": "-c statement_timeout=5000 -c lock_timeout=1000",
        },
    )
    try:
        qualify(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        while not stop.is_set():
            try:
                print(
                    json.dumps(
                        run_once(
                            factory, clock=lambda: datetime.now(UTC), limit=settings.batch_size
                        )
                    ),
                    flush=True,
                )
            except Exception:
                print(json.dumps({"status": "analytics_unavailable"}), flush=True)
            if args.once:
                break
            stop.wait(settings.poll_seconds)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
