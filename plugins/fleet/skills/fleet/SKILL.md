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

You are the orchestrator. Lanes do the work in their own Herdr panes. You decide, brief, watch, review, integrate and clean up. Stay small: when a brief needs digging, a researcher pane digs (`/fleet:research`); when a change needs a plan first, a planner pane writes it.

## Start here

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" conventions`. It prints the mode and the user's conventions: agents, models, critics, context limits and, in remuda mode, accounts. Follow them. If it prints the plugin defaults, suggest `/fleet:onboard` once.
2. Plain mode (no remuda): never pass `--account` and ignore account rules. Every lane uses the agent's own login.
3. Your memory is the project home, `~/.fleet/projects/<name>/`, which every clone of the repository shares. Run `orch show`. When no live pane is registered, run `orch claim`: it registers your pane as the project's one orchestrator and creates `PROJECT.md`. When another live pane is registered, that pane orchestrates this project: tell the user, and do not claim it.
4. Run `digest`: what changed since the orchestrator last looked (lane phases, and the events of the lanes). Then `board` whenever you need the lane table; it consumes nothing.
5. Write only what nothing else knows to `PROJECT.md`: the user's rules for this project, decisions with reasons, follow-ups, and the project's high-risk paths under `## High-risk paths` (template and pattern rules: `${CLAUDE_PLUGIN_ROOT}/skills/fleet/references/templates.md`). When the user names files or folders that must never change without their yes (a schema file, an auth folder), add them there. The board and the event log keep the rest. At every session start of your pane (startup, resume, /clear, compaction), the SessionStart hook re-briefs it from the project home: PROJECT.md's rules and follow-ups, the cached board, and the number of new events.
6. An existing `.fleet/FLEET.md` keeps working: the hook still re-briefs the pane on its `Orchestrator:` line after /clear or compaction, and `orch claim` warns while that pane is alive. Do not start a new FLEET.md.

## The helper

Run every command as `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" <command> ...`, written out in full each time: each Bash call is a new shell. Commands print JSON unless the table says "text".

