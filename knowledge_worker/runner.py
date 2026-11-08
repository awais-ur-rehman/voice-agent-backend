"""Recoverable lease worker. Every external operation runs outside SQL transactions."""

import threading
import time
from datetime import timedelta

from sqlalchemy import func, select

from knowledge.activation import StaleLease, stage_and_activate
from knowledge.chunking import chunk_document
from knowledge.embedding import EmbeddingUnavailable, validate_vectors
from knowledge.fetching import fetch_url
from knowledge.models import Job, Source
from knowledge.parsing import IngestionError, parse_bytes
from knowledge.queue import claim_job, discover_jobs, lease_current
from knowledge.storage import owned_key
from scheduler.db import tenant_context

SAFE_ERRORS = frozenset(
    {
        "input_too_large",
        "extracted_too_large",
        "encrypted_pdf",
        "pdf_no_text",
        "too_many_pages",
        "archive_limit",
        "unsafe_archive",
        "external_relationship",
        "invalid_format",
        "invalid_document",
        "no_text",
        "parser_timeout",
        "parser_memory",
        "parser_output",
        "parser_failed",
        "too_many_chunks",
        "tokenizer_limit",
        "unsafe_url",
        "url_destination",
        "url_redirects",
        "url_timeout",
        "url_too_large",
        "url_busy",
        "url_encoding",
        "url_format",
        "url_response",
        "url_unavailable",
        "url_dns_busy",
        "blob_unavailable",
        "embedding_unavailable",
        "processing_failed",
        "unsupported_docx_part",
    }
)


def _current(db, lease):
    return lease_current(
        db, lease["source_id"], lease["job_id"], lease["source_revision"], lease["lease_token"]
    )


def renew_lease(factory, lease):
    with factory() as db, db.begin():
        tenant_context(db, lease["tenant_id"])
        if not _current(db, lease):
            return False
        job = db.get(Job, lease["job_id"])
        job.lease_until = db.scalar(select(func.clock_timestamp())) + timedelta(seconds=120)
        return True


def fail_job(factory, lease, code, *, retryable=False):
    code = code if code in SAFE_ERRORS else "processing_failed"
    with factory() as db, db.begin():
        tenant_context(db, lease["tenant_id"])
        if not _current(db, lease):
            return "stale"
        source, job = db.get(Source, lease["source_id"]), db.get(Job, lease["job_id"])
        now = db.scalar(select(func.clock_timestamp()))
        status = "queued" if retryable and job.attempts < 3 else "failed"
        source.status, source.error_code, source.error_at = status, code, now
        source.updated_at = now
        job.status, job.lease_token, job.lease_until = status, None, None
        job.available_at = now + timedelta(seconds=30 * job.attempts)
        return status


class _Heartbeat:
    def __init__(self, factory, lease):
        self.factory, self.lease = factory, lease
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.wait(30):
            try:
                if renew_lease(self.factory, self.lease):
                    continue
            except Exception:
                pass
            self.lost.set()
            return

    def check(self):
        if self.lost.is_set():
            raise StaleLease


def process_job(factory, blob_store, embeddings, lease, *, clock=time.monotonic, tokenizer):
    heartbeat = _Heartbeat(factory, lease)
    heartbeat.thread.start()
    try:
        with factory() as db, db.begin():
            tenant_context(db, lease["tenant_id"])
            if not _current(db, lease):
                raise StaleLease
            source = db.get(Source, lease["source_id"])
            kind, content = source.kind, dict(source.content)
            key, checksum = source.blob_key, source.checksum
        if kind == "file":
            parts = owned_key(key)
            if parts is None or parts[2] > lease["source_revision"]:
                raise IngestionError("blob_unavailable")
            data = blob_store.read(key, lease["tenant_id"], lease["source_id"], parts[2], checksum)
            document = parse_bytes(data, content["format"])
        elif kind == "url":
            data, format = fetch_url(content["url"])
            document = parse_bytes(data, format)
        elif kind == "faq":
            # Plain text keeps FAQ punctuation/content literal (not Markdown syntax).
            document = parse_bytes(
                (content["question"] + "\n\n" + content["answer"]).encode(), "txt"
            )
        else:
            document = parse_bytes(content["text"].encode(), content["format"])
        heartbeat.check()
        chunks = chunk_document(document, tokenizer)
        vectors = []
        for chunk in chunks:
            heartbeat.check()
            vectors.extend(validate_vectors(embeddings.embed([chunk.text], deadline=clock() + 10)))
        heartbeat.check()
        with factory() as db, db.begin():
            tenant_context(db, lease["tenant_id"])
            stage_and_activate(db, lease, chunks, vectors)
        return "ready"
    except StaleLease:
        return "stale"
    except EmbeddingUnavailable:
        return fail_job(factory, lease, "embedding_unavailable", retryable=True)
    except IngestionError as exc:
        return fail_job(
            factory,
            lease,
            str(exc),
            retryable=str(exc) in {"url_timeout", "url_unavailable", "url_dns_busy", "url_busy"},
        )
    except Exception:
        # A crash before this write is recoverable through the expired-lease discovery path.
        return fail_job(factory, lease, "processing_failed", retryable=True)
    finally:
        heartbeat.stop.set()
        heartbeat.thread.join(timeout=5)


def run_once(factory, blob_store, embeddings, *, tokenizer_loader, clock=time.monotonic):
    # Intentional unavailability never consumes a job attempt or loads a tokenizer.
    if not embeddings.available():
        return 0
    tokenizer = tokenizer_loader()
    with factory() as db, db.begin():
        routes = discover_jobs(db)
    count = 0
    for route in routes:
        if not embeddings.available():
            break
        with factory() as db, db.begin():
            tenant_context(db, route["tenant_id"])
            lease = claim_job(db, route["source_id"], route["job_id"])
        if lease is not None:
            process_job(factory, blob_store, embeddings, lease, clock=clock, tokenizer=tokenizer)
            count += 1
    return count
