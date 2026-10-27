"""Run unit tests with workspace credentials and network connections blocked."""

import os
from pathlib import Path
import re
import socket
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ORIGINAL_READ = Path.read_text


def guarded_read(path, *args, **kwargs):
    if path.resolve() == ROOT / '.env':
        raise AssertionError('Offline tests must not read workspace credentials')
    return ORIGINAL_READ(path, *args, **kwargs)


def blocked_connect(*args, **kwargs):
    raise AssertionError('Offline tests must not make network connections')


class RedactedOutput:
    def write(self, text):
        return sys.__stderr__.write(re.sub(r'nvapi-[A-Za-z0-9_-]{20,}', '[REDACTED]', text))

    def flush(self):
        sys.__stderr__.flush()


if __name__ == '__main__':
    for name in list(os.environ):
        if name.startswith('LLM_') or name == 'NVIDIA_API_KEY':
            os.environ.pop(name)
    Path.read_text = guarded_read
    socket.socket.connect = blocked_connect
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    result = unittest.TextTestRunner(stream=RedactedOutput(), verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