| Command | Does |
|---|---|
| `conventions` | The user's rules (text). |
| `doctor` | Tools, mode, accounts, conventions path (text; `--json`). |
| `accounts` | remuda accounts and cached usage (text). Pass `--live` only when the user asks. |
| `status [--workspace W]` | Each agent pane: status, role, account, branch, lane files (text; `--json`). |
| `worktree BRANCH --base REF --name LABEL` | A Herdr worktree in its own workspace, with a shell pane and `.fleet/` ready. |
| `spawn ...` | Start an agent in a pane and confirm that it works. See below. With `--role impl`, or no `--role`, it refuses (exit 2, `changed: nothing`) while the lane has a `.fleet/PLAN.md` whose newest `PLAN READY: <n>` round has no `PLAN APPROVED: <n>` line, also when no round is ready yet, and when git fails in a directory with a PLAN.md at or above it. The agent then starts with a `cd` to the directory that was checked. There is no flag around it: approve the plan, or remove PLAN.md on purpose. |
| `send PANE --text T` or `--file F` | Submit a prompt and confirm that it landed. Long or multi-line text goes to `.fleet/inbox/` and is sent as a one-line pointer. |
| `watch PANE [--marker M] [--file F]` | Wait until a line of a lane file (default `.fleet/STATUS.md`) starts with the marker, or the agent stops, blocks or exits. A mention inside a sentence does not count. Run it in the background. |
| `reset PANE [--compact [FOCUS]]` | `/clear` (or `/compact`) a Claude lane at a boundary, confirm it, and send a resume prompt. PANE comes first. |
| `retire PANE [--close]` | Exit the agent, and close the pane if asked. |
| `approve-plan [DIR] --by orch\|user [--round N] [--base REF]` | Approve the newest round of the lane's `.fleet/PLAN.md` (DIR: the lane's worktree): it appends `PLAN APPROVED: <n> by <who> <date>` and records the approval. It refuses, changing nothing, when PLAN.md has no `PLAN READY: <n>` line for the round, the round is already approved, the worktree has changes outside `.fleet/` (untracked files and renames count, whatever the git settings), the lane has commits of its own (HEAD commits that no other branch, remote branch or tag holds; a detached HEAD refuses), or the project's high-risk patterns cannot be read. With `--by orch` it also refuses when the `Risk:` line is not `none`, when `## Files` lists no path, or when a listed path matches a high-risk pattern (the refusal names the matches): such a plan is high risk whatever its `Risk:` line says. `--by user` overrides only the risk and the missing files; pass it only after the user said yes. `--base REF` counts the lane's commits as `REF..HEAD` instead: use it when no branch, remote or tag holds the lane's base, or when you plan a lane again that already has commits. |
| `risk [DIR] [--base REF]` | The lane's real changes (its own commits and `git status`, outside `.fleet/`) that match a high-risk pattern, each with what covers it: the newest plan round that the user approved, when it shows that category, or a line in `.fleet/RISK.md`. The rest needs the user before merge. When the changes cannot be told, the lane needs the user (text; `--json`). It only reads. |
| `approve-risk [DIR] --by user [--base REF]` | After the user's yes for a risky diff: append one `RISK APPROVED: <category> <path> by user <date>` line per uncovered path to `.fleet/RISK.md`, and record it. It refuses, changing nothing, outside a git worktree, when the changes cannot be told, or when nothing needs the user. |
| `sweep` | Every lane on this machine with a verdict, ready, waiting or keep, and the reason (text; `--json`). It only reads. |
| `cleanup TARGET... [--unmerged] [--dry-run]` | Remove lanes: exit their agents, archive `.fleet/`, remove the worktree, delete the local branch when the merge holds every commit. It checks every target first and changes nothing when one is not ready. |
| `prep [DIR] [--handoff]` | Create `.fleet/` and keep it out of git. `--handoff` also returns where a handoff file goes. |
| `orch [show\|claim\|release] [--pane P] [--replace] [--force] [--project NAME]` | The project's orchestrator pane in `orch.json` (`show`: text; `--json`). `claim` registers the calling pane, or `--pane P`; it refuses while another registered pane is alive, except `--replace` from that pane (a handoff). When herdr cannot tell whether the registered pane is alive, it refuses too: check that pane, then `--force` takes over. It warns about a live FLEET.md orchestrator. `release` removes your record (`--force`: another pane's). |
| `board [--project NAME]` | One row per lane of this project: phase, pull request, panes, newest event, and a note: the plan warning, a risky diff that needs the user, or the blocked or next-step line of STATUS.md (text; `--json`, where `risk` holds the high-risk matches). Then the project's research files without a `RESEARCH DONE` line. It saves the board for the re-brief and never consumes a change. |
| `digest [--peek] [--project NAME]` | Lane phase changes and the lanes' events since the last digest, at most 30, newest last (text; `--json`). It records each phase change in the log and moves the cursor. With `--peek`, or from another pane while the registered orchestrator is alive (or herdr cannot tell), it only reads. |
| `log [PROJECT] [--lane L] [--pane P] [--event E] [--since 7d] [--sessions]` | The project's event log (text; `--json`): the commands above that change something, and each Claude session in a fleet checkout. `--sessions`: one row per agent session, with its transcript. It only reads. |

`spawn` options:
- Placement: `--pane P` (an existing shell pane), `--worktree BRANCH --base REF`, `--tab`, `--new-workspace`, or a split (`--split auto|right|down`, `--from PANE`; default: split the calling pane). A split starts in the directory of the pane it splits unless you pass `--cwd`.
- Agent: `--agent claude|codex`, `--model`, `--effort` (claude `--effort`; codex `model_reasoning_effort`), `--permission-mode` (claude), `--account` (remuda only), `--name LABEL`, `--role impl|critic|orchestrator|helper|planner|researcher`, `-- <more agent arguments>`.
- Task file: `--task FILE` (needs `--role`) keeps the file's absolute path with the pane's role. After /clear or compaction, the SessionStart hook tells a researcher or helper to re-read it.
- Task: `--prompt "<one line>"` (passed as an argument, so it is never pasted). Put longer tasks in a file and point at it.
- `--resume SESSION_ID` (claude): fork that session in the new pane, for example on another account. It refuses when the target account cannot see the transcript.
- `--accept-trust`: answer the folder trust dialog. Use it only for a worktree you just created.
- Exit code 2: the agent did not confirm. Read its pane: `herdr pane read <pane> --source recent-unwrapped --lines 300`.

