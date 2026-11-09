"""Opt-in operator recovery; no startup imports, owner dotenv loading, or restore DSN."""

import argparse
import contextlib
import os
import re
import resource
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from scripts import recovery_bundle as bundle_io
from scripts.recovery_bundle import (
    HEX_RE,
    MAX_ARCHIVE,
    MAX_DUMP,
    MAX_MANIFEST,
    MAX_ROLES,
    RecoveryError,
    canonical,
    decode,
    digest,
    private_directory,
    read_private,
    require,
    restore_blobs,
    verify_blobs,
    write_private,
)

IMAGE = "pgvector/pgvector:0.8.7-pg16-bookworm@sha256:7b822b0aac60967beb1ea5e576b8602c94c300a157d187f385ae3e0da199b90a"
LABEL = "voice-agent.recovery"
SOCKET_DIR = "/var/run/postgresql"
TMPFS_MIB = {"/var/lib/postgresql/data": 256, SOCKET_DIR: 8, "/tmp": 16}
TMPFS = {
    path: f"rw,noexec,nosuid,nodev,size={size}m,uid=999,gid=999,mode=0700"
    for path, size in TMPFS_MIB.items()
}
MOUNT_PROBE = """set -eu
for path do
  resolved=$(readlink -f -- "$path")
  printf '%s %s ' "$path" "$resolved"
  stat -c '%u:%g:%a' -- "$path"
  awk -v path="$resolved" '$5 == path { print }' /proc/self/mountinfo
done
"""
FILE_LIMITS = {
    "database.dump": MAX_DUMP,
    "roles.sql": MAX_ROLES,
    "state.json": MAX_MANIFEST,
    "blobs.tar": MAX_ARCHIVE,
}
EXTERNAL_KEYS = ["knowledge_blob_hmac", "jwt", "runtime", "worker_and_provider_credentials"]
# Deliberately project only non-secret fields; never fetch Config.Env or full inspect output.
INSPECT_FIELDS = {
    "id": ".Id",
    "name": ".Name",
    "image": ".Config.Image",
    "image_id": ".Image",
    "labels": ".Config.Labels",
    "running": ".State.Running",
    "network": ".HostConfig.NetworkMode",
    "ports": ".NetworkSettings.Ports",
    "bindings": ".HostConfig.PortBindings",
    "mounts": ".Mounts",
    "bind_mounts": '(index .HostConfig "Binds")',
    "declared_mounts": '(index .HostConfig "Mounts")',
    # Docker omits this key for containers without tmpfs; targets still require the exact map.
    "tmpfs": '(index .HostConfig "Tmpfs")',
    "memory": ".HostConfig.Memory",
    "swap": ".HostConfig.MemorySwap",
    "nanocpus": ".HostConfig.NanoCpus",
    "readonly": ".HostConfig.ReadonlyRootfs",
    "user": ".Config.User",
    "privileged": ".HostConfig.Privileged",
    "capadd": ".HostConfig.CapAdd",
    "capdrop": ".HostConfig.CapDrop",
    "security": ".HostConfig.SecurityOpt",
    "pidmode": ".HostConfig.PidMode",
    "ipcmode": ".HostConfig.IpcMode",
    "pids": ".HostConfig.PidsLimit",
}
INSPECT_FORMAT = (
    "{" + ",".join('"' + k + '":{{json ' + v + "}}" for k, v in INSPECT_FIELDS.items()) + "}"
)


class Runner:
    """Bounded output to anonymous private files, hard wall/total deadlines, sanitized errors."""

    def __init__(self):
        self.deadline = time.monotonic() + 600

    def run(self, argv, *, data=None, limit=MAX_MANIFEST, timeout=60, cleanup=False):
        remaining = 15 if cleanup else self.deadline - time.monotonic()
        require(remaining > 0, "operation deadline")
        stop = time.monotonic() + min(timeout, remaining)
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as incoming:
            if data is not None:
                incoming.write(data)
                incoming.seek(0)
            try:

                def file_limit():
                    resource.setrlimit(resource.RLIMIT_FSIZE, (limit + 1, limit + 1))
                    os.umask(0o077)

                process = subprocess.Popen(
                    argv,
                    stdin=incoming,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    preexec_fn=file_limit,
                    env={
                        k: v
                        for k, v in os.environ.items()
                        if k in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}
                    },
                )
                try:
                    while process.poll() is None:
                        require(time.monotonic() < stop, "command deadline")
                        require(os.fstat(output.fileno()).st_size <= limit, "command output limit")
                        time.sleep(0.01)
                    require(process.returncode == 0, "command failed")
                    require(os.fstat(output.fileno()).st_size <= limit, "command output limit")
                finally:
                    if process.poll() is None:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                output.seek(0)
                return output.read(limit + 1)
            except (OSError, subprocess.SubprocessError):
                raise RecoveryError("command unavailable") from None


