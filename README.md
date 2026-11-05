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
| `lane.json` | `fleet worktree`, `spawn`, `adopt` | The orch that owns the lane, by name: `orch`, `project`, `branch`, `since` and `by` (the pane that wrote it). `worktree` writes it, `spawn` writes it into an unowned lane, `adopt` sets or moves it. A lane without it is unowned. |
| `panes.json` | `fleet spawn --role` | The role of each pane (impl, critic, orchestrator, helper, planner, researcher), and its `--task` file. |
| `FLEET.md` | orchestrator (old form) | Lane table, decisions, next actions, in the orchestrator's checkout. PROJECT.md and `fleet board` replace it; an existing one keeps working. |

`fleet cleanup` copies a lane's `.fleet/` (without `panes.json`) to the `lanes/` folder of its project home before it removes the worktree.

Markers such as `READY FOR REVIEW: round <n>`, `PLAN READY: <n>` and `RESEARCH DONE` count only at the start of a line, after optional list or bold marks; a mention inside a sentence does not.

After `/clear` and compaction, the SessionStart hook re-briefs a pane from these files by its role: the implementer from BRIEF, STATUS, HANDOFF and PLAN; a planner from BRIEF and PLAN, changing nothing else; a researcher or helper from its `--task` file; a critic from its newest review request; an old-style orchestrator from FLEET.md when its `Orchestrator: pane <id>` line names that pane or the pane has the role `orchestrator`. A pane with another role is told its role and the lane files. A session without a role gets a short pointer to the lane files only while no pane has the role `impl`; otherwise it gets nothing. When FLEET.md has no `Orchestrator:` line, every session there also gets a pointer to it. A registered orchestrator is re-briefed from the project home instead (see below).

## Project home

Each repository is a project. The project is known by its normalized remote URL, so all clones of a repository share one home: `~/.fleet/projects/<name>/` (`$FLEET_HOME` moves the root). A repository without a remote is known by its main checkout, and keeps its project when it gets a remote.

| Path | Holds |
|---|---|
| `project.json` | The project's name and keys: the remote URL and the main checkout of each clone. |
| `events/YYYY-MM.jsonl` | The event log, one JSON object per line. |
| `lanes/<branch>-<time>/` | The `.fleet/` files of each lane that `fleet cleanup` removed. |
| `research/<slug>.md` | One research question each: the orchestrator writes the question and context, a researcher the findings and answer, ending with `RESEARCH DONE`. |
| `PROJECT.md` | The memory that every orchestrator of the project shares: `## Rules`, `## Decisions`, `## Follow-ups` (one `- [F7] <text>` item each, written by `fleet follow-up`), `## Notes`, and `## High-risk paths`, the project's own high-risk path patterns (`- migration: db/schema.rb`). The orchestrators write it; you can edit it. `fleet orch claim` creates it. Once the project has several orchestrators, each keeps the decisions and notes of its own theme in its `ORCH.md`. |
| `orch.json` | The project's orchestrators (orchs), by name: the pane, workspace, checkout, session and claim time of each. With one orch named `default` it keeps the shape of 0.8.x, one record; from a second name on it is format 2 (see Orchestrators). Written by `fleet orch claim`, `rename` and `release`. |
| `orchs/<name>/` | Each orch's own files in format 2: `ORCH.md` (the decisions and notes of its theme), `digest.json` and `board.json`. |
| `follow-ups.json` | The counter of follow-up IDs, `{"next": N}`. It only goes up. Written by `fleet follow-up`. |
| `board.json` | The newest lane board and when it was made, the cache for the re-brief. Written by `fleet board` and `fleet digest`. The file at the root belongs to the one orch of the legacy shape; in format 2 each orch has its own in `orchs/<name>/`. |
| `digest.json` | The digest's cursor, how far it read each monthly log (a byte offset), and each lane's phase at that time. Only `fleet digest` moves it. At the root for the legacy shape, else in `orchs/<name>/`. |
| `.lock` | Serializes `fleet orch claim`, `release`, `rename`, `fleet adopt`, a digest that consumes, board.json writes, and every follow-up write (`fleet follow-up` and the follow-up step of `fleet cleanup`). `--peek` and the SessionStart hook never take it. |

