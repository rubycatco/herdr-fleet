# Fleet file templates

The orchestrator's own files, PROJECT.md and the research files, live in the project home (`~/.fleet/projects/<name>/`). All lane files live in `.fleet/` at the top of the lane's worktree. `fleet prep` and `fleet worktree` create the folder and add `/.fleet/` to the repository's `.git/info/exclude`, so nothing here is committed. Keep each file short: link to plans, issues, PRs and specs instead of copying them.

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
  <with an approved .fleet/PLAN.md instead: the Plan section points to PLAN.md and lists only deviations from it>
- Commit at each working step on branch <branch>. Do not push unless this brief says so.
- A change to a high-risk path (a migration, auth or payments, deletion logic, a hotfix) that the approved plan did not show needs the user before merge: note it in STATUS.md.
- When done, add the line `READY FOR REVIEW: round 1` to STATUS.md and stop.
- <who reviews; critic files are .fleet/REVIEW-<n>.md>
```

## PLAN.md (the orchestrator starts it; the planner writes it)

The orchestrator copies this template to `.fleet/PLAN.md` with the title filled in and the `Risk:` line still open, then starts a planner. The planner keeps one value on the `Risk:` line, writes the sections, ends the file with `PLAN READY: <n>` on its own line and stops. Each revision ends with a new line and the next number. Only `fleet approve-plan` writes the `PLAN APPROVED: <n> by <who> <date>` lines; never write one by hand.

The `Risk:` line is the planner's reading, not the decision. The paths under `## Files` decide, with the project's high-risk patterns (see PROJECT.md below): when one of them matches, the plan is high risk with that category, whatever `Risk:` says. So `## Files` lists every file the plan changes, one list item each (a revision may add another `## Files` section; every one counts): the path is the first word after the dash and optional backticks, up to the first colon or space (`- db/migrate/<new>.sql: the new table`). Name a file that does not exist yet by its directory. A plan whose `## Files` lists no path is not complete: the orchestrator cannot approve it.

```markdown
# Plan: <lane name>

Risk: none | migration | auth-payments | deletion | hotfix

## Approach
<the design in a few lines: what changes, and why this way>

## Steps
1. <one commit: what it changes>. Verify: <command or check>
2. ...

## Files
- <path>: <what changes>

## Tests and verification
- <tests to add or change; exact commands>

## Risks and open questions
- <what can go wrong; what the orchestrator must decide>

## Out of scope
- <what this plan leaves alone>

PLAN READY: <n>
```

Risk values: `none`, `migration`, `auth-payments` (auth or payments), `deletion` (deletion logic), `hotfix` (incident hotfix). The orchestrator approves a plan only when its `Risk:` is `none`, `## Files` lists at least one path, and no listed path matches a high-risk pattern. Any other value, more than one, a missing `Risk:` line, an empty `## Files`, or a high-risk path needs the user's yes, and then `approve-plan --by user`. Write the `Risk:` line once, and start no other line with `Risk:`. Several values go on the one line, split by commas (`Risk: migration, deletion`).

Once the user approved the newest round, the plan covers the lane's real changes in the categories it shows: its `Risk:` values and the categories that its `## Files` paths match. A change to a high-risk path of another category needs the user again before merge (`fleet risk`, `fleet approve-risk`; see RISK.md below).

## RESEARCH (`<home>/research/<slug>.md`; the orchestrator asks, a researcher answers)

The orchestrator writes the title, `Asked:`, Question and Context; the researcher writes the rest. Look in `<home>/research/` first: an earlier answer may exist. Do not write the done line yourself: `fleet watch` would end at once.

```markdown
# Research: <the question, short>

Asked: <date>, by pane <orchestrator pane>, for <lane branch | the project>

Researcher: when the answer is complete, the last line of this file says RESEARCH DONE and nothing else. Until then, read code, docs and the web as you need, run only commands that change nothing, change no file except this one, and fill in Findings, Answer and Open questions.

## Question
<one question, and what the answer is for>

## Context
- <what the orchestrator already knows>
- <where to look: paths, docs, URLs>

## Findings
- <fact>. Source: <file:line, URL or command>

## Answer
<the answer in a few lines, ready to link from a brief or a plan>

## Open questions
- <what the researcher could not settle>
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

The `READY FOR REVIEW: round <n>` line is the marker watchers look for. A marker counts only at the start of a line (after a list dash, `**`, a backtick or `>`); a mention inside a sentence does not. Move it to the end with a new round number each time; never reuse an old number.

## REVIEW-REQUEST-<n>.md (the orchestrator writes it for the critic)

```markdown
# Review request: <lane name>, round <n>

