"""Synthetic recovery fixtures: never consult dotenv, Docker, owner data or credentials."""

import hashlib
import hmac
import importlib.util
import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def synthetic_memory(monkeypatch):
    import psutil

    for name in tuple(os.environ):
        if name.startswith("PG"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(
        psutil, "virtual_memory", lambda: type("Memory", (), {"available": 2 * 1024**3})()
    )


SECRET = "synthetic-recovery-secret-" * 2
KEY = "1" * 32 + "-" + "2" * 32 + "-1-" + "3" * 32 + ".blob"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def module():
    assert importlib.util.find_spec("scripts.recovery_bundle") is not None, (
        "recovery boundary missing"
    )
    from scripts import recovery_bundle

    return recovery_bundle


def original(root):
    root.mkdir(mode=0o700)
    data = b"synthetic private original"
    blob = root / KEY
    blob.write_bytes(data)
    blob.chmod(0o600)
    info = blob.stat()
    payload = dict(
        version=1,
        key=KEY,
        tenant_id="1" * 32,
        source_id="2" * 32,
        revision=1,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        device=info.st_dev,
        inode=info.st_ino,
        created_at=123.5,
        unreferenced_at=456.5,
    )
    receipt = canonical(
        {
            "payload": payload,
            "signature": hmac.new(SECRET.encode(), canonical(payload), "sha256").hexdigest(),
        }
    )
    (root / (KEY + ".receipt")).write_bytes(receipt)
    (root / (KEY + ".receipt")).chmod(0o600)
    (root / ".upload.lock").touch(mode=0o600)
    return payload, receipt


def archive(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as output:
        for name, data, kind in entries:
            member = tarfile.TarInfo(name)
            member.mode = 0o600
            member.size = len(data)
            member.type = kind
            output.addfile(member, io.BytesIO(data))
    return stream.getvalue()


def test_capture_authenticates_original_and_rebinds_only_copy(tmp_path):
    m = module()
    source = tmp_path / "source"
    payload, receipt = original(source)
    out = io.BytesIO()
    before = m.capture_blobs(source, SECRET, out)
    assert before[KEY]["inode"] == source.joinpath(KEY).stat().st_ino
    captured = out.getvalue()
    verified = m.verify_blobs(captured, SECRET)
    target = tmp_path / "new-copy"
    m.restore_blobs(verified, target, SECRET)
    copied = json.loads((target / (KEY + ".receipt")).read_bytes())
    assert copied["payload"] == {
        **payload,
        "device": target.joinpath(KEY).stat().st_dev,
        "inode": target.joinpath(KEY).stat().st_ino,
    }
    assert source.joinpath(KEY + ".receipt").read_bytes() == receipt
    from knowledge.storage import BlobStore

    with (target / KEY).open("rb") as stream:
        assert BlobStore(target, secret=SECRET).receipt(KEY, stream.fileno()) == copied["payload"]
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in target.iterdir())
    with pytest.raises(m.RecoveryError):
        m.restore_blobs(verified, target, SECRET)


@pytest.mark.parametrize(
    "change", ["signature", "tenant_id", "key", "size", "sha256", "unreferenced_at", "inode"]
)
def test_source_tamper_refused_before_capture(tmp_path, change):
    m = module()
    root = tmp_path / "source"
    original(root)
    path = root / (KEY + ".receipt")
    record = json.loads(path.read_bytes())
    if change == "signature":
        record["signature"] = "0" * 64
    else:
        record["payload"][change] = 999 if change in {"size", "inode", "unreferenced_at"} else "bad"
    path.write_bytes(canonical(record))
    with pytest.raises(m.RecoveryError):
        m.capture_blobs(root, SECRET, io.BytesIO())


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE]
)
def test_archive_non_regular_members_refused(kind):
    m = module()
    with pytest.raises(m.RecoveryError):
        m.verify_blobs(archive([(KEY, b"", kind)]), SECRET)


@pytest.mark.parametrize("name", ["../x", "/x", "blobs/../x", "x", "./" + KEY])
def test_archive_unexpected_paths_refused(name):
    m = module()
    with pytest.raises(m.RecoveryError):
        m.verify_blobs(archive([(name, b"x", tarfile.REGTYPE)]), SECRET)


