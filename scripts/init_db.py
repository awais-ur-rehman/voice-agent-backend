"""Initialize the dedicated application role using separate migration credentials."""

import os

import psycopg
from psycopg import sql


def init_database(
    admin_url: str,
    app_password: str,
    worker_password: str | None = None,
    *,
    analytics_worker_password: str | None = None,
) -> None:
    """Idempotently provision a restricted login; never print credentials."""
    if not app_password:
        raise ValueError("The application database password must not be empty.")
    if analytics_worker_password == "":
        raise ValueError("The analytics worker password must not be empty.")
    connection_url = admin_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(connection_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT oid FROM pg_roles WHERE rolname = 'scheduler_app'")
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datdba = %s) "
                    "OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner = %s) "
                    "OR EXISTS (SELECT 1 FROM pg_class WHERE relowner = %s)",
                    (row[0], row[0], row[0]),
                )
                if cursor.fetchone() == (True,):
                    raise RuntimeError(
                        "scheduler_app must not own a database, schema, or relation."
                    )
            else:
                cursor.execute("CREATE ROLE scheduler_app")
            cursor.execute(
                sql.SQL(
                    "ALTER ROLE scheduler_app WITH LOGIN NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Literal(app_password))
            )
            cursor.execute("SELECT oid FROM pg_roles WHERE rolname = 'knowledge_worker'")
            worker = cursor.fetchone()
            if worker:
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datdba=%s) "
                    "OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner=%s) "
                    "OR EXISTS (SELECT 1 FROM pg_class WHERE relowner=%s)",
                    (worker[0], worker[0], worker[0]),
                )
                if cursor.fetchone() == (True,):
                    raise RuntimeError(
                        "knowledge_worker must not own a database, schema, or relation."
                    )
            else:
                cursor.execute("CREATE ROLE knowledge_worker NOLOGIN")
            cursor.execute(
                "ALTER ROLE knowledge_worker NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOINHERIT NOREPLICATION NOBYPASSRLS"
            )
            if worker_password:
                cursor.execute(
                    sql.SQL("ALTER ROLE knowledge_worker LOGIN PASSWORD {}").format(
                        sql.Literal(worker_password)
                    )
                )
            # Never reset an existing private worker credential on ordinary API provisioning.
            cursor.execute("SELECT oid FROM pg_roles WHERE rolname='analytics_worker'")
            analytics_worker = cursor.fetchone()
            if analytics_worker:
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datdba=%s) "
                    "OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner=%s) "
                    "OR EXISTS (SELECT 1 FROM pg_class WHERE relowner=%s)",
                    (analytics_worker[0], analytics_worker[0], analytics_worker[0]),
                )
                if cursor.fetchone() == (True,):
                    raise RuntimeError(
                        "analytics_worker must not own a database, schema, or relation."
                    )
            else:
                cursor.execute("CREATE ROLE analytics_worker NOLOGIN")
            cursor.execute(
                "ALTER ROLE analytics_worker NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOINHERIT NOREPLICATION NOBYPASSRLS"
            )
            if analytics_worker_password is not None:
                cursor.execute(
                    sql.SQL("ALTER ROLE analytics_worker LOGIN PASSWORD {}").format(
                        sql.Literal(analytics_worker_password)
                    )
                )
            cursor.execute("SELECT current_database()")
            database_row = cursor.fetchone()
            assert database_row is not None
            database_name = database_row[0]
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO scheduler_app").format(
                    sql.Identifier(database_name)
                )
            )


def main() -> None:
    try:
        init_database(
            os.environ["SCHEDULER_MIGRATION_DATABASE_URL"],
            os.environ["SCHEDULER_APP_PASSWORD"],
            os.environ.get("SCHEDULER_KNOWLEDGE_WORKER_PASSWORD"),
            analytics_worker_password=os.environ.get("SCHEDULER_ANALYTICS_WORKER_PASSWORD"),
        )
    except Exception:
        # Driver errors can contain connection details; keep the CLI error credential-free.
        raise SystemExit(
            "Database role initialization failed; check configuration and connectivity."
        )
    print("Application database role initialized.")


if __name__ == "__main__":
    main()
