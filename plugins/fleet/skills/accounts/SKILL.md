---
name: accounts
description: Show remuda accounts, cached usage and the account of each agent pane; choose an account for new work by the user's rules; or move this session or a lane to another account. In plain mode (no remuda) it explains that accounts are not switched. Use when the user asks about accounts, usage, limits, or switching accounts.
argument-hint: "[switch <account> [self | PANE]]"
allowed-tools:
  - Read
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" *)
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fleet *)
---

# Accounts

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" conventions` for the mode and the user's account rules.
2. Plain mode: say that remuda is not installed, so every lane uses the agent's own login and nothing switches accounts. To get switching, the user installs remuda and runs `/fleet:onboard` again. Stop here.
3. Show the state:
   - `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" accounts`: accounts and cached usage. An account whose cache is 24 h old or older is marked `STALE` (`(age unknown)` when remuda cannot tell). Pass `--live` only when the user asks for fresh numbers; it starts each agent briefly, seconds per account.
   - `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" status`: the ACCOUNT column shows each pane's account.
4. Summarize per account: plan, usage, which panes use it. Flag each account above the skip level in the conventions (default 85 % of a limit), and each `STALE` one: its numbers are old.
5. Recommend by the user's rules, in one or two lines. For a new claude or codex pane, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" accounts --pick <agent>`: it prints the account to pass as `--account` and why, by the conventions' `- Skip level: <n> %` and `- Pick order <agent>: ...` lines (it reads the cache only and never refreshes). `account: none` means every account of that agent is above the skip level: fall back as the conventions say (for example a Claude critic instead of a Codex one), or ask. A `warning:` line names an account whose usage is unknown (stale or missing cache) and how to refresh it. When the user's rules are prose that `--pick` cannot read (its `rules:` lines say which line it could not read), choose by the prose; when such a rule chooses by usage and a candidate is `STALE`, say so and offer `accounts --live` before you rely on it.

## Switch

- This session: `/fleet:handoff self <account>`. It resumes this conversation on the new account when the context is healthy, and writes a handoff file otherwise.
- A lane: `/fleet:handoff <pane> <account>`.
- New lanes: pass `--account <account>` to `spawn` (see `/fleet:lane`).

Account names are `provider:name` (for example `claude:work`), or a bare name when only one account of that agent has it.
