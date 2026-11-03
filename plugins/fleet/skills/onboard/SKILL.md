---
name: onboard
description: Set up the fleet plugin for this user — check Herdr, Claude Code, Codex and remuda, ask how implementer and critic lanes should run, and write the user's conventions file (with account rules only when remuda is installed). Run it once, and again to change the conventions.
disable-model-invocation: true
allowed-tools:
  - Read
  - Write
  - AskUserQuestion
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" *)
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fleet *)
  - Bash(herdr integration status*)
  - Bash(mkdir -p *)
---

# Fleet onboarding

The result is one conventions file in plain language. Every fleet skill reads it before it acts.

## 1. Check the environment

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" doctor --json` and `herdr integration status`. Tell the user, in a few lines:

- **Herdr**: lanes need it. If it is missing, or this session does not run inside Herdr, say so. The conventions are still worth writing.
- **Agents**: which of `claude` and `codex` are installed. Offer only installed agents below.
- **Mode**:
  - `remuda`: remuda is installed, so lanes can run on different accounts.
  - `plain`: no remuda. Every lane uses the agent's own login, and nothing in the plugin switches accounts. `FLEET_NO_REMUDA=1` forces plain mode.
- **Agent state hooks**: if `herdr integration status` lists `claude` or `codex` as not installed, offer `herdr integration install claude` (or `codex`). It adds Herdr's state hook to that agent's configuration, and working/idle/blocked detection then becomes reliable. Run it only after the user agrees.
- **Existing file**: if `conventions.exists` is true, read the file first. Ask only about what the user wants to change.

## 2. Ask

Use AskUserQuestion: at most two calls of up to four questions each. Put the recommended option first. The user can always answer "Other" in their own words; keep their words in the file.

Call 1, implementer lanes:
1. Agent and model, for installed agents only. For example: "Claude Code, default model (Recommended)", "Claude Code, opus", "Claude Code, fable", "Codex".
2. Permission mode for Claude lanes: "auto (Recommended)", "acceptEdits", "manual (ask for each tool)".
3. Git: "Commit each step, never push; the orchestrator pushes and opens PRs (Recommended)", "Push the branch; the orchestrator opens the PR", "The lane opens the PR".
4. Verification: "The project's documented checks, real results in STATUS.md (Recommended)", "Tests for the changed files only", "The full suite before each review".

Call 2, review and context, plus accounts in remuda mode:
1. Critic: "Codex critic (Recommended when Codex is installed)", "Claude critic", "Both in parallel (dual review)", "No separate critic".
2. Stop rule: "From round 3, re-check fixes only; new findings block only at high severity (Recommended)", "Until the critic approves", "One round, then I decide".
3. Lane context: "Reset at a commit once context passes about 60 % (Recommended)", "Only near auto-compaction (about 80 %)", "Never; I decide".
4. Remuda mode, accounts. Build the options from `remuda.accounts` in the doctor output, for example "Lanes use my current account", "Spread lanes across accounts by remaining usage", "Ask me each time". Invite the user to pick Other and write rules in their own words, such as "team account for work repositories, max for personal projects, codex v0 when codex default passes 80 %".
   Plain mode, placement instead: "A new worktree workspace per lane (Recommended)", "A split beside the orchestrator", "A new tab".

## 3. Write

1. Get the path: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" conventions --path`. Create its folder with `mkdir -p`.
2. Start from `${CLAUDE_PLUGIN_ROOT}/defaults/conventions.md`. Replace each section with the user's answers, in short plain sentences that another agent can follow without this conversation:
   - `## Implementer lanes`: agent, model as a CLI value (for example `--model opus`), permission mode, git rules, verification.
   - `## Critic`: which agents, which model, what the critic writes, the stop rule.
   - `## Context and handoff`: the reset threshold, when to compact, when to hand off, where new panes go.
   - `## Accounts`: **remuda mode only.** First list each account as `account — plan — what it is for`. Then write the user's rules in their words: which account the orchestrator, implementers and critics use, when to switch, and the usage level at which to skip an account (default: skip an account above 85 % of a limit in `fleet accounts`). In plain mode, leave this section out.
   - `## Projects`: optional. Per-repository notes that the user gives: setup command, check command, base branch.
3. Show the file. Tell the user that they can edit it in plain language at any time, and that the fleet skills read it each time they run.

## 4. Next steps

Give one line each:
- `/fleet:lane <task>` opens a lane.
- `/fleet:handoff` moves this session to a new pane.
- `/fleet:context` keeps context small.
- `/fleet:accounts` shows accounts and usage (remuda mode only).
