"""Private bounded bundle primitives; standalone stdlib source also runs in the volume view."""

import fcntl
import hashlib
import hmac
import io
import json
import math
import os
import re
import stat
import tarfile
from contextlib import ExitStack
from pathlib import Path

MAX_BLOB = 10 * 1024**2
MAX_BLOBS = 128
MAX_BLOB_TOTAL = 64 * 1024**2
MAX_ARCHIVE = 66 * 1024**2
MAX_MANIFEST = 1024**2
MAX_DUMP = 64 * 1024**2
MAX_ROLES = 1024**2
KEY_RE = re.compile(r"([0-9a-f]{32})-([0-9a-f]{32})-([1-9][0-9]{0,9})-([0-9a-f]{32})\.blob")
HEX_RE = re.compile(r"[0-9a-f]{64}")
PAYLOAD_KEYS = {
    "version",
    "key",
    "tenant_id",
    "source_id",
    "revision",
    "sha256",
    "size",
    "device",
    "inode",
    "created_at",
    "unreferenced_at",
}


class RecoveryError(Exception):
    """Fixed, non-sensitive reason; never attach subprocess/SQL/environment text."""


def require(condition, reason="recovery validation failed"):
    if not condition:
        raise RecoveryError(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def decode(data):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            require(key not in out)
            out[key] = value
        return out

    try:
        return json.loads(data, object_pairs_hook=unique, parse_constant=lambda _: require(False))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise RecoveryError("invalid metadata") from None


def private_directory(path):
    info = Path(path).lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o700
        and info.st_uid == os.geteuid(),
        "private directory required",
    )