@pytest.mark.parametrize(
    "attack", ["symlink", "hardlink", "permissions", "tmp", "locked", "receipt-permissions"]
)
def test_capture_rejects_inflight_and_unsafe_files(tmp_path, attack):
    import fcntl

    m = module()
    root = tmp_path / "source"
    original(root)
    fd = None
    if attack == "symlink":
        (root / KEY).unlink()
        (root / KEY).symlink_to(tmp_path / "absent")
    elif attack == "hardlink":
        os.link(root / KEY, tmp_path / "second-link")
    elif attack == "permissions":
        (root / KEY).chmod(0o644)
    elif attack == "receipt-permissions":
        (root / (KEY + ".receipt")).chmod(0o644)
    elif attack == "tmp":
        (root / "in-flight.tmp").touch(mode=0o600)
    else:
        fd = os.open(root / KEY, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(m.RecoveryError):
            m.capture_blobs(root, SECRET, io.BytesIO())
    finally:
        if fd is not None:
            os.close(fd)


def test_archive_limits_precede_member_reads(monkeypatch):
    m = module()
    hostile = tarfile.TarInfo(KEY)
    hostile.size = 10 * 1024 * 1024 + 1

    def no_read(*args, **kwargs):
        pytest.fail("oversized member was read")

    monkeypatch.setattr(tarfile.TarFile, "extractfile", no_read)
    with pytest.raises(m.RecoveryError):
        m.verify_blobs(hostile.tobuf() + b"\0" * 1024, SECRET)


def test_cli_optin_precedes_environment_files_and_processes(monkeypatch):
    assert importlib.util.find_spec("scripts.recovery") is not None, "operator CLI missing"
    from scripts import recovery

    def forbidden(*args, **kwargs):
        pytest.fail("operation before opt-in")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(recovery, "Runner", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    assert recovery.main(["backup", "--bundle", "/never-read"]) == 2


def operator():
    from scripts import recovery

    return recovery


def target_info(m, token="a" * 32, cid="b" * 64):
    return dict(
        id=cid,
        name="/voice-recovery-" + token,
        image=m.IMAGE,
        image_id="sha256:" + "c" * 64,
        labels={"voice-agent.recovery": token},
        running=True,
        network="none",
        ports={},
        bindings={},
        mounts=[{"Type": "tmpfs", "Destination": p} for p in m.TMPFS],
        bind_mounts=None,
        declared_mounts=None,
        tmpfs=m.TMPFS.copy(),
        memory=512 * 1024**2,
        swap=512 * 1024**2,
        nanocpus=1000000000,
        readonly=True,
        user="999:999",
        privileged=False,
        capadd=None,
        capdrop=["ALL"],
        security=["no-new-privileges"],
        pidmode="",
        ipcmode="private",
        pids=128,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "d" * 64),
        ("name", "/owner-postgres"),
        ("image", "postgres:latest"),
        ("image_id", "sha256:wrong"),
        ("network", "bridge"),
        ("ports", {"5432/tcp": [{"HostPort": "55432"}]}),
        ("bindings", {"5432/tcp": []}),
        ("bind_mounts", ["/host:/tmp"]),
        ("declared_mounts", [{"Type": "bind", "Target": "/tmp"}]),
        ("tmpfs", None),
        ("tmpfs", {"/tmp": "size=16m"}),
        (
            "mounts",
            [{"Type": "volume", "Name": "owner", "Destination": "/var/lib/postgresql/data"}],
        ),
        ("memory", 0),
        ("swap", -1),
        ("nanocpus", 0),
        ("readonly", False),
        ("user", "0"),
        ("privileged", True),
        ("capadd", ["SYS_ADMIN"]),
        ("capdrop", []),
        ("security", []),
        ("pidmode", "host"),
        ("ipcmode", "host"),
        ("pids", 0),
        ("labels", {}),
    ],
)
def test_restore_target_guard_rejects_each_escape(field, value):
    m = operator()
    assert hasattr(m, "validate_target"), "target guards missing"
    info = target_info(m)
    info[field] = value
    with pytest.raises(m.RecoveryError):
        m.validate_target(info, "b" * 64, "a" * 32, "sha256:" + "c" * 64)


def bundle_fixture(tmp_path):
    m = operator()
    b = module()
    root = tmp_path / "original"
    original(root)
    stream = io.BytesIO()
    b.capture_blobs(root, SECRET, stream)
    bundle = tmp_path / "bundle"
    bundle.mkdir(mode=0o700)
    files = {
        "database.dump": b"PGDMP-synthetic",
        "roles.sql": b"CREATE ROLE postgres;",
        "state.json": canonical({"blob_refs": []}),
        "blobs.tar": stream.getvalue(),
    }
    for name, content in files.items():
        b.write_private(bundle / name, content)
    assert hasattr(m, "seal_bundle"), "manifest sealing missing"
    receipt = tmp_path / "trusted-receipt.json"
    expected = m.seal_bundle(bundle, receipt, SECRET)
    return bundle, expected, receipt


def test_manifest_requires_external_anchor_and_hashes_every_member(tmp_path):
    m = operator()
    bundle, expected, receipt = bundle_fixture(tmp_path)
    verified = m.verify_bundle(bundle, expected, SECRET, allowed=True)
    assert verified["files"]["roles.sql"] == b"CREATE ROLE postgres;"
    assert json.loads(receipt.read_bytes())["manifest_sha256"] == expected
    with pytest.raises(m.RecoveryError):
        m.verify_bundle(bundle, "0" * 64, SECRET, allowed=True)
    # A self-consistent modified manifest cannot replace the external trusted receipt.
    (bundle / "roles.sql").write_bytes(b"COPY x TO PROGRAM 'hostile';")
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    manifest["files"]["roles.sql"] = dict(
        size=27, sha256=hashlib.sha256(b"COPY x TO PROGRAM 'hostile';").hexdigest()
    )
    (bundle / "manifest.json").write_bytes(canonical(manifest))
    with pytest.raises(m.RecoveryError):
        m.verify_bundle(bundle, expected, SECRET, allowed=True)


