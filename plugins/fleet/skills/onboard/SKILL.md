---
name: onboard
description: Set up the fleet plugin for this user — check Herdr, Claude Code, Codex, remuda and Herdr's agent hooks, ask how implementer, critic and planner lanes should run (agents, models, effort, review, planning and context rules), and write the user's conventions file (with account rules only when remuda is installed). Run it once, and again to change the conventions.
disable-model-invocation: true
allowed-tools:
  - Read
  - Write
  - AskUserQuestion
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" *)
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fleet *)
  - Bash(mkdir -p *)
---

# Fleet onboarding

The result is one conventions file in plain language. Every fleet skill reads it before it acts.

## 1. Check the environment

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" doctor --json`. Tell the user, in a few lines:

- **Herdr**: lanes need it. If it is missing, or this session does not run inside Herdr, say so. The conventions are still worth writing.
- **Agents**: which of `claude` and `codex` are installed, and the model and effort that each one's configuration sets (`claude.model`, `claude.effort`, `codex.model`, `codex.effort`). Offer only installed agents below.
- **Mode**:
  - `remuda`: remuda is installed, so lanes can run on different accounts. Each row of `remuda.accounts` carries the model and effort that the account's own configuration sets. Point out an account that sets none, or a different one: a lane on that account gets other settings unless the conventions name the model and effort.
  - `plain`: no remuda. Every lane uses the agent's own login, and nothing in the plugin switches accounts. `FLEET_NO_REMUDA=1` forces plain mode.
- **Herdr hooks**: `hooks` has one row for each configuration directory that a lane can use: each remuda account, or the caller's directories in plain mode. Herdr reads an agent's state (working, idle, blocked) from this hook, and `fleet watch` depends on that state. For each row with `ok: false`, show its `fix` and offer to run it. Run a fix only after the user agrees. Codex also runs a new hook only after one approval inside Codex: start Codex on that account, open its hooks review and press `t` (trust all). Run doctor again afterwards.
- **Existing file**: if `conventions.exists` is true, read the file first. Ask only about what the user wants to change.

## 2. Ask

Use AskUserQuestion: at most three calls of up to four questions each. Put the recommended option first. Where the doctor output shows a model or effort in the user's configuration, offer it first and mark it "(your config)". The user can always answer "Other" in their own words; keep their words in the file.

Call 1, implementer lanes:
1. Agent and model, for installed agents only. For example: "Claude Code, <claude.model> (your config) (Recommended)", "Claude Code, opus", "Claude Code, fable", "Codex, <codex.model>".
2. Effort for implementer lanes: "<claude.effort> (your config) (Recommended)" when it is set, otherwise "high (Recommended)"; then "xhigh", "max", "The agent's default (no flag)". Claude Code takes low, medium, high, xhigh and max.
3. Permission mode for Claude lanes: "auto (Recommended)", "acceptEdits", "manual (ask for each tool)".
4. Git: "Commit each step, never push; the orchestrator pushes and opens PRs (Recommended)", "Push the branch; the orchestrator opens the PR", "The lane opens the PR".

Call 2, review:
1. Critic: "Codex critic (Recommended when Codex is installed)", "Claude critic", "Both in parallel (dual review)", "No separate critic".
2. Codex model, when Codex is installed: "<codex.model> (your config) (Recommended)", up to two other models from `codex.models`, and "Codex's default (no flag)". Without Codex, ask for the Claude critic's model and effort instead: "Same as the implementers (Recommended)", "fable, high", "opus, xhigh".
3. Codex effort, when Codex is installed: "<codex.effort> (your config) (Recommended)" when it is set, otherwise "high (Recommended)"; then other levels from that model's `efforts` in `codex.models`, for example "xhigh" and "max". Leave this question out without Codex.
4. Stop rule: "From round 3, re-check fixes only; new findings block only at high severity (Recommended)", "Until the critic approves", "One round, then I decide".

Call 3, verification, context, planning and accounts:
1. Verification: "The project's documented checks, real results in STATUS.md (Recommended)", "Tests for the changed files only", "The full suite before each review".
2. Lane context: "Reset at a commit once context passes about 60 % (Recommended)", "Only near auto-compaction (about 80 %)", "Never; I decide".
3. Remuda mode, accounts. Build the options from `remuda.accounts`, for example "Lanes use my current account", "Spread lanes across accounts by remaining usage", "Ask me each time". Invite the user to pick Other and write rules in their own words, such as "the work account for work repositories, the personal account for personal projects, the second Codex account when the first passes 80 %".
   Plain mode, placement instead: "A new worktree workspace per lane (Recommended)", "A split beside the orchestrator", "A new tab".
4. Plan first, and the planner: "By the triage rules; planner on the strongest model available, high effort (Recommended)", "Always plan first, same planner", "Never; the implementer plans in STATUS.md". Invite the user to pick Other to name the planner's agent and model, for example "by the triage rules, planner Claude Code <model>, max effort".

## 3. Write

1. Get the path: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" conventions --path`. Create its folder with `mkdir -p`.
2. Start from `${CLAUDE_PLUGIN_ROOT}/defaults/conventions.md`. Replace each section with the user's answers, in short plain sentences that another agent can follow without this conversation. Write each agent's settings as `fleet spawn` options, for example `--agent claude --model opus --effort high`, or `--agent codex --model <model> --effort high`. Leave an option out where the user chose the agent's default.
   - `## Implementer lanes`: agent, model, effort, permission mode, git rules, verification.
   - `## Critic`: which agents, with their model and effort; what the critic writes; the stop rule. A Claude critic uses the implementers' Claude model and effort unless the user says otherwise. Write that into the file.
   - `## Planning and research`: when to plan first (the triage rules of the defaults, always, or never), the planner as `fleet spawn` options, and the researcher (the implementer settings unless the user says otherwise). Keep the defaults' research and approval rules unless the user changes them.
   - `## Context and handoff`: the reset threshold, when to compact, when to hand off, where new panes go.
   - `## Accounts`: **remuda mode only.** First list each account as `account — plan — what it is for`. Then write the user's rules in their words: which account the orchestrator, implementers and critics use, when to switch, and the usage level at which to skip an account (default: skip an account above 85 % of a limit in `fleet accounts`). In plain mode, leave this section out.
   - `## Projects`: optional. Per-repository notes that the user gives: setup command, check command, base branch, and a cleanup command that `/fleet:cleanup` offers after it removes lanes (for example, one that drops the test databases of removed worktrees).
3. Show the file. Tell the user that they can edit it in plain language at any time, and that the fleet skills read it each time they run.

## 4. Next steps

Give one line each:
- `/fleet:lane <task>` opens a lane, with a plan first when the triage says so.
- `/fleet:research <question>` has a researcher pane answer a question into the project home.
- `/fleet:handoff` moves this session to a new pane.
- `/fleet:context` keeps context small.
- `/fleet:accounts` shows accounts and usage (remuda mode only).
- `/fleet:cleanup` removes finished lanes, after you pick them.
- Several themes in one project: one orchestrator pane per theme, each opened with `fleet orch claim --name <theme>`; `fleet orch list` shows them.
