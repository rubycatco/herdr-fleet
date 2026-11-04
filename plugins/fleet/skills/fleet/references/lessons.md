# Field lessons

Lessons from fleet runs that each cost hours. `fleet spawn`, `fleet send` and `fleet reset` already handle the mechanical ones; the rest are judgment.

## Dispatch

- **A prompt sent to a pane can sit unsubmitted.** Long or multi-line text sent with `herdr pane run` often lands in a Claude Code composer as `[Pasted text #N]` without being submitted, mostly when the agent has just finished a turn. A status read right after dispatch can still say `working` from the previous turn. One fleet run lost two to three hours on each of four lanes this way. Codex panes do not show this problem. `fleet send` verifies that the prompt was taken and presses Enter when it was not. If you dispatch by hand, read the pane afterwards and require an empty composer and the new prompt above it.
- **Send one line that points at a file.** Put the task in a file (`BRIEF.md`, a `REVIEW-REQUEST`, `.fleet/inbox/*.md`) and send `Read <file> and follow it.` This avoids the paste problem and keeps the pane's context small. `fleet send` writes long text to `.fleet/inbox/` for you.
- **Some models refuse pasted prompts.** A pane that treats every typed prompt as pasted content may answer "please type this yourself" and do nothing. Start such lanes with the task as the launch argument (`fleet spawn --prompt`, which passes argv). For a later prompt, `/exit` the pane and relaunch it with `claude --resume <session-id> "<one-line pointer>"`. Do not fake keystrokes to get around a refusal.
- **A tip can eat the first Enter.** After a turn, Claude Code can show a "Heads up" tip with numbered choices. A one-line prompt then stays in the composer as plain text. One more Enter submits it. Never press the digit keys: they may land in the composer.
- **Pass literal file paths in briefs.** Shell expansions such as zsh `${(f)"$(git diff --name-only …)"}` can trip Claude Code's dangerous-command check. An unattended pane then waits on a permission prompt until it blocks. Cancel with `herdr pane send-keys <pane> Escape` and re-instruct. Never approve a flagged command on a lane's behalf.

## Starting agents

- **The folder trust dialog defaults to "No, exit".** Pressing Enter on it exits Claude Code. `fleet spawn --accept-trust` selects "Yes". Use it only for a worktree you just created, or a folder the fleet already works in. Trust is kept per account: a folder that one account trusts still shows the dialog to another, so an account switch usually needs `--accept-trust`.
- **Codex starts slowly.** It reports `idle` while its MCP servers start (often 10 s or more), and it shows the launch prompt as sent before it works on it. It may also show an update menu: skip it with Down and Enter (`fleet spawn` does this). Codex can also update itself at launch and exit with "restart Codex": `fleet spawn` then reports that the agent did not start. Read the pane and spawn it again. In the codex `/model` picker, a number key followed by Enter selects the highlighted row, not the numbered one. Use the arrow keys.
- **First-run prompts.** A new Claude Code version can show a one-time "fullscreen renderer" question. Escape dismisses it (`fleet spawn` does this).
- **New panes do not inherit your account.** A Herdr pane starts a fresh login shell. Bare `claude` uses the default config dir. `fleet spawn` passes your `CLAUDE_CONFIG_DIR` / `CODEX_HOME` on, or uses `remuda run <account>` in remuda mode.

## Watching

- **`done` is not "finished".** A lane can end its turn while verification still runs in a background shell, and leave a placeholder in its report. Brief lanes to run verification in the foreground and record real results. Before you dispatch a review, grep the report for `PLACEHOLDER`.
- **A lane that waits on its own background job reads `idle` or `done`.** Read the pane before acting on a stopped watcher.
- **Watch files and git, not scrollback.** A scrollback grep for a sentinel matches your own instruction text ("…print DONE 5") and the agent's restatement of it. Key watchers on a round marker inside a file (`grep -q "READY FOR REVIEW: round 2" .fleet/STATUS.md`), a new commit, or a PR head change. A watcher keyed on a file's existence fires at once in round 2: use the round number.
- **Pane reads need the full viewport.** A TUI pads its screen with blank rows, so `herdr pane read --lines 40` can return only padding. Read `viewport_rows + 200` lines and drop blank lines (`fleet` does this), or have the lane write long reports to files.

## Review loops

- **Implementer pane plus a separate adversarial critic pane works.** The implementer builds against the brief; the critic attacks the diff against the same brief.
- **Dual review finds the load-bearing decisions.** Run a Claude critic and a Codex critic on the same diff in parallel. Where they disagree is usually a design decision, not noise; decide it or ask the user.
- **Loops need a stop rule.** Adversarial critics keep finding rarer orderings. After round 2 or 3, write a ruling into the next review request: re-check the earlier fixes and look for regressions only; new findings block only at an agreed severity; everything else goes to follow-ups. Enforce the ruling yourself when a critic still returns CHANGES REQUESTED for a follow-up item.
- **Lanes plan for themselves.** Give the lane goal, sources and design points; let it write its own plan into STATUS.md and use its own review subagents on critical commits. The orchestrator keeps the independent review, integration, push, PR, merge and cleanup.

## Context and handoff

- **Reset long lanes at a commit boundary.** A lane past about 60 % context, or one that has compacted, gets slower and loses detail. At its next commit, make sure STATUS.md is current, then `fleet reset <pane>` (`/clear` plus a resume prompt). The SessionStart hook points the fresh session at `.fleet/`. `fleet reset` works for Claude lanes only. Hand a Codex lane off instead (`/fleet:handoff <pane>`): a fresh agent in the same pane, re-briefed from the lane's files.
- **Keep the orchestrator's state outside its context.** An orchestrator that compacts loses pane ids and decisions unless a file has them. The lanes are derived (`fleet board`, the event log); decisions, follow-ups and the user's rules go to the project's PROJECT.md, which the SessionStart hook shows to the orchestrator pane. Older fleets kept all of it in a hand-written FLEET.md, which drifted from the truth.
- **Switching accounts means a new agent process.** An agent's account is fixed at launch. To move a lane or yourself to another account, hand off (`/fleet:handoff`): write HANDOFF.md and start a successor, or, for a Claude agent with healthy context, resume the same session on the new account (`fleet spawn --resume`). A lane's successor starts in the lane's own pane after `fleet retire`; you, the caller, move to a new pane, because the helper never drives its own pane.

## Cleanup

- `/exit` the agent (`fleet retire <pane> --close`) before you remove its worktree.
- `herdr worktree remove --workspace <id>` removes the worktree and its workspace. It refuses a worktree with changes that `git status` shows. With `status.showUntrackedFiles=no`, git status hides untracked files, so it does not refuse them and deletes them. Before you remove a worktree by hand or ever pass `--force`, check it with `fleet sweep`, which counts untracked files whatever the git settings say.
- Let `fleet cleanup` delete a lane branch. It deletes the branch only when the merge holds every commit, also after a squash or rebase merge. Do not use `git cherry` for this check: after a squash merge it marks the lane's commits `+` (not merged).
- Run the project's own worktree cleanup (test databases, caches) if it documents one.
- Do not close panes, tabs or workspaces you did not create.
