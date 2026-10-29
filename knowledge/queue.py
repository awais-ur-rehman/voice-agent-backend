"""Worker-only job routing and Source-before-Job lease claims; no model imports."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, text

from scheduler.errors import AppError

from .models import Job, Source


def worker_only(db):
    if db.scalar(text("SELECT current_user")) != "knowledge_worker":
        raise AppError("forbidden", 403)


def discover_jobs(db):
    worker_only(db)
    return list(db.execute(text("SELECT * FROM knowledge_discover_jobs()")).mappings())


def claim_job(db, source_id: UUID, job_id: UUID):
    worker_only(db)
    now = datetime.now(UTC)
    source = db.scalar(
        select(Source)
        .where(Source.id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if source is None or source.deleted_at is not None:
        return None
    job = db.scalar(
        select(Job)
        .where(Job.id == job_id, Job.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job is None or job.source_revision != source.revision:
        return None
    if not (
        (job.status == "queued" and job.available_at <= now)
        or (job.status == "processing" and job.lease_until is not None and job.lease_until <= now)
    ):
        return None
    if job.attempts >= 3:
        job.status, job.lease_token, job.lease_until = "failed", None, None
        source.status, source.error_code, source.error_at = "failed", "lease_exhausted", now
        db.flush()
        return None
    job.status, job.lease_token, job.lease_until = (
        "processing",
        uuid4(),
        now + timedelta(seconds=120),
    )
    job.attempts += 1
    source.status = "processing"
    db.flush()
    return {
        "tenant_id": job.tenant_id,
        "agent_id": job.agent_id,
        "source_id": job.source_id,
        "job_id": job.id,
        "source_revision": job.source_revision,
        "lease_token": job.lease_token,
        "lease_until": job.lease_until,
    }


def lease_current(db, source_id: UUID, job_id: UUID, revision: int, token: UUID):
    """Final write guard. Caller keeps these locks through its short commit."""
    worker_only(db)
    source = db.scalar(
        select(Source)
        .where(Source.id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if source is None or source.deleted_at is not None or source.revision != revision:
        return False
    job = db.scalar(
        select(Job)
        .where(Job.id == job_id, Job.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return bool(
        job
        and job.source_revision == revision
        and job.status == "processing"
        and job.lease_token == token
        and job.lease_until
        and job.lease_until > datetime.now(UTC)
    )


def activate_generation(
    db,
    source_id: UUID,
    job_id: UUID,
    revision: int,
    token: UUID,
    generation_id: UUID,
    *,
    profile: str | None = None,
) -> bool:
    """Atomically seal and activate complete staging rows under current lease authority."""
    from .profile import PROFILE

    worker_only(db)
    return bool(
        db.scalar(
            text(
                "SELECT knowledge_activate_generation(:source,:job,:revision,:token,:generation,:profile)"
            ),
            {
                "source": source_id,
                "job": job_id,
                "revision": revision,
                "token": token,
                "generation": generation_id,
                "profile": profile if profile is not None else PROFILE.fingerprint,
            },
        )
    )
