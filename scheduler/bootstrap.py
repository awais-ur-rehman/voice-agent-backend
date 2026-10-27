"""Local administrative provisioning; never reads the Phase 0 credential file."""

import argparse
import getpass
import json
import os
import secrets
import stat
from datetime import time
from pathlib import Path

from dotenv import dotenv_values
from pydantic import EmailStr, TypeAdapter
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from .models import Patient, Provider, ProviderService, Schedule, Service, Tenant, User
from .security import passwords

ROOT = Path(__file__).resolve().parents[1]


def validate_local_database(admin_url: str) -> None:
    """Refuse remote targets, unrelated databases, and connection-routing overrides."""
    url = make_url(admin_url)
    if (
        url.drivername != "postgresql+psycopg"
        or url.host not in {"localhost", "127.0.0.1", "::1"}
        or url.database not in {"voice_agent", "voice_agent_test"}
        or url.query
    ):
        raise ValueError("Provisioning requires a local voice_agent or voice_agent_test database.")


def _verify_existing(db: Session, path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("Existing demo credentials must be a private regular file with mode 0600.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    clinics = payload["clinics"]
    if len(clinics) != 2:
        raise ValueError("Existing demo credentials do not contain both clinics.")
    for suffix, clinic in zip(("a", "b"), clinics, strict=True):
        email = f"demo-owner-{suffix}@example.com"
        user = db.scalar(select(User).where(User.email == email))
        if (
            user is None
            or user.role != "owner"
            or not user.active
            or clinic["owner_email"] != email
            or str(user.tenant_id) != clinic["tenant_id"]
            or not passwords.verify(clinic["owner_password"], user.password_hash)
        ):
            raise ValueError("Existing demo credentials do not match the provisioned accounts.")


def seed_demo(admin_url: str, credentials_path: Path) -> Path:
    """Create two synthetic clinics atomically and preserve matching reruns unchanged."""
    validate_local_database(admin_url)
    credentials_path = Path(credentials_path)
    engine = create_engine(admin_url, hide_parameters=True)
    descriptor = None
    created = False
    try:
        credentials_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(credentials_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created = True
        except FileExistsError:
            with Session(engine) as db:
                _verify_existing(db, credentials_path)
            return credentials_path
        with Session(engine) as db, db.begin():
            clinics = []
            for suffix in ("a", "b"):
                email = f"demo-owner-{suffix}@example.com"
                if db.scalar(select(User.id).where(User.email == email)) is not None:
                    raise ValueError("Demo account already exists without matching credentials.")
                tenant = Tenant(name=f"Synthetic Clinic {suffix.upper()}")
                db.add(tenant)
                db.flush()
                password = secrets.token_urlsafe(32)
                db.add(
                    User(
                        tenant_id=tenant.id,
                        email=email,
                        password_hash=passwords.hash(password),
                        role="owner",
                    )
                )
                provider = Provider(tenant_id=tenant.id, name=f"Dr Synthetic {suffix.upper()}")
                service = Service(tenant_id=tenant.id, name="Consultation", duration_min=30)
                patient = Patient(
                    tenant_id=tenant.id,
                    name=f"Synthetic Patient {suffix.upper()}",
                    name_key=f"synthetic patient {suffix}",
                    phone=f"+92300000000{1 if suffix == 'a' else 2}",
                )
                db.add_all([provider, service, patient])
                db.flush()
                db.add(
                    ProviderService(
                        tenant_id=tenant.id, provider_id=provider.id, service_id=service.id
                    )
                )
                db.add_all(
                    Schedule(
                        tenant_id=tenant.id,
                        provider_id=provider.id,
                        weekday=weekday,
                        start_time=time(9),
                        end_time=time(17),
                        slot_min=30,
                    )
                    for weekday in range(1, 8)
                )
                clinics.append(
                    {
                        "name": tenant.name,
                        "tenant_id": str(tenant.id),
                        "owner_email": email,
                        "owner_password": password,
                        "provider_id": str(provider.id),
                        "service_id": str(service.id),
                        "patient_id": str(patient.id),
                    }
                )
            db.flush()
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                descriptor = None
                json.dump({"clinics": clinics}, output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
        return credentials_path
    except BaseException:
        if created:
            credentials_path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        engine.dispose()


def provision_owner(admin_url: str, clinic_name: str, email: str, password: str) -> str:
    validate_local_database(admin_url)
    normalized_email = str(TypeAdapter(EmailStr).validate_python(email)).lower()
    if not clinic_name.strip() or len(password) < 12:
        raise ValueError("A clinic name and password of at least 12 characters are required.")
    engine = create_engine(admin_url, hide_parameters=True)
    try:
        with Session(engine) as db, db.begin():
            if db.scalar(select(User.id).where(User.email == normalized_email)) is not None:
                raise ValueError("Account already exists.")
            tenant = Tenant(name=clinic_name.strip())
            db.add(tenant)
            db.flush()
            db.add(
                User(
                    tenant_id=tenant.id,
                    email=normalized_email,
                    password_hash=passwords.hash(password),
                    role="owner",
                )
            )
            tenant_id = str(tenant.id)
        return tenant_id
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--clinic-name")
    parser.add_argument("--email")
    args = parser.parse_args()
    if args.demo == bool(args.clinic_name or args.email):
        parser.error("Choose --demo or both --clinic-name and --email.")
    if not args.demo and not (args.clinic_name and args.email):
        parser.error("Both --clinic-name and --email are required.")
    try:
        # Disable interpolation so values cannot pull unrelated environment secrets.
        config = dotenv_values(ROOT / ".env.api", interpolate=False)
        admin_url = config.get("SCHEDULER_MIGRATION_DATABASE_URL")
        if not admin_url:
            raise ValueError("Missing migration database URL.")
        validate_local_database(admin_url)
        if args.demo:
            print(seed_demo(admin_url, ROOT / ".local" / "phase1-demo-credentials.json"))
        else:
            password = getpass.getpass("New owner password (at least 12 characters): ")
            print(provision_owner(admin_url, args.clinic_name, args.email, password))
    except Exception:
        raise SystemExit("Local provisioning failed; verify the API configuration and database.")


if __name__ == "__main__":
    main()
