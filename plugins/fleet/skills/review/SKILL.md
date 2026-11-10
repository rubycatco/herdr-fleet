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
2. Find the lane with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" board` (its phase is `ready <n>`; the board shows your orch's lanes and the unowned ones, `board --all` every lane of the project), or with `status`. Round `<n>` is the number on the newest `READY FOR REVIEW: round <n>` line in the lane's STATUS.md.
3. Read STATUS.md. Verification results must be real: no `PLACEHOLDER`, nothing "still running". The work must be committed (`git -C <worktree> status --short` is empty). If not, send the implementer one request to finish, and watch again.
4. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" risk <worktree>`. It lists the lane's changed paths that match a high-risk pattern and who covers each. Put its result in the request. A path that needs the user does not stop the review, but it stops the merge until the user said yes (the finish step in `/fleet:fleet`); tell the user now, so the answer is ready.

## 2. Write the request

Write `<worktree>/.fleet/REVIEW-REQUEST-<n>.md` from the template in `${CLAUDE_PLUGIN_ROOT}/skills/fleet/references/templates.md`:
- Base, branch and diff range. From round 2, give the commits since the last review and point at the dispositions in STATUS.md.
- Focus: the acceptance criteria in BRIEF.md, plus the risks you see in the diff.
- High-risk paths: the matches from `fleet risk`, with their category and what covers them. Ask the critic to look hardest at those paths.
- The round rule. Apply the stop rule from the conventions here.
- The output file: `.fleet/REVIEW-<n>.md`. With two critics, give each its own file (`REVIEW-<n>-codex.md`, `REVIEW-<n>-claude.md`). Name every output file in the request by its exact name: `fleet board` counts the round as reviewed only when each named file has a `VERDICT:` line.

## 3. Start the critics

For each critic in the conventions (dual review: one Codex and one Claude):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" spawn --from <impl pane> --role critic --name <label>-review-<n> \
  --agent <agent> [--model <model>] [--effort <effort>] [--account <account>] --accept-trust \
  --prompt "Read .fleet/REVIEW-REQUEST-<n>.md and follow it."
```

The split starts in the lane's worktree. `--accept-trust` answers the folder trust dialog there, because the critic's account has not seen this new worktree. It never answers Codex's hooks review: when the result's `warnings` or its blocked `reason` names Codex project hooks, tell the user which account must trust them (they open Codex in the checkout and press `t`), and run that critic on another account or wait. Then, for each critic, start a background watcher (Bash with `run_in_background: true`) on that critic's own output file, the one that the request names for it:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" watch <critic pane> --marker "VERDICT:" --file .fleet/REVIEW-<n>.md
```

With two critics, the Codex critic's watcher reads `.fleet/REVIEW-<n>-codex.md` and the Claude critic's reads `.fleet/REVIEW-<n>-claude.md`. A watcher reads only the file that `--file` names, so a watcher on `REVIEW-<n>.md` never sees their verdicts.

A critic from an earlier round can take the re-check instead of a new one: it remembers its findings. Send it the new request with `send <critic pane> --file <worktree>/.fleet/REVIEW-REQUEST-<n>.md`.

## 4. Rule on the findings

Read each review file yourself. For each finding, decide: fix, decline with a reason, or ask the user (product and design decisions belong to the user). Where two critics disagree, the disagreement is usually a design decision: decide it, or ask. Add your rulings to each review file under `## Orchestrator rulings`.

- `VERDICT: APPROVE` and nothing left to fix: the lane is done. Retire the critics and continue with the finish steps in `/fleet:fleet`.
- Otherwise, send the implementer one pointer:
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" send <impl pane> --text "Read .fleet/REVIEW-<n>.md and its Orchestrator rulings. Fix or decline each finding, record each disposition in .fleet/STATUS.md, commit, then add the line READY FOR REVIEW: round <n+1> to STATUS.md and stop."
  ```
  With two critics, name each review file in the pointer: "Read .fleet/REVIEW-<n>-codex.md and .fleet/REVIEW-<n>-claude.md and their Orchestrator rulings. ..." and the rest as above.
  Watch for `READY FOR REVIEW: round <n+1>` and repeat from step 1.

## 5. Stop

Enforce the stop rule from the conventions. Once it applies, a finding below the blocking level becomes a follow-up in PROJECT.md (or an issue, if the user wants one), not another round. Each declined finding that the user may want later is a follow-up too. Add each one with the lane and the review file as its source:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" follow-up add "<the finding in one line>" --source "<branch> REVIEW-<n>.md"
```

It prints the follow-up's ID; a later lane takes it with `/fleet:lane --from <ID>`. Retire critic panes you no longer need: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" retire <critic pane> --close`.