@pytest.mark.parametrize(
    "attack",
    ["version", "extra", "truncated", "permissions", "symlink", "hardlink", "count", "size"],
)
def test_bundle_rejects_hostile_metadata_before_content(tmp_path, attack):
    m = operator()
    bundle, expected, _ = bundle_fixture(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    if attack == "version":
        manifest["version"] = 99
    elif attack == "extra":
        (bundle / "extra.sql").touch(mode=0o600)
    elif attack == "truncated":
        (bundle / "database.dump").write_bytes(b"PG")
    elif attack == "permissions":
        (bundle / "roles.sql").chmod(0o644)
    elif attack == "symlink":
        (bundle / "roles.sql").unlink()
        (bundle / "roles.sql").symlink_to(tmp_path / "absent")
    elif attack == "hardlink":
        os.link(bundle / "roles.sql", tmp_path / "alias")
    elif attack == "count":
        manifest["files"]["../escape"] = dict(size=0, sha256="0" * 64)
    elif attack == "size":
        manifest["files"]["database.dump"]["size"] = 2**40
    (bundle / "manifest.json").write_bytes(canonical(manifest))
    expected = hashlib.sha256(canonical(manifest)).hexdigest()
    with pytest.raises(m.RecoveryError):
        m.verify_bundle(bundle, expected, SECRET, allowed=True)


def test_verify_optin_refuses_before_read(tmp_path, monkeypatch):
    m = operator()
    assert hasattr(m, "verify_bundle"), "bundle verification missing"

    def forbidden(*args, **kwargs):
        pytest.fail("read without opt-in")

    monkeypatch.setattr(Path, "lstat", forbidden)
    with pytest.raises(m.RecoveryError):
        m.verify_bundle(tmp_path, "0" * 64, SECRET)


def test_runner_sanitizes_failures_and_bounds_output(tmp_path):
    import sys

    m = operator()
    assert hasattr(m.Runner, "run"), "bounded runner missing"
    runner = m.Runner()
    with pytest.raises(m.RecoveryError) as error:
        runner.run([sys.executable, "-c", "import sys; print('raw-secret'); sys.exit(3)"])
    assert "raw-secret" not in str(error.value)
    with pytest.raises(m.RecoveryError):
        runner.run([sys.executable, "-c", "print('x'*100000)"], limit=10)
    with pytest.raises(m.RecoveryError):
        runner.run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)