`watch` exit codes: 0 marker found, 4 blocked on a dialog, 5 agent exited, 6 stopped without the marker, 7 timeout. It prints the end of the screen for 4, 5 and 6.

## Lane lifecycle

0. **Research** (on demand): `/fleet:research <question>`. A researcher pane answers into `<home>/research/<slug>.md`; briefs and plans link to the answer.
1. **Open**: `/fleet:lane`. Worktree, BRIEF.md, then the triage below. Then an implementer pane with `--role impl`, and a background `watch` for `READY FOR REVIEW: round 1`.
   - **Plan first** (when the triage says so): a planner pane writes `.fleet/PLAN.md` and ends it with `PLAN READY: <n>`; you approve it with `approve-plan` (the user approves a high-risk plan), retire the planner, and start the implementer in the same pane on BRIEF.md and PLAN.md. `spawn` refuses the implementer until the newest round is approved. The board shows `planning`, `plan ready <n>`, `plan approved <n>`.
2. **Watch**: act on each `watch` result, then watch again.
   - 4 (blocked): read the pane. Answer only dialogs that you can safely decide. Never approve a flagged command for a lane: cancel it with `herdr pane send-keys <pane> Escape` and re-instruct.
   - 6 (stopped): read STATUS.md (its `Blocked` section) and the pane. The lane may have a question, a placeholder, or a background job. Answer with `send`.
3. **Review**: `/fleet:review`. Review request, critic panes per the conventions, verdict, findings back to the implementer, stop rule.
4. **Context**: `/fleet:context`. Reset long lanes at a commit. Hand off when an account or model must change (`/fleet:handoff`).
5. **Finish**: first run `risk <worktree>`. When it says that paths need the user, or that the changes cannot be told, show the user the matches and the diff of those paths, and ask. After the yes, run `approve-risk <worktree> --by user` and check that `risk` says nothing needs the user. Never push, open the pull request or merge while the lane needs the user. Then push, open the pull request and merge as the conventions and the project's rules say. The board shows the merge; write follow-ups and decisions to PROJECT.md.
6. **Clean up**: `/fleet:cleanup`, which the user runs. It sweeps every lane, asks which ones to remove, removes them and runs the project's own cleanup. After a merge, run `sweep` and tell the user which lanes are ready. Run `cleanup` yourself only on lanes that the user named for removal.

## Rules

- Triage, by the conventions' `## Planning and research` section. Research first when you cannot name the files or facts a brief needs, or several lanes need the same answer. Plan first for a high-risk change (migration, auth or payments, deletion logic, incident hotfix), a change that spans subsystems, or a brief that cannot name its files. Otherwise the implementer plans in STATUS.md.
- A planner writes only PLAN.md, and a researcher only its research file. Approve a plan only with `approve-plan`; a high-risk plan (a `Risk:` other than `none`, or a path under `## Files` that matches a high-risk pattern) needs the user's yes.
- Only `approve-plan` writes `PLAN APPROVED` lines, and only `approve-risk` writes `RISK APPROVED` lines. A line written by hand passes the gates, so read PLAN.md and RISK.md before you rely on them, and never let an agent write those lines.
- One implementer per worktree. Critics work in the lane's worktree, write only their review file, and never edit code.
- Dispatch with `send`, `spawn --prompt` or files. Use `herdr pane run` or `send-keys` only to answer a dialog.
- Watch files and git, not scrollback. Each marker carries a round number. Never reuse a number.
- To see where the lanes stand, run `digest` or `board`, not `status` and scrollback. To recall what a lane was told and which sessions worked on it, read `log --lane <branch>` or `log --sessions`.
- `done` does not mean finished. Before a review, check that STATUS.md has real verification results and no `PLACEHOLDER`.
- The helper refuses to drive your own pane. For yourself, use `/fleet:context` or `/fleet:handoff`.
- Close only panes, tabs and workspaces that you created. Never delete a branch or worktree that holds unmerged work.
- Decisions, follow-ups and the user's project rules go to PROJECT.md as soon as they happen. Do not keep a lane table: the board and the log keep it.

The reasons behind these rules, from earlier fleets: `${CLAUDE_PLUGIN_ROOT}/skills/fleet/references/lessons.md`.