def inspect(runner, cid, *, cleanup=False):
    require(isinstance(cid, str) and HEX_RE.fullmatch(cid) is not None, "container id required")
    info = decode(
        runner.run(["docker", "inspect", "--format", INSPECT_FORMAT, cid], cleanup=cleanup)
    )
    require(info["id"] == cid, "container identity mismatch")
    return info


def validate_target(info, cid, token, image_id):
    require(
        info["id"] == cid
        and info["name"] == "/voice-recovery-" + token
        and info["labels"].get(LABEL) == token,
        "restore target identity",
    )
    require(info["image"] == IMAGE and info["image_id"] == image_id, "restore image mismatch")
    require(
        info["network"] == "none"
        and not info["bindings"]
        and all(value is None for value in (info["ports"] or {}).values()),
        "restore network isolation",
    )
    mounts = info["mounts"]
    require(
        info["tmpfs"] == TMPFS
        and info["bind_mounts"] in (None, [])
        and info["declared_mounts"] in (None, [])
        and isinstance(mounts, list)
        # Docker may omit CLI --tmpfs entries here; verify kernel mounts after start.
        and (
            not mounts
            or (
                len(mounts) == len(TMPFS)
                and {item["Destination"] for item in mounts} == set(TMPFS)
                and all(item["Type"] == "tmpfs" for item in mounts)
            )
        ),
        "restore storage isolation",
    )
    require(
        info["memory"] == info["swap"] == 512 * 1024**2
        and info["nanocpus"] == 1000000000
        and info["pids"] == 128,
        "restore resource limits",
    )
    require(
        info["readonly"] is True
        and info["user"] == "999:999"
        and info["privileged"] is False
        and not info["capadd"]
        and info["capdrop"] == ["ALL"]
        and info["security"] == ["no-new-privileges"]
        and info["pidmode"] == ""
        and info["ipcmode"] == "private",
        "restore process isolation",
    )


def verify_target_mounts(runner, cid, *, allow_socket_bootstrap_mode=False):
    lines = (
        runner.run(
            ["docker", "exec", cid, "sh", "-ec", MOUNT_PROBE, "recovery-mount-check", *TMPFS],
            limit=8192,
        )
        .decode()
        .splitlines()
    )
    reason = "restore runtime storage isolation"
    require(len(lines) == 2 * len(TMPFS), reason)
    devices = set()
    reset_socket_mode = False
    for (path, size), directory, mount in zip(
        TMPFS_MIB.items(), lines[::2], lines[1::2], strict=True
    ):
        path_info = directory.split()
        require(len(path_info) == 3 and path_info[0] == path, reason)
        resolved, owner_mode = path_info[1:]
        require(resolved == path or (path == SOCKET_DIR and resolved == "/run/postgresql"), reason)
        bootstrap_mode = (
            path == SOCKET_DIR and allow_socket_bootstrap_mode and owner_mode == "999:999:3775"
        )
        require(owner_mode == "999:999:700" or bootstrap_mode, reason)
        reset_socket_mode |= bootstrap_mode

        before, separator, after = mount.partition(" - ")
        fields, filesystem = before.split(), after.split()
        require(
            bool(separator)
            and len(fields) >= 6
            and len(filesystem) == 3
            and fields[3:5] == ["/", resolved]
            and filesystem[:2] == ["tmpfs", "tmpfs"],
            reason,
        )
        device = fields[2]
        require(
            re.fullmatch(r"0:[1-9][0-9]*", device) is not None and device not in devices, reason
        )
        devices.add(device)
        require(
            {"rw", "noexec", "nosuid", "nodev"} <= set(fields[5].split(","))
            and {"rw", f"size={size * 1024}k", "mode=700", "uid=999", "gid=999"}
            <= set(filesystem[2].split(",")),
            reason,
        )
    return reset_socket_mode


