"""The helper as the tests run it: the same script, with a long limit for each git call that it makes.

No test result may depend on how fast git answers, or on the wall clock. The helper gives some git calls a short
limit (in a hook, the time left until the hook's one git deadline; 3 s in git_toplevel), and on a loaded machine
git can take longer: the hook then gives other text, or none. While the tests run, each git call of the helper that
has a limit gets at least GIT_TEST_LIMIT seconds, at the subprocess boundary (patient_run). The helper's own limits
do not change: patient_run first notes the limit that the helper asked for, and test_hooks_pass_their_git_limit
checks those. A limit below MIN_GIT_LIMIT, the product's smallest git limit (HOOK_GIT_MIN: a hook does not start git
with less time left), is broken: patient_run lets it expire at once (TimeoutExpired), as it would on any machine
where git takes time, so the suite sees it in any git call.

The hook deadline reads the helper's hook_clock(). In the tests that clock is HOOK_CLOCK, which stands still: in
this process load() puts it in place of hook_clock and HOOK_STARTED, and in a child this file puts it in place of
time.monotonic before the script starts. A hook therefore asks git for exactly its deadline, however long the test
takes. Only git_takes() moves the clock: each git call then "takes" the given seconds, and expires (TimeoutExpired)
when it asked for less, so a test can play a slow git without waiting for it. In a child, everything that reads
time.monotonic by its attribute stands still too, also project_lock's wait (no child test takes that lock).

test_fleet.py loads the helper with load(). A test that runs the helper in a child process runs this file, which
runs the script as __main__ (its own `sys.exit(main())`, argument and exit handling):
    python3 patient_fleet.py <path of scripts/fleet> <fleet arguments...>
A test that runs the script directly instead gets the product's own limits: use this launcher (HELPER in
test_fleet.py) unless the test is about those limits. A test of what happens when a limit expires uses
git_times_out(), which raises subprocess.TimeoutExpired at the boundary; it never waits for a real limit.

In a child, two environment variables name files that get one JSON line for each event:
    PATIENT_GIT_LOG    each git call: its arguments and the limit that the helper asked for (before widening);
    PATIENT_SLEEP_LOG  each time.sleep request: the seconds and the file of the caller.
"""

import importlib.machinery
import importlib.util
import json
import os
import runpy
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

GIT_TEST_LIMIT = 300  # seconds
MIN_GIT_LIMIT = 0.25  # seconds: the helper's smallest git limit, HOOK_GIT_MIN (a test checks that they agree)

_real_run = subprocess.run  # taken before any patch, so that patient_run never calls itself
_real_sleep = time.sleep
_lock = threading.Lock()
_records: list[list] = []  # the lists of record_git_limits() that are open, in this process
_timing_out = [0]  # open git_times_out() scopes
_git_takes: list[float] = []  # open git_takes() scopes: the innermost one counts


class StoppedClock:
    """A monotonic clock that stands still; git_takes() moves it."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


HOOK_CLOCK = StoppedClock()


def _note(path_var: str, entry: dict) -> None:
    path = os.environ.get(path_var)
    if path:
        with _lock, open(path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")


def patient_run(cmd, *args, **kwargs):
    """subprocess.run, with at least GIT_TEST_LIMIT seconds for a git call that has a limit. It notes the limit
    that the caller asked for first. With git_times_out(), or a limit below MIN_GIT_LIMIT, it raises
    TimeoutExpired instead of running git."""
    if cmd and os.path.basename(str(cmd[0])) == "git":
        asked = kwargs.get("timeout")
        with _lock:
            for record in _records:
                record.append((list(map(str, cmd[1:])), asked))
        _note("PATIENT_GIT_LOG", {"args": list(map(str, cmd[1:])), "timeout": asked})
        if _timing_out[0]:
            raise subprocess.TimeoutExpired(cmd, asked, output="patient_fleet: git_times_out() is open")
        with _lock:
            takes = _git_takes[-1] if _git_takes else None
            if takes is not None:
                HOOK_CLOCK.now += takes if asked is None else min(asked, takes)
        if takes is not None and asked is not None and asked < takes:
            raise subprocess.TimeoutExpired(cmd, asked, output=f"patient_fleet: git takes {takes} s, the helper "
                                            f"asked for {asked} s")
        if asked is not None and asked < MIN_GIT_LIMIT:
            raise subprocess.TimeoutExpired(cmd, asked, output=f"patient_fleet: the helper asked git for {asked} s, "
                                            f"below the smallest product limit, {MIN_GIT_LIMIT} s")
        if asked is not None:
            kwargs["timeout"] = max(asked, GIT_TEST_LIMIT)
    return _real_run(cmd, *args, **kwargs)


@contextmanager
def record_git_limits():
    """A list that gets (arguments, the limit asked for) of each git call in this process while it is open."""
    record: list = []
    with _lock:
        _records.append(record)
    try:
        yield record
    finally:
        with _lock:
            _records[:] = [r for r in _records if r is not record]  # by identity: an equal list may be open too


@contextmanager
def git_times_out():
    """Each git call in this process raises subprocess.TimeoutExpired at the boundary, as when its limit expires,
    until every open scope has closed."""
    with _lock:
        _timing_out[0] += 1
    try:
        yield
    finally:
        with _lock:
            _timing_out[0] -= 1


@contextmanager
def git_takes(seconds: float):
    """Each git call in this process moves HOOK_CLOCK on by `seconds`, as if git took that long, then runs; a call
    that asked for less moves the clock by its limit and raises TimeoutExpired. The clock is put back at the end."""
    with _lock:
        start = HOOK_CLOCK.now
        _git_takes.append(seconds)
    try:
        yield HOOK_CLOCK
    finally:
        with _lock:
            _git_takes.pop()
            HOOK_CLOCK.now = start


def noting_sleep(seconds):
    """time.sleep, which notes each request and the file of its caller in PATIENT_SLEEP_LOG."""
    _note("PATIENT_SLEEP_LOG", {"seconds": seconds, "file": sys._getframe(1).f_code.co_filename})
    return _real_sleep(seconds)


class PatientSubprocess:
    """The subprocess module, whose run is patient_run."""

    run = staticmethod(patient_run)

    def __getattr__(self, name):
        return getattr(subprocess, name)


def load(script):
    """The helper at `script` as a module named fleet_cli, whose git calls go through patient_run and whose hook
    deadline reads HOOK_CLOCK."""
    loader = importlib.machinery.SourceFileLoader("fleet_cli", str(script))
    spec = importlib.util.spec_from_loader("fleet_cli", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    module.subprocess = PatientSubprocess()
    module.hook_clock = HOOK_CLOCK
    module.HOOK_STARTED = HOOK_CLOCK.now
    return module


if __name__ == "__main__":
    script = sys.argv[1]
    subprocess.run = patient_run  # in place: the script's `import subprocess` binds this module
    time.monotonic = HOOK_CLOCK  # in place, before the script reads HOOK_STARTED (subprocess keeps its own clock)
    if os.environ.get("PATIENT_SLEEP_LOG"):
        time.sleep = noting_sleep
    sys.argv = [script, *sys.argv[2:]]
    runpy.run_path(script, run_name="__main__")  # the script's own `sys.exit(main())` ends the process
