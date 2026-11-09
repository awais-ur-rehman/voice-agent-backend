"""Read-only guard and measurement tests for the disposable load CLI."""

from unittest.mock import Mock

import pytest

from scripts import hardening_load as load


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--clients", "20"],
        ["--allow-disposable-fixtures", "--clients", "21"],
        ["--allow-disposable-fixtures", "--clients", "0"],
        ["--allow-disposable-fixtures", "--rounds", "31"],
        ["--allow-disposable-fixtures", "--rounds", "0"],
        ["--allow-disposable-fixtures", "--rounds", "secret-value"],
    ],
)
def test_opt_in_and_profile_reject_before_dispatch(args, capsys):
    runner = Mock()
    assert load.main(args, {}, runner) == 2
    runner.assert_not_called()
    assert "secret-value" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("name", "replace", "with_value"),
    [
        ("TEST_DATABASE_URL", "55433", "55432"),
        ("TEST_DATABASE_URL", "127.0.0.1", "example.invalid"),
        ("TEST_DATABASE_URL", "127.0.0.1", "localhost"),
        ("TEST_DATABASE_URL", "voice_agent_test", "voice_agent"),
        ("TEST_DATABASE_URL", "phase1_test_only", "secret-value"),
        ("TEST_APP_DATABASE_URL", "voice_agent_test", "voice_agent_test?host=other"),
        ("TEST_APP_DATABASE_URL", "scheduler_app", "postgres"),
        ("TEST_VALKEY_URL", "56379", "56380"),
        ("TEST_VALKEY_URL", "/0", "/1"),
        ("TEST_VALKEY_URL", "/0", "/0#secret-value"),
    ],
)
def test_target_rejects_before_dispatch(name, replace, with_value, capsys):
    runner = Mock()
    invalid = load.FIXTURE_ENV[name].replace(replace, with_value)
    assert load.main(["--allow-disposable-fixtures"], {name: invalid}, runner) == 2
    runner.assert_not_called()
    assert "secret-value" not in capsys.readouterr().err


def test_runner_receives_only_bounded_profile_and_fixed_targets():
    runner = Mock(return_value={"qualification": None, "correctness": True})
    assert (
        load.main(
            ["--allow-disposable-fixtures", "--clients", "2", "--rounds", "3"],
            {
                **load.FIXTURE_ENV,
                "SCHEDULER_DATABASE_URL": "secret",
                "LLM_API_KEY": "secret",
                "HF_TOKEN": "secret",
                "PGSERVICE": "secret",
            },
            runner,
        )
        == 0
    )
    runner.assert_called_once_with(load.Profile(2, 3))


def test_actual_database_guard_rejects_before_fixture_writes():
    connection = Mock()
    connection.scalar.return_value = "voice_agent"
    with pytest.raises(load.Refusal):
        load.require_database(connection)
    assert connection.scalar.call_count == 1
    assert "current_database" in str(connection.scalar.call_args.args[0])
    connection.execute.assert_not_called()


def test_actual_database_checks_schema_after_database_identity():
    connection = Mock()
    connection.scalar.side_effect = ["voice_agent_test", "0016"]
    with pytest.raises(load.Refusal):
        load.require_database(connection)
    assert connection.scalar.call_count == 2
    assert "alembic_version" in str(connection.scalar.call_args.args[0])


def test_continuous_quantile_empty_and_interpolated():
    assert load.quantile([], 0.95) is None
    assert load.quantile([8], 0.95) == 8
    assert load.quantile([20, 0, 10], 0.95) == pytest.approx(19)


def test_exit_failure_keeps_report(capsys):
    runner = Mock(return_value={"qualification": False, "correctness": True})
    assert load.main(["--allow-disposable-fixtures"], {}, runner) == 1
    assert '"qualification": false' in capsys.readouterr().out


def test_full_profile_is_the_only_qualification_profile():
    assert load.Profile(20, 30).qualifying
    assert not load.Profile(19, 30).qualifying
    assert not load.Profile(20, 29).qualifying


def test_replay_matches_sql_projection_without_ignoring_changed_results():
    from scripts.hardening_load_workloads import exact_information_replay

    original = {
        "revision": 2,
        "result": {"ok": True},
        "state": {"revision": 2, "history": [{"content": "synthetic"}], "summary": "transient"},
    }
    replay = original | {"state": {"revision": 2, "history": [], "summary": ""}}
    assert exact_information_replay(original, replay)
    assert not exact_information_replay(original, replay | {"revision": 3})
    assert not exact_information_replay(original, replay | {"result": {"ok": False}})
    assert not exact_information_replay(original, replay | {"state": {"revision": 2}})


def test_restricted_role_checks_database_identity_without_alembic_access():
    connection = Mock()
    connection.scalar.return_value = "voice_agent_test"
    load.require_database(connection, schema=False)
    connection.scalar.assert_called_once()


def test_workload_runner_drops_all_inherited_configuration(monkeypatch):
    import os
    from unittest.mock import patch

    from scripts import hardening_load_workloads

    expected = load.Profile(2, 3)
    result = {"qualification": None, "correctness": True}

    def fake_run(profile):
        assert profile == expected
        assert dict(os.environ) == {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"}
        return result

    monkeypatch.setattr(hardening_load_workloads, "run", fake_run)
    with patch.dict(
        os.environ, {"SCHEDULER_LLM_API_KEY": "synthetic-do-not-inherit", "PGHOST": "other"}
    ):
        assert load.run_isolated(expected) == result


def test_socket_fence_allows_only_fixed_cache_address():
    from scripts.hardening_load_workloads import OfflineAdapters

    offline = OfflineAdapters()
    offline.audit_socket("socket.connect", (None, ("127.0.0.1", 56379)))
    offline.audit_socket("socket.getaddrinfo", ("127.0.0.1", 56379))
    assert offline.external_attempts == 0
    with pytest.raises(RuntimeError, match="External transport is disabled"):
        offline.audit_socket("socket.connect", (None, ("127.0.0.1", 56380)))
    with pytest.raises(RuntimeError, match="External transport is disabled"):
        offline.audit_socket("socket.getaddrinfo", ("provider.invalid", 443))
    assert offline.external_attempts == 2
