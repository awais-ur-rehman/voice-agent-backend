"""Authenticated orphan cleanup with per-inode, transaction and tenant fences."""

import fcntl
import os
import stat
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from knowledge.models import Job, Source
from knowledge.queue import worker_only
from knowledge.storage import owned_key
from scheduler.db import tenant_context


def cleanup_blobs(factory, blob_store, *, now=None, limit=100):
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=24)
    removed = 0
    if len(blob_store.secret) < 32 or not blob_store.root.is_dir() or blob_store.root.is_symlink():
        return 0
    for path in blob_store.cleanup_candidates(min(limit, 100)):
        parts = owned_key(path.name)
        if parts is None:
            continue
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mtime > cutoff.timestamp():
                continue
            receipt = blob_store.receipt(path.name, fd)
            if receipt is None or receipt["created_at"] > cutoff.timestamp():
                continue
            tenant, source_id, revision = parts
            with factory() as db, db.begin():
                worker_only(db)
                tenant_context(db, tenant)
                if not db.scalar(
                    text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key,7241))"),
                    {"key": path.name},
                ):
                    continue
                source = db.scalar(select(Source).where(Source.id == source_id).with_for_update())
                if source is not None and (
                    source.tenant_id != tenant or revision > source.revision
                ):
                    continue
                references = list(db.scalars(select(Source).where(Source.blob_key == path.name)))
                if any(s.deleted_at is None or s.deleted_at > cutoff for s in references):
                    if receipt.get("unreferenced_at") is not None:
                        blob_store.mark_unreferenced(path.name, receipt, None)
                    continue
                if (
                    references
                    and db.scalar(
                        select(Job.id)
                        .where(
                            Job.source_id.in_([s.id for s in references]),
                            Job.status.in_(["queued", "processing"]),
                        )
                        .limit(1)
                    )
                    is not None
                ):
                    if receipt.get("unreferenced_at") is not None:
                        blob_store.mark_unreferenced(path.name, receipt, None)
                    continue
                retired = receipt.get("unreferenced_at")
                if retired is None or (
                    source is not None and source.updated_at.timestamp() > retired
                ):
                    blob_store.mark_unreferenced(path.name, receipt, now.timestamp())
                    continue
                if retired > cutoff.timestamp():
                    continue
                # Recheck path identity while all authorities remain held. No arbitrary unlink.
                current = path.lstat()
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    continue
                path.unlink()
                (blob_store.root / (path.name + ".receipt")).unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
        finally:
            if fd is not None:
                os.close(fd)
    return removed
