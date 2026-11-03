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

## Context and handoff
- Reset a lane (/clear, then re-brief from .fleet/) at its next commit once its context passes about 60 %.
- Use /compact only when an agent cannot stop at a boundary.
- Hand the orchestrator off to a new pane when its context passes about 70 %.
- New panes split beside the caller (right when the pane is wide, down when it is tall). Lanes get their own worktree workspace.

## Projects
<!-- Optional notes per repository: setup and check commands, the base branch, and a cleanup command that
     /fleet:cleanup offers after it removes lanes. For example:
- my-app: base branch main. Checks: `make check`. After lanes are removed, run `make prune-test-dbs` in the main checkout. -->
