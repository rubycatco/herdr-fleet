---
name: review
description: Run a critic round on a fleet lane — write the review request, start the critics that the conventions name in the lane's worktree, wait for the verdict, rule on the findings, hand them to the implementer, and apply the stop rule. Use when a lane reports READY FOR REVIEW, or the user asks to review a lane.
argument-hint: "[lane label or pane]"
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
  - Bash(git diff *)
---

# Review a lane

## 1. Check that the lane is ready

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" conventions`. Use its critic rules and stop rule.
2. Find the lane in `.fleet/FLEET.md`, or with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" status`. Round `<n>` is the number on the newest `READY FOR REVIEW: round <n>` line in the lane's STATUS.md.
3. Read STATUS.md. Verification results must be real: no `PLACEHOLDER`, nothing "still running". The work must be committed (`git -C <worktree> status --short` is empty). If not, send the implementer one request to finish, and watch again.

## 2. Write the request

Write `<worktree>/.fleet/REVIEW-REQUEST-<n>.md` from the template in `${CLAUDE_PLUGIN_ROOT}/skills/fleet/references/templates.md`:
- Base, branch and diff range. From round 2, give the commits since the last review and point at the dispositions in STATUS.md.
- Focus: the acceptance criteria in BRIEF.md, plus the risks you see in the diff.
- The round rule. Apply the stop rule from the conventions here.
- The output file: `.fleet/REVIEW-<n>.md`. With two critics, give each its own file (`REVIEW-<n>-codex.md`, `REVIEW-<n>-claude.md`).

## 3. Start the critics

For each critic in the conventions (dual review: one Codex and one Claude):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" spawn --from <impl pane> --role critic --name <label>-review-<n> \
  --agent <agent> [--model <model>] [--account <account>] \
  --prompt "Read .fleet/REVIEW-REQUEST-<n>.md and follow it."
```

The split starts in the lane's worktree. Then, for each critic, start a background watcher (Bash with `run_in_background: true`):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" watch <critic pane> --marker "VERDICT:" --file .fleet/REVIEW-<n>.md
```

A critic from an earlier round can take the re-check instead of a new one: it remembers its findings. Send it the new request with `send <critic pane> --file <worktree>/.fleet/REVIEW-REQUEST-<n>.md`.

## 4. Rule on the findings

Read each review file yourself. For each finding, decide: fix, decline with a reason, or ask the user (product and design decisions belong to the user). Where two critics disagree, the disagreement is usually a design decision: decide it, or ask. Add your rulings to the review file under `## Orchestrator rulings`.

- `VERDICT: APPROVE` and nothing left to fix: the lane is done. Retire the critics and continue with the finish steps in `/fleet:fleet`.
- Otherwise, send the implementer one pointer:
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" send <impl pane> --text "Read .fleet/REVIEW-<n>.md and its Orchestrator rulings. Fix or decline each finding, record each disposition in .fleet/STATUS.md, commit, then add the line READY FOR REVIEW: round <n+1> to STATUS.md and stop."
  ```
  Watch for `READY FOR REVIEW: round <n+1>` and repeat from step 1.

## 5. Stop

Enforce the stop rule from the conventions. Once it applies, a finding below the blocking level becomes a follow-up in FLEET.md (or an issue, if the user wants one), not another round. Retire critic panes you no longer need: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" retire <critic pane> --close`.
