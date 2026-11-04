# herdr-fleet

A Claude Code plugin for running fleets of coding-agent lanes in [Herdr](https://herdr.dev) panes. It opens lanes (worktree, brief, implementer pane), runs critic review rounds, keeps context small with `/clear` and `/compact`, and hands work off to a new pane. [remuda](https://github.com/ya-luotao/remuda) is optional: with it, lanes can run on different Claude and Codex accounts; without it, every lane uses the agent's own login and nothing switches accounts.

## How it works

You talk to one Claude Code session, the orchestrator. Each project has one, registered by its Herdr pane. It opens each lane in its own git worktree and Herdr workspace, starts the agents there, and drives them. The agents report through files in `.fleet/`; the helper turns those files, git, the pull requests and its event log into the board. In the diagrams, `/fleet:<skill>` is a skill, and `fleet <command>` is the helper CLI that the skills call (`plugins/fleet/scripts/fleet`).

```text
┌─ Herdr ──────────────────────────────────────────────────────────────────────┐
│                                                                              │
│  workspace: my-repo                 workspace: fix-login  (git worktree)     │
│  ┌──────────────────────────┐       ┌───────────────────┬──────────────────┐ │
│  │ orchestrator             │       │ implementer       │ critic           │ │
│  │ you + Claude Code        │ spawn │ Claude Code       │ Codex or Claude  │ │
│  │ fleet skills             │──────>│                   │ (review rounds)  │ │
│  │ fleet helper CLI         │ send  │ reads BRIEF.md    │ reads the diff   │ │
│  │                          │       │ writes STATUS.md  │ writes REVIEW-n  │ │
│  │ fleet board, digest      │<──────│                   │                  │ │
│  │                          │ watch │                   │                  │ │
│  └──────────────────────────┘       └───────────────────┴──────────────────┘ │
│                                                                              │
│                                     workspace: add-export  (another lane)    │
│                                     ┌───────────────────┐                    │
│                                     │ implementer       │                    │
│                                     └───────────────────┘                    │
└──────────────────────────────────────────────────────────────────────────────┘
```

One lane from start to finish. You type the first command; the orchestrator does the rest and asks you only for decisions. The thresholds and the stop rule are defaults; your conventions file can change them.

```text
setup (once)
  /fleet:onboard ─> ~/.config/fleet/conventions.md (every skill reads it)

each orchestrator session
  fleet orch claim     register this pane as the project's orchestrator (once)
  fleet digest         phase changes and lane events since the last look
  fleet board          one row per lane, any time

/fleet:lane "fix the login redirect"    <── you type this
  ├─ fleet worktree    branch, git worktree, its own Herdr workspace
  ├─ BRIEF.md          goal, scope, acceptance, checks
  ├─ triage            research first? plan first? (your conventions)
  │    ├─ /fleet:research    a researcher pane answers into the project home
  │    └─ plan first         a planner pane writes PLAN.md, then stops;
  │                          fleet approve-plan (you approve a high-risk plan)
  ├─ fleet spawn       implementer: plan (or PLAN.md) ─> code and commit ─> checks
  └─ fleet watch       waits for "READY FOR REVIEW: round 1"
        │
        ▼              from here on, the orchestrator drives
/fleet:review <─────────────────────────────────────────────────┐
  ├─ REVIEW-REQUEST-n  diff range, focus, round rule            │
  ├─ fleet spawn       critic: reads the diff, writes REVIEW-n  │
  ├─ rulings           fix / decline / ask you                  │
  └─ fleet send, watch implementer: fixes, commits, round n+1 ──┘
        │              repeats until APPROVE or the stop rule
        ▼
fleet risk           a risky diff you have not approved? you say yes first
push, open the PR, merge per your rules
  └─ /fleet:cleanup    fleet sweep, you pick, fleet cleanup
                       closes the follow-ups whose work merged
                       decisions go to PROJECT.md, follow-ups via fleet follow-up

along the way
  a lane asks or blocks   fleet watch returns; orchestrator answers or asks you
  a lane's context fills  at ~60 %: fleet reset at a commit, /clear, re-brief
  your session fills up   /clear: the hook re-briefs you from the project home
                          or /fleet:handoff; fleet orch claim --replace moves the role
  an account runs low     /fleet:accounts, then /fleet:handoff self <account>
                          (remuda only; plain mode never switches accounts)
```

## Requirements

- Herdr 0.9 or later, with Claude Code running inside a Herdr pane (`HERDR_ENV=1`).
- Claude Code; Codex is optional (used as a critic or implementer when installed).
- Python 3.8 or later (standard library only) and git.
- Optional: remuda 0.4 or later, for accounts.

## Install

From GitHub:

```bash
claude plugin marketplace add rubycatco/herdr-fleet
claude plugin install fleet@herdr-fleet
```

To work on the plugin, add your local checkout instead (`claude plugin marketplace add /path/to/herdr-fleet`). Claude Code then reads the folder in place, and edits take effect after `/reload-plugins`.

After installing, run `/reload-plugins` in open sessions, and `/fleet:onboard` once.

## Skills

| Skill | Does |
|---|---|
| `/fleet:onboard` | Checks the tools, asks how implementer and critic lanes should run, and writes your conventions file. Adds account rules only when remuda is installed. |
| `/fleet:fleet` | The orchestrator's playbook: helper commands, lane lifecycle, rules. Claude loads it when you work with lanes. |
| `/fleet:lane <task>` | Opens a lane: worktree and branch, `.fleet/BRIEF.md`, a triage (research or plan first), an implementer pane, a background watcher. `/fleet:lane --from F7` opens it for a follow-up of PROJECT.md and marks the follow-up as taken by the lane. |
| `/fleet:research <question>` | A researcher pane answers one question into `research/<slug>.md` in the project home, where later lanes reuse it. |
| `/fleet:review` | One critic round: review request, critic panes, verdict, rulings, findings back to the implementer, stop rule. |
| `/fleet:context` | Chooses between continuing, `/compact`, `/clear` with a re-brief, or a handoff, and does it. |
| `/fleet:handoff` | Moves this session or a lane to a new pane: through a handoff file, or by resuming the same conversation on another account. |
| `/fleet:accounts` | remuda accounts, usage and the account of each pane. |
| `/fleet:cleanup` | Lists every lane with a verdict (ready, waiting, keep), asks which ones to remove, then exits their agents, archives `.fleet/`, removes the worktrees, deletes merged local branches, closes the follow-ups whose work merged, and runs the project's own cleanup. A lane is kept while it has changed or untracked files (whatever the settings for untracked files, the file system monitor or the untracked cache say) or existing paths outside `.fleet/` marked assume-unchanged or skip-worktree (absent paths of a sparse checkout do not count), and cleanup checks again after the agents exit, right before the removal. Ignored files do not keep a lane and go with the worktree (only `.fleet/` is archived), and a mode-only change is not seen when `core.fileMode` is false. Only you start it. |

## Conventions

`/fleet:onboard` writes `~/.config/fleet/conventions.md` (`$XDG_CONFIG_HOME/fleet/conventions.md`, or `$FLEET_CONVENTIONS`). It is plain language: agents, models and effort for implementers, critics and planners, permission mode, git and verification rules, the review stop rule, when to research or plan first, context thresholds, and, with remuda, which account to use when. Edit it any time; the skills read it each time they run. Until it exists, the skills use `plugins/fleet/defaults/conventions.md`.

## Lane files

Each lane keeps its state in `.fleet/` at the top of its worktree. The helper adds `/.fleet/` to `.git/info/exclude`, so nothing is committed.

| File | Written by | Purpose |
|---|---|---|
| `BRIEF.md` | orchestrator | Goal, scope, sources, acceptance, verification. |
| `STATUS.md` | lane | Plan, log, real verification results, `READY FOR REVIEW: round <n>`. |
| `PLAN.md` | planner | An approved plan, when the lane plans first: `Risk:`, approach, steps, files, tests, risks; `PLAN READY: <n>`, then `PLAN APPROVED: <n> by orch\|user <date>` from `fleet approve-plan`. The paths under `## Files` decide whether the plan is high risk. |
| `RISK.md` | `fleet approve-risk` | The user's yes for the high-risk paths of the lane's real diff: one `RISK APPROVED: <category> <path> by user <date>` line each. |
| `REVIEW-REQUEST-<n>.md`, `REVIEW-<n>.md` | orchestrator, critic | One review round. |
| `HANDOFF.md` | outgoing agent | Everything a successor needs. |
| `panes.json` | `fleet spawn --role` | The role of each pane (impl, critic, orchestrator, helper, planner, researcher), and its `--task` file. |
| `FLEET.md` | orchestrator (old form) | Lane table, decisions, next actions, in the orchestrator's checkout. PROJECT.md and `fleet board` replace it; an existing one keeps working. |

`fleet cleanup` copies a lane's `.fleet/` (without `panes.json`) to the `lanes/` folder of its project home before it removes the worktree.

Markers such as `READY FOR REVIEW: round <n>`, `PLAN READY: <n>` and `RESEARCH DONE` count only at the start of a line, after optional list or bold marks; a mention inside a sentence does not.

After `/clear` and compaction, the SessionStart hook re-briefs a pane from these files by its role: the implementer from BRIEF, STATUS, HANDOFF and PLAN; a planner from BRIEF and PLAN, changing nothing else; a researcher or helper from its `--task` file; a critic from its newest review request; an old-style orchestrator from FLEET.md when its `Orchestrator: pane <id>` line names that pane or the pane has the role `orchestrator`. A pane with another role is told its role and the lane files. A session without a role gets a short pointer to the lane files only while no pane has the role `impl`; otherwise it gets nothing. When FLEET.md has no `Orchestrator:` line, every session there also gets a pointer to it. The registered orchestrator of a project is re-briefed from the project home instead (see below).

## Project home

Each repository is a project. The project is known by its normalized remote URL, so all clones of a repository share one home: `~/.fleet/projects/<name>/` (`$FLEET_HOME` moves the root). A repository without a remote is known by its main checkout, and keeps its project when it gets a remote.

| Path | Holds |
|---|---|
| `project.json` | The project's name and keys: the remote URL and the main checkout of each clone. |
| `events/YYYY-MM.jsonl` | The event log, one JSON object per line. |
| `lanes/<branch>-<time>/` | The `.fleet/` files of each lane that `fleet cleanup` removed. |
| `research/<slug>.md` | One research question each: the orchestrator writes the question and context, a researcher the findings and answer, ending with `RESEARCH DONE`. |
| `PROJECT.md` | The orchestrator's memory: `## Rules`, `## Decisions`, `## Follow-ups` (one `- [F7] <text>` item each, written by `fleet follow-up`), `## Notes`, and `## High-risk paths`, the project's own high-risk path patterns (`- migration: db/schema.rb`). The orchestrator writes it; you can edit it. `fleet orch claim` creates it. |
| `orch.json` | The project's orchestrator: its pane, workspace, checkout, session and claim time. Written by `fleet orch claim` and `release`. |
| `follow-ups.json` | The counter of follow-up IDs, `{"next": N}`. It only goes up. Written by `fleet follow-up`. |
| `board.json` | The newest lane board and when it was made, the cache for the re-brief. Written by `fleet board` and `fleet digest`. |
| `digest.json` | The digest's cursor, how far it read each monthly log (a byte offset), and each lane's phase at that time. Only `fleet digest` moves it. |
| `.lock` | Serializes `fleet orch claim`, `release`, a digest that consumes, board.json writes, and every follow-up write (`fleet follow-up` and the follow-up step of `fleet cleanup`). `--peek` and the SessionStart hook never take it. |

The helper records each command that changes something: `orch_claim` and `orch_release`, `phase` (a lane's phase change, recorded by `fleet digest`), `worktree`, `spawn` (agent, account, model, effort, role, prompt; recorded at launch) and `spawn_result` (whether the agent started, its session), `send` (the prompt), `reset`, `retire`, `plan_approved` (round, who approved, risk, the number of files under `## Files`, and the high-risk paths among them), `risk_approved` (the paths of a risky diff that you approved, and the HEAD commit), `cleanup` (the result, the last session of each agent, and the follow-ups it closed or released), `follow_up_added`, `follow_up_taken`, `follow_up_released` and `follow_up_done` (the follow-up's ID and text, its lane, and for `done` the reason), and `handoff` (`prep --handoff`: the path of the handoff file). Each event names its lane (branch, workspace, directory) and who ran the command: the Herdr pane and, inside Claude Code, the session. The SessionStart and SessionEnd hooks record each Claude session in a Herdr pane of a checkout that has `.fleet/` (`session_start`, `session_end`): the session id, the transcript path, the role, and why the session ended. Outside Herdr the hooks do not start Python, except to re-brief after /clear or compaction. Events outside a repository go to `~/.fleet/events/`. The log stays on this machine. It can hold prompts, so the helper keeps the home private (mode 0700).

`fleet log` prints the log of the project that holds the current directory (`--all`: every project). Filter with `--lane` (a branch, workspace, label or path), `--pane`, `--event` and `--since 7d`. `--sessions` prints one row per agent session, with its transcript. `--json` prints the events.

## One orchestrator per project

Each project has at most one orchestrator, known by its Herdr pane. `fleet orch claim` registers the calling pane in `orch.json` and creates `PROJECT.md`. It refuses while another registered pane is alive; the registered pane hands over with `fleet orch claim --pane <new pane> --replace`, and a dead pane never blocks. When herdr cannot tell whether the registered pane is alive, the claim refuses too, and `--force` takes over. It warns when a checkout of the project has a FLEET.md whose `Orchestrator:` line names another live pane. `fleet orch show` prints the record; `fleet orch release` removes it.

The orchestrator does not keep a lane table. `fleet board` derives one row per lane of the project from Herdr, git, the pull requests and the lane files: its phase (`removed`, `merged`, `pr open`, `blocked`, `planning`, `plan ready <n>`, `plan approved <n>`, `review <n>`, `reviewed <n>: <verdict>`, `ready <n>`, `working`, `idle`, `no agent`), its panes, its newest event, and a note: the plan warning (what makes `approve-plan` refuse), a risky diff that needs your yes, the blocked or next-step line of its STATUS.md, or else the IDs of the follow-ups that the lane holds (the `--json` record always has them, in `follow_ups`). Under the lanes it lists the project's research files that have no `RESEARCH DONE` line. `fleet digest` prints what changed since the last digest: each phase change, and the lanes' events, without the orchestrator's own. It records each phase change in the log and moves its cursor. It reads the log by byte offset, so an event is shown once, also when it arrives late. `--peek` only reads, and so does a digest from another pane while the registered one is alive or herdr cannot tell. `board` never consumes a change.

Follow-ups are the orchestrator's open work: review findings below the stop rule, gaps that a lane leaves, work that you park. They stay list items under `## Follow-ups` in PROJECT.md, each with a stable ID: `- [F7] <text>`, and ` (lane: <branch>)` once a lane took it. `fleet follow-up list` prints them, `add TEXT [--source S]` adds one and prints its ID, `take ID --lane BRANCH` links it to a lane (it refuses while another lane holds it), `release ID` frees it, and `done ID [--reason R]` removes it. `/fleet:lane --from F7` opens a lane for a follow-up and takes it; after it removes a lane, `fleet cleanup` closes a follow-up as done when the lane's pull request merged, or when the lane is merged and its branch moved since the lane was created (its oldest reflog entry, the creation, is not HEAD; a lane that was only rebased onto main also counts as moved). Otherwise (a lane whose branch never moved, a lane removed with `--unmerged`) it releases it. When git cannot tell, for example because reflog expiry removed the creation entry, it releases: an error can turn a close into a release, never the reverse. A line that you write by hand still counts: the next `follow-up` command gives it an ID. Each write runs under the project lock and keeps every other byte of the file. IDs come from the counter in `follow-ups.json`, which only goes up, so an ID is never used twice. Each change also records an event, so the log keeps the history of each follow-up, including the text of a removed one; an event write is best effort, as for every event kind. `fleet digest` shows the follow-up events that other panes recorded (the orchestrator's own are filtered, as for every event); `fleet log --event follow_up_done` shows them all.

At every session start of the orchestrator's pane (startup, resume, `/clear`, compaction), the SessionStart hook re-briefs it from the project home: the path of PROJECT.md with its rules and follow-ups, the cached board with its age, and the number of new events. It runs git (two calls to find the project, one more for the session record) and otherwise only reads files. The whole hook ends inside its time in hooks.json (10 s for SessionStart, 5 s for SessionEnd), because Claude Code drops all of a hook's output when it stops the hook. The times count from the moment the script can first read a clock, after Python has started (up to 1.5 s under heavy load, which the times allow for): all git calls of one hook run share one deadline 6 s after it (2 s for SessionEnd), and the run ends 8 s after it (3 s). The session record comes last; it gives up waiting for a locked log or for another process's new project at that end, and is skipped when no time is left. The re-brief is printed in any case. The hook fails closed for a registered pane (one whose pane an `orch.json` names). It reads the `orch.json` files once, first, each on its own (a file that cannot be read or parsed names no pane and drops no other), and decides once: the full re-brief when git gives the identity of the session's directory (its top and remotes) and that is the identity of its project. When git gives the identity of another project, and the `project.json` of each of its projects can be read and has keys, the pane is treated as any other pane. Anything else gives a short note instead: git found no repository here (exit code 128, for any reason: `orch claim` needs a checkout, so a registered pane without one has usually lost it), a git error (another exit code, an error, output that is not UTF-8, a failed remote query), git did not answer in time, a project file that could not be read, hook input that could not be read, or an error while the hook decides or builds the re-brief (an interrupt or exit request too). The note says that the pane is registered as the orchestrator of the project, that the hook could not confirm that the session works there and why, and that it should run `fleet digest --project NAME` and read PROJECT.md; when even the note cannot be built, a fixed short text says the same. It never gets a lane's text then. A pane that no `orch.json` names never gets the note. The hook reads at most about 1 MB of unread log, so it stays fast. The orchestrator then runs `fleet digest`. An orchestrator that still keeps FLEET.md works as before.

## Research and plans

The orchestrator stays small: it decides, and it does not dig. Two steps run on demand, each in its own pane, before an implementer starts. The triage rules in your conventions say when.

- **Research** answers an open question: how a subsystem works, what an API does, which option fits. The orchestrator writes the question to `research/<slug>.md` in the project home and starts a researcher (`fleet spawn --role researcher --task <file>`) in a checkout of the project. The researcher writes only that file and ends it with `RESEARCH DONE`. Briefs and plans link to the answer, and later lanes reuse it.
- **Plan** turns a brief into an approved plan before any code changes. A planner (`--role planner`) writes only `.fleet/PLAN.md`, ends it with `PLAN READY: <n>` and stops; the orchestrator asks for changes in later rounds. `fleet approve-plan` approves it: it refuses when the worktree has changes outside `.fleet/` (whatever the file system monitor or the untracked cache say), or existing paths there marked assume-unchanged or skip-worktree (an absent skip-worktree path of a sparse checkout does not count), or the lane has commits of its own, and a plan whose `Risk:` is not `none` (migration, auth or payments, deletion logic, hotfix) needs `--by user`, after your yes. Then the implementer starts fresh in the same pane, with BRIEF.md and PLAN.md, without the planner's exploration in its context.

Your yes for high-risk work holds in the helper, not only in the orchestrator's discipline:

1. **Spawn checks the approval.** In a lane with `.fleet/PLAN.md`, `fleet spawn --role impl` (or without `--role`) refuses until the newest `PLAN READY: <n>` round has its `PLAN APPROVED: <n>` line. There is no flag around it.
2. **High-risk paths override the `Risk:` line.** The built-in migration directories (`**/migrations/**`, `**/migration/**`, `**/migrate/**`, `**/alembic/**`) and the patterns under `## High-risk paths` in PROJECT.md are matched against the paths under `## Files` in PLAN.md. A match makes the plan high risk with that category, so `approve-plan --by orch` refuses and names the matches. It also refuses a plan that lists no files.
3. **The real diff is checked again.** At review and before merge, `fleet risk` matches the lane's actual changes (its own commits and uncommitted changes, whatever the file system monitor or the untracked cache say) against the same patterns. A match is covered by a plan that you approved and that showed its category, or by your yes recorded with `fleet approve-risk --by user` in `.fleet/RISK.md`. The board shows a risky diff that still needs you, and the orchestrator does not merge it until you say yes. When the diff cannot be told, the lane needs you as well.

`PLAN APPROVED` and `RISK APPROVED` lines are text in the lane's `.fleet/`: an agent that writes one by hand passes the gates. Only `approve-plan` and `approve-risk` write them, and the orchestrator reads the files.

## Helper CLI

`plugins/fleet/scripts/fleet` does the parts that must be exact: start agents and get past their first-run dialogs, submit prompts and confirm that they landed, wait for markers, confirm `/clear` and `/compact`, refuse to drive the calling pane, remove a lane only when it is clean, and delete its branch only when its merge holds every commit. `python3 plugins/fleet/scripts/fleet --help` lists the commands. `FLEET_NO_REMUDA=1` forces plain mode.

## Development

```bash
python3 -m unittest discover -s plugins/fleet/tests
claude plugin validate plugins/fleet
claude plugin validate .
```

## License

MIT. See [LICENSE](LICENSE).