def source_info(m, service):
    info = target_info(m)
    info.update(
        labels={
            "com.docker.compose.project": "voice-agent-phase1",
            "com.docker.compose.service": service,
        },
        mounts=[
            dict(
                Type="volume",
                Name="voice-agent-phase1_"
                + ("postgres-dev-data" if service == "postgres" else "knowledge-private-data"),
                Destination="/var/lib/postgresql/data"
                if service == "postgres"
                else "/var/lib/knowledge",
            )
        ],
        ports={"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]}
        if service == "postgres"
        else {},
    )
    return info


def test_docker_inspect_projects_absent_source_tmpfs_as_null():
    m = operator()
    db, api = source_info(m, "postgres"), source_info(m, "api")
    api["id"] = "d" * 64
    sources = {info["id"]: info for info in (db, api)}

    class SourceDocker:
        def run(self, argv, *, cleanup):
            _, _, _, template, container_id = argv
            # Docker's composite template errors on a direct lookup of an omitted key.
            assert '(index .HostConfig "Tmpfs")' in template
            return canonical(dict(sources[container_id], tmpfs=None))

    runner = SourceDocker()
    projected_db, projected_api = (m.inspect(runner, info["id"]) for info in (db, api))
    assert projected_db["tmpfs"] is None and projected_api["tmpfs"] is None
    m.validate_source_containers(projected_db, projected_api, db["image_id"])


@pytest.mark.parametrize(
    "attack",
    [
        "database",
        "host",
        "port",
        "query",
        "user",
        "source-volume",
        "source-port",
        "source-label",
        "source-image",
    ],
)
def test_source_profile_refuses_other_databases_before_connect(attack):
    m = operator()
    assert hasattr(m, "validate_source"), "source guard missing"
    db, api = source_info(m, "postgres"), source_info(m, "api")
    dsn = "postgresql://postgres:synthetic@127.0.0.1:55432/voice_agent"
    if attack == "database":
        dsn = dsn.replace("/voice_agent", "/voice_agent_test")
    elif attack == "host":
        dsn = dsn.replace("127.0.0.1", "example.org")
    elif attack == "port":
        dsn = dsn.replace("55432", "55433")
    elif attack == "query":
        dsn += "?host=example.org"
    elif attack == "user":
        dsn = dsn.replace("postgres:", "scheduler_app:")
    elif attack == "source-volume":
        api["mounts"][0]["Name"] = "other-volume"
    elif attack == "source-port":
        db["ports"]["5432/tcp"][0]["HostPort"] = "55433"
    elif attack == "source-label":
        api["labels"]["com.docker.compose.service"] = "worker"
    elif attack == "source-image":
        db["image"] = "postgres:latest"
    with pytest.raises(m.RecoveryError):
        m.validate_source(db, api, dsn, "sha256:" + "c" * 64)


def kernel_mounts(socket_mode="700"):
    paths = (
        ("/var/lib/postgresql/data", "/var/lib/postgresql/data", "700", 262144),
        ("/var/run/postgresql", "/run/postgresql", socket_mode, 8192),
        ("/tmp", "/tmp", "700", 16384),
    )
    return "".join(
        f"{path} {resolved} 999:999:{mode}\n"
        f"{i} 1 0:{i} / {resolved} rw,nosuid,nodev,noexec,relatime - "
        f"tmpfs tmpfs rw,size={size}k,mode=700,uid=999,gid=999\n"
        for i, (path, resolved, mode, size) in enumerate(paths, start=5)
    )


class FakeDocker:
    def __init__(self, m, *, create_failure=False, restore_failure=False, bad_inspect=False):
        self.m, self.calls = m, []
        self.create_failure, self.restore_failure, self.bad_inspect = (
            create_failure,
            restore_failure,
            bad_inspect,
        )
        self.token = None
        self.cid = "b" * 64
        self.reported_mounts = None
        self.mount_proof = kernel_mounts()

    def run(self, argv, *, data=None, **kwargs):
        self.calls.append((argv, data, kwargs))
        assert argv[0] == "docker"
        if argv[1:3] == ["context", "inspect"]:
            return b'"unix:///synthetic/docker.sock"'
        if argv[1:3] == ["image", "inspect"]:
            return ("sha256:" + "c" * 64 + "\n").encode()
        if argv[1] == "create":
            self.token = argv[argv.index("--name") + 1].removeprefix("voice-recovery-")
            assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
            assert (
                "--mount" not in argv
                and "--volume" not in argv
                and "-v" not in argv
                and "-p" not in argv
            )
            assert argv[argv.index("-e") + 1].startswith("POSTGRES_USER=recovery_")
            if self.create_failure:
                raise self.m.RecoveryError("synthetic create failed")
            cidfile = Path(argv[argv.index("--cidfile") + 1])
            module().write_private(cidfile, self.cid.encode())
            return (self.cid + "\n").encode()
        if argv[1] == "inspect":
            info = target_info(self.m, self.token, self.cid)
            if self.bad_inspect:
                info["network"] = "bridge"
            if self.reported_mounts is not None:
                info["mounts"] = self.reported_mounts
            return canonical(info)
        if argv[1] in {"start", "rm"}:
            return b""
        if argv[1] == "exec":
            assert self.cid in argv
            if argv[3] == "sh":
                return self.mount_proof.encode()
            if argv[3:] == ["chmod", "00700", "/var/run/postgresql"]:
                self.mount_proof = kernel_mounts()
                return b""
            if "pg_isready" in argv:
                return b""
            if "psql" in argv:
                if data and b"listen_addresses" in data:
                    return canonical({"database": argv[argv.index("--dbname") + 1], "ready": True})
                if data == b"CREATE ROLE postgres;":
                    if self.restore_failure:
                        raise self.m.RecoveryError("synthetic restore failed")
                    return b""
                if data and b"system_identifier" in data:
                    return b'"synthetic-cluster"\n'
                return b"{}"
            if "pg_restore" in argv:
                assert data == b"PGDMP-synthetic"
                assert "--create" in argv and "--clean" not in argv
                assert argv[argv.index("--username") + 1] == "postgres"
                assert argv[argv.index("--host") + 1] == "/var/run/postgresql"
                assert argv[argv.index("--dbname") + 1] == "recovery_" + self.token
                return b""
        raise AssertionError(argv)


def test_drill_streams_unchanged_roles_dump_and_cleans_created_id(tmp_path, monkeypatch):
    m = operator()
    assert hasattr(m, "drill"), "drill orchestration missing"
    bundle, expected, _ = bundle_fixture(tmp_path)
    verified = m.verify_bundle(bundle, expected, SECRET, allowed=True)
    fake = FakeDocker(m)
    monkeypatch.setattr(m, "resource_admission", lambda runner: None)
    monkeypatch.setattr(m, "target_state", lambda *args: verified["state"])
    monkeypatch.setattr(m, "check_rls", lambda *args: None)
    result = m.drill(verified, tmp_path, SECRET, fake)
    assert result["status"] == "verified" and result["blobs"] == 1
    assert any(data == b"CREATE ROLE postgres;" for argv, data, _ in fake.calls)
    assert any(data == b"PGDMP-synthetic" for argv, data, _ in fake.calls)
    assert fake.calls[-1][0] == ["docker", "rm", "--force", fake.cid]
    assert fake.calls[-1][2]["cleanup"] is True
    assert not list(tmp_path.glob("voice-recovery-*"))


@pytest.mark.parametrize(
    "difference",
    ["default", "added_grant", "missing_privilege", "target_acl", "owner", "unknown", "malformed"],
)
def test_restored_table_acl_only_accepts_the_same_builtin_default(monkeypatch, difference):
    m = operator()
    from scripts import recovery_database

    owner_acl = ["postgres=arwdDxt/postgres"]
    saved_table = dict(name="admission_buckets", owner="postgres", acl=owner_acl.copy())
    actual_table = dict(saved_table, acl=None)
    defaults = {"admission_buckets": {"owner": "postgres", "acl": owner_acl.copy()}}
    if difference == "added_grant":
        saved_table["acl"].append("scheduler_app=r/postgres")
    elif difference == "missing_privilege":
        saved_table["acl"] = ["postgres=r/postgres"]
    elif difference == "target_acl":
        actual_table["acl"] = ["postgres=r/postgres"]
    elif difference == "owner":
        defaults["admission_buckets"]["owner"] = "other_role"
    elif difference == "unknown":
        defaults.clear()
    elif difference == "malformed":
        saved_table["acl"] = owner_acl[0]
    saved = {"tables": [saved_table], "roles": {}}
    actual = {"tables": [actual_table]}
    saved_bytes = canonical(saved)
    monkeypatch.setattr(recovery_database, "database_state", lambda query: actual)
    monkeypatch.setattr(recovery_database, "role_state", lambda query, exclude: {})

    def project_default(runner, cid, user, db, sql):
        assert db == "voice_agent"
        assert "pg_catalog.acldefault('r', c.relowner)" in sql
        assert "c.relkind IN ('r','p')" in sql and "c.relacl IS NULL" in sql
        return canonical(defaults)

    monkeypatch.setattr(m, "psql", project_default)
    compared = m.target_state(None, "b" * 64, "recovery_" + "a" * 32, saved)
    assert (compared == saved) is (difference == "default")
    assert canonical(saved) == saved_bytes


def test_drill_proves_kernel_mounts_when_docker_omits_tmpfs_descriptors(tmp_path, monkeypatch):
    m = operator()
    bundle, expected, _ = bundle_fixture(tmp_path)
    verified = m.verify_bundle(bundle, expected, SECRET, allowed=True)
    fake = FakeDocker(m)
    fake.reported_mounts = []
    fake.mount_proof = kernel_mounts(socket_mode="3775")
    monkeypatch.setattr(m, "resource_admission", lambda runner: None)
    monkeypatch.setattr(m, "target_state", lambda *args: verified["state"])
    monkeypatch.setattr(m, "check_rls", lambda *args: None)
    assert m.drill(verified, tmp_path, SECRET, fake)["status"] == "verified"
    mode_fix = ["docker", "exec", fake.cid, "chmod", "00700", "/var/run/postgresql"]
    args = [argv for argv, _, _ in fake.calls]
    assert args.count(mode_fix) == 1
    roles = next(i for i, (_, data, _) in enumerate(fake.calls) if data == b"CREATE ROLE postgres;")
    assert args.index(mode_fix) < roles
    assert any(argv[:4] == ["docker", "exec", fake.cid, "sh"] for argv in args[roles + 1 :])


@pytest.mark.parametrize(
    "bad_proof",
    [
        kernel_mounts().splitlines()[0],
        kernel_mounts() + kernel_mounts().splitlines()[1],
        kernel_mounts().replace("/run/postgresql", "/unrelated/postgresql"),
        kernel_mounts().replace("noexec,", "", 1),
        kernel_mounts().replace("0:6", "0:5"),
        kernel_mounts().replace("size=8192k", "size=8193k"),
        kernel_mounts().replace("tmpfs tmpfs", "ext4 tmpfs", 1),
        kernel_mounts().replace("999:999:700", "0:999:700", 1),
        kernel_mounts().replace("999:999:700", "999:999:777", 1),
    ],
    ids=["missing", "extra", "path", "flags", "device", "size", "type", "owner", "mode"],
)
def test_drill_rejects_unsafe_kernel_mounts_before_any_sql(tmp_path, monkeypatch, bad_proof):
    m = operator()
    bundle, expected, _ = bundle_fixture(tmp_path)
    verified = m.verify_bundle(bundle, expected, SECRET, allowed=True)
    fake = FakeDocker(m)
    fake.mount_proof = bad_proof
    monkeypatch.setattr(m, "resource_admission", lambda runner: None)
    monkeypatch.setattr(m, "target_state", lambda *args: verified["state"])
    monkeypatch.setattr(m, "check_rls", lambda *args: None)
    with pytest.raises(m.RecoveryError, match="restore runtime storage isolation"):
        m.drill(verified, tmp_path, SECRET, fake)
    assert not any("psql" in argv or "pg_restore" in argv for argv, _, _ in fake.calls)
    assert fake.calls[-1][0] == ["docker", "rm", "--force", fake.cid]


@pytest.mark.parametrize("failure", ["create", "restore", "inspect"])
def test_drill_failure_never_removes_an_existing_name(tmp_path, monkeypatch, failure):
    m = operator()
    assert hasattr(m, "drill"), "drill orchestration missing"
    bundle, expected, _ = bundle_fixture(tmp_path)
    verified = m.verify_bundle(bundle, expected, SECRET, allowed=True)
    fake = FakeDocker(
        m,
        create_failure=failure == "create",
        restore_failure=failure == "restore",
        bad_inspect=failure == "inspect",
    )
    monkeypatch.setattr(m, "resource_admission", lambda runner: None)
    with pytest.raises(m.RecoveryError):
        m.drill(verified, tmp_path, SECRET, fake)
    removals = [argv for argv, _, _ in fake.calls if argv[1] == "rm"]
    assert removals == ([] if failure == "create" else [["docker", "rm", "--force", fake.cid]])
    assert not any("--clean" in argv or "dropdb" in argv for argv, _, _ in fake.calls)


def test_resource_floor_prevents_container_creation(monkeypatch, tmp_path):
    m = operator()
    assert hasattr(m, "resource_admission"), "resource admission missing"
    import psutil

    monkeypatch.setattr(
        psutil, "virtual_memory", lambda: type("Memory", (), {"available": 768 * 1024**2})()
    )
    fake = FakeDocker(m)
    with pytest.raises(m.RecoveryError):
        m.resource_admission(fake)
    assert not fake.calls


def test_database_snapshot_bounds_before_row_digest_and_missing_blobs():
    assert importlib.util.find_spec("scripts.recovery_database") is not None, (
        "snapshot digest missing"
    )
    from scripts import recovery_database as db

    seen = []

    def query(sql):
        seen.append(sql)
        if "pg_database_size" in sql:
            return 65 * 1024**2
        pytest.fail("row read before database bound")

    with pytest.raises(module().RecoveryError):
        db.database_state(query)
    assert len(seen) == 1
    with pytest.raises(module().RecoveryError):
        db.check_references(
            {
                "blob_refs": [
                    {
                        "key": KEY,
                        "sha256": "0" * 64,
                        "tenant": "1" * 32,
                        "source": "2" * 32,
                        "revision": 1,
                    }
                ]
            },
            ({}, {}),
            SECRET,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "0" * 32),
        ("key", "wrong"),
        ("size", 999),
        ("sha256", "0" * 64),
        ("inode", 1),
        ("created_at", -1),
        ("unreferenced_at", -1),
        ("version", 2),
    ],
)
def test_resigned_invalid_receipts_still_refused(tmp_path, field, value):
    m = module()
    root = tmp_path / "source"
    original(root)
    path = root / (KEY + ".receipt")
    record = json.loads(path.read_bytes())
    record["payload"][field] = value
    record["signature"] = hmac.new(
        SECRET.encode(), canonical(record["payload"]), "sha256"
    ).hexdigest()
    path.write_bytes(canonical(record))
    with pytest.raises(m.RecoveryError):
        m.capture_blobs(root, SECRET, io.BytesIO())


