"""Bounded private raw uploads. No request-controlled paths or SQL locks."""

import asyncio
import fcntl
import hashlib
import os
import shutil
import stat
from contextlib import suppress
from pathlib import Path
from uuid import UUID, uuid4

from scheduler.errors import AppError

MAX_BYTES = 10 * 1024 * 1024
UPLOAD_SECONDS = 30.0
QUOTA_BYTES = 1024**3
DISK_RESERVE = 4 * 1024**3


class BlobStore:
    def __init__(self, root: Path):
        self.root = root

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

    async def receive(self, stream, tenant: UUID, source: UUID, revision: int, checksum: str):
        temporary = self.root / (uuid4().hex + ".tmp")
        key = f"{tenant.hex}-{source.hex}-{revision}-{uuid4().hex}.blob"
        lock_fd = None
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
                    self.usage() + MAX_BYTES > QUOTA_BYTES
                    or shutil.disk_usage(self.root).free < DISK_RESERVE + MAX_BYTES
                ):
                    raise AppError("storage_capacity", 507)
                digest, size = hashlib.sha256(), 0
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as output:
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
                return key
        except TimeoutError:
            raise AppError("upload_timeout", 408) from None
        except OSError:
            raise AppError("storage_unavailable", 503) from None
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            if lock_fd is not None:
                os.close(lock_fd)