def file_info(info, limit):
    require(
        stat.S_ISREG(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
        and info.st_uid == os.geteuid()
        and 0 <= info.st_size <= limit,
        "unsafe file or size",
    )


def stable_stat(info):
    # Reads may update atime on Linux; ownership, contents and mutation clocks must not change.
    return tuple(
        getattr(info, key)
        for key in (
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_dev",
            "st_ino",
            "st_ctime_ns",
            "st_mtime_ns",
        )
    )


def read_private(path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise RecoveryError("unsafe file") from None
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        file_info(before, limit)
        data = stream.read(limit + 1)
        require(
            len(data) == before.st_size
            and stable_stat(os.fstat(stream.fileno())) == stable_stat(before),
            "file changed",
        )
        return data


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def authenticate(key, data, receipt):
    require(isinstance(key, str) and KEY_RE.fullmatch(key) is not None, "invalid blob identity")
    record = decode(receipt)
    require(isinstance(record, dict) and set(record) == {"payload", "signature"})
    payload, signature = record["payload"], record["signature"]
    require(isinstance(payload, dict) and set(payload) == PAYLOAD_KEYS)
    require(isinstance(signature, str) and HEX_RE.fullmatch(signature) is not None)
    return payload, signature


def verify_receipt(key, data, receipt, secret):
    require(isinstance(secret, str) and len(secret) >= 32, "external blob key required")
    payload, signature = authenticate(key, data, receipt)
    expected = hmac.new(secret.encode(), canonical(payload), "sha256").hexdigest()
    require(hmac.compare_digest(signature, expected), "blob authentication failed")
    match = KEY_RE.fullmatch(key)
    assert match is not None
    require(type(payload["version"]) is int and payload["version"] == 1)
    require(
        (payload["key"], payload["tenant_id"], payload["source_id"], payload["revision"])
        == (key, match[1], match[2], int(match[3])),
        "blob identity mismatch",
    )
    require(type(payload["revision"]) is int)
    require(
        type(payload["size"]) is int
        and payload["size"] == len(data)
        and 0 < len(data) <= MAX_BLOB
        and payload["sha256"] == digest(data),
        "blob content mismatch",
    )
    require(
        all(type(payload[field]) is int and payload[field] >= 0 for field in ("device", "inode"))
    )
    for field in ("created_at", "unreferenced_at"):
        value = payload[field]
        require(
            (field == "unreferenced_at" and value is None)
            or (type(value) in (int, float) and math.isfinite(value) and value >= 0)
        )
    return payload


def capture_blobs(root, secret, output):
    """Read-only, nonblocking capture in original filesystem; no receipt rebinding here."""
    root = Path(root)
    private_directory(root)
    require(len(secret) >= 32, "external blob key required")
    try:
        with ExitStack() as stack:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, directory)
            names = []
            with os.scandir(directory) as entries:
                for entry in entries:
                    names.append(entry.name)
                    require(len(names) <= MAX_BLOBS * 2 + 1, "too many source files")
            # Empty never-used roots have no lock. Any nonempty root must have the existing lock.
            require(not names or ".upload.lock" in names, "source upload lock missing")
            if names:
                lock = os.open(
                    ".upload.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                stack.callback(os.close, lock)
                file_info(os.fstat(lock), 0)
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            keys = sorted(name for name in names if KEY_RE.fullmatch(name))
            require(
                set(names)
                == ({".upload.lock"} if names else set())
                | set(keys)
                | {key + ".receipt" for key in keys},
                "inflight or unexpected source file",
            )
            captured, records, opened, total = {}, {}, {}, 0
            for key in keys:
                fd = os.open(key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                stack.callback(os.close, fd)
                info = os.fstat(fd)
                file_info(info, MAX_BLOB)
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                total += info.st_size
                require(total <= MAX_BLOB_TOTAL, "source blob limit")
                file_info(os.stat(key + ".receipt", dir_fd=directory, follow_symlinks=False), 4096)
                opened[key] = (fd, info)
            # Admit all members/counts/aggregate sizes before the first content read.
            for key, (fd, info) in opened.items():
                with os.fdopen(os.dup(fd), "rb") as stream:
                    data = stream.read(MAX_BLOB + 1)
                receipt_fd = os.open(
                    key + ".receipt", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                with os.fdopen(receipt_fd, "rb") as stream:
                    rinfo = os.fstat(stream.fileno())
                    file_info(rinfo, 4096)
                    receipt = stream.read(4097)
                    require(stable_stat(os.fstat(stream.fileno())) == stable_stat(rinfo))
                payload = verify_receipt(key, data, receipt, secret)
                require(
                    (info.st_dev, info.st_ino, info.st_size)
                    == (payload["device"], payload["inode"], payload["size"]),
                    "original inode mismatch",
                )
                require(stable_stat(os.fstat(fd)) == stable_stat(info), "source changed")
                captured[key] = dict(
                    device=info.st_dev,
                    inode=info.st_ino,
                    size=info.st_size,
                    ctime_ns=info.st_ctime_ns,
                    receipt_inode=rinfo.st_ino,
                    receipt_ctime_ns=rinfo.st_ctime_ns,
                    sha256=digest(data),
                    receipt_sha256=digest(receipt),
                )
                archive_key = digest(key.encode()) + ".blob"
                records[archive_key] = data
                records[archive_key + ".receipt"] = receipt
            require(set(os.listdir(directory)) == set(names), "source changed")
            for fd, info in opened.values():
                require(stable_stat(os.fstat(fd)) == stable_stat(info), "source changed")
            records["capture.json"] = canonical(captured)
            with tarfile.open(fileobj=output, mode="w|", format=tarfile.USTAR_FORMAT) as archive:
                for name, data in records.items():
                    member = tarfile.TarInfo(name)
                    member.size, member.mode = len(data), 0o600
                    archive.addfile(member, io.BytesIO(data))
            return captured
    except (OSError, ValueError, TypeError, KeyError):
        raise RecoveryError("source capture refused") from None


def verify_blobs(data, secret):
    """Parse bounded plain tar headers ourselves; no PAX/GNU extension allocation or extraction."""
    require(isinstance(secret, str) and len(secret) >= 32, "external blob key required")
    require(len(data) <= MAX_ARCHIVE and len(data) % 512 == 0, "archive limit or truncation")
    records: dict[str, bytes] = {}
    offset, total = 0, 0
    while True:
        require(offset + 1024 <= len(data), "truncated archive")
        header = data[offset : offset + 512]
        if header == bytes(512):
            require(not any(data[offset:]), "archive trailing content")
            break
        try:
            member = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
        except (ValueError, UnicodeError, tarfile.TarError):
            raise RecoveryError("invalid archive header") from None
        require(
            member.type in (tarfile.REGTYPE, tarfile.AREGTYPE)
            and not member.linkname
            and member.mode == 0o600,
            "unsafe archive member",
        )
        name = member.name
        key = name.removesuffix(".receipt")
        require(
            name == "capture.json" or re.fullmatch(r"[0-9a-f]{64}\.blob", key) is not None,
            "invalid archive path",
        )
        require(
            name not in records and len(records) < MAX_BLOBS * 2 + 1, "archive duplicate or count"
        )
        limit = (
            MAX_MANIFEST
            if name == "capture.json"
            else (4096 if name.endswith(".receipt") else MAX_BLOB)
        )
        require(0 <= member.size <= limit, "archive member size")
        total += member.size
        require(total <= MAX_BLOB_TOTAL + MAX_MANIFEST + MAX_BLOBS * 4096, "archive total size")
        offset += 512
        end = offset + member.size
        padded = offset + ((member.size + 511) // 512) * 512
        require(padded <= len(data) and not any(data[end:padded]), "archive truncation or padding")
        records[name] = data[offset:end]
        offset = padded
    require("capture.json" in records, "capture metadata missing")
    capture = decode(records.pop("capture.json"))
    require(isinstance(capture, dict) and len(capture) <= MAX_BLOBS)
    names = {key: digest(key.encode()) + ".blob" for key in capture}
    require(
        set(records) == set(names.values()) | {name + ".receipt" for name in names.values()},
        "unpaired blob",
    )
    records = {
        key + suffix: records[name + suffix]
        for key, name in names.items()
        for suffix in ("", ".receipt")
    }
    for key, info in capture.items():
        payload = verify_receipt(key, records[key], records[key + ".receipt"], secret)
        require(
            isinstance(info, dict)
            and set(info)
            == {
                "device",
                "inode",
                "size",
                "ctime_ns",
                "receipt_inode",
                "receipt_ctime_ns",
                "sha256",
                "receipt_sha256",
            }
        )
        require(all(info[k] == payload[k] for k in ("device", "inode", "size", "sha256")))
        require(info["receipt_sha256"] == digest(records[key + ".receipt"]))
        require(
            all(
                type(info[k]) is int and info[k] >= 0
                for k in ("ctime_ns", "receipt_inode", "receipt_ctime_ns")
            )
        )
    return records, capture


def restore_blobs(verified, target, secret):
    require(isinstance(secret, str) and len(secret) >= 32, "external blob key required")
    records, capture = verified
    payloads = {
        key: verify_receipt(key, records[key], records[key + ".receipt"], secret) for key in capture
    }
    target = Path(target)
    private_directory(target.parent)
    try:
        target.mkdir(mode=0o700)
    except OSError:
        raise RecoveryError("new private restore directory required") from None
    for key, payload in payloads.items():
        write_private(target / key, records[key])
        info = (target / key).stat()
        rebound = dict(payload, device=info.st_dev, inode=info.st_ino)
        signature = hmac.new(secret.encode(), canonical(rebound), "sha256").hexdigest()
        write_private(
            target / (key + ".receipt"), canonical(dict(payload=rebound, signature=signature))
        )
    write_private(target / ".upload.lock", b"")