def test_archive_duplicate_truncation_and_oversized_header_are_rejected():
    m = module()
    name = "a" * 64 + ".blob"
    duplicate = archive([(name, b"x", tarfile.REGTYPE)] * 2)
    for data in (duplicate, duplicate[:600], duplicate[:512]):
        with pytest.raises(m.RecoveryError):
            m.verify_blobs(data, SECRET)
    header = tarfile.TarInfo(name)
    header.size = 10 * 1024**2 + 1
    header.mode = 0o600
    with pytest.raises(m.RecoveryError):
        m.verify_blobs(header.tobuf() + bytes(1024), SECRET)


def test_one_gib_resource_budget_is_admitted_after_verified_buffers(monkeypatch):
    m = operator()
    import psutil

    monkeypatch.setattr(
        psutil, "virtual_memory", lambda: type("Memory", (), {"available": 1024**3})()
    )

    class MemoryDocker:
        def run(self, argv, **kwargs):
            return b"2147483648" if argv[1] == "info" else b"512MiB / 2GiB\n"

    m.resource_admission(MemoryDocker())


@pytest.mark.parametrize("changed", [False, True, "deadline", "roles"])
def test_source_backup_snapshot_and_change_refusal(tmp_path, monkeypatch, changed):
    import argparse

    import psycopg

    from scripts import recovery_database as db

    m, b = operator(), module()
    root = tmp_path / "source"
    original(root)
    captures = 0

    def capture(*args, **kwargs):
        nonlocal captures
        captures += 1
        if captures == 2 and changed is True:
            os.utime(root / (KEY + ".receipt"), ns=(100, 100))
        stream = io.BytesIO()
        metadata = b.capture_blobs(root, SECRET, stream)
        return canonical(metadata) if kwargs.get("metadata_only") else stream.getvalue()

    monkeypatch.setattr(m, "capture_source", capture)
    monkeypatch.setenv(
        "RECOVERY_SOURCE_DSN", "postgresql://postgres:synthetic@127.0.0.1:55432/voice_agent"
    )
    monkeypatch.setenv("RECOVERY_BLOB_SECRET", SECRET)
    role_calls = 0

    def role_state(query):
        nonlocal role_calls
        role_calls += 1
        return {"roles": [role_calls] if changed == "roles" else []}

    monkeypatch.setattr(db, "role_state", role_state)
    monkeypatch.setattr(db, "database_state", lambda query: {"sequences": {}, "blob_refs": []})
    monkeypatch.setattr(db, "sequence_state", lambda query: {})
    queries = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, statement):
            queries.append(statement)
            return type(
                "Cursor",
                (),
                {
                    "fetchone": lambda self: (
                        "00000001-00000002-1"
                        if "pg_export_snapshot" in statement
                        else "synthetic-cluster",
                    )
                },
            )()

    def connect(dsn, **kwargs):
        assert (
            dsn.endswith("127.0.0.1:55432/voice_agent")
            and "default_transaction_read_only=on" in kwargs["options"]
        )
        return Connection()

    monkeypatch.setattr(psycopg, "connect", connect)

    class SourceDocker:
        def __init__(self):
            self.calls = []

        def run(self, argv, **kwargs):
            self.calls.append(argv)
            if argv[1:3] == ["context", "inspect"]:
                return b'"unix:///synthetic/docker.sock"'
            if argv[1:3] == ["image", "inspect"]:
                return ("sha256:" + "c" * 64).encode()
            if argv[1] == "inspect":
                return canonical(
                    {
                        **source_info(m, "postgres" if argv[-1] == "d" * 64 else "api"),
                        "id": argv[-1],
                    }
                )
            if "psql" in argv:
                return b'"synthetic-cluster"'
            if "pg_dump" in argv:
                assert argv[argv.index("--snapshot") + 1] == "00000001-00000002-1"
                assert (
                    "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY" in queries
                    and "ROLLBACK" not in queries
                )
                return b"PGDMP-synthetic"
            if "pg_dumpall" in argv:
                return b"CREATE ROLE postgres;"
            pytest.fail("unexpected source operation")

    runner = SourceDocker()
    import time

    runner.deadline = 0 if changed == "deadline" else time.monotonic() + 600
    args = argparse.Namespace(
        bundle=tmp_path / "backup",
        creation_receipt=tmp_path / "trusted.json",
        source_postgres="d" * 64,
        source_api="e" * 64,
    )
    if changed:
        reason = (
            "operation deadline"
            if changed == "deadline"
            else ("source roles changed" if changed == "roles" else "source blobs changed")
        )
        with pytest.raises(m.RecoveryError, match=reason):
            m.backup(args, runner)
        assert not args.creation_receipt.exists()
    else:
        result = m.backup(args, runner)
        assert result["status"] == "created"
        expected = json.loads(args.creation_receipt.read_bytes())["manifest_sha256"]
        assert (
            m.verify_bundle(args.bundle, expected, SECRET, allowed=True)["files"]["roles.sql"]
            == b"CREATE ROLE postgres;"
        )
    assert not queries if changed == "deadline" else queries[-1] == "ROLLBACK"
    assert not any(
        "create" in argv or "rm" in argv or "pg_restore" in argv for argv in runner.calls
    )


