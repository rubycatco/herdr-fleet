"""The helper as the tests run it: the same script, with a long limit for each git call that it makes.

No test result may depend on how fast git answers, or on the wall clock. The helper gives some git calls a short
limit (in a hook, the time left until the hook's one git deadline; 3 s in git_toplevel), and on a loaded machine
git can take longer: the hook then gives other text, or none. While the tests run, each git call of the helper that
has a limit gets at least GIT_TEST_LIMIT seconds, at the subprocess boundary (patient_run). The helper's own limits
do not change: patient_run first notes the limit that the helper asked for, and test_hooks_pass_their_git_limit
checks those. A limit below MIN_GIT_LIMIT, the product's smallest git limit (HOOK_GIT_MIN: a hook does not start git
with less time left), is broken: patient_run lets it expire at once (TimeoutExpired), as it would on any machine
where git takes time, so the suite sees it in any git call.

The helper's clock is HOOK_CLOCK, a fake one, in this process and in a child: the product's own code (HOOK_STARTED,
hook_clock(), project_lock, the waits of ensure_project and record) runs unchanged and reads it through
time.monotonic and time.sleep. Its monotonic() stands still, so a hook asks git for exactly its deadline, however
long the test takes. It moves only in two ways: git_takes() says that each git call took some seconds (a call that
asked for less expires at its limit, TimeoutExpired), and each time.sleep of the helper moves it by the seconds asked
and returns at once. So a slow git, a held lock and every wait of the helper end without real waiting. A sleep of
the subprocess module (its wait loop while git runs) is a real sleep and does not move the clock. In this process
load() gives the script a time module whose monotonic and sleep are HOOK_CLOCK's (the test's own time module stays
real); in a child this file puts them in place of time.monotonic and time.sleep before the script starts. The clock
goes back to its start before each test (reset(); test_fleet.FleetTestCase).

test_fleet.py loads the helper with load(). A test that runs the helper in a child process runs this file, which
runs the script as __main__ (its own `sys.exit(main())`, argument and exit handling):
    python3 patient_fleet.py <path of scripts/fleet> <fleet arguments...>
A test that runs the script directly instead gets the product's own limits: use this launcher (HELPER in
test_fleet.py) unless the test is about those limits. A test of what happens when a limit expires uses
git_times_out(), which raises subprocess.TimeoutExpired at the boundary; it never waits for a real limit.

In a child, environment variables name files that get one JSON line for each event, and play a slow git:
    PATIENT_GIT_LOG    each git call: its arguments and the limit that the helper asked for (before widening);
    PATIENT_SLEEP_LOG  each time.sleep request: the seconds and the file of the caller;
    PATIENT_GIT_TAKES  seconds: the child runs as inside git_takes(PATIENT_GIT_TAKES).
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

# The script's other imports, loaded here with the real time module: load() lends the script its fake time module
# while it runs the script's imports, and a module imported first then would keep the fake one.
import argparse, concurrent.futures, datetime, fcntl, glob, shlex, shutil, textwrap, uuid  # noqa: E401,F401

GIT_TEST_LIMIT = 300  # seconds
MIN_GIT_LIMIT = 0.25  # seconds: the helper's smallest git limit, HOOK_GIT_MIN (a test checks that they agree)

_real_run = subprocess.run  # taken before any patch, so that patient_run never calls itself
_real_sleep = time.sleep
_real_monotonic = time.monotonic
_lock = threading.Lock()
_records: list[list] = []  # the lists of record_git_limits() that are open, in this process
_timing_out = [0]  # open git_times_out() scopes
_git_takes: list[float] = []  # open git_takes() scopes: the innermost one counts


class FakeClock:
    """The helper's monotonic clock and sleep in the tests: it stands still, git_takes() and the helper's sleeps move
    it, and a sleep of the subprocess module is real."""

    START = 1000.0

    def __init__(self):
        self.now = self.START

    def reset(self):
        with _lock:
            self.now = self.START

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds) -> None:
        caller = sys._getframe(1).f_code.co_filename
        _note("PATIENT_SLEEP_LOG", {"seconds": seconds, "file": caller})
        if os.path.realpath(caller) == os.path.realpath(subprocess.__file__):
            return _real_sleep(seconds)  # subprocess waits for git in short steps: real time, not the helper's
        with _lock:
            self.now += max(0.0, float(seconds))


HOOK_CLOCK = FakeClock()


class FakeTime:
    """The time module as load() gives it to the script: monotonic and sleep are HOOK_CLOCK's, the rest is real."""

    def monotonic(self):
        return HOOK_CLOCK.monotonic()

    def sleep(self, seconds):
        caller = sys._getframe(1).f_code.co_filename
        _note("PATIENT_SLEEP_LOG", {"seconds": seconds, "file": caller})
        with _lock:
            HOOK_CLOCK.now += max(0.0, float(seconds))

    def __getattr__(self, name):
        return getattr(time, name)


FAKE_TIME = FakeTime()


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


class PatientSubprocess:
    """The subprocess module, whose run is patient_run."""

    run = staticmethod(patient_run)

    def __getattr__(self, name):
        return getattr(subprocess, name)


def load(script):
    """The helper at `script` as a module named fleet_cli, whose git calls go through patient_run and whose time
    module is FAKE_TIME, from its first line on: its own HOOK_STARTED and hook_clock() read HOOK_CLOCK."""
    loader = importlib.machinery.SourceFileLoader("fleet_cli", str(script))
    spec = importlib.util.spec_from_loader("fleet_cli", loader)
    module = importlib.util.module_from_spec(spec)
    real_time = sys.modules["time"]
    sys.modules["time"] = FAKE_TIME  # the script's `import time` binds it
    try:
        loader.exec_module(module)
    finally:
        sys.modules["time"] = real_time
    module.subprocess = PatientSubprocess()
    return module


if __name__ == "__main__":
    script = sys.argv[1]
    subprocess.run = patient_run  # in place: the script's `import subprocess` binds this module
    # In place, before the script reads HOOK_STARTED. subprocess and threading keep their own monotonic (bound when
    # they were imported); subprocess's sleeps stay real (FakeClock.sleep).
    time.monotonic = HOOK_CLOCK.monotonic
    time.sleep = HOOK_CLOCK.sleep
    if os.environ.get("PATIENT_GIT_TAKES"):
        _git_takes.append(float(os.environ["PATIENT_GIT_TAKES"]))
    sys.argv = [script, *sys.argv[2:]]
    runpy.run_path(script, run_name="__main__")  # the script's own `sys.exit(main())` ends the process
