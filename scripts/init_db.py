"""Initialize the dedicated application role using separate migration credentials."""

import os

import psycopg
from psycopg import sql


def init_database(admin_url: str, app_password: str) -> None:
    """Idempotently provision a restricted login; never print credentials."""
    if not app_password:
        raise ValueError("The application database password must not be empty.")
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
                if cursor.fetchone()[0]:
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
            cursor.execute("SELECT current_database()")
            database_name = cursor.fetchone()[0]
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
        )
    except Exception:
        # Driver errors can contain connection details; keep the CLI error credential-free.
        raise SystemExit(
            "Database role initialization failed; check configuration and connectivity."
        )
    print("Application database role initialized.")


if __name__ == "__main__":
    main()