def seal_bundle(bundle, receipt, secret, *, observations=None):
    private_directory(bundle)
    private_directory(receipt.parent)
    require(receipt.parent.resolve() != bundle.resolve(), "creation receipt must be outside bundle")
    files: dict[str, dict[str, int | str]] = {}
    manifest = dict(
        version=1,
        database="voice_agent",
        image=IMAGE,
        plaintext=True,
        external_keys=EXTERNAL_KEYS,
        files=files,
    )
    for name, limit in FILE_LIMITS.items():
        content = read_private(bundle / name, limit)
        files[name] = dict(size=len(content), sha256=digest(content))
    del content
    raw = canonical(manifest)
    write_private(bundle / "manifest.json", raw)
    expected = digest(raw)
    verify_bundle(bundle, expected, secret, allowed=True)
    write_private(
        receipt,
        canonical(
            dict(
                version=1,
                manifest_sha256=expected,
                created_unix=time.time(),
                database="voice_agent",
                observations=observations or {},
            )
        ),
    )
    return expected


def verify_bundle(bundle, expected, secret, *, allowed=False):
    require(allowed, "explicit opt-in required")
    require(
        isinstance(expected, str) and HEX_RE.fullmatch(expected) is not None,
        "trusted manifest hash required",
    )
    host_admission()
    private_directory(bundle)
    names = set()
    with os.scandir(bundle) as entries:
        for entry in entries:
            names.add(entry.name)
            require(len(names) <= 5, "bundle file count")
    require(names == set(FILE_LIMITS) | {"manifest.json"}, "unexpected bundle members")
    raw = read_private(bundle / "manifest.json", MAX_MANIFEST)
    require(digest(raw) == expected, "trusted manifest hash mismatch")
    manifest = decode(raw)
    require(
        isinstance(manifest, dict)
        and set(manifest) == {"version", "database", "image", "plaintext", "external_keys", "files"}
    )
    require(
        type(manifest["version"]) is int
        and manifest["version"] == 1
        and manifest["database"] == "voice_agent"
        and manifest["image"] == IMAGE
        and manifest["plaintext"] is True
        and manifest["external_keys"] == EXTERNAL_KEYS,
        "unsupported bundle profile",
    )
    require(isinstance(manifest["files"], dict) and set(manifest["files"]) == set(FILE_LIMITS))
    # Validate ALL declared bounds before the first content read.
    for name, limit in FILE_LIMITS.items():
        entry = manifest["files"][name]
        require(isinstance(entry, dict) and set(entry) == {"size", "sha256"})
        require(
            type(entry["size"]) is int
            and 0 < entry["size"] <= limit
            and isinstance(entry["sha256"], str)
            and HEX_RE.fullmatch(entry["sha256"]) is not None
        )
    files = {}
    for name, limit in FILE_LIMITS.items():
        content = read_private(bundle / name, limit)
        require(
            len(content) == manifest["files"][name]["size"]
            and digest(content) == manifest["files"][name]["sha256"],
            "bundle content mismatch",
        )
        files[name] = content
    require(files["database.dump"].startswith(b"PGDMP"), "custom database dump required")
    return dict(
        files=files,
        blobs=verify_blobs(files["blobs.tar"], secret),
        state=decode(files["state.json"]),
    )


def local_docker(runner):
    host, context = os.getenv("DOCKER_HOST", ""), os.getenv("DOCKER_CONTEXT", "")
    require(not host or host.startswith("unix:///"), "local Docker socket required")
    if not host or context:
        endpoint = decode(
            runner.run(
                ["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"],
                limit=4096,
            )
        )
        require(
            isinstance(endpoint, str) and endpoint.startswith("unix:///"),
            "local Docker context required",
        )


def validate_source_containers(db, api, image_id):
    for info, service in ((db, "postgres"), (api, "api")):
        require(
            info["running"]
            and info["labels"].get("com.docker.compose.project") == "voice-agent-phase1"
            and info["labels"].get("com.docker.compose.service") == service,
            "source container identity",
        )
    require(db["image"] == IMAGE and db["image_id"] == image_id, "source database image")
    require(
        db["ports"] == {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]},
        "source database port",
    )
    for info, destination, name in (
        (db, "/var/lib/postgresql/data", "postgres-dev-data"),
        (api, "/var/lib/knowledge", "knowledge-private-data"),
    ):
        mounts = [item for item in info["mounts"] if item["Destination"] == destination]
        require(
            len(mounts) == 1
            and mounts[0]["Type"] == "volume"
            and mounts[0]["Name"] == "voice-agent-phase1_" + name,
            "source volume identity",
        )


