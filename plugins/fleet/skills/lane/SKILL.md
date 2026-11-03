---
name: lane
description: Open a fleet lane for a task — a worktree and branch, a written brief, and an implementer agent in its own Herdr pane (on the right remuda account when remuda is installed) — then watch it until it is ready for review. Use when the user asks to open, start or dispatch a lane, or to put an agent on a task in parallel.
argument-hint: "<task, issue or plan> [branch] [account]"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" *)
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fleet *)
  - Bash(herdr pane read *)
  - Bash(herdr pane get *)
  - Bash(git status*)
  - Bash(git log *)
  - Bash(git fetch *)
---

# Open a lane

The task comes from the arguments. If there are none, ask the user what the lane should do.

## 1. Prepare

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" conventions`. Use its implementer, critic and account rules.
2. Learn enough to write the brief: read the issue, plan or code that the task names. Settle the scope with the user when the goal is unclear or another lane could touch the same files.
3. Names: a short lane label (for example `fix-login-redirect`) and a branch (the project's naming, else the label). Base: the remote default branch after `git fetch`, unless the user names one.
4. Account: in remuda mode, choose by the conventions. When the rules depend on usage, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" accounts`. In plain mode there is no account.

## 2. Create the worktree

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" worktree <branch> --base <base> --name <label>
```

It returns `pane_id` (a shell pane in a new workspace), `cwd` (the worktree) and `fleet_dir`.

## 3. Write the brief

Write `<cwd>/.fleet/BRIEF.md` from the BRIEF template in `${CLAUDE_PLUGIN_ROOT}/skills/fleet/references/templates.md`:
- Goal, scope, sources, constraints, acceptance, verification and reporting.
- If the project documents a worktree setup step, put it under Setup.
- Link to sources instead of copying them. Name exact files and commands. Keep it under about 80 lines.
- Give the lane the goal and the design points, not a step list. The lane writes its own plan into STATUS.md.

## 4. Start the implementer

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" spawn --pane <pane_id> --role impl --name <label> \
  --agent <agent> [--model <model>] [--effort <effort>] [--permission-mode <mode>] [--account <account>] --accept-trust \
  --prompt "Read .fleet/BRIEF.md and execute it."
```

`ok: true` with status `working` means it started. Otherwise read the pane (`herdr pane read <pane_id> --source recent-unwrapped --lines 300`) and fix what blocks it.

## 5. Record and watch

1. Add the lane to `.fleet/FLEET.md` in your own checkout: pane, workspace, worktree, branch, agent, account, model, state and next action.
2. Start a watcher with the Bash tool and `run_in_background: true`:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" watch <pane_id> --marker "READY FOR REVIEW: round 1"
   ```
3. Tell the user in two or three lines: lane, pane, branch, account, and what happens next. Then wait for the watcher. Do not poll.

When the watcher ends:
- 0: run `/fleet:review` for this lane.
- 4 (blocked): read the pane. Cancel an unsafe prompt with `herdr pane send-keys <pane_id> Escape` and re-instruct with `send`. Then watch again.
- 6 (stopped): read STATUS.md (`Blocked`) and the pane. Answer with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" send <pane_id> --text "..."`. Then watch again.
- 5 (exited): read the pane. Start the agent again with `spawn --pane <pane_id> --role impl ...` and a prompt that points at BRIEF.md and STATUS.md.