- Worktree: <path>   Branch: <branch>   Base: <base ref>
- Diff: `git diff <base>...HEAD` (or the commit range <a>..<b> for a re-check round)
- Read first: .fleet/BRIEF.md, .fleet/STATUS.md
- Focus: <correctness, concurrency, security, data loss, tests ...>
- High-risk paths: <what `fleet risk` prints: each match with its category and who covers it, or "none">
- Round rule: <round 1: full review | later rounds: re-check the fixes for REVIEW-<n-1> and look for regressions; new findings block only at <severity>>

Write findings to .fleet/REVIEW-<n>.md, most severe first, each with file:line, the failure scenario and a fix direction. End with one line: `VERDICT: APPROVE` or `VERDICT: CHANGES REQUESTED`. Do not edit code.
```

## RISK.md (only `fleet approve-risk` writes it)

The record of the user's yes for a risky diff. `fleet risk` matches the lane's real changes (the paths of its own commits and of `git status`, outside `.fleet/`, whatever the file system monitor or the untracked cache say) against the project's high-risk patterns. A match is covered by the newest plan round when the user approved it and the plan shows that category, or by a line in this file for the same category and path. Everything else needs the user before merge. After the user's yes, `fleet approve-risk --by user` appends one line per path that needs the user, and records `risk_approved` with the HEAD commit:

```markdown
# Risk approvals: <branch>

RISK APPROVED: migration db/migrate/20261103_add_index.sql by user 2026-11-03T10:00:00+00:00
```

A later commit that touches no new risky path keeps the yes; a new risky path needs a new yes. Never write these lines by hand. A path whose name is not valid UTF-8 cannot be told exactly, so no line covers it and `approve-risk` refuses it: rename the file, or cover its category with a plan that the user approved. When `fleet risk` cannot tell the lane's changes (a detached HEAD without `--base`, git fails, git cannot tell the project, or PROJECT.md's patterns cannot be read), the lane needs the user, and `approve-risk` refuses until the cause is fixed. `approve-risk` also refuses when git cannot resolve HEAD, and refuses a path with a control character. Known gaps: an edit made while resolving a conflict inside a merge commit is not listed; with `--base <ref>`, a lane that merged (not rebased) the default branch lists that branch's files too. `fleet spawn` reads an existing pane's directory once, and the plan gate, the registry and the event all use that value; a shell that changes directory between that read and the launch starts the agent somewhere else.

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

## PROJECT.md (the orchestrator keeps it in the project home)

`fleet orch claim` creates it when it is missing, with these sections, each holding `- none`. `fleet orch show` prints its path. It holds only what nothing else knows: `fleet board` derives the lanes, and the event log records every command. The SessionStart hook shows its Rules and Follow-ups to the orchestrator pane at each session start. `## High-risk paths` holds the project's own high-risk path patterns.

```markdown
# Project: <name>

The orchestrator's memory for this project. `fleet board` and the event log keep the lanes; this file keeps
what nothing else knows. Keep each entry to one line.

## Rules
- <a rule the user gave for this project, e.g. "never push to main">

## Decisions
- <date> <decision and reason; who decided>

## Follow-ups
- <work left for later, one item each, as `fleet follow-up add` writes it: "[F1] text (from source)">

## Notes
- <anything else the next orchestrator session needs>

## High-risk paths
- <category: path pattern, e.g. "migration: db/schema.rb" or "auth-payments: app/auth/">
```

An empty section, `-`, `none` and `- none` count as empty.

`## Follow-ups` holds one list item per follow-up, `- [F1] <text>`, which `fleet follow-up` manages:

```markdown
## Follow-ups
- [F1] Add a CHANGELOG entry for 0.8.0. (from feat/a REVIEW-2.md)
- [F2] Retry the flaky upload test. (lane: fix/upload-retry)
```

