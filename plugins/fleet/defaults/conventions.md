# Fleet conventions

<!-- Plain-language rules that the fleet skills read before they open a lane, run a review,
     reset a lane or hand off. /fleet:onboard writes your own copy; edit it any time. -->

## Implementer lanes
- Agent: Claude Code with its default model, effort and permission mode.
- One lane per task, each in its own worktree and branch, based on the default branch.
- The lane writes its plan into .fleet/STATUS.md before it edits code, commits at each working step, and does not push. The orchestrator pushes and opens the pull request.
- Verification: the project's documented checks, run in the foreground, with the real results in STATUS.md.

## Critic
- One critic per review round, in its own pane in the lane's worktree: Codex when it is installed, otherwise Claude Code, with the model and effort that its configuration sets.
- The critic writes findings to .fleet/REVIEW-<n>.md, most severe first, and never edits code.
- Stop rule: from round 3, the critic re-checks earlier fixes and looks for regressions only. New findings block only when they are high severity; the rest become follow-ups.
- Review round limit: 4. When a lane's review files reach this many round numbers, `fleet board` says so, and the orchestrator asks the user before it starts another round. Keep this line's form: `- Review round limit: <n>`.

## Planning and research
- Research first when the orchestrator cannot name the files or the facts that a brief needs, or when several lanes need the same answer. A researcher pane answers into the project home (research/<slug>.md), and briefs and plans link to the answer.
- Plan first for a high-risk change (a migration, auth or payments, deletion logic, an incident hotfix), a change that spans subsystems, or a brief that cannot name its files. Otherwise the implementer plans in .fleet/STATUS.md.
- Planner: the strongest model available, with high effort. It writes only .fleet/PLAN.md, then stops.
- Researcher: the implementer settings. It writes only its research file.
- Approval: the user approves a high-risk plan; the orchestrator approves the others. A plan is high risk when its Risk is not none, or when a path under its ## Files matches a high-risk pattern (the built-in migration directories, or the project's ## High-risk paths in PROJECT.md).
- Before merge, the lane's real diff is checked against the same patterns. A risky path that the user did not approve (in a plan that showed its category, or with fleet approve-risk) needs the user's yes.

## Context and handoff
- Reset a lane (/clear, then re-brief from .fleet/) at its next commit once its context passes about 60 %, or once it has auto-compacted (`fleet board` notes a lane that compacted since its last reset).
- Use /compact only when an agent cannot stop at a boundary.
- Hand the orchestrator off to a new pane when its context passes about 70 %.
- New panes split beside the caller (right when the pane is wide, down when it is tall). Lanes get their own worktree workspace.

## Accounts
<!-- remuda mode only: plain mode has no accounts and ignores this section. `fleet accounts --pick <agent>` reads the
     two kinds of lines below; keep their form. An order line for codex names its accounts, for example:
- Pick order codex: codex:first, codex:second
     Without an order line, codex takes its accounts in remuda's order and claude the one with the most headroom. -->
- Skip level: 85 %. An account is skipped while any of its limits in `fleet accounts` is above this level. Keep this line's form: `- Skip level: <n> %`.
- Pick order claude: most headroom. Keep this line's form: `- Pick order <agent>: most headroom`, or `- Pick order <agent>: <account>, <account>` to take the named accounts in that order (an account the line does not name is not picked).
- Choose an account for a new pane with `fleet accounts --pick <agent>`. When it gives no account (every account is above the level), use the other agent where the conventions allow it (a Claude critic instead of a Codex one), or ask the user.

## Projects
<!-- Optional notes per repository: setup and check commands, the base branch, and a cleanup command that
     /fleet:cleanup offers after it removes lanes. For example:
- my-app: base branch main. Checks: `make check`. After lanes are removed, run `make prune-test-dbs` in the main checkout. -->