def test_recovery_queries_disposable_postgres():
    """Explicit opt-in: only the existing disposable synthetic test database, read-only."""
    if os.getenv("RECOVERY_SQL_TEST") != "1":
        pytest.skip("set RECOVERY_SQL_TEST=1 only during a coordinated test DB slot")
    from urllib.parse import urlsplit

    import psycopg

    from scripts import recovery_database as db

    dsn = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://postgres:phase1_test_only@127.0.0.1:55433/voice_agent_test",
    ).replace("postgresql+psycopg://", "postgresql://", 1)
    parsed = urlsplit(dsn)
    assert parsed.scheme == "postgresql" and parsed.hostname in {"127.0.0.1", "::1"}
    assert parsed.port in {55433, 5432} and parsed.path == "/voice_agent_test"
    assert parsed.username == "postgres" and not parsed.query and not parsed.fragment
    with psycopg.connect(
        dsn,
        autocommit=True,
        hostaddr=parsed.hostname,
        sslmode="disable",
        connect_timeout=3,
        options="-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=1000 -c timezone=UTC -c datestyle=ISO,YMD",
    ) as connection:
        assert connection.execute("SELECT current_database()").fetchone() == ("voice_agent_test",)
        connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")

        def query(statement):
            return connection.execute(
                "SELECT CASE WHEN octet_length(result::text) <= 1048576 THEN result ELSE NULL END FROM ("
                + statement
                + ") bounded(result)"
            ).fetchone()[0]

        state = db.database_state(query)
        roles = db.role_state(query)
        assert state["migration_head"] and state["fingerprints"] and roles["roles"]
        assert db.sequence_state(query) == state["sequences"]
        connection.execute("ROLLBACK")
        for statement in db.rls_queries(state):
            with connection.cursor() as cursor:
                cursor.execute(statement)
                values = []
                while True:
                    if cursor.description:
                        values.append(cursor.fetchone()[0])
                    if not cursor.nextset():
                        break
                assert values == [0]