Only the helper writes the `[F<n>]` IDs and the ` (lane: <branch>)` marks: `add` writes the ID (and ` (from <source>)` with `--source`), `take` the lane mark, `release` removes the mark, and `done` removes the line. IDs come from a counter in the project home (`follow-ups.json`), which only goes up, so an ID is never used twice, also after `done` removed it. The history of a follow-up, including the text of a removed one, is in the event log; an event write is best effort, as for every event kind. A line that the orchestrator writes by hand, without an ID, still counts: the next `follow-up` command gives it the next ID. Indented lines under an item belong to it, also after a blank line. The helper changes only the lines that it must and keeps the rest of the file as it is; `- none` goes with the first item and comes back when the last one goes. `fleet cleanup` closes a follow-up as done when the lane's pull request merged, or when the lane is merged and its branch moved since the lane was created (its oldest reflog entry, the creation, is not HEAD; a lane that was only rebased onto main also counts as moved). Otherwise (a lane whose branch never moved, a lane removed with `--unmerged`) it releases it. When git cannot tell, for example because reflog expiry removed the creation entry, it releases: an error can turn a close into a release, never the reverse.

`## High-risk paths` has one pattern per line: `- <category>: <pattern>`, with a category of `migration`, `auth-payments`, `deletion` or `hotfix`. Patterns match paths relative to the top of the repository, case-sensitive. `*` and `?` stay inside one directory, `**` crosses directories, and `[...]` is a character class. A pattern without a `/` (a trailing `/` aside) matches at any depth (`*.sql`, `auth/`); a pattern that ends in `/` matches everything under that directory (`app/auth/`), and a pattern that names a directory matches everything under it too. A leading `/` anchors a pattern at the top. The built-in patterns always apply: `**/migrations/**`, `**/migration/**`, `**/migrate/**` and `**/alembic/**` (category `migration`). Put the project's own files and folders here, such as `db/schema.rb`, `*.sql` or `db/cache_migrate/`. There is no way to turn a built-in off: a false match costs one yes from the user. A line of another form, an unknown category or a pattern that does not compile makes `approve-plan` refuse for both approvers, and the board and `fleet risk` say that the diff cannot be told, until the line is fixed.

## How `fleet board` reads the lane files

A marker counts only at the start of a line, after optional list or bold marks; a mention inside a sentence does not. The phase of a lane comes from these markers, the first rule that matches wins: `removed` (cleanup removed it), `merged` and `pr open` (its pull request), `blocked` (an agent pane is blocked, or STATUS.md has text under `## Blocked`), then, for a lane with PLAN.md while no implementer pane runs an agent and STATUS.md has no `READY FOR REVIEW` line: `planning` (a planner pane is working, or PLAN.md has no `PLAN READY: <n>` line), `plan approved <n>` (the newest round n has a `PLAN APPROVED: <n>` line) or `plan ready <n>` (the note says so when the worktree has changes outside `.fleet/` or paths outside it marked assume-unchanged or skip-worktree, or the lane has commits of its own: the checks of `approve-plan`); then `review <n>` (REVIEW-REQUEST-<n>.md is the newest request and a review file of round n has no `VERDICT:` line yet), `reviewed <n>: <verdict>` (every review file of the newest round has a verdict, and STATUS.md has no newer `READY FOR REVIEW` round; `CHANGES REQUESTED` wins), `ready <n>` (the highest `READY FOR REVIEW: round <n>` without a request), then `working` or `idle` (the implementer pane), else `no agent`. The review files of round n are the ones that REVIEW-REQUEST-<n>.md names (`REVIEW-<n>.md`, `REVIEW-<n>-<critic>.md`); a named file that does not exist yet has no verdict. When the request names none, the files that exist count. The board also shows the first line under `## Blocked` and `## Next step` in STATUS.md, and lists the project's research files that have no `RESEARCH DONE` line. For a lane with code to check (every phase except `removed`, `merged`, `planning`, `plan ready <n>` and `plan approved <n>`), it matches the lane's real changes against the high-risk patterns, like `fleet risk`, and keeps the result in the record's `risk` field. The NOTE column then shows, in this order, the first that applies: the plan warning, `risky diff needs the user (<count>): <category> <path>` or `risky diff cannot be told: <why>`, the blocked line, the next step, the IDs of the follow-ups that the lane holds.

## FLEET.md (the old form: the orchestrator kept it in its own checkout)

PROJECT.md and `fleet board` replace this file. An existing FLEET.md keeps working: after /clear or compaction, the SessionStart hook still re-briefs the pane that its `Orchestrator:` line names, and `fleet orch claim` warns while that pane is alive. Do not start a new one.

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

An orchestrator that still uses FLEET.md updates it after every spawn, review round, handoff, merge and cleanup.
