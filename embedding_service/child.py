"""Private stdio protocol; only synthetic metrics leave the smoke command."""

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path


def _watch_parent(parent_pid):
    while True:
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGKILL)
        time.sleep(0.1)


def main():
    parent_pid = os.getppid()
    if parent_pid == 1:
        return 1
    threading.Thread(target=_watch_parent, args=(parent_pid,), daemon=True).start()
    from embedding_service.model import Model

    try:
        model = Model(Path(sys.argv[1]))
        print(json.dumps({"ready": True}), flush=True)
        for line in sys.stdin:
            try:
                vectors = model.embed(json.loads(line))
                print(json.dumps({"vectors": vectors}, allow_nan=False), flush=True)
            except Exception:
                print(json.dumps({"error": "model_rejected"}), flush=True)
    except Exception:
        print(json.dumps({"error": "startup_failed"}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
