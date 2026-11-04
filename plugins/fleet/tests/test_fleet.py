"""Unit tests for scripts/fleet. Run: python3 -m unittest discover -s plugins/fleet/tests"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager, nullcontext, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # also when unittest gets this file by its path
import patient_fleet  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fleet"
# Each git call of the helper gets a long limit while the tests run (see patient_fleet.py), here and in a child.
fleet = patient_fleet.load(SCRIPT)
# The helper in a child process, run as __main__ by patient_fleet.py. A child that runs SCRIPT directly gets the
# product's own limits: use HELPER unless the test is about those limits. A test of an expired limit uses
# patient_fleet.git_times_out() (an injected TimeoutExpired), never a real wait.
HELPER = [sys.executable, str(Path(patient_fleet.__file__).resolve()), str(SCRIPT)]
CHILD_LIMIT = 300  # seconds for a child process: a guard against a hang, never a measure of speed
NOTE = "could not confirm that this session works in that project"  # the note for an unconfirmed orch pane

# No test writes to the real ~/.fleet, also not through a subprocess.
_FLEET_HOME = tempfile.TemporaryDirectory()
os.environ["FLEET_HOME"] = _FLEET_HOME.name

# No test runs herdr or gh through fleet.run: a test that needs them stubs fleet.run (or the function that calls
# it). git runs for real.
_UNSTUBBED = []
_REAL_RUN = fleet.run


def _guarded_run(cmd, *args, **kwargs):
    if cmd and os.path.basename(str(cmd[0])) in ("herdr", "gh"):
        _UNSTUBBED.append(" ".join(map(str, cmd)))
        raise AssertionError(f"a test ran {cmd[0]} without a stub: {cmd}")
    return _REAL_RUN(cmd, *args, **kwargs)


fleet.run = _guarded_run


def tearDownModule():
    _FLEET_HOME.cleanup()
    # A guard error that a caller swallowed still fails the run here.
    if _UNSTUBBED:
        raise AssertionError(f"tests ran herdr or gh without a stub: {_UNSTUBBED}")

REMUDA_LIST = """\
ACCOUNT         EMAIL                  ORG                            PLAN  HOME
claude:default  a@example.com          a@example.com's Organization   max   default
claude:max      b@example.com          Some Person                    max   /home/u/.profiles/max
claude:team     c@example.org          Example Team                   team  /home/u/.profiles/team
codex:default   logged in (ChatGPT)    -                              -     default
codex:v0        logged in (ChatGPT)    -                              -     /home/u/.remuda/homes/codex/v0
"""

CLAUDE_SCREEN = """\
 ▐▛███▛█   Claude Code v2.1.317
❯ Reply with the single word OK and stop.
⏺ OK
✻ Sautéed for 2s · done 3:35 PM
❯ Reply with the single word TWO and stop.
⏺ TWO
❯
  fleet-smoke │ Opus 5.5 │ 4% (47k/1000k) │ 7d:37% │ 0m v2.1.317
  ⏵⏵ auto mode on (shift+tab to cycle)
"""

CLAUDE_STUCK = """\
❯ Reply with the single word OK and stop.
⏺ OK
❯ [Pasted text #1 +8 lines]
  fleet-smoke │ Opus 5.5 │ 4% (47k/1000k)
"""

CODEX_SCREEN = """\
  >_ OpenAI Codex (v0.164.0)
› Reply with the single word OK and stop.
• Starting MCP servers (6/8): codex_apps (3s • esc to interrupt)
› Ask Codex to do anything
  model-x high · /tmp/x
"""

CODEX_TRUST_SCREEN = """\
  Folder access
  /tmp/x
  Trust this folder? Codex can read, edit, and run files here, subject to your permission settings.
› 1. Trust and continue
  2. Quit
  enter continue · esc quit
"""

TRUST_SCREEN = """\
 Accessing workspace:
 /tmp/x
 Quick safety check: Is this a project you created or one you trust?
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
"""


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class FleetTestCase(unittest.TestCase):
    """Each test starts with the helper's fake clock at its start (patient_fleet.HOOK_CLOCK): a wait of an earlier
    test (its sleeps move the clock) cannot spend the time of a hook that this test runs."""

    def run(self, result=None):
        patient_fleet.HOOK_CLOCK.reset()
        return super().run(result)


class Clock:
    """fleet.time for a test: the real time module, with a calendar that is fixed when the test begins and a wall
    clock that the test can stop.

    strftime() without a time gives the fixed calendar, so the helper and the test name the same month's log even
    when a month ends during the test. That holds for every such strftime, not only the month: spawned_at and the
    time in the names of handoff and inbox files stay at the second when the test began (with the real clock,
    two handoffs in one second get the same name too). time() is the real clock until the test sets `stopped` (seconds since the
    epoch); then an age that the helper computes is the same however long the test takes. monotonic() and sleep()
    stay the harness's fake clock (patient_fleet.HOOK_CLOCK), so the project lock and its deadlines work as they do,
    without real waiting."""

    def __init__(self):
        self.calendar = time.localtime()
        self.stopped = None
        self.base = fleet.time  # patient_fleet.FAKE_TIME: its monotonic and sleep are the fake clock's

    def time(self):
        return time.time() if self.stopped is None else self.stopped

    def strftime(self, fmt, t=None):
        return time.strftime(fmt, self.calendar if t is None else t)

    def __getattr__(self, name):
        return getattr(self.base, name)

    def child_env(self) -> dict:
        """The environment that gives a HELPER child this calendar and stopped clock (see patient_fleet.py)."""
        env = {"PATIENT_CALENDAR": repr(time.mktime(self.calendar))}
        if self.stopped is not None:
            env["PATIENT_TIME"] = repr(self.stopped)
        return env


def helper_env(env: dict) -> dict:
    """`env` for a HELPER child, with the test's calendar when the test has a Clock, and the parent's TZ (none when
    the parent has none): parent and child then name and lock the same month's log, also when a month ends during
    the test and whatever the timezone."""
    env = {k: v for k, v in env.items() if k != "TZ"}
    if os.environ.get("TZ") is not None:
        env["TZ"] = os.environ["TZ"]
    return {**env, **fleet.time.child_env()} if isinstance(fleet.time, Clock) else env


def use_clock(test):
    """Give the helper a Clock for the length of `test`, and return it."""
    clock = Clock()
    patch = mock.patch.object(fleet, "time", clock)
    patch.start()
    test.addCleanup(patch.stop)
    return clock


def install_stale_monitor(root, repo, lane, run_git):
    """A protocol-v2 file system monitor that answers with a token and no paths, so it misses every change, and a
    warmed untracked cache: plain `git status` in `lane` then reports no change made after this call."""
    monitor = Path(root) / "monitor"
    monitor.write_text("#!/bin/sh\nprintf 'token\\000'\n")
    monitor.chmod(0o755)
    for key, value in (("core.fsmonitor", str(monitor)), ("core.fsmonitorHookVersion", "2"),
                       ("core.untrackedCache", "true")):
        run_git(repo, "config", key, value)
    for _ in range(3):
        run_git(lane, "status", "--porcelain", "-z", "--untracked-files=normal")


class AccountTests(FleetTestCase):
    def setUp(self):
        self.accounts = fleet.parse_remuda_list(REMUDA_LIST)

    def test_parse_remuda_list(self):
        self.assertEqual([a["account"] for a in self.accounts],
                         ["claude:default", "claude:max", "claude:team", "codex:default", "codex:v0"])
        self.assertEqual(self.accounts[1]["home"], "/home/u/.profiles/max")
        self.assertEqual(self.accounts[2]["plan"], "team")
        self.assertEqual(self.accounts[3]["home"], "default")

    def test_resolve_bare_name(self):
        self.assertEqual(fleet.resolve_account("max", None, self.accounts)["account"], "claude:max")

    def test_resolve_ambiguous_needs_agent(self):
        with self.assertRaises(fleet.FleetError):
            fleet.resolve_account("default", None, self.accounts)
        self.assertEqual(fleet.resolve_account("default", "codex", self.accounts)["account"], "codex:default")

    def test_resolve_provider_mismatch(self):
        with self.assertRaises(fleet.FleetError):
            fleet.resolve_account("claude:team", "codex", self.accounts)

    def test_resolve_unknown(self):
        with self.assertRaisesRegex(fleet.FleetError, "no remuda account"):
            fleet.resolve_account("nope", "claude", self.accounts)

    def test_default_agent(self):
        self.assertEqual(fleet.default_agent("default", self.accounts), "claude")
        self.assertEqual(fleet.default_agent("v0", self.accounts), "codex")
        self.assertEqual(fleet.default_agent("codex:default", self.accounts), "codex")
        self.assertEqual(fleet.default_agent(None, self.accounts), "claude")
        self.assertEqual(fleet.default_agent("max", None), "claude")

    def test_caller_account(self):
        env = {"CLAUDE_CONFIG_DIR": "/home/u/.profiles/max"}
        self.assertEqual(fleet.caller_account("claude", self.accounts, env)["account"], "claude:max")
        self.assertEqual(fleet.caller_account("claude", self.accounts, {})["account"], "claude:default")
        self.assertEqual(fleet.caller_account("codex", self.accounts, env)["account"], "codex:default")


class LaunchTests(FleetTestCase):
    def setUp(self):
        self.accounts = fleet.parse_remuda_list(REMUDA_LIST)

    def launch(self, **kw):
        base = dict(agent="claude", account=None, model=None, permission_mode=None, name=None, prompt=None,
                    extra=[], remuda=None, accounts=None, env={})
        base.update(kw)
        return fleet.build_launch(**base)

    def test_plain_mode(self):
        out = self.launch(model="opus", prompt="Read .fleet/BRIEF.md and execute it.")
        self.assertEqual(out["command"], "claude --model opus 'Read .fleet/BRIEF.md and execute it.'")
        self.assertIsNone(out["account"])

    def test_plain_mode_keeps_caller_config_dir_unquoted_assignment(self):
        out = self.launch(env={"CLAUDE_CONFIG_DIR": "/x y/claude"})
        self.assertEqual(out["command"], "CLAUDE_CONFIG_DIR='/x y/claude' claude")
        self.assertEqual(out["home"], "/x y/claude")

    def test_home_follows_the_chosen_account(self):
        row = self.launch(account="max", remuda="/bin/remuda", accounts=self.accounts)
        self.assertEqual(row["home"], "/home/u/.profiles/max")
        default = self.launch(account="claude:default", remuda="/bin/remuda", accounts=self.accounts)
        self.assertEqual(default["home"], os.path.expanduser("~/.claude"))
        codex = self.launch(agent="codex", account="v0", remuda="/bin/remuda", accounts=self.accounts)
        self.assertEqual(codex["home"], "/home/u/.remuda/homes/codex/v0")

    def test_plain_mode_rejects_account(self):
        with self.assertRaisesRegex(fleet.FleetError, "accounts need remuda"):
            self.launch(account="max")

    def test_remuda_explicit(self):
        out = self.launch(account="team", name="lane-a", permission_mode="auto", remuda="/bin/remuda",
                          accounts=self.accounts, prompt="Go.")
        self.assertEqual(out["command"], "/bin/remuda run claude:team --permission-mode auto -n lane-a Go.")
        self.assertEqual(out["account_source"], "explicit")

    def test_remuda_defaults_to_caller_account(self):
        out = self.launch(remuda="/bin/remuda", accounts=self.accounts,
                          env={"CLAUDE_CONFIG_DIR": "/home/u/.profiles/max"})
        self.assertEqual(out["account"], "claude:max")
        self.assertEqual(out["account_source"], "caller")

    def test_codex_extra_args_and_no_permission_mode(self):
        out = self.launch(agent="codex", model="gpt-6", extra=["--full-auto"], prompt="Review.")
        self.assertEqual(out["command"], "codex --model gpt-6 --full-auto Review.")
        with self.assertRaises(fleet.FleetError):
            self.launch(agent="codex", permission_mode="auto")

    def test_effort_maps_per_agent(self):
        claude = self.launch(model="opus", effort="high", permission_mode="auto")
        self.assertEqual(claude["command"], "claude --model opus --effort high --permission-mode auto")
        codex = self.launch(agent="codex", account="v0", model="model-x", effort="max",
                            remuda="/bin/remuda", accounts=self.accounts, prompt="Review.")
        self.assertEqual(codex["command"],
                         "/bin/remuda run codex:v0 --model model-x -c 'model_reasoning_effort=\"max\"' Review.")
        with self.assertRaisesRegex(fleet.FleetError, "one lowercase word"):
            self.launch(effort="High")

    def test_main_passes_args_after_double_dash(self):
        buf = io.StringIO()
        old = os.environ.get("FLEET_NO_REMUDA")
        os.environ["FLEET_NO_REMUDA"] = "1"
        try:
            with redirect_stdout(buf):
                rc = fleet.main(["spawn", "--dry-run", "--agent", "claude", "--", "--effort", "high"])
        finally:
            if old is None:
                os.environ.pop("FLEET_NO_REMUDA", None)
            else:
                os.environ["FLEET_NO_REMUDA"] = old
        self.assertEqual(rc, 0)
        self.assertIn("--effort high", json.loads(buf.getvalue())["command"])

    def test_spawn_rejects_long_or_multiline_prompt(self):
        from contextlib import redirect_stderr
        for prompt in ("line one\nline two", "x" * (fleet.PROMPT_MAX + 1)):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = fleet.main(["spawn", "--dry-run", "--prompt", prompt])
            self.assertEqual(rc, 1)
            self.assertIn("one line", err.getvalue())


    def test_spawn_task_needs_a_role_and_a_file(self):
        from contextlib import redirect_stderr
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FLEET_NO_REMUDA": "1"}):
            task = Path(tmp) / "q.md"
            for argv, msg in ((["--task", str(task)], "needs --role"),
                              (["--role", "researcher", "--task", str(task)], "no such file")):
                err = io.StringIO()
                with redirect_stderr(err):
                    rc = fleet.main(["spawn", "--dry-run", *argv])
                self.assertEqual(rc, 1)
                self.assertIn(msg, err.getvalue())
            task.write_text("q")
            buf = io.StringIO()
            with chdir(tmp), redirect_stdout(buf):
                rc = fleet.main(["spawn", "--dry-run", "--role", "researcher", "--task", "q.md"])
            self.assertEqual((rc, json.loads(buf.getvalue())["task"]), (0, str(task.resolve())))

    def test_spawn_keeps_the_task_with_the_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(os.path.realpath(tmp)) / "repo"
            repo.mkdir()
            git("init", "-q", "-b", "main", cwd=repo)
            task = Path(os.path.realpath(tmp)) / "research.md"
            task.write_text("q")
            herdr_calls = []

            def fake_run(cmd, *a, **k):
                if cmd[0] == "herdr":
                    herdr_calls.append(cmd)
                    return subprocess.CompletedProcess(cmd, 0, "{}", "")
                return _guarded_run(cmd, *a, **k)
            target = {"pane_id": "w2:p5", "cwd": str(repo), "workspace_id": "w2", "placement": "split"}
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {"FLEET_NO_REMUDA": "1"}), mock.patch.object(fleet, "run", fake_run), \
                    mock.patch.object(fleet, "require_herdr"), mock.patch.object(fleet, "create_pane", return_value=target), \
                    mock.patch.object(fleet, "pane_info", return_value={}), redirect_stdout(buf):
                rc = fleet.main(["spawn", "--role", "researcher", "--task", str(task), "--no-wait", "--cwd", str(repo),
                                 "--prompt", f"Read {task} and follow it."])
            self.assertEqual(rc, 0)
            entry = fleet.load_panes(repo / ".fleet")["w2:p5"]
            self.assertEqual((entry["role"], entry["task"]), ("researcher", str(task)))
            self.assertEqual(json.loads(buf.getvalue())["task"], str(task))
            self.assertTrue(any(c[:3] == ["herdr", "pane", "run"] for c in herdr_calls))
            # Without --task, the entry has no task key, as before.
            target["pane_id"] = "w2:p6"
            with mock.patch.dict(os.environ, {"FLEET_NO_REMUDA": "1"}), mock.patch.object(fleet, "run", fake_run), \
                    mock.patch.object(fleet, "require_herdr"), mock.patch.object(fleet, "create_pane", return_value=target), \
                    mock.patch.object(fleet, "pane_info", return_value={}), redirect_stdout(io.StringIO()):
                fleet.main(["spawn", "--role", "impl", "--no-wait", "--cwd", str(repo)])
            self.assertNotIn("task", fleet.load_panes(repo / ".fleet")["w2:p6"])


class SpawnGateTests(FleetTestCase):
    """spawn refuses an implementer in a lane whose PLAN.md is not approved, before it creates anything."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.env = mock.patch.dict(os.environ, {"FLEET_HOME": str(self.root / "home"), "FLEET_NO_REMUDA": "1",
                                                "HERDR_PANE_ID": "w1:p1"})
        self.env.start()
        self.lane = self.root / "lane"
        self.lane.mkdir()
        git("init", "-q", "-b", "feat/x", cwd=self.lane)
        self.fleet_dir = Path(fleet.prep_dir(str(self.lane))["fleet_dir"])
        self.plan = self.fleet_dir / "PLAN.md"
        self.made = []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def spawn(self, *argv, pane_cwd=None):
        """Run spawn with herdr stubbed; returns (exit code, JSON out)."""
        pane_cwd = str(self.lane) if pane_cwd is None else pane_cwd

        def create(args, cwd=None, info=None):
            self.made.append(cwd)
            return {"pane_id": "w1:p9", "workspace_id": "w1", "cwd": cwd or pane_cwd, "placement": "split"}

        def run(cmd, *a, **k):
            if cmd[0] == "herdr":
                return subprocess.CompletedProcess(cmd, 0, "{}", "")
            return _guarded_run(cmd, *a, **k)
        buf = io.StringIO()
        with mock.patch.object(fleet, "require_herdr"), mock.patch.object(fleet, "run", run), \
                mock.patch.object(fleet, "create_pane", side_effect=create), \
                mock.patch.object(fleet, "pane_info", return_value={"cwd": pane_cwd}), \
                mock.patch.object(fleet, "herdr_ok", return_value=True), chdir(self.root), redirect_stdout(buf):
            code = fleet.main(["spawn", "--no-wait", *argv])
        return code, json.loads(buf.getvalue())

    def events(self):
        return [e["event"] for e in fleet.read_events([fleet.project_for(str(self.lane))])]

    def refused(self, *argv, reason, **kw):
        code, out = self.spawn(*argv, **kw)
        self.assertEqual((code, out["ok"], out["changed"]), (2, False, "nothing"), out)
        self.assertIn(reason, out["reason"])
        self.assertEqual(self.made, [])  # no pane
        self.assertEqual(fleet.load_panes(self.fleet_dir), {})  # no registry entry
        self.assertEqual(self.events(), [])  # no event
        return out

    def test_impl_and_no_role_wait_for_the_approved_round(self):
        for argv in (["--role", "impl"], []):
            self.plan.write_text("# Plan\nRisk: none\nPLAN READY: <n>\n")  # the template: no round yet
            out = self.refused(*argv, reason="has no PLAN READY line: the planner has not finished")
            self.assertEqual((out["plan"], out["round"]), (str(self.plan), None))
            self.plan.write_text("PLAN READY: 1\nPLAN APPROVED: 1 by orch 2026\nPLAN READY: 2\n")
            self.assertEqual(self.refused(*argv, reason="round 2 of")["round"], 2)
            self.refused(*argv, "--pane", "w1:p5", reason="round 2 of")  # in an existing pane
            self.refused(*argv, "--cwd", str(self.lane / ".fleet"), reason="round 2 of")  # below the git top
        self.plan.write_text("PLAN READY: 1\nPLAN APPROVED: 1 by orch 2026\nPLAN READY: 2\nPLAN APPROVED: 2 by user\n")
        code, out = self.spawn("--role", "impl")
        self.assertEqual((code, out["ok"], self.made), (0, True, [str(self.lane)]))
        self.assertEqual(fleet.load_panes(self.fleet_dir)["w1:p9"]["role"], "impl")
        self.assertEqual(self.events(), ["spawn", "spawn_result"])

    def test_other_roles_and_lanes_without_a_plan_pass(self):
        self.plan.write_text("PLAN READY: 1\n")
        for role in ("planner", "critic", "helper", "researcher", "orchestrator"):
            self.assertEqual(self.spawn("--role", role)[0], 0, role)
        self.plan.unlink()
        self.assertEqual(self.spawn("--role", "impl")[0], 0)
        self.assertEqual(self.spawn("--role", "impl", pane_cwd=str(self.root))[0], 0)  # not a git checkout

    def test_git_failure_with_a_plan_fails_closed(self):
        # When git cannot tell the checkout but a PLAN.md lies at or above the directory, the gate refuses.
        self.plan.write_text("PLAN READY: 1\nPLAN APPROVED: 1 by orch\n")
        with mock.patch.object(fleet, "git_toplevel", return_value=None):
            out = self.refused("--role", "impl", "--cwd", str(self.fleet_dir), reason="git cannot tell the checkout")
            self.assertEqual(out["plan"], str(self.plan))
            self.assertEqual(self.spawn("--role", "impl", "--cwd", str(self.root))[0], 0)  # no PLAN.md above

    def test_dry_run_and_a_pane_herdr_cannot_place(self):
        self.plan.write_text("PLAN READY: 1\n")
        buf = io.StringIO()
        with chdir(self.lane), redirect_stdout(buf):
            self.assertEqual(fleet.main(["spawn", "--dry-run", "--role", "impl"]), 0)  # it checks nothing
        self.plan.unlink()
        self.refused("--role", "impl", "--pane", "w1:p5", pane_cwd="", reason="herdr reports no directory")

    def test_an_existing_pane_is_read_once(self):
        # Through the real create_pane: one pane snapshot drives the gate, the registry, the event and the
        # target, for a gated role and for another role. The launch command is the one 0.6.0 ran: no `cd`.
        other = self.root / "elsewhere"
        other.mkdir()
        git("init", "-q", "-b", "main", cwd=other)
        self.plan.write_text("PLAN READY: 1\n")  # the lane's plan is not approved
        for role, pane in (("impl", "w1:p5"), ("critic", "w1:p6")):
            reads, runs = [], []

            def info(p):
                reads.append(("read", p))
                return {"cwd": str(other)} if len(reads) == 1 else {"cwd": str(self.lane)}

            def run(cmd, *a, **k):
                if cmd[0] == "herdr":
                    runs.append(cmd)
                    reads.append(tuple(cmd[:3]))
                    return subprocess.CompletedProcess(cmd, 0, "{}", "")
                return _guarded_run(cmd, *a, **k)
            buf = io.StringIO()
            argv = ["spawn", "--no-wait", "--pane", pane, "--role", role, "--prompt", "Read .fleet/BRIEF.md."]
            with mock.patch.object(fleet, "require_herdr"), mock.patch.object(fleet, "run", run), \
                    mock.patch.object(fleet, "pane_info", side_effect=info), \
                    mock.patch.object(fleet, "herdr_ok", return_value=True), chdir(self.root), redirect_stdout(buf):
                code = fleet.main(argv)
            out = json.loads(buf.getvalue())
            # One read before the launch; the read after it only looks up the new session, as in 0.6.0.
            before = reads[:reads.index(("herdr", "pane", "run"))]
            self.assertEqual((code, before, out["cwd"]), (0, [("read", pane)], str(other)), role)
            self.assertEqual(fleet.load_panes(other / ".fleet")[pane]["role"], role)
            self.assertNotIn(pane, fleet.load_panes(self.fleet_dir))
            [event] = [e for e in fleet.read_events([fleet.project_for(str(other))])
                       if e["event"] == "spawn" and e.get("pane") == pane]
            self.assertEqual(event["cwd"], str(other))
            # The launch equals the command that --dry-run prints for the same arguments.
            dry = io.StringIO()
            with chdir(self.root), redirect_stdout(dry):
                fleet.main([a for a in argv if a != "--no-wait"] + ["--dry-run"])
            [launch] = [c for c in runs if c[:3] == ["herdr", "pane", "run"]]
            self.assertEqual(launch, ["herdr", "pane", "run", pane, json.loads(dry.getvalue())["command"]])
            self.assertFalse(launch[4].startswith("cd "))

    def test_target_cwd_mirrors_create_pane(self):
        parse = lambda *argv: fleet.build_parser().parse_args(["spawn", *argv])
        with mock.patch.object(fleet, "pane_info", return_value={"cwd": "/src/pane"}) as info, chdir(self.root):
            self.assertEqual(fleet.target_cwd(parse("--pane", "w1:p5")), "/src/pane")
            self.assertIsNone(fleet.target_cwd(parse("--worktree", "feat/y")))
            self.assertEqual(fleet.target_cwd(parse("--tab")), str(self.root))
            self.assertEqual(fleet.target_cwd(parse("--new-workspace", "--cwd", "/a/b")), "/a/b")
            self.assertEqual(fleet.target_cwd(parse("--cwd", "/a/b")), "/a/b")  # a split with --cwd
            self.assertEqual(fleet.target_cwd(parse("--from", "w3:p3")), "/src/pane")  # the split pane's directory
            self.assertEqual(info.call_args_list[-1][0], ("w3:p3",))
            with mock.patch.dict(os.environ, {"HERDR_PANE_ID": ""}):
                self.assertIsNone(fleet.target_cwd(parse()))  # nothing to split: create_pane refuses
        # create_pane starts the split in the directory that target_cwd found.
        calls = []

        def herdr(*argv, **kw):
            calls.append(argv)
            return {"pane": {"pane_id": "w1:p7"}}
        with mock.patch.object(fleet, "herdr", side_effect=herdr), mock.patch.object(fleet, "pane_rect"), \
                mock.patch.object(fleet, "auto_direction", return_value="right"), \
                mock.patch.object(fleet, "find_pane_id", return_value="w1:p7"):
            out = fleet.create_pane(parse("--from", "w3:p3"), "/src/pane")
        self.assertEqual((out["cwd"], calls[0][:4], calls[0][calls[0].index("--cwd") + 1]),
                         ("/src/pane", ("pane", "split", "w3:p3", "--direction"), "/src/pane"))


class ScreenTests(FleetTestCase):
    def test_claude_composer_and_sent_count(self):
        lines = fleet.clean_lines(CLAUDE_SCREEN)
        self.assertEqual(fleet.composer_line(lines, "claude"), "")
        self.assertEqual(fleet.submitted_count(lines, fleet.text_head("Reply with the single word TWO and stop."), "claude"), 1)
        self.assertEqual(fleet.submitted_count(lines, fleet.text_head("Something never sent"), "claude"), 0)

    def test_claude_stuck_paste(self):
        lines = fleet.clean_lines(CLAUDE_STUCK)
        self.assertIn("[Pasted text", fleet.composer_line(lines, "claude"))

    def test_codex_prompt_counts_as_sent_during_mcp_start(self):
        lines = fleet.clean_lines(CODEX_SCREEN)
        self.assertEqual(fleet.composer_line(lines, "codex"), "Ask Codex to do anything")
        self.assertEqual(fleet.submitted_count(lines, fleet.text_head("Reply with the single word OK and stop."), "codex"), 1)

    def test_trust_dialog_detection_and_selection(self):
        lines = fleet.clean_lines(TRUST_SCREEN)
        self.assertTrue(fleet.TRUST_DIALOG.search("\n".join(lines)))
        sel = fleet.selected_line(lines)
        self.assertIn("No, exit", lines[sel])
        hits = [i for i, l in enumerate(lines) if fleet.TRUST_ACCEPT.search(l)]
        self.assertEqual(len(hits), 1)
        self.assertGreater(hits[0], sel)

    def test_codex_trust_dialog_and_unknown_menus(self):
        lines = fleet.clean_lines(CODEX_TRUST_SCREEN)
        text = "\n".join(lines)
        self.assertTrue(fleet.TRUST_DIALOG.search(text))
        self.assertTrue(fleet.TRUST_ACCEPT.search(lines[fleet.selected_line(lines)]))
        self.assertTrue(fleet.MENU_SELECTED.search(text))
        # A composer, or a prompt that was sent, is not a menu.
        for screen in (CODEX_SCREEN, CLAUDE_SCREEN):
            self.assertIsNone(fleet.MENU_SELECTED.search("\n".join(fleet.clean_lines(screen))))

    def test_clean_lines_drops_padding_and_rules(self):
        self.assertEqual(fleet.clean_lines("\n\n────────\n  text  \n\n"), ["  text"])

    def test_compact_line(self):
        self.assertEqual(fleet.compact_line(" " * 300 + "52277 tokens"), "52277 tokens")
        self.assertEqual(fleet.compact_line("─" * 150 + " fleet-hook " + "─" * 3), "── fleet-hook ──")
        self.assertEqual(len(fleet.compact_line("x" * 500)), 200)

    def test_auto_direction(self):
        self.assertEqual(fleet.auto_direction({"width": 172, "height": 105}), "down")
        self.assertEqual(fleet.auto_direction({"width": 344, "height": 60}), "right")


class DoctorTests(FleetTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_toml_top_level_stops_at_first_table(self):
        text = 'model = "model-x"  # main\nmodel_reasoning_effort = \'high\'\n\n[profiles.fast]\nmodel = "other"\n'
        self.assertEqual(fleet.toml_top_level(text), {"model": "model-x", "model_reasoning_effort": "high"})

    def test_agent_defaults(self):
        (self.home / "settings.json").write_text(json.dumps({"model": "opus", "effortLevel": "high"}))
        (self.home / "config.toml").write_text('model = "model-x"\nmodel_reasoning_effort = "xhigh"\n')
        self.assertEqual(fleet.agent_defaults("claude", str(self.home)), {"model": "opus", "effort": "high"})
        self.assertEqual(fleet.agent_defaults("codex", str(self.home)), {"model": "model-x", "effort": "xhigh"})
        self.assertEqual(fleet.agent_defaults("codex", str(self.home / "missing")), {"model": None, "effort": None})

    def test_codex_models_lists_visible_models(self):
        (self.home / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "model-x", "visibility": "list", "default_reasoning_level": "medium",
             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}, {"effort": "max"}]},
            {"slug": "hidden", "visibility": "hide", "supported_reasoning_levels": ["low"]},
        ]}))
        self.assertEqual(fleet.codex_models(str(self.home)),
                         [{"model": "model-x", "efforts": ["low", "high", "max"], "default_effort": "medium"}])
        self.assertEqual(fleet.codex_models(str(self.home / "missing")), [])

    def test_parse_integration_status(self):
        text = ("pi: not installed (/h/.pi/agent/extensions/herdr-agent-state.ts)\n"
                "claude: current (v9) (/h/.claude/hooks/herdr-agent-state.sh)\n"
                "codex: not installed (/h/v0/herdr-agent-state.sh)\n")
        self.assertEqual(fleet.parse_integration_status(text), {
            "claude": {"status": "current (v9)", "path": "/h/.claude/hooks/herdr-agent-state.sh"},
            "codex": {"status": "not installed", "path": "/h/v0/herdr-agent-state.sh"},
        })

    def test_hook_fix_command_and_env(self):
        self.assertEqual(fleet.hook_install_command("codex", "default"), "herdr integration install codex")
        self.assertEqual(fleet.hook_install_command("codex", "/x y/v0"),
                         "CODEX_HOME='/x y/v0' herdr integration install codex")
        env = {"CODEX_HOME": "/old", "PATH": "/bin"}
        self.assertEqual(fleet.config_env("codex", "default", env), {"PATH": "/bin"})
        self.assertEqual(fleet.config_env("codex", "/new", env)["CODEX_HOME"], "/new")
        self.assertEqual(fleet.config_env("claude", "/c", env)["CLAUDE_CONFIG_DIR"], "/c")

    def test_codex_hook_trusted(self):
        herdr_hook = {"hooks": [{"type": "command", "command": f"bash '{self.home}/herdr-agent-state.sh' session"}]}
        other_hook = {"hooks": [{"type": "command", "command": "other-tool session-start"}]}
        (self.home / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [other_hook, herdr_hook]}}))
        table = f'[hooks.state."{self.home / "hooks.json"}:session_start:1:0"]'
        config = self.home / "config.toml"
        config.write_text('model = "model-x"\n')
        self.assertIs(fleet.codex_hook_trusted(str(self.home)), False)
        config.write_text(f'[hooks.state]\n\n{table}\ntrusted_hash = "sha256:abc"\n\n[other]\nx = 1\n')
        self.assertIs(fleet.codex_hook_trusted(str(self.home)), True)
        config.write_text(f'{table}\nenabled = false\ntrusted_hash = "sha256:abc"\n')
        self.assertIs(fleet.codex_hook_trusted(str(self.home)), False)
        (self.home / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [other_hook]}}))
        self.assertIsNone(fleet.codex_hook_trusted(str(self.home)))


class SweepTests(FleetTestCase):
    def lane(self, **kw):
        f = {"exists": True, "git": True, "linked": True, "self": False, "locked": False, "branch": "fix/x",
             "default_branch": False, "head": "abc", "dirty": 0, "panes": [], "pr": None, "pr_error": None,
             "merged_into": None, "ahead": 0, "ahead_of": "remotes", "workspace": "w1", "path": "/w/x"}
        f.update(kw)
        return f

    def test_pick_pr(self):
        prs = [{"number": 1, "state": "CLOSED"},
               {"number": 2, "state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"},
               {"number": 3, "state": "MERGED", "mergedAt": "2026-02-01T00:00:00Z"}]
        self.assertEqual(fleet.pick_pr(prs)["number"], 3)
        self.assertEqual(fleet.pick_pr(prs + [{"number": 4, "state": "OPEN"}])["number"], 4)
        self.assertEqual(fleet.pick_pr(prs[:1])["number"], 1)
        self.assertIsNone(fleet.pick_pr([]))

    def test_merged_pull_request(self):
        merged = {"number": 7, "state": "MERGED", "headRefOid": "abc"}
        f = self.lane(pr=merged, ahead_of="PR #7", panes=[{"pane": "w1:p1", "agent": "claude", "status": "done"}])
        self.assertEqual(fleet.classify(f), ("ready", ["PR #7 merged"]))
        self.assertEqual(fleet.branch_plan(f), (True, "merged"))
        # The PR's head commit is not in this clone: the worktree goes, the branch stays.
        f = self.lane(pr=merged, ahead_of="remotes")
        self.assertEqual(fleet.classify(f)[0], "ready")
        self.assertFalse(fleet.branch_plan(f)[0])
        # Commits that the merge lacks keep the lane.
        self.assertEqual(fleet.classify(self.lane(pr=merged, ahead=2, ahead_of="PR #7")),
                         ("keep", ["PR #7 merged", "2 commits not in it"]))
        self.assertEqual(fleet.classify(self.lane(pr=merged, ahead=4, ahead_of="remotes")),
                         ("keep", ["PR #7 merged", "4 commits not pushed"]))
        self.assertEqual(fleet.classify(self.lane(pr=merged, ahead=None))[0], "keep")

    def test_pr_commit_oids(self):
        calls = []

        def fake_run(cmd, timeout=30, input_text=None, env=None, cwd=None):
            calls.append((cmd, cwd))
            if cmd[3] == "boom":
                raise fleet.FleetError("timed out")
            return subprocess.CompletedProcess(cmd, 1 if cmd[3] == "gone" else 0, stdout="aa\nbb\n", stderr="")

        pr = {"number": 5, "url": "https://example.com/o/r/pull/5"}
        with mock.patch.object(fleet, "run", fake_run), mock.patch.object(fleet.shutil, "which", return_value="gh"):
            self.assertEqual(fleet.pr_commit_oids("/w", pr), ["aa", "bb"])
            self.assertEqual(calls[0], (["gh", "pr", "view", pr["url"], "--json", "commits",
                                         "--jq", ".commits[].oid"], "/w"))
            self.assertEqual(fleet.pr_commit_oids("/w", {"number": 5})[0], "aa")
            self.assertEqual(calls[1][0][3], "5")
            self.assertIsNone(fleet.pr_commit_oids("/w", {"number": 6, "url": "gone"}))
            self.assertIsNone(fleet.pr_commit_oids("/w", {"number": 7, "url": "boom"}))
        with mock.patch.object(fleet.shutil, "which", return_value=None):
            self.assertIsNone(fleet.pr_commit_oids("/w", pr))

    def test_open_closed_missing_and_git_fallback(self):
        self.assertEqual(fleet.classify(self.lane(pr={"number": 8, "state": "OPEN"}, ahead=1)),
                         ("waiting", ["PR #8 open", "1 commit not pushed"]))
        self.assertEqual(fleet.classify(self.lane(pr={"number": 9, "state": "CLOSED"})),
                         ("keep", ["PR #9 closed without merging"]))
        self.assertEqual(fleet.classify(self.lane(pr_error="no git remote")),
                         ("keep", ["no pull request (gh: no git remote)"]))
        f = self.lane(merged_into="origin/main", ahead_of="origin/main")
        self.assertEqual(fleet.classify(f), ("ready", ["in origin/main"]))
        self.assertEqual(fleet.branch_plan(f), (True, "merged"))
        f = self.lane(merged_into="main", ahead_of="main", pr={"number": 9, "state": "CLOSED"})
        self.assertEqual(fleet.classify(f), ("ready", ["in main", "PR #9 closed"]))

    def test_blockers_keep_a_merged_lane(self):
        f = self.lane(dirty=3, locked=True, pr={"number": 7, "state": "MERGED", "headRefOid": "abc"},
                      ahead_of="PR #7", panes=[
                          {"pane": "w1:p1", "agent": "claude", "status": "working"},
                          {"pane": "w1:p2", "agent": "pi", "status": "idle"},
                          {"pane": "w1:p3", "agent": None, "status": "unknown", "busy": "node"},
                          {"pane": "w1:p4", "agent": None, "status": "unknown", "busy": None}])
        f["self"] = True
        self.assertEqual(fleet.classify(f), ("keep", [
            "this session runs here", "worktree is locked", "claude working in w1:p1", "pi in w1:p2: exit it yourself",
            "node running in w1:p3", "3 uncommitted changes", "PR #7 merged"]))
        self.assertFalse(fleet.branch_plan(f)[0])
        self.assertEqual(fleet.lane_blockers(self.lane(branch="main", default_branch=True)),
                         ["on the default branch main"])
        self.assertTrue(fleet.lane_blockers(self.lane(exists=False))[0].startswith("checkout missing"))
        self.assertIn("repository may be gone", fleet.lane_blockers(self.lane(git=False))[0])
        self.assertEqual(fleet.lane_blockers(self.lane(linked=False)), ["the repository's main checkout"])
        self.assertEqual(fleet.lane_blockers(self.lane(dirty=None)), ["git status failed"])

    def test_detached_review_checkout(self):
        f = self.lane(branch=None, unreachable=0)
        self.assertEqual(fleet.classify(f)[0], "ready")
        self.assertEqual(fleet.branch_plan(f), (False, "detached HEAD"))
        self.assertEqual(fleet.classify(self.lane(branch=None, unreachable=2)),
                         ("keep", ["detached HEAD with 2 commits on no branch"]))
        self.assertEqual(fleet.classify(self.lane(branch=None, unreachable=None))[0], "keep")

    def test_lane_why_names_each_agent_once(self):
        f = fleet.finish_lane(self.lane(pr={"number": 7, "state": "OPEN"}, panes=[
            {"pane": "w1:p1", "agent": "claude", "status": "working"},
            {"pane": "w1:p10", "agent": "codex", "status": "done"}]))
        self.assertEqual(fleet.lane_why(f), "claude working in w1:p1; PR #7 open; p10 codex done")
        self.assertEqual(f["target"], "w1")

    def test_parse_worktree_porcelain(self):
        text = ("worktree /r\nHEAD aaa\nbranch refs/heads/main\n\n"
                "worktree /w/a\nHEAD bbb\nbranch refs/heads/fix/a\nlocked\n\n"
                "worktree /w/b\nHEAD ccc\ndetached\nprunable gitdir file points to non-existent location\n")
        entries = fleet.parse_worktree_porcelain(text)
        self.assertEqual([e["path"] for e in entries], ["/r", "/w/a", "/w/b"])
        self.assertEqual((entries[1]["branch"], entries[1]["locked"]), ("fix/a", True))
        self.assertEqual((entries[2]["branch"], entries[2]["prunable"]), (None, True))
        self.assertEqual(fleet.other_kind("/h/.codex/worktrees/11ea/repo"), "Codex app")
        self.assertEqual(fleet.other_kind("/r/.claude/worktrees/agent-1"), "Claude Code agents")
        self.assertEqual(fleet.other_kind("/h/src/repo-feature"), "other")

    def test_config_status_and_process_parsing(self):
        config = '[ui]\ndirectory = "x"\n\n[worktrees]\ndirectory = "~/wt"  # checkouts\n'
        self.assertEqual(fleet.worktrees_dir_from_config(config), "~/wt")
        self.assertIsNone(fleet.worktrees_dir_from_config("[ui]\ntheme = 'dark'\n"))
        self.assertEqual(fleet.count_dirty([["a.rb"], [".fleet/"], ["new.txt"]]), 2)
        self.assertEqual(fleet.count_dirty([]), 0)
        self.assertIsNone(fleet.count_dirty(None))
        self.assertEqual(fleet.count_dirty([["app2.py", ".fleet/draft.py"]]), 1)  # a rename out of .fleet/
        self.assertEqual(fleet.count_dirty([[".fleet/b.md", ".fleet/a.md"]]), 0)  # a rename inside it
        self.assertEqual(fleet.count_dirty([["d/"]]), 1)  # one untracked directory
        self.assertEqual(fleet.count_dirty([["a -> b.py"]]), 1)  # -z never splits a name at an arrow
        self.assertEqual(fleet.count_dirty([[".fleet"]]), 1)  # a file or symlink named .fleet is no lane record
        self.assertEqual(fleet.count_dirty([[".fleet"], [".fleet/a.md"]]), 1)
        shell = {"foreground_process_group_id": 5, "shell_pid": 5, "foreground_processes": [{"name": "zsh"}]}
        self.assertIsNone(fleet.busy_from_process_info(shell))
        job = {"foreground_process_group_id": 9, "shell_pid": 5, "foreground_processes": [{"name": "node"}]}
        self.assertEqual(fleet.busy_from_process_info(job), "node")

    def test_archive_lane_files(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FLEET_HOME": tmp + "/home"}):
            lane = Path(tmp) / "lane"
            (lane / ".fleet" / "inbox").mkdir(parents=True)
            (lane / ".fleet" / "STATUS.md").write_text("log")
            (lane / ".fleet" / "inbox" / "1.md").write_text("task")
            (lane / ".fleet" / "panes.json").write_text("{}")
            dest = Path(fleet.archive_lane_files(str(lane), "repo", "fix/a b"))
            # Outside a repository there is no project: the archive goes under the home.
            self.assertEqual(dest.parent, Path(tmp) / "home" / "archive" / "repo")
            self.assertTrue(dest.name.startswith("fix-a-b-"))
            self.assertEqual(sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()),
                             ["STATUS.md", "inbox/1.md"])
            self.assertIsNone(fleet.archive_lane_files(str(Path(tmp) / "none"), "repo", "x"))


class LaneGitTests(FleetTestCase):
    """lane_facts and the verdict on real git worktrees. gh is replaced by a stub."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo, self.lane_path = root / "repo", root / "lane"
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}
        self.repo.mkdir()
        self.git(self.repo, "init", "-q", "-b", "main")
        self.git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")
        self.git(self.repo, "worktree", "add", "-q", "-b", "lane-a", str(self.lane_path))

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def facts(self, pr=None, caller_cwd=None, commits=None):
        stub = {"workspace": None, "label": None, "path": str(self.lane_path), "panes": []}
        stub.update(fleet.repo_paths(stub["path"]))
        return fleet.finish_lane(fleet.lane_facts(stub, None, caller_cwd or self.tmp.name,
                                                  lambda path, branch: (pr, None), commits or self.no_gh))

    def no_gh(self, path, pr):
        self.fail(f"unexpected lookup of the commits of PR #{pr['number']}")

    def test_lane_from_open_to_merged(self):
        f = self.facts()
        self.assertEqual((f["linked"], f["repo"], f["branch"]), (True, "repo", "lane-a"))
        # A lane without commits of its own is in main.
        self.assertEqual((f["verdict"], f["reasons"]), ("ready", ["in main"]))
        self.assertEqual(self.facts(caller_cwd=str(self.lane_path))["reasons"][0], "this session runs here")
        (self.lane_path / "a.txt").write_text("a")
        (self.lane_path / ".fleet").mkdir()
        (self.lane_path / ".fleet" / "STATUS.md").write_text("log")
        self.assertEqual(self.facts()["reasons"], ["1 uncommitted change"])
        self.git(self.lane_path, "add", "a.txt")
        self.git(self.lane_path, "commit", "-q", "-m", "a")
        self.assertEqual(self.facts()["reasons"], ["no pull request"])
        head = self.git(self.lane_path, "rev-parse", "HEAD")
        merged = {"number": 5, "state": "MERGED", "headRefOid": head}
        f = self.facts(pr=merged)
        self.assertEqual((f["verdict"], f["delete_branch"]), ("ready", True))
        self.git(self.lane_path, "commit", "-q", "--allow-empty", "-m", "b")
        self.assertEqual(self.facts(pr=merged)["reasons"], ["PR #5 merged", "1 commit not in it"])
        self.git(self.repo, "merge", "-q", "--no-ff", "-m", "merge lane-a", "lane-a")
        f = self.facts()
        self.assertEqual((f["verdict"], f["reasons"], f["delete_branch"]), ("ready", ["in main"], True))
        self.assertEqual(f["dirty"], 0)  # only .fleet/ files are left
        self.assertFalse(fleet.repo_paths(str(self.repo))["linked"])

    def test_git_settings_cannot_hide_untracked_files_from_sweep(self):
        # The lane is merged (no commit of its own); its only change is one untracked file.
        self.git(self.lane_path, "config", "status.showUntrackedFiles", "no")
        (self.lane_path / "new.txt").write_text("work")
        self.assertEqual(self.git(self.lane_path, "status", "--porcelain"), "")  # the setting took
        f = self.facts()
        self.assertEqual((f["verdict"], f["reasons"], f["dirty"]), ("keep", ["1 uncommitted change"], 1))
        # The same setting in the global config.
        self.git(self.lane_path, "config", "--unset", "status.showUntrackedFiles")
        global_config = Path(self.tmp.name) / "gitconfig"
        global_config.write_text("[status]\n\tshowUntrackedFiles = no\n")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(global_config)}):
            plain = subprocess.run(["git", "status", "--porcelain"], cwd=self.lane_path, capture_output=True,
                                   text=True)
            self.assertEqual(plain.stdout, "")
            f = self.facts()
        self.assertEqual((f["verdict"], f["reasons"], f["dirty"]), ("keep", ["1 uncommitted change"], 1))

    def test_an_untracked_directory_is_one_change(self):
        (self.lane_path / "d").mkdir()
        (self.lane_path / "d" / "a.txt").write_text("a")
        (self.lane_path / "d" / "b.txt").write_text("b")
        self.assertEqual(self.facts()["dirty"], 1)
        self.git(self.lane_path, "add", "d")
        f = self.facts()
        self.assertEqual((f["dirty"], f["reasons"]), (2, ["2 uncommitted changes"]))

    def test_a_rename_out_of_fleet_counts_for_sweep(self):
        # A base that tracks a file in .fleet/; a new lane moves it to a code path, staged, without a commit.
        (self.repo / ".fleet").mkdir()
        (self.repo / ".fleet" / "draft.py").write_text("print('x')\n")
        self.git(self.repo, "add", "-f", ".fleet/draft.py")
        self.git(self.repo, "commit", "-q", "-m", "draft")
        self.lane_path = Path(self.tmp.name) / "lane-r"
        self.git(self.repo, "worktree", "add", "-q", "-b", "lane-r", str(self.lane_path))
        self.git(self.lane_path, "mv", ".fleet/draft.py", "app2.py")
        f = self.facts()
        self.assertEqual((f["verdict"], f["reasons"], f["dirty"]), ("keep", ["1 uncommitted change"], 1))
        # A move inside .fleet/ is no change.
        self.git(self.lane_path, "mv", "app2.py", ".fleet/draft.py")
        self.git(self.lane_path, "mv", ".fleet/draft.py", ".fleet/kept.py")
        self.assertIn("R  .fleet/draft.py -> .fleet/kept.py", self.git(self.lane_path, "status", "--porcelain"))
        f = self.facts()
        self.assertEqual((f["verdict"], f["dirty"]), ("ready", 0))

    def new_lane(self, name, files):
        """A merged lane made after main commits `files` (name -> contents)."""
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text)
            self.git(self.repo, "add", rel)
        self.git(self.repo, "commit", "-q", "-m", "files")
        self.lane_path = Path(self.tmp.name) / name
        self.git(self.repo, "worktree", "add", "-q", "-b", name, str(self.lane_path))

    def test_a_file_named_fleet_is_a_change(self):
        # Only the .fleet/ directory holds lane records; a file or symlink of that name is work like any other.
        self.git(self.lane_path, "config", "status.showUntrackedFiles", "no")
        (self.lane_path / ".fleet").write_text("not a lane record")
        self.assertEqual(self.git(self.lane_path, "status", "--porcelain"), "")  # the setting took
        f = self.facts()
        self.assertEqual((f["verdict"], f["reasons"], f["dirty"]), ("keep", ["1 uncommitted change"], 1))
        (self.lane_path / "target.txt").write_text("x")
        (self.lane_path / ".fleet").unlink()
        (self.lane_path / ".fleet").symlink_to("target.txt")
        self.assertEqual(self.facts()["reasons"], ["2 uncommitted changes"])

    def test_a_stale_file_system_monitor_cannot_hide_work(self):
        for name, change in (("lane-t", lambda lane: (lane / "tracked.txt").write_text("changed")),
                             ("lane-u", lambda lane: (lane / "new.txt").write_text("new"))):
            with self.subTest(name):
                if name == "lane-t":
                    self.new_lane(name, {"tracked.txt": "a"})
                else:
                    self.lane_path = Path(self.tmp.name) / name
                    self.git(self.repo, "worktree", "add", "-q", "-b", name, str(self.lane_path))
                install_stale_monitor(self.tmp.name, self.repo, self.lane_path, self.git)
                change(self.lane_path)
                self.assertEqual(self.git(self.lane_path, "status", "--porcelain"), "")  # the monitor missed it
                f = self.facts()
                self.assertEqual((f["verdict"], f["reasons"], f["dirty"]), ("keep", ["1 uncommitted change"], 1))

    def test_assume_unchanged_and_skip_worktree_keep_the_lane(self):
        self.new_lane("lane-f", {"tracked.txt": "a", ".fleet/draft.md": "d"})
        self.assertEqual((self.facts()["verdict"], self.facts()["flagged"]), ("ready", 0))
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag):
                self.git(self.lane_path, "update-index", flag, "tracked.txt", ".fleet/draft.md")
                (self.lane_path / "tracked.txt").write_text("edited")
                self.assertEqual(self.git(self.lane_path, "status", "--porcelain"), "")  # git no longer looks
                f = self.facts()
                # The flagged path counts once its flag hides it; the one in .fleet/ does not.
                self.assertEqual((f["verdict"], f["reasons"], f["dirty"], f["flagged"]),
                                 ("keep", ["1 path marked assume-unchanged or skip-worktree"], 0, 1))
                self.git(self.lane_path, "update-index", flag.replace("--", "--no-", 1), "tracked.txt",
                         ".fleet/draft.md")
                self.git(self.lane_path, "checkout", "--", "tracked.txt")

    def test_a_sparse_checkout_stays_ready(self):
        self.new_lane("lane-s", {"keep/a.txt": "a", "other/b.txt": "b"})
        self.git(self.lane_path, "sparse-checkout", "set", "keep")
        self.assertFalse((self.lane_path / "other").exists())
        self.assertIn("S other/b.txt", self.git(self.lane_path, "ls-files", "-v"))
        f = self.facts()
        self.assertEqual((f["verdict"], f["reasons"], f["dirty"], f["flagged"]), ("ready", ["in main"], 0, 0))
        # An excluded path that comes back in the worktree keeps the lane: git clears its skip-worktree bit when it
        # reads the index, and status then counts it as a change.
        (self.lane_path / "other").mkdir()
        (self.lane_path / "other" / "b.txt").write_text("edited")
        f = self.facts()
        self.assertEqual((f["verdict"], f["reasons"]), ("keep", ["1 uncommitted change"]))

    def test_a_failing_ls_files_keeps_the_lane(self):
        with mock.patch.object(fleet, "flagged_count", return_value=None):
            f = self.facts()
        self.assertIsNone(f["flagged"])
        self.assertEqual((f["verdict"], f["reasons"]), ("keep", ["git ls-files failed"]))

    def test_a_failing_status_keeps_the_lane(self):
        with mock.patch.object(fleet, "status_entries", return_value=None):
            f = self.facts()
        self.assertIsNone(f["dirty"])
        self.assertEqual((f["verdict"], f["reasons"]), ("keep", ["git status failed"]))

    def test_merged_pull_request_whose_head_is_not_in_the_clone(self):
        origin = Path(self.tmp.name) / "origin.git"
        self.git(self.tmp.name, "init", "-q", "--bare", str(origin))
        self.git(self.repo, "remote", "add", "origin", str(origin))
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.lane_path, "commit", "-q", "--allow-empty", "-m", "a")
        first = self.git(self.lane_path, "rev-parse", "HEAD")
        self.git(self.lane_path, "commit", "-q", "--allow-empty", "-m", "b")
        lane = set(self.git(self.lane_path, "rev-list", "HEAD", "--not", "--remotes").split())
        # The head on GitHub is a merge commit that this clone never fetched, and the remote deleted the branch.
        merged = {"number": 5, "state": "MERGED", "headRefOid": "f" * 40, "url": "https://example.com/pull/5"}
        listed = lane | {"e" * 40}
        f = self.facts(pr=merged, commits=lambda path, pr: listed)
        self.assertEqual((f["verdict"], f["reasons"], f["delete_branch"]), ("ready", ["PR #5 merged"], True))
        # A short list without HEAD: HEAD is not in the pull request, and only the commits not in it count.
        f = self.facts(pr=merged, commits=lambda path, pr: [first])
        self.assertEqual((f["verdict"], f["reasons"]), ("keep", ["PR #5 merged", "1 commit not pushed"]))
        # A full page without HEAD can be cut short: nobody can tell.
        page = [first] + [f"{i:040x}" for i in range(fleet.GH_COMMITS_PAGE - 1)]
        f = self.facts(pr=merged, commits=lambda path, pr: page)
        self.assertEqual((f["verdict"], f["reasons"]),
                         ("keep", ["PR #5 merged", "cannot tell whether every commit is pushed"]))
        # HEAD in a full page is still proof.
        f = self.facts(pr=merged, commits=lambda path, pr: page + sorted(lane))
        self.assertEqual((f["verdict"], f["delete_branch"]), ("ready", True))
        # A commit after the merge keeps the lane.
        self.git(self.lane_path, "commit", "-q", "--allow-empty", "-m", "c")
        f = self.facts(pr=merged, commits=lambda path, pr: listed)
        self.assertEqual((f["verdict"], f["reasons"]), ("keep", ["PR #5 merged", "1 commit not pushed"]))
        # When gh cannot list the commits, every commit on no remote counts.
        f = self.facts(pr=merged, commits=lambda path, pr: None)
        self.assertEqual((f["verdict"], f["reasons"]), ("keep", ["PR #5 merged", "3 commits not pushed"]))
        # A pushed lane with an open pull request needs no list.
        self.git(self.lane_path, "push", "-q", "origin", "lane-a")
        f = self.facts(pr={"number": 6, "state": "OPEN", "headRefOid": "d" * 40})
        self.assertEqual((f["verdict"], f["reasons"]), ("waiting", ["PR #6 open"]))

    def test_lane_archive_goes_to_its_project(self):
        (self.lane_path / ".fleet").mkdir()
        (self.lane_path / ".fleet" / "BRIEF.md").write_text("brief")
        home = Path(self.tmp.name) / "home"
        with mock.patch.dict(os.environ, {"FLEET_HOME": str(home)}):
            dest = Path(fleet.archive_lane_files(str(self.lane_path), "repo", "lane-a"))
        self.assertEqual(dest.parent, home / "projects" / "repo" / "lanes")
        self.assertTrue((dest / "BRIEF.md").is_file())

    def test_detached_checkout(self):
        self.git(self.lane_path, "checkout", "-q", "--detach")
        f = self.facts()
        self.assertEqual((f["verdict"], f["delete_branch"]), ("ready", False))
        self.git(self.lane_path, "commit", "-q", "--allow-empty", "-m", "only here")
        self.assertEqual(self.facts()["reasons"], ["detached HEAD with 1 commit on no branch"])



class CleanupTests(FleetTestCase):
    """clean_lane and cmd_cleanup on real git worktrees: a lane in a temporary Herdr worktrees directory. Herdr, gh
    and the agents' exit are stubs."""

    PANE = {"pane_id": "w1:p1", "agent": "claude", "agent_status": "idle"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.repo, self.wt, self.home = self.root / "repo", self.root / "wt", self.root / "home"
        self.lane_path = self.wt / "repo" / "lane-a"
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}
        for patch in (mock.patch.dict(os.environ, {"FLEET_HOME": str(self.home)}),
                      mock.patch.object(fleet, "herdr_worktrees_dir", return_value=self.wt),
                      mock.patch.object(fleet, "herdr", side_effect=self.fake_herdr),
                      mock.patch.object(fleet, "require_herdr"),
                      mock.patch.object(fleet, "pr_for_branch", return_value=(None, None))):
            patch.start()
            self.addCleanup(patch.stop)
        self.repo.mkdir()
        self.git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "tracked.txt").write_text("a")
        self.git(self.repo, "add", "tracked.txt")
        self.git(self.repo, "commit", "-q", "-m", "init")
        # The lane is merged: it has no commit of its own. Its .fleet/ is excluded, as `fleet worktree` leaves it.
        self.git(self.repo, "worktree", "add", "-q", "-b", "lane-a", str(self.lane_path))
        (Path(fleet.prep_dir(str(self.lane_path))["fleet_dir"]) / "STATUS.md").write_text("log")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def fake_herdr(*args, **kwargs):
        return {"workspaces": []} if args[:2] == ("workspace", "list") else {"panes": []}

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def facts(self, panes=()):
        stub = {"workspace": None, "label": None, "path": str(self.lane_path), "panes": list(panes)}
        stub.update(fleet.repo_paths(stub["path"]))
        return fleet.finish_lane(fleet.lane_facts(stub, None, str(self.root)))

    def cleanup(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = fleet.main(["cleanup", str(self.lane_path), *args])
        return code, json.loads(buf.getvalue())

    def clean(self, write=None):
        """clean_lane on the lane with one idle agent, whose exit writes the file `write` into the lane."""
        f = self.facts([self.PANE])
        self.assertEqual(f["verdict"], "ready")

        def exit_agent(pane, agent, timeout):
            if write:
                (self.lane_path / write).parent.mkdir(parents=True, exist_ok=True)
                (self.lane_path / write).write_text("written while the agent exits")
            return True
        info = {"agent": "claude", "agent_status": "idle", "agent_session": {"value": "s-1"}}
        with mock.patch.object(fleet, "pane_info", return_value=info), \
                mock.patch.object(fleet, "exit_agent", side_effect=exit_agent):
            return fleet.clean_lane(f, 5)

    def cleanup_events(self):
        return [e for e in fleet.read_events([fleet.project_for(str(self.repo))]) if e["event"] == "cleanup"]

    def assert_lane_stays(self):
        self.assertTrue(self.lane_path.is_dir())
        self.assertIn(str(self.lane_path), self.git(self.repo, "worktree", "list"))
        self.assertEqual(self.git(self.repo, "rev-parse", "--verify", "--quiet", "--symbolic-full-name", "lane-a"),
                         "refs/heads/lane-a")

    def test_cleanup_refuses_a_lane_with_a_hidden_untracked_file(self):
        self.git(self.lane_path, "config", "status.showUntrackedFiles", "no")
        (self.lane_path / "new.txt").write_text("work")
        self.assertEqual(self.git(self.lane_path, "status", "--porcelain"), "")  # the setting took
        global_config = self.root / "gitconfig"
        global_config.write_text("[status]\n\tshowUntrackedFiles = no\n")
        for where in ("repository", "global"):
            if where == "global":
                self.git(self.lane_path, "config", "--unset", "status.showUntrackedFiles")
            with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(global_config)} if where == "global" else {}):
                for extra in ([], ["--dry-run"]):
                    code, out = self.cleanup(*extra)
                    self.assertEqual(code, 2, (where, extra))
                    self.assertEqual((out["ok"], out["changed"]), (False, "nothing"))
                    [refused] = out["refused"]
                    self.assertEqual((refused["verdict"], refused["reasons"]), ("keep", ["1 uncommitted change"]))
                    self.assertTrue((self.lane_path / "new.txt").is_file())
                    self.assert_lane_stays()
        self.assertEqual(self.cleanup_events(), [])
        self.assertEqual(list(self.home.rglob("lanes")), [])  # no archive either

    def assert_refused(self, reasons):
        code, out = self.cleanup()
        self.assertEqual((code, out["ok"], out["changed"]), (2, False, "nothing"), out)
        [refused] = out["refused"]
        self.assertEqual((refused["verdict"], refused["reasons"]), ("keep", reasons))
        self.assert_lane_stays()
        self.assertEqual(self.cleanup_events(), [])

    def test_cleanup_refuses_a_file_named_fleet(self):
        self.git(self.lane_path, "config", "status.showUntrackedFiles", "no")
        shutil.rmtree(self.lane_path / ".fleet")
        (self.lane_path / ".fleet").write_text("not a lane record")
        self.assert_refused(["1 uncommitted change"])
        self.assertEqual((self.lane_path / ".fleet").read_text(), "not a lane record")

    def test_cleanup_refuses_work_that_a_stale_monitor_hides(self):
        install_stale_monitor(self.root, self.repo, self.lane_path, self.git)
        for name in ("tracked.txt", "new.txt"):
            with self.subTest(name):
                (self.lane_path / name).write_text("work")
                self.assertEqual(self.git(self.lane_path, "status", "--porcelain"), "")  # the monitor missed it
                self.assert_refused(["1 uncommitted change"])
                self.assertEqual((self.lane_path / name).read_text(), "work")
                (self.lane_path / "tracked.txt").write_text("a")  # git checkout would trust the monitor too
                (self.lane_path / "new.txt").unlink(missing_ok=True)

    def test_cleanup_refuses_flagged_paths(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag):
                self.git(self.lane_path, "update-index", flag, "tracked.txt")
                (self.lane_path / "tracked.txt").write_text("edited")
                self.assert_refused(["1 path marked assume-unchanged or skip-worktree"])
                self.assertEqual((self.lane_path / "tracked.txt").read_text(), "edited")
                self.git(self.lane_path, "update-index", flag.replace("--", "--no-", 1), "tracked.txt")
                self.git(self.lane_path, "checkout", "--", "tracked.txt")

    def test_cleanup_recount_reads_the_flags(self):
        # The agent marks a file assume-unchanged and edits it while it exits.
        f = self.facts([self.PANE])
        self.assertEqual(f["verdict"], "ready")

        def exit_agent(pane, agent, timeout):
            self.git(self.lane_path, "update-index", "--assume-unchanged", "tracked.txt")
            (self.lane_path / "tracked.txt").write_text("edited while the agent exits")
            return True
        info = {"agent": "claude", "agent_status": "idle", "agent_session": {"value": "s-1"}}
        with mock.patch.object(fleet, "pane_info", return_value=info), \
                mock.patch.object(fleet, "exit_agent", side_effect=exit_agent):
            out = fleet.clean_lane(f, 5)
        self.assertEqual((out["ok"], out.get("removed")), (False, None))
        self.assertEqual(out["error"], "1 path marked assume-unchanged or skip-worktree; the worktree stays")
        self.assertEqual((self.lane_path / "tracked.txt").read_text(), "edited while the agent exits")
        self.assert_lane_stays()
        self.git(self.lane_path, "update-index", "--no-assume-unchanged", "tracked.txt")
        self.git(self.lane_path, "checkout", "--", "tracked.txt")
        f = self.facts([self.PANE])
        self.assertEqual(f["verdict"], "ready")
        with mock.patch.object(fleet, "pane_info", return_value=info), \
                mock.patch.object(fleet, "exit_agent", return_value=True), \
                mock.patch.object(fleet, "flagged_count", return_value=None):
            out = fleet.clean_lane(f, 5)
        self.assertEqual(out["error"], "git ls-files failed; the worktree stays")
        self.assert_lane_stays()

    def test_cleanup_keeps_a_lane_that_changes_while_its_agent_exits(self):
        # Once hidden by the git settings: then neither removal command would refuse, and only the count again
        # stands between the file and its deletion. Then once in plain sight.
        for name, hide in (("hidden.txt", True), ("late.txt", False)):
            if hide:
                self.git(self.lane_path, "config", "status.showUntrackedFiles", "no")
            else:
                self.git(self.lane_path, "config", "--unset", "status.showUntrackedFiles")
            out = self.clean(write=name)
            self.assertEqual((out["ok"], out.get("removed"), out["exited"]), (False, None, ["w1:p1"]))
            self.assertEqual(out["error"], "1 uncommitted change appeared after the check; the worktree stays")
            self.assertTrue((self.lane_path / name).is_file())
            self.assert_lane_stays()
            event = self.cleanup_events()[-1]
            self.assertEqual((event["removed"], event["ok"], event["error"]), (False, False, out["error"]))
            self.assertEqual(event["archive"], out["archive"])  # the archive comes first, and the event names it
            (self.lane_path / name).unlink()
        # A file that appears in .fleet/ is no change: the removal goes ahead.
        out = self.clean(write=".fleet/notes.md")
        self.assertEqual((out["ok"], out["removed"]), (True, True), out)
        self.assertFalse(self.lane_path.exists())

    def test_cleanup_removes_a_clean_merged_lane(self):
        out = self.clean()
        self.assertEqual((out["ok"], out["removed"], out["branch"], out["exited"]),
                         (True, True, "deleted lane-a", ["w1:p1"]), out)
        self.assertNotIn("error", out)
        self.assertFalse(self.lane_path.exists())
        self.assertFalse((self.wt / "repo").exists())  # Herdr's repository folder held only this lane
        self.assertEqual((Path(out["archive"]) / "STATUS.md").read_text(), "log")
        self.assertEqual(self.git(self.repo, "branch", "--list", "--format=%(refname)", "lane-a"), "")
        event = self.cleanup_events()[-1]
        self.assertEqual((event["removed"], event["ok"], event["branch_result"]), (True, True, "deleted lane-a"))

    def test_cleanup_end_to_end_removes_a_clean_merged_lane(self):
        code, out = self.cleanup()
        self.assertEqual(code, 0, out)
        [lane] = out["lanes"]
        self.assertEqual((lane["ok"], lane["removed"], lane["branch"]), (True, True, "deleted lane-a"))
        self.assertFalse(self.lane_path.exists())

    def test_cleanup_with_a_failing_status_keeps_the_worktree(self):
        f = self.facts([self.PANE])
        info = {"agent": "claude", "agent_status": "idle", "agent_session": {"value": "s-1"}}
        with mock.patch.object(fleet, "pane_info", return_value=info), \
                mock.patch.object(fleet, "exit_agent", return_value=True), \
                mock.patch.object(fleet, "status_entries", return_value=None):
            out = fleet.clean_lane(f, 5)
        self.assertEqual((out["ok"], out.get("removed")), (False, None))
        self.assertEqual(out["error"], "git status failed; the worktree stays")
        self.assert_lane_stays()

class ProjectLogTests(FleetTestCase):
    """Project homes, the event log, the session hooks and `fleet log`, in a temporary FLEET_HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.patch = mock.patch.dict(os.environ, {"FLEET_HOME": str(self.home)})
        self.patch.start()
        self.repo = self.repo_with_remote("app", "git@github.com:Acme/App.git")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=self.repo)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def repo_with_remote(self, name, url):
        path = self.root / name
        path.mkdir()
        git("init", "-q", "-b", "main", cwd=path)
        if url:
            git("remote", "add", "origin", url, cwd=path)
        return path

    def last_event(self, path):
        return json.loads(Path(path).read_text().splitlines()[-1])

    def log(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = fleet.main(["log", *argv])
        return code, buf.getvalue()

    def test_normalize_remote(self):
        same = ["git@github.com:Acme/App.git", "https://github.com/acme/app", "https://github.com/acme/app.git/",
                "ssh://git@github.com:22/acme/app.git", "https://user:secret@github.com/acme/app.git",
                "git@github.com:/acme/app.git", "https://user:p@ss@github.com/acme/app.git",
                "https://github.com/acme/app.git?token=abc#top"]
        self.assertEqual({fleet.normalize_remote(u) for u in same}, {"github.com/acme/app"})
        self.assertEqual(fleet.normalize_remote("git@git.example.com:group/sub/app.git"), "git.example.com/group/sub/app")
        for local in ("/srv/git/app.git", "file:///srv/git/app.git", "", "C:/repos/app"):
            self.assertIsNone(fleet.normalize_remote(local), local)

    def test_one_project_per_repository(self):
        home = fleet.project_for(str(self.repo))
        self.assertEqual(home, self.home / "projects" / "app")
        self.assertEqual(fleet.read_json(home / "project.json")["keys"],
                         ["github.com/acme/app", f"local:{os.path.realpath(self.repo)}"])
        # Another clone (HTTPS remote) and a linked worktree share the project.
        clone = self.repo_with_remote("app-copy", "https://github.com/acme/app")
        self.assertEqual(fleet.project_for(str(clone)), home)
        git("worktree", "add", "-q", "-b", "lane", str(self.root / "lane"), cwd=self.repo)
        self.assertEqual(fleet.project_for(str(self.root / "lane")), home)
        # Another owner's repository with the same name gets owner-name.
        other = self.repo_with_remote("other", "git@github.com:beta/app.git")
        self.assertEqual(fleet.project_for(str(other)).name, "beta-app")
        # Without a remote, the main checkout is the key.
        self.assertEqual(fleet.project_for(str(self.repo_with_remote("solo", None))).name, "solo")
        self.assertIsNone(fleet.project_for(str(self.root)))
        self.assertIsNone(fleet.project_for(str(self.repo_with_remote("new", "git@example.com:x/new.git")), create=False))
        self.assertEqual([p.name for p in fleet.project_dirs()], ["app", "beta-app", "solo"])

    def test_remote_added_later_keeps_the_project(self):
        solo = self.repo_with_remote("solo", None)
        home = fleet.project_for(str(solo))
        git("remote", "add", "origin", "git@github.com:acme/solo.git", cwd=solo)
        self.assertEqual(fleet.project_for(str(solo)), home)
        self.assertIn("github.com/acme/solo", fleet.read_json(home / "project.json")["keys"])
        # A clone elsewhere now finds the project by its remote.
        self.assertEqual(fleet.project_for(str(self.repo_with_remote("solo-2", "https://github.com/acme/solo"))), home)
        # A project.json of 0.4.0 has "remotes" instead of "keys".
        fleet.write_json(home / "project.json", {"name": "solo", "remotes": ["github.com/acme/solo"]})
        self.assertEqual(fleet.find_project(["github.com/acme/solo"]), home)

    def test_record(self):
        env = {"HERDR_PANE_ID": "w1:p1", "CLAUDE_CODE_SESSION_ID": "orch-1", "CLAUDECODE": "1"}
        with mock.patch.dict(os.environ, env):
            path = fleet.record("spawn", str(self.repo), pane="w2:p1", role="impl", prompt="Read .fleet/BRIEF.md",
                                ok=True, model=None, exited=[])
        e = self.last_event(path)
        self.assertEqual(Path(path).parent, self.home / "projects" / "app" / "events")
        self.assertEqual((e["event"], e["project"], e["pane"], e["ok"]), ("spawn", "app", "w2:p1", True))
        self.assertEqual(e["by"], {"pane": "w1:p1", "session": "orch-1", "agent": "claude"})
        self.assertNotIn("model", e)
        self.assertNotIn("exited", e)
        # Outside a repository, the event goes to the home's own log.
        self.assertEqual(Path(fleet.record("handoff", str(self.root), path="/h.md")).parent, self.home / "events")
        # Only the user can open the home; a home made with the default mode is made private.
        self.assertEqual(self.home.stat().st_mode & 0o777, 0o700)
        self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
        os.chmod(self.home, 0o755)
        fleet.record("send", str(self.repo), pane="w2:p1")
        self.assertEqual(self.home.stat().st_mode & 0o777, 0o700)
        # A broken home never fails the command.
        with mock.patch.dict(os.environ, {"FLEET_HOME": path}):
            self.assertIsNone(fleet.record("send", str(self.repo), pane="w2:p1"))

    def test_session_hook_records_fleet_sessions_only(self):
        data = {"session_id": "s-1", "transcript_path": "/t/s-1.jsonl", "cwd": str(self.repo), "source": "startup"}
        env = {"HERDR_PANE_ID": "w2:p1", "HERDR_WORKSPACE_ID": "w2", "CLAUDE_CONFIG_DIR": "/c"}
        self.assertIsNone(fleet.hook_session_event("session_start", data, env))  # no .fleet/
        (self.repo / ".fleet").mkdir()
        fleet.save_panes(self.repo / ".fleet", {"w2:p1": {"role": "impl"}})
        self.assertIsNone(fleet.hook_session_event("session_start", data, {}))  # not in Herdr
        e = self.last_event(fleet.hook_session_event("session_start", data, env))
        self.assertEqual((e["event"], e["pane"], e["role"], e["session"], e["transcript"], e["source"], e["branch"]),
                         ("session_start", "w2:p1", "impl", "s-1", "/t/s-1.jsonl", "startup", "main"))
        self.assertNotIn("by", e)
        # The hook command: SessionEnd prints nothing; SessionStart after startup prints no context.
        for event, payload in (("session-end", {"session_id": "s-1", "reason": "prompt_input_exit"}),
                               ("session-start", {"session_id": "s-2", "source": "startup"})):
            cp = subprocess.run([*HELPER, "hook", event], cwd=self.repo, capture_output=True,
                                text=True, timeout=CHILD_LIMIT, input=json.dumps({**payload, "cwd": str(self.repo)}),
                                env=helper_env({**os.environ, **env}))
            self.assertEqual((cp.returncode, cp.stdout), (0, ""))
        events = fleet.read_events([self.home / "projects" / "app"])
        self.assertEqual([(e["event"], e["session"]) for e in events[-2:]],
                         [("session_end", "s-1"), ("session_start", "s-2")])
        self.assertEqual(events[-2]["reason"], "prompt_input_exit")
        # Fields of the wrong type are not recorded.
        bad = {"session_id": {"a": 1}, "source": ["x"], "cwd": str(self.repo), "transcript_path": 7, "model": {"id": "m"}}
        e = self.last_event(fleet.hook_session_event("session_start", bad, env))
        self.assertEqual((e.get("session"), e.get("source"), e.get("transcript"), e["model"]), (None, None, None, "m"))
        # A failing record does not cost the re-brief.
        (self.repo / ".fleet" / "HANDOFF.md").write_text("handoff")
        buf = io.StringIO()
        payload = io.StringIO(json.dumps({"source": "compact", "cwd": str(self.repo)}))
        with mock.patch.object(fleet, "hook_session_event", side_effect=RuntimeError), \
                mock.patch.dict(os.environ, env), mock.patch.object(sys, "stdin", payload), redirect_stdout(buf):
            self.assertEqual(fleet.main(["hook", "session-start"]), 0)
        self.assertIn("HANDOFF.md", json.loads(buf.getvalue())["hookSpecificOutput"]["additionalContext"])

    def test_hooks_skip_sessions_outside_herdr(self):
        hooks = json.loads((SCRIPT.parent.parent / "hooks" / "hooks.json").read_text())["hooks"]
        entries = [(event, h.get("matcher"), c["command"]) for event, hs in hooks.items() for h in hs for c in h["hooks"]]
        self.assertEqual({(e, m) for e, m, _ in entries},
                         {("SessionStart", "clear|compact"), ("SessionStart", "startup|resume"), ("SessionEnd", None)})
        (self.repo / ".fleet").mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        env["CLAUDE_PLUGIN_ROOT"] = str(SCRIPT.parent.parent)
        for event, matcher, command in entries:
            if matcher == "clear|compact":
                continue  # the re-brief also runs outside Herdr
            self.assertTrue(command.startswith('[ -n "$HERDR_PANE_ID" ] || exit 0;'), command)
            cp = subprocess.run(["sh", "-c", command], cwd=self.repo, capture_output=True, text=True,
                                timeout=CHILD_LIMIT,
                                input=json.dumps({"session_id": "s-1", "source": "startup", "cwd": str(self.repo)}),
                                env=env)
            self.assertEqual((cp.returncode, cp.stdout, cp.stderr), (0, "", ""))
        self.assertFalse((self.home / "projects").exists())

    def test_commands_record_their_lane(self):
        lane = str(self.root / "lane")
        git("worktree", "add", "-q", "-b", "feat/x", lane, cwd=self.repo)
        (Path(lane) / ".fleet").mkdir()
        info = {"pane_id": "w2:p1", "agent": "claude", "agent_status": "idle", "cwd": lane, "workspace_id": "w2",
                "agent_session": {"value": "s-1"}}
        target = {"pane_id": "w2:p1", "workspace_id": "w2", "cwd": lane, "placement": "split"}
        quiet = redirect_stdout(io.StringIO())
        err = mock.patch.object(sys, "stderr", io.StringIO())
        real_run = fleet.run

        def run(cmd, *args, **kwargs):  # git runs for real; herdr does not run
            return subprocess.CompletedProcess(cmd, 0, "", "") if cmd[0] == "herdr" else real_run(cmd, *args, **kwargs)
        with mock.patch.dict(os.environ, {"FLEET_NO_REMUDA": "1", "HERDR_PANE_ID": "w1:p1"}), \
                mock.patch.object(fleet, "require_herdr"), mock.patch.object(fleet, "pane_info", return_value=info), \
                mock.patch.object(fleet, "create_pane", return_value=target), mock.patch.object(fleet, "run", run), \
                mock.patch.object(fleet, "wait_for_start", side_effect=fleet.FleetError("pane w2:p1 closed")), \
                mock.patch.object(fleet, "wait_not_working", return_value=True), \
                mock.patch.object(fleet, "submit_prompt", return_value={"ok": True, "status": "working"}), \
                mock.patch.object(fleet, "exit_agent", return_value=True), \
                mock.patch.object(fleet, "herdr", side_effect=fleet.FleetError("no such pane")), quiet, err:
            # The launch is recorded although the wait fails.
            self.assertEqual(fleet.main(["spawn", "--agent", "claude", "--name", "impl-x", "--role", "impl",
                                         "--prompt", "Read .fleet/BRIEF.md and execute it."]), 1)
            self.assertEqual(fleet.main(["send", "w2:p1", "--text", "Fix the test."]), 0)
            # The agent exited, but the pane did not close.
            self.assertEqual(fleet.main(["retire", "w2:p1", "--close"]), 1)
        events = fleet.read_events([fleet.project_for(lane)])
        self.assertEqual([(e["event"], e.get("ok")) for e in events],
                         [("spawn", None), ("spawn_result", False), ("send", True), ("retire", True)])
        for e in events:
            self.assertEqual((e["branch"], e["workspace"], e["cwd"], e["by"]["pane"]), ("feat/x", "w2", lane, "w1:p1"))
        self.assertEqual(events[0]["prompt"], "Read .fleet/BRIEF.md and execute it.")
        self.assertEqual((events[1]["reason"], events[3]["closed"], events[3]["error"]),
                         ("pane w2:p1 closed", False, "no such pane"))
        for lane_name in ("feat/x", "w2", "impl-x", lane):
            self.assertEqual(len(json.loads(self.log("app", "--json", "--lane", lane_name)[1])), 4, lane_name)
        text = self.log("app")[1]
        self.assertIn("did not start  FAILED: pane w2:p1 closed", text)
        self.assertIn("could not close the pane: no such pane", text)
        self.assertEqual([(r["session"], r["ended"]) for r in json.loads(self.log("app", "--sessions", "--json")[1])],
                         [("s-1", "retired")])

    def test_session_rows(self):
        def ev(kind, session, **fields):
            return {"ts": "2026-01-02T03:04:05+00:00", "event": kind, "session": session, **fields}
        rows = fleet.session_rows([
            ev("session_start", "s-1", source="startup"), ev("session_end", "s-1", reason="other"),
            ev("session_start", "s-1", source="resume"),
            ev("session_start", "s-2", source="startup"), ev("retire", "s-2", ok=False, reason="still running"),
            ev("session_start", "s-3", source="startup"), ev("retire", "s-3", ok=True),
        ])
        self.assertEqual([(r["session"], r["ended"]) for r in rows], [("s-1", None), ("s-2", None), ("s-3", "retired")])

    def test_log_skips_fields_of_the_wrong_type(self):
        d = fleet.project_for(str(self.repo))
        (d / "events").mkdir(exist_ok=True)
        bad = [{"ts": "2026-01-02T03:04:05+00:00", "event": "session_start", "session": {"a": 1}, "source": ["x"],
                "pane": "w2:p1"},
               {"ts": "2026-01-02T03:04:06+00:00", "event": "cleanup", "lane": 5, "exited": 7, "by": "w1:p1"},
               {"ts": "2026-01-02T03:04:07+00:00", "event": "retire", "session": 9, "agent": "claude",
                "exited": [{"pane": 1, "session": "s-1"}, "x"], "by": {"pane": ["w1:p1"]}, "ok": "yes"},
               {"ts": 5, "event": ["send"], "pane": {"p": 1}}]
        with open(d / "events" / "2026-01.jsonl", "a") as fh:
            fh.write("".join(json.dumps(e) + "\n" for e in bad))
        for argv in ([], ["--sessions"], ["--pane", "w2:p1"], ["--lane", "feat/x"], ["--sessions", "--pane", "w1:p1"]):
            code, out = self.log("app", "--limit", "0", *argv)
            self.assertEqual(code, 0, argv)
        events = json.loads(self.log("app", "--json", "--limit", "0")[1])
        self.assertEqual([sorted(e) for e in events],  # the event without a valid time sorts first
                         [[], ["event", "pane", "ts"], ["event", "ts"], ["agent", "by", "event", "exited", "ts"]])
        self.assertEqual((events[3]["by"], events[3]["exited"]), ({}, [{"session": "s-1"}]))

    def test_log(self):
        lane = str(self.root / "lane")
        git("worktree", "add", "-q", "-b", "feat/x", lane, cwd=self.repo)
        fleet.record("worktree", lane, lane=lane, branch="feat/x", base="origin/main", workspace="w2", pane="w2:p1")
        fleet.record("spawn", lane, pane="w2:p1", cwd=lane, branch="feat/x", agent="claude", model="opus",
                     role="impl", session="s-1", prompt="Read .fleet/BRIEF.md and execute it.", ok=True)
        fleet.record("send", lane, pane="w2:p1", cwd=lane, branch="feat/x", workspace="w2", text="Fix the test.",
                     ok=False, reason="busy")
        fleet.record("session_start", lane, by=False, pane="w2:p1", cwd=lane, branch="feat/x", workspace="w2",
                     agent="claude", role="impl", session="s-2", source="clear", transcript="/t/s-2.jsonl")
        fleet.record("reset", lane, pane="w2:p1", cwd=lane, branch="feat/x", workspace="w2", command="/clear",
                     session_before="s-1", session="s-2", ok=True)
        fleet.record("cleanup", str(self.repo), lane=lane, branch="feat/x", removed=True, branch_result="deleted feat/x",
                     exited=[{"pane": "w2:p1", "agent": "claude", "session": "s-2"}], ok=True)
        old = {"ts": "2026-01-02T03:04:05+00:00", "event": "send", "pane": "w9:p1", "text": "old"}
        with open(fleet.project_for(str(self.repo)) / "events" / "2026-01.jsonl", "a") as fh:
            fh.write(json.dumps(old) + "\nnot json\n")
        code, out = self.log("app", "--json", "--limit", "0")
        events = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual([e["event"] for e in events],
                         ["send", "worktree", "spawn", "send", "session_start", "reset", "cleanup"])
        self.assertEqual(len(json.loads(self.log("app", "--json", "--since", "1d")[1])), 6)
        self.assertEqual(len(json.loads(self.log("app", "--json", "--limit", "2")[1])), 2)
        self.assertEqual([e["event"] for e in json.loads(self.log("app", "--json", "--event", "spawn,reset")[1])],
                         ["spawn", "reset"])
        self.assertEqual(len(json.loads(self.log("app", "--json", "--lane", "feat/x")[1])), 6)
        self.assertEqual(len(json.loads(self.log("app", "--json", "--lane", lane)[1])), 6)
        self.assertEqual(len(json.loads(self.log("app", "--json", "--pane", "w9:p1")[1])), 1)
        text = self.log("app")[1]
        self.assertIn('impl claude/opus session s-1  "Read .fleet/BRIEF.md and execute it."', text)
        self.assertIn('"Fix the test."  FAILED: busy', text)
        self.assertIn("removed; branch: deleted feat/x", text)
        rows = json.loads(self.log("app", "--sessions", "--json")[1])
        self.assertEqual([(r["session"], r["ended"], r.get("transcript")) for r in rows],
                         [("s-1", "clear", None), ("s-2", "cleanup", "/t/s-2.jsonl")])
        self.assertIn("/t/s-2.jsonl", self.log("app", "--sessions")[1])
        # The project of the current directory is the default; elsewhere the command names the projects.
        cwd = os.getcwd()
        try:
            os.chdir(lane)
            self.assertEqual(len(json.loads(self.log("--json", "--limit", "0")[1])), 7)
            os.chdir(self.root)
            err = io.StringIO()
            with mock.patch.object(sys, "stderr", err):
                self.assertEqual(self.log()[0], 1)
            self.assertIn("Projects: app", err.getvalue())
        finally:
            os.chdir(cwd)
        self.assertEqual(self.log("--all", "--event", "worktree")[1].splitlines()[1].split()[2], "app")
        with self.assertRaises(fleet.FleetError):
            fleet.parse_since("yesterday")


class FilesTests(FleetTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_prep_creates_dir_and_excludes_once(self):
        first = fleet.prep_dir(str(self.repo))
        second = fleet.prep_dir(str(self.repo))
        self.assertTrue(Path(first["fleet_dir"]).is_dir())
        exclude = Path(first["exclude_file"]).read_text()
        self.assertEqual(exclude.count("/.fleet/"), 1)
        self.assertTrue(second["excluded"])
        (self.repo / ".fleet" / "BRIEF.md").write_text("x")
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertEqual(status.strip(), "")

    def test_prep_in_linked_worktree_uses_common_exclude(self):
        wt = Path(self.tmp.name) / "wt"
        git("worktree", "add", "-q", "-b", "lane", str(wt), cwd=self.repo)
        out = fleet.prep_dir(str(wt))
        self.assertEqual(Path(out["fleet_dir"]).resolve(), (wt / ".fleet").resolve())
        self.assertTrue(out["exclude_file"].endswith(os.path.join("info", "exclude")))
        self.assertNotIn("worktrees", out["exclude_file"])

    def lane(self, roles=None):
        d = self.repo / ".fleet"
        d.mkdir(exist_ok=True)
        (d / "BRIEF.md").write_text("brief")
        (d / "STATUS.md").write_text("status")
        if roles:
            fleet.save_panes(d, roles)
        return d

    def test_session_start_lane_context_without_roles(self):
        self.lane()
        sub = self.repo / "src"
        sub.mkdir()
        text = fleet.session_start_context({"source": "clear", "cwd": str(sub)}, env={})
        self.assertIn("fleet lane files", text)
        self.assertIn("If you implement this lane", text)
        self.assertIn("BRIEF.md", text)
        self.assertIn("cleared", text)
        self.assertIsNone(fleet.session_start_context({"source": "startup", "cwd": str(sub)}, env={}))

    def test_session_start_impl_role(self):
        self.lane({"w1:p1": {"role": "impl"}, "w1:p2": {"role": "critic"}})
        text = fleet.session_start_context({"source": "compact", "cwd": str(self.repo)}, env={"HERDR_PANE_ID": "w1:p1"})
        self.assertIn("You implement this lane", text)
        self.assertIn("compacted", text)

    def test_session_start_critic_role_reads_latest_request(self):
        d = self.lane({"w1:p1": {"role": "impl"}, "w1:p2": {"role": "critic"}})
        (d / "REVIEW-REQUEST-2.md").write_text("r2")
        (d / "REVIEW-REQUEST-10.md").write_text("r10")
        text = fleet.session_start_context({"source": "clear", "cwd": str(self.repo)}, env={"HERDR_PANE_ID": "w1:p2"})
        self.assertIn("you do not implement it", text)
        self.assertIn("REVIEW-REQUEST-10.md", text)
        self.assertNotIn("You implement", text)

    def test_session_start_unregistered_pane_stays_quiet_when_lane_has_implementer(self):
        self.lane({"w1:p1": {"role": "impl"}})
        self.assertIsNone(fleet.session_start_context({"source": "clear", "cwd": str(self.repo)},
                                                     env={"HERDR_PANE_ID": "w9:p9"}))

    def test_session_start_old_roles_byte_for_byte(self):
        """The texts of the roles that existed before planner and researcher, exactly as before."""
        d = self.lane({"w1:p1": {"role": "impl"}, "w1:p2": {"role": "critic"}, "w1:p3": {"role": "helper"},
                       "w1:p5": {"role": "researcher"}})
        d = Path(os.path.realpath(d))  # git prints the real path
        files = f"{d / 'BRIEF.md'}, {d / 'STATUS.md'}"

        def text(pane, source="clear"):
            return fleet.session_start_context({"source": source, "cwd": str(self.repo)},
                                               env={"HERDR_PANE_ID": pane} if pane else {})
        self.assertEqual(text("w1:p1"), (
            "Fleet lane: your context was just cleared. You implement this lane (pane w1:p1). Before you act, "
            f"read {files}, then run `git status` and `git log --oneline -15`. Continue from the next step in "
            "STATUS.md; HANDOFF.md, when present, is newer than STATUS.md. Keep STATUS.md current: the next "
            "session resumes from it."))
        self.assertEqual(text("w1:p2", "compact"), (
            "Fleet lane: your context was just compacted. You review this lane (pane w1:p2); you do not implement "
            f"it. Before you act, re-read {d / 'BRIEF.md'} and wait for a review request. Write findings only to "
            "the review file the request names, and do not edit code."))
        generic = ("Fleet lane: your context was just cleared. You are this lane's {} (pane {}). "
                   f"Its files are {files}; re-read the ones your task needs before you act.")
        self.assertEqual(text("w1:p3"), generic.format("helper", "w1:p3"))
        self.assertEqual(text("w1:p5"), generic.format("researcher", "w1:p5"))  # no task: the generic text
        self.assertIsNone(text("w9:p9"))
        fleet.save_panes(d, {})
        self.assertEqual(text(None), (
            f"Fleet lane: your context was just cleared. This worktree has fleet lane files: {files}. If you "
            "implement this lane, read them, run `git status` and `git log --oneline -15`, and continue from "
            "the next step in STATUS.md. If you have another role here, re-read the file that defines your "
            "task before you act."))

    def test_session_start_ignores_the_risk_files(self):
        """RISK.md and a plan with high-risk paths change no re-brief, and the hook runs no extra git for them."""
        d = Path(os.path.realpath(self.lane({"w1:p1": {"role": "impl"}, "w1:p2": {"role": "critic"},
                                             "w1:p4": {"role": "planner"}})))
        (d / "PLAN.md").write_text("Risk: none\n## Files\n- db/migrate/1.sql\nPLAN READY: 1\n")

        def texts():
            return [fleet.session_start_context({"source": source, "cwd": str(self.repo)}, env={"HERDR_PANE_ID": pane})
                    for pane in ("w1:p1", "w1:p2", "w1:p4", "w9:p9") for source in ("clear", "compact")]
        before = texts()
        (d / "RISK.md").write_text("# Risk approvals: main\n\nRISK APPROVED: migration db/migrate/1.sql by user x\n")
        (self.repo / "db" / "migrate").mkdir(parents=True)
        (self.repo / "db" / "migrate" / "1.sql").write_text("")
        with mock.patch.object(fleet, "risk_state", side_effect=AssertionError("the hook checks no diff")), \
                mock.patch.object(fleet, "lane_changed_paths", side_effect=AssertionError("no git log")):
            self.assertEqual(texts(), before)
        self.assertNotIn("RISK.md", fleet.LANE_FILES)

    def test_session_start_planner_researcher_and_helper(self):
        d = self.lane({"w1:p1": {"role": "impl"}, "w1:p4": {"role": "planner"},
                       "w1:p5": {"role": "researcher", "task": "/home/x/research/q.md"},
                       "w1:p6": {"role": "helper", "task": "/home/x/task.md"},
                       "w1:p7": {"role": "impl", "task": "/home/x/ignored.md"}})
        d = Path(os.path.realpath(d))

        def text(pane, source="compact"):
            return fleet.session_start_context({"source": source, "cwd": str(self.repo)},
                                               env={"HERDR_PANE_ID": pane})
        for source, what in (("clear", "cleared"), ("compact", "compacted")):
            self.assertEqual(text("w1:p4", source), (
                f"Fleet lane: your context was just {what}. You plan this lane (pane w1:p4); you do not implement "
                f"it. Re-read {d / 'BRIEF.md'} and your {d / 'PLAN.md'}, change no file except PLAN.md, and "
                "continue the plan. When it is done, end PLAN.md with the next PLAN READY line and stop."))
            self.assertIn(f"just {what}. You research one question (pane w1:p5)", text("w1:p5", source))
            self.assertIn(f"just {what}. You are a helper (pane w1:p6)", text("w1:p6", source))
        for source in ("startup", "resume"):
            self.assertIsNone(text("w1:p4", source))
            self.assertIsNone(text("w1:p5", source))
        research = text("w1:p5")
        self.assertIn("Re-read your task file /home/x/research/q.md", research)
        self.assertIn("RESEARCH DONE", research)
        self.assertNotIn("implement this lane", research)
        self.assertIn("Re-read your task file /home/x/task.md", text("w1:p6"))
        self.assertIn("You implement this lane", text("w1:p7"))  # only a researcher or helper uses its task
        # The impl re-brief names PLAN.md once the lane has one.
        self.assertNotIn("PLAN.md", text("w1:p1"))
        (self.repo / ".fleet" / "PLAN.md").write_text("# Plan")
        self.assertIn(f"{d / 'STATUS.md'}, {d / 'PLAN.md'}", text("w1:p1"))
        # A researcher in a checkout without lane files still gets its task.
        for name in ("BRIEF.md", "STATUS.md", "PLAN.md"):
            (self.repo / ".fleet" / name).unlink()
        self.assertIn("/home/x/research/q.md", text("w1:p5"))
        self.assertIsNone(text("w1:p1"))

    def test_session_start_orchestrator_context(self):
        d = self.repo / ".fleet"
        d.mkdir()
        (d / "FLEET.md").write_text("# Fleet\n\nOrchestrator: pane w1:p1, account claude:max.\n")
        mine = fleet.session_start_context({"source": "compact", "cwd": str(self.repo)}, env={"HERDR_PANE_ID": "w1:p1"})
        self.assertIn("Fleet orchestrator", mine)
        self.assertIn("compacted", mine)
        self.assertNotIn("lane files", mine)
        other = fleet.session_start_context({"source": "compact", "cwd": str(self.repo)}, env={"HERDR_PANE_ID": "w2:p3"})
        self.assertIsNone(other)

    def test_session_start_fleet_md_without_orchestrator_line(self):
        d = self.repo / ".fleet"
        d.mkdir()
        (d / "FLEET.md").write_text("lanes")
        text = fleet.session_start_context({"source": "clear", "cwd": str(self.repo)}, env={})
        self.assertIn("If you orchestrate that fleet", text)

    def test_session_start_silent_without_files(self):
        self.assertIsNone(fleet.session_start_context({"source": "clear", "cwd": str(self.repo)}, env={}))
        (self.repo / ".fleet").mkdir()
        self.assertIsNone(fleet.session_start_context({"source": "clear", "cwd": str(self.repo)}, env={}))

    def test_handoff_path_lane_and_shared_checkout(self):
        d = self.repo / ".fleet"
        d.mkdir()
        shared = fleet.handoff_path(d)
        self.assertEqual(shared.parent.name, "handoffs")
        self.assertTrue(shared.parent.is_dir())
        (d / "BRIEF.md").write_text("brief")
        self.assertEqual(fleet.handoff_path(d), d / "HANDOFF.md")

    def test_register_and_unregister_pane(self):
        path = fleet.register_pane(str(self.repo), "w1:p4", {"role": "impl"})
        self.assertTrue(path.endswith(".fleet/panes.json"))
        self.assertEqual(fleet.load_panes(self.repo / ".fleet")["w1:p4"]["role"], "impl")
        self.assertTrue(fleet.unregister_pane(str(self.repo), "w1:p4"))
        self.assertFalse(fleet.unregister_pane(str(self.repo), "w1:p4"))
        self.assertEqual(fleet.load_panes(self.repo / ".fleet"), {})

    def test_session_account_codex_and_claude(self):
        tmp = Path(self.tmp.name)
        codex_a, codex_b, claude_c = tmp / "codex-a", tmp / "codex-b", tmp / "claude-c"
        (codex_a / "sessions" / "2026" / "11" / "03").mkdir(parents=True)
        (codex_b / "sessions").mkdir(parents=True)
        (codex_a / "sessions" / "2026" / "11" / "03" / "rollout-2026-11-03T11-40-34-01a19f7e-8f94.jsonl").write_text("")
        (claude_c / "sessions").mkdir(parents=True)
        (claude_c / "sessions" / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "abc12345-1"}))
        accounts = [
            {"account": "codex:a", "provider": "codex", "name": "a", "home": str(codex_a)},
            {"account": "codex:b", "provider": "codex", "name": "b", "home": str(codex_b)},
            {"account": "claude:c", "provider": "claude", "name": "c", "home": str(claude_c)},
        ]
        self.assertEqual(fleet.session_account("codex", "01a19f7e-8f94", accounts), "codex:a")
        self.assertIsNone(fleet.session_account("codex", "ffffffff-0000", accounts))
        self.assertEqual(fleet.session_account("claude", "abc12345-1", accounts), "claude:c")
        self.assertIsNone(fleet.session_account("claude", "../../etc", accounts))

    def test_transcript_visible(self):
        home = Path(self.tmp.name) / "home"
        (home / "projects" / "-x-y").mkdir(parents=True)
        (home / "projects" / "-x-y" / "abc12345-0000.jsonl").write_text("{}\n")
        self.assertTrue(fleet.transcript_visible(str(home), "abc12345-0000"))
        self.assertFalse(fleet.transcript_visible(str(home), "abc12345-9999"))

    def test_hook_command_end_to_end(self):
        d = self.repo / ".fleet"
        d.mkdir()
        (d / "HANDOFF.md").write_text("handoff")
        payload = json.dumps({"source": "compact", "cwd": str(self.repo), "hook_event_name": "SessionStart"})
        cp = subprocess.run([*HELPER, "hook", "session-start"], input=payload,
                            capture_output=True, text=True, timeout=CHILD_LIMIT)
        self.assertEqual(cp.returncode, 0)
        out = json.loads(cp.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("HANDOFF.md", out["hookSpecificOutput"]["additionalContext"])

    def test_helper_child_exits_with_the_code_of_main(self):
        # The launcher runs the script's own `sys.exit(main())`: an error of main is the child's exit code.
        with tempfile.TemporaryDirectory() as tmp:
            cp = subprocess.run([*HELPER, "log", "no-such-project", "--json"], capture_output=True, text=True,
                                timeout=CHILD_LIMIT, env={**os.environ, "FLEET_HOME": str(Path(tmp) / "home")})
        self.assertEqual((cp.returncode, cp.stdout, cp.stderr),
                         (1, "", "fleet: no project 'no-such-project'. Projects: none yet\n"))

    def test_hook_command_survives_bad_input(self):
        # The script itself, as hooks.json runs it: bad input ends before any git call.
        cp = subprocess.run([sys.executable, str(SCRIPT), "hook", "session-start"], input="not json",
                            capture_output=True, text=True, timeout=CHILD_LIMIT)
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(cp.stdout, "")


@contextmanager
def chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


class MarkerTests(FleetTestCase):
    """Markers are lines: a marker counts at the start of a line, after list or bold marks, not in a sentence."""

    SENTENCE = "## Plan\n5. Verify, then add READY FOR REVIEW: round 1 and stop.\nSee `READY FOR REVIEW: round 1`.\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(os.path.realpath(self.tmp.name)) / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        (self.repo / ".fleet").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def watch(self, text, marker="READY FOR REVIEW: round 1"):
        (self.repo / ".fleet" / "STATUS.md").write_text(text)
        info = {"pane_id": "w2:p1", "cwd": str(self.repo), "agent": "claude", "agent_status": "idle"}
        buf = io.StringIO()
        with mock.patch.object(fleet, "require_herdr"), mock.patch.object(fleet, "pane_info", return_value=info), \
                mock.patch.object(fleet, "screen_tail", return_value=[]), mock.patch.object(fleet.time, "sleep"), \
                redirect_stdout(buf):
            code = fleet.main(["watch", "w2:p1", "--marker", marker, "--idle-grace", "0", "--interval", "0"])
        return code, json.loads(buf.getvalue())["event"]

    def test_watch_matches_a_line_not_a_sentence(self):
        self.assertEqual(self.watch(self.SENTENCE), (6, "stopped"))
        for line in ("READY FOR REVIEW: round 1", "- READY FOR REVIEW: round 1", "**READY FOR REVIEW: round 1**",
                     "`READY FOR REVIEW: round 1`", "> READY FOR REVIEW: round 1", "  READY FOR REVIEW: round 1  "):
            self.assertEqual(self.watch(self.SENTENCE + line + "\n"), (0, "marker"), line)
        # Round 1 is not round 10, and the marker's own regex characters are literal.
        self.assertEqual(self.watch("READY FOR REVIEW: round 10\n"), (6, "stopped"))
        self.assertEqual(self.watch("a.b (x)\n", marker="a.b (x)"), (0, "marker"))
        self.assertEqual(self.watch("axb (x)\n", marker="a.b (x)"), (6, "stopped"))

    def test_board_ready_line(self):
        d = self.repo / ".fleet"
        idle = [{"pane": "w2:p1", "agent": "claude", "status": "idle", "role": "impl"}]
        (d / "STATUS.md").write_text(self.SENTENCE)
        self.assertEqual(fleet.lane_phase({"panes": idle, "pr": None}, d), "idle")
        (d / "STATUS.md").write_text(self.SENTENCE + "**READY FOR REVIEW: round 2**\n")
        self.assertEqual(fleet.lane_phase({"panes": idle, "pr": None}, d), "ready 2")
        # A sentence under Next step is a note now; the marker line is not.
        md = "## Next step\nAdd READY FOR REVIEW: round 1 when done.\n"
        self.assertEqual(fleet.first_note(md, r"Next steps?"), "Add READY FOR REVIEW: round 1 when done.")
        self.assertIsNone(fleet.first_note("## Next step\nREADY FOR REVIEW: round 1\n", r"Next steps?"))


class ApprovePlanTests(FleetTestCase):
    """approve-plan, the gate between a planner and the implementer: it refuses on every condition of the design,
    and then writes neither PLAN.md nor the log."""

    PLAN = "# Plan: x\n\nRisk: none\n\n## Approach\nDo it.\n\n## Files\n- `app.py`: the change\n\nPLAN READY: 1\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.patch = mock.patch.dict(os.environ, {"FLEET_HOME": str(self.root / "home")})
        self.patch.start()
        self.repo = self.root / "app"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        (self.repo / "app.py").write_text("x = 1\n")
        git("add", "app.py", cwd=self.repo)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=self.repo)
        self.lane = self.root / "lane"
        git("worktree", "add", "-q", "-b", "feat/lane", str(self.lane), cwd=self.repo)
        self.plan = Path(fleet.prep_dir(str(self.lane))["fleet_dir"]) / "PLAN.md"

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def approve(self, *argv, cwd=None):
        buf = io.StringIO()
        with chdir(cwd or self.lane), redirect_stdout(buf):
            code = fleet.main(["approve-plan", *argv])
        return code, json.loads(buf.getvalue())

    def approvals(self):
        self.project_home = getattr(self, "project_home", None) or fleet.project_for(str(self.repo))
        return [e for e in fleet.read_events([self.project_home]) if e["event"] == "plan_approved"]

    def logs(self):
        """The bytes of every event file under FLEET_HOME."""
        return {str(p): p.read_bytes() for p in sorted((self.root / "home").rglob("*.jsonl"))}

    def commit(self, path, name, text, message):
        (path / name).write_text(text)
        git("add", name, cwd=path)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message, cwd=path)

    def refused(self, *argv, reason, cwd=None, plan=None):
        plan = plan or self.plan
        before = plan.read_bytes() if plan.is_file() else None
        fleet.record("send", str(self.repo), by=False, pane="w9:p9", text="an earlier event")
        logs = self.logs()
        approvals = self.approvals()
        code, out = self.approve(*argv, cwd=cwd)
        self.assertEqual((code, out["ok"], out["changed"]), (2, False, "nothing"), out)
        self.assertIn(reason, out["reason"])
        self.assertEqual(plan.read_bytes() if plan.is_file() else None, before)
        self.assertEqual(self.logs(), logs)
        self.assertEqual(self.approvals(), approvals)
        return out

    def test_refuses_without_a_plan_or_a_ready_round(self):
        self.refused("--by", "user", reason="no .fleet/PLAN.md")
        self.assertFalse(self.plan.exists())
        self.plan.write_text("# Plan\n\nRisk: none\n\nPLAN READY: <n>\n")  # the template line is no round
        self.refused("--by", "orch", reason="no PLAN READY line")
        self.plan.write_text("Risk: none\n5. Then add PLAN READY: 1 and stop.\n")  # a sentence is no marker
        self.refused("--by", "orch", reason="no PLAN READY line")
        self.plan.write_text(self.PLAN)
        self.refused("--by", "orch", "--round", "2", reason="no PLAN READY: 2 line")
        self.refused("--by", "orch", reason="not in a git worktree", cwd=self.root)

    def test_refuses_changes_outside_fleet(self):
        self.plan.write_text(self.PLAN)
        (self.lane / "app.py").write_text("x = 2\n")
        out = self.refused("--by", "user", reason="1 changed path outside .fleet/")
        self.assertEqual((out["changes"], out["paths"]), (1, ["app.py"]))
        git("checkout", "app.py", cwd=self.lane)
        (self.lane / "src").mkdir()
        for i in range(7):
            (self.lane / "src" / f"m{i}.py").write_text("")
        out = self.refused("--by", "orch", reason="7 changed paths outside .fleet/")
        self.assertEqual(out["paths"], [f"src/m{i}.py" for i in range(5)])  # each file, at most five
        shutil.rmtree(self.lane / "src")
        (self.plan.parent / "notes.md").write_text("inside .fleet/ is fine")
        self.assertEqual(self.approve("--by", "orch")[0], 0)

    def test_git_settings_cannot_hide_untracked_files(self):
        self.plan.write_text(self.PLAN)
        git("config", "status.showUntrackedFiles", "no", cwd=self.lane)
        (self.lane / "new.py").write_text("")
        plain = subprocess.run(["git", "status", "--porcelain"], cwd=self.lane, capture_output=True, text=True)
        self.assertEqual(plain.stdout, "")  # what the gate read before
        self.assertEqual(self.refused("--by", "user", reason="outside .fleet/")["paths"], ["new.py"])

    def note(self, lane=None):
        """The board's note for the lane at plan ready, which says what approve-plan says."""
        record = fleet.board_record({"path": str(lane or self.lane), "panes": [], "pr": None, "branch": None}, [])
        self.assertEqual(record["phase"], "plan ready 1")
        return record.get("warning")

    def gate_reads(self):
        """What the gate read before 0.8.1: git status as `status_paths` ran it, without the scan options."""
        return subprocess.run(["git", "status", "--porcelain", "-z", "--untracked-files=all",
                               "--ignore-submodules=none"], cwd=self.lane, capture_output=True, text=True).stdout

    def test_a_stale_monitor_cannot_hide_a_change(self):
        self.plan.write_text(self.PLAN)
        install_stale_monitor(self.root, self.repo, self.lane, lambda path, *args: git(*args, cwd=path))
        self.assertIsNone(self.note())  # without a change the gate passes
        # Another size: a git without nanosecond times (Apple's) compares mtimes in whole seconds, so a change of
        # the same size in the second of the checkout looks clean to git itself once the plain status below has
        # written the index in a later second; the gate would miss it whatever its scan options.
        (self.lane / "app.py").write_text("x = 20\n")
        self.assertEqual(self.gate_reads(), "")  # the monitor missed it
        out = self.refused("--by", "user", reason="1 changed path outside .fleet/")
        self.assertEqual((out["changes"], out["paths"]), (1, ["app.py"]))
        self.assertEqual(self.note(), "1 changed path outside .fleet/: approve-plan refuses")
        (self.lane / "app.py").write_text("x = 1\n")
        self.assertIsNone(self.note())
        self.assertEqual(self.approve("--by", "orch")[0], 0)

    def test_a_stale_untracked_cache_cannot_hide_a_new_file(self):
        # The untracked cache serves `--untracked-files=all` only when the config's mode is all too, and it goes
        # stale only when a monitor says that no directory changed.
        self.plan.write_text(self.PLAN)
        git("config", "status.showUntrackedFiles", "all", cwd=self.lane)
        install_stale_monitor(self.root, self.repo, self.lane, lambda path, *args: git(*args, cwd=path))
        for _ in range(3):
            self.gate_reads()
        self.assertIsNone(self.note())
        (self.lane / "new.py").write_text("")
        self.assertEqual(self.gate_reads(), "")  # neither the monitor nor the cache shows it
        self.assertEqual(self.refused("--by", "user", reason="1 changed path outside .fleet/")["paths"], ["new.py"])
        self.assertEqual(self.note(), "1 changed path outside .fleet/: approve-plan refuses")

    def test_refuses_flagged_paths(self):
        self.plan.write_text(self.PLAN)
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag):
                git("update-index", flag, "app.py", cwd=self.lane)
                (self.lane / "app.py").write_text("x = 2\n")
                self.assertEqual(fleet.status_paths(str(self.lane)), [])  # git status does not look at it
                for by in ("orch", "user"):
                    out = self.refused("--by", by, reason="1 path marked assume-unchanged or skip-worktree")
                    self.assertEqual(out["flagged"], 1)
                self.assertEqual(self.note(), "1 path marked assume-unchanged or skip-worktree: approve-plan refuses")
                git("update-index", flag.replace("--", "--no-", 1), "app.py", cwd=self.lane)
                git("checkout", "app.py", cwd=self.lane)
        # A skip-worktree path that is absent (a sparse checkout) holds no work.
        git("update-index", "--skip-worktree", "app.py", cwd=self.lane)
        (self.lane / "app.py").unlink()
        self.assertIsNone(self.note())
        self.assertEqual(self.approve("--by", "orch")[0], 0)

    def test_a_failing_ls_files_refuses(self):
        self.plan.write_text(self.PLAN)
        with mock.patch.object(fleet, "flagged_count", return_value=None):
            self.refused("--by", "user", reason="git ls-files failed")
            self.assertEqual(self.note(), "git ls-files failed: approve-plan refuses")
        # Both say every reason, in the same order: changes, flags, commits.
        self.commit(self.lane, "lib.py", "", "planner wrote code")
        (self.lane / "app.py").write_text("x = 2\n")
        git("update-index", "--assume-unchanged", "lib.py", cwd=self.lane)
        (self.lane / "lib.py").write_text("y = 1\n")
        self.assertEqual(self.note(), "1 changed path outside .fleet/, 1 path marked assume-unchanged or "
                                      "skip-worktree, 1 lane commit: approve-plan refuses")
        self.refused("--by", "user", reason="1 changed path outside .fleet/")
        git("checkout", "app.py", cwd=self.lane)
        self.refused("--by", "user", reason="1 path marked assume-unchanged or skip-worktree")

    def test_a_rename_out_of_fleet_is_a_change(self):
        # A base that tracks a file in .fleet/; the lane moves it to a code path, staged, without a commit.
        (self.repo / ".fleet").mkdir(exist_ok=True)
        (self.repo / ".fleet" / "draft.py").write_text("print('x')\n")
        git("add", "-f", ".fleet/draft.py", cwd=self.repo)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "draft", cwd=self.repo)
        lane = self.root / "lane-r"
        git("worktree", "add", "-q", "-b", "feat/r", str(lane), cwd=self.repo)
        plan = Path(fleet.prep_dir(str(lane))["fleet_dir"]) / "PLAN.md"
        plan.write_text(self.PLAN)
        git("mv", ".fleet/draft.py", "app2.py", cwd=lane)
        self.assertIn(" -> ", subprocess.run(["git", "status", "--porcelain"], cwd=lane, capture_output=True,
                                             text=True).stdout)
        self.assertEqual(fleet.status_paths(str(lane)), ["app2.py", ".fleet/draft.py"])
        out = self.refused("--by", "user", reason="1 changed path outside .fleet/", cwd=lane, plan=plan)
        self.assertEqual(out["paths"], ["app2.py"])
        # A name with spaces and an arrow stays one path.
        git("mv", "app2.py", "a -> b.py", cwd=lane)
        self.assertEqual(fleet.outside_fleet(fleet.status_paths(str(lane))), ["a -> b.py"])

    def test_refuses_commits_of_the_lane(self):
        self.plan.write_text(self.PLAN)
        self.commit(self.lane, "app.py", "x = 2\n", "planner wrote code")
        out = self.refused("--by", "user", reason="1 commit of its own")
        self.assertIn("--base", out["reason"])
        self.assertEqual(len(out["commits"]), 1)
        self.assertRegex(out["commits"][0], r"^[0-9a-f]{7,} planner wrote code$")
        self.refused("--by", "user", "--base", "main", reason="1 commit of its own")
        # A remote branch of the lane's own name does not hide it, on any remote.
        for name in ("origin", "upstream"):
            remote = self.root / f"{name}.git"
            git("init", "-q", "--bare", str(remote), cwd=self.root)
            git("remote", "add", name, str(remote), cwd=self.repo)
            git("push", "-q", name, "main", "feat/lane", cwd=self.lane)
        self.refused("--by", "orch", reason="1 commit of its own")
        # A commit and its revert leave the tree as it was, and still count.
        git("-c", "user.email=t@t", "-c", "user.name=t", "revert", "--no-edit", "HEAD", cwd=self.lane)
        self.assertEqual(subprocess.run(["git", "diff", "main", "--stat"], cwd=self.lane, capture_output=True,
                                        text=True).stdout, "")
        self.assertEqual(self.refused("--by", "orch", reason="2 commits of its own")["lane_commits"], 2)
        # --base names the start when a commit predates the plan.
        code, out = self.approve("--by", "orch", "--base", "HEAD")
        self.assertEqual((code, out["base"]), (0, "HEAD"))
        self.assertEqual(self.approvals()[0]["base"], "HEAD")

    def assert_one_lane_commit(self, lane, plan):
        """The lane's one commit counts: in lane_commits, in the board's note, and in a refusal by either approver."""
        commits, why = fleet.lane_commits(str(lane))
        self.assertEqual((len(commits), why), (1, None))
        record = fleet.board_record({"path": str(lane), "panes": [], "pr": None, "branch": None}, [])
        self.assertEqual((record["phase"], record["warning"]), ("plan ready 1", "1 lane commit: approve-plan refuses"))
        for by in ("orch", "user"):
            self.assertEqual(self.refused("--by", by, reason="1 commit of its own", cwd=lane, plan=plan)["lane_commits"], 1)

    def test_a_tag_named_like_the_branch_does_not_hide_its_commits(self):
        git("tag", "feat/lane", cwd=self.repo)  # at the base: it does not hold the lane's commit
        self.plan.write_text(self.PLAN)
        self.commit(self.lane, "app.py", "x = 2\n", "planner wrote code")
        short = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=self.lane, capture_output=True,
                               text=True).stdout.strip()
        self.assertEqual(short, "heads/feat/lane")  # what the gate read before
        self.assert_one_lane_commit(self.lane, self.plan)

    def test_a_branch_name_that_ends_in_unicode_whitespace(self):
        branch = "feat/lane\u00a0"  # a valid name; strip() would cut the space and exclude self.lane's feat/lane
        lane = self.root / "lane-nbsp"
        git("worktree", "add", "-q", "-b", branch, str(lane), cwd=self.repo)
        plan = Path(fleet.prep_dir(str(lane))["fleet_dir"]) / "PLAN.md"
        plan.write_text(self.PLAN)
        self.commit(lane, "app.py", "x = 3\n", "planner wrote code")
        self.assert_one_lane_commit(lane, plan)

    def test_a_remote_branch_in_a_deeper_folder_holds_the_base(self):
        # origin/team/feat/y holds a commit that no local branch or tag holds; the lane feat/y starts there.
        remote = self.root / "origin.git"
        git("init", "-q", "--bare", str(remote), cwd=self.root)
        git("remote", "add", "origin", str(remote), cwd=self.repo)
        git("checkout", "-q", "-b", "tmp", cwd=self.repo)
        self.commit(self.repo, "team.py", "", "the team's work")
        git("push", "-q", "origin", "tmp:refs/heads/team/feat/y", cwd=self.repo)
        git("checkout", "-q", "main", cwd=self.repo)
        git("branch", "-q", "-D", "tmp", cwd=self.repo)
        git("fetch", "-q", "origin", cwd=self.repo)
        lane = self.root / "lane-y"
        git("worktree", "add", "-q", "-b", "feat/y", str(lane), "origin/team/feat/y", cwd=self.repo)
        plan = Path(fleet.prep_dir(str(lane))["fleet_dir"]) / "PLAN.md"
        plan.write_text(self.PLAN)
        self.assertEqual(fleet.lane_commits(str(lane)), ([], None))
        self.assertEqual(self.approve("--by", "orch", cwd=lane)[0], 0)

    def test_a_lane_on_a_feature_branch_or_ahead_of_origin_has_no_commits(self):
        self.commit(self.repo, "lib.py", "", "main moves on, ahead of any remote")
        git("branch", "feat/base", cwd=self.repo)
        self.commit(self.repo, "more.py", "", "main moves again")
        lane = self.root / "stacked"
        git("worktree", "add", "-q", "-b", "feat/stacked", str(lane), "feat/base", cwd=self.repo)
        plan = Path(fleet.prep_dir(str(lane))["fleet_dir"]) / "PLAN.md"
        plan.write_text(self.PLAN)
        self.assertEqual(fleet.lane_commits(str(lane)), ([], None))
        self.assertEqual(self.approve("--by", "orch", cwd=lane)[0], 0)

    def test_detached_head_and_a_bad_base(self):
        self.plan.write_text(self.PLAN)
        git("checkout", "-q", "--detach", cwd=self.lane)
        self.refused("--by", "user", reason="HEAD is detached")
        self.refused("--by", "orch", "--base", "no-such-ref", reason="--base no-such-ref does not name a commit")
        self.refused("--by", "orch", "--base=--all", reason="does not name a commit")
        self.assertEqual(self.approve("--by", "orch", "--base", "main")[0], 0)

    def test_a_round_number_too_long_is_no_marker(self):
        self.plan.write_text("Risk: none\nPLAN READY: " + "9" * 5000 + "\n")
        self.refused("--by", "orch", reason="no PLAN READY line")
        self.plan.write_text("Risk: none\nPLAN READY: 1234567890\n")
        self.refused("--by", "orch", reason="no PLAN READY line")
        self.plan.write_text("Risk: none\n## Files\n- app.py\nPLAN READY: 123456789\nPLAN APPROVED: " + "1" * 5000 + "\n")
        self.assertEqual(self.approve("--by", "orch")[1]["round"], 123456789)

    def test_risk_needs_the_user(self):
        for risk in (None, "none | migration | auth-payments | deletion | hotfix", "migration", "auth-payments",
                     "deletion", "hotfix", "low", "none, deletion"):
            head = "" if risk is None else f"Risk: {risk}\n"
            self.plan.write_text(f"# Plan\n{head}\n## Approach\nx\n\nPLAN READY: 1\n")
            self.assertEqual(self.refused("--by", "orch", reason="Ask the user")["risk"], risk)
        # A second Risk line that disagrees also needs the user.
        self.plan.write_text("Risk: none\n## Risks\n- Risk: deletion of old rows\nPLAN READY: 1\n")
        self.assertEqual(self.refused("--by", "orch", reason="Ask the user")["risk"], "none, deletion of old rows")
        self.plan.write_text("# Plan\n**Risk:** `deletion`\n\nPLAN READY: 1\n")
        code, out = self.approve("--by", "user")
        self.assertEqual((code, out["ok"], out["risk"], out["round"]), (0, True, "deletion", 1))
        self.assertRegex(self.plan.read_text().splitlines()[-1], r"^PLAN APPROVED: 1 by user \d{4}-\d\d-\d\dT")
        self.assertEqual([(e["round"], e["approved_by"], e["risk"]) for e in self.approvals()], [(1, "user", "deletion")])

    def test_high_risk_files_need_the_user(self):
        # A built-in pattern makes the plan high risk whatever its Risk: line says.
        self.plan.write_text("# Plan\nRisk: none\n\n## Files\n- app.py\n- `db/migrate/001.sql`: new table\n\n"
                             "PLAN READY: 1\n")
        out = self.refused("--by", "orch", reason="names high-risk paths under ## Files (migration db/migrate/001.sql)")
        self.assertIn("only the user can approve", out["reason"])
        self.assertEqual(out["high_risk"], [{"path": "db/migrate/001.sql", "category": "migration",
                                             "pattern": "**/migrate/**", "source": "builtin"}])
        code, out = self.approve("--by", "user")
        self.assertEqual((code, out["files"], [m["path"] for m in out["high_risk"]]), (0, 2, ["db/migrate/001.sql"]))
        [event] = self.approvals()
        self.assertEqual((event["files"], event["high_risk"]), (2, [{"category": "migration", "path": "db/migrate/001.sql"}]))
        self.assertEqual(fleet.describe_event(event), "plan round 1 approved by user, risk none; high-risk paths: "
                                                      "migration db/migrate/001.sql")
        logged = [e for e in fleet.read_events([fleet.project_for(str(self.repo))]) if e["event"] == "plan_approved"]
        self.assertEqual(fleet.clean_event(logged[0])["high_risk"], event["high_risk"])  # `fleet log` keeps it

    def test_project_patterns_override_the_risk_line(self):
        home = fleet.project_for(str(self.repo))
        (home / "PROJECT.md").write_text("# Project\n\n## High-risk paths\n- auth-payments: app/auth/\n")
        self.plan.write_text("Risk: none\n## Files\n- app/auth/session.py: shorter tokens\nPLAN READY: 1\n")
        out = self.refused("--by", "orch", reason="auth-payments app/auth/session.py")
        self.assertEqual(out["high_risk"][0]["source"], "PROJECT.md")
        # A pattern source that cannot be read refuses both approvers.
        (home / "PROJECT.md").write_text("## High-risk paths\n- payments: app/pay/\n")
        for by in ("orch", "user"):
            self.refused("--by", by, reason="the project's high-risk paths cannot be read")
        (home / "PROJECT.md").write_text("## High-risk paths\n- deletion: [z-a]\n")
        self.refused("--by", "user", reason="is not valid")

    def test_a_failed_project_lookup_refuses_both_approvers(self):
        home = fleet.project_for(str(self.repo))
        (home / "PROJECT.md").write_text("# Project\n\n## High-risk paths\n- auth-payments: app/auth/\n")
        self.plan.write_text("Risk: none\n## Files\n- app/auth/session.py: shorter tokens\nPLAN READY: 1\n")
        real = fleet.git_out

        def failing(path, *args, **kw):
            return None if args[:2] == ("rev-parse", "--git-common-dir") else real(path, *args, **kw)
        self.approvals()  # finds the project home before the lookup fails
        with mock.patch.object(fleet, "git_out", failing):
            for by in ("orch", "user"):
                self.refused("--by", by, reason="git cannot tell the project of")
        real_run = fleet.run_git

        def config_fails(path, *args, **kw):  # git config failing (not "no remote") is a failed lookup too
            if args[:2] == ("config", "--get-regexp"):
                return subprocess.CompletedProcess(args, 3, "", "bad config")
            return real_run(path, *args, **kw)
        with mock.patch.object(fleet, "run_git", config_fails):
            self.assertEqual(fleet.project_lookup(str(self.lane)), (None, f"git cannot tell the project of {self.lane}"))
            self.assertIsNotNone(fleet.repo_identity(str(self.lane)))  # the hook's lookup keeps its behaviour
        self.assertEqual(fleet.project_lookup(str(self.lane)), (home, None))
        self.refused("--by", "orch", reason="auth-payments app/auth/session.py")

    def test_a_later_files_section_counts(self):
        self.plan.write_text(self.PLAN)
        self.assertEqual(self.approve("--by", "orch")[0], 0)
        with self.plan.open("a") as fh:
            fh.write("\n## Approach (round 2)\nAlso migrate.\n\n## Files\n- db/migrate/002.sql: the new index\n\n"
                     "PLAN READY: 2\n")
        self.assertEqual(fleet.plan_files(self.plan.read_text()), ["app.py", "db/migrate/002.sql"])
        out = self.refused("--by", "orch", reason="migration db/migrate/002.sql")
        self.assertEqual((out["round"], out["files"]), (2, 2))
        self.assertEqual(fleet.plan_categories(self.plan.read_text(), fleet.risk_patterns(None)[0]), {"migration"})

    def test_a_plan_without_files_needs_the_user(self):
        for files in ("", "## Files\n\n", "## Files\n- <path>: <what changes>\n", "## Files\n- none\n"):
            self.plan.write_text(f"# Plan\nRisk: none\n\n{files}PLAN READY: 1\n")
            self.assertEqual(self.refused("--by", "orch", reason="lists no files under ## Files")["files"], 0)
        # A path that is no list item counts for the high-risk match, not as a listed file.
        self.plan.write_text("Risk: none\n## Files\nThese change:\ndb/migrate/002.sql\nPLAN READY: 1\n")
        out = self.refused("--by", "orch", reason="migration db/migrate/002.sql")
        self.assertEqual(out["files"], 0)
        code, out = self.approve("--by", "user")
        self.assertEqual((code, out["files"]), (0, 0))

    def test_orch_approves_a_low_risk_plan_once_per_round(self):
        self.plan.write_text(self.PLAN.rstrip("\n"))  # no newline at the end
        code, out = self.approve("--by", "orch")
        self.assertEqual((code, out["ok"], out["round"], out["by"], out["risk"], out["branch"], out["files"],
                          out["high_risk"]), (0, True, 1, "orch", "none", "feat/lane", 1, []))
        lines = self.plan.read_text().splitlines()
        self.assertEqual(lines[-2], "PLAN READY: 1")
        self.assertTrue(lines[-1].startswith("PLAN APPROVED: 1 by orch "))
        [event] = self.approvals()
        self.assertEqual((event["round"], event["approved_by"], event["risk"], event["branch"], event["lane"]),
                         (1, "orch", "none", "feat/lane", str(self.lane)))
        self.assertEqual(fleet.describe_event(event), "plan round 1 approved by orch, risk none")
        self.assertEqual((event["files"], "high_risk" in event), (1, False))
        # Approving the same round again refuses; the next round can be approved, also by --round.
        before = self.plan.read_bytes()
        code, out = self.approve("--by", "user")
        self.assertEqual((code, out["changed"]), (2, "nothing"))
        self.assertIn("already approved", out["reason"])
        self.assertEqual(self.plan.read_bytes(), before)
        with self.plan.open("a") as fh:
            fh.write("\n## Changes\n- y\n\n- PLAN READY: 2\n")
        self.assertIn("already approved", self.approve("--by", "orch", "--round", "1")[1]["reason"])
        code, out = self.approve(str(self.lane), "--by", "orch", cwd=self.root)
        self.assertEqual((code, out["round"]), (0, 2))
        self.assertEqual([e["round"] for e in self.approvals()], [1, 2])


class PlanFilesTests(FleetTestCase):
    """What approve-plan reads from a PLAN.md besides its rounds: the paths under ## Files, the approver of a
    round, and the categories that the plan shows."""

    def test_plan_files(self):
        text = ("## Files\n\n- `db/migrate/001.sql`: new table\n* **app/auth/login.py** (changed)\n"
                "- ./README.md\n1. src/a.py: x\n- `src/a.py`\n- -\n- none\n- <path>: <what changes>\n"
                "plain/line.txt more words\n```\n\n## Tests\n- not/a/file.py\n")
        self.assertEqual(fleet.plan_files(text), ["db/migrate/001.sql", "app/auth/login.py", "README.md", "src/a.py"])
        # The high-risk match also reads a line that is no list item, and never a marker line.
        self.assertEqual(fleet.plan_files(text + "## Files\nPLAN READY: 1\n", every_line=True),
                         ["db/migrate/001.sql", "app/auth/login.py", "README.md", "src/a.py", "plain/line.txt"])
        self.assertEqual(fleet.plan_files("# Plan\n\nRisk: none\n"), [])
        self.assertEqual(fleet.plan_files("## Files\n- none\n"), [])

    def test_plan_approver(self):
        text = "PLAN READY: 1\nPLAN APPROVED: 1 by orch 2026\nPLAN READY: 2\n- PLAN APPROVED: 2 by user 2026\n"
        self.assertEqual([fleet.plan_approver(text, n) for n in (1, 2, 3)], ["orch", "user", None])
        self.assertEqual(fleet.plan_approver("PLAN APPROVED: 1\n", 1), "orch")  # names no one
        self.assertEqual(fleet.plan_approver("PLAN APPROVED: 1 by user\nPLAN APPROVED: 1 by orch\n", 1), "orch")
        self.assertIsNone(fleet.plan_approver("Then PLAN APPROVED: 1 by user follows.\n", 1))  # a sentence

    def test_plan_categories(self):
        patterns = fleet.risk_patterns(None)[0]
        self.assertEqual(fleet.plan_categories("Risk: none\n## Files\n- app.py\n", patterns), set())
        self.assertEqual(fleet.plan_categories("**Risk:** `deletion`, hotfix and auth-payments\n", patterns),
                         {"deletion", "hotfix", "auth-payments"})
        self.assertEqual(fleet.plan_categories("Risk: none\n## Files\n- db/migrate/1.sql\n", patterns), {"migration"})
        # The template's list of values, and a sentence, show no category.
        self.assertEqual(fleet.plan_categories("Risk: none | migration | auth-payments | deletion | hotfix\n"
                                               "- Risk: deletion of old rows\n", patterns), set())


class RiskPatternTests(FleetTestCase):
    """The high-risk path patterns: the glob rules, the built-ins, and the PROJECT.md section."""

    def matches(self, pattern, path):
        return fleet.glob_regex(pattern).match(path) is not None

    def test_glob_rules(self):
        for pattern, path, want in (
                ("db/*.sql", "db/x.sql", True), ("db/*.sql", "db/a/x.sql", False),  # `*` stops at `/`
                ("db/**/*.sql", "db/a/b/x.sql", True), ("db/**/*.sql", "db/x.sql", True),  # `**` crosses
                ("a**", "ab/c", True), ("a/?.py", "a/b.py", True), ("a/?.py", "a/bc.py", False),
                ("*.sql", "a/b/x.sql", True), ("schema.rb", "db/schema.rb", True),  # no `/`: at any depth
                ("db/*.sql", "x/db/x.sql", False), ("/secrets", "x/secrets", False),  # a `/` anchors at the top
                ("/secrets", "secrets/a", True), ("migrate/", "db/migrate/1.sql", True),  # a trailing `/` does not
                ("app/auth/", "app/auth/x.py", True), ("app/auth/", "app/auth", True),  # `<pattern>**`
                ("app/auth/", "app/authx.py", False), ("app/auth", "app/auth/z/y.py", True),  # a directory prefix
                ("app/auth", "app/author.py", False),
                ("db/[!x]*.rb", "db/a.rb", True), ("db/[!x]*.rb", "db/x.rb", False),  # a character class
                ("db/[ab].rb", "db/b.rb", True), ("a/[/]b", "a//b", False), ("a[b", "a[b", True),
                ("db/schema.rb", "DB/schema.rb", False),  # case-sensitive
                ("./db/x", "db/x", True)):
            self.assertEqual(self.matches(pattern, path), want, (pattern, path))
        for bad in ("[z-a]", "", "/"):
            with self.assertRaises(fleet.re.error):
                fleet.glob_regex(bad)

    def test_builtins(self):
        patterns, error = fleet.risk_patterns(None)
        self.assertIsNone(error)
        self.assertEqual([(p["category"], p["pattern"], p["source"]) for p in patterns],
                         [("migration", g, "builtin") for _, g in fleet.BUILTIN_RISK_PATTERNS])
        paths = ["db/migrate/001.sql", "migrations/0001_init.py", "app/alembic/env.py", "src/migration/x.go",
                 "app/migrate.py", "db/schema.rb", "app/migrations.py"]
        self.assertEqual([m["path"] for m in fleet.match_risk(paths, patterns)], paths[:4])

    def test_project_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(len(fleet.risk_patterns(home)[0]), 4)  # no PROJECT.md
            md = home / "PROJECT.md"
            md.write_text(fleet.PROJECT_TEMPLATE.format(name="acme"))  # the template's section is empty
            patterns, error = fleet.risk_patterns(home)
            self.assertEqual((len(patterns), error), (4, None))
            md.write_text("# Project: acme\n\n## Rules\n- none\n")  # a PROJECT.md from 0.6.0
            self.assertEqual(fleet.risk_patterns(home)[1], None)
            md.write_text("# Project: acme\n\n## High-risk paths\n- auth-payments: app/auth/\n"
                          "* deletion: `jobs/purge_*.py`\n- hotfix: hotfix/\n- migration: db/schema.rb\n\n"
                          "## Notes\n- deletion: not/a/pattern\n")
            patterns, error = fleet.risk_patterns(home)
            self.assertIsNone(error)
            self.assertEqual([(p["category"], p["pattern"], p["source"]) for p in patterns[4:]],
                             [("auth-payments", "app/auth/", "PROJECT.md"), ("deletion", "jobs/purge_*.py", "PROJECT.md"),
                              ("hotfix", "hotfix/", "PROJECT.md"), ("migration", "db/schema.rb", "PROJECT.md")])
            found = fleet.match_risk(["app/auth/login.py", "jobs/purge_old.py", "README.md", "db/schema.rb",
                                      "db/migrate/1.sql"], patterns)
            self.assertEqual([(m["path"], m["category"], m["source"]) for m in found],
                             [("app/auth/login.py", "auth-payments", "PROJECT.md"),
                              ("jobs/purge_old.py", "deletion", "PROJECT.md"),
                              ("db/schema.rb", "migration", "PROJECT.md"),
                              ("db/migrate/1.sql", "migration", "builtin")])
            for line, why in (("- payments: app/pay/", "is not '- <category>: <pattern>'"),
                              ("app/auth/", "is not '- <category>: <pattern>'"),
                              ("- deletion:", "is not '- <category>: <pattern>'"),
                              ("- deletion: [z-a]", "the pattern '[z-a]' is not valid")):
                md.write_text(f"## High-risk paths\n- hotfix: hotfix/\n{line}\n")
                patterns, error = fleet.risk_patterns(home)
                self.assertIn(why, error or "", line)

    def test_first_pattern_wins_and_order_of_paths(self):
        patterns = [{"category": c, "pattern": g, "source": "PROJECT.md", "regex": fleet.glob_regex(g)}
                    for c, g in (("deletion", "app/"), ("auth-payments", "app/auth/"))]
        found = fleet.match_risk(["z.py", "app/auth/x.py", "app/a.py"], patterns)
        self.assertEqual([(m["path"], m["category"]) for m in found],
                         [("app/auth/x.py", "deletion"), ("app/a.py", "deletion")])


class RiskLaneCase(FleetTestCase):
    """A main checkout and a lane worktree in a scratch directory, with a temporary FLEET_HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.patch = mock.patch.dict(os.environ, {"FLEET_HOME": str(self.root / "home")})
        self.patch.start()
        self.repo = self.root / "app"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        self.commit(self.repo, "app.py", "x = 1\n", "init")
        self.lane = self.root / "lane"
        git("worktree", "add", "-q", "-b", "feat/lane", str(self.lane), cwd=self.repo)
        self.fleet_dir = Path(fleet.prep_dir(str(self.lane))["fleet_dir"])
        self.home = fleet.project_for(str(self.repo))

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def commit(self, path, name, text, message):
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
        git("add", name, cwd=path)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message, cwd=path)

    def changed(self, base=None, lane=None):
        return fleet.lane_changed_paths(str(lane or self.lane), base)

    def state(self, base=None):
        return fleet.risk_state(str(self.lane), self.home, base)


class RiskDiffTests(RiskLaneCase):
    """The lane's real changes against the high-risk patterns: which paths count, and who covers a match."""

    def test_changed_paths(self):
        self.assertEqual(self.changed(), ([], None))
        self.commit(self.lane, "db/migrate/001.sql", "create\n", "a migration")
        (self.lane / "notes.txt").write_text("")  # uncommitted
        (self.fleet_dir / "STATUS.md").write_text("in .fleet/: never counts")
        self.assertEqual(self.changed(), (["db/migrate/001.sql", "notes.txt"], None))
        # A commit and its revert leave the tree as it was; both are listed, once.
        self.commit(self.lane, "jobs/purge.py", "x\n", "purge")
        git("-c", "user.email=t@t", "-c", "user.name=t", "revert", "--no-edit", "HEAD", cwd=self.lane)
        self.assertEqual(self.changed()[0], ["db/migrate/001.sql", "jobs/purge.py", "notes.txt"])
        # A merge from a main that moved brings none of main's files.
        self.commit(self.repo, "main_only.py", "", "main moves on")
        git("-c", "user.email=t@t", "-c", "user.name=t", "merge", "-q", "--no-edit", "main", cwd=self.lane)
        self.assertNotIn("main_only.py", self.changed()[0])
        # A committed rename lists both sides.
        git("mv", "app.py", "core.py", cwd=self.lane)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "rename", cwd=self.lane)
        self.assertTrue({"app.py", "core.py"} <= set(self.changed()[0]))
        # --base: the commits of base..HEAD (a lane that merged main lists main's files too: a known gap).
        self.assertIn("main_only.py", self.changed("main@{1}")[0])
        self.assertEqual(self.changed("no-such-ref"), (None, "--base no-such-ref does not name a commit"))

    def test_git_settings_cannot_hide_a_submodule_change(self):
        sub = self.root / "sub"
        sub.mkdir()
        git("init", "-q", "-b", "main", cwd=sub)
        self.commit(sub, "a", "a\n", "sub")
        git("-c", "protocol.file.allow=always", "submodule", "-q", "add", str(sub), "db/migrate", cwd=self.lane)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "add the submodule", cwd=self.lane)
        self.commit(self.lane / "db" / "migrate", "b", "b\n", "move the submodule")
        git("add", "db/migrate", cwd=self.lane)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "bump", cwd=self.lane)
        git("config", "diff.ignoreSubmodules", "all", cwd=self.lane)
        self.assertIn("db/migrate", self.changed()[0])

    def test_a_stale_monitor_cannot_hide_an_uncommitted_change(self):
        # migrations/001.sql is on main before the lane's branch: only git status can show the lane's change.
        self.commit(self.repo, "migrations/001.sql", "create\n", "a migration on main")
        git("merge", "-q", "--ff-only", "main", cwd=self.lane)
        self.assertEqual(fleet.lane_commits(str(self.lane)), ([], None))
        install_stale_monitor(self.root, self.repo, self.lane, lambda path, *args: git(*args, cwd=path))
        self.assertEqual(self.changed(), ([], None))
        (self.lane / "migrations" / "001.sql").write_text("drop\n")
        plain = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.lane,
                               capture_output=True, text=True)
        self.assertEqual(plain.stdout, "")  # the monitor missed it
        self.assertEqual(self.changed(), (["migrations/001.sql"], None))
        state = self.state()
        self.assertEqual([(m["category"], m["path"], m["covered_by"]) for m in state["needs_user"]],
                         [("migration", "migrations/001.sql", None)])
        buf = io.StringIO()
        with chdir(self.lane), redirect_stdout(buf):
            fleet.main(["risk", "--json"])
        self.assertEqual([m["path"] for m in json.loads(buf.getvalue())["needs_user"]], ["migrations/001.sql"])

    def test_a_root_commit_counts_whatever_log_showroot_says(self):
        solo = self.root / "solo"
        solo.mkdir()
        git("init", "-q", "-b", "feat/solo", cwd=solo)  # the only branch: its root commit is the lane's own
        self.commit(solo, "db/migrate/initial.sql", "create\n", "root")
        git("config", "log.showRoot", "false", cwd=solo)
        self.assertEqual(len(fleet.lane_commits(str(solo))[0]), 1)
        self.assertEqual(self.changed(lane=solo), (["db/migrate/initial.sql"], None))
        self.assertTrue(fleet.needs_user(fleet.risk_state(str(solo), None)))

    def test_a_staged_rename_out_of_fleet(self):
        (self.repo / ".fleet").mkdir(exist_ok=True)
        (self.repo / ".fleet" / "draft.sql").write_text("drop\n")
        git("add", "-f", ".fleet/draft.sql", cwd=self.repo)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "draft", cwd=self.repo)
        lane = self.root / "lane-r"
        git("worktree", "add", "-q", "-b", "feat/r", str(lane), cwd=self.repo)
        (lane / "db" / "migrate").mkdir(parents=True)
        git("mv", ".fleet/draft.sql", "db/migrate/002.sql", cwd=lane)
        self.assertEqual(self.changed(lane=lane), (["db/migrate/002.sql"], None))

    def test_errors_fail_closed(self):
        git("checkout", "-q", "--detach", cwd=self.lane)
        paths, why = self.changed()
        self.assertEqual((paths, why), (None, "HEAD is detached, so the lane's changes cannot be told; pass --base <ref>"))
        state = self.state()
        self.assertEqual((state["error"], fleet.needs_user(state)), (why, True))
        self.assertEqual(self.changed("main"), ([], None))
        git("checkout", "-q", "feat/lane", cwd=self.lane)
        real = fleet.git_text

        def failing(path, *args, **kw):
            return None if args[:1] == ("status",) else real(path, *args, **kw)
        with mock.patch.object(fleet, "git_text", failing):
            self.assertEqual(self.changed(), (None, "git status failed, so the lane's changes cannot be told"))

        def failing_log(path, *args, **kw):
            return None if args[:1] == ("log",) else real(path, *args, **kw)
        with mock.patch.object(fleet, "git_text", failing_log):
            self.assertEqual(self.changed(), (None, "git log failed, so the lane's changes cannot be told"))
        (self.home / "PROJECT.md").write_text("## High-risk paths\n- migrations: db/\n")
        state = self.state()
        self.assertIn("the project's high-risk paths cannot be read", state["error"])
        self.assertTrue(fleet.needs_user(state))

    def test_coverage(self):
        self.assertFalse(fleet.needs_user(self.state()))  # nothing changed
        self.commit(self.lane, "db/migrate/001.sql", "create\n", "a migration")
        state = self.state()
        self.assertEqual([(m["category"], m["path"], m["covered_by"]) for m in state["needs_user"]],
                         [("migration", "db/migrate/001.sql", None)])
        self.assertTrue(fleet.needs_user(state))
        plan = self.fleet_dir / "PLAN.md"
        # A plan that the orch approved covers nothing.
        plan.write_text("Risk: none\n## Files\n- app.py\nPLAN READY: 1\nPLAN APPROVED: 1 by orch 2026\n")
        self.assertEqual(len(self.state()["needs_user"]), 1)
        # A plan that the user approved covers the categories it shows: by its Risk line, or by its files.
        for text in ("Risk: migration\n## Files\n- app.py\n", "Risk: none\n## Files\n- db/migrate/<new>.sql\n"):
            plan.write_text(text + "PLAN READY: 1\nPLAN APPROVED: 1 by user 2026\n")
            state = self.state()
            self.assertEqual((state["needs_user"], state["plan_categories"], state["plan_approved_by"]),
                             ([], ["migration"], "user"), text)
            self.assertEqual(state["covered"][0]["covered_by"], "plan round 1, approved by the user")
        # Not a category that the plan did not show.
        plan.write_text("Risk: deletion\n## Files\n- jobs/purge.py\nPLAN READY: 1\nPLAN APPROVED: 1 by user\n")
        self.assertEqual(len(self.state()["needs_user"]), 1)
        # The newest round counts: a round 2 that is not approved yet covers nothing.
        plan.write_text("Risk: migration\nPLAN READY: 1\nPLAN APPROVED: 1 by user\nPLAN READY: 2\n")
        self.assertEqual(len(self.state()["needs_user"]), 1)
        plan.unlink()
        # A RISK.md line covers its category and path; a new risky path needs a new yes.
        (self.fleet_dir / "RISK.md").write_text("# Risk approvals: feat/lane\n\n"
                                                "RISK APPROVED: migration db/migrate/001.sql by user 2026-11-03T10:00:00+00:00\n"
                                                "RISK APPROVED: deletion db/migrate/002.sql by user 2026\n"
                                                "Then RISK APPROVED: migration db/migrate/002.sql by user x\n")
        self.assertEqual(fleet.risk_approvals(self.fleet_dir),
                         {("migration", "db/migrate/001.sql"), ("deletion", "db/migrate/002.sql")})
        state = self.state()
        self.assertEqual((state["needs_user"], state["covered"][0]["covered_by"]), ([], "RISK.md"))
        (self.lane / "db" / "migrate" / "002.sql").write_text("alter\n")
        self.assertEqual([m["path"] for m in self.state()["needs_user"]], ["db/migrate/002.sql"])


class RiskCommandTests(RiskLaneCase):
    """`risk` (reads only) and `approve-risk` (the record of the user's yes)."""

    def run_cmd(self, *argv, cwd=None):
        buf = io.StringIO()
        with chdir(cwd or self.lane), redirect_stdout(buf):
            code = fleet.main(list(argv))
        return code, buf.getvalue()

    def logs(self):
        return {str(p): p.read_bytes() for p in sorted((self.root / "home").rglob("*.jsonl"))}

    def refused(self, *argv, reason, cwd=None):
        risk_md = self.fleet_dir / "RISK.md"
        before = risk_md.read_bytes() if risk_md.is_file() else None
        fleet.record("send", str(self.repo), by=False, pane="w9:p9", text="an earlier event")
        logs = self.logs()
        code, text = self.run_cmd("approve-risk", *argv, cwd=cwd)
        out = json.loads(text)
        self.assertEqual((code, out["ok"], out["changed"]), (2, False, "nothing"), out)
        self.assertIn(reason, out["reason"])
        self.assertEqual(risk_md.read_bytes() if risk_md.is_file() else None, before)
        self.assertEqual(self.logs(), logs)
        return out

    def test_risk_text_and_json(self):
        code, text = self.run_cmd("risk")
        self.assertEqual((code, text.splitlines()[-1]), (0, "Nothing needs the user."))
        (self.home / "PROJECT.md").write_text("## High-risk paths\n- auth-payments: app/auth/\n")
        self.commit(self.lane, "db/migrate/001.sql", "create\n", "a migration")
        (self.lane / "app" / "auth").mkdir(parents=True)
        (self.lane / "app" / "auth" / "x.py").write_text("")
        (self.fleet_dir / "RISK.md").write_text("RISK APPROVED: auth-payments app/auth/x.py by user 2026\n")
        code, text = self.run_cmd("risk", str(self.lane), cwd=self.root)
        lines = text.splitlines()
        self.assertEqual(lines[0], f"lane {fleet.short_path(str(self.lane))} (feat/lane): 2 changed paths, 2 high-risk")
        self.assertRegex(lines[1], r"^  auth-payments  app/auth/x\.py\s+PROJECT\.md app/auth/\s+covered by RISK\.md$")
        self.assertRegex(lines[2], r"^  migration\s+db/migrate/001\.sql\s+builtin \*\*/migrate/\*\*\s+needs the user$")
        self.assertIn("1 path needs the user", lines[3])
        self.assertIn("approve-risk --by user", lines[3])
        code, text = self.run_cmd("risk", "--json")
        out = json.loads(text)
        self.assertEqual((out["needs_the_user"], [m["path"] for m in out["needs_user"]], out["branch"]),
                         (True, ["db/migrate/001.sql"], "feat/lane"))
        git("checkout", "-q", "--detach", cwd=self.lane)
        text = self.run_cmd("risk")[1]
        self.assertIn("the changes cannot be told: HEAD is detached", text)
        self.assertTrue(json.loads(self.run_cmd("risk", "--json")[1])["needs_the_user"])

    def test_a_failed_project_lookup_needs_the_user(self):
        (self.home / "PROJECT.md").write_text("## High-risk paths\n- auth-payments: app/auth/\n")
        self.commit(self.lane, "app/auth/x.py", "", "auth")
        self.assertTrue(json.loads(self.run_cmd("risk", "--json")[1])["needs_the_user"])
        real = fleet.git_out

        def failing(path, *args, **kw):
            return None if args[:2] == ("rev-parse", "--git-common-dir") else real(path, *args, **kw)
        with mock.patch.object(fleet, "git_out", failing):
            out = json.loads(self.run_cmd("risk", "--json")[1])
            self.assertEqual((out["needs_the_user"], out["matches"]), (True, []))
            self.assertIn("git cannot tell the project of", out["error"])
            self.refused("--by", "user", reason="git cannot tell the project of")

    def test_a_path_that_is_not_utf8_is_never_covered_by_risk_md(self):
        (self.home / "PROJECT.md").write_text("## High-risk paths\n- auth-payments: app/auth/\n")

        def commit_bytes(name: bytes):
            blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.lane, input=b"x\n",
                                  capture_output=True, check=True).stdout.strip().decode()
            subprocess.run(["git", "update-index", "--add", "--cacheinfo", f"100644,{blob}".encode() + b"," + name],
                           cwd=self.lane, check=True, capture_output=True)
            git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "bytes", cwd=self.lane)
        commit_bytes(b"app/auth/\xff.py")
        [match] = self.state()["needs_user"]
        self.assertEqual(match["path"], "app/auth/\ufffd.py")
        out = self.refused("--by", "user", reason="bytes that are not UTF-8 cannot be told exactly")
        self.assertIn("rename the file", out["reason"])
        # Even a hand-written line for the decoded name covers nothing, so a second name that decodes alike
        # still needs the user.
        (self.fleet_dir / "RISK.md").write_text("RISK APPROVED: auth-payments app/auth/\ufffd.py by user x\n")
        commit_bytes(b"app/auth/\xfe.py")
        self.assertEqual([m["path"] for m in self.state()["needs_user"]], ["app/auth/\ufffd.py"])
        self.assertTrue(fleet.needs_user(self.state()))
        # A plan that the user approved still covers the category, whatever the exact name.
        (self.fleet_dir / "PLAN.md").write_text("Risk: auth-payments\nPLAN READY: 1\nPLAN APPROVED: 1 by user x\n")
        self.assertFalse(fleet.needs_user(self.state()))

    def test_approve_risk_needs_head_before_it_writes(self):
        self.commit(self.lane, "db/migrate/001.sql", "create\n", "a migration")
        real = fleet.git_text

        def failing(path, *args, **kw):
            return None if args == ("rev-parse", "--verify", "--quiet", "HEAD") else real(path, *args, **kw)
        with mock.patch.object(fleet, "git_text", failing):
            self.refused("--by", "user", reason="git cannot resolve HEAD")
        self.assertFalse((self.fleet_dir / "RISK.md").exists())
        self.assertEqual(self.run_cmd("approve-risk", "--by", "user")[0], 0)

    def test_approve_risk(self):
        self.refused("--by", "user", reason="not in a git worktree", cwd=self.root)
        self.refused("--by", "user", reason="nothing needs the user: 0 high-risk matches are covered")
        self.commit(self.lane, "db/migrate/001.sql", "create\n", "a migration")
        (self.lane / "db" / "migrations").mkdir(parents=True)
        (self.lane / "db" / "migrations" / "x.py").write_text("")
        git("checkout", "-q", "--detach", cwd=self.lane)
        self.refused("--by", "user", reason="cannot be told (HEAD is detached")
        git("checkout", "-q", "feat/lane", cwd=self.lane)
        code, text = self.run_cmd("approve-risk", "--by", "user")
        out = json.loads(text)
        self.assertEqual((code, out["ok"], out["approved"]),
                         (0, True, [{"category": "migration", "path": "db/migrate/001.sql"},
                                    {"category": "migration", "path": "db/migrations/x.py"}]))
        lines = (self.fleet_dir / "RISK.md").read_text().splitlines()
        self.assertEqual(lines[0], "# Risk approvals: feat/lane")
        self.assertRegex(lines[2], r"^RISK APPROVED: migration db/migrate/001\.sql by user \d{4}-\d\d-\d\dT")
        self.assertFalse(fleet.needs_user(self.state()))
        [event] = [e for e in fleet.read_events([self.home]) if e["event"] == "risk_approved"]
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.lane, capture_output=True, text=True).stdout.strip()
        self.assertEqual((event["head"], event["branch"], event["lane"], event["matches"]),
                         (head, "feat/lane", str(self.lane), out["approved"]))
        self.assertEqual(fleet.describe_event(event), "risky diff approved by user: migration db/migrate/001.sql, "
                                                      "migration db/migrations/x.py")
        self.assertEqual(fleet.clean_event(event)["matches"], out["approved"])
        self.refused("--by", "user", reason="nothing needs the user: 2 high-risk matches are covered")
        # A later risky path gets its own line; the old lines stay.
        with (self.fleet_dir / "RISK.md").open("a") as fh:
            fh.write("a note without a newline")
        self.commit(self.lane, "alembic/env.py", "", "alembic")
        self.assertEqual(self.run_cmd("approve-risk", "--by", "user")[0], 0)
        lines = (self.fleet_dir / "RISK.md").read_text().splitlines()
        self.assertEqual((len(lines), lines[4], lines[5][:40]),
                         (6, "a note without a newline", "RISK APPROVED: migration alembic/env.py "))
        # A path with a control character cannot be written as one line.
        (self.lane / "db" / "migrate" / "a\nb.sql").write_text("")
        self.refused("--by", "user", reason="with a control character")
        with self.assertRaises(SystemExit), mock.patch.object(sys, "stderr", io.StringIO()):
            fleet.main(["approve-risk", "--by", "orch"])
        self.assertEqual(fleet.status_paths(str(self.lane)).count(".fleet/RISK.md"), 0)  # .fleet/ stays out of git


class DocsTests(FleetTestCase):
    """The templates and skills that the plan and research flow relies on match the code."""

    PLUGIN = SCRIPT.parent.parent

    def template(self, heading):
        text = (self.PLUGIN / "skills" / "fleet" / "references" / "templates.md").read_text()
        part = text.split(f"\n## {heading}", 1)[1]
        return part.split("```markdown\n", 1)[1].split("\n```", 1)[0]

    def test_a_copied_template_ends_no_watch(self):
        plan = self.template("PLAN.md")
        self.assertEqual(fleet.plan_rounds(plan), ([], set()))
        self.assertEqual(fleet.plan_risk(plan), ("none | migration | auth-payments | deletion | hotfix", False))
        self.assertEqual(fleet.plan_risk(plan.replace("none | migration | auth-payments | deletion | hotfix",
                                                      "none")), ("none", True))
        self.assertEqual((fleet.plan_files(plan), fleet.plan_files(plan, every_line=True)), ([], []))
        self.assertEqual(fleet.plan_categories(plan, fleet.risk_patterns(None)[0]), set())
        with tempfile.TemporaryDirectory() as tmp:  # the PROJECT.md template adds no pattern and no error
            (Path(tmp) / "PROJECT.md").write_text(self.template("PROJECT.md"))
            self.assertEqual(fleet.risk_patterns(Path(tmp))[1], None)
            self.assertEqual(len(fleet.risk_patterns(Path(tmp))[0]), len(fleet.BUILTIN_RISK_PATTERNS))
        self.assertIn("## High-risk paths", self.template("PROJECT.md"))
        self.assertIn("High-risk paths:", self.template("REVIEW-REQUEST"))
        self.assertIsNone(fleet.RISK_APPROVED_LINE.search(self.template("BRIEF.md")))
        research = self.template("RESEARCH")
        self.assertIn("RESEARCH DONE", research)  # the researcher learns the marker from the file
        self.assertIsNone(fleet.RESEARCH_DONE_LINE.search(research))
        self.assertIsNone(fleet.READY_LINE.search(self.template("BRIEF.md")))

    def test_skills_and_defaults_name_the_new_parts(self):
        skill = (self.PLUGIN / "skills" / "research" / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: research\n"))
        self.assertIn("--role researcher", skill)
        self.assertIn("--task", skill)
        self.assertIn("## Planning and research", (self.PLUGIN / "defaults" / "conventions.md").read_text())
        lane = (self.PLUGIN / "skills" / "lane" / "SKILL.md").read_text()
        for text in ("--role planner", "approve-plan <cwd> --by orch", "PLAN READY: 1",
                     "Read .fleet/BRIEF.md and .fleet/PLAN.md and execute them."):
            self.assertIn(text, lane)
        for role in fleet.ROLES:
            self.assertIn(role, (self.PLUGIN / "skills" / "fleet" / "SKILL.md").read_text())
        manifest = json.loads((self.PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["version"], fleet.VERSION)

    def test_docs_name_the_risk_gates(self):
        skill = (self.PLUGIN / "skills" / "fleet" / "SKILL.md").read_text()
        templates = (self.PLUGIN / "skills" / "fleet" / "references" / "templates.md").read_text()
        readme = (self.PLUGIN.parent.parent / "README.md").read_text()
        for text in ("`risk [DIR]", "`approve-risk [DIR] --by user", "## High-risk paths", "RISK.md"):
            self.assertIn(text, skill)
        for doc in (templates, readme):
            for text in ("## High-risk paths", "RISK.md", "approve-risk", "RISK APPROVED", "**/migrate/**"):
                self.assertIn(text, doc)
        for _, pattern in fleet.BUILTIN_RISK_PATTERNS:
            self.assertIn(f"`{pattern}`", templates)
            self.assertIn(f"`{pattern}`", readme)
        self.assertIn("risk_approved", readme)
        self.assertIn("fleet risk", (self.PLUGIN / "skills" / "review" / "SKILL.md").read_text())
        lane = (self.PLUGIN / "skills" / "lane" / "SKILL.md").read_text()
        planner = lane.split('--prompt "Read .fleet/BRIEF.md and write the plan', 1)[1].split('"', 1)[0]
        self.assertIn("list every file", planner)
        self.assertLessEqual(len("Read .fleet/BRIEF.md and write the plan" + planner), fleet.PROMPT_MAX)
        self.assertIn('changed: "nothing"', lane)
        self.assertIn("high-risk pattern", (self.PLUGIN / "defaults" / "conventions.md").read_text())


    def test_docs_name_the_follow_up_commands(self):
        skills = self.PLUGIN / "skills"
        for name in ("fleet", "lane", "review", "cleanup", "context", "handoff"):
            self.assertIn("follow-up", (skills / name / "SKILL.md").read_text(), name)
        lane = (skills / "lane" / "SKILL.md").read_text()
        self.assertIn("--from <ID>", lane.split("\n---\n", 1)[0])  # the argument hint
        for text in ("follow-up list --json", "follow-up take <ID> --lane <branch>", "held_by"):
            self.assertIn(text, lane)
        self.assertIn('follow-up add "<the finding in one line>" --source "<branch> REVIEW-<n>.md"',
                      (skills / "review" / "SKILL.md").read_text())
        self.assertIn("follow_ups_closed", (skills / "cleanup" / "SKILL.md").read_text())
        templates = (skills / "fleet" / "references" / "templates.md").read_text()
        readme = (self.PLUGIN.parent.parent / "README.md").read_text()
        for doc in (templates, readme):
            self.assertIn("[F1]" if doc is templates else "[F7]", doc)
            self.assertIn("(lane: ", doc)
        for kind in fleet.FOLLOW_UP_EVENTS:
            self.assertIn(f"`{kind}`", readme)
        self.assertIn("/fleet:lane --from", readme)
        # One lane holds a follow-up: the skill offers no second owner, and goes on only after a take that worked.
        self.assertNotIn("second lane", lane)
        self.assertIn("open the lane without `--from`", lane)
        self.assertIn("Go on only when it prints `ok: true`", lane)
        # The close rule as it is: a merged pull request, or a merged lane whose branch moved; reflog expiry releases.
        for doc in (templates, readme, lane, (skills / "cleanup" / "SKILL.md").read_text(),
                    (skills / "fleet" / "SKILL.md").read_text()):
            self.assertNotIn("since the take", doc)
            self.assertNotIn("since it took", doc)
            self.assertIn("reflog expiry", doc)
            self.assertIn("rebase", doc)
        for doc in (templates, readme):  # uniqueness from the counter; the history is best effort
            self.assertIn("follow-ups.json", doc)
            self.assertIn("best effort", doc)
        # The template's Follow-ups section is empty, and its example parses as the helper writes it.
        doc = fleet.parse_follow_ups(self.template("PROJECT.md"))
        self.assertEqual((doc["items"], len(doc["placeholders"]), doc["other_lines"]), ([], 1, 0))
        example = templates.split("`## Follow-ups` holds one list item", 1)[1].split("```markdown\n", 1)[1]
        items = fleet.parse_follow_ups(example.split("\n```", 1)[0])["items"]
        self.assertEqual([(i["id"], i["lane"]) for i in items], [("F1", None), ("F2", "fix/upload-retry")])


class OrchTests(FleetTestCase):
    """orch, board, digest and the orch re-brief, in a temporary FLEET_HOME. herdr and gh never run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.home = self.root / "home"
        self.patch = mock.patch.dict(os.environ, {"FLEET_HOME": str(self.home)})
        self.patch.start()
        self.repo = self.root / "app"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("remote", "add", "origin", "git@github.com:acme/app.git", cwd=self.repo)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=self.repo)
        self.project = fleet.project_for(str(self.repo))
        self.clock = use_clock(self)
        self.alive, self.unknown = set(), set()
        self.alive_patch = mock.patch.object(fleet, "pane_state", side_effect=lambda p: (
            "alive" if p in self.alive else "unknown" if p in self.unknown else "dead"))
        self.alive_patch.start()

    def tearDown(self):
        self.alive_patch.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def env(self, pane=None, **extra):
        """os.environ for a caller in `pane` (None: outside Herdr), without this machine's Herdr and Claude ids."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("HERDR_") and k not in ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE")}
        if pane:
            env.update(HERDR_PANE_ID=pane, HERDR_ENV="1", HERDR_WORKSPACE_ID=pane.split(":")[0])
        env.update(extra)
        return mock.patch.dict(os.environ, env, clear=True)

    def main(self, *argv, pane=None, **extra):
        buf = io.StringIO()
        with self.env(pane, **extra), chdir(self.repo), redirect_stdout(buf):
            code = fleet.main(list(argv))
        return code, buf.getvalue()

    def events(self, kind=None):
        return [e for e in fleet.read_events([self.project]) if kind is None or e["event"] == kind]

    def add_lane(self, name, **files):
        path = self.root / name
        git("worktree", "add", "-q", "-b", f"feat/{name}", str(path), cwd=self.repo)
        d = path / ".fleet"
        d.mkdir()
        for fname, text in files.items():
            (d / fname.replace("_", "-").replace("-md", ".md")).write_text(text)
        return path

    def facts(self, path, label=None, panes=(), pr=None, branch=None):
        return {"path": str(path), "label": label, "workspace": "w2", "branch": branch or f"feat/{Path(path).name}",
                "panes": [dict(p) for p in panes], "pr": pr, "exists": True, "git": True, "linked": True}

    # orch -------------------------------------------------------------

    def test_claim_refuse_replace_and_release(self):
        code, out = self.main("orch", "claim", pane="w1:p1", CLAUDE_CODE_SESSION_ID="sess-orch-1")
        res = json.loads(out)
        self.assertEqual((code, res["ok"], res["previous"], res["project_md_created"]), (0, True, None, True))
        rec = fleet.read_orchs(self.project)["default"]
        self.assertEqual((rec["pane"], rec["workspace"], rec["session"], rec["checkout"]),
                         ("w1:p1", "w1", "sess-orch-1", str(self.repo)))
        self.assertIn("claimed_at", rec)
        self.assertNotIn("cursor", rec)
        self.assertEqual(res["project_md"], str(self.project / "PROJECT.md"))
        self.assertEqual(res["board"], str(self.project / "board.json"))
        self.assertIn("# Project: app", (self.project / "PROJECT.md").read_text())
        (self.project / "PROJECT.md").write_text("# mine\n")
        # Another pane cannot take a live orch, with or without --replace.
        self.alive.add("w1:p1")
        for argv in (("orch", "claim"), ("orch", "claim", "--replace"), ("orch", "claim", "--pane", "w2:p1")):
            code, out = self.main(*argv, pane="w2:p1")
            self.assertEqual((code, json.loads(out)["ok"]), (2, False), argv)
            self.assertEqual(fleet.read_orchs(self.project)["default"]["pane"], "w1:p1")
        self.assertIn("--replace works only from that pane",
                      json.loads(self.main("orch", "claim", "--replace", pane="w2:p1")[1])["refused"])
        # The registered pane hands over to a successor; the session is not the caller's.
        with mock.patch.object(fleet, "pane_info", return_value={"workspace_id": "w3"}):
            code, out = self.main("orch", "claim", "--pane", "w3:p1", "--replace", pane="w1:p1",
                                  CLAUDE_CODE_SESSION_ID="sess-orch-1")
        self.assertEqual((code, json.loads(out)["previous"]), (0, "w1:p1"))
        rec = fleet.read_orchs(self.project)["default"]
        self.assertEqual((rec["pane"], rec["workspace"], rec.get("session")), ("w3:p1", "w3", None))
        self.assertEqual((self.project / "PROJECT.md").read_text(), "# mine\n")  # never overwritten
        # A dead registered pane never blocks.
        self.assertEqual(self.main("orch", "claim", pane="w4:p1")[0], 0)
        claims = self.events("orch_claim")
        self.assertEqual([(e.get("old"), e["pane"], e.get("replace")) for e in claims],
                         [(None, "w1:p1", None), ("w1:p1", "w3:p1", True), ("w3:p1", "w4:p1", None)])
        self.assertEqual(claims[1]["by"]["pane"], "w1:p1")
        # Release: only the registered pane, or --force.
        code, out = self.main("orch", "release", pane="w2:p1")
        self.assertEqual((code, json.loads(out)["ok"]), (2, False))
        self.assertEqual(self.main("orch", "release", pane="w4:p1")[0], 0)
        self.assertFalse((self.project / "orch.json").exists())
        self.main("orch", "claim", pane="w4:p1")
        self.assertEqual(self.main("orch", "release", "--force")[0], 0)
        self.assertEqual([(e["pane"], e.get("force")) for e in self.events("orch_release")],
                         [("w4:p1", None), ("w4:p1", True)])
        self.assertIn("no orch", json.loads(self.main("orch", "release", pane="w4:p1")[1])["note"])
        # The new events read well in the log.
        text = self.main("log")[1]
        self.assertIn("orch default pane w1:p1 -> w3:p1 (replace)", text)
        self.assertIn("released orch default pane w4:p1 (force)", text)
        self.assertEqual({e["orch"] for e in self.events() if e["event"].startswith("orch_")}, {"default"})

    # several orchs ------------------------------------------------------

    def legacy_bytes(self, rec):
        """orch.json as 0.8.3 writes it: one record, write_json's format, no `format` key."""
        return (json.dumps(rec, indent=2, ensure_ascii=False) + "\n").encode()

    def digest_and_board(self, pane, *argv):
        with mock.patch.object(fleet, "collect_lanes", return_value=([], [], [])), \
                mock.patch.object(fleet.shutil, "which", return_value="/usr/bin/herdr"):
            self.assertEqual(self.main("digest", *argv, pane=pane)[0], 0)
            self.assertEqual(self.main("board", *argv, pane=pane)[0], 0)

    def test_a_single_orch_keeps_the_legacy_files(self):
        """A project with one orch, `default`, keeps 0.8.3's orch.json, digest.json and board.json: the legacy shape
        and the root files, after a claim, a re-claim, a consuming digest and a board; no orchs/ folder."""
        orch = self.project / "orch.json"
        self.main("orch", "claim", pane="w1:p1", CLAUDE_CODE_SESSION_ID="sess-1")
        rec = json.loads(orch.read_text())
        self.assertEqual(set(rec), {"pane", "workspace", "checkout", "session", "claimed_at"})
        self.assertEqual(orch.read_bytes(), self.legacy_bytes(rec))
        self.assertEqual(fleet.read_orchs(self.project), {"default": rec})
        self.main("orch", "claim", pane="w1:p1", CLAUDE_CODE_SESSION_ID="sess-2")  # a re-claim
        rec = json.loads(orch.read_text())
        self.assertEqual((rec["session"], orch.read_bytes()), ("sess-2", self.legacy_bytes(rec)))
        self.digest_and_board("w1:p1")
        self.assertEqual(orch.read_bytes(), self.legacy_bytes(rec))
        for name in ("digest.json", "board.json"):
            self.assertTrue((self.project / name).is_file(), name)
        self.assertFalse((self.project / "orchs").exists())
        self.assertEqual(fleet.orch_files(self.project, "default")["digest"], self.project / "digest.json")
        self.assertIsNone(fleet.orch_files(self.project, "default")["orch_md"])
        # A legacy file written by 0.8.3 reads as `default`, and `show` names it.
        self.alive.add("w1:p1")
        self.assertIn("orch        default: pane w1:p1 (this pane), alive", self.main("orch", "show", pane="w1:p1")[1])
        # A release of the only orch removes the file, as before.
        self.assertEqual(self.main("orch", "release", pane="w1:p1")[0], 0)
        self.assertFalse(orch.exists())

    def test_the_switch_to_format_2(self):
        """The claim of a second name switches orch.json to format 2 once: the root digest.json and board.json move
        into orchs/default/, and each orch gets its ORCH.md. A release never switches back."""
        self.main("orch", "claim", pane="w1:p1", CLAUDE_CODE_SESSION_ID="sess-1")
        self.digest_and_board("w1:p1")
        digest, board = ((self.project / n).read_bytes() for n in ("digest.json", "board.json"))
        self.alive.add("w1:p1")
        code, out = self.main("orch", "claim", "--name", "billing", pane="w2:p1", CLAUDE_CODE_SESSION_ID="sess-2")
        res = json.loads(out)
        self.assertEqual((code, res["name"], res["format_switched"], res["previous"]), (0, "billing", True, None))
        self.assertEqual(res["board"], str(self.project / "orchs" / "billing" / "board.json"))
        self.assertEqual(res["orch_md"], str(self.project / "orchs" / "billing" / "ORCH.md"))
        data = fleet.read_json(self.project / "orch.json")
        self.assertEqual((data["format"], sorted(data["orchs"])), (2, ["billing", "default"]))
        self.assertEqual((data["orchs"]["billing"]["pane"], data["orchs"]["default"]["session"]), ("w2:p1", "sess-1"))
        self.assertFalse((self.project / "digest.json").exists())
        self.assertFalse((self.project / "board.json").exists())
        self.assertEqual((self.project / "orchs" / "default" / "digest.json").read_bytes(), digest)
        self.assertEqual((self.project / "orchs" / "default" / "board.json").read_bytes(), board)
        for name in ("default", "billing"):
            md = (self.project / "orchs" / name / "ORCH.md").read_text()
            self.assertTrue(md.startswith(f"# Orch: {name} (project app)\n"), md)
            self.assertEqual(fleet.section_text(md, "Decisions") + fleet.section_text(md, "Notes"), [])
        self.assertFalse((self.project / "orchs" / "billing" / "digest.json").exists())  # a new orch starts fresh
        self.assertEqual(self.events("orch_claim")[-1].get("switched"), True)
        self.assertIn("orch billing pane - -> w2:p1; orch.json now holds several orchs (format 2)", self.main("log")[1])
        # With two orchs, a bare claim of a third pane refuses and lists them; --name NEW opens a theme.
        self.alive.add("w2:p1")
        code, out = self.main("orch", "claim", pane="w3:p1")
        res = json.loads(out)
        self.assertEqual((code, res["orchs"]), (2, {"billing": "w2:p1", "default": "w1:p1"}))
        self.assertIn("has 2 orchs (billing, default); `fleet orch claim --name NEW` opens a theme", res["refused"])
        # A pane that holds one orch cannot claim another name in the same project.
        code, out = self.main("orch", "claim", "--name", "docs", pane="w2:p1")
        self.assertEqual(code, 2)
        self.assertIn("pane w2:p1 is already orch billing of project app", json.loads(out)["refused"])
        # A live orch of the name refuses another pane, as one orch did before.
        code, out = self.main("orch", "claim", "--name", "billing", pane="w3:p1")
        self.assertIn("pane w2:p1 is orch billing of project app and is alive", json.loads(out)["refused"])
        # A re-claim from the holder refreshes the session and the checkout; the other record stays.
        lane = self.add_lane("x")
        with chdir(lane):
            with self.env("w2:p1", CLAUDE_CODE_SESSION_ID="sess-3"), redirect_stdout(io.StringIO()):
                self.assertEqual(fleet.main(["orch", "claim"]), 0)
        orchs = fleet.read_orchs(self.project)
        self.assertEqual((orchs["billing"]["session"], orchs["billing"]["checkout"]), ("sess-3", str(lane)))
        self.assertEqual(orchs["default"]["session"], "sess-1")
        # --replace from the holder hands the name to the new pane and clears the session.
        with mock.patch.object(fleet, "pane_info", return_value={"workspace_id": "w4"}):
            code, out = self.main("orch", "claim", "--pane", "w4:p1", "--replace", pane="w2:p1",
                                  CLAUDE_CODE_SESSION_ID="sess-3")
        self.assertEqual((code, json.loads(out)["name"]), (0, "billing"))
        rec = fleet.read_orchs(self.project)["billing"]
        self.assertEqual((rec["pane"], rec.get("session")), ("w4:p1", None))
        # A release never switches back: one orch left, then none, and the file stays in format 2.
        self.assertEqual(self.main("orch", "release", pane="w4:p1")[0], 0)
        self.assertEqual(fleet.read_json(self.project / "orch.json")["format"], 2)
        self.assertEqual(sorted(fleet.read_orchs(self.project)), ["default"])
        self.assertEqual(self.main("orch", "release", pane="w1:p1")[0], 0)
        self.assertEqual(fleet.read_json(self.project / "orch.json"), {"format": 2, "orchs": {}})
        self.assertTrue((self.project / "orchs" / "billing" / "ORCH.md").exists())  # its history stays
        # A claim of `default` again keeps format 2 and the folder's files.
        self.main("orch", "claim", pane="w1:p1")
        self.assertEqual(fleet.orch_files(self.project, "default")["digest"],
                         self.project / "orchs" / "default" / "digest.json")
        self.assertTrue(fleet.orch_files(self.project, "default")["digest"].exists())

    def test_a_first_claim_of_another_name_starts_in_format_2(self):
        """A project with no orch that claims a name other than `default` starts in format 2; its root files (a
        digest made with no orch) become that orch's."""
        self.digest_and_board(None)
        digest = (self.project / "digest.json").read_bytes()
        code, out = self.main("orch", "claim", "--name", "billing", pane="w1:p1")
        self.assertEqual((code, json.loads(out)["format_switched"]), (0, True))
        self.assertEqual(sorted(fleet.read_json(self.project / "orch.json")["orchs"]), ["billing"])
        self.assertEqual((self.project / "orchs" / "billing" / "digest.json").read_bytes(), digest)
        self.assertTrue((self.project / "orchs" / "billing" / "ORCH.md").exists())
        self.assertFalse((self.project / "orchs" / "default").exists())

    def test_orch_names_and_unknown_formats(self):
        for bad in ("-x", ".x", "a/b", "x" * 41, "", "a b", "é"):
            with self.assertRaises(fleet.FleetError, msg=bad):
                fleet.check_orch_name(bad)
        self.assertEqual(fleet.check_orch_name("x" * 40), "x" * 40)
        with mock.patch.object(sys, "stderr", io.StringIO()) as err:
            self.assertEqual(self.main("orch", "claim", "--name", "a/b", pane="w1:p1")[0], 1)
        self.assertIn("not an orch name: 'a/b'", err.getvalue())
        self.assertFalse((self.project / "orch.json").exists())
        # A format that this helper does not know: no writer replaces the file.
        fleet.write_json(self.project / "orch.json", {"format": 3, "orchs": {}})
        with mock.patch.object(sys, "stderr", io.StringIO()) as err:
            self.assertEqual(self.main("orch", "claim", pane="w1:p1")[0], 1)
        self.assertIn("has format 3, which this helper does not know", err.getvalue())
        self.assertEqual(fleet.read_json(self.project / "orch.json"), {"format": 3, "orchs": {}})
        # A format 2 record with a bad name, or that is no object, is skipped.
        fleet.write_json(self.project / "orch.json", {"format": 2, "orchs": {"../x": {"pane": "w1:p1"},
                                                                             "ok": "w2:p1", "good": {"pane": "w3:p1"}}})
        self.assertEqual(fleet.read_orchs(self.project), {"good": {"pane": "w3:p1"}})

    def test_show_list_and_release_with_several_orchs(self):
        self.main("orch", "claim", pane="w1:p1", CLAUDE_CODE_SESSION_ID="aaaaaaaa-1")
        self.main("orch", "claim", "--name", "billing", pane="w2:p1")
        self.alive.add("w1:p1")
        self.unknown.add("w2:p1")
        data = json.loads(self.main("orch", "show", "--json", pane="w2:p1")[1])
        self.assertEqual((data["name"], data["orch"]["pane"], data["alive"], data["self"], data["format"]),
                         ("billing", "w2:p1", None, True, 2))
        self.assertEqual(data["orchs"], [{"name": "billing", "pane": "w2:p1", "alive": None, "self": True},
                                         {"name": "default", "pane": "w1:p1", "alive": True, "self": False}])
        text = self.main("orch", "show", pane="w2:p1")[1]
        self.assertIn("orch        billing: pane w2:p1 (this pane), alive or not: herdr cannot tell", text)
        self.assertIn("other orchs default: pane w1:p1, alive", text)
        self.assertIn(f"ORCH.md     {self.project / 'orchs' / 'billing' / 'ORCH.md'}", text)
        # A caller that is neither orch: show names none and lists both; --orch picks one.
        text = self.main("orch", "show", pane="w9:p9")[1]
        self.assertIn("orch        none of the 2 orchs is this pane's: pass --orch NAME", text)
        self.assertIn("other orchs billing: pane w2:p1", text)
        self.assertEqual(json.loads(self.main("orch", "show", "--orch", "default", "--json")[1])["name"], "default")
        with mock.patch.object(sys, "stderr", io.StringIO()) as err:
            self.assertEqual(self.main("orch", "show", "--orch", "nope")[0], 1)
        self.assertIn("no orch 'nope' in project app; its orchs: billing, default", err.getvalue())
        # list: every orch, its pane's state and its cached board.
        fleet.save_board(self.project, [{"lane": "a", "phase": "idle"}, {"lane": "b", "phase": "removed"}],
                         fleet.precise_now(), fleet.orch_files(self.project, "default")["board"])
        rows = json.loads(self.main("orch", "list", "--json")[1])["orchs"]
        self.assertEqual([(r["name"], r["pane"], r["state"], r["lanes"]) for r in rows],
                         [("billing", "w2:p1", "unknown", None), ("default", "w1:p1", "alive", 1)])
        self.assertEqual(rows[1]["session"], "aaaaaaaa-1")
        text = self.main("orch", "list", pane="w1:p1")[1]
        self.assertIn("project app: 2 orchs", text)
        self.assertRegex(text, r"default\s+w1:p1 \(this pane\)\s+alive\s+aaaaaaaa\s+\S+\s+\S+ \S+\s+1 lane, \d+s old")
        self.assertRegex(text, r"billing\s+w2:p1\s+unknown\s+-\s+.*none yet")
        # release: the caller's own; another one only with --force; a caller with none names it.
        code, out = self.main("orch", "release", pane="w9:p9")
        self.assertEqual(code, 2)
        self.assertIn("name the orch: --orch NAME", json.loads(out)["refused"])
        code, out = self.main("orch", "release", "--orch", "default", pane="w2:p1")
        self.assertIn("pane w1:p1 is orch default of project app, not this pane; --force", json.loads(out)["refused"])
        self.assertEqual(self.main("orch", "release", "--orch", "default", "--force", pane="w2:p1")[0], 0)
        self.assertEqual(sorted(fleet.read_orchs(self.project)), ["billing"])
        self.assertEqual(self.events("orch_release")[-1]["orch"], "default")
        self.assertIn("released orch default pane w1:p1 (force)", self.main("log")[1])
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(self.main("orch", "release", "a", "b")[0], 1)  # names belong to rename only

    def test_rename(self):
        """rename moves the record, the orch's folder (a legacy file switches to format 2 first, its root files into
        the new folder) and the lane.json of each lane of the project that names the old name."""
        self.main("orch", "claim", pane="w1:p1")
        self.digest_and_board("w1:p1")
        digest = (self.project / "digest.json").read_bytes()
        mine, theirs, unowned = self.add_lane("a"), self.add_lane("b"), self.add_lane("c")
        fleet.write_json(mine / ".fleet" / "lane.json", {"orch": "default", "project": "app", "branch": "feat/a"})
        fleet.write_json(theirs / ".fleet" / "lane.json", {"orch": "docs", "project": "app"})
        stubs = [{"workspace": f"w{i}", "label": None, "path": str(p), "panes": []}
                 for i, p in enumerate((mine, theirs, unowned), start=5)]

        def rename(*argv, pane="w1:p1"):
            with mock.patch.object(fleet, "enumerate_lanes", return_value=([dict(s) for s in stubs], [])), \
                    mock.patch.object(fleet.shutil, "which", return_value="/usr/bin/herdr"):
                code, out = self.main("orch", "rename", *argv, pane=pane)
            return code, (json.loads(out) if out.strip() else None)
        code, res = rename("default", "billing")
        self.assertEqual((code, res["name"], res["old"], res["format_switched"], res["lanes"]),
                         (0, "billing", "default", True, [str(mine)]))
        self.assertEqual(sorted(fleet.read_json(self.project / "orch.json")["orchs"]), ["billing"])
        self.assertEqual((self.project / "orchs" / "billing" / "digest.json").read_bytes(), digest)
        self.assertFalse((self.project / "digest.json").exists())
        self.assertTrue((self.project / "orchs" / "billing" / "ORCH.md").exists())
        self.assertEqual(fleet.read_json(mine / ".fleet" / "lane.json"),
                         {"orch": "billing", "project": "app", "branch": "feat/a"})
        self.assertEqual(fleet.read_json(theirs / ".fleet" / "lane.json")["orch"], "docs")
        self.assertFalse((unowned / ".fleet" / "lane.json").exists())
        self.assertIn("renamed orch default to billing; 1 lane moved with it; orch.json now holds several orchs",
                      self.main("log")[1])
        # In format 2: the folder moves.
        code, res = rename("billing", "money")
        self.assertEqual((code, res["format_switched"]), (0, False))
        self.assertTrue((self.project / "orchs" / "money" / "digest.json").exists())
        self.assertFalse((self.project / "orchs" / "billing").exists())
        self.assertEqual(fleet.read_json(mine / ".fleet" / "lane.json")["orch"], "money")
        # Refusals change nothing: an unknown or existing name, a folder left by a released orch, a bad name.
        self.main("orch", "claim", "--name", "docs", pane="w2:p1")
        before = (self.project / "orch.json").read_bytes()
        self.assertIn("no orch 'nope'", rename("nope", "x")[1]["refused"])
        self.assertIn("already has an orch 'docs'", rename("money", "docs")[1]["refused"])
        (self.project / "orchs" / "old").mkdir()
        self.assertIn("orchs/old exists", rename("money", "old")[1]["refused"])
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(rename("money", "a/b")[0], 1)
            self.assertEqual(rename("money")[0], 1)
        self.assertEqual((self.project / "orch.json").read_bytes(), before)

    def test_claim_needs_a_pane_and_a_project(self):
        with self.assertRaises(fleet.FleetError):
            with self.env(None), chdir(self.repo):
                fleet.cmd_orch(fleet.build_parser().parse_args(["orch", "claim"]))
        with self.env("w1:p1"), chdir(self.root), mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(fleet.main(["orch", "claim"]), 1)
            self.assertEqual(fleet.main(["orch", "show", "--project", "nope"]), 1)

    def test_show(self):
        code, out = self.main("orch")
        self.assertEqual(code, 0)
        self.assertIn("none registered", out)
        self.main("orch", "claim", pane="w1:p1")
        self.alive.add("w1:p1")
        data = json.loads(self.main("orch", "show", "--json", pane="w1:p1")[1])
        self.assertEqual((data["project"], data["orch"]["pane"], data["alive"], data["self"], data["board_made"]),
                         ("app", "w1:p1", True, True, None))
        text = self.main("orch", "show", "--project", "app")[1]
        self.assertIn("pane w1:p1, alive", text)
        self.assertIn("board.json", text)

    def test_claim_warns_about_a_live_fleet_md(self):
        (self.repo / ".fleet").mkdir()
        (self.repo / ".fleet" / "FLEET.md").write_text("# Fleet\n\nOrchestrator: pane w9:p9, account plain.\n")
        lane = self.add_lane("x")
        (lane / ".fleet" / "FLEET.md").write_text("Orchestrator: pane w8:p8\n")
        self.alive.update({"w9:p9"})
        warnings = json.loads(self.main("orch", "claim", pane="w1:p1")[1])["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("w9:p9", warnings[0])
        self.assertIn(str(self.repo / ".fleet" / "FLEET.md"), warnings[0])
        self.alive.update({"w8:p8"})
        self.assertEqual(len(json.loads(self.main("orch", "claim", pane="w1:p1")[1])["warnings"]), 2)
        # The claimer's own pane on the old line is no warning; neither is a dead pane.
        self.alive.clear()
        self.assertEqual(json.loads(self.main("orch", "claim", pane="w9:p9")[1])["warnings"], [])

    # board ------------------------------------------------------------

    def phase(self, files=None, panes=(), pr=None):
        d = self.root / f"lane-{len(list(self.root.glob('lane-*')))}"
        d.mkdir()
        for name, text in (files or {}).items():
            (d / name).write_text(text)
        return fleet.lane_phase({"panes": [dict(p) for p in panes], "pr": pr}, d)

    def test_phase_rules(self):
        working = [{"pane": "w2:p1", "agent": "claude", "status": "working", "role": "impl"}]
        idle_critic = [{"pane": "w2:p2", "agent": "codex", "status": "working", "role": "critic"},
                       {"pane": "w2:p1", "agent": "claude", "status": "done", "role": "impl"}]
        blocked_md = "# Status\n\n## Blocked\n- Which API?\n\n## Next step\nAsk.\n"
        self.assertEqual(fleet.lane_phase({"panes": [], "pr": None}, self.root / "gone", removed=True), "removed")
        self.assertEqual(self.phase(pr={"number": 3, "state": "MERGED"}, files={"STATUS.md": blocked_md}), "merged")
        self.assertEqual(self.phase(pr={"number": 3, "state": "OPEN"}, panes=working), "pr open")
        self.assertEqual(self.phase(pr={"number": 3, "state": "CLOSED"}, panes=working), "working")
        self.assertEqual(self.phase(files={"STATUS.md": blocked_md}, panes=working), "blocked")
        self.assertEqual(self.phase(panes=[{"pane": "w2:p1", "agent": "claude", "status": "blocked"}]), "blocked")
        for empty in ("", "-", "none", "- none", "- None.", "- <question for the orchestrator, if any>"):
            md = f"## Blocked\n{empty}\n\n## Next step\nGo.\n"
            self.assertEqual(self.phase(files={"STATUS.md": md}, panes=working), "working", empty)
        ready1 = "## Log\n- x\n\nREADY FOR REVIEW: round 1\n"
        ready2 = ready1 + "READY FOR REVIEW: round 2\n"
        self.assertEqual(self.phase(files={"STATUS.md": ready1}, panes=working), "ready 1")
        self.assertEqual(self.phase(files={"STATUS.md": ready1, "REVIEW-REQUEST-1.md": "r"}), "review 1")
        self.assertEqual(self.phase(files={"STATUS.md": ready1, "REVIEW-REQUEST-1.md": "r", "REVIEW-1.md": "#1 bug"}),
                         "review 1")
        self.assertEqual(self.phase(files={"STATUS.md": ready1, "REVIEW-REQUEST-1.md": "r",
                                           "REVIEW-1.md": "x\nVERDICT: APPROVE\n"}), "reviewed 1: APPROVE")
        # Two critics: the round waits for both, and CHANGES REQUESTED wins.
        two = {"STATUS.md": ready1, "REVIEW-REQUEST-1.md": "r", "REVIEW-1-codex.md": "**VERDICT: APPROVE**\n",
               "REVIEW-1-claude.md": "still reading"}
        self.assertEqual(self.phase(files=two), "review 1")
        two["REVIEW-1-claude.md"] = "VERDICT: CHANGES REQUESTED\n"
        self.assertEqual(self.phase(files=two), "reviewed 1: CHANGES REQUESTED")
        # The implementer reports the next round: ready 2, then review 2 (REVIEW-REQUEST-10 sorts as 10).
        self.assertEqual(self.phase(files={**two, "STATUS.md": ready2}, panes=idle_critic), "ready 2")
        self.assertEqual(self.phase(files={**two, "STATUS.md": ready2, "REVIEW-REQUEST-2.md": "r"}), "review 2")
        self.assertEqual(self.phase(files={"REVIEW-REQUEST-2.md": "r", "REVIEW-REQUEST-10.md": "r",
                                           "REVIEW-2.md": "VERDICT: APPROVE"}), "review 10")
        self.assertEqual(self.phase(panes=working), "working")
        self.assertEqual(self.phase(panes=idle_critic), "idle")  # the implementer's status, not the critic's
        self.assertEqual(self.phase(panes=[{"pane": "w2:p1", "agent": "claude", "status": "done"}]), "idle")
        self.assertEqual(self.phase(panes=[{"pane": "w2:p1", "agent": None, "status": None}]), "no agent")

    def test_plan_phases(self):
        planner = lambda status: [{"pane": "w2:p1", "agent": "claude", "status": status, "role": "planner"}]
        template = "# Plan: x\n\nRisk: none | migration\n\n## Approach\n\nPLAN READY: <n>\n"
        ready1 = "# Plan: x\nRisk: none\n\nPLAN READY: 1\n"
        approved1 = ready1 + "PLAN APPROVED: 1 by orch 2026-11-03T10:00:00+00:00\n"
        self.assertEqual(self.phase(files={"PLAN.md": template}, panes=planner("working")), "planning")
        self.assertEqual(self.phase(files={"PLAN.md": template}), "planning")
        self.assertEqual(self.phase(files={"PLAN.md": "Then add PLAN READY: 1 and stop.\n"}, panes=planner("idle")),
                         "planning")  # a sentence is no marker
        self.assertEqual(self.phase(files={"PLAN.md": ready1}, panes=planner("working")), "planning")  # revising
        self.assertEqual(self.phase(files={"PLAN.md": ready1}, panes=planner("idle")), "plan ready 1")
        self.assertEqual(self.phase(files={"PLAN.md": ready1}), "plan ready 1")
        self.assertEqual(self.phase(files={"PLAN.md": approved1}), "plan approved 1")
        self.assertEqual(self.phase(files={"PLAN.md": approved1 + "\n- **PLAN READY: 2**\n"}, panes=planner("idle")),
                         "plan ready 2")
        # A critic or a shell pane does not stop the plan rules; blocked still wins.
        others = [{"pane": "w2:p2", "agent": "codex", "status": "working", "role": "critic"},
                  {"pane": "w2:p3", "agent": None, "status": None, "role": None}]
        self.assertEqual(self.phase(files={"PLAN.md": approved1}, panes=others), "plan approved 1")
        self.assertEqual(self.phase(files={"PLAN.md": ready1, "STATUS.md": "## Blocked\n- Which API?\n"}), "blocked")
        # Once an implementer runs, or STATUS.md reports a round, the existing rules decide.
        impl = [{"pane": "w2:p1", "agent": "claude", "status": "working", "role": "impl"}]
        self.assertEqual(self.phase(files={"PLAN.md": approved1}, panes=impl), "working")
        self.assertEqual(self.phase(files={"PLAN.md": approved1},
                                    panes=[{"pane": "w2:p1", "agent": "claude", "status": "idle"}]), "idle")
        self.assertEqual(self.phase(files={"PLAN.md": approved1, "STATUS.md": "READY FOR REVIEW: round 1\n"}),
                         "ready 1")
        # Without PLAN.md, a planner pane is never the implementer.
        self.assertEqual(self.phase(panes=planner("working")), "no agent")

    def test_a_round_number_too_long_breaks_neither_board_nor_digest(self):
        huge = "9" * 5000
        a = self.add_lane("a", PLAN_md=f"Risk: none\nPLAN READY: {huge}\n")
        b = self.add_lane("b", PLAN_md=f"Risk: none\nPLAN READY: 2\nPLAN APPROVED: {huge}\n")
        c = self.add_lane("c", STATUS_md=f"## Log\n- x\n\nREADY FOR REVIEW: round {huge}\n")
        d = self.add_lane("d", STATUS_md="READY FOR REVIEW: round 123456789\n")
        idle = [{"pane": "w2:p1", "agent": "claude", "status": "idle"}]
        lanes = [self.facts(a), self.facts(b), self.facts(c, panes=idle), self.facts(d, panes=idle)]
        want = {"feat/a": "planning", "feat/b": "plan ready 2", "feat/c": "idle", "feat/d": "ready 123456789"}
        for argv in (("board", "--json"), ("digest", "--peek", "--json")):
            code, out = self.board(lanes, *argv)
            self.assertEqual(code, 0, argv)
            self.assertEqual({r["lane"]: r["phase"] for r in json.loads(out)["lanes"]}, want, argv)

    def test_marker_sentences_now_count_as_notes(self):
        """Approved change: a line that mentions a marker inside a sentence is no longer dropped as a marker."""
        md = "## Blocked\n- Should I add READY FOR REVIEW: round 1 before the docs?\n"
        self.assertEqual(self.phase(files={"STATUS.md": md},
                                    panes=[{"pane": "w2:p1", "agent": "claude", "status": "idle", "role": "impl"}]),
                         "blocked")
        self.orch_setup(rules=("- Wait for READY FOR REVIEW: round 1 before review.",))
        text = fleet.session_start_context({"source": "clear", "cwd": str(self.repo)}, env={"HERDR_PANE_ID": "w1:p1"})
        self.assertIn("- Wait for READY FOR REVIEW: round 1 before review.", text)

    def test_a_researcher_is_never_the_implementer(self):
        """Approved change: without PLAN.md, a researcher alone is `no agent`; an implementer without a role still
        decides the phase next to one."""
        researcher = {"pane": "w2:p2", "agent": "claude", "status": "working", "role": "researcher"}
        self.assertEqual(self.phase(panes=[researcher]), "no agent")
        self.assertEqual(self.phase(panes=[researcher, {"pane": "w2:p1", "agent": "claude", "status": "working"}]),
                         "working")
        self.assertEqual(self.phase(panes=[researcher, {"pane": "w2:p1", "agent": "claude", "status": "done"}]),
                         "idle")

    def test_board_plan_warning_and_open_research(self):
        a = self.add_lane("a", PLAN_md="Risk: none\nPLAN READY: 1\n", STATUS_md="## Next step\nWait.\n")
        b = self.add_lane("b", STATUS_md="## Next step\nGo.\n")
        fleet.prep_dir(str(a))
        fleet.save_panes(a / ".fleet", {"w2:p1": {"role": "planner"}})
        (a / "x.py").write_text("")
        (a / "y.py").write_text("")
        (b / "z.py").write_text("")
        lanes = [self.facts(a, panes=[{"pane": "w2:p1", "agent": "claude", "status": "idle"}]),
                 self.facts(b, panes=[{"pane": "w3:p1", "agent": "claude", "status": "working"}])]
        research = self.project / "research"
        research.mkdir()
        (research / "auth-flow.md").write_text("# Research: how does auth work?\n\n## Question\nx\n"
                                                "\nEnd with the line RESEARCH DONE when done.\n")
        (research / "cache.md").write_text("# Research: cache\n\n## Answer\ny\n\nRESEARCH DONE\n")
        (research / "notes.txt").write_text("not research")
        out = json.loads(self.board(lanes, "board", "--json")[1])
        recs = {r["lane"]: r for r in out["lanes"]}
        self.assertEqual(recs["feat/a"]["phase"], "plan ready 1")
        self.assertEqual(recs["feat/a"]["warning"], "2 changed paths outside .fleet/: approve-plan refuses")
        self.assertEqual(recs["feat/b"]["phase"], "working")
        self.assertNotIn("warning", recs["feat/b"])  # only at plan ready
        self.assertEqual(out["research"], [{"file": str(research / "auth-flow.md"),
                                            "title": "Research: how does auth work?"}])
        text = self.board(lanes, "board")[1]
        self.assertRegex(text, r"feat/a\s+plan ready 1\s+-\s+planner:idle\s+-\s+2 changed paths outside")
        self.assertIn("Open research (1, no RESEARCH DONE line yet):", text)
        self.assertIn("auth-flow.md  Research: how does auth work?", text)
        self.assertNotIn("cache.md", text)
        # The note counts the lane's commits too, like approve-plan; a clean lane has no note.
        git("add", "x.py", "y.py", cwd=a)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "code", cwd=a)
        recs = {r["lane"]: r for r in json.loads(self.board(lanes, "board", "--json")[1])["lanes"]}
        self.assertEqual(recs["feat/a"]["warning"], "1 lane commit: approve-plan refuses")
        git("reset", "-q", "--hard", "HEAD~1", cwd=a)
        (research / "auth-flow.md").write_text("# Research\n\n- RESEARCH DONE\n")
        text = self.board(lanes, "board")[1]
        self.assertNotIn("approve-plan refuses", text)
        self.assertNotIn("Open research", text)

    def test_board_risk(self):
        def commit(path, name, message):
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_text("x\n")
            git("add", name, cwd=path)
            git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message, cwd=path)
        (self.project / "PROJECT.md").write_text("# Project: app\n\n## High-risk paths\n- auth-payments: app/auth/\n")
        a = self.add_lane("a", STATUS_md="## Blocked\n- Which API?\n")
        commit(a, "db/migrations/0002.py", "a migration")
        (a / "app" / "auth").mkdir(parents=True)
        (a / "app" / "auth" / "token.py").write_text("")  # uncommitted, from the project's own pattern
        b = self.add_lane("b", STATUS_md="## Next step\nMerge.\n",
                          RISK_md="RISK APPROVED: migration db/migrate/1.sql by user 2026\n")
        commit(b, "db/migrate/1.sql", "covered by RISK.md")
        c = self.add_lane("c", STATUS_md="## Next step\nGo.\n")
        git("checkout", "-q", "--detach", cwd=c)
        d = self.add_lane("d", STATUS_md="## Next step\nGo.\n")
        commit(d, "app.py", "no risky path")
        e = self.add_lane("e", PLAN_md="Risk: none\nPLAN READY: 1\n")
        (e / "db" / "migrate").mkdir(parents=True)
        (e / "db" / "migrate" / "x.sql").write_text("")  # at plan ready: the plan warning, not the diff
        lanes = [self.facts(p, panes=[{"pane": f"w{i}:p1", "agent": "claude", "status": "idle"}])
                 for i, p in enumerate((a, b, c, d), start=2)] + [self.facts(e)]
        lanes[2]["branch"] = None
        recs = {r["path"]: r for r in json.loads(self.board(lanes, "board", "--json")[1])["lanes"]}
        self.assertEqual(recs[str(a)]["risk"], {
            "matches": [{"category": "auth-payments", "path": "app/auth/token.py"},
                        {"category": "migration", "path": "db/migrations/0002.py"}],
            "needs_user": [{"category": "auth-payments", "path": "app/auth/token.py"},
                           {"category": "migration", "path": "db/migrations/0002.py"}]})
        self.assertEqual(recs[str(b)]["risk"], {"matches": [{"category": "migration", "path": "db/migrate/1.sql"}],
                                                "needs_user": []})
        self.assertEqual(recs[str(c)]["risk"]["needs_user"],
                         "cannot be told: HEAD is detached, so the lane's changes cannot be told; pass --base <ref>")
        self.assertIsNone(recs[str(d)]["risk"])
        self.assertEqual((recs[str(e)]["phase"], recs[str(e)]["risk"]), ("plan ready 1", None))
        saved = {r["path"]: r for r in fleet.read_json(self.project / "board.json")["lanes"]}
        self.assertEqual(saved[str(a)]["risk"], recs[str(a)]["risk"])
        lines = {l.split()[0]: l for l in self.board(lanes, "board")[1].splitlines()[2:]}
        # NOTE order: the plan warning, then the risk, then blocked, then next.
        self.assertRegex(lines["feat/a"], r"blocked .* risky diff needs the user \(2\): auth-payments app/auth/to\S*$")
        self.assertTrue(lines["feat/b"].endswith("next: Merge."))
        self.assertIn("risky diff cannot be told: HEAD is detached", lines["c"])  # no branch: the lane is its folder
        self.assertTrue(lines["feat/d"].endswith("next: Go."))
        self.assertTrue(lines["feat/e"].endswith("1 changed path outside .fleet/: approve-plan refuses"))

    def test_board_cannot_tell_a_git_lane_whose_top_lookup_fails(self):
        a = self.add_lane("a", STATUS_md="## Next step\nMerge.\n")
        (a / "db" / "migrate").mkdir(parents=True)
        (a / "db" / "migrate" / "1.sql").write_text("")
        plain = self.root / "plain"
        plain.mkdir()
        lanes = [self.facts(a), {**self.facts(plain), "git": False}]
        real = fleet.git_toplevel
        failing = lambda path: None if os.path.realpath(path) == os.path.realpath(a) else real(path)
        with mock.patch.object(fleet, "git_toplevel", failing):
            recs = {r["path"]: r for r in json.loads(self.board(lanes, "board", "--json")[1])["lanes"]}
            text = self.board(lanes, "board")[1]
            peek = {r["path"]: r for r in json.loads(self.board(lanes, "digest", "--peek", "--json")[1])["lanes"]}
        want = "cannot be told: git cannot find the top of the lane's checkout"
        self.assertEqual((peek[str(a)]["risk"]["needs_user"], peek[str(plain)]["risk"]), (want, None))
        self.assertEqual(recs[str(a)]["risk"], {"matches": [], "needs_user": want})
        self.assertIsNone(recs[str(plain)]["risk"])  # not a git checkout: nothing to check
        self.assertIn("risky diff cannot be told: git cannot find the top", text)
        saved = {r["path"]: r for r in fleet.read_json(self.project / "board.json")["lanes"]}
        self.assertEqual(saved[str(a)]["risk"]["needs_user"], want)

    def test_rebrief_shows_the_risk_note_only_for_a_lane_that_needs_the_user(self):
        self.orch_setup()
        board = fleet.read_json(self.project / "board.json")
        self.stop_clock(board, 5)  # each re-brief says "from 5s ago", however long the test takes
        quiet = dict(board, lanes=[dict(board["lanes"][0], risk=None)])
        covered = dict(board, lanes=[dict(board["lanes"][0], risk={
            "matches": [{"category": "migration", "path": "db/migrate/1.sql"}], "needs_user": []})])
        risky = dict(board, lanes=[dict(board["lanes"][0], risk={
            "matches": [{"category": "migration", "path": "db/migrate/1.sql"}],
            "needs_user": [{"category": "migration", "path": "db/migrate/1.sql"}]})])

        def rebriefs(data):
            fleet.write_json(self.project / "board.json", data)  # the same `made`: the same age text
            return [fleet.session_start_context({"source": s, "cwd": str(self.repo)}, env={"HERDR_PANE_ID": "w1:p1"})
                    for s in ("startup", "resume", "clear", "compact")]
        before = rebriefs(board)  # records as 0.6.0 wrote them, without `risk`
        self.assertTrue(all("\nBoard from 5s ago (cached" in text for text in before), before)
        self.assertEqual(rebriefs(quiet), before)
        self.assertEqual(rebriefs(covered), before)
        for old, new in zip(before, rebriefs(risky)):
            diff = [(a, b) for a, b in zip(old.splitlines(), new.splitlines()) if a != b]
            self.assertEqual(len(old.splitlines()), len(new.splitlines()))
            [(a, b)] = diff
            self.assertTrue(a.endswith("next: Fix it."), a)
            self.assertEqual(b[:len(a) - len("next: Fix it.")], a[:len(a) - len("next: Fix it.")])
            self.assertTrue(b.endswith("risky diff needs the user (1): migration db/migrate/1.sql"), b)

    def test_board_lines_of_a_record_without_risk(self):
        record = {"lane": "feat/x", "phase": "working", "pr": None, "panes": [{"role": "impl", "agent": "claude",
                  "status": "working"}], "last_event": None, "blocked": None, "next": "Write the tests."}
        before = ["LANE    PHASE    PR  PANES         LAST  NOTE", "feat/x  working  -   impl:working  -     next: Write the tests."]
        self.assertEqual(fleet.board_lines([record]), before)  # a 0.6.0 record
        self.assertEqual(fleet.board_lines([{**record, "risk": None}]), before)
        self.assertEqual(fleet.board_lines([{**record, "risk": {"matches": [], "needs_user": []}}]), before)
        for damaged in ("x", {"needs_user": 3}, {"needs_user": [3, None]}):
            self.assertEqual(fleet.board_lines([{**record, "risk": damaged}]), before)
        self.assertTrue(fleet.board_lines([{**record, "risk": {"needs_user": [{"path": "a"}]}}])[1]
                        .endswith("risky diff needs the user (1): ? a"))

    def test_status_notes(self):
        md = ("# Status: x\n\n## Plan\n1. a\n\n## Blocked\n- none\n\n## Next steps\n\n- Run the tests.\n- More.\n\n"
              "READY FOR REVIEW: round 1\n")
        self.assertIsNone(fleet.first_note(md, "Blocked"))
        self.assertEqual(fleet.first_note(md, r"Next steps?"), "Run the tests.")
        # A level-3 heading belongs to the section; the next level-1 or level-2 heading ends it.
        self.assertEqual(fleet.section_text("## Rules\n- a\n### sub\n- b\n# Top\n- c\n", "Rules"), ["- a", "### sub", "- b"])

    def board(self, lanes, *argv, pane=None):
        with mock.patch.object(fleet, "collect_lanes", return_value=(lanes, [], [])) as collect, \
                mock.patch.object(fleet.shutil, "which", return_value="/usr/bin/herdr"):
            code, out = self.main(*argv, pane=pane)
        self.assertEqual(collect.call_args.kwargs["select"].__name__, "select")
        return code, out

    def test_board(self):
        a = self.add_lane("a", STATUS_md="## Blocked\n- Which API?\n\n## Next step\nAsk.\n")
        b = self.add_lane("b", STATUS_md="## Next step\nWrite the tests.\n")
        fleet.save_panes(a / ".fleet", {"w2:p1": {"role": "impl"}, "w2:p2": {"role": "critic"}})
        fleet.record("spawn", str(a), pane="w2:p1", cwd=str(a), branch="feat/a")
        fleet.record("phase", None, home=self.project, lane=str(a), branch="feat/a", old="idle", new="blocked")
        fleet.record("send", str(b), pane="w5:p1", cwd=str(b), branch="feat/b")
        lanes = [self.facts(a, label="lane-a", panes=[{"pane": "w2:p1", "agent": "claude", "status": "idle"},
                                                      {"pane": "w2:p2", "agent": "codex", "status": "working"},
                                                      {"pane": "w2:p3", "agent": None, "status": None}]),
                 self.facts(b, pr={"number": 12, "state": "OPEN", "url": "https://example.com/pr/12"})]
        code, out = self.board(lanes, "board", "--json")
        self.assertEqual(code, 0)
        recs = {r["lane"]: r for r in json.loads(out)["lanes"]}
        ra, rb = recs["lane-a"], recs["feat/b"]
        self.assertEqual((ra["phase"], ra["blocked"], ra["next"], ra["workspace"], ra["branch"]),
                         ("blocked", "Which API?", "Ask.", "w2", "feat/a"))
        self.assertEqual([(p["pane"], p["role"], p["agent"], p["status"]) for p in ra["panes"]],
                         [("w2:p1", "impl", "claude", "idle"), ("w2:p2", "critic", "codex", "working"),
                          ("w2:p3", None, None, None)])
        self.assertEqual(ra["last_event"]["event"], "spawn")  # the phase event does not count
        self.assertEqual((rb["phase"], rb["pr"], rb["last_event"]["event"], rb["blocked"]),
                         ("pr open", {"number": 12, "state": "OPEN"}, "send", None))
        saved = fleet.read_json(self.project / "board.json")
        self.assertEqual((saved["project"], len(saved["lanes"])), ("app", 2))
        self.assertIn("made", saved)
        self.assertFalse((self.project / "digest.json").exists())  # board never moves the digest
        text = self.board(lanes, "board")[1]
        self.assertIn("project app: 2 lanes", text)
        self.assertRegex(text, r"lane-a\s+blocked\s+-\s+impl:idle critic:working\s+\S+ \S+ spawn\s+blocked: Which API\?")
        self.assertRegex(text, r"feat/b\s+pr open\s+#12 open")
        self.assertLessEqual(max(len(l) for l in text.splitlines()), 160)
        # A lane that cleanup removed after the digest cursor shows as removed, once it is gone.
        fleet.write_json(self.project / "digest.json", {"cursor": "2020-01-01T00:00:00+00:00", "lanes": {
            str(b): {"lane": "lane-b", "branch": "feat/b", "phase": "pr open"}}})
        fleet.record("cleanup", str(self.repo), lane=str(b), branch="feat/b", workspace="w5", removed=True)
        fleet.record("cleanup", str(self.repo), lane=str(self.root / "kept"), branch="feat/kept", removed=False)
        recs = json.loads(self.board(lanes[:1], "board", "--json")[1])["lanes"]
        self.assertEqual([(r["lane"], r["phase"]) for r in recs], [("lane-a", "blocked"), ("lane-b", "removed")])
        # Without a cursor, no lane counts as removed.
        (self.project / "digest.json").unlink()
        self.assertEqual(len(json.loads(self.board(lanes[:1], "board", "--json")[1])["lanes"]), 1)

    def test_board_keeps_only_this_projects_lanes(self):
        other = self.root / "other"
        other.mkdir()
        git("init", "-q", "-b", "main", cwd=other)
        git("remote", "add", "origin", "https://example.com/acme/other.git", cwd=other)
        fleet.project_for(str(other))
        lane = self.add_lane("x")
        select = fleet.lane_selector(self.project)
        calls = []
        real = fleet.repo_identity
        with mock.patch.object(fleet, "repo_identity", side_effect=lambda *a, **k: calls.append(a) or real(*a, **k)):
            self.assertTrue(select({"main_root": str(self.repo), "path": str(lane)}))
            self.assertTrue(select({"main_root": str(self.repo), "path": str(self.root / "y")}))
            self.assertFalse(select({"main_root": str(other), "path": str(other / "z")}))
            self.assertFalse(select({"path": str(self.root / "missing")}))
        self.assertEqual(len(calls), 2)  # cached per main checkout
        # collect_lanes applies the filter before it reads any lane's facts.
        stubs = [{"workspace": "w2", "label": None, "path": str(lane), "panes": []},
                 {"workspace": "w3", "label": None, "path": str(other), "panes": []}]
        seen = []
        with mock.patch.object(fleet, "enumerate_lanes", return_value=(stubs, [])), \
                mock.patch.object(fleet, "lane_facts", side_effect=lambda s, *a, **k: seen.append(s["path"]) or {
                    **s, "panes": []}), mock.patch.object(fleet, "finish_lane", side_effect=lambda f: f):
            lanes, _, _ = fleet.collect_lanes(select=select)
        self.assertEqual(seen, [str(lane)])
        self.assertEqual([l["path"] for l in lanes], [str(lane)])

    # digest -----------------------------------------------------------

    def test_digest_cursor_phases_and_peek(self):
        a = self.add_lane("a", STATUS_md="## Next step\nGo.\n")
        working = [{"pane": "w2:p1", "agent": "claude", "status": "working"}]
        lanes = [self.facts(a, label="lane-a", panes=working)]
        fleet.record("spawn", str(a), pane="w2:p1", cwd=str(a), branch="feat/a", role="impl")
        # --peek reads only: nothing recorded, saved or moved.
        code, out = self.board(lanes, "digest", "--peek", pane="w1:p1")
        self.assertIn("read only (--peek)", out)
        self.assertIn("lane-a: new -> working", out)
        self.assertIn("spawn", out)
        self.assertFalse((self.project / "digest.json").exists())
        self.assertFalse((self.project / "board.json").exists())
        self.assertEqual(self.events("phase"), [])
        # The first digest: every lane is new.
        data = json.loads(self.board(lanes, "digest", "--json", pane="w1:p1")[1])
        self.assertEqual((data["read_only"], data["since"]), (False, None))
        self.assertEqual([(c["lane"], c["old"], c["new"]) for c in data["changes"]], [("lane-a", None, "working")])
        base = fleet.read_json(self.project / "digest.json")
        log = self.project / "events" / f"{self.clock.strftime('%Y-%m')}.jsonl"
        self.assertEqual(base["read"], {log.name: log.stat().st_size - len(log.read_bytes().splitlines(True)[-1])})
        self.assertEqual(base["lanes"], {str(a): {"lane": "lane-a", "branch": "feat/a", "phase": "working"}})
        self.assertTrue((self.project / "board.json").exists())
        phases = self.events("phase")
        self.assertEqual([(e["lane"], e["name"], e["branch"], e.get("old"), e["new"]) for e in phases],
                         [(str(a), "lane-a", "feat/a", None, "working")])
        # Nothing changed: no phase change, no event (the phase events are not shown).
        out = self.board(lanes, "digest", pane="w1:p1")[1]
        self.assertIn("No phase changes.", out)
        self.assertIn("No new events.", out)
        # `board` between two digests consumes nothing.
        (a / ".fleet" / "STATUS.md").write_text("READY FOR REVIEW: round 1\n")
        self.board(lanes, "board", pane="w1:p1")
        fleet.record("send", str(a), pane="w2:p1", cwd=str(a), branch="feat/a", text="Fix it.")
        out = self.board(lanes, "digest", pane="w1:p1")[1]
        self.assertIn("lane-a: working -> ready 1", out)
        self.assertIn('"Fix it."', out)
        self.assertNotIn("spawn", out)  # before the cursor
        self.assertEqual([(e.get("old"), e["new"]) for e in self.events("phase")][-1], ("working", "ready 1"))
        # A lane that leaves the board without a cleanup event is gone; a removed lane is reported once.
        out = self.board([], "digest", pane="w1:p1")[1]
        self.assertIn("lane-a: ready 1 -> gone", out)
        self.assertEqual(fleet.read_json(self.project / "digest.json")["lanes"], {})

    def two_orchs(self):
        """Orch default (w1:p1) and orch billing (w2:p1), both alive, with lanes a (default's), b (billing's) and c
        (unowned)."""
        self.main("orch", "claim", pane="w1:p1")
        self.main("orch", "claim", "--name", "billing", pane="w2:p1")
        self.alive.update({"w1:p1", "w2:p1"})
        lanes = {n: self.add_lane(n, STATUS_md="## Next step\nGo.\n") for n in "abc"}
        fleet.write_json(lanes["a"] / ".fleet" / "lane.json", {"orch": "default", "branch": "feat/a"})
        fleet.write_json(lanes["b"] / ".fleet" / "lane.json", {"orch": "billing", "branch": "feat/b"})
        return lanes

    def views(self, lanes, *argv, pane=None, **extra):
        """`board` or `digest` through the real collect_lanes, with herdr's lanes stubbed (`lanes`, open workspaces)
        and their facts too: (exit code, output, the lanes whose facts were read)."""
        stubs = [{"workspace": f"w{i}", "label": None, "path": str(p), "panes": []} for i, p in enumerate(lanes, 5)]
        seen = []

        def facts(stub, *args, **kwargs):
            seen.append(stub["path"])
            return {**self.facts(stub["path"]), "main_root": stub.get("main_root")}
        with mock.patch.object(fleet, "enumerate_lanes", return_value=(stubs, [])), \
                mock.patch.object(fleet, "lane_facts", side_effect=facts), \
                mock.patch.object(fleet, "finish_lane", side_effect=lambda f: f), \
                mock.patch.object(fleet.shutil, "which", return_value="/usr/bin/herdr"):
            code, out = self.main(*argv, pane=pane, **extra)
        return code, out, seen

    def test_each_orch_sees_its_own_lanes_and_the_unowned_ones(self):
        lanes = self.two_orchs()
        a, b, c = (str(lanes[n]) for n in "abc")
        orchs = self.project / "orchs"
        code, out, seen = self.views(lanes.values(), "board", "--json", pane="w1:p1")
        data = json.loads(out)
        self.assertEqual((code, data["orch"], data["all"], data["saved"]), (0, "default", False, True))
        self.assertEqual(seen, [a, c])  # filtered before the facts: gh is asked for these only
        self.assertEqual([(r["lane"], r["orch"]) for r in data["lanes"]], [("feat/a", "default"), ("feat/c", None)])
        self.assertEqual(data["board"], str(orchs / "default" / "board.json"))
        self.assertEqual([r["lane"] for r in fleet.read_json(orchs / "default" / "board.json")["lanes"]],
                         ["feat/a", "feat/c"])
        self.assertFalse((orchs / "billing" / "board.json").exists())
        text = self.views(lanes.values(), "board", pane="w1:p1")[1]
        self.assertIn("project app, orch default: 2 lanes", text)
        self.assertRegex(text, r"(?m)^feat/a .*next: Go\.$")  # another note wins over `unowned`
        self.assertNotIn("ORCH", text)
        (lanes["c"] / ".fleet" / "STATUS.md").write_text("# Status\n")
        self.assertRegex(self.views(lanes.values(), "board", pane="w1:p1")[1], r"(?m)^feat/c .*unowned: adopt it$")
        _, out, seen = self.views(lanes.values(), "board", "--json", pane="w2:p1")
        self.assertEqual(([r["lane"] for r in json.loads(out)["lanes"]], seen), (["feat/b", "feat/c"], [b, c]))
        # --all, and a caller that is no orch: every lane with its orch, an ORCH column, nothing saved.
        saved = {n: (orchs / n / "board.json").read_bytes() for n in ("default", "billing")}
        for argv, pane in ((("board", "--all"), "w1:p1"), (("board",), "w9:p9"), (("board",), None)):
            code, text, seen = self.views(lanes.values(), *argv, pane=pane)
            self.assertEqual((code, seen), (0, [a, b, c]), argv)
            self.assertIn("project app (every orch's lanes): 3 lanes", text)
            self.assertRegex(text, r"(?m)^LANE\s+ORCH\s+PHASE")
            self.assertRegex(text, r"(?m)^feat/b\s+billing\s+")
            self.assertRegex(text, r"(?m)^feat/c\s+-\s+.*unowned: adopt it$")
            self.assertLessEqual(max(len(l) for l in text.splitlines()), 160)
        data = json.loads(self.views(lanes.values(), "board", "--all", "--json", pane="w1:p1")[1])
        self.assertEqual((data["all"], data["saved"], data["board"]), (True, False, None))
        self.assertEqual({n: (orchs / n / "board.json").read_bytes() for n in saved}, saved)
        self.assertFalse((self.project / "board.json").exists())  # a board never moves a file to the root
        # The ORCH column clips a long name; LANE is clipped to 24.
        lines = fleet.board_lines([{"lane": "x" * 40, "orch": "y" * 30, "phase": "idle"}], owners=True, column=True)
        self.assertEqual(lines[1].split()[:2], ["x" * 23 + "…", "y" * 11 + "…"])

    def test_two_orchs_have_their_own_digests(self):
        lanes = self.two_orchs()
        a, b, c = (str(lanes[n]) for n in "abc")
        orchs = self.project / "orchs"
        for name, path in (("a", a), ("b", b), ("c", c)):
            fleet.record("send", path, pane=f"w{name}:p1", cwd=path, branch=f"feat/{name}", text=f"to {name}")
        fleet.record("send", str(self.repo), by=False, pane="w7:p1", cwd=str(self.repo), branch="main", text="main")
        fleet.record("follow_up_added", None, home=self.project, by=False, pane="w7:p1", id="F1", text="shared")
        with open(self.log_file(), "a") as fh:  # removed long ago: the result never depends on the wall clock
            for lane, orch in (("gone", "billing"), ("mine", "default")):
                fh.write(json.dumps({"ts": "2021-01-01T00:00:00+00:00", "event": "cleanup", "project": "app",
                                     "lane": str(self.root / lane), "branch": f"feat/{lane}", "removed": True,
                                     "orch": orch}) + "\n")
        data = json.loads(self.views(lanes.values(), "digest", "--peek", "--json", pane="w1:p1")[1])
        self.assertEqual(data["orch"], "default")
        texts = [e.get("text") or e["event"] for e in data["events"]]
        # The other orch's claim and the follow-up are project events; a send in main is no lane's.
        self.assertEqual(texts, ["orch_claim", "to a", "to c", "main", "shared", "cleanup"])
        self.assertEqual(data["events"][0]["orch"], "billing")
        self.assertEqual(data["events"][-1]["lane"], str(self.root / "mine"))
        # The consuming digest of each orch: its own cursor, board and phase events.
        data = json.loads(self.views(lanes.values(), "digest", "--json", pane="w1:p1")[1])
        self.assertEqual(sorted((c["lane"], c["new"]) for c in data["changes"]), [("feat/a", "no agent"), ("feat/c", "no agent")])
        self.assertEqual(sorted(fleet.read_json(orchs / "default" / "digest.json")["lanes"]), [a, c])
        self.assertFalse((orchs / "billing" / "digest.json").exists())
        self.assertEqual({e["orch"] for e in self.events("phase")}, {"default"})
        data = json.loads(self.views(lanes.values(), "digest", "--json", pane="w2:p1")[1])
        self.assertEqual(data["orch"], "billing")
        self.assertEqual(sorted(c["lane"] for c in data["changes"]), ["feat/b", "feat/c"])  # c: on both (D9)
        self.assertEqual([e.get("text") or e["event"] for e in data["events"]],
                         ["orch_claim", "to b", "to c", "main", "shared", "cleanup"])
        self.assertEqual(data["events"][0]["orch"], "default")
        self.assertEqual(data["events"][-1]["lane"], str(self.root / "gone"))
        self.assertEqual(sorted(fleet.read_json(orchs / "billing" / "digest.json")["lanes"]), [b, c])
        self.assertEqual(sorted(e["orch"] for e in self.events("phase")), ["billing", "billing", "default", "default"])
        self.assertFalse((self.project / "digest.json").exists())  # a digest never moves a file
        # The default's cursor did not move with billing's: its next digest has nothing new.
        out = self.views(lanes.values(), "digest", pane="w1:p1")[1]
        self.assertIn("project app, orch default: digest since", out)
        self.assertIn("No phase changes.", out)
        self.assertIn("No new events.", out)
        # A removed lane is on its own orch's board only.
        fleet.write_json(orchs / "billing" / "digest.json", {**fleet.read_json(orchs / "billing" / "digest.json"),
                                                             "cursor": "2020-01-01T00:00:00+00:00"})
        recs = json.loads(self.views(lanes.values(), "board", "--json", pane="w2:p1")[1])["lanes"]
        self.assertEqual([(r["lane"], r["phase"], r["orch"]) for r in recs if r["phase"] == "removed"],
                         [("feat/gone", "removed", "billing")])

    def test_who_may_consume_a_digest_with_two_orchs(self):
        lanes = self.two_orchs()
        orchs = self.project / "orchs"
        # A caller that is no orch only reads, and is told to name one.
        data = json.loads(self.views(lanes.values(), "digest", "--json", pane="w9:p9")[1])
        self.assertEqual((data["read_only"], data["why"], data["orch"]), (True, "name the orch: --orch NAME", None))
        self.assertEqual({r["orch"] for r in data["lanes"]}, {"default", "billing", None})  # every lane
        # --orch from another pane only reads while that orch's pane is alive.
        data = json.loads(self.views(lanes.values(), "digest", "--orch", "default", "--json", pane="w9:p9")[1])
        self.assertEqual((data["read_only"], data["why"]), (True, "pane w1:p1 is orch default of this project"))
        self.assertFalse((orchs / "default" / "digest.json").exists())
        self.alive.discard("w1:p1")
        data = json.loads(self.views(lanes.values(), "digest", "--orch", "default", "--json", pane="w9:p9")[1])
        self.assertFalse(data["read_only"])
        self.assertTrue((orchs / "default" / "digest.json").exists())
        self.assertFalse((orchs / "billing" / "digest.json").exists())
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(self.views(lanes.values(), "digest", "--orch", "nope", pane="w9:p9")[0], 1)
        # A consuming digest from the orch's pane records its session (the hook's session match), once.
        before = fleet.read_orchs(self.project)
        self.views(lanes.values(), "digest", pane="w2:p1", CLAUDE_CODE_SESSION_ID="sess-new")
        after = fleet.read_orchs(self.project)
        self.assertEqual((after["billing"]["session"], after["default"]), ("sess-new", before["default"]))
        stamp = (self.project / "orch.json").stat().st_mtime_ns
        self.views(lanes.values(), "digest", pane="w2:p1", CLAUDE_CODE_SESSION_ID="sess-new")
        self.assertEqual((self.project / "orch.json").stat().st_mtime_ns, stamp)  # no change, no write
        self.views(lanes.values(), "digest", "--peek", pane="w2:p1", CLAUDE_CODE_SESSION_ID="sess-peek")
        self.assertEqual(fleet.read_orchs(self.project)["billing"]["session"], "sess-new")  # --peek writes nothing
        # One legacy orch: a consuming digest refreshes its session in the legacy shape.
        (self.project / "orch.json").unlink()
        self.main("orch", "claim", pane="w1:p1")
        self.views([], "digest", pane="w1:p1", CLAUDE_CODE_SESSION_ID="sess-legacy")
        rec = json.loads((self.project / "orch.json").read_text())
        self.assertEqual((rec["session"], (self.project / "orch.json").read_bytes()),
                         ("sess-legacy", self.legacy_bytes(rec)))

    def test_log_by_orch(self):
        lanes = self.two_orchs()
        a, b = str(lanes["a"]), str(lanes["b"])
        fleet.record("worktree", a, lane=a, branch="feat/a", orch="default")
        fleet.record("worktree", b, lane=b, branch="feat/b", orch="billing")
        fleet.record("send", a, pane="w5:p1", cwd=a, branch="feat/a", text="to a")
        fleet.record("send", b, pane="w6:p1", cwd=b, branch="feat/b", text="to b")
        fleet.record("follow_up_taken", None, home=self.project, id="F2", branch="feat/a", text="x")
        rows = json.loads(self.main("log", "--orch", "default", "--json")[1])
        self.assertEqual([(e["event"], e.get("text")) for e in rows],
                         [("orch_claim", None), ("worktree", None), ("send", "to a"), ("follow_up_taken", "x")])
        self.assertEqual([e["event"] for e in json.loads(self.main("log", "--orch", "billing", "--json")[1])],
                         ["orch_claim", "worktree", "send"])

    def test_rebrief_count_follows_the_view(self):
        """The re-brief of an orch of a project with two orchs reads its own files and counts the events of its view,
        from the cached boards only."""
        lanes = self.two_orchs()
        (lanes["c"] / ".fleet" / "STATUS.md").write_text("# Status\n")
        for pane in ("w1:p1", "w2:p1"):
            self.views(lanes.values(), "digest", pane=pane)
        for name in "abc":
            path = str(lanes[name])
            fleet.record("send", path, by=False, pane="w8:p1", cwd=path, branch=f"feat/{name}", text=name)
        with mock.patch.object(fleet, "collect_lanes", side_effect=AssertionError("no scan")):
            text = fleet.orch_rebrief(self.project, "w1:p1", {"source": "clear"}, {})
        self.assertIn("2 events since the last digest.", text)  # a and c, not b
        self.assertRegex(text, r"feat/c .*unowned: adopt it")

    def test_digest_only_reads_for_another_pane_than_a_live_orch(self):
        a = self.add_lane("a")
        lanes = [self.facts(a, label="lane-a")]
        self.main("orch", "claim", pane="w1:p1")
        self.alive.add("w1:p1")
        for pane in ("w2:p1", None):  # another pane, and outside Herdr
            out = self.board(lanes, "digest", pane=pane)[1]
            self.assertIn("read only (pane w1:p1 is the orch of this project)", out)
            self.assertFalse((self.project / "digest.json").exists())
        self.assertTrue(json.loads(self.board(lanes, "digest", "--json", pane="w2:p1")[1])["read_only"])
        # The orch itself moves the cursor; so does anyone once the orch pane is dead.
        self.board(lanes, "digest", pane="w1:p1")
        self.assertTrue((self.project / "digest.json").exists())
        self.alive.clear()
        (self.project / "digest.json").unlink()
        self.board(lanes, "digest", pane="w2:p1")
        self.assertTrue((self.project / "digest.json").exists())

    def test_digest_event_filter_and_cap(self):
        a = self.add_lane("a")
        lanes = [self.facts(a, label="lane-a")]
        self.main("orch", "claim", pane="w1:p1")
        with self.env("w1:p1"):
            fleet.record("send", str(a), pane="w2:p1", cwd=str(a), branch="feat/a", text="by the orch")
        with self.env("w2:p1"):
            fleet.record("send", str(a), pane="w3:p1", cwd=str(a), branch="feat/a", text="by a lane")
        fleet.record("send", str(a), by=False, pane="w1:p1", text="to the orch")
        for source, cwd in (("startup", a), ("compact", a), ("compact", self.repo)):
            fleet.record("session_start", str(a), by=False, pane="w2:p1", cwd=str(cwd), source=source, role="impl")
        fleet.record("session_end", str(a), by=False, pane="w2:p1", cwd=str(a), reason="other")
        fleet.record("phase", None, home=self.project, lane=str(a), new="idle")
        shown = json.loads(self.board(lanes, "digest", "--json", "--peek", pane="w1:p1")[1])["events"]
        self.assertEqual([(e["event"], e.get("text") or e.get("source")) for e in shown],
                         [("send", "by a lane"), ("session_start", "compact")])
        self.assertEqual(shown[1]["cwd"], str(a))
        # At most 30 events, newest last.
        for i in range(40):
            fleet.record("send", str(a), by=False, pane="w2:p1", cwd=str(a), branch="feat/a", text=f"n{i}")
        data = json.loads(self.board(lanes, "digest", "--json", "--peek", pane="w1:p1")[1])
        self.assertEqual((len(data["events"]), data["omitted"], data["events"][-1]["text"]), (30, 12, "n39"))
        out = self.board(lanes, "digest", "--peek", pane="w1:p1")[1]
        self.assertIn("12 earlier not shown", out)

    # round 2: liveness, the lock, log offsets, the re-brief's failure path -----------

    def herdr_answers(self, answers):
        """fleet.run for `herdr pane get`: each pane maps to (exit code, stdout) or an exception. git runs."""
        real = fleet.run

        def run(cmd, *args, **kwargs):
            if cmd[:3] != ["herdr", "pane", "get"]:
                return real(cmd, *args, **kwargs)
            answer = answers[cmd[3]]
            if isinstance(answer, Exception):
                raise answer
            return subprocess.CompletedProcess(cmd, answer[0], answer[1], "")
        return mock.patch.object(fleet, "run", run)

    def test_pane_state_from_herdr(self):
        self.alive_patch.stop()
        try:
            answers = {
                "w1:p1": (0, json.dumps({"result": {"pane": {"pane_id": "w1:p1", "agent": "claude"}}})),
                "w1:p2": (0, json.dumps({"result": {"pane": {"pane_id": "w1:p2", "agent": None}}})),
                "w1:p3": (1, json.dumps({"error": {"code": "pane_not_found", "message": "pane w1:p3 not found"}})),
                "w1:p4": fleet.FleetError("timed out after 10s: herdr pane get w1:p4"),
                "w1:p5": (1, json.dumps({"error": {"code": "server_unreachable", "message": "no server"}})),
                "w1:p6": (0, "not json"),
                "w1:p7": (0, json.dumps({"result": {}})),
                # A pane object is valid only for the pane asked about; a shell pane has no `agent` key.
                "w2:p1": (0, json.dumps({"result": {"pane": {"pane_id": "w2:p1"}}})),
                "w2:p2": (0, json.dumps({"result": {"pane": {"pane_id": "w2:p2", "agent": False}}})),
                "w2:p3": (0, json.dumps({"result": {"pane": {"pane_id": "w2:p3", "agent": []}}})),
                "w2:p4": (0, json.dumps({"result": {"pane": {"unexpected": "shape"}}})),
                "w2:p5": (0, json.dumps({"result": {"pane": {"pane_id": "w9:p9", "agent": "claude"}}})),
                "w2:p6": (0, json.dumps({"result": {"pane": {"pane_id": "w2:p6", "agent": ""}}})),
            }
            with self.herdr_answers(answers), mock.patch.object(fleet.shutil, "which", return_value="/usr/bin/herdr"):
                self.assertEqual([fleet.pane_state(f"w1:p{i}") for i in range(1, 8)],
                                 ["alive", "dead", "dead", "unknown", "unknown", "unknown", "unknown"])
                self.assertEqual([fleet.pane_state(f"w2:p{i}") for i in range(1, 7)],
                                 ["dead", "unknown", "unknown", "unknown", "unknown", "unknown"])
                # A malformed pane object never authorizes a claim or a foreign digest.
                for bad in ("w2:p2", "w2:p3", "w2:p4"):
                    fleet.write_json(self.project / "orch.json", {"pane": bad})
                    self.assertEqual(self.main("orch", "claim", pane="w9:p9")[0], 2, bad)
                    self.assertEqual(fleet.read_json(self.project / "orch.json")["pane"], bad)
                    with mock.patch.object(fleet, "collect_lanes", return_value=([], [], [])):
                        self.assertTrue(json.loads(self.main("digest", "--json")[1])["read_only"], bad)
                    self.assertFalse((self.project / "digest.json").exists())
                # A shell pane whose agent exited is dead: a successor claims without --force.
                fleet.write_json(self.project / "orch.json", {"pane": "w2:p1"})
                self.assertEqual(self.main("orch", "claim", pane="w9:p9")[0], 0)
                (self.project / "orch.json").unlink()
                # Claim and digest on the real lookup: a timeout never authorizes a takeover or a foreign digest.
                self.main("orch", "claim", pane="w1:p4")
                code, out = self.main("orch", "claim", pane="w9:p9")
                self.assertEqual(code, 2)
                self.assertIn("`fleet orch claim --force` takes over", json.loads(out)["refused"])
                self.assertEqual(fleet.read_json(self.project / "orch.json")["pane"], "w1:p4")
                with mock.patch.object(fleet, "collect_lanes", return_value=([], [], [])):
                    for pane in ("w9:p9", None):
                        data = json.loads(self.main("digest", "--json", pane=pane)[1])
                        self.assertTrue(data["read_only"])
                        self.assertIn("cannot tell", data["why"])
                    self.assertFalse((self.project / "digest.json").exists())
                self.assertIsNone(json.loads(self.main("orch", "show", "--json")[1])["alive"])
                self.assertIn("herdr cannot tell", self.main("orch", "show")[1])
                # --force takes over an unknown pane, but never a live one.
                self.assertEqual(self.main("orch", "claim", "--force", pane="w9:p9")[0], 0)
                self.assertEqual(self.events("orch_claim")[-1].get("force"), True)
                answers["w9:p9"] = (0, json.dumps({"result": {"pane": {"pane_id": "w9:p9", "agent": "claude"}}}))
                self.assertEqual(self.main("orch", "claim", "--force", pane="w8:p8")[0], 2)
                # The registered pane hands over even when herdr cannot see the successor.
                answers["w7:p7"] = fleet.FleetError("timed out")
                self.assertEqual(self.main("orch", "claim", "--pane", "w7:p7", "--replace", pane="w9:p9")[0], 0)
                self.assertIs(json.loads(self.main("orch", "show", "--json")[1])["alive"], None)
                answers["w7:p7"] = answers["w1:p3"]
                self.assertIs(json.loads(self.main("orch", "show", "--json")[1])["alive"], False)
        finally:
            self.alive_patch.start()

    def test_the_project_lock(self):
        with mock.patch.object(fleet, "LOCK_WAIT", 0.3), mock.patch.object(sys, "stderr", io.StringIO()) as err:
            with fleet.project_lock(self.project):
                # claim and release wait for the lock, then fail with a clear message, and change nothing.
                self.assertEqual(self.main("orch", "claim", pane="w1:p1")[0], 1)
                self.assertIn("project app is busy", err.getvalue())
                self.assertFalse((self.project / "orch.json").exists())
                fleet.write_json(self.project / "orch.json", {"pane": "w1:p1"})
                self.assertEqual(self.main("orch", "release", pane="w1:p1")[0], 1)
                self.assertTrue((self.project / "orch.json").exists())
                # A consuming digest waits too; --peek and the re-brief never take the lock.
                with mock.patch.object(fleet, "collect_lanes", return_value=([], [], [])), \
                        mock.patch.object(fleet.shutil, "which", return_value="/usr/bin/herdr"):
                    self.assertEqual(self.main("digest", pane="w1:p1")[0], 1)
                    self.assertEqual(self.main("digest", "--peek", pane="w1:p1")[0], 0)
                fleet.write_json(self.project / "digest.json", {"cursor": fleet.now_iso()})
                self.assertIn("you orchestrate project app", fleet.session_start_context(
                    {"source": "clear", "cwd": str(self.repo)}, env={"HERDR_PANE_ID": "w1:p1"}))

    def test_a_digest_holds_the_lock_through_its_commit(self):
        """While a digest collects its board, no claim and no second digest can run; the check holds at commit."""
        a = self.add_lane("a")
        results = []

        def slow_board(*args, **kwargs):
            with mock.patch.object(fleet, "LOCK_WAIT", 0.2), mock.patch.object(sys, "stderr", io.StringIO()), \
                    mock.patch.object(fleet, "pane_info", return_value={"workspace_id": "w2"}):
                results.append(("claim", fleet.main(["orch", "claim", "--pane", "w2:p1"])))
                results.append(("digest", fleet.main(["digest"])))
            return ([self.facts(a, label="lane-a")], [], [])
        with mock.patch.object(fleet, "collect_lanes", side_effect=slow_board), \
                mock.patch.object(fleet.shutil, "which", return_value="/usr/bin/herdr"):
            data = json.loads(self.main("digest", "--json")[1])  # outside Herdr, no orch registered
        self.assertEqual(results, [("claim", 1), ("digest", 1)])
        self.assertFalse(data["read_only"])
        self.assertFalse((self.project / "orch.json").exists())
        self.assertEqual(len(self.events("phase")), 1)

    def test_board_json_never_goes_back_in_time(self):
        # A fixed clock, one microsecond a step: the result never depends on how far the real clock moves.
        older, newer, began, during, after = (f"2026-11-04T10:00:00.00000{i}+00:00" for i in range(1, 6))
        self.assertTrue(fleet.save_board(self.project, [{"lane": "new"}], newer))
        self.assertFalse(fleet.save_board(self.project, [{"lane": "old"}], older))
        self.assertEqual(fleet.read_json(self.project / "board.json")["lanes"], [{"lane": "new"}])
        # `board` stamps the board with the time its collection began, and says when it keeps a newer one.
        collected = []

        def collect(*args, **kwargs):
            fleet.save_board(self.project, [{"lane": "newer"}], during)  # another board, begun after this one
            collected.append(True)
            return ([], [], [])
        with mock.patch.object(fleet, "precise_now", side_effect=lambda: after if collected else began), \
                mock.patch.object(fleet, "collect_lanes", side_effect=collect), \
                mock.patch.object(fleet.shutil, "which", return_value="/usr/bin/herdr"):
            self.assertIn("board.json holds a newer board", self.main("board")[1])
        self.assertEqual(fleet.read_json(self.project / "board.json"), {"made": during, "project": "app",
                                                                        "lanes": [{"lane": "newer"}]})

    def log_file(self, month=None):
        return self.project / "events" / f"{month or self.clock.strftime('%Y-%m')}.jsonl"

    def raw_event(self, text, ts="2026-11-03T10:00:00+00:00", month=None, end="\n"):
        path = self.log_file(month)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps({"ts": ts, "event": "send", "pane": "w2:p1", "text": text}) + end)

    def digest(self, *argv):
        with mock.patch.object(fleet, "collect_lanes", return_value=([], [], [])), \
                mock.patch.object(fleet.shutil, "which", return_value="/usr/bin/herdr"):
            data = json.loads(self.main("digest", "--json", *argv)[1])
        return [e.get("text") for e in data["events"]]

    def test_digest_reads_the_log_by_offset(self):
        # An empty log, and an event appended while the digest runs, after it read the log.
        real = fleet.read_new_events

        def read_then_append(*args, **kwargs):
            out = real(*args, **kwargs)
            self.raw_event("during", ts="2026-11-03T09:00:00+00:00")
            return out
        with mock.patch.object(fleet, "read_new_events", side_effect=read_then_append):
            self.assertEqual(self.digest(), [])
        self.assertEqual(self.digest(), ["during"])
        self.assertEqual(self.digest(), [])
        # A late append with an older time than the events already read.
        self.raw_event("new", ts="2026-11-03T12:00:00+00:00")
        self.assertEqual(self.digest(), ["new"])
        self.raw_event("late", ts="2026-11-03T11:00:00+00:00")
        self.assertEqual(self.digest(), ["late"])
        # A partial last line waits until it is complete.
        self.raw_event("partial", end="")
        self.assertEqual(self.digest(), [])
        with open(self.log_file(), "a") as fh:
            fh.write("\n")
        self.raw_event("after")
        self.assertEqual(self.digest("--peek"), ["partial", "after"])
        self.assertEqual(self.digest(), ["partial", "after"])
        # A new month is read from its start; a file that shrank was rewritten and is read again.
        self.raw_event("next month", month="2099-01")
        self.assertEqual(self.digest(), ["next month"])
        self.log_file("2099-01").write_text(json.dumps({"ts": "2099-01-01T00:00:00+00:00", "event": "send",
                                                        "text": "x"}) + "\n")
        self.assertEqual(self.digest(), ["x"])
        self.assertEqual(set(fleet.read_json(self.project / "digest.json")["read"]),
                         {self.log_file().name, "2099-01.jsonl"})

    def test_unicode_line_separators_stay_inside_their_event(self):
        texts = ["a\u2028b", "c\u2029d", "e\u0085f"]
        for text in texts:
            fleet.record("send", None, home=self.project, by=False, pane="w2:p1", text=text)
        log = self.log_file()
        self.assertEqual(log.read_bytes().count(b"\n"), 3)
        self.assertEqual(self.digest("--peek"), texts)
        self.assertEqual(self.digest(), texts)
        self.assertEqual(fleet.read_json(self.project / "digest.json")["read"], {log.name: log.stat().st_size})
        self.assertEqual(self.digest(), [])
        self.assertEqual([e["text"] for e in json.loads(self.main("log", "--json")[1])], texts)

    def test_record_takes_its_time_under_the_lock(self):
        order = []
        real_flock = fleet.fcntl.flock
        real_now = fleet.now_iso

        def flock(fd, op):
            order.append("lock")
            return real_flock(fd, op)

        def now():
            order.append("time")
            return real_now()
        with mock.patch.object(fleet.fcntl, "flock", flock), mock.patch.object(fleet, "now_iso", now):
            fleet.record("send", None, home=self.project, pane="w2:p1")
        self.assertEqual(order[-2:], ["lock", "time"])

    def test_rebrief_reads_only_the_unread_log_within_a_budget(self):
        self.orch_setup()
        with mock.patch.object(fleet, "collect_lanes", return_value=([], [], [])), \
                mock.patch.object(fleet.shutil, "which", return_value="/usr/bin/herdr"):
            for i in range(2000):
                self.raw_event(f"old {i}")
            self.main("digest", pane="w1:p1")
        self.raw_event("one")
        self.raw_event("two")

        def brief():
            return fleet.session_start_context({"source": "clear", "cwd": str(self.repo)},
                                               env={"HERDR_PANE_ID": "w1:p1"})
        with mock.patch.object(fleet, "REBRIEF_READ_BUDGET", 1000):  # the consumed history is far larger
            self.assertIn("\n2 events since the last digest.", brief())
            for i in range(50):
                self.raw_event(f"backlog {i}")
            self.assertRegex(brief(), r"More than \d+ events since the last digest\.")

    def test_a_failed_rebrief_never_gives_the_lane_text(self):
        d = self.repo / ".fleet"
        d.mkdir()
        (d / "BRIEF.md").write_text("brief")
        (d / "STATUS.md").write_text("status")
        fleet.save_panes(d, {"w1:p1": {"role": "impl"}})
        before = fleet.session_start_context({"source": "clear", "cwd": str(self.repo)}, env={"HERDR_PANE_ID": "w1:p1"})
        self.assertIn("You implement this lane", before)
        self.orch_setup()
        # A damaged cached board renders as blanks.
        fleet.write_json(self.project / "board.json", {"made": fleet.now_iso(), "lanes": [
            {"lane": "x", "pr": "#4", "panes": "w2:p1", "last_event": 7, "phase": None, "blocked": ["?"]}, "bad"]})
        payload = json.dumps({"source": "clear", "cwd": str(self.repo)})
        cp = subprocess.run([*HELPER, "hook", "session-start"], input=payload, cwd=self.repo,
                            capture_output=True, text=True, timeout=CHILD_LIMIT,
                            env=helper_env({**os.environ, "HERDR_PANE_ID": "w1:p1"}))
        context = json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("you orchestrate project app", context)
        self.assertNotIn("You implement this lane", context)
        # Any other failure once the pane is known as the orch: the note.
        with mock.patch.object(fleet, "orch_rebrief", side_effect=RuntimeError("boom")):
            text = fleet.session_start_context({"source": "clear", "cwd": str(self.repo)},
                                               env={"HERDR_PANE_ID": "w1:p1", "CLAUDE_PLUGIN_ROOT": "/plugins/fleet"})
        self.assertIn(f"orchestrator of project app, but this hook {NOTE} (its re-brief failed: RuntimeError)", text)
        self.assertIn('`python3 "/plugins/fleet/scripts/fleet" digest --project app`', text)
        self.assertNotIn("You implement", text)
        # The same through the real entry: an exception in orch_rebrief, then one before any git call.
        for target, cause in (("orch_rebrief", "its re-brief failed: RuntimeError"),
                              ("identity_from", "an error in the hook: RuntimeError")):
            cp = subprocess.run([*HELPER, "hook", "session-start"], input=payload, cwd=self.repo,
                                capture_output=True, text=True, timeout=CHILD_LIMIT,
                                env=helper_env({**os.environ, "HERDR_PANE_ID": "w1:p1", "PATIENT_RAISE_IN": target}))
            self.assertEqual((cp.returncode, cp.stderr), (0, ""), target)
            context = json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn(f"{NOTE} ({cause})", context, target)
            self.assertNotIn("You implement", context, target)
        # A candidate read that fails knows no candidate: the pane behaves as on main.
        with mock.patch.object(fleet, "orch_candidates", side_effect=RuntimeError("boom")):
            self.assertEqual(fleet.session_start_context({"source": "clear", "cwd": str(self.repo)},
                                                         env={"HERDR_PANE_ID": "w1:p1"}), before)

    def test_round_expects_the_review_files_its_request_names(self):
        two = ("Write findings to .fleet/REVIEW-3-codex.md (codex) and .fleet/REVIEW-3-claude.md (claude). "
               "Re-check the fixes for REVIEW-2.md.")
        files = {"REVIEW-REQUEST-3.md": two, "REVIEW-3-codex.md": "VERDICT: APPROVE", "REVIEW-2.md": "VERDICT: APPROVE"}
        self.assertEqual(self.phase(files=files), "review 3")  # the claude critic has not written yet
        self.assertEqual(self.phase(files={**files, "REVIEW-3-claude.md": "VERDICT: APPROVE"}), "reviewed 3: APPROVE")
        # A request that names no file: the files that exist decide.
        self.assertEqual(self.phase(files={"REVIEW-REQUEST-1.md": "see the template",
                                           "REVIEW-1-x.md": "VERDICT: APPROVE"}), "reviewed 1: APPROVE")
        # REVIEW-10.md does not count for round 1.
        self.assertEqual(self.phase(files={"REVIEW-REQUEST-1.md": "Write .fleet/REVIEW-1.md",
                                           "REVIEW-10.md": "VERDICT: APPROVE"}), "review 1")

    # re-brief ---------------------------------------------------------

    def orch_setup(self, rules=("- Never push to main.",), follow_ups=("- Add a CHANGELOG entry.",)):
        self.main("orch", "claim", pane="w1:p1")
        (self.project / "PROJECT.md").write_text(
            "# Project: app\n\n## Rules\n" + "\n".join(rules) + "\n\n## Decisions\n- 11-01 Use sqlite.\n\n"
            "## Follow-ups\n" + "\n".join(follow_ups) + "\n\n## Notes\n- none\n")
        fleet.save_board(self.project, [{"lane": "lane-a", "path": str(self.root / "a"), "phase": "review 1",
                                         "panes": [], "pr": None, "last_event": None, "blocked": None,
                                         "next": "Fix it."}], fleet.precise_now())
        fleet.write_json(self.project / "digest.json", {"cursor": fleet.now_iso(), "lanes": {}})
        for text in ("one", "two"):
            fleet.record("send", str(self.repo), by=False, pane="w2:p1", text=text)
        fleet.record("send", str(self.repo), by=False, pane="w1:p1", text="the orch's own")

    def stop_clock(self, board, seconds):
        """Stop the helper's wall clock `seconds` after the board was made: its age is then always the same."""
        self.clock.stopped = fleet.event_time({"ts": board["made"]}) + seconds

    def test_rebrief_on_every_source(self):
        self.orch_setup()
        self.stop_clock(fleet.read_json(self.project / "board.json"), 5)
        for source in ("startup", "resume", "clear", "compact"):
            text = fleet.session_start_context({"source": source, "cwd": str(self.repo)},
                                               env={"HERDR_PANE_ID": "w1:p1"})
            self.assertIn("you orchestrate project app (pane w1:p1)", text, source)
            self.assertIn(str(self.project / "PROJECT.md"), text)
            self.assertIn("Never push to main.", text)
            self.assertIn("Add a CHANGELOG entry.", text)
            self.assertNotIn("Use sqlite", text)  # decisions stay in the file
            self.assertIn("\nBoard from 5s ago (cached; `board` refreshes it):\n", text)
            self.assertRegex(text, r"lane-a\s+review 1")
            self.assertIn("2 events since the last digest.", text)
            self.assertIn("digest` first, then load the fleet:fleet skill.", text)
        self.assertIn("just cleared", fleet.session_start_context({"source": "clear", "cwd": str(self.repo)},
                                                                  env={"HERDR_PANE_ID": "w1:p1"}))
        # The command matches the skills' allowed-tools when the hook knows the plugin root.
        text = fleet.session_start_context({"source": "clear", "cwd": str(self.repo)},
                                           env={"HERDR_PANE_ID": "w1:p1", "CLAUDE_PLUGIN_ROOT": "/plugins/fleet"})
        self.assertIn('Run `python3 "/plugins/fleet/scripts/fleet" digest` first', text)
        self.assertIn(f'Run `python3 "{SCRIPT.resolve().parent.parent}/scripts/fleet" digest` first', fleet.session_start_context(
            {"source": "clear", "cwd": str(self.repo)}, env={"HERDR_PANE_ID": "w1:p1"}))
        # In a checkout of another project, the orch pane is not briefed as this project's orch.
        other = self.root / "other"
        other.mkdir()
        git("init", "-q", "-b", "main", cwd=other)
        self.assertIsNone(fleet.session_start_context({"source": "startup", "cwd": str(other)},
                                                      env={"HERDR_PANE_ID": "w1:p1"}))
        # Without a digest or a board, the re-brief says so.
        (self.project / "digest.json").unlink()
        (self.project / "board.json").unlink()
        text = fleet.session_start_context({"source": "clear", "cwd": str(self.repo)}, env={"HERDR_PANE_ID": "w1:p1"})
        self.assertIn("No board yet.", text)
        self.assertIn("No digest yet.", text)

    def test_rebrief_is_capped(self):
        self.orch_setup(rules=[f"- rule {i}" for i in range(40)], follow_ups=[f"- follow-up {i}" for i in range(40)])
        fleet.save_board(self.project, [{"lane": f"lane-{i}", "path": str(self.root / str(i)), "phase": "idle",
                                         "panes": []} for i in range(30)], fleet.precise_now())
        text = fleet.session_start_context({"source": "clear", "cwd": str(self.repo)}, env={"HERDR_PANE_ID": "w1:p1"})
        self.assertLessEqual(len(text.splitlines()), fleet.REBRIEF_LINES_MAX)
        self.assertIn("25 more in PROJECT.md", text)
        self.assertTrue(text.splitlines()[-1].endswith("then load the fleet:fleet skill."))

    def test_other_panes_get_todays_output(self):
        d = self.repo / ".fleet"
        d.mkdir()
        (d / "BRIEF.md").write_text("brief")
        (d / "STATUS.md").write_text("status")
        (d / "FLEET.md").write_text("Orchestrator: pane w7:p7\n")
        fleet.save_panes(d, {"w2:p1": {"role": "impl"}})
        cases = [(source, pane) for source in ("startup", "resume", "clear", "compact")
                 for pane in ("w2:p1", "w7:p7", "w9:p9", None)]

        def outputs():
            return [fleet.session_start_context({"source": s, "cwd": str(self.repo)},
                                                env={"HERDR_PANE_ID": p} if p else {}) for s, p in cases]
        before = outputs()
        self.orch_setup()
        self.assertEqual(outputs(), before)
        self.assertIn("Fleet orchestrator: your context was just cleared. Before you act, read", before[9])
        # The orch pane gets the project re-brief instead of the FLEET.md one.
        text = fleet.session_start_context({"source": "clear", "cwd": str(self.repo)}, env={"HERDR_PANE_ID": "w1:p1"})
        self.assertIn("you orchestrate project app", text)

    def hook_child(self, event, payload, path_dirs=(), cwd=True, raw=None, stdin=None, **env):
        """The real hook entry in a child (HELPER: the script as __main__, with the stopped hook clock). PATH holds
        only `path_dirs`, so herdr and gh cannot run. Its input is `payload` as JSON, else the bytes `raw`, else the
        file descriptor `stdin`. Returns (completed process with text output, git limits asked, waits asked)."""
        limits, sleeps = self.root / "git-limits.jsonl", self.root / "sleeps.jsonl"
        for f in (limits, sleeps):
            f.unlink(missing_ok=True)
        env = {"PATH": os.pathsep.join(map(str, path_dirs)) or str(self.root / "no-bin"), "FLEET_HOME": str(self.home),
               "HERDR_PANE_ID": "w1:p1", "HOME": str(self.root), "PATIENT_GIT_LOG": str(limits),
               "PATIENT_SLEEP_LOG": str(sleeps), **env}
        given = {"stdin": stdin} if stdin is not None else \
            {"input": raw if raw is not None else json.dumps(payload).encode()}
        cp = subprocess.run([*HELPER, "hook", event], **given, cwd=self.repo if cwd is True else cwd,
                            capture_output=True, timeout=CHILD_LIMIT, env=helper_env(env))
        cp.stdout, cp.stderr = cp.stdout.decode(), cp.stderr.decode()
        asked = [json.loads(line)["timeout"] for line in limits.read_text().splitlines()] if limits.exists() else []
        waits = [json.loads(line) for line in sleeps.read_text().splitlines()] if sleeps.exists() else []
        return cp, asked, waits

    def git_wrapper(self, body=None):
        """A bin directory whose git notes each call in <root>/git-calls, then runs `body` (default: the real git)."""
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        calls = self.root / "git-calls"
        body = body or f'exec "{shutil.which("git")}" "$@"'
        (bin_dir / "git").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\n{body}\n')
        (bin_dir / "git").chmod(0o755)
        return bin_dir, calls

    def test_rebrief_hook_is_fast_and_offline(self):
        """The hook's work is bounded: two git calls within the hook's one deadline, and files; never herdr, gh, a
        scan of the lanes or a wait (time.sleep) of the helper. The test counts that work instead of timing it, so
        a loaded machine cannot fail it, and a wait of any length can."""
        self.orch_setup()
        bin_dir, calls = self.git_wrapper()  # git only, through a wrapper that notes each call
        cp, asked, waits = self.hook_child("session-start", {"source": "startup", "cwd": str(self.repo),
                                                             "session_id": "s-9"}, [bin_dir])
        self.assertEqual((cp.returncode, cp.stderr), (0, ""))
        context = json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("you orchestrate project app (pane w1:p1)", context)
        # The re-brief finds the project (two calls); the session record reuses it and stops: no .fleet/.
        self.assertEqual(calls.read_text().splitlines(),
                         [f"-C {self.repo} rev-parse --git-common-dir --show-toplevel",
                          f"-C {self.repo} config --get-regexp ^remote\\..*\\.url$"])
        # The child's clock stands still: each call asks for the whole deadline, budget - reserve.
        self.assertEqual(asked, [6, 6])
        # The helper asks for no wait on this path. The subprocess module's own wait loop sleeps in short steps
        # while git runs; that is not a wait of the hook.
        self.assertEqual([e for e in waits if Path(e["file"]).resolve() == SCRIPT], [])
        self.assertTrue(all(Path(e["file"]).name == "subprocess.py" for e in waits), waits)

    def test_hook_budgets_agree_with_hooks_json(self):
        """HOOK_BUDGET is the time that hooks.json gives each hook; the run's end and git deadline lie inside it."""
        hooks = json.loads((SCRIPT.parent.parent / "hooks" / "hooks.json").read_text())["hooks"]
        budgets = {name: {h["timeout"] for group in hooks[name] for h in group["hooks"]}
                   for name in ("SessionStart", "SessionEnd")}
        self.assertEqual(budgets, {"SessionStart": {fleet.HOOK_BUDGET["session-start"]},
                                   "SessionEnd": {fleet.HOOK_BUDGET["session-end"]}})
        self.assertEqual((fleet.HOOK_BUDGET, fleet.HOOK_BEFORE_CLOCK, fleet.HOOK_EXIT_MARGIN, fleet.HOOK_AFTER_GIT),
                         ({"session-start": 10, "session-end": 5}, 1.5, 0.5, {"session-start": 2, "session-end": 1}))
        self.assertEqual(patient_fleet.MIN_GIT_LIMIT, fleet.HOOK_GIT_MIN)  # a smaller git limit expires at once
        # The run's times, from its start: the end leaves the time before the clock and the exit margin.
        for event, end, deadline in (("session-start", 8, 6), ("session-end", 3, 2)):
            run = fleet.HookRun(event, start=100)
            self.assertEqual((run.end - 100, run.deadline - 100), (end, deadline), event)
            self.assertLessEqual(run.end + fleet.HOOK_EXIT_MARGIN - 100 + fleet.HOOK_BEFORE_CLOCK,
                                 fleet.HOOK_BUDGET[event])

    def test_hooks_pass_their_git_limit(self):
        """The tests give each git call a long limit (patient_fleet.py), so that a slow git cannot change a result;
        the hooks still ask for the time left until their one deadline, budget - reserve after the start of the
        run. The limits come from the subprocess boundary, before patient_run widens them: what the helper really
        passes to git. The hook clock stands still here, so the limits are exact."""
        self.assertIsInstance(fleet.subprocess, patient_fleet.PatientSubprocess)
        self.orch_setup()
        (self.repo / ".fleet").mkdir()
        data, env = {"source": "startup", "cwd": str(self.repo)}, {"HERDR_PANE_ID": "w1:p1"}
        run = fleet.HookRun("session-start")
        with patient_fleet.record_git_limits() as calls:
            self.assertEqual(fleet.orch_candidates(env), ("w1:p1", [self.project]))  # files only
            self.assertEqual(fleet.orch_decide(data, [self.project], run), (fleet.REBRIEF, self.project))
            self.assertEqual([limit for _, limit in calls], [6, 6])
            self.assertIsNotNone(fleet.hook_session_event("session_start", data, env, run))
        # The session record reuses the project of the run, and asks only for the branch.
        self.assertEqual([limit for _, limit in calls], [6] * 3)
        self.assertEqual([args[2] for args, _ in calls], ["rev-parse", "config", "rev-parse"])
        self.assertEqual(self.events("session_start")[-1]["branch"], "main")
        # SessionEnd: a shorter deadline. A record without a run starts its own: the project, then the branch.
        with patient_fleet.record_git_limits() as calls:
            self.assertIsNotNone(fleet.hook_session_event("session_end", data, env))
        self.assertEqual([limit for _, limit in calls], [2] * 3)
        # The same through the real entry, in a child.
        cp, asked, _ = self.hook_child("session-end", {**data, "reason": "exit"}, [Path(shutil.which("git")).parent])
        self.assertEqual((cp.returncode, cp.stdout, asked), (0, "", [2, 2, 2]))
        self.assertEqual(self.events("session_end")[-1]["reason"], "exit")

    def test_slow_git_shares_one_deadline(self):
        """With git at 2 s for each call the orch pane still gets its full re-brief, and git ends by the deadline,
        inside the budget less the reserve; the session record keeps what time is left. The clock moves only by
        git_takes(): nothing waits."""
        self.orch_setup()
        self.stop_clock(fleet.read_json(self.project / "board.json"), 5)
        (self.repo / ".fleet").mkdir()
        data, env = {"source": "clear", "cwd": str(self.repo)}, {"HERDR_PANE_ID": "w1:p1"}
        full = fleet.session_start_context(data, env)
        for takes, limits, branch in ((2, [6, 4, 2], "main"), (2.5, [6, 3.5, 1], None)):
            with patient_fleet.record_git_limits() as calls, patient_fleet.git_takes(takes) as clock:
                start = clock.now
                run = fleet.HookRun("session-start")
                self.assertEqual(fleet.session_start_context(data, env, run), full, takes)
                self.assertIsNotNone(fleet.hook_session_event("session_start", data, env, run))
                self.assertLessEqual(clock.now, run.end)  # the record too
                self.assertEqual(run.deadline - start, 6)
            self.assertEqual([limit for _, limit in calls], limits, takes)
            self.assertEqual(self.events("session_start")[-1].get("branch"), branch, takes)  # 2.5 s: no time left

    def test_git_slower_than_the_deadline_gives_the_orch_a_note(self):
        """A git slower than the whole deadline: the first call expires at the deadline, and the orch pane gets the
        note, never silence or the lane text; nothing is recorded, and no second call starts."""
        self.orch_setup()
        d = self.repo / ".fleet"
        d.mkdir()
        (d / "BRIEF.md").write_text("brief")
        data, env = {"source": "clear", "cwd": str(self.repo)}, {"HERDR_PANE_ID": "w1:p1"}
        before = self.log_file().read_bytes()
        with patient_fleet.record_git_limits() as calls, patient_fleet.git_takes(7) as clock:
            start = clock.now
            run = fleet.HookRun("session-start")
            text = fleet.session_start_context(data, env, run)
            self.assertIsNone(fleet.hook_session_event("session_start", data, env, run))
            self.assertEqual(clock.now - start, 6)
        self.assertEqual([limit for _, limit in calls], [6])
        self.assertEqual(self.log_file().read_bytes(), before)
        self.assertIn(f"this pane (w1:p1) is registered as the orchestrator of project app, but this hook {NOTE} "
                      "(git did not answer in time)", text)
        self.assertNotIn("Fleet lane", text)

    def test_hooks_when_git_times_out(self):
        """Each git call raises TimeoutExpired at the subprocess boundary, as when the deadline expires: no hook
        raises and nothing is recorded. A pane that an orch.json names gets the note, on every source and also in a
        checkout with lane files; with no orch.json it is a pane in no panes.json, as before."""
        self.orch_setup()
        d = self.repo / ".fleet"
        d.mkdir()
        (d / "BRIEF.md").write_text("brief")
        (d / "STATUS.md").write_text("status")
        env = {"HERDR_PANE_ID": "w1:p1", "CLAUDE_PLUGIN_ROOT": "/plugins/fleet"}
        log = self.log_file()  # orch_setup recorded events in it
        before = log.read_bytes()
        with patient_fleet.record_git_limits() as calls, patient_fleet.git_times_out():
            data = {"source": "clear", "cwd": str(self.repo)}
            self.assertEqual(fleet.orch_decide(data, [self.project], fleet.HookRun()),
                             (fleet.NOTE, fleet.HOOK_NO_ANSWER))
            self.assertIsNone(fleet.hook_session_event("session_start", data, env))
            texts = {source: fleet.session_start_context({**data, "source": source}, env)
                     for source in ("startup", "resume", "clear", "compact")}
            (self.project / "orch.json").rename(self.root / "orch.json")
            lane = fleet.session_start_context(data, env)
            (self.root / "orch.json").rename(self.project / "orch.json")
        self.assertTrue(calls)  # git was asked, and timed out each time
        self.assertEqual(log.read_bytes(), before)
        note = ("Fleet orchestrator: this pane (w1:p1) is registered as the orchestrator of project app, but this hook "
                "could not confirm that this session works in that project (git did not answer in time), so it gives "
                'no re-brief. If you orchestrate it, run `python3 "/plugins/fleet/scripts/fleet" digest --project app` '
                f'first, then read {self.project / "PROJECT.md"} and load the fleet:fleet skill.')
        self.assertEqual(texts, dict.fromkeys(texts, note))
        # With no orch.json naming the pane: the lane text, the same as when git answers.
        self.assertIn("This worktree has fleet lane files", lane)
        (self.project / "orch.json").unlink()
        self.assertEqual(lane, fleet.session_start_context(data, env))
        # Two projects name the pane: the note names both, and asks which.
        self.main("orch", "claim", pane="w1:p1")
        other = fleet.ensure_project(["local:/elsewhere"])
        fleet.write_json(other / "orch.json", {"pane": "w1:p1"})
        with patient_fleet.git_times_out():
            text = fleet.session_start_context(data, env)
        self.assertIn(f"orchestrator of one of the projects app, {other.name}, but", text)
        self.assertIn("digest --project NAME` first", text)

    def test_orch_outcomes(self):
        """A pane that orch candidates name has three outcomes: (a) the identity of a candidate's project, the full
        re-brief; (b) the identity of another project, the ordinary path and no note; (c) anything else, the note:
        a directory outside any repository too. A pane that no candidate names never gets the note."""
        self.orch_setup()
        plain = self.root / "plain"
        plain.mkdir()
        other = self.root / "other"
        other.mkdir()
        git("init", "-q", "-b", "main", cwd=other)
        git_dir = Path(shutil.which("git")).parent
        for source in ("startup", "compact"):
            # (c) outside any repository: the note, with git's answer as its cause.
            cp, asked, _ = self.hook_child("session-start", {"source": source, "cwd": str(plain)}, [git_dir])
            self.assertEqual((cp.returncode, cp.stderr, asked), (0, "", [6, 6]), source)  # the record asks again
            context = json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn(f"orchestrator of project app, but this hook {NOTE} (git found no repository here)",
                          context)
            # (b) another project's checkout: no note.
            cp, _, _ = self.hook_child("session-start", {"source": source, "cwd": str(other)}, [git_dir])
            self.assertEqual((cp.returncode, cp.stdout, cp.stderr), (0, "", ""), source)
            # A pane that no candidate names, outside any repository: no note.
            cp, _, _ = self.hook_child("session-start", {"source": source, "cwd": str(plain)}, [git_dir],
                                       HERDR_PANE_ID="w5:p5")
            self.assertEqual((cp.returncode, cp.stdout, cp.stderr), (0, "", ""), source)
        # (a) the project's checkout: the re-brief.
        cp, _, _ = self.hook_child("session-start", {"source": "startup", "cwd": str(self.repo)}, [git_dir])
        self.assertIn("you orchestrate project app (pane w1:p1)",
                      json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"])
        # In-process, the same outcomes.
        env = {"HERDR_PANE_ID": "w1:p1"}
        self.assertEqual(fleet.orch_candidates(env), ("w1:p1", [self.project]))
        self.assertEqual(fleet.orch_candidates({"HERDR_PANE_ID": "w5:p5"}), ("w5:p5", []))
        homes = [self.project]
        self.assertEqual(fleet.orch_decide({"cwd": str(plain)}, homes, fleet.HookRun()),
                         (fleet.NOTE, fleet.HOOK_NO_REPOSITORY))
        self.assertEqual(fleet.orch_decide({"cwd": str(other)}, homes, fleet.HookRun()), (fleet.ORDINARY, None))
        self.assertEqual(fleet.orch_decide({"cwd": str(self.repo)}, homes, fleet.HookRun()),
                         (fleet.REBRIEF, self.project))
        # project_home's refusal still tells "no repository" by the .git entries; the hook does not use that.
        with mock.patch.dict(os.environ, {"GIT_DIR": str(plain / "nowhere")}):
            self.assertFalse(fleet.no_git_entry(str(self.root)))
        self.assertTrue(fleet.no_git_entry(str(plain)))
        self.assertFalse(fleet.no_git_entry(str(self.repo / "src")))  # a parent has .git

    def test_git_errors_that_are_no_answer_give_the_note(self):
        """Through the real entry, with a registered orch pane and lane files: every failure of git that does not
        establish "no repository" gives the note, never the lane text: a malformed .git/config, exit code 3, exit
        code 128 inside a repository, a remote query that fails, a signal, and a git that cannot start."""
        self.orch_setup()
        d = self.repo / ".fleet"
        d.mkdir()
        (d / "BRIEF.md").write_text("brief")
        fleet.save_panes(d, {"w1:p1": {"role": "impl"}})
        real = shutil.which("git")
        cases = {"exit 3": 'echo "fatal: I/O failure" >&2; exit 3',
                 "exit 128 in a repository": 'echo "fatal: not a git repository" >&2; exit 128',
                 "config exit 3": f'case "$*" in *config*) exit 3;; esac; exec "{real}" "$@"',
                 "signal": "kill -TERM $$"}
        causes = {"exit 3": fleet.HOOK_GIT_ERROR, "exit 128 in a repository": fleet.HOOK_NO_REPOSITORY,
                  "config exit 3": fleet.HOOK_GIT_ERROR, "signal": fleet.HOOK_GIT_ERROR}
        for name, body in cases.items():
            bin_dir, _ = self.git_wrapper(body)
            cp, _, _ = self.hook_child("session-start", {"source": "compact", "cwd": str(self.repo)}, [bin_dir])
            self.assertEqual((cp.returncode, cp.stderr), (0, ""), name)
            context = json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn(f"orchestrator of project app, but this hook {NOTE} ({causes[name]})", context, name)
            self.assertNotIn("Fleet lane", context, name)
        # A git that cannot start: not executable (PermissionError), not a program (OSError).
        for name, text, mode in (("not executable", "#!/bin/sh\nexit 0\n", 0o644), ("not a program", "junk\n", 0o755)):
            bin_dir = self.root / name.replace(" ", "-")
            bin_dir.mkdir()
            (bin_dir / "git").write_text(text)
            (bin_dir / "git").chmod(mode)
            cp, _, _ = self.hook_child("session-start", {"source": "compact", "cwd": str(self.repo)}, [bin_dir])
            self.assertEqual((cp.returncode, cp.stderr), (0, ""), name)
            self.assertIn(f"{NOTE} (a git error)", json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"],
                          name)
        # A malformed configuration with the real git.
        config = self.repo / ".git" / "config"
        good = config.read_text()
        config.write_text("[broken\n")
        try:
            cp, _, _ = self.hook_child("session-start", {"source": "compact", "cwd": str(self.repo)},
                                       [Path(real).parent])
        finally:
            config.write_text(good)
        self.assertIn(NOTE, json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"])
        # A byte that is not UTF-8 in the origin URL: git answers, its output cannot be read.
        git("remote", "set-url", "origin", "git@github.com:acme/app.git", cwd=self.repo)
        raw = config.read_bytes()
        config.write_bytes(raw.replace(b"acme/app.git", b"acme/app-\xff.git"))
        try:
            for source in ("startup", "compact"):
                cp, _, _ = self.hook_child("session-start", {"source": source, "cwd": str(self.repo)},
                                           [Path(real).parent])
                self.assertEqual((cp.returncode, cp.stderr), (0, ""), source)
                self.assertIn(f"{NOTE} (a git error)",
                              json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"], source)
        finally:
            config.write_bytes(raw)
        # A checkout directory that cannot be searched, entered before it lost the right (git: "cannot change to
        # ...", exit 128); the child inherits it as its working directory.
        mode = self.repo.stat().st_mode & 0o777
        with chdir(self.repo):
            self.repo.chmod(0o600)
            try:
                for source in ("startup", "compact"):
                    cp, _, _ = self.hook_child("session-start", {"source": source, "cwd": str(self.repo)},
                                               [Path(real).parent], cwd=None)
                    self.assertEqual((cp.returncode, cp.stderr), (0, ""), source)
                    self.assertIn(f"{NOTE} (git found no repository here)",
                                  json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"], source)
            finally:
                self.repo.chmod(mode)
        cp, _, _ = self.hook_child("session-start", {"source": "compact", "cwd": str(self.repo)}, [Path(real).parent])
        self.assertIn("you orchestrate project app", json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_a_failure_is_not_kept_while_time_is_left(self):
        """A remote query that fails once: the re-brief gives the note, and the session record asks git again and
        records; a failed remote query never counts as an identity without remotes."""
        self.orch_setup()
        (self.repo / ".fleet").mkdir()
        once = self.root / "failed-once"
        real = shutil.which("git")
        bin_dir, calls = self.git_wrapper(
            f'case "$*" in *config*) if [ ! -e "{once}" ]; then : > "{once}"; exit 3; fi;; esac; exec "{real}" "$@"')
        cp, asked, _ = self.hook_child("session-start", {"source": "compact", "cwd": str(self.repo),
                                                         "session_id": "s-7"}, [bin_dir])
        self.assertIn(NOTE, json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(asked, [6] * 5)  # identity (failed), identity again, branch
        e = self.events("session_start")[-1]
        self.assertEqual((e["session"], e["project"], e["branch"]), ("s-7", "app", "main"))
        # In-process: the failure is not kept, a found identity is.
        run = fleet.HookRun()
        with patient_fleet.git_times_out(), self.assertRaises(fleet.HookGitFailed):
            run.identity(str(self.repo))
        self.assertEqual(run.identity(str(self.repo))["top"], str(self.repo))
        with patient_fleet.git_times_out():
            self.assertEqual(run.identity(str(self.repo))["top"], str(self.repo))  # kept: no git call
        # Once the deadline is spent, no git starts at all.
        spent = fleet.HookRun(start=patient_fleet.HOOK_CLOCK.now - 6)
        with patient_fleet.record_git_limits() as limits, self.assertRaises(fleet.HookGitFailed):
            spent.identity(str(self.repo))
        self.assertEqual(limits, [])

    def test_lane_files_from_the_confirmed_top(self):
        """A lane session in a subdirectory, with no orch.json naming its pane: rev-parse finds the top, the remote
        query fails. The lane re-brief still comes from the top's .fleet/; nothing is recorded (no identity)."""
        d = self.repo / ".fleet"
        d.mkdir()
        (d / "BRIEF.md").write_text("brief")
        fleet.save_panes(d, {"w1:p1": {"role": "impl"}})
        sub = self.repo / "src"
        sub.mkdir()
        real = shutil.which("git")
        bin_dir, _ = self.git_wrapper(f'case "$*" in *config*) exit 3;; esac; exec "{real}" "$@"')
        before = self.events()
        cp, _, _ = self.hook_child("session-start", {"source": "compact", "cwd": str(sub)}, [bin_dir])
        context = json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"You implement this lane (pane w1:p1). Before you act, read {d / 'BRIEF.md'}", context)
        self.assertEqual(self.events(), before)
        # The orch match stays strict: with an orch.json naming the pane, the same failure gives the note.
        self.orch_setup()
        cp, _, _ = self.hook_child("session-start", {"source": "compact", "cwd": str(sub)}, [bin_dir])
        self.assertIn(NOTE, json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_hook_entry_without_git_gives_the_note(self):
        """The real entry with no git on PATH: git cannot run, which is no answer; the orch pane gets the note in the
        hook's JSON, at once, and the hook ends cleanly. The session record asks git once more, and gives up."""
        self.orch_setup()
        cp, asked, _ = self.hook_child("session-start", {"source": "compact", "cwd": str(self.repo)})
        self.assertEqual((cp.returncode, cp.stderr, asked), (0, "", [6, 6]))
        context = json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"this pane (w1:p1) is registered as the orchestrator of project app, but this hook {NOTE} "
                      "(a git error)", context)
        cp, _, _ = self.hook_child("session-end", {"cwd": str(self.repo)})
        self.assertEqual((cp.returncode, cp.stdout, cp.stderr), (0, "", ""))

    def test_slow_git_in_the_real_entry(self):
        """The real entry in a child, with git at 2 s for each call on the fake clock: the limits shrink as git time
        passes (the product's own clock reads it), the orch pane gets its full re-brief, and the branch call,
        which would end after the deadline, expires at it."""
        self.orch_setup()
        (self.repo / ".fleet").mkdir()
        git_dir = Path(shutil.which("git")).parent
        payload = {"source": "compact", "cwd": str(self.repo), "session_id": "s-2"}
        cp, asked, _ = self.hook_child("session-start", payload, [git_dir], PATIENT_GIT_TAKES="2")
        self.assertEqual(asked, [6, 4, 2])
        self.assertIn("you orchestrate project app (pane w1:p1)",
                      json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.events("session_start")[-1]["branch"], "main")
        cp, asked, _ = self.hook_child("session-start", payload, [git_dir], PATIENT_GIT_TAKES="2.5")
        self.assertEqual(asked, [6, 3.5, 1])
        self.assertNotIn("branch", self.events("session_start")[-1])
        cp, asked, _ = self.hook_child("session-end", payload, [git_dir], PATIENT_GIT_TAKES="0.5")
        self.assertEqual(asked, [2, 1.5, 1])

    def test_the_record_waits_end_at_the_runs_end(self):
        """A held event log, and project names whose project.json never comes: the record's waits give up at the
        run's end time (the fake clock moves by each wait), it records nothing, and the hook still prints its
        re-brief. Commands other than the hooks keep their 1 s for each name."""
        import fcntl
        self.orch_setup()
        (self.repo / ".fleet").mkdir()
        data, env = {"source": "clear", "cwd": str(self.repo), "session_id": "s-3"}, {"HERDR_PANE_ID": "w1:p1"}
        log = self.log_file()
        before = log.read_bytes()
        fd = os.open(log, os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            clock = patient_fleet.HOOK_CLOCK
            for event, hook in (("session_start", "session-start"), ("session_end", "session-end")):
                run = fleet.HookRun(hook)
                start = clock.now
                self.assertIsNone(fleet.hook_session_event(event, data, env, run))
                self.assertAlmostEqual(clock.now - start, 20 * fleet.HOOK_WAIT_STEP)  # its own cap: 20 retries
                # With less time left than that, the retries stop at the run's end.
                run = fleet.HookRun(hook)
                run.identity(str(self.repo))  # found while there was time
                clock.now = run.end - 0.3  # the git deadline has passed: no branch
                with patient_fleet.record_git_limits() as limits:
                    self.assertIsNone(fleet.hook_session_event(event, data, env, run))
                self.assertEqual(limits, [])
                self.assertLessEqual(clock.now, run.end)
                self.assertGreater(clock.now, run.end - 2 * fleet.HOOK_WAIT_STEP)  # it waited up to the end
            # Through the real entry: the re-brief comes, the record does not, the child does not hang.
            cp, _, waits = self.hook_child("session-start", data, [Path(shutil.which("git")).parent])
            self.assertIn("you orchestrate project app", json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"])
            self.assertTrue(any(Path(w["file"]).resolve() == SCRIPT for w in waits))  # the record's lock waits
        finally:
            os.close(fd)
        self.assertEqual(log.read_bytes(), before)
        # With the log free but the run's end come, the record is skipped: it is best effort.
        for hook in ("session-start", "session-end"):
            run = fleet.HookRun(hook)
            run.identity(str(self.repo))
            clock.now = run.end
            self.assertIsNone(fleet.hook_session_event("session_end", data, env, run))
        self.assertEqual(log.read_bytes(), before)
        run = fleet.HookRun("session-end")  # and with time left, it records
        self.assertIsNotNone(fleet.hook_session_event("session_end", data, env, run))
        # Occupied names: folders without project.json for the first 11 names of a new project.
        other = self.root / "other"
        other.mkdir()
        git("init", "-q", "-b", "main", cwd=other)
        git("remote", "add", "origin", "git@github.com:acme/other.git", cwd=other)
        (other / ".fleet").mkdir()
        names = fleet.project_names("github.com/acme/other")[:11]
        for name in names:
            (self.home / "projects" / name).mkdir(parents=True)
        for event, hook in (("session_start", "session-start"), ("session_end", "session-end")):
            patient_fleet.HOOK_CLOCK.reset()
            run = fleet.HookRun(hook)
            self.assertIsNone(fleet.hook_session_event(event, {"cwd": str(other)}, env, run))
            self.assertLessEqual(patient_fleet.HOOK_CLOCK.now, run.end)
        self.assertEqual(sorted(p.name for p in (self.home / "projects").iterdir() if not (p / "project.json").exists()),
                         sorted(names))  # no second home was made for the keys
        # No time left at all: nothing waits, nothing is recorded.
        spent = fleet.HookRun("session-end", start=patient_fleet.HOOK_CLOCK.now - 3)
        self.assertIsNone(fleet.hook_session_event("session_end", data, env, spent))
        # A command: 1 s for each name, then the next free name.
        patient_fleet.HOOK_CLOCK.reset()
        home = fleet.ensure_project(["github.com/acme/other"])
        self.assertEqual(home.name, "other-11")
        self.assertAlmostEqual(patient_fleet.HOOK_CLOCK.now - patient_fleet.FakeClock.START, 11 * 20 * 0.05)

    def test_the_hook_entry_never_loses_the_orch_note(self):
        """The boundary of the hook entry: when anything raises between the candidate read and the output, a pane
        that orch candidates name gets the note; a pane that none names keeps today's behaviour (no output)."""
        self.orch_setup()
        git_dir = Path(shutil.which("git")).parent
        payload = {"source": "compact", "cwd": str(self.repo)}
        cp, _, _ = self.hook_child("session-start", payload, [git_dir], PATIENT_RAISE_IN="orch_context")
        self.assertEqual((cp.returncode, cp.stderr), (0, ""))
        self.assertIn(f"orchestrator of project app, but this hook {NOTE} (an error in the hook: RuntimeError)",
                      json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"])
        cp, _, _ = self.hook_child("session-start", payload, [git_dir], PATIENT_RAISE_IN="lane_context",
                                   HERDR_PANE_ID="w5:p5")
        self.assertEqual((cp.returncode, cp.stdout, cp.stderr), (0, "", ""))
        # In-process through main: the same, and a failing candidate read gives no note.
        for target, pane, want in (("orch_context", "w1:p1", NOTE), ("lane_context", "w5:p5", None)):
            buf = io.StringIO()
            with mock.patch.object(fleet, target, side_effect=RuntimeError("boom")), \
                    mock.patch.dict(os.environ, {"HERDR_PANE_ID": pane}), \
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(buf):
                self.assertEqual(fleet.main(["hook", "session-start"]), 0)
            self.assertEqual(want in buf.getvalue() if want else buf.getvalue(), True if want else "", pane)
        buf = io.StringIO()
        with mock.patch.object(fleet, "lane_context", side_effect=RuntimeError("boom")), \
                mock.patch.object(fleet, "orch_candidates", side_effect=OSError("unreadable")), \
                mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1"}), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(buf):
            self.assertEqual(fleet.main(["hook", "session-start"]), 0)
        self.assertEqual(buf.getvalue(), "")

    def test_one_snapshot_one_decision(self):
        """SessionStart reads the orch candidates once, before its input, and decides once against that snapshot,
        through the real entry on every source: a file that cannot be parsed drops no other candidate; a
        candidate's project.json that cannot be read gives the note, never the lane text."""
        self.orch_setup()
        d = self.repo / ".fleet"
        d.mkdir()
        (d / "BRIEF.md").write_text("brief")
        (d / "STATUS.md").write_text("status")
        fleet.save_panes(d, {"w1:p1": {"role": "impl"}})  # the lane text, if the pane were taken as no orch
        git_dir = Path(shutil.which("git")).parent
        deep = self.home / "projects" / "aaa-deep"  # read before app's orch.json
        deep.mkdir()
        (deep / "orch.json").write_text("[" * 10000 + "0" + "]" * 10000)
        with self.assertRaises(RecursionError):
            json.loads((deep / "orch.json").read_text())
        self.assertEqual(fleet.read_json(deep / "orch.json"), {})
        self.assertEqual(fleet.orch_candidates({"HERDR_PANE_ID": "w1:p1"}), ("w1:p1", [self.project]))
        real_read = fleet.read_json

        def read_fails_for_deep(path):  # each file is read alone, also when a read itself raises
            if Path(path).parent == deep:
                raise RecursionError("maximum recursion depth exceeded")
            return real_read(path)
        with mock.patch.object(fleet, "read_json", side_effect=read_fails_for_deep):
            self.assertEqual(fleet.orch_candidates({"HERDR_PANE_ID": "w1:p1"}), ("w1:p1", [self.project]))
        project_json = self.project / "project.json"
        good = project_json.read_text()
        for source in ("startup", "resume", "clear", "compact"):
            payload = {"source": source, "cwd": str(self.repo)}
            cp, _, _ = self.hook_child("session-start", payload, [git_dir])
            self.assertEqual((cp.returncode, cp.stderr), (0, ""), source)
            self.assertIn("you orchestrate project app (pane w1:p1)",
                          json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"], source)
            # app's project.json truncated: git still names the checkout, but the comparison cannot be made.
            project_json.write_text("{")
            try:
                cp, _, _ = self.hook_child("session-start", payload, [git_dir])
            finally:
                project_json.write_text(good)
            self.assertEqual((cp.returncode, cp.stderr), (0, ""), source)
            context = json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn(f"orchestrator of project app, but this hook {NOTE} (the project file of app could not be "
                          "read)", context, source)
            self.assertNotIn("You implement", context, source)
        # A project.json with no keys is no comparison either; another project's checkout with readable files is.
        other = self.root / "other"
        other.mkdir()
        git("init", "-q", "-b", "main", cwd=other)
        self.assertEqual(fleet.orch_decide({"cwd": str(other)}, [self.project], fleet.HookRun()),
                         (fleet.ORDINARY, None))
        fleet.write_json(project_json, {"name": "app"})
        self.assertEqual(fleet.orch_decide({"cwd": str(other)}, [self.project], fleet.HookRun()),
                         (fleet.NOTE, "the project file of app could not be read"))
        project_json.write_text(good)
        # One candidate read per SessionStart, before the input is read; none for SessionEnd.
        order = []
        real_candidates, real_input = fleet.orch_candidates, fleet.read_hook_input
        for event, pane, want in (("session-start", "w1:p1", ["candidates", "input"]),
                                  ("session-start", "w5:p5", ["candidates", "input"]),
                                  ("session-end", "w1:p1", ["input"])):
            order.clear()
            with mock.patch.object(fleet, "orch_candidates",
                                   side_effect=lambda env: order.append("candidates") or real_candidates(env)), \
                    mock.patch.object(fleet, "read_hook_input",
                                      side_effect=lambda: order.append("input") or real_input()), \
                    mock.patch.dict(os.environ, {"HERDR_PANE_ID": pane}), \
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"source": "compact",
                                                                            "cwd": str(self.repo)}))), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(fleet.main(["hook", event]), 0)
            self.assertEqual(order, want, (event, pane))

    def test_the_hook_input_and_the_boundary(self):
        """A pane that orch candidates name: input that cannot be read gives the note; SystemExit and
        KeyboardInterrupt after the candidate read give the note and exit 0; a note that cannot be built gives the
        fixed note. FakeClockLimit (the harness's failure) still ends the process. A pane that no candidate names
        keeps today's behaviour: no output."""
        self.orch_setup()
        git_dir = Path(shutil.which("git")).parent
        for raw in (b"{", b"[]", b"\xff{}", json.dumps({"source": "compact", "cwd": str(self.repo)}).encode()[:-1]):
            cp, _, _ = self.hook_child("session-start", None, [git_dir], raw=raw)
            self.assertEqual((cp.returncode, cp.stderr), (0, ""), raw)
            self.assertIn(f"orchestrator of project app, but this hook {NOTE} (the hook input could not be read)",
                          json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"], raw)
            cp, _, _ = self.hook_child("session-start", None, [git_dir], raw=raw, HERDR_PANE_ID="w5:p5")
            self.assertEqual((cp.returncode, cp.stdout, cp.stderr), (0, "", ""), raw)
        (self.root / "write-only").touch()
        unreadable = os.open(self.root / "write-only", os.O_WRONLY)  # stdin open for writing: a read raises OSError
        self.addCleanup(os.close, unreadable)
        cp, _, _ = self.hook_child("session-start", None, [git_dir], stdin=unreadable)
        self.assertEqual((cp.returncode, cp.stderr), (0, ""))
        self.assertIn(f"{NOTE} (the hook input could not be read)",
                      json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"])
        cp, _, _ = self.hook_child("session-start", None, [git_dir], stdin=unreadable, HERDR_PANE_ID="w5:p5")
        self.assertEqual((cp.returncode, cp.stdout, cp.stderr), (0, "", ""))
        plain = self.root / "plain"
        plain.mkdir()
        fixed = ("Fleet orchestrator: this pane (w1:p1) is registered as the orchestrator of app, but this hook could "
                 "not confirm that this session works in that project (git found no repository here), so it gives no "
                 "re-brief. If you orchestrate it, run the fleet helper's `digest` command first, then read the "
                 "project's PROJECT.md.")
        for source in ("startup", "resume", "clear", "compact"):
            payload = {"source": source, "cwd": str(self.repo)}
            for injection, cause in (("orch_rebrief:SystemExit", "an error in the hook: SystemExit"),
                                     ("identity_from:KeyboardInterrupt", "an error in the hook: KeyboardInterrupt")):
                cp, _, _ = self.hook_child("session-start", payload, [git_dir], PATIENT_RAISE_IN=injection)
                self.assertEqual((cp.returncode, cp.stderr), (0, ""), (source, injection))
                context = json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"]
                self.assertIn(f"orchestrator of project app, but this hook {NOTE} ({cause})", context)
            # The note builder fails: the fixed note.
            cp, _, _ = self.hook_child("session-start", {"source": source, "cwd": str(plain)}, [git_dir],
                                       PATIENT_RAISE_IN="orch_unconfirmed_note")
            self.assertEqual((cp.returncode, cp.stderr), (0, ""), source)
            self.assertEqual(json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"], fixed, source)
            # The harness's FakeClockLimit is no product failure: the child ends with it, and prints no note.
            cp, _, _ = self.hook_child("session-start", payload, [git_dir], PATIENT_RAISE_IN="orch_rebrief:FakeClockLimit")
            self.assertEqual((cp.returncode, cp.stdout), (1, ""), source)
            self.assertIn("\nFakeClockLimit: patient_fleet: injected in orch_rebrief", cp.stderr, source)
        self.assertEqual(fleet.orch_fixed_note([self.project], "w1:p1", fleet.HOOK_NO_REPOSITORY), fixed)

    def test_write_json_syncs_before_it_replaces(self):
        path = self.root / "data.json"
        fleet.write_json(path, {"a": 1})
        synced = []
        with mock.patch.object(fleet.os, "fsync", side_effect=lambda fd: synced.append(fd)) as fsync, \
                mock.patch.object(fleet.os, "replace", side_effect=lambda *a: synced.append("replace") or
                                  os.rename(*a)):
            fleet.write_json(path, {"a": 2})
        self.assertTrue(fsync.called)
        self.assertEqual(synced[-1], "replace")
        self.assertEqual(fleet.read_json(path), {"a": 2})
        with mock.patch.object(fleet.os, "replace", side_effect=OSError("disk")), self.assertRaises(OSError):
            fleet.write_json(path, {"a": 3})
        self.assertEqual((fleet.read_json(path), sorted(p.name for p in self.root.glob(".data.json*"))), ({"a": 2}, []))

    def test_parent_and_child_share_the_calendar(self):
        """At the last second of a month, a HELPER child names and locks the parent's month's log, not the next
        one: the child gets the parent's calendar and TZ (helper_env). Also with a parent TZ that is not the
        machine's zone, where the child would otherwise read the calendar's seconds as another day."""
        import calendar
        self.orch_setup()
        (self.repo / ".fleet").mkdir()
        last = time.strptime("2026-10-31 23:59:59", "%Y-%m-%d %H:%M:%S")
        machine = calendar.timegm(last) - time.mktime(last)  # the machine's (or the run's) UTC offset then
        self.month_boundary(last, "s-31")
        # A parent zone west of the machine's: in the machine's zone, the parent's last second of October is
        # already November. (On a machine at -12 h, the first second of November at +14 h is still October.)
        zone, when = ("Etc/GMT+12", last) if machine > -12 * 3600 else \
            ("Etc/GMT-14", time.strptime("2026-11-01 00:00:00", "%Y-%m-%d %H:%M:%S"))
        try:
            with mock.patch.dict(os.environ, {"TZ": zone}):
                time.tzset()
                self.assertNotEqual(calendar.timegm(when) - time.mktime(when), machine)
                self.month_boundary(when, "s-41")
                self.assertEqual(helper_env({"TZ": "UTC"})["TZ"], zone)
        finally:
            time.tzset()  # the run's own zone again
        if "TZ" not in os.environ:
            self.assertNotIn("TZ", helper_env({"TZ": "UTC"}))  # no TZ of its own: the child has none either
        self.assertEqual(helper_env({})["PATIENT_CALENDAR"], repr(time.mktime(self.clock.calendar)))
        self.clock.stopped = 1_700_000_000.0
        self.assertEqual(helper_env({})["PATIENT_TIME"], "1700000000.0")

    def month_boundary(self, last, session):
        """With the parent's calendar at `last` and its month's log locked, a child hook waits for that lock and does
        not write elsewhere; once the log is free, it records `session` there."""
        import fcntl
        self.clock.calendar = time.localtime(time.mktime(last))
        log = self.log_file()
        self.assertEqual(log.name, time.strftime("%Y-%m.jsonl", last))
        log.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(log, os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        git_dir = Path(shutil.which("git")).parent
        try:
            cp, _, waits = self.hook_child("session-start", {"source": "startup", "cwd": str(self.repo),
                                                             "session_id": session}, [git_dir])
        finally:
            os.close(fd)
        self.assertEqual(cp.returncode, 0)
        self.assertTrue(any(Path(w["file"]).resolve() == SCRIPT for w in waits), session)  # it met the lock
        self.assertFalse(any(session in f.read_text() for f in log.parent.glob("*.jsonl")))
        cp, _, _ = self.hook_child("session-start", {"source": "startup", "cwd": str(self.repo),
                                                     "session_id": session + "b"}, [git_dir])
        self.assertIn(f'"session":"{session}b"', log.read_text())  # the parent's month

    def test_a_hook_never_makes_a_second_home(self):
        """One name whose folder exists and whose project.json comes later (another process is writing it): the
        hook waits for it, within its end time, and records in that home; it never makes a home under the next
        name. When it does not come in time, nothing is recorded and no home is made."""
        other = self.root / "other"
        other.mkdir()
        git("init", "-q", "-b", "main", cwd=other)
        git("remote", "add", "origin", "git@github.com:acme/other.git", cwd=other)
        (other / ".fleet").mkdir()
        keys = ["github.com/acme/other", f"local:{other}"]
        folder = self.home / "projects" / "other"
        folder.mkdir(parents=True)
        writes = []
        real_sleep = fleet.time.sleep

        def writer_finishes(seconds):  # the other process writes project.json during the hook's wait, at 1.6 s
            real_sleep(seconds)
            if patient_fleet.HOOK_CLOCK.now - start >= 1.6 and not writes:
                fleet.write_json(folder / "project.json", {"name": "other", "keys": keys})
                writes.append(patient_fleet.HOOK_CLOCK.now)
        env = {"HERDR_PANE_ID": "w2:p1"}
        start = patient_fleet.HOOK_CLOCK.now
        run = fleet.HookRun("session-end")
        with mock.patch.object(fleet.time, "sleep", writer_finishes):
            self.assertIsNotNone(fleet.hook_session_event("session_end", {"cwd": str(other)}, env, run))
        self.assertTrue(writes)
        self.assertEqual(sorted(p.name for p in (self.home / "projects").iterdir()), ["app", "other"])
        self.assertEqual(fleet.read_events([folder])[-1]["event"], "session_end")
        # The writer never finishes: the hook gives up at its end, with no record and no second home.
        (folder / "project.json").unlink()
        (folder / "events").rename(self.root / "other-events")
        patient_fleet.HOOK_CLOCK.reset()
        run = fleet.HookRun("session-end")
        self.assertIsNone(fleet.hook_session_event("session_end", {"cwd": str(other)}, env, run))
        self.assertLessEqual(patient_fleet.HOOK_CLOCK.now, run.end)
        self.assertGreater(patient_fleet.HOOK_CLOCK.now, run.end - 2 * fleet.HOOK_WAIT_STEP)
        self.assertEqual(sorted(p.name for p in (self.home / "projects").iterdir()), ["app", "other"])
        # A command (no end time) keeps its 1 s for the name, then makes the next one.
        self.assertEqual(fleet.ensure_project(keys).name, "acme-other")

    def test_a_held_project_lock_in_a_child_is_busy(self):
        """In a child the fake clock moves with each sleep: a command that waits for a held project lock reports
        busy, without real waiting and without hanging."""
        import fcntl
        self.orch_setup()
        fd = os.open(self.project / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            cp = subprocess.run([*HELPER, "orch", "release"], cwd=self.repo, capture_output=True, text=True,
                                timeout=CHILD_LIMIT, env=helper_env({**os.environ, "FLEET_HOME": str(self.home),
                                                                         "HERDR_PANE_ID": "w1:p1"}))
        finally:
            os.close(fd)
        self.assertEqual(cp.returncode, 1)
        self.assertIn("project app is busy", cp.stderr)

    def test_project_home_refusals(self):
        """F6: the refusal tells "no repository", "a repository but not a working tree here", "a repository without a
        home yet" and "git failed" apart."""
        plain = self.root / "plain"
        plain.mkdir()
        fresh = self.root / "fresh"
        fresh.mkdir()
        git("init", "-q", "-b", "main", cwd=fresh)
        bare = self.root / "bare.git"
        bare.mkdir()
        git("init", "-q", "--bare", cwd=bare)
        broken = self.root / "broken"
        broken.mkdir()
        git("init", "-q", "-b", "main", cwd=broken)
        (broken / ".git" / "config").write_text("[broken\n")
        args = fleet.build_parser().parse_args(["orch", "show"])
        no_repo = ("this directory is in no git repository, so it is in no fleet project; run this in a checkout of "
                   "the project, or pass --project NAME")
        no_tree = ("this directory is in a git repository but not in a working tree (a bare repository, or inside "
                   ".git); run this in a checkout of the project, or pass --project NAME")
        no_home = (f"the repository {fresh} has no fleet project home yet: `fleet orch claim` (registers this pane "
                   "as its orchestrator) or `fleet board` creates it; or pass --project NAME")
        failed = ("git could not tell the project of this directory (a git call failed or timed out); try again, or "
                  "pass --project NAME")
        cases = ((plain, None, no_repo), (bare, None, no_tree), (self.repo / ".git", None, no_tree),
                 (fresh, None, no_home), (broken, None, failed), (self.repo, patient_fleet.git_times_out, failed))
        for where, scope, message in cases:
            with chdir(where), (scope() if scope else nullcontext()):
                with self.assertRaises(fleet.FleetError) as caught:
                    fleet.project_home(args, create=False)
            self.assertEqual(str(caught.exception), message, where)
        # Through the real entry: the same texts on stderr.
        for where, message in ((plain, no_repo), (bare, no_tree), (fresh, no_home), (broken, failed)):
            cp = subprocess.run([*HELPER, "orch", "show"], cwd=where, capture_output=True, text=True,
                                timeout=CHILD_LIMIT, env=helper_env({**os.environ, "FLEET_HOME": str(self.home)}))
            self.assertEqual((cp.returncode, cp.stderr), (1, f"fleet: {message}\n"), where)
        self.assertEqual(fleet.project_dirs(), [self.project])  # no refusal created a home
        with chdir(fresh):  # the command that the refusal names creates the home
            self.assertEqual(fleet.project_home(args).name, "fresh")
        # follow-up gives the same refusal in its JSON.
        buf = io.StringIO()
        with chdir(plain), redirect_stdout(buf):
            self.assertEqual(fleet.main(["follow-up", "list"]), 2)
        self.assertEqual(json.loads(buf.getvalue())["refused"], no_repo)


class PatientFleetTests(FleetTestCase):
    """The test harness's boundary (patient_fleet.py): its scopes nest, and a broken git limit expires at once."""

    GIT = ["git", "--version"]

    def test_scopes_nest(self):
        with patient_fleet.record_git_limits() as outer:
            with patient_fleet.record_git_limits() as inner:
                patient_fleet.patient_run(self.GIT, capture_output=True, timeout=5)
            patient_fleet.patient_run(self.GIT, capture_output=True, timeout=6)
        self.assertEqual(([t for _, t in outer], [t for _, t in inner]), ([5, 6], [5]))
        self.assertEqual(patient_fleet._records, [])
        with patient_fleet.git_times_out():
            with patient_fleet.git_times_out():
                pass
            with self.assertRaises(subprocess.TimeoutExpired):  # the outer scope is still open
                patient_fleet.patient_run(self.GIT, capture_output=True, timeout=5)
        self.assertEqual(patient_fleet.patient_run(self.GIT, capture_output=True, timeout=5).returncode, 0)

    def test_a_limit_below_the_smallest_expires_at_once(self):
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            patient_fleet.patient_run(self.GIT, capture_output=True, timeout=0)
        self.assertIn("below the smallest product limit, 0.25 s", caught.exception.output)
        with self.assertRaises(subprocess.TimeoutExpired):
            patient_fleet.patient_run(self.GIT, capture_output=True, timeout=0.2)
        self.assertEqual(patient_fleet.patient_run(self.GIT, capture_output=True, timeout=0.25).returncode, 0)
        self.assertEqual(patient_fleet.patient_run(self.GIT, capture_output=True).returncode, 0)  # no limit


    def test_git_takes_moves_only_the_hook_clock(self):
        clock = patient_fleet.HOOK_CLOCK
        start = clock.now
        self.assertIs(fleet.time, patient_fleet.FAKE_TIME)  # the product's own hook_clock reads it
        self.assertEqual((fleet.HOOK_STARTED, fleet.hook_clock()), (patient_fleet.FakeClock.START, start))
        with patient_fleet.git_takes(2):
            patient_fleet.patient_run(self.GIT, capture_output=True, timeout=5)
            self.assertEqual(clock.now, start + 2)
            with patient_fleet.git_takes(3):  # the inner scope counts; a call that asks for less expires at its limit
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    patient_fleet.patient_run(self.GIT, capture_output=True, timeout=1)
                self.assertIn("git takes 3 s, the helper asked for 1 s", caught.exception.output)
                self.assertEqual(clock.now, start + 3)
            self.assertEqual(clock.now, start + 2)  # each scope puts back the clock it found
            self.assertEqual(patient_fleet.patient_run(["true"], capture_output=True).returncode, 0)  # not git
            self.assertEqual(clock.now, start + 2)
        self.assertEqual((clock.now, patient_fleet._git_takes), (start, []))  # put back
        patient_fleet.patient_run(self.GIT, capture_output=True, timeout=5)
        self.assertEqual(clock.now, start)  # stands still outside the scope
        fleet.time.sleep(0.3)  # a sleep of the helper moves it, at once
        self.assertEqual((clock.now, fleet.hook_clock()), (start + 0.3, start + 0.3))
        clock.reset()
        self.assertEqual(clock.now, patient_fleet.FakeClock.START)
        self.assertIs(time.monotonic, patient_fleet._real_monotonic)  # the tests' own clock stays real

    def test_an_injection_that_never_fires_fails_the_child(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        script = Path(tmp.name) / "script.py"
        script.write_text("import sys\n\ndef target():\n    return 0\n\ntry:\n    target()\nexcept Exception:\n"
                          "    pass\nsys.exit(int(sys.argv[1]))\n")
        for injection, code, want in (("target", "0", 0), ("target:SystemExit", "0", 7), ("nothing", "0", 97),
                                      ("nothing", "3", 97), (None, "3", 3)):
            env = {k: v for k, v in os.environ.items() if k != "PATIENT_RAISE_IN"}
            if injection:
                env["PATIENT_RAISE_IN"] = injection
            cp = subprocess.run([*HELPER[:2], str(script), code], capture_output=True, text=True, timeout=60, env=env)
            self.assertEqual(cp.returncode, want, (injection, cp.stderr))
            self.assertEqual("never fired" in cp.stderr, want == patient_fleet.NEVER_FIRED, injection)
            if want == patient_fleet.NEVER_FIRED:  # also when the script ends with its own SystemExit
                self.assertIn("patient_fleet: PATIENT_RAISE_IN=nothing never fired", cp.stderr)

    def test_a_wait_without_end_fails_instead_of_hanging(self):
        clock, limit = patient_fleet.HOOK_CLOCK, patient_fleet.FakeClock.LIMIT
        self.assertFalse(issubclass(patient_fleet.FakeClockLimit, Exception))  # `except Exception` lets it through
        fleet.time.sleep(limit)  # up to the limit: a sleep as any other
        self.assertEqual(clock.now - patient_fleet.FakeClock.START, limit)
        with self.assertRaises(patient_fleet.FakeClockLimit) as caught:
            try:
                fleet.time.sleep(0.05)
            except Exception:  # noqa: BLE001 - as the helper's own handlers
                self.fail("an `except Exception` caught FakeClockLimit")
        self.assertIn("past its limit of 300.0 s: a wait without end", str(caught.exception))
        clock.reset()  # each test starts below the limit again (FleetTestCase.run)
        with self.assertRaises(patient_fleet.FakeClockLimit):
            clock.sleep(limit + 1)  # the child's time.sleep, too
        clock.reset()
        # In a child: a script that waits without end, and catches every Exception, ends with the traceback.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        spin = Path(tmp.name) / "spin.py"
        spin.write_text("import time\nwhile True:\n    try:\n        time.sleep(0.05)\n    except Exception:\n"
                        "        pass\n")
        cp = subprocess.run([*HELPER[:2], str(spin)], capture_output=True, text=True, timeout=120)
        self.assertEqual(cp.returncode, 1, cp.stderr)
        self.assertIn("\nFakeClockLimit: patient_fleet: the helper slept the fake clock to 300.05 s", cp.stderr)


class FollowUpTests(FleetTestCase):
    """fleet follow-up, the cleanup close and the board's IDs, in a temporary FLEET_HOME. herdr and gh never run."""

    HAND = ("# Project: app\n\nThe memory.\n\n## Rules\n- Never push to main.\n\n## Follow-ups\n\n"
            "- Hand-written one\n  with a continuation line\n* Star bullet (lane: feat/x)   \nSome prose, no item\n"
            "- [F5] Has an ID\n\n## Notes\n- a note\n\n## Follow-ups\n- In the second section")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.home = self.root / "home"
        self.patch = mock.patch.dict(os.environ, {"FLEET_HOME": str(self.home)})
        self.patch.start()
        self.repo = self.root / "app"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("remote", "add", "origin", "git@github.com:acme/app.git", cwd=self.repo)
        git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init",
            cwd=self.repo)
        self.project = fleet.project_for(str(self.repo))
        self.md = self.project / "PROJECT.md"

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def main(self, *argv, cwd=None):
        buf = io.StringIO()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("HERDR_") and k not in ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE")}
        with mock.patch.dict(os.environ, env, clear=True), chdir(cwd or self.repo), redirect_stdout(buf):
            code = fleet.main(list(argv))
        return code, buf.getvalue()

    def fu(self, *argv, code=0, cwd=None):
        got, out = self.main("follow-up", *argv, cwd=cwd)
        self.assertEqual(got, code, out)
        return json.loads(out) if out.startswith("{") else out

    def write(self, text, mode=0o600):
        self.md.write_bytes(text.encode())
        os.chmod(self.md, mode)

    def text(self):
        return self.md.read_bytes().decode()

    def events(self, kind=None):
        return [e for e in fleet.read_events([self.project]) if kind is None or e["event"] == kind]

    # format -----------------------------------------------------------

    def test_template_placeholder_goes_and_comes_back(self):
        self.write(fleet.PROJECT_TEMPLATE.format(name="app"))
        before = self.text()
        res = self.fu("add", "Add a CHANGELOG entry.")
        self.assertEqual((res["id"], res["changed"], res["text"]), ("F1", "added", "Add a CHANGELOG entry."))
        self.assertEqual(self.text(), before.replace("## Follow-ups\n- none\n", "## Follow-ups\n- [F1] Add a CHANGELOG entry.\n"))
        self.assertEqual(self.fu("add", "Second", "--source", "feat/a REVIEW-1.md")["id"], "F2")
        self.assertIn("- [F1] Add a CHANGELOG entry.\n- [F2] Second (from feat/a REVIEW-1.md)\n\n## Notes", self.text())
        self.fu("done", "F1")
        self.fu("done", "f2")
        self.assertEqual(self.text(), before)  # the last done brings `- none` back where the item was
        self.assertEqual(oct(self.md.stat().st_mode & 0o777), oct(0o600))
        self.assertEqual(sorted(p.name for p in self.project.iterdir() if p.name.endswith(".tmp")), [])

    def test_hand_written_lines_get_ids_and_every_other_byte_stays(self):
        self.write(self.HAND, mode=0o640)
        before = self.HAND.split("\n")
        res = self.fu("list", "--json")
        self.assertEqual(res["changed"], "ids")
        self.assertEqual(res["assigned"], ["F6", "F7", "F8"])
        self.assertEqual((res["sections"], res["other_lines"]), (2, 1))
        self.assertEqual([(x["id"], x["text"], x["lane"]) for x in res["follow_ups"]],
                         [("F6", "Hand-written one", None), ("F7", "Star bullet", "feat/x"),
                          ("F5", "Has an ID", None), ("F8", "In the second section", None)])
        self.assertEqual(res["follow_ups"][0]["detail"], ["with a continuation line"])
        after = self.text().split("\n")
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(len(after), len(before))
        self.assertEqual([after[i] for i in changed], ["- [F6] Hand-written one", "* [F7] Star bullet (lane: feat/x)   ",
                                                        "- [F8] In the second section"])
        self.assertFalse(self.text().endswith("\n"))  # no final newline before, none after
        self.assertEqual(oct(self.md.stat().st_mode & 0o777), oct(0o640))
        # A second list changes nothing and records nothing.
        n = len(self.events())
        self.assertEqual(self.fu("list", "--json")["changed"], "nothing")
        self.assertEqual(len(self.events()), n)
        self.assertEqual([e["id"] for e in self.events("follow_up_added")], ["F6", "F7", "F8"])
        # add goes after the last item of the first section; take, release and done change only their lines.
        base = self.text().split("\n")
        self.assertEqual(self.fu("add", "New one")["id"], "F9")
        want = base[:base.index("- [F5] Has an ID") + 1] + ["- [F9] New one"] + base[base.index("- [F5] Has an ID") + 1:]
        self.assertEqual(self.text().split("\n"), want)
        self.fu("take", "6", "--lane", "feat/a")
        self.assertIn("- [F6] Hand-written one (lane: feat/a)\n  with a continuation line\n", self.text())
        res = self.fu("done", "F6", "--reason", "fixed in feat/a")
        self.assertEqual((res["changed"], res["lane"]), ("done", "feat/a"))
        self.fu("release", "F7")
        want = [("* [F7] Star bullet   " if l.startswith("* [F7]") else l) for l in want
                if not l.startswith("- [F6]") and l != "  with a continuation line"]
        self.assertEqual(self.text().split("\n"), want)
        done = self.events("follow_up_done")[-1]
        self.assertEqual((done["id"], done["text"], done["branch"], done["reason"], done["detail"]),
                         ("F6", "Hand-written one", "feat/a", "fixed in feat/a", "  with a continuation line"))
        text = self.main("follow-up", "list")[1]
        self.assertIn("F7  Star bullet\n", text)
        self.assertIn("2 ## Follow-ups sections", text)
        self.assertIn("1 line under ## Follow-ups is no list item", text)

    def test_ids_are_never_used_twice(self):
        self.write(fleet.PROJECT_TEMPLATE.format(name="app"))
        self.assertEqual([self.fu("add", t)["id"] for t in ("a", "b")], ["F1", "F2"])
        self.fu("done", "F2")
        self.assertEqual(self.fu("add", "c")["id"], "F3")  # F2 is gone from the file, not from the log
        self.write(self.text().replace("- [F3] c\n", "- [F3] c\n- by hand\n"))
        self.assertEqual(self.fu("list", "--json")["assigned"], ["F4"])
        self.write(self.text().replace("- [F4] by hand\n", ""))  # the orchestrator deletes it by hand
        self.assertEqual(self.fu("add", "d")["id"], "F5")
        # An ID in any section and any list item counts, also one the parser does not see as a follow-up.
        self.write(self.text() + "\n## Notes\n- [F40] an old note\n")
        self.assertEqual(self.fu("add", "e")["id"], "F41")

    def test_no_section_crlf_and_empty_file(self):
        self.write("# Project: app\n\n## Rules\n- none\n")
        self.fu("add", "x")
        self.assertEqual(self.text(), "# Project: app\n\n## Rules\n- none\n\n## Follow-ups\n- [F1] x\n")
        self.write("# Project: app")
        self.fu("add", "y")
        self.assertEqual(self.text(), "# Project: app\n\n## Follow-ups\n- [F2] y")  # still no final newline
        self.write("")
        self.fu("add", "z")
        self.assertEqual(self.text(), "## Follow-ups\n- [F3] z\n")
        self.write("# P\r\n\r\n## Follow-ups\r\n- none\r\n\r\n## Notes\r\n- n\r\n")
        self.fu("add", "w")
        self.fu("take", "F4", "--lane", "feat/w")
        self.assertEqual(self.text(), "# P\r\n\r\n## Follow-ups\r\n- [F4] w (lane: feat/w)\r\n\r\n## Notes\r\n- n\r\n")
        self.fu("done", "F4")
        self.assertEqual(self.text(), "# P\r\n\r\n## Follow-ups\r\n- none\r\n\r\n## Notes\r\n- n\r\n")

    def test_crlf_without_a_final_newline(self):
        start = b"# P\r\n## Follow-ups\r\n- [F1] Existing"
        self.md.write_bytes(start)
        self.fu("add", "New")
        self.assertEqual(self.md.read_bytes(), b"# P\r\n## Follow-ups\r\n- [F1] Existing\r\n- [F2] New")
        self.fu("take", "F2", "--lane", "feat/n")
        self.assertEqual(self.md.read_bytes(), b"# P\r\n## Follow-ups\r\n- [F1] Existing\r\n- [F2] New (lane: feat/n)")
        self.fu("release", "F2")
        self.assertEqual(self.md.read_bytes(), b"# P\r\n## Follow-ups\r\n- [F1] Existing\r\n- [F2] New")
        self.fu("done", "F2")
        self.assertEqual(self.md.read_bytes(), start)
        self.fu("done", "F1")
        self.assertEqual(self.md.read_bytes(), b"# P\r\n## Follow-ups\r\n- none")

    def test_continuation_across_blank_lines_and_indentation(self):
        self.write("## Follow-ups\n- [F1] Fix parser\n  first paragraph\n\n  second paragraph\n  - nested requirement\n"
                   "\n- [F2] Keep this\n  if ready:\n      run_task()\n\n## Notes\n- n\n")
        items = {x["id"]: x for x in self.fu("list", "--json")["follow_ups"]}
        self.assertEqual(items["F1"]["detail"], ["first paragraph", "", "second paragraph", "- nested requirement"])
        self.assertEqual(items["F2"]["detail"], ["if ready:", "    run_task()"])  # only the common indent goes
        self.fu("done", "F1")
        self.assertEqual(self.text(), "## Follow-ups\n\n- [F2] Keep this\n  if ready:\n      run_task()\n\n## Notes\n- n\n")
        self.assertEqual(self.events("follow_up_done")[-1]["detail"],
                         "  first paragraph\n\n  second paragraph\n  - nested requirement")
        self.fu("done", "F2")
        self.assertEqual(self.events("follow_up_done")[-1]["detail"], "  if ready:\n      run_task()")
        self.assertEqual(self.text(), "## Follow-ups\n\n- none\n\n## Notes\n- n\n")
        # A blank line before text at the bullet's indent ends the item.
        self.write("## Follow-ups\n- [F3] a\n\nprose\n  indented after prose\n")
        res = self.fu("list", "--json")
        self.assertNotIn("detail", res["follow_ups"][0])
        self.assertEqual(res["other_lines"], 2)

    def test_the_counter(self):
        self.write(fleet.PROJECT_TEMPLATE.format(name="app"))
        counter = self.project / "follow-ups.json"
        self.assertEqual([self.fu("add", t)["id"] for t in ("a", "b")], ["F1", "F2"])
        self.assertEqual(json.loads(counter.read_text()), {"next": 3})
        self.fu("done", "F2")
        self.assertEqual(json.loads(counter.read_text()), {"next": 3})  # done gives no ID
        # Without the counter, the start comes from PROJECT.md and the log, read strictly.
        counter.unlink()
        log = next((self.project / "events").glob("*.jsonl"))
        saved = log.read_bytes()
        log.unlink()
        log.mkdir()  # a log that cannot be read, also by root
        before = self.text()
        res = self.fu("add", "c", code=2)
        self.assertIn("cannot read the event log", res["refused"])
        self.assertEqual((res["changed"], self.text(), counter.exists()), ("nothing", before, False))
        log.rmdir()
        log.write_bytes(saved + b'{"event":"follow_up_added","id":"F7","text":"dam')  # a damaged line counts
        self.assertEqual(self.fu("add", "c")["id"], "F8")
        # With the counter, the log is not needed.
        log.unlink()
        log.mkdir()
        self.assertEqual(self.fu("add", "d")["id"], "F9")
        log.rmdir()
        # A hand-written higher ID moves it up; nothing moves it down.
        self.write(self.text().replace("- [F9] d\n", "- [F9] d\n- [F20] by hand\n"))
        self.assertEqual(self.fu("add", "e")["id"], "F21")
        counter.write_text('{"next": 5}')
        self.assertEqual(self.fu("add", "f")["id"], "F22")
        self.assertEqual(json.loads(counter.read_text()), {"next": 23})
        # A broken counter refuses every write and changes nothing; a no-op does not read it.
        self.fu("take", "F21", "--lane", "feat/x")
        for broken in ('{"next": "x"}', "not json", '{"next": 0}', "[]"):
            counter.write_text(broken)
            before, n = self.text(), len(self.events())
            for argv in (("add", "g"), ("done", "F22"), ("release", "F21"), ("take", "F22", "--lane", "feat/y")):
                res = self.fu(*argv, code=2)
                self.assertIn("follow-ups.json", res["refused"], argv)
            self.assertEqual((self.text(), counter.read_text(), len(self.events())), (before, broken, n))
            self.assertEqual(self.fu("take", "F21", "--lane", "feat/x")["changed"], "nothing")

    def test_done_moves_the_counter_past_the_ids_it_removes(self):
        self.write(fleet.PROJECT_TEMPLATE.format(name="app"))
        counter = self.project / "follow-ups.json"
        self.assertEqual(self.fu("add", "a")["id"], "F1")
        self.assertEqual(json.loads(counter.read_text()), {"next": 2})
        # A hand-numbered higher line, removed by done before any add: its ID stays used.
        self.write(self.text().replace("- [F1] a\n", "- [F1] a\n- [F2] Hand-written numbered work\n"))
        self.fu("done", "F2")
        self.assertEqual(json.loads(counter.read_text()), {"next": 3})
        self.assertEqual(self.fu("add", "b")["id"], "F3")
        # A migrated file without a counter, whose done event does not land: the counter still holds the ID.
        counter.unlink()
        self.write(self.text().replace("- [F3] b\n", "- [F3] b\n- [F9] migrated\n"))
        with mock.patch.object(fleet, "record", return_value=None):
            self.fu("done", "F9")
        self.assertEqual(json.loads(counter.read_text()), {"next": 10})
        self.assertEqual(self.fu("add", "c")["id"], "F10")

    def test_the_last_id(self):
        self.write("## Follow-ups\n- [F999999998] near the end\n")
        self.assertEqual(self.fu("add", "last")["id"], "F999999999")
        self.assertEqual(self.fu("done", "F999999999")["changed"], "done")
        before, events = self.text(), len(self.events())
        self.assertIn("past F999999999", self.fu("add", "one more", code=2)["refused"])
        self.write(before + "- by hand\n")
        before = self.text()
        self.assertIn("past F999999999", self.fu("list", code=2)["refused"])
        self.assertEqual((self.text(), len(self.events())), (before, events))
        self.assertEqual(json.loads((self.project / "follow-ups.json").read_text()), {"next": 1000000000})

    # actions and refusals ---------------------------------------------

    def test_take_release_and_refusals(self):
        self.write(fleet.PROJECT_TEMPLATE.format(name="app"))
        self.fu("add", "Fix the flaky test.")
        res = self.fu("take", "F1", "--lane", "feat/a")
        self.assertEqual((res["changed"], res["lane"]), ("taken", "feat/a"))
        self.write(self.text().replace("(lane: feat/a)\n", "(lane: feat/a)\n- by hand\n"))
        before, n = self.text(), len(self.events())
        self.assertEqual(self.fu("take", "F1", "--lane", "feat/a")["changed"], "nothing")  # the same lane
        self.assertEqual((self.text(), len(self.events())), (before, n))
        res = self.fu("take", "F1", "--lane", "feat/b", code=2)
        self.assertEqual((res["changed"], res["held_by"]), ("nothing", "feat/a"))
        self.assertEqual(len(self.events()), n)
        self.assertEqual(self.fu("release", "F1")["changed"], "released")
        self.assertIn("- [F2] by hand\n", self.text())  # a write gives the other lines their IDs
        before, n = self.text(), len(self.events())
        self.write(before.replace("- [F2] by hand\n", "- by hand again\n"))
        before = self.text()
        self.assertEqual(self.fu("release", "F1")["changed"], "nothing")
        self.assertEqual((self.text(), len(self.events())), (before, n))
        self.write(before.replace("- by hand again\n", ""))
        self.assertEqual(self.fu("take", "F1", "--lane", "feat/b")["lane"], "feat/b")
        before = self.text()
        for argv in (("done", "F9"), ("take", "x7", "--lane", "feat/a"), ("take", "F1", "--lane", "a b"),
                     ("add", "   "), ("add", "Ends like a mark (lane: feat/z)")):
            self.assertEqual(self.fu(*argv, code=2)["changed"], "nothing", argv)
        self.write(before.replace("(lane: feat/b)\n", "(lane: feat/b)\n- [F1] a copy\n"))
        self.assertIn("on 2 lines", self.fu("done", "F1", code=2)["refused"])
        self.write(before)
        self.assertEqual(self.text(), before)
        # Missing PROJECT.md: list is empty, a write refuses.
        self.md.unlink()
        res = self.fu("list", "--json")
        self.assertEqual((res["ok"], res["follow_ups"], res["note"]), (True, [], fleet.NO_PROJECT_MD))
        self.assertEqual(self.fu("add", "x", code=2)["refused"], fleet.NO_PROJECT_MD)
        self.assertFalse(self.md.exists())
        # Outside a project: a refusal; --project NAME finds it from anywhere; a wrong name is an error.
        outside = self.root / "outside"
        outside.mkdir()
        self.assertEqual(self.fu("list", cwd=outside, code=2)["changed"], "nothing")
        self.write(before)
        self.assertEqual(self.fu("list", "--json", "--project", "app", cwd=outside)["follow_ups"][0]["id"], "F1")
        self.assertEqual(self.main("follow-up", "list", "--project", "nope", cwd=outside)[0], 1)
        # A file that is not UTF-8 is never written.
        self.md.write_bytes(b"## Follow-ups\n- caf\xe9\n")
        self.assertEqual(self.main("follow-up", "add", "x")[0], 1)
        self.assertEqual(self.main("follow-up", "list")[0], 1)
        self.assertEqual(self.md.read_bytes(), b"## Follow-ups\n- caf\xe9\n")

    def test_events_in_the_log(self):
        self.write(fleet.PROJECT_TEMPLATE.format(name="app"))
        self.fu("add", "Write docs.", "--source", "feat/a REVIEW-2.md")
        self.fu("take", "F1", "--lane", "feat/b")
        self.fu("release", "F1")
        self.fu("take", "F1", "--lane", "feat/b")
        self.fu("done", "F1", "--reason", "merged")
        kinds = [e["event"] for e in self.events() if e["event"].startswith("follow_up_")]
        self.assertEqual(kinds, ["follow_up_added", "follow_up_taken", "follow_up_released", "follow_up_taken",
                                 "follow_up_done"])
        self.assertTrue(set(fleet.FOLLOW_UP_EVENTS) <= set(fleet.LOG_EVENTS))
        out = self.main("log", "--lane", "feat/b")[1]
        self.assertIn('F1 taken by feat/b: "Write docs. (from feat/a REVIEW-2.md)"', out)
        self.assertIn('F1 released from feat/b', out)
        self.assertIn('F1 done in feat/b: merged', out)
        self.assertNotIn("follow_up_added", out)
        out = self.main("log", "--event", "follow_up_added")[1]
        self.assertIn('F1 "Write docs. (from feat/a REVIEW-2.md)"', out)

    # cleanup ----------------------------------------------------------

    def commit(self, path, msg="x"):
        git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", msg, cwd=path)

    def make_lane(self, name="lane-a"):
        path = self.root / name
        git("worktree", "add", "-q", "-b", name, str(path), cwd=self.repo)
        return path

    def facts(self, path, pr=None):
        stub = {"workspace": None, "label": None, "path": str(path), "panes": []}
        stub.update(fleet.repo_paths(str(path)))
        return fleet.finish_lane(fleet.lane_facts(stub, None, str(self.root), lambda p, b: (pr, None),
                                                  lambda p, pr: None))

    def head(self, path):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True).stdout.strip()

    def held(self, extra=""):
        self.write("# Project: app\n\n## Follow-ups\n- [F3] Do a (lane: lane-a)\n  if ready:\n      run_task()\n"
                   "- [F4] Do b (lane: lane-b)\n- hand-written (lane: lane-a)\n" + extra + "\n## Notes\n- none\n")

    def merged_lane(self, name="lane-a"):
        """A lane with a commit of its own, merged into main."""
        path = self.make_lane(name)
        self.commit(path)
        git("merge", "-q", "--no-ff", "-m", "merge", name, cwd=self.repo)
        return path

    def clean(self, f):
        """The dry run's plan and the cleanup's result for lane `f`; they must agree."""
        plan = fleet.cleanup_plan(f)["follow_ups"]
        out = fleet.clean_lane(f, 5)
        done = "close" if out["follow_ups_closed"] else "release" if out["follow_ups_released"] else None
        self.assertEqual(list(plan), [done] if done else [])
        return out

    def test_cleanup_closes_the_follow_ups_of_a_merged_lane_that_moved(self):
        self.held()
        f = self.facts(self.merged_lane())
        self.assertEqual((f["verdict"], f["merged_into"], f["branch_moved"]), ("ready", "main", True))
        self.assertEqual(fleet.cleanup_plan(f)["follow_ups"],
                         {"close": [{"id": "F3", "text": "Do a"}, {"id": None, "text": "hand-written"}]})
        out = self.clean(f)
        self.assertEqual((out["ok"], out["removed"], out["branch"]), (True, True, "deleted lane-a"))
        self.assertEqual((out["follow_ups_closed"], out["follow_ups_released"], out["follow_up_error"]),
                         (["F3", "F5"], [], None))
        self.assertEqual(self.text(), "# Project: app\n\n## Follow-ups\n- [F4] Do b (lane: lane-b)\n\n## Notes\n- none\n")
        event = self.events("cleanup")[-1]
        self.assertEqual(event["follow_ups_closed"], ["F3", "F5"])
        self.assertIn("follow-ups closed F3, F5", fleet.describe_event(event))
        done = self.events("follow_up_done")
        self.assertEqual([(e["id"], e["branch"]) for e in done], [("F3", "lane-a"), ("F5", "lane-a")])
        self.assertEqual(done[0]["detail"], "  if ready:\n      run_task()")
        self.assertIn("lane lane-a merged and removed", done[0]["reason"])

    def test_cleanup_releases_the_follow_ups_of_a_lane_without_commits(self):
        self.held()
        f = self.facts(self.make_lane())
        self.assertEqual((f["verdict"], f["reasons"], f["branch_moved"]), ("ready", ["in main"], False))
        out = self.clean(f)
        self.assertEqual((out["ok"], out["follow_ups_closed"], out["follow_ups_released"]), (True, [], ["F3", "F5"]))
        self.assertIn("- [F3] Do a\n  if ready:", self.text())

    def test_cleanup_closes_the_follow_ups_of_a_merged_pull_request(self):
        self.held()
        path = self.make_lane()
        self.commit(path)
        f = self.facts(path, pr={"number": 5, "state": "MERGED", "headRefOid": self.head(path)})
        self.assertEqual(f["verdict"], "ready")
        self.assertEqual((self.clean(f)["follow_ups_closed"]), ["F3", "F5"])

    def test_cleanup_releases_when_the_reflog_cannot_tell(self):
        logs = self.repo / ".git" / "logs" / "refs" / "heads"
        # No reflog at all.
        self.held()
        path = self.merged_lane()
        (logs / "lane-a").unlink()
        f = self.facts(path)
        self.assertEqual((f["verdict"], f["branch_moved"]), ("ready", None))
        self.assertEqual(self.clean(f)["follow_ups_released"], ["F3", "F5"])
        # The creation entry expired: the oldest entry is a commit.
        self.held()
        path = self.merged_lane("lane-b")
        log = logs / "lane-b"
        log.write_text("".join(log.read_text().splitlines(keepends=True)[1:]))
        f = self.facts(path)
        self.assertEqual((f["verdict"], f["branch_moved"]), ("ready", None))
        self.assertEqual(self.clean(f)["follow_ups_released"], ["F4"])

    def test_cleanup_closes_for_a_lane_that_was_only_rebased(self):
        self.held()
        path = self.make_lane()
        self.commit(self.repo, "main moves on")
        git("rebase", "-q", "main", cwd=path)  # no commit of its own, but its branch moved: the accepted edge
        f = self.facts(path)
        self.assertEqual((f["verdict"], f["branch_moved"]), ("ready", True))
        self.assertEqual(self.clean(f)["follow_ups_closed"], ["F3", "F5"])

    def test_cleanup_unmerged_releases(self):
        self.held()
        path = self.make_lane()
        self.commit(path)
        f = self.facts(path)
        self.assertEqual((f["verdict"], f["blockers"], f["branch_moved"]), ("keep", [], True))
        out = self.clean(f)
        self.assertEqual((out["ok"], out["follow_ups_released"], out["follow_ups_closed"]), (True, ["F3", "F5"], []))
        self.assertIn("- [F3] Do a\n  if ready:\n      run_task()\n- [F4] Do b (lane: lane-b)\n- [F5] hand-written\n",
                      self.text())
        self.assertEqual(self.events("cleanup")[-1]["follow_ups_released"], ["F3", "F5"])

    def test_cleanup_refuses_a_duplicated_id(self):
        for copy in ("- [F3] a copy (lane: lane-a)\n", "- [F3] a copy (lane: lane-b)\n"):
            self.held(extra=copy)
            before, events = self.text(), len(self.events())
            path = self.root / f"lane-{events}"
            git("worktree", "add", "-q", "-B", "lane-a", str(path), "main", cwd=self.repo)
            self.commit(path)
            out = fleet.clean_lane(self.facts(path, pr={"number": 5, "state": "MERGED", "headRefOid": self.head(path)}), 5)
            self.assertEqual((out["ok"], out["removed"]), (True, True), copy)
            self.assertIn("F3 is on more than one line", out["follow_up_error"])
            self.assertEqual((out["follow_ups_closed"], out["follow_ups_released"]), ([], []))
            self.assertEqual(self.text(), before)
            self.assertEqual([e["event"] for e in self.events()[events:]], ["cleanup"])

    def test_cleanup_reports_a_follow_up_failure_and_succeeds(self):
        self.md.write_bytes(b"## Follow-ups\n- [F1] caf\xe9 (lane: lane-a)\n")
        out = fleet.clean_lane(self.facts(self.make_lane()), 5)
        self.assertEqual((out["ok"], out["removed"], out.get("error")), (True, True, None))
        self.assertIn("not UTF-8", out["follow_up_error"])
        self.assertEqual(self.md.read_bytes(), b"## Follow-ups\n- [F1] caf\xe9 (lane: lane-a)\n")
        self.assertIn("follow-ups: ", fleet.describe_event(self.events("cleanup")[-1]))
        # A lock that another command holds: the cleanup still succeeds.
        self.held()
        f = self.facts(self.make_lane("lane-b"))
        with mock.patch.object(fleet, "LOCK_WAIT", 0.2), fleet.project_lock(self.project):
            out = fleet.clean_lane(f, 5)
        self.assertEqual((out["ok"], out["removed"]), (True, True))
        self.assertIn("is busy", out["follow_up_error"])
        self.assertIn("(lane: lane-b)", self.text())
        # A broken counter: reported, nothing changed.
        self.held(extra="- [F6] Do d (lane: lane-d)\n")
        before = self.text()
        (self.project / "follow-ups.json").write_text("broken json")
        out = fleet.clean_lane(self.facts(self.merged_lane("lane-d")), 5)
        self.assertEqual((out["ok"], out["removed"], out["follow_ups_closed"]), (True, True, []))
        self.assertIn("follow-ups.json", out["follow_up_error"])
        self.assertEqual(((self.project / "follow-ups.json").read_text(), self.text()), ("broken json", before))
        # No PROJECT.md is no failure.
        self.md.unlink()
        f = self.facts(self.make_lane("lane-c"))
        self.assertEqual(fleet.cleanup_plan(f)["follow_ups"], {})
        out = fleet.clean_lane(f, 5)
        self.assertEqual((out["ok"], out["follow_up_error"]), (True, None))
        self.assertNotIn("follow-ups", fleet.describe_event(self.events("cleanup")[-1]))

    def test_cleanup_dry_run_lists_and_changes_nothing(self):
        self.held()
        f = self.facts(self.merged_lane())
        before, events = self.text(), len(self.events())
        with mock.patch.object(fleet, "require_herdr"), \
                mock.patch.object(fleet, "collect_lanes", return_value=([f], [], [])):
            code, out = self.main("cleanup", f["target"], "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["plan"][0]["follow_ups"]["close"][0], {"id": "F3", "text": "Do a"})
        self.assertEqual((self.text(), len(self.events())), (before, events))
        self.assertTrue(Path(f["path"]).is_dir())

    # board and re-brief -----------------------------------------------

    def test_board_shows_the_ids(self):
        self.held()
        a, b = self.root / "a", self.root / "b"
        for path, status in ((a, "## Plan\n- x\n"), (b, "## Next step\nWrite the tests.\n")):
            (path / ".fleet").mkdir(parents=True)
            (path / ".fleet" / "STATUS.md").write_text(status)
        lanes = [{"path": str(p), "label": None, "workspace": "w2", "branch": br, "panes": [], "pr": None,
                  "exists": True, "git": False, "linked": True} for p, br in ((a, "lane-a"), (b, "lane-b"))]
        with mock.patch.object(fleet, "collect_lanes", return_value=(lanes, [], [])), \
                mock.patch.object(fleet.shutil, "which", return_value="/usr/bin/herdr"):
            recs = {r["branch"]: r for r in json.loads(self.main("board", "--json")[1])["lanes"]}
            text = self.main("board")[1]
        self.assertEqual((recs["lane-a"]["follow_ups"], recs["lane-b"]["follow_ups"]), (["F3"], ["F4"]))
        self.assertRegex(text, r"(?m)^lane-a .*follow-ups: F3$")
        self.assertRegex(text, r"(?m)^lane-b .*next: Write the tests\.$")  # another note wins
        self.assertLessEqual(max(len(l) for l in text.splitlines()), 160)
        self.assertEqual(fleet.removed_records([{"event": "cleanup", "removed": True, "lane": "/tmp/gone",
                                                 "ts": fleet.now_iso()}], 0, set(), {})[0]["follow_ups"], [])

    def test_digest_shows_the_follow_up_events_of_other_panes(self):
        fleet.write_json(self.project / "orch.json", {"pane": "w1:p1"})
        fleet.record("follow_up_taken", None, home=self.project, by=False, pane="w2:p1", id="F3", text="Do a",
                     branch="lane-a")
        fleet.record("follow_up_added", None, home=self.project, by=False, pane="w1:p1", id="F4", text="own")
        with mock.patch.object(fleet, "collect_lanes", return_value=([], [], [])), \
                mock.patch.object(fleet.shutil, "which", return_value="/usr/bin/herdr"), \
                mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1"}):
            buf = io.StringIO()
            with chdir(self.repo), redirect_stdout(buf):
                self.assertEqual(fleet.main(["digest", "--peek"]), 0)
        out = buf.getvalue()
        self.assertIn('F3 taken by lane-a: "Do a"', out)
        self.assertNotIn('"own"', out)  # the orchestrator's own events are filtered, as for every kind

    def test_rebrief_shows_the_ids(self):
        self.held()
        fleet.write_json(self.project / "orch.json", {"pane": "w1:p1"})
        text = fleet.session_start_context({"source": "clear", "cwd": str(self.repo)}, env={"HERDR_PANE_ID": "w1:p1"})
        self.assertIn("- [F3] Do a (lane: lane-a)", text)
        self.assertLessEqual(len(text.splitlines()), fleet.REBRIEF_LINES_MAX)


if __name__ == "__main__":
    unittest.main()
