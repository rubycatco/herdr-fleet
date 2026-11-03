"""Unit tests for scripts/fleet. Run: python3 -m unittest discover -s plugins/fleet/tests"""

import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fleet"
_loader = importlib.machinery.SourceFileLoader("fleet_cli", str(SCRIPT))
_spec = importlib.util.spec_from_loader("fleet_cli", _loader)
fleet = importlib.util.module_from_spec(_spec)
_loader.exec_module(fleet)

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
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp + "/state"}):
            lane = Path(tmp) / "lane"
            (lane / ".fleet" / "inbox").mkdir(parents=True)
            (lane / ".fleet" / "STATUS.md").write_text("log")
            (lane / ".fleet" / "inbox" / "1.md").write_text("task")
            (lane / ".fleet" / "panes.json").write_text("{}")
            dest = Path(fleet.archive_lane_files(str(lane), "repo", "fix/a b"))
            self.assertEqual(dest.parent, Path(tmp) / "state" / "fleet" / "archive" / "repo")
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

    def facts(self, pr=None, caller_cwd=None):
        stub = {"workspace": None, "label": None, "path": str(self.lane_path), "panes": []}
        stub.update(fleet.repo_paths(stub["path"]))
        return fleet.finish_lane(fleet.lane_facts(stub, None, caller_cwd or self.tmp.name,
                                                  lambda path, branch: (pr, None)))

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

    def test_detached_checkout(self):
        self.git(self.lane_path, "checkout", "-q", "--detach")
        f = self.facts()
        self.assertEqual((f["verdict"], f["delete_branch"]), ("ready", False))
        self.git(self.lane_path, "commit", "-q", "--allow-empty", "-m", "only here")
        self.assertEqual(self.facts()["reasons"], ["detached HEAD with 1 commit on no branch"])


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


if __name__ == "__main__":
    unittest.main()