def validate_source(db, api, dsn, image_id):
    from urllib.parse import urlsplit

    validate_source_containers(db, api, image_id)
    parsed = urlsplit(dsn)
    require(
        parsed.scheme == "postgresql"
        and parsed.hostname in {"127.0.0.1", "::1"}
        and parsed.port == 55432
        and parsed.path == "/voice_agent"
        and parsed.username == "postgres"
        and bool(parsed.password)
        and not parsed.query
        and not parsed.fragment,
        "fixed source database profile required",
    )


def capture_source(runner, cid, secret, *, metadata_only=False):
    # Only the stdlib helper is injected, not workspace code/imports or a bind mount.
    helper = Path(bundle_io.__file__).read_text()
    program = (
        helper
        + "\nimport signal,sys\nsignal.alarm(60)\ntry:\n    capture_blobs(Path('/var/lib/knowledge'), RECOVERY_KEY, sys.stdout.buffer)\nexcept BaseException:\n    sys.exit(1)\n"
    )
    if metadata_only:
        program = (
            helper
            + "\nimport signal,sys\nsignal.alarm(60)\nclass Discard:\n    def write(self, data): return len(data)\ntry:\n    result=capture_blobs(Path('/var/lib/knowledge'), RECOVERY_KEY, Discard())\n    sys.stdout.buffer.write(canonical(result))\nexcept BaseException:\n    sys.exit(1)\n"
        )
    envelope = canonical(dict(source=program, key=secret))
    launcher = "import json,sys; p=json.load(sys.stdin); exec(compile(p['source'],'<recovery-helper>','exec'), {'RECOVERY_KEY':p['key']})"
    return runner.run(
        ["docker", "exec", "-i", cid, "/app/.venv/bin/python", "-c", launcher],
        data=envelope,
        limit=MAX_MANIFEST if metadata_only else MAX_ARCHIVE,
        timeout=65,
    )


def psql(runner, cid, user, database, statement, *, timeout=60):
    require(re.fullmatch(r"postgres|recovery_[0-9a-f]{32}", user) is not None)
    require(re.fullmatch(r"voice_agent|recovery_[0-9a-f]{32}", database) is not None)
    return runner.run(
        [
            "docker",
            "exec",
            "-i",
            "-e",
            "PGOPTIONS=-c statement_timeout=30000 -c lock_timeout=2000 -c timezone=UTC -c datestyle=ISO,YMD",
            cid,
            "psql",
            "--no-password",
            "-X",
            "-qAt",
            "--set=ON_ERROR_STOP=1",
            "--username",
            user,
            "--dbname",
            database,
        ],
        data=statement.encode() if isinstance(statement, str) else statement,
        timeout=timeout,
    )


