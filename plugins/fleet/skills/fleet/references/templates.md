# Fleet file templates

All lane files live in `.fleet/` at the top of the lane's worktree. `fleet prep` and `fleet worktree` create the folder and add `/.fleet/` to the repository's `.git/info/exclude`, so nothing here is committed. Keep each file short: link to plans, issues, PRs and specs instead of copying them.

## BRIEF.md (the orchestrator writes it; the lane reads it first)

```markdown
# Brief: <lane name>

## Goal
<one or two sentences: the outcome, not the steps>

## Scope
- In: <files, modules, behaviour this lane owns>
- Out: <what it must not touch; other lanes own it>

## Sources
- <issue / plan / spec / PR links and paths the lane must read>

## Setup (do this first)
- <project worktree setup, if the project documents one>

## Constraints
- <project rules that matter here: tests, style, migrations, flags>
- Ask the orchestrator (write the question under "Blocked" in STATUS.md and stop) instead of guessing on product or design decisions.

## Acceptance
- <observable checks: behaviour, tests that pass, docs updated>

## Verification
- <exact commands; run them in the foreground and record real results in STATUS.md>

## Reporting
- Write your plan as the first section of .fleet/STATUS.md before you edit code.
- Commit at each working step on branch <branch>. Do not push unless this brief says so.
- When done, add the line `READY FOR REVIEW: round 1` to STATUS.md and stop.
- <who reviews; critic files are .fleet/REVIEW-<n>.md>
```

## STATUS.md (the lane writes it and keeps it current)

```markdown
# Status: <lane name>

## Plan
1. ...

## Log
- <time> <what changed, commit sha>

## Verification
- <command> → <real result> (never a placeholder)

## Review dispositions
- REVIEW-1 #1 <finding> → fixed in <sha> | declined: <reason>

## Blocked
- <question for the orchestrator, if any>

## Next step
<the one thing the next session should do first>

READY FOR REVIEW: round <n>
```

The `READY FOR REVIEW: round <n>` line is the marker watchers look for. Move it to the end with a new round number each time; never reuse an old number.

## REVIEW-REQUEST-<n>.md (the orchestrator writes it for the critic)

```markdown
# Review request: <lane name>, round <n>

- Worktree: <path>   Branch: <branch>   Base: <base ref>
- Diff: `git diff <base>...HEAD` (or the commit range <a>..<b> for a re-check round)
- Read first: .fleet/BRIEF.md, .fleet/STATUS.md
- Focus: <correctness, concurrency, security, data loss, tests ...>
- Round rule: <round 1: full review | later rounds: re-check the fixes for REVIEW-<n-1> and look for regressions; new findings block only at <severity>>

Write findings to .fleet/REVIEW-<n>.md, most severe first, each with file:line, the failure scenario and a fix direction. End with one line: `VERDICT: APPROVE` or `VERDICT: CHANGES REQUESTED`. Do not edit code.
```

## HANDOFF.md (the outgoing session writes it; the successor reads it first)

```markdown
# Handoff: <what this work is>

Written <date time> by <pane / account / model>. Reason: <context full | account usage | model change | user asked>.

## Goal
<the outcome, and who it is for>

## State
- Done: <what is finished and verified, with commits / PRs / files>
- In progress: <exactly where you stopped: file:line, half-done edits, running jobs>
- Not started: <remaining steps>

## Next steps (in order)
1. <first concrete action>
2. ...

## Decisions and why
- <decision> — <reason; who decided (user or you)>

## Rules the user gave
- <instructions, preferences and limits from the conversation that are not written anywhere else>

## Commands and places
- <commands that worked, URLs, paths, pane ids, background jobs>

## Do not redo
- <checks already done, approaches already rejected and why>

## Open questions
- <for the user or the orchestrator>

## Suggested skills
- <skills the successor should load, e.g. fleet:fleet>
```

Redact secrets: never copy a key, token or password into a handoff; name where it lives instead.

## FLEET.md (the orchestrator keeps it in its own checkout)

```markdown
# Fleet: <what this fleet delivers>

Orchestrator: pane <id>, account <account or "plain">. Conventions: <path>.

## Lanes
| lane | pane (impl / critic) | workspace | worktree | branch | agent / account / model | state | next action |
|---|---|---|---|---|---|---|---|

## Decisions
- <date> <decision and reason>

## Done
- <lane> → <PR / merge sha>, cleaned up <date>
```

Update FLEET.md after every spawn, review round, handoff, merge and cleanup. After /clear, compaction or a handoff it is the only memory the orchestrator has.
