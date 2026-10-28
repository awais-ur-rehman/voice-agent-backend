import json
import stat

import pytest


def test_private_local_media_config_uses_saved_keys_and_no_public_ports(tmp_path):
    from scripts.setup_livekit import write_config

    env = tmp_path / ".env.api"
    env.write_text(
        "SCHEDULER_LIVEKIT_API_KEY=synthetic-key\nSCHEDULER_LIVEKIT_API_SECRET=synthetic-secret-value-at-least32characters\n"
    )
    target = tmp_path / "private" / "livekit.json"
    write_config(env, target)
    config = json.loads(target.read_text())
    assert config["keys"] == {"synthetic-key": "synthetic-secret-value-at-least32characters"}
    assert config["rtc"]["node_ip"] == "127.0.0.1"
    assert config["rtc"]["udp_port"] == 7882
    assert config["room"]["auto_create"] is False
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_media_setup_refuses_symlink_output(tmp_path):
    from scripts.setup_livekit import write_config

    env = tmp_path / ".env.api"
    env.write_text(
        "SCHEDULER_LIVEKIT_API_KEY=synthetic-key\nSCHEDULER_LIVEKIT_API_SECRET=synthetic-secret-value-at-least32characters\n"
    )
    original = tmp_path / "saved"
    original.write_text("preserved")
    target = tmp_path / "livekit.json"
    target.symlink_to(original)
    with pytest.raises(ValueError):
        write_config(env, target)
    assert original.read_text() == "preserved"
