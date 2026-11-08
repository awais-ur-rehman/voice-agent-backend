"""Worker control heartbeat independent of long-running source ingestion."""

import time

from sqlalchemy import text

from knowledge.readiness import publish_observation
from knowledge_worker.settings import verify_tokenizer


def observe(factory, settings, embeddings, *, verify=verify_tokenizer, clock=time.time):
    # Timestamp BEFORE probes. Slow/stuck observations cannot become fresh on completion.
    observed_at = clock()
    worker = embedding = artifacts = False
    try:
        with factory() as db, db.begin():
            worker = db.scalar(text("SELECT current_user")) == "knowledge_worker"
        embedding = embeddings.available()
        verify(settings.tokenizer)
        artifacts = True
    except Exception:
        # Provider, database and artifact exceptions never enter the public record/log.
        pass
    publish_observation(
        settings.blob_root,
        settings.blob_secret,
        worker=worker,
        embedding=embedding,
        artifacts=artifacts,
        observed_at=observed_at,
    )


def heartbeat(stop, factory, settings, embeddings):
    while not stop.is_set():
        try:
            observe(factory, settings, embeddings)
        except Exception:
            pass  # Missing storage/secret fails closed; last successful observation expires.
        stop.wait(10)
