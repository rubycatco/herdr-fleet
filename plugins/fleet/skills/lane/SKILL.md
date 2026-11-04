---
name: lane
description: Open a fleet lane for a task — a worktree and branch, a written brief, and an implementer agent in its own Herdr pane (on the right remuda account when remuda is installed) — then watch it until it is ready for review. Use when the user asks to open, start or dispatch a lane, or to put an agent on a task in parallel.
argument-hint: "<task, issue or plan> | --from <ID> [branch] [account]"
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

The task comes from the arguments. If there are none, ask the user what the lane should do. With `--from <ID>` (for example `--from F7`), the task is a follow-up of the project's PROJECT.md.

## 1. Prepare

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" conventions`. Use its implementer, critic, planning and account rules.
2. With `--from <ID>`: run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" follow-up list --json` and find the ID. Its `text` (and `detail`, when present) is the task. When it has a `lane`, another lane holds it, and one lane holds a follow-up. Ask the user, with two choices: release it first (`follow-up release <ID>`, when that lane was dropped), or open the lane without `--from` (a separate task: no take, no link, and cleanup does not close the follow-up). An unknown ID: tell the user and stop.
3. Learn enough to write the brief: read the issue, plan or code that the task names. Settle the scope with the user when the goal is unclear or another lane could touch the same files.
4. Names: a short lane label (for example `fix-login-redirect`) and a branch (the project's naming, else the label). Base: the remote default branch after `git fetch`, unless the user names one.
5. Account: in remuda mode, choose by the conventions. When the rules depend on usage, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" accounts`. In plain mode there is no account.

## 2. Create the worktree

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" worktree <branch> --base <base> --name <label>
```

It returns `pane_id` (a shell pane in a new workspace), `cwd` (the worktree) and `fleet_dir`.

With `--from <ID>`, link the follow-up to the lane right away:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" follow-up take <ID> --lane <branch>
```

Go on only when it prints `ok: true`. Any refusal (exit code 2, for example `held_by` when another lane took it since step 1): stop and ask the user. After the take, the board shows the ID on the lane. When `fleet cleanup` removes the lane, it closes the follow-up as done if the lane's pull request merged, or if the lane is merged and its branch moved since `fleet worktree` created it (a rebase counts as a move); otherwise, or when reflog expiry hides the branch's creation, it releases it.

## 3. Write the brief

Write `<cwd>/.fleet/BRIEF.md` from the BRIEF template in `${CLAUDE_PLUGIN_ROOT}/skills/fleet/references/templates.md`:
- Goal, scope, sources, constraints, acceptance, verification and reporting.
- If the project documents a worktree setup step, put it under Setup.
- Link to sources instead of copying them. Name exact files and commands. Keep it under about 80 lines.
- Give the lane the goal and the design points, not a step list. The lane writes its own plan into STATUS.md, or a planner writes PLAN.md (step 4b).
- Link to research files in the project home instead of copying them.
- With `--from <ID>`: quote the ID and its text under Sources (`- Follow-up F7 in PROJECT.md: "<text>"`).

### Triage: research or plan first?

Follow the conventions' `## Planning and research` section. Its defaults:
- **Research first** when you cannot name the files or the facts that the brief needs, or when several lanes need the same answer: run `/fleet:research <question>`, then finish the brief with a link to the answer.
- **Plan first** for a high-risk change (a migration, auth or payments, deletion logic, an incident hotfix), a change that spans subsystems, or a brief that cannot name its files: step 4b instead of step 4.
- Otherwise go on with step 4: the implementer plans in STATUS.md.

## 4. Start the implementer

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" spawn --pane <pane_id> --role impl --name <label> \
  --agent <agent> [--model <model>] [--effort <effort>] [--permission-mode <mode>] [--account <account>] --accept-trust \
  --prompt "Read .fleet/BRIEF.md and execute it."
```

`ok: true` means it started: status `working`, or a note that the prompt shows as sent. Otherwise read the pane (`herdr pane read <pane_id> --source recent-unwrapped --lines 300`) and fix what blocks it.

## 4b. Plan first (instead of step 4)

1. Copy the PLAN template from `${CLAUDE_PLUGIN_ROOT}/skills/fleet/references/templates.md` to `<cwd>/.fleet/PLAN.md`. Fill in the title; leave the `Risk:` line open.
2. Start the planner in the lane's pane, with the planner settings from the conventions (default: the strongest model available, high effort):
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" spawn --pane <pane_id> --role planner --name <label>-plan \
     --agent <agent> [--model <model>] [--effort <effort>] [--account <account>] --accept-trust \
     --prompt "Read .fleet/BRIEF.md and write the plan in .fleet/PLAN.md. Change no other file. The Risk line is your reading; the paths under ## Files decide, so list every file. End with the line PLAN READY: 1 and stop."
   ```
3. Watch for the plan, in the background: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" watch <pane_id> --marker "PLAN READY: 1" --file .fleet/PLAN.md`.
4. Read PLAN.md. To ask for changes, send them to the same planner, and watch for the next number:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" send <pane_id> --text "<changes>. Revise .fleet/PLAN.md, change no other file, and end it with the line PLAN READY: <n+1>."
   ```
5. Approve it. With `Risk: none`:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" approve-plan <cwd> --by orch
   ```
   Any other risk: first ask the user, with the plan's approach and risks in a few lines. Pass `--by user` only after the user's yes. `approve-plan` refuses, and changes nothing, when the round is not ready or already approved, the worktree has changes outside `.fleet/` or an existing path outside it marked assume-unchanged or skip-worktree (`flagged`: git status cannot see a change there; an absent skip-worktree path of a sparse checkout does not count), or the lane has commits of its own (a planner changes and commits nothing but PLAN.md, which git does not track), or the risk needs the user. A path under `## Files` that matches a high-risk pattern makes the plan high risk whatever its `Risk:` line says: `--by orch` then refuses and names the matches (`high_risk`), so ask the user. `--by orch` also refuses a plan whose `## Files` lists no path: send it back to the planner, or ask the user. `--by user` is the only override, and it overrides only the risk and the missing files. When the project's patterns in PROJECT.md cannot be read, both refuse: fix the line that the reason names. Read its `reason` (with `paths`, `flagged` or `commits`), fix the cause, and run it again. When no branch, remote or tag holds the lane's base, or the lane had commits before the plan, pass `--base <ref>`.
6. Exit the planner, keep the pane, and start the implementer in it:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" retire <pane_id>
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" spawn --pane <pane_id> --role impl --name <label> \
     --agent <agent> [--model <model>] [--effort <effort>] [--permission-mode <mode>] [--account <account>] --accept-trust \
     --prompt "Read .fleet/BRIEF.md and .fleet/PLAN.md and execute them."
   ```
   The implementer's STATUS.md Plan section points to PLAN.md and lists only deviations from it. `spawn --role impl` (and a spawn without `--role`) refuses while the newest `PLAN READY: <n>` round has no `PLAN APPROVED: <n>` line. Exit code 2 with `changed: "nothing"` is that refusal, not a failed start: nothing was created, so approve the plan (step 5) and run the same command again. Go on with step 5.

The board shows `planning`, `plan ready <n>` and `plan approved <n>` for the lane until the implementer runs.

## 5. Record and watch

1. Nothing to write down for the lane itself: `fleet board` finds it, and the event log holds the spawn with its agent, account, model and prompt. A lane opened `--from <ID>` is already linked: `follow-up take` marked it, and the board shows the ID. Write a decision from step 1 to the project's PROJECT.md (`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" orch show` prints its path); add a follow-up with `follow-up add "<text>" --source "<where>"`. An orchestrator that still keeps the old `.fleet/FLEET.md` adds the lane there.
2. Start a watcher with the Bash tool and `run_in_background: true`:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" watch <pane_id> --marker "READY FOR REVIEW: round 1"
   ```
3. Tell the user in two or three lines: lane, pane, branch, account, and what happens next. Then wait for the watcher. Do not poll.

When the watcher ends:
- 0: run `/fleet:review` for this lane.
- 4 (blocked): read the pane. Cancel an unsafe prompt with `herdr pane send-keys <pane_id> Escape` and re-instruct with `send`. Then watch again.
- 6 (stopped): read STATUS.md (`Blocked`) and the pane. Answer with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" send <pane_id> --text "..."`. Then watch again.
- 5: read the `event` field of the watch result.
  - `agent-exited`: the shell pane stays. Read the pane. Start the agent again in it with `spawn --pane <pane_id> --role impl ...` and a prompt that points at BRIEF.md and STATUS.md.
  - `pane-gone`: the pane is closed, but the worktree stays. Open a new pane in it and start the agent there, with the settings of step 4:
    ```bash
    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" spawn --new-workspace --cwd <cwd> --role impl --name <label> \
      --agent <agent> [--model <model>] [--effort <effort>] [--permission-mode <mode>] [--account <account>] --accept-trust \
      --prompt "Read .fleet/BRIEF.md and .fleet/STATUS.md and continue this lane."
    ```
    Its `pane_id` is the lane's new pane: watch that one.
