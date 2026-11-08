"""Short fenced SQL staging; caller has already parsed and embedded all chunks."""

from uuid import uuid4

from .models import Chunk, Generation
from .profile import PROFILE
from .queue import activate_generation, lease_current


class StaleLease(RuntimeError):
    pass


def stage_and_activate(db, lease, chunks, vectors):
    if not lease_current(
        db, lease["source_id"], lease["job_id"], lease["source_revision"], lease["lease_token"]
    ):
        raise StaleLease
    # Source -> Job is already locked before any FK lock or staging insert.
    generation = Generation(
        id=uuid4(),
        tenant_id=lease["tenant_id"],
        agent_id=lease["agent_id"],
        source_id=lease["source_id"],
        source_revision=lease["source_revision"],
        profile=PROFILE.fingerprint,
        chunk_count=len(chunks),
    )
    db.add(generation)
    db.flush()
    for chunk, vector in zip(chunks, vectors, strict=True):
        db.add(
            Chunk(
                id=uuid4(),
                tenant_id=lease["tenant_id"],
                agent_id=lease["agent_id"],
                source_id=lease["source_id"],
                generation_id=generation.id,
                ordinal=chunk.ordinal,
                heading=chunk.heading,
                language=chunk.language,
                text=chunk.text,
                token_count=chunk.token_count,
                embedding=vector,
            )
        )
    db.flush()
    if not activate_generation(
        db,
        lease["source_id"],
        lease["job_id"],
        lease["source_revision"],
        lease["lease_token"],
        generation.id,
    ):
        # Roll back every staging insert, including if DB-clock expiry occurred during flush.
        raise StaleLease
    return generation.id
