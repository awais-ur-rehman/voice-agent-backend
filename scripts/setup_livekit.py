"""Write private loopback-only media configuration; never print signing keys."""

import json
import os
import tempfile
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def write_config(env: Path, target: Path) -> None:
    values = dotenv_values(env)
    key = values.get("SCHEDULER_LIVEKIT_API_KEY")
    secret = values.get("SCHEDULER_LIVEKIT_API_SECRET")
    if not key or not secret or len(secret) < 32 or target.is_symlink():
        raise ValueError("Valid private media configuration required")
    config = {
        "port": 7880,
        "bind_addresses": ["0.0.0.0"],
        "rtc": {
            "tcp_port": 7881,
            "udp_port": 7882,
            "node_ip": "127.0.0.1",
            "use_external_ip": False,
            "enable_loopback_candidate": True,
        },
        "room": {"auto_create": False, "empty_timeout": 30, "departure_timeout": 30},
        "keys": {key: secret},
        "logging": {"level": "warn", "pion_level": "error"},
    }
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".livekit-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(config, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    write_config(ROOT / ".env.api", ROOT / ".local/livekit.json")
    print("Private LiveKit config ready in .local/livekit.json; keys were not displayed.")
