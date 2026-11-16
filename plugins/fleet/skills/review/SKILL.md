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

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" conventions`. Use its critic rules, its stop rule and its `Review round limit` (default 4).
2. Find the lane with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" board` (its phase is `ready <n>`; the board shows your orch's lanes and the unowned ones, `board --all` every lane of the project), or with `status`. Round `<n>` is the number on the newest `READY FOR REVIEW: round <n>` line in the lane's STATUS.md.
   If the lane's note on the board is `<n> review rounds (limit <m>): ask the user first`, stop here: the hard limit applies (section 5). Start no round before the user answers.
3. Read STATUS.md. Verification results must be real: no `PLACEHOLDER`, nothing "still running". The work must be committed (`git -C <worktree> status --short` is empty). If not, send the implementer one request to finish, and watch again.
4. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" risk <worktree>`. It lists the lane's changed paths that match a high-risk pattern and who covers each. Put its result in the request. A path that needs the user does not stop the review, but it stops the merge until the user said yes (the finish step in `/fleet:fleet`); tell the user now, so the answer is ready.

## 2. Write the request

Write `<worktree>/.fleet/REVIEW-REQUEST-<n>.md` from the template in `${CLAUDE_PLUGIN_ROOT}/skills/fleet/references/templates.md`:
- Base, branch and diff range. From round 2, give the commits since the last review and point at the dispositions in STATUS.md; cite prior board messages by `#<seq>` (`fleet board --seqs <n>`) instead of copying prose.
- Resolved helper path: include `Fleet CLI: <resolved python3 .../scripts/fleet>` so Codex/Claude critics (without `$CLAUDE_PLUGIN_ROOT`) can run `fleet post`.
- Focus: the acceptance criteria in BRIEF.md, plus the risks you see in the diff.
- High-risk paths: the matches from `fleet risk`, with their category and what covers them. Ask the critic to look hardest at those paths.
- The round rule. Apply the stop rule from the conventions here.
- The output file: `.fleet/REVIEW-<n>.md`. With two critics, give each its own file (`REVIEW-<n>-codex.md`, `REVIEW-<n>-claude.md`). Name every output file in the request by its exact name: `fleet board` counts the round as reviewed only when each named file has a `VERDICT:` line. A round on the pull request takes the next round number too (`REVIEW-<n>-pr.md`): the board counts the review rounds by the distinct numbers of `REVIEW-<n>.md` and `REVIEW-<n>-<name>.md`, each number once, and a file named any other way is not counted.

## 3. Start the critics

For each critic in the conventions (dual review: one Codex and one Claude):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" spawn --from <impl pane> --role critic --name <label>-review-<n> \
  --agent <agent> [--model <model>] [--effort <effort>] [--account <account>] --accept-trust \
  --prompt "Read .fleet/REVIEW-REQUEST-<n>.md and follow it."
```

In remuda mode, choose `<account>` with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" accounts --pick <agent>` (`--json` for the fields). Pass the `account` that it prints. When it prints `account: none` (every account of that agent is above the skip level), fall back as the conventions say, for example a Claude critic instead of a Codex one, or ask the user; when the conventions' account rules are prose that `--pick` does not read, choose by them. Tell the user about each `warning:` line (an account with unknown usage). In plain mode leave `--account` out.

The split starts in the lane's worktree. `--accept-trust` answers the folder trust dialog there, because the critic's account has not seen this new worktree. It never answers Codex's hooks review: when the result's `warnings` or its blocked `reason` names Codex project hooks, tell the user which account must trust them (they open Codex in the checkout and press `t`). Then wait via the unified board waiter (Bash with `run_in_background: true`, passing `head_seq` from `board --json` as `--since <seq>`):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" wait --since <seq> --mentions-only
```

With `--mentions-only`, the orchestrator sleeps through intermediate `impl <-> critic` rounds on `Risk: none` lanes (`fleet post` auto-syncs dual critics and enforces clean git + no `PLACEHOLDER`) and wakes only when a critic or implementer posts `--to orch --done` (`VERDICT: APPROVE`), `--blocked`, dual-critic disagreement (`escalate`), an uncovered high-risk path, or `Review round limit`. Pass `--lanes <branch>` only to isolate one lane, or omit `--mentions-only` for supervised rounds.

A critic from an earlier round can take the re-check instead of a new one: it remembers its findings. Send it the new request with `send <critic pane> --file <worktree>/.fleet/REVIEW-REQUEST-<n>.md` (or `post --to critic --file <worktree>/.fleet/REVIEW-REQUEST-<n>.md --round <n> "..."`).

## 4. Rule on the findings (when escalated or supervised)

On peer-to-peer `Risk: none` rounds, the implementer fixes `CHANGES REQUESTED` findings directly and posts round `n+1` to `critic`; the orchestrator wakes here only on `VERDICT: APPROVE` (`--done`), dual-critic disagreement, uncovered high-risk paths, `--blocked`, or the round limit.

When woken with findings to rule on, read each review file yourself. For each finding, decide: fix, decline with a reason, or ask the user (product and design decisions belong to the user). Where two critics disagree, the disagreement is usually a design decision: decide it, or ask. Add your rulings to each review file under `## Orchestrator rulings`.

- `VERDICT: APPROVE` and nothing left to fix: the lane is done. Retire the critics and continue with the finish steps in `/fleet:fleet`.
- Otherwise, send the implementer one pointer via `post` (or `send`):
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" post --to impl --file .fleet/REVIEW-<n>.md --round <n> "Read .fleet/REVIEW-<n>.md and its Orchestrator rulings. Fix or decline each finding, record dispositions in .fleet/STATUS.md, commit, then post round <n+1> to critic."
  ```
  With two critics, name each review file in the summary: "Read .fleet/REVIEW-<n>-codex.md and .fleet/REVIEW-<n>-claude.md and their Orchestrator rulings. ..." and the rest as above.
  Then `wait --since <new_seq> --mentions-only` and check the round limit first when woken.

## 5. Stop

The hard limit: once a lane's review files hold as many round numbers as the conventions' `Review round limit` (default 4), the board notes `<n> review rounds (limit <m>): ask the user first`. Then ask the user before another round: show the open findings and the rounds so far, and offer to merge with follow-ups, to run one more round, or to stop the lane. The helper never stops a review by itself; you do, on this note. A lane whose newest round every critic approved (also after its pull request opened), a merged lane and a removed one show no note.

Enforce the stop rule from the conventions. Once it applies, a finding below the blocking level becomes a follow-up in PROJECT.md (or an issue, if the user wants one), not another round. Each declined finding that the user may want later is a follow-up too. Add each one with the lane and the review file as its source:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" follow-up add "<the finding in one line>" --source "<branch> REVIEW-<n>.md"
```

It prints the follow-up's ID; a later lane takes it with `/fleet:lane --from <ID>`. Retire critic panes you no longer need: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" retire <critic pane> --close`.