The helper records each command that changes something: `orch_claim`, `orch_release` and `orch_rename`, `adopt` (a lane's orch set or moved), `phase` (a lane's phase change, recorded by `fleet digest`), `worktree`, `spawn` (agent, account, model, effort, role, prompt; recorded at launch) and `spawn_result` (whether the agent started, its session), `send` (the prompt), `reset`, `retire`, `plan_approved` (round, who approved, risk, the number of files under `## Files`, and the high-risk paths among them), `risk_approved` (the paths of a risky diff that you approved, and the HEAD commit), `cleanup` (the result, the last session of each agent, and the follow-ups it closed or released), `follow_up_added`, `follow_up_taken`, `follow_up_released` and `follow_up_done` (the follow-up's ID and text, its lane, and for `done` the reason), and `handoff` (`prep --handoff`: the path of the handoff file). Each event names its lane (branch, workspace, directory), its orch in the field `orch` (for an event of a lane, the lane's owner when the event was recorded, read from its `lane.json` with no git; for a claim, rename, release, `adopt`, `cleanup` or a digest's `phase`, the orch it concerns), and who ran the command: the Herdr pane and, inside Claude Code, the session. The SessionStart and SessionEnd hooks record each Claude session in a Herdr pane of a checkout that has `.fleet/` (`session_start`, `session_end`): the session id, the transcript path, the role, and why the session ended. Outside Herdr the hooks do not start Python, except to re-brief after /clear or compaction. Events outside a repository go to `~/.fleet/events/`. The log stays on this machine. It can hold prompts, so the helper keeps the home private (mode 0700).

`fleet log` prints the log of the project that holds the current directory (`--all`: every project). Filter with `--lane` (a branch, workspace, label or path), `--orch` (the events of that orch: those that name it, also by a name it had before a rename, an `adopt` that took a lane from it, and those without an `orch` whose lane it owned at the time, by the `worktree`, `adopt`, `cleanup` and adopting `spawn` events of the whole log; `--event`, `--since` and `--lane` cut the result, not that history), `--pane`, `--event` and `--since 7d`. `--sessions` prints one row per agent session, with its transcript. `--json` prints the events.

## Orchestrators

Each orchestrator of a project, an orch, is known by a name and by its Herdr pane. A project can have several, one per theme (billing, search, a migration), each in its own pane, all sharing the project home. `fleet orch claim` registers the calling pane in `orch.json` and creates `PROJECT.md`. A project with no orch gets the orch `default`; `fleet orch claim --name <theme>` opens another theme. An orch name is lowercase: `a-z`, `0-9`, `.`, `_` and `-`, at most 40 characters (it names a folder, and two names that differ only in case would share it on a file system that ignores case). Without `--name`, a claim takes the name that the pane already holds (a re-claim), else the one orch of the project (the per-name rules below decide), and with several orchs it refuses and lists them. Per name the rules stay: the claim refuses while another registered pane of that name is alive; the registered pane hands over with `fleet orch claim --pane <new pane> --replace`, which keeps the name and clears the session, and a dead pane never blocks. When herdr cannot tell whether the registered pane is alive, the claim refuses too, and `--force` takes over. A pane holds one orch of a project: a claim of a second name from it refuses. It warns when a checkout of the project has a FLEET.md whose `Orchestrator:` line names another live pane. `fleet orch show` prints the caller's orch (`--orch NAME`: another one), `fleet orch list` every orch with its pane, whether the pane is alive, and the lane count of its cached board (`orch list --project NAME` reads files and asks herdr, with no git; a bare `orch list` finds the project from the checkout, which needs git), `fleet orch rename OLD NEW` renames one (its lanes move with it, and its record keeps the old name: the events that name it go to it, and no other orch can claim it or be renamed to it), and `fleet orch release` removes the caller's record (`--orch NAME` names it when the caller holds none; `--force`: another pane's).

`orch.json` has two shapes. While the only orch is `default`, it keeps the shape of 0.8.x, one record with a top-level `pane`, and the project's `digest.json` and `board.json` stay at the root of the project home. A project with one orch stays readable by 0.8.x: `orch.json` and `PROJECT.md` keep their bytes, there is no `orchs/` folder, and `digest.json` and `board.json` keep their shape. Their contents move on as with 0.9.0's events and rows: the byte offsets in `digest.json` grow because events carry `orch`, and board rows gain `orch` and `checkout`. The 0.8.3 helper runs `orch show`, `digest` and `board` on such a home. The first claim of another name, or an `orch rename`, switches it to format 2, `{"format": 2, "orchs": {"<name>": {"pane", "workspace", "checkout", "session", "claimed_at"}}}`, under the project lock: the root cursor and board move into `orchs/<name>/` of the orch that had them, and each orch gets `orchs/<name>/ORCH.md` for the decisions and notes of its theme. A project with no orch whose first claim names another theme starts in format 2. The switch happens once; a project never goes back, also not when a release leaves one orch. `PROJECT.md` stays shared: its rules, high-risk paths and follow-ups, and the decisions and notes that concern the whole project. A helper of 0.8.x reads format 2 as no orch, and its claim would rewrite the file in its own shape and drop the orchs that it holds. That window opens at any switch to format 2: the claim of a second orch, an `orch rename`, or a first claim with a name other than `default`; and only for a session whose plugin comes from an older checkout of the plugin. Upgrade every checkout of the plugin to 0.9.0 and run `/reload-plugins` in each open session before any of them.

Each lane belongs to one orch, or to none. `fleet worktree` records the caller's orch in the lane's `.fleet/lane.json` (`--orch NAME` names another); `fleet spawn` into a lane that no orch owns records the caller's orch there, and `fleet adopt TARGET...` sets it for a lane, or for any linked worktree made by hand. The owner is a name, never a pane, so it survives a handoff. `fleet board` and `fleet digest` show an orch its own lanes and the unowned ones; when no other note applies, the board marks a lane whose main checkout is not the one of its orch's checkout `in another clone: <path>`, and, in a project with several orchs, an unowned lane `unowned: adopt it`. An event goes to the orch that owned its lane when it was recorded, once, wherever the lane goes later; an event without an owner (an unowned lane, a log of 0.8.x) follows the lane's owner now. An `adopt` that moves a lane from orch A to orch B goes to both: to B (its `orch`) and to A (its `old`), also under a name that either has after a rename, in the digest and in `fleet log --orch`. So A's next digest shows the adopt once, with the lane's phase change to `gone`, and its board no longer lists the lane; the pane that ran the adopt does not see it in its own digest, as with every own event. After the adopt the lane is B's: its later events go to B. While several orchs remain after a release, the released orch's lanes and their events go to none of them (`board --all` shows them under the released name, and `fleet log` keeps every event) until an orch of that name is claimed again or the lanes are adopted; when one orch remains, its view is not filtered and it sees every lane. A release also drops the names that the orch had before a rename. `fleet board --all` shows every lane of the project, with an ORCH column, and saves nothing. A board is saved only when the orchs of the project and the owners of its lanes did not change while it was made; otherwise it says so and saves nothing. `fleet sweep` prints each lane's orch. `fleet cleanup` and `fleet adopt` refuse a lane of another orch (`lane <path> belongs to orch <name>; pass --other-orch to remove it`, or `to adopt it`) and change nothing, until `--other-orch` is passed; the orchestrator passes it only for a lane that you picked. A caller that is no orch, in a project with several, passes `--orch NAME` or `--other-orch`. The owner check and the change that it guards are one step under the project lock: in `adopt`, in the adoption of a first `spawn`, and in `cleanup`, which reads the owner again after the agents exit, refuses that lane when its orch changed since the check, and marks the lane in `lane.json` while it removes it (an `adopt` refuses a lane that a running cleanup marked, and `spawn` starts no agent in it, owned or not; a mark whose process is gone does not count). Under the lock these steps read and write files only: their git calls run before it, and the lock is not held across the Herdr call. `cleanup` finds the project of each lane once, before its checks, and locks that project at the mark; when git cannot tell the project (an error or a timeout), the lane stays. One limit is known: in a repository that has no fleet project yet (no fleet event was ever recorded there, so only a worktree made by hand can be a lane), `cleanup` takes no project lock, and an `orch claim` that creates the project and an `adopt` of that lane while the cleanup runs do not stop the removal; the checks for uncommitted and unmerged work still run right before it. A mark whose process id now belongs to another program also keeps the lane; the refusal names the file, and removing the `removing` key from it clears the mark when no cleanup runs. `retire`, `reset`, `send` and `watch` do not look at the owner. A worktree belongs to the clone that its own `.git` file names, also when another clone copied the first one's `.git/worktrees/` and lists it too: `sweep` lists it once, under that clone, and `cleanup` removes it from there. When `herdr worktree remove` fails, `cleanup` keeps that lane's worktree and branch, as 0.8.3 does, and its result names in `by_hand` the command that removes it by hand. Every `git worktree remove` that fleet runs passes `-c status.showUntrackedFiles=all`, so git refuses a worktree with an untracked file whatever your configuration.

The orchestrator does not keep a lane table. `fleet board` derives one row per lane of its view from Herdr, git, the pull requests and the lane files: its phase (`removed`, `merged`, `pr open`, `blocked`, `planning`, `plan ready <n>`, `plan approved <n>`, `review <n>`, `reviewed <n>: <verdict>`, `ready <n>`, `working`, `idle`, `no agent`), its panes, its newest event, and a note: the plan warning (what makes `approve-plan` refuse), a risky diff that needs your yes, the blocked or next-step line of its STATUS.md, or else the IDs of the follow-ups that the lane holds (the `--json` record always has them, in `follow_ups`). Under the lanes it lists the project's research files that have no `RESEARCH DONE` line. `fleet digest` prints what changed since the orch's last digest: each phase change, and the lanes' events, without the orchestrator's own. It records each phase change in the log and moves the orch's own cursor; a digest from a pane that is no orch, in a project with several, names the orch with `--orch NAME`. It reads the log by byte offset, so an event is shown once, also when it arrives late. `--peek` only reads, and so does a digest from another pane while the orch's registered pane is alive or herdr cannot tell. `board` never consumes a change.

Follow-ups are the open work of the project, one list that every orch sees, without an owner (the `--source` text can name the theme): review findings below the stop rule, gaps that a lane leaves, work that you park. They stay list items under `## Follow-ups` in PROJECT.md, each with a stable ID: `- [F7] <text>`, and ` (lane: <branch>)` once a lane took it. `fleet follow-up list` prints them, `add TEXT [--source S]` adds one and prints its ID, `take ID --lane BRANCH` links it to a lane (it refuses while another lane holds it), `release ID` frees it, and `done ID [--reason R]` removes it. `/fleet:lane --from F7` opens a lane for a follow-up and takes it; after it removes a lane, `fleet cleanup` closes a follow-up as done when the lane's pull request merged, or when the lane is merged and its branch moved since the lane was created (its oldest reflog entry, the creation, is not HEAD; a lane that was only rebased onto main also counts as moved). Otherwise (a lane whose branch never moved, a lane removed with `--unmerged`) it releases it. When git cannot tell, for example because reflog expiry removed the creation entry, it releases: an error can turn a close into a release, never the reverse. A line that you write by hand still counts: the next `follow-up` command gives it an ID. Each write runs under the project lock and keeps every other byte of the file. IDs come from the counter in `follow-ups.json`, which only goes up, so an ID is never used twice. Each change also records an event, so the log keeps the history of each follow-up, including the text of a removed one; an event write is best effort, as for every event kind. `fleet digest` shows the follow-up events that other panes recorded (the orchestrator's own are filtered, as for every event); `fleet log --event follow_up_done` shows them all.

At every session start of an orch's pane (startup, resume, `/clear`, compaction), the SessionStart hook re-briefs it from the project home: the orch's name, the path of PROJECT.md with its rules and follow-ups, in format 2 the path of the orch's ORCH.md with its notes, the orch's cached board with its age, and the number of new events in its view. It first matches the pane and the session: when the hook input's session id is the `session` of exactly one orch registered for that pane (a claim and a consuming digest record it, and compaction and resume keep it), that orch gets the full re-brief with no git call, also from another project's checkout. Otherwise it runs git (two calls to find the project, one more for the session record) and otherwise only reads files. The whole hook ends inside its time in hooks.json (10 s for SessionStart, 5 s for SessionEnd), because Claude Code drops all of a hook's output when it stops the hook. The times count from the moment the script can first read a clock, after Python has started (up to 1.5 s under heavy load, which the times allow for): all git calls of one hook run share one deadline 6 s after it (2 s for SessionEnd), and the run ends 8 s after it (3 s). The session record comes last; it gives up waiting for a locked log or for another process's new project at that end, and is skipped when no time is left. The re-brief is printed in any case. The hook fails closed for a registered pane (one whose pane an `orch.json` names). It reads the `orch.json` files once, first, each on its own (a file that cannot be read or parsed names no pane and drops no other), and decides once: the full re-brief when git gives the identity of the session's directory (its top and remotes) and that is the identity of its project. When git gives the identity of another project, and the `project.json` of each of its projects can be read and has keys, the pane is treated as any other pane. Anything else gives a short note instead: git found no repository here (exit code 128, for any reason: `orch claim` needs a checkout, so a registered pane without one has usually lost it), a git error (another exit code, an error, output that is not UTF-8, a failed remote query), git did not answer in time, a project file that could not be read, hook input that could not be read, or an error while the hook decides or builds the re-brief (an interrupt or exit request too). The note says that the pane is registered as an orch of the project (by name), that the hook could not confirm that the session works there and why, and that it should run `fleet digest --project NAME` and read PROJECT.md; when even the note cannot be built, a fixed short text says the same. It never gets a lane's text then. A pane that no `orch.json` names never gets the note. The hook reads at most about 1 MB of unread log, so it stays fast. The orchestrator then runs `fleet digest`. An orchestrator that still keeps FLEET.md works as before.

### A checkout that several orchestrators share

Several orchs often work from one checkout of the repository, usually its main checkout. Keep that checkout on the default branch, and update it only with `git fetch` and a fast-forward (`git merge --ff-only`): a branch switch, a rebase or a reset there changes the files under every orch at once. Branch work, dev servers and tests run in lanes, where each worktree has its own files, ports and build output. An orchestrator that needs its own working copy keeps its own clone of the repository.

### Several clones of one repository

Before 0.9.0 the way to run two orchestrators for one repository was a second clone, made by copying the first one. All clones share one project home, so they share one orch.json. To move to themes in one clone (placeholders: `<kept clone>`, `<extra clone>`, `<theme-a>`, `<theme-b>`):

1. Upgrade the plugin to 0.9.0 and run `/reload-plugins` in every open session. In the orchestrator pane of the kept clone, `fleet orch show` prints `default`; `fleet orch rename default <theme-a>` switches the project to format 2, moves the root cursor and board into `orchs/<theme-a>/` and creates its `ORCH.md`.
2. For each other theme, open a pane in the kept clone, start Claude there and run `fleet orch claim --name <theme-b>`. Its memory starts empty: copy the decisions that the old orchestrator holds into `orchs/<theme-b>/ORCH.md`, or hand the old session off into the new pane with `/fleet:handoff`.
3. Drain the lanes of each extra clone where they are: the orch that moved adopts them (`fleet adopt <target>` from its new pane; they are unowned, so no flag), watches them until they merge (its board marks them `in another clone`), and `cleanup` removes each one from the lane's own main checkout. New lanes start in the kept clone. An open lane can hold uncommitted work and a working agent, so moving it is the exception: a worktree cannot move to another clone, so the branch moves (`git -C <kept clone> fetch <extra clone> <branch>:<branch>`), `fleet worktree <branch> --base <branch> --name <label>` makes it anew, the old `.fleet/` files except `panes.json` are copied over, a new implementer starts with `spawn --pane ... --role impl`, and the old lane goes with `fleet retire` and `fleet cleanup <old target> --unmerged`.
4. Worktrees made by hand beside the clones: `fleet adopt <path>` in place; they drain as in step 3.
5. When `sweep` lists no lane of an extra clone and `git -C <extra clone> branch --no-merged <default branch>` names only branches that the kept clone has, delete the extra clone's folder. A copy's `.git/worktrees/` entries are copies, and the worktrees of the first clone never point at it, so deleting it touches none of them. Never run `git worktree remove` in a copy for a worktree of the first clone; `project.json` keeps the copy's key, which does no harm.

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
