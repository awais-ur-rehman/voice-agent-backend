"""Bounded private uploads with authenticated ownership and per-blob commit locks."""

import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import time
from collections.abc import Generator
from contextlib import suppress
from pathlib import Path
from uuid import UUID, uuid4

from scheduler.errors import AppError

MAX_BYTES = 10 * 1024 * 1024
UPLOAD_SECONDS = 30.0
QUOTA_BYTES = 1024**3
DISK_RESERVE = 4 * 1024**3


class PendingBlob:
    """The upload's exclusive inode lock survives until outer SQL commit/rollback."""

    def __init__(self, key, fd):
        self.key, self.fd = key, fd

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __del__(self):
        self.release()


def owned_key(key):
    if not isinstance(key, str):
        return None
    match = re.fullmatch(
        r"([0-9a-f]{32})-([0-9a-f]{32})-([1-9][0-9]{0,9})-([0-9a-f]{32})\.blob", key
    )
    if match is None:
        return None
    return UUID(hex=match[1]), UUID(hex=match[2]), int(match[3])


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


class BlobStore:
    def __init__(self, root: Path, *, secret="", clock=time.time):
        self.root, self.secret, self.clock = root, secret, clock
        self._cleanup_iterator: Generator[os.DirEntry[str], None, None] | None = None

    def prepare(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise AppError("storage_unavailable", 503)
        os.chmod(self.root, 0o700)

    def usage(self):
        total = 0
        for path in self.root.iterdir():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise AppError("storage_unavailable", 503)
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
        return total

    def _write_receipt(self, key, fd, checksum):
        tenant, source, revision = owned_key(key)
        info = os.fstat(fd)
        payload = dict(
            version=1,
            key=key,
            tenant_id=tenant.hex,
            source_id=source.hex,
            revision=revision,
            sha256=checksum,
            size=info.st_size,
            device=info.st_dev,
            inode=info.st_ino,
            created_at=self.clock(),
            unreferenced_at=None,
        )
        signature = hmac.new(self.secret.encode(), _canonical(payload), "sha256").hexdigest()
        receipt = self.root / (key + ".receipt")
        receipt_fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(receipt_fd, "wb") as output:
            output.write(_canonical({"payload": payload, "signature": signature}))
            output.flush()
            os.fsync(output.fileno())
        directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    async def receive(self, stream, tenant: UUID, source: UUID, revision: int, checksum: str):
        if len(self.secret) < 32:
            raise AppError("storage_unavailable", 503)
        temporary = self.root / (uuid4().hex + ".tmp")
        key = f"{tenant.hex}-{source.hex}-{revision}-{uuid4().hex}.blob"
        lock_fd = blob_fd = None
        try:
            self.prepare()
            lock_fd = os.open(
                self.root / ".upload.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            async with asyncio.timeout(UPLOAD_SECONDS):
                while True:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.02)
                if (
                    self.usage() + MAX_BYTES + 4096 > QUOTA_BYTES
                    or shutil.disk_usage(self.root).free < DISK_RESERVE + MAX_BYTES + 4096
                ):
                    raise AppError("storage_capacity", 507)
                digest, size = hashlib.sha256(), 0
                blob_fd = os.open(
                    temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                fcntl.flock(blob_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with os.fdopen(os.dup(blob_fd), "wb") as output:
                    async for chunk in stream:
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise AppError("upload_too_large", 413)
                        if shutil.disk_usage(self.root).free < DISK_RESERVE + len(chunk):
                            raise AppError("storage_capacity", 507)
                        digest.update(chunk)
                        output.write(chunk)
                    if size == 0 or digest.hexdigest() != checksum.lower():
                        raise AppError("upload_checksum", 422)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.root / key)
                self._write_receipt(key, blob_fd, digest.hexdigest())
                pending = PendingBlob(key, blob_fd)
                blob_fd = None
                return pending
        except TimeoutError:
            raise AppError("upload_timeout", 408) from None
        except OSError:
            raise AppError("storage_unavailable", 503) from None
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            if blob_fd is not None:
                os.close(blob_fd)
            if lock_fd is not None:
                os.close(lock_fd)

    def receipt(self, key, fd):
        """Verify HMAC, canonical identity and actual complete contents under a held lock."""
        if len(self.secret) < 32 or owned_key(key) is None:
            return None
        try:
            receipt_fd = os.open(self.root / (key + ".receipt"), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(receipt_fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return None
                record = json.loads(stream.read(4097))
            payload = record["payload"]
            signature = hmac.new(self.secret.encode(), _canonical(payload), "sha256").hexdigest()
            if not hmac.compare_digest(signature, record["signature"]):
                return None
            tenant, source, revision = owned_key(key)
            info = os.fstat(fd)
            if (
                payload["version"] != 1
                or payload["key"] != key
                or (payload["tenant_id"], payload["source_id"], payload["revision"])
                != (tenant.hex, source.hex, revision)
                or (payload["device"], payload["inode"], payload["size"])
                != (info.st_dev, info.st_ino, info.st_size)
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not 0 < info.st_size <= MAX_BYTES
            ):
                return None
            os.lseek(fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(fd), "rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != payload["sha256"]:
                    return None
            return payload
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _scan(self):
        with os.scandir(self.root) as entries:
            yield from entries

    def mark_unreferenced(self, key, payload, when):
        """Authenticated retirement state; called only with inode and SQL fences held."""
        payload = dict(payload, unreferenced_at=when)
        signature = hmac.new(self.secret.encode(), _canonical(payload), "sha256").hexdigest()
        temporary = self.root / (key + ".receipt." + uuid4().hex + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(_canonical({"payload": payload, "signature": signature}))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.root / (key + ".receipt"))
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def cleanup_candidates(self, limit):
        # Keep a directory cursor between bounded sweeps so live early entries cannot starve orphans.
        if self._cleanup_iterator is None:
            self._cleanup_iterator = self._scan()
        for _ in range(limit):
            entry = next(self._cleanup_iterator, None)
            if entry is None:
                self._cleanup_iterator.close()
                self._cleanup_iterator = None
                return
            yield Path(entry.path)

    def read(self, key, tenant, source, revision, checksum):
        from knowledge.parsing import IngestionError

        if owned_key(key) != (tenant, source, revision) or self.root.is_symlink():
            raise IngestionError("blob_unavailable")
        try:
            fd = os.open(self.root / key, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
                    raise IngestionError("blob_unavailable")
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != checksum:
                raise IngestionError("blob_unavailable")
            return data
        except OSError:
            raise IngestionError("blob_unavailable") from None
