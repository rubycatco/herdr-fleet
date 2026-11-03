---
name: fleet
description: Orchestrate a fleet of coding-agent lanes in Herdr panes — open lanes in worktrees, brief and watch them, run critic rounds, reset or hand off lanes, pick remuda accounts, and clean up. Use whenever you drive other agents through Herdr (HERDR_ENV=1), or the user mentions lanes, a fleet, critic panes, or moving work to another pane.
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" *)
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fleet *)
  - Bash(herdr pane read *)
  - Bash(herdr pane get *)
  - Bash(herdr agent list*)
  - Bash(git status*)
  - Bash(git log *)
  - Bash(git diff *)
---

# Fleet

You are the orchestrator. Lanes do the work in their own Herdr panes. You plan, brief, watch, review, integrate and clean up.

## Start here

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" conventions`. It prints the mode and the user's conventions: agents, models, critics, context limits and, in remuda mode, accounts. Follow them. If it prints the plugin defaults, suggest `/fleet:onboard` once.
2. Plain mode (no remuda): never pass `--account` and ignore account rules. Every lane uses the agent's own login.
3. Keep `.fleet/FLEET.md` in your own checkout (template: `${CLAUDE_PLUGIN_ROOT}/skills/fleet/references/templates.md`). Put your pane id (`$HERDR_PANE_ID`) on its `Orchestrator:` line. After /clear or compaction, the SessionStart hook re-briefs only that pane from FLEET.md.

## The helper

Run every command as `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" <command> ...`, written out in full each time: each Bash call is a new shell. Commands print JSON unless the table says "text".

| Command | Does |
|---|---|
| `conventions` | The user's rules (text). |
| `doctor` | Tools, mode, accounts, conventions path (text; `--json`). |
| `accounts` | remuda accounts and cached usage (text). Pass `--live` only when the user asks. |
| `status [--workspace W]` | Each agent pane: status, role, account, branch, lane files (text; `--json`). |
| `worktree BRANCH --base REF --name LABEL` | A Herdr worktree in its own workspace, with a shell pane and `.fleet/` ready. |
| `spawn ...` | Start an agent in a pane and confirm that it works. See below. |
| `send PANE --text T` or `--file F` | Submit a prompt and confirm that it landed. Long or multi-line text goes to `.fleet/inbox/` and is sent as a one-line pointer. |
| `watch PANE [--marker M] [--file F]` | Wait until a marker shows in a lane file (default `.fleet/STATUS.md`), or the agent stops, blocks or exits. Run it in the background. |
| `reset PANE [--compact [FOCUS]]` | `/clear` (or `/compact`) a Claude lane at a boundary, confirm it, and send a resume prompt. PANE comes first. |
| `retire PANE [--close]` | Exit the agent, and close the pane if asked. |
| `sweep` | Every lane on this machine with a verdict, ready, waiting or keep, and the reason (text; `--json`). It only reads. |
| `cleanup TARGET... [--unmerged] [--dry-run]` | Remove lanes: exit their agents, archive `.fleet/`, remove the worktree, delete the local branch when the merge holds every commit. It checks every target first and changes nothing when one is not ready. |
| `prep [DIR] [--handoff]` | Create `.fleet/` and keep it out of git. `--handoff` also returns where a handoff file goes. |
| `log [PROJECT] [--lane L] [--pane P] [--event E] [--since 7d] [--sessions]` | The project's event log (text; `--json`): the commands above that change something, and each Claude session in a fleet checkout. `--sessions`: one row per agent session, with its transcript. It only reads. |

`spawn` options:
- Placement: `--pane P` (an existing shell pane), `--worktree BRANCH --base REF`, `--tab`, `--new-workspace`, or a split (`--split auto|right|down`, `--from PANE`; default: split the calling pane). A split starts in the directory of the pane it splits unless you pass `--cwd`.
- Agent: `--agent claude|codex`, `--model`, `--effort` (claude `--effort`; codex `model_reasoning_effort`), `--permission-mode` (claude), `--account` (remuda only), `--name LABEL`, `--role impl|critic|orchestrator|helper`, `-- <more agent arguments>`.
- Task: `--prompt "<one line>"` (passed as an argument, so it is never pasted). Put longer tasks in a file and point at it.
- `--resume SESSION_ID` (claude): fork that session in the new pane, for example on another account. It refuses when the target account cannot see the transcript.
- `--accept-trust`: answer the folder trust dialog. Use it only for a worktree you just created.
- Exit code 2: the agent did not confirm. Read its pane: `herdr pane read <pane> --source recent-unwrapped --lines 300`.

`watch` exit codes: 0 marker found, 4 blocked on a dialog, 5 agent exited, 6 stopped without the marker, 7 timeout. It prints the end of the screen for 4, 5 and 6.

## Lane lifecycle

1. **Open**: `/fleet:lane`. Worktree, BRIEF.md, an implementer pane with `--role impl`, and a background `watch` for `READY FOR REVIEW: round 1`.
2. **Watch**: act on each `watch` result, then watch again.
   - 4 (blocked): read the pane. Answer only dialogs that you can safely decide. Never approve a flagged command for a lane: cancel it with `herdr pane send-keys <pane> Escape` and re-instruct.
   - 6 (stopped): read STATUS.md (its `Blocked` section) and the pane. The lane may have a question, a placeholder, or a background job. Answer with `send`.
3. **Review**: `/fleet:review`. Review request, critic panes per the conventions, verdict, findings back to the implementer, stop rule.
4. **Context**: `/fleet:context`. Reset long lanes at a commit. Hand off when an account or model must change (`/fleet:handoff`).
5. **Finish**: push, open the pull request and merge as the conventions and the project's rules say. Record the result in FLEET.md.
6. **Clean up**: `/fleet:cleanup`, which the user runs. It sweeps every lane, asks which ones to remove, removes them and runs the project's own cleanup. After a merge, run `sweep` and tell the user which lanes are ready. Run `cleanup` yourself only on lanes that the user named for removal.

## Rules

- One implementer per worktree. Critics work in the lane's worktree, write only their review file, and never edit code.
- Dispatch with `send`, `spawn --prompt` or files. Use `herdr pane run` or `send-keys` only to answer a dialog.
- Watch files and git, not scrollback. Each marker carries a round number. Never reuse a number.
- To recall what a lane was told and which sessions worked on it, read `log --lane <branch>` or `log --sessions`, not old scrollback.
- `done` does not mean finished. Before a review, check that STATUS.md has real verification results and no `PLACEHOLDER`.
- The helper refuses to drive your own pane. For yourself, use `/fleet:context` or `/fleet:handoff`.
- Close only panes, tabs and workspaces that you created. Never delete a branch or worktree that holds unmerged work.
- Update FLEET.md after each spawn, review round, reset, handoff, merge and cleanup.

The reasons behind these rules, from earlier fleets: `${CLAUDE_PLUGIN_ROOT}/skills/fleet/references/lessons.md`.