def test_memory_refusal_precedes_bulk_bundle_reads(tmp_path, monkeypatch):
    m = operator()
    import psutil

    monkeypatch.setattr(
        psutil, "virtual_memory", lambda: type("Memory", (), {"available": 768 * 1024**2})()
    )
    monkeypatch.setattr(
        m, "read_private", lambda *args: pytest.fail("read before memory admission")
    )
    with pytest.raises(m.RecoveryError, match="host memory"):
        m.verify_bundle(tmp_path, "0" * 64, SECRET, allowed=True)


def test_remote_docker_refused_without_commands(monkeypatch):
    m = operator()
    assert hasattr(m, "local_docker"), "local daemon guard missing"
    monkeypatch.setenv("DOCKER_HOST", "tcp://example.org:2375")
    fake = FakeDocker(m)
    with pytest.raises(m.RecoveryError):
        m.local_docker(fake)
    assert not fake.calls


def test_invalid_bundle_never_gets_creation_receipt(tmp_path):
    m = operator()
    bundle, _, receipt = bundle_fixture(tmp_path)
    receipt.unlink()
    (bundle / "manifest.json").unlink()
    (bundle / "database.dump").write_bytes(b"not-a-dump")
    with pytest.raises(m.RecoveryError):
        m.seal_bundle(bundle, receipt, SECRET)
    assert not receipt.exists()


