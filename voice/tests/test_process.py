import importlib.util
import sys
import threading

import pytest


def test_process_implementation_exists():
    assert importlib.util.find_spec("voice_runtime.speech.process") is not None


def test_deadline_kills_child_and_never_returns_late_output():
    from voice_runtime.speech.process import JobFailure, Supervisor

    supervisor = Supervisor(memory_bytes=64 * 1024**2)
    with pytest.raises(JobFailure, match="deadline"):
        supervisor.run(
            [sys.executable, "-c", "import time; time.sleep(3); print('late')"],
            generation=0,
            timeout=0.1,
        )
    assert (
        supervisor.run(
            [sys.executable, "-c", "print('fresh')"], generation=0, timeout=2
        ).stdout.strip()
        == b"fresh"
    )


def test_cancelled_generation_cannot_publish():
    from voice_runtime.speech.process import JobFailure, Supervisor

    supervisor = Supervisor(memory_bytes=64 * 1024**2)
    supervisor.cancel()
    with pytest.raises(JobFailure, match="stale"):
        supervisor.run([sys.executable, "-c", "print('stale')"], generation=0, timeout=2)


def test_memory_limit_terminates_real_child():
    from voice_runtime.speech.process import JobFailure, Supervisor

    supervisor = Supervisor(memory_bytes=24 * 1024**2)
    with pytest.raises(JobFailure, match="memory"):
        supervisor.run(
            [sys.executable, "-c", "import time; x=bytearray(80*1024**2); time.sleep(3)"],
            generation=0,
            timeout=2,
        )


def test_output_is_bounded():
    from voice_runtime.speech.process import JobFailure, Supervisor

    supervisor = Supervisor(memory_bytes=64 * 1024**2)
    with pytest.raises(JobFailure, match="output"):
        supervisor.run(
            [sys.executable, "-c", "import sys;sys.stdout.write('x'*9000000)"],
            generation=0,
            timeout=3,
        )


def test_cancel_running_child_discards_its_late_result():
    from voice_runtime.speech.process import JobFailure, Supervisor

    supervisor = Supervisor(memory_bytes=64 * 1024**2)
    timer = threading.Timer(0.15, supervisor.cancel)
    timer.start()
    try:
        with pytest.raises(JobFailure, match="stale"):
            supervisor.run(
                [sys.executable, "-c", "import time;time.sleep(3);print('late')"],
                generation=0,
                timeout=2,
            )
    finally:
        timer.join()
