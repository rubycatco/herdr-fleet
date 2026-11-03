"""Unit tests for scripts/fleet. Run: python3 -m unittest discover -s plugins/fleet/tests"""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fleet"
_loader = importlib.machinery.SourceFileLoader("fleet_cli", str(SCRIPT))
_spec = importlib.util.spec_from_loader("fleet_cli", _loader)
fleet = importlib.util.module_from_spec(_spec)
_loader.exec_module(fleet)

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


class AccountTests(unittest.TestCase):
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


class LaunchTests(unittest.TestCase):
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
                rc = fleet.main(["spawn", "--role", "researcher", "--task", str(task), "--no-wait",
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
                fleet.main(["spawn", "--role", "impl", "--no-wait"])
            self.assertNotIn("task", fleet.load_panes(repo / ".fleet")["w2:p6"])


class ScreenTests(unittest.TestCase):
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


class DoctorTests(unittest.TestCase):
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


class SweepTests(unittest.TestCase):
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
        self.assertEqual(fleet.count_dirty(" M a.rb\n?? .fleet/\n?? new.txt\n"), 2)
        self.assertEqual(fleet.count_dirty(""), 0)
        self.assertIsNone(fleet.count_dirty(None))
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


class LaneGitTests(unittest.TestCase):
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
        self.assertFalse(fleet.repo_paths(str(self.repo))["linked"])

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


class ProjectLogTests(unittest.TestCase):
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
            cp = subprocess.run([sys.executable, str(SCRIPT), "hook", event], cwd=self.repo, capture_output=True,
                                text=True, timeout=20, input=json.dumps({**payload, "cwd": str(self.repo)}),
                                env={**os.environ, **env})
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
            cp = subprocess.run(["sh", "-c", command], cwd=self.repo, capture_output=True, text=True, timeout=20,
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


class FilesTests(unittest.TestCase):
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

    def test_session_start_planner_researcher_and_helper(self):
        d = self.lane({"w1:p1": {"role": "impl"}, "w1:p4": {"role": "planner"},
                       "w1:p5": {"role": "researcher", "task": "/home/x/research/q.md"},
                       "w1:p6": {"role": "helper", "task": "/home/x/task.md"},
                       "w1:p7": {"role": "impl", "task": "/home/x/ignored.md"}})
        d = Path(os.path.realpath(d))

        def text(pane, cwd=None):
            return fleet.session_start_context({"source": "compact", "cwd": str(cwd or self.repo)},
                                               env={"HERDR_PANE_ID": pane})
        self.assertEqual(text("w1:p4"), (
            "Fleet lane: your context was just compacted. You plan this lane (pane w1:p4); you do not implement it. "
            f"Re-read {d / 'BRIEF.md'} and your {d / 'PLAN.md'}, change no file except PLAN.md, and continue the "
            "plan. When it is done, end PLAN.md with the next PLAN READY line and stop."))
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
        cp = subprocess.run([sys.executable, str(SCRIPT), "hook", "session-start"], input=payload,
                            capture_output=True, text=True, timeout=20)
        self.assertEqual(cp.returncode, 0)
        out = json.loads(cp.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("HANDOFF.md", out["hookSpecificOutput"]["additionalContext"])

    def test_hook_command_survives_bad_input(self):
        cp = subprocess.run([sys.executable, str(SCRIPT), "hook", "session-start"], input="not json",
                            capture_output=True, text=True, timeout=20)
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


class MarkerTests(unittest.TestCase):
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


class ApprovePlanTests(unittest.TestCase):
    """approve-plan, the gate between a planner and the implementer: it refuses on every condition of the design,
    and then writes neither PLAN.md nor the log."""

    PLAN = "# Plan: x\n\nRisk: none\n\n## Approach\nDo it.\n\nPLAN READY: 1\n"

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
        home = fleet.project_for(str(self.repo))
        return [e for e in fleet.read_events([home]) if e["event"] == "plan_approved"]

    def refused(self, *argv, reason, cwd=None):
        before = self.plan.read_bytes() if self.plan.is_file() else None
        code, out = self.approve(*argv, cwd=cwd)
        self.assertEqual((code, out["ok"], out["changed"]), (2, False, "nothing"), out)
        self.assertIn(reason, out["reason"])
        self.assertEqual(self.plan.read_bytes() if self.plan.is_file() else None, before)
        self.assertEqual(self.approvals(), [])
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
        out = self.refused("--by", "user", reason="1 changed file outside .fleet/")
        self.assertEqual(out["changes"], 1)
        git("checkout", "app.py", cwd=self.lane)
        (self.lane / "new.py").write_text("")
        self.refused("--by", "orch", reason="outside .fleet/")
        (self.lane / "new.py").unlink()
        (self.plan.parent / "notes.md").write_text("inside .fleet/ is fine")
        self.assertEqual(self.approve("--by", "orch")[0], 0)

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

    def test_orch_approves_a_low_risk_plan_once_per_round(self):
        self.plan.write_text(self.PLAN.rstrip("\n"))  # no newline at the end
        code, out = self.approve("--by", "orch")
        self.assertEqual((code, out["ok"], out["round"], out["by"], out["risk"], out["branch"]),
                         (0, True, 1, "orch", "none", "feat/lane"))
        lines = self.plan.read_text().splitlines()
        self.assertEqual(lines[-2], "PLAN READY: 1")
        self.assertTrue(lines[-1].startswith("PLAN APPROVED: 1 by orch "))
        [event] = self.approvals()
        self.assertEqual((event["round"], event["approved_by"], event["risk"], event["branch"], event["lane"]),
                         (1, "orch", "none", "feat/lane", str(self.lane)))
        self.assertEqual(fleet.describe_event(event), "plan round 1 approved by orch, risk none")
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


class OrchTests(unittest.TestCase):
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
        rec = fleet.read_json(self.project / "orch.json")
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
            self.assertEqual(fleet.read_json(self.project / "orch.json")["pane"], "w1:p1")
        self.assertIn("--replace works only from that pane",
                      json.loads(self.main("orch", "claim", "--replace", pane="w2:p1")[1])["refused"])
        # The registered pane hands over to a successor; the session is not the caller's.
        with mock.patch.object(fleet, "pane_info", return_value={"workspace_id": "w3"}):
            code, out = self.main("orch", "claim", "--pane", "w3:p1", "--replace", pane="w1:p1",
                                  CLAUDE_CODE_SESSION_ID="sess-orch-1")
        self.assertEqual((code, json.loads(out)["previous"]), (0, "w1:p1"))
        rec = fleet.read_json(self.project / "orch.json")
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
        self.assertIn("orch pane w1:p1 -> w3:p1 (replace)", text)
        self.assertIn("released orch pane w4:p1 (force)", text)

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

    def test_board_plan_warning_and_open_research(self):
        a = self.add_lane("a", PLAN_md="Risk: none\nPLAN READY: 1\n", STATUS_md="## Next step\nWait.\n")
        b = self.add_lane("b", STATUS_md="## Next step\nGo.\n")
        fleet.save_panes(a / ".fleet", {"w2:p1": {"role": "planner"}})
        lanes = [{**self.facts(a, panes=[{"pane": "w2:p1", "agent": "claude", "status": "idle"}]), "dirty": 2},
                 {**self.facts(b, panes=[{"pane": "w3:p1", "agent": "claude", "status": "working"}]), "dirty": 3}]
        research = self.project / "research"
        research.mkdir()
        (research / "auth-flow.md").write_text("# Research: how does auth work?\n\n## Question\nx\n"
                                                "\nEnd with the line RESEARCH DONE when done.\n")
        (research / "cache.md").write_text("# Research: cache\n\n## Answer\ny\n\nRESEARCH DONE\n")
        (research / "notes.txt").write_text("not research")
        out = json.loads(self.board(lanes, "board", "--json")[1])
        recs = {r["lane"]: r for r in out["lanes"]}
        self.assertEqual(recs["feat/a"]["phase"], "plan ready 1")
        self.assertIn("2 changed files outside .fleet/", recs["feat/a"]["warning"])
        self.assertEqual(recs["feat/b"]["phase"], "working")
        self.assertNotIn("warning", recs["feat/b"])  # only at plan ready
        self.assertEqual(out["research"], [{"file": str(research / "auth-flow.md"),
                                            "title": "Research: how does auth work?"}])
        text = self.board(lanes, "board")[1]
        self.assertRegex(text, r"feat/a\s+plan ready 1\s+-\s+planner:idle\s+-\s+2 changed files outside")
        self.assertIn("Open research (1, no RESEARCH DONE line yet):", text)
        self.assertIn("auth-flow.md  Research: how does auth work?", text)
        self.assertNotIn("cache.md", text)
        # Clean worktree: no warning. No open research: no research block.
        lanes[0]["dirty"] = 0
        (research / "auth-flow.md").write_text("# Research\n\n- RESEARCH DONE\n")
        text = self.board(lanes, "board")[1]
        self.assertNotIn("outside .fleet", text)
        self.assertNotIn("Open research", text)

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
        log = self.project / "events" / f"{time.strftime('%Y-%m')}.jsonl"
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
        older, newer = fleet.precise_now(), None
        time.sleep(0.01)
        newer = fleet.precise_now()
        self.assertTrue(fleet.save_board(self.project, [{"lane": "new"}], newer))
        self.assertFalse(fleet.save_board(self.project, [{"lane": "old"}], older))
        self.assertEqual(fleet.read_json(self.project / "board.json")["lanes"], [{"lane": "new"}])
        # `board` stamps the board with the time its collection began, and says when it keeps a newer one.
        made = []

        def collect(*args, **kwargs):
            made.append(fleet.precise_now())
            fleet.save_board(self.project, [{"lane": "newer"}], fleet.precise_now())
            return ([], [], [])
        with mock.patch.object(fleet, "collect_lanes", side_effect=collect), \
                mock.patch.object(fleet.shutil, "which", return_value="/usr/bin/herdr"):
            self.assertIn("board.json holds a newer board", self.main("board")[1])
        self.assertEqual(fleet.read_json(self.project / "board.json")["lanes"], [{"lane": "newer"}])

    def log_file(self, month=None):
        return self.project / "events" / f"{month or time.strftime('%Y-%m')}.jsonl"

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
        cp = subprocess.run([sys.executable, str(SCRIPT), "hook", "session-start"], input=payload, cwd=self.repo,
                            capture_output=True, text=True, timeout=20, env={**os.environ, "HERDR_PANE_ID": "w1:p1"})
        context = json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("you orchestrate project app", context)
        self.assertNotIn("You implement this lane", context)
        # Any other failure once the pane is known as the orch: the short fixed re-brief.
        with mock.patch.object(fleet, "orch_rebrief", side_effect=RuntimeError("boom")):
            text = fleet.session_start_context({"source": "clear", "cwd": str(self.repo)},
                                               env={"HERDR_PANE_ID": "w1:p1", "CLAUDE_PLUGIN_ROOT": "/plugins/fleet"})
        self.assertIn("you orchestrate project app (pane w1:p1). The re-brief from the project home failed.", text)
        self.assertIn('`python3 "/plugins/fleet/scripts/fleet" digest`', text)
        self.assertNotIn("You implement", text)
        # A failure before that point behaves as on main.
        with mock.patch.object(fleet, "orch_match", side_effect=RuntimeError("boom")):
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

    def test_rebrief_on_every_source(self):
        self.orch_setup()
        for source in ("startup", "resume", "clear", "compact"):
            text = fleet.session_start_context({"source": source, "cwd": str(self.repo)},
                                               env={"HERDR_PANE_ID": "w1:p1"})
            self.assertIn("you orchestrate project app (pane w1:p1)", text, source)
            self.assertIn(str(self.project / "PROJECT.md"), text)
            self.assertIn("Never push to main.", text)
            self.assertIn("Add a CHANGELOG entry.", text)
            self.assertNotIn("Use sqlite", text)  # decisions stay in the file
            self.assertRegex(text, r"Board from \d+s ago")
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

    def test_rebrief_hook_is_fast_and_offline(self):
        self.orch_setup()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        os.symlink(shutil.which("git"), bin_dir / "git")  # git only: herdr and gh cannot run
        env = {"PATH": str(bin_dir), "FLEET_HOME": str(self.home), "HERDR_PANE_ID": "w1:p1", "HOME": str(self.root)}
        payload = json.dumps({"source": "startup", "cwd": str(self.repo), "session_id": "s-9"})
        start = time.monotonic()
        cp = subprocess.run([sys.executable, str(SCRIPT), "hook", "session-start"], input=payload, cwd=self.repo,
                            capture_output=True, text=True, timeout=20, env=env)
        elapsed = time.monotonic() - start
        self.assertEqual((cp.returncode, cp.stderr), (0, ""))
        self.assertLess(elapsed, 1.0)
        context = json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("you orchestrate project app (pane w1:p1)", context)


if __name__ == "__main__":
    unittest.main()