def backup(args, runner):
    from scripts.recovery_database import (
        check_references,
        database_state,
        role_state,
        sequence_state,
    )

    local_docker(runner)
    image_id = (
        runner.run(["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE]).decode().strip()
    )
    source_db, source_api = inspect(runner, args.source_postgres), inspect(runner, args.source_api)
    validate_source_containers(source_db, source_api, image_id)
    # Deliberately load operator secrets only AFTER source/container/volume guards.
    dsn, secret = os.getenv("RECOVERY_SOURCE_DSN", ""), os.getenv("RECOVERY_BLOB_SECRET", "")
    validate_source(source_db, source_api, dsn, image_id)
    require(len(secret) >= 32, "external blob key required")
    private_directory(args.bundle.parent)
    private_directory(args.creation_receipt.parent)
    require(not args.creation_receipt.exists(), "new creation receipt required")
    host_admission()
    args.bundle.mkdir(mode=0o700)
    started = time.monotonic()
    observations = {"capture_started_unix": time.time()}
    before = capture_source(runner, args.source_api, secret)
    blobs = verify_blobs(before, secret)
    from urllib.parse import urlsplit

    import psycopg

    try:
        with psycopg.connect(
            dsn,
            autocommit=True,
            hostaddr=urlsplit(dsn).hostname,
            connect_timeout=5,
            options="-c default_transaction_read_only=on -c statement_timeout=30000 -c lock_timeout=2000 -c timezone=UTC -c datestyle=ISO,YMD",
        ) as connection:

            def query(statement):
                require(time.monotonic() < runner.deadline, "operation deadline")
                row = connection.execute(
                    "SELECT CASE WHEN octet_length(result::text) <= 1048576 THEN result ELSE NULL END FROM ("
                    + statement
                    + ") bounded(result)"
                ).fetchone()
                require(time.monotonic() < runner.deadline, "operation deadline")
                assert row is not None
                value = row[0]
                require(value is not None, "database metadata limit")
                return value

            system = query("SELECT to_jsonb(system_identifier::text) FROM pg_control_system()")
            require(
                system
                == decode(
                    psql(
                        runner,
                        args.source_postgres,
                        "postgres",
                        "voice_agent",
                        "SELECT to_jsonb(system_identifier::text) FROM pg_control_system()",
                    )
                ),
                "source connection identity",
            )
            roles_before = role_state(query)
            connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot_row = connection.execute("SELECT pg_export_snapshot()").fetchone()
            assert snapshot_row is not None
            snapshot = snapshot_row[0]
            observations["snapshot_exported_unix"] = time.time()
            require(re.fullmatch(r"[0-9A-Fa-f-]+", snapshot) is not None)
            state = database_state(query)
            check_references(state, blobs, secret)
            dump = runner.run(
                [
                    "docker",
                    "exec",
                    "-e",
                    "PGOPTIONS=-c statement_timeout=60000 -c lock_timeout=2000",
                    args.source_postgres,
                    "pg_dump",
                    "--format=custom",
                    "--create",
                    "--snapshot",
                    snapshot,
                    "--username",
                    "postgres",
                    "--dbname",
                    "voice_agent",
                ],
                limit=MAX_DUMP,
                timeout=65,
            )
            roles = runner.run(
                [
                    "docker",
                    "exec",
                    "-e",
                    "PGOPTIONS=-c statement_timeout=30000 -c lock_timeout=2000",
                    args.source_postgres,
                    "pg_dumpall",
                    "--roles-only",
                    "--username",
                    "postgres",
                    "--database=voice_agent",
                ],
                limit=MAX_ROLES,
            )
            require(sequence_state(query) == state["sequences"], "source sequences changed")
            connection.execute("ROLLBACK")
            require(role_state(query) == roles_before, "source roles changed")
    except psycopg.Error:
        raise RecoveryError("source database capture failed") from None
    after = capture_source(runner, args.source_api, secret, metadata_only=True)
    require(decode(after) == blobs[1], "source blobs changed")
    # Detect source container/volume replacement at the other end of the capture.
    validate_source_containers(
        inspect(runner, args.source_postgres), inspect(runner, args.source_api), image_id
    )
    state["roles"] = roles_before
    for name, content in {
        "database.dump": dump,
        "roles.sql": roles,
        "state.json": canonical(state),
        "blobs.tar": before,
    }.items():
        write_private(args.bundle / name, content)
    del before, after, blobs, dump, roles, content
    observations["capture_seconds"] = round(time.monotonic() - started, 3)
    seal_bundle(args.bundle, args.creation_receipt, secret, observations=observations)
    print("recovery: backup created; trusted hash is in the separate private creation receipt")
    return dict(status="created", seconds=round(time.monotonic() - started, 3))


def host_admission():
    # Installed host dev tool only: do not add psutil to runtime or import it at startup.
    import psutil  # type: ignore[import-untyped]

    require(psutil.virtual_memory().available >= 1024**3, "host memory headroom")


def resource_admission(runner):
    host_admission()
    total = int(runner.run(["docker", "info", "--format", "{{.MemTotal}}"], limit=64))
    samples = (
        runner.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}"], limit=65536)
        .decode()
        .splitlines()
    )
    used = 0.0
    scales = {
        "B": 1,
        "kB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
    }
    for sample in samples:
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(B|kB|MB|GB|KiB|MiB|GiB) / .+", sample)
        require(match is not None, "Docker memory sample unavailable")
        assert match is not None
        used += float(match[1]) * scales[match[2]]
    require(total - used >= 1024**3, "Docker memory headroom")


def target_state(runner, cid, user, expected):
    from scripts.recovery_database import database_state, role_state

    def query(statement):
        return decode(psql(runner, cid, user, "voice_agent", statement))

    state = database_state(query)
    state["roles"] = role_state(query, exclude=user)
    if state["tables"] != expected["tables"]:
        # pg_dump can omit an explicit owner-only ACL equal to the built-in default.
        # Compare only that representation, using the actual target table and owner.
        defaults = query(
            "SELECT COALESCE(jsonb_object_agg(c.relname, jsonb_build_object("
            "'owner', pg_catalog.pg_get_userbyid(c.relowner), "
            "'acl', pg_catalog.acldefault('r', c.relowner))), '{}'::jsonb) "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind IN ('r','p') AND c.relacl IS NULL"
        )
        saved_tables = {table["name"]: table for table in expected["tables"]}
        for table in state["tables"]:
            saved = saved_tables.get(table["name"])
            default = defaults.get(table["name"])
            if (
                table["acl"] is None
                and saved is not None
                and default is not None
                and saved["owner"] == table["owner"] == default["owner"]
                and isinstance(saved["acl"], list)
                and len(saved["acl"]) == 1
                and saved["acl"] == default["acl"]
            ):
                table["acl"] = saved["acl"]
    return state


def check_rls(runner, cid, user, state):
    from scripts.recovery_database import rls_queries

    safe = decode(
        psql(
            runner,
            cid,
            user,
            "voice_agent",
            "SELECT to_jsonb(NOT (rolsuper OR rolbypassrls)) FROM pg_roles WHERE rolname='scheduler_app'",
        )
    )
    require(safe is True, "restored app role bypasses RLS")
    for statement in rls_queries(state):
        require(
            decode(psql(runner, cid, user, "voice_agent", statement)) == 0,
            "restored RLS scope failed",
        )


def drill(verified, scratch_parent, secret, runner):
    import shutil

    from scripts.recovery_database import check_references

    private_directory(scratch_parent)
    local_docker(runner)
    resource_admission(runner)
    image_id = (
        runner.run(["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE]).decode().strip()
    )
    token, cid = uuid4().hex, None
    user = "recovery_" + token
    scratch = Path(tempfile.mkdtemp(prefix="voice-recovery-", dir=scratch_parent))
    started = time.monotonic()
    cidfile = scratch / "container.id"
    try:
        restore_blobs(verified["blobs"], scratch / "blobs", secret)
        argv = [
            "docker",
            "create",
            "--pull=never",
            "--cidfile",
            str(cidfile),
            "--name",
            "voice-recovery-" + token,
            "--label",
            LABEL + "=" + token,
            "--network",
            "none",
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
            "--cpus",
            "1",
            "--pids-limit",
            "128",
            "--read-only",
            "--user",
            "999:999",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--ipc",
            "private",
        ]
        for path, options in TMPFS.items():
            argv.extend(["--tmpfs", path + ":" + options])
        argv.extend(
            [
                "-e",
                "POSTGRES_USER=" + user,
                "-e",
                "POSTGRES_DB=" + user,
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                IMAGE,
            ]
        )
        created = runner.run(argv).decode().strip()
        require(
            HEX_RE.fullmatch(created) is not None
            and read_private(cidfile, 65).decode().strip() == created,
            "created container identity missing",
        )
        cid = created  # Never assign a name/preexisting id; only a successful create result.
        validate_target(inspect(runner, cid), cid, token, image_id)
        runner.run(["docker", "start", cid])
        verify_target_mounts(runner, cid, allow_socket_bootstrap_mode=True)
        deadline = time.monotonic() + 30
        while True:
            try:
                ready = decode(
                    psql(
                        runner,
                        cid,
                        user,
                        user,
                        "SELECT json_build_object('database',current_database(),'ready',current_setting('listen_addresses')='*')",
                        timeout=3,
                    )
                )
                require(ready == {"database": user, "ready": True}, "restore initializing")
                break
            except RecoveryError:
                require(time.monotonic() < deadline, "restore startup deadline")
                time.sleep(0.25)
        info = inspect(runner, cid)
        validate_target(info, cid, token, image_id)
        require(info["running"], "restore target not running")
        if verify_target_mounts(runner, cid, allow_socket_bootstrap_mode=True):
            # The pinned PostgreSQL entrypoint sets its socket directory to 03775.
            runner.run(["docker", "exec", cid, "chmod", "00700", SOCKET_DIR])
        verify_target_mounts(runner, cid)
        psql(runner, cid, user, user, verified["files"]["roles.sql"])
        # SQL, including psql metacommands and COPY PROGRAM, runs only inside this isolated container.
        validate_target(inspect(runner, cid), cid, token, image_id)
        verify_target_mounts(runner, cid)
        runner.run(
            [
                "docker",
                "exec",
                "-i",
                "-e",
                "PGOPTIONS=-c statement_timeout=30000 -c lock_timeout=2000",
                cid,
                "pg_restore",
                "--exit-on-error",
                "--create",
                "--use-set-session-authorization",
                "--no-password",
                "--host",
                SOCKET_DIR,
                "--username",
                "postgres",
                "--dbname",
                user,
            ],
            data=verified["files"]["database.dump"],
            timeout=120,
        )
        state = target_state(runner, cid, user, verified["state"])
        require(state == verified["state"], "restored state mismatch")
        check_rls(runner, cid, user, state)
        check_references(state, verified["blobs"], secret)
        # Re-open actual rebound files through the unchanged live verifier.
        from knowledge.storage import BlobStore

        for key in verified["blobs"][1]:
            with (scratch / "blobs" / key).open("rb") as stream:
                require(
                    BlobStore(scratch / "blobs", secret=secret).receipt(key, stream.fileno())
                    is not None,
                    "restored blob ownership failed",
                )
        validate_target(inspect(runner, cid), cid, token, image_id)
        verify_target_mounts(runner, cid)
        return dict(
            status="verified",
            seconds=round(time.monotonic() - started, 3),
            blobs=len(verified["blobs"][1]),
        )
    finally:
        try:
            if cid is None and cidfile.exists():
                candidate = read_private(cidfile, 65).decode().strip()
                require(
                    HEX_RE.fullmatch(candidate) is not None, "created container receipt invalid"
                )
                cid = candidate
            if cid is not None:
                owned = inspect(runner, cid, cleanup=True)
                require(
                    owned["id"] == cid
                    and owned["name"] == "/voice-recovery-" + token
                    and owned["labels"].get(LABEL) == token,
                    "cleanup ownership mismatch",
                )
                runner.run(["docker", "rm", "--force", cid], timeout=15, cleanup=True)
        finally:
            shutil.rmtree(scratch)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--allow-local-operations" not in argv:
        print("recovery: explicit --allow-local-operations required", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "verify", "drill"))
    parser.add_argument("--allow-local-operations", action="store_true", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--creation-receipt", type=Path)
    parser.add_argument("--drill-receipt", type=Path)
    parser.add_argument("--source-postgres")
    parser.add_argument("--source-api")
    parser.add_argument("--scratch-parent", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.operation == "backup":
            require(
                args.source_postgres and args.source_api and args.creation_receipt,
                "backup source ids and creation receipt required",
            )
            backup(args, Runner())
        else:
            require(args.expected_manifest_sha256, "trusted manifest hash required")
            secret = os.getenv("RECOVERY_BLOB_SECRET", "")
            verified = verify_bundle(
                args.bundle, args.expected_manifest_sha256, secret, allowed=True
            )
            if args.operation == "drill":
                require(
                    args.scratch_parent is not None and args.drill_receipt is not None,
                    "private scratch parent and drill receipt required",
                )
                private_directory(args.drill_receipt.parent)
                require(not args.drill_receipt.exists(), "new drill receipt required")
                result = drill(verified, args.scratch_parent, secret, Runner())
                result.update(
                    manifest_sha256=args.expected_manifest_sha256, completed_unix=time.time()
                )
                write_private(args.drill_receipt, canonical(result))
            print("recovery: " + args.operation + " verified")
        return 0
    except (RecoveryError, OSError, ValueError, TypeError, KeyError, ImportError):
        # Even malformed metadata/OS/SQL exceptions must not print paths, values, DSNs or rows.
        print("recovery: operation refused or failed; no acceptance receipt", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
