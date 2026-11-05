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
the subprocess module (its wait loop while git runs) is a real sleep and does not move the clock. A wait without end
would spin for ever on this clock, since no sleep waits: so a sleep that takes the clock past FakeClock.LIMIT seconds
from its start raises FakeClockLimit, a BaseException that no `except Exception` of the helper catches, and the test
fails instead of hanging (in a child, the process ends with a traceback and exit code 1). The largest fake time of a
normal full run is 30 s (a child's `orch release`; 13 s in this process): the limit is ten times that. In this process
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
    PATIENT_GIT_TAKES  seconds: the child runs as inside git_takes(PATIENT_GIT_TAKES);
    PATIENT_RAISE_IN   NAME or NAME:TYPE: the first call of the script's function NAME raises TYPE, RuntimeError
                       (the default), SystemExit (code 7), KeyboardInterrupt or FakeClockLimit (through sys.settrace,
                       which then stops), so a test can make the real entry fail anywhere. An injection that never
                       fired fails the child: when the script ends (SystemExit too), the launcher writes
                       "patient_fleet: PATIENT_RAISE_IN=... never fired" to stderr and exits NEVER_FIRED (97).

A child can also stand in for a second fleet command that runs at the same time as another one (the races of the
lane owner, test_fleet.OwnerRaceTests). Then the clock is the real one (a held project lock must make the other
command wait for real), and Herdr, gh and the panes are stubs. Such a race has one time bound only, the test's
watchdog (RACE_WATCHDOG real seconds for the whole test): no wait of a race expires before it, whatever the git calls
of the other child take (each may take GIT_TEST_LIMIT, and a child makes several before it meets the other). The
test passes the seconds left of its watchdog to each child (PATIENT_WATCHDOG); a pause and a wait for the project
lock (the script's project_lock without its own `wait`) end when their condition comes, when the parent test ends
(it kills its children; a pause also ends when its parent process is gone), or at that watchdog:
    PATIENT_SERVICES   a JSON file {"stubs": [...], "checkouts": [...], "pane_state": "dead"}: enumerate_lanes gives
                       stubs and checkouts, pane_state the state, pane_info {}, the pull request lookup none,
                       require_herdr passes, herdr() raises (no Herdr at all), and project_lock waits until the
                       watchdog;
    PATIENT_WATCHDOG   seconds: the time left of the test's watchdog when it started the child (RACE_WATCHDOG
                       without it);
    PATIENT_CALL       NAME: call the script's function NAME with the JSON list PATIENT_ARGS as its arguments and
                       print its result as JSON, instead of main();
    PATIENT_SYNC       a directory for the files below, and PATIENT_ID the child's name in it;
    PATIENT_PAUSE      NAME or after:NAME: at the first call of the script's function NAME (or when it returns), the
                       child creates <sync>/<id>.ready and waits until <sync>/<id>.go exists (until its parent
                       is gone or the watchdog expires at the latest, then it fails: no test hangs);
    PATIENT_NOTE       NAME: each call of the script's function NAME creates <sync>/<id>.<NAME> (it never waits).
PATIENT_PAUSE and PATIENT_NOTE trace calls with sys.settrace, as PATIENT_RAISE_IN does: a child uses one or the other.

A test's calendar goes to its child too (test_fleet.helper_env), so that both name and lock the same month's log;
helper_env also gives the child the parent's TZ, so that the child turns these seconds back into the parent's
calendar:
    PATIENT_CALENDAR   seconds since the epoch: time.strftime and time.localtime without a time give this instant;
    PATIENT_TIME       seconds since the epoch: time.time gives this instant (the parent's stopped wall clock).
datetime stays real, as in the parent: it only stamps events and never names a file.
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
NEVER_FIRED = 97  # the exit code of a child whose PATIENT_RAISE_IN never fired
RACE_WATCHDOG = 4 * GIT_TEST_LIMIT  # real seconds for a whole race test (test_fleet.OwnerRaceTests): its one bound
MIN_GIT_LIMIT = 0.25  # seconds: the helper's smallest git limit, HOOK_GIT_MIN (a test checks that they agree)

_real_run = subprocess.run  # taken before any patch, so that patient_run never calls itself
_real_sleep = time.sleep
_real_monotonic = time.monotonic
_lock = threading.Lock()
_records: list[list] = []  # the lists of record_git_limits() that are open, in this process
_timing_out = [0]  # open git_times_out() scopes
_git_takes: list[float] = []  # open git_takes() scopes: the innermost one counts


class FakeClockLimit(BaseException):
    """A sleep of the helper took the fake clock past FakeClock.LIMIT: a wait without end. Not an Exception, so that
    the helper's own `except Exception` (record, the hook's catch-all) lets it through."""


class FakeClock:
    """The helper's monotonic clock and sleep in the tests: it stands still, git_takes() and the helper's sleeps move
    it, and a sleep of the subprocess module is real."""

    START = 1000.0
    LIMIT = 300.0  # fake seconds from START: a sleep that passes it raises FakeClockLimit


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
        self.advance(seconds)

    def advance(self, seconds) -> None:
        """The helper slept `seconds`: move the clock, or raise FakeClockLimit past the limit."""
        with _lock:
            self.now += max(0.0, float(seconds))
            spent = self.now - self.START
        if spent > self.LIMIT:
            raise FakeClockLimit(f"patient_fleet: the helper slept the fake clock to {spent:.2f} s from its start, "
                                 f"past its limit of {self.LIMIT} s: a wait without end")


HOOK_CLOCK = FakeClock()


class FakeTime:
    """The time module as load() gives it to the script: monotonic and sleep are HOOK_CLOCK's, the rest is real."""

    def monotonic(self):
        return HOOK_CLOCK.monotonic()

    def sleep(self, seconds):
        caller = sys._getframe(1).f_code.co_filename
        _note("PATIENT_SLEEP_LOG", {"seconds": seconds, "file": caller})
        HOOK_CLOCK.advance(seconds)

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


def _touch(name: str) -> None:
    open(os.path.join(os.environ["PATIENT_SYNC"], f"{os.environ['PATIENT_ID']}.{name}"), "a").close()


_parent = os.getppid()  # the test that started this child
_watchdog_end = _real_monotonic() + float(os.environ.get("PATIENT_WATCHDOG") or RACE_WATCHDOG)


def _watchdog_left() -> float:
    """Real seconds until the test's watchdog expires (0 after it)."""
    return max(0.0, _watchdog_end - _real_monotonic())


def _pause() -> None:
    """PATIENT_PAUSE: say ready, then wait for the go file (real time) until the parent test is gone or its
    watchdog expires."""
    _touch("ready")
    go = os.path.join(os.environ["PATIENT_SYNC"], f"{os.environ['PATIENT_ID']}.go")
    while not os.path.exists(go):
        if os.getppid() != _parent:
            raise RuntimeError(f"patient_fleet: the test that started this child is gone; no {go}")
        if not _watchdog_left():
            raise RuntimeError(f"patient_fleet: no {go} before the test's watchdog")
        _real_sleep(0.01)


def _stub_services(g: dict) -> None:
    """PATIENT_SERVICES: Herdr, gh and the panes as stubs, in the script's own globals."""
    with open(os.environ["PATIENT_SERVICES"]) as fh:
        spec = json.load(fh)
    project_lock = g["project_lock"]
    # A wait for the lock ends at the test's watchdog: the holder may pause, and run git before its pause.
    g["project_lock"] = lambda home, wait=None: project_lock(home, _watchdog_left() if wait is None else wait)
    g["require_herdr"] = lambda: None
    g["enumerate_lanes"] = lambda: (spec.get("stubs", []), spec.get("checkouts", []))
    g["pane_state"] = lambda pane: spec.get("pane_state", "dead")
    g["pane_info"] = lambda pane: {}
    g["pr_for_branch"] = lambda *a, **k: (None, None)

    def herdr(*args, **kwargs):
        raise g["FleetError"](f"patient_fleet: no herdr here ({' '.join(map(str, args))})")
    g["herdr"] = herdr


if __name__ == "__main__":
    script = sys.argv[1]
    subprocess.run = patient_run  # in place: the script's `import subprocess` binds this module
    services = os.environ.get("PATIENT_SERVICES")
    if not services:
        # In place, before the script reads HOOK_STARTED. subprocess and threading keep their own monotonic (bound
        # when they were imported); subprocess's sleeps stay real (FakeClock.sleep). With PATIENT_SERVICES the
        # clock stays real: another child holds the project lock for real.
        time.monotonic = HOOK_CLOCK.monotonic
        time.sleep = HOOK_CLOCK.sleep
    if os.environ.get("PATIENT_GIT_TAKES"):
        _git_takes.append(float(os.environ["PATIENT_GIT_TAKES"]))
    if os.environ.get("PATIENT_CALENDAR"):
        calendar = time.localtime(float(os.environ["PATIENT_CALENDAR"]))
        real_strftime, real_localtime = time.strftime, time.localtime
        time.strftime = lambda fmt, t=None: real_strftime(fmt, calendar if t is None else t)
        time.localtime = lambda secs=None: calendar if secs is None else real_localtime(secs)
    if os.environ.get("PATIENT_TIME"):
        stopped = float(os.environ["PATIENT_TIME"])
        time.time = lambda: stopped
    pause, notes = os.environ.get("PATIENT_PAUSE"), set(filter(None, os.environ.get("PATIENT_NOTE", "").split(",")))
    if pause or notes:
        when, _, paused_fn = pause.rpartition(":") if pause else ("", "", None)
        script_file, paused = os.path.realpath(script), []

        def _sync_trace(frame, event, arg):
            if event != "call" or os.path.realpath(frame.f_code.co_filename) != script_file:
                return None
            name = frame.f_code.co_name
            if name in notes:
                _touch(name)
            if name == paused_fn and not paused:
                paused.append(name)
                if when != "after":
                    _pause()
                    return None

                def _on_return(frame, event, arg):
                    if event == "return":
                        _pause()
                    return _on_return
                return _on_return
            return None
        sys.settrace(_sync_trace)
    injection, fired = os.environ.get("PATIENT_RAISE_IN"), []
    if injection:
        target, _, kind = injection.partition(":")
        script_path = os.path.realpath(script)
        make = {"": RuntimeError, "RuntimeError": RuntimeError, "SystemExit": lambda _: SystemExit(7),
                "KeyboardInterrupt": KeyboardInterrupt, "FakeClockLimit": FakeClockLimit}[kind]

        def _raise_in(frame, event, arg):
            if event == "call" and frame.f_code.co_name == target and \
                    os.path.realpath(frame.f_code.co_filename) == script_path:
                fired.append(target)
                raise make(f"patient_fleet: injected in {target}")
            return None
        sys.settrace(_raise_in)
    sys.argv = [script, *sys.argv[2:]]
    if services:
        g = runpy.run_path(script, run_name="patient_fleet_script")["main"].__globals__  # the functions' own globals
        _stub_services(g)
        call = os.environ.get("PATIENT_CALL")
        if call:
            result = g[call](*json.loads(os.environ.get("PATIENT_ARGS", "[]")))
            print(json.dumps(result))
            sys.exit(0)
        sys.exit(g["main"](sys.argv[1:]))
    try:
        runpy.run_path(script, run_name="__main__")  # the script's own `sys.exit(main())` ends the process
    finally:
        if injection and not fired:
            sys.stdout.flush()
            sys.stderr.write(f"patient_fleet: PATIENT_RAISE_IN={injection} never fired\n")
            sys.stderr.flush()
            os._exit(NEVER_FIRED)