def test_read_does_not_confuse_access_time_with_source_mutation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    m = module()
    path = tmp_path / "private"
    m.write_private(path, b"synthetic")
    real_fstat = os.fstat
    calls = 0

    def accessed(fd):
        nonlocal calls
        calls += 1
        info = real_fstat(fd)
        fields = {
            key: getattr(info, key)
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
        }
        return SimpleNamespace(**fields, st_atime_ns=calls)

    monkeypatch.setattr(os, "fstat", accessed)
    assert m.read_private(path, 100) == b"synthetic"


def test_drill_waits_for_final_postgres_not_temporary_init_server(tmp_path, monkeypatch):
    m = operator()
    bundle, expected, _ = bundle_fixture(tmp_path)
    verified = m.verify_bundle(bundle, expected, SECRET, allowed=True)

    class InitializingDocker(FakeDocker):
        final_ready = False

        def run(self, argv, *, data=None, **kwargs):
            if "psql" in argv and data and b"listen_addresses" in data:
                self.final_ready = True
                return canonical({"database": argv[argv.index("--dbname") + 1], "ready": True})
            if data == b"CREATE ROLE postgres;" and not self.final_ready:
                raise m.RecoveryError("temporary init server still running")
            return super().run(argv, data=data, **kwargs)

    fake = InitializingDocker(m)
    monkeypatch.setattr(m, "resource_admission", lambda runner: None)
    monkeypatch.setattr(m, "target_state", lambda *args: verified["state"])
    monkeypatch.setattr(m, "check_rls", lambda *args: None)
    assert m.drill(verified, tmp_path, SECRET, fake)["status"] == "verified"


def test_source_total_size_rejected_before_first_content_read(tmp_path, monkeypatch):
    m = module()
    root = tmp_path / "source"
    original(root)
    key2 = KEY.replace("-1-", "-2-")
    (root / key2).write_bytes(b"second synthetic original")
    (root / key2).chmod(0o600)
    (root / (key2 + ".receipt")).write_bytes(b"synthetic placeholder")
    (root / (key2 + ".receipt")).chmod(0o600)
    monkeypatch.setattr(m, "MAX_BLOB_TOTAL", 30)
    monkeypatch.setattr(
        os, "fdopen", lambda *args: pytest.fail("content read before total admission")
    )
    with pytest.raises(m.RecoveryError, match="source blob limit"):
        m.capture_blobs(root, SECRET, io.BytesIO())


def test_restored_app_role_must_not_bypass_rls_even_when_tables_empty():
    m = operator()

    class UnsafeRole:
        def run(self, argv, **kwargs):
            return b"false"

    with pytest.raises(m.RecoveryError, match="restored app role"):
        m.check_rls(UnsafeRole(), "b" * 64, "recovery_" + "a" * 32, {})


def test_empty_blob_snapshot_still_requires_external_key(tmp_path):
    m = module()
    root = tmp_path / "empty"
    root.mkdir(mode=0o700)
    stream = io.BytesIO()
    m.capture_blobs(root, SECRET, stream)
    with pytest.raises(m.RecoveryError, match="external blob key"):
        m.verify_blobs(stream.getvalue(), "")
