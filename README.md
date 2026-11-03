# herdr-fleet

A Claude Code plugin for running fleets of coding-agent lanes in [Herdr](https://herdr.dev) panes. It opens lanes (worktree, brief, implementer pane), runs critic review rounds, keeps context small with `/clear` and `/compact`, and hands work off to a new pane. [remuda](https://github.com/ya-luotao/remuda) is optional: with it, lanes can run on different Claude and Codex accounts; without it, every lane uses the agent's own login and nothing switches accounts.

## How it works

You talk to one Claude Code session, the orchestrator. Each project has one, registered by its Herdr pane. It opens each lane in its own git worktree and Herdr workspace, starts the agents there, and drives them. The agents report through files in `.fleet/`; the helper turns those files, git, the pull requests and its event log into a lane board. In the diagrams, `/fleet:<skill>` is a skill, and `fleet <command>` is the helper CLI that the skills call (`plugins/fleet/scripts/fleet`).

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
  fleet board          the lane table, any time

/fleet:lane "fix the login redirect"    <── you type this
  ├─ fleet worktree    branch, git worktree, its own Herdr workspace
  ├─ BRIEF.md          goal, scope, acceptance, checks
  ├─ fleet spawn       implementer: plan ─> code and commit ─> checks
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
push, open the PR, merge per your rules
  └─ /fleet:cleanup    fleet sweep, you pick, fleet cleanup
                       decisions and follow-ups go to PROJECT.md all along

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
| `/fleet:lane <task>` | Opens a lane: worktree and branch, `.fleet/BRIEF.md`, an implementer pane, a background watcher. |
| `/fleet:review` | One critic round: review request, critic panes, verdict, rulings, findings back to the implementer, stop rule. |
| `/fleet:context` | Chooses between continuing, `/compact`, `/clear` with a re-brief, or a handoff, and does it. |
| `/fleet:handoff` | Moves this session or a lane to a new pane: through a handoff file, or by resuming the same conversation on another account. |
| `/fleet:accounts` | remuda accounts, usage and the account of each pane. |
| `/fleet:cleanup` | Lists every lane with a verdict (ready, waiting, keep), asks which ones to remove, then exits their agents, archives `.fleet/`, removes the worktrees, deletes merged local branches and runs the project's own cleanup. Only you start it. |

## Conventions

`/fleet:onboard` writes `~/.config/fleet/conventions.md` (`$XDG_CONFIG_HOME/fleet/conventions.md`, or `$FLEET_CONVENTIONS`). It is plain language: agents, models and effort for implementers and critics, permission mode, git and verification rules, the review stop rule, context thresholds, and, with remuda, which account to use when. Edit it any time; the skills read it each time they run. Until it exists, the skills use `plugins/fleet/defaults/conventions.md`.

## Lane files

Each lane keeps its state in `.fleet/` at the top of its worktree. The helper adds `/.fleet/` to `.git/info/exclude`, so nothing is committed.

| File | Written by | Purpose |
|---|---|---|
| `BRIEF.md` | orchestrator | Goal, scope, sources, acceptance, verification. |
| `STATUS.md` | lane | Plan, log, real verification results, `READY FOR REVIEW: round <n>`. |
| `REVIEW-REQUEST-<n>.md`, `REVIEW-<n>.md` | orchestrator, critic | One review round. |
| `HANDOFF.md` | outgoing agent | Everything a successor needs. |
| `panes.json` | `fleet spawn --role` | The role of each pane (impl, critic, orchestrator). |
| `FLEET.md` | orchestrator (old form) | Lane table, decisions, next actions, in the orchestrator's checkout. PROJECT.md and `fleet board` replace it; an existing one keeps working. |

`fleet cleanup` copies a lane's `.fleet/` (without `panes.json`) to the `lanes/` folder of its project home before it removes the worktree.

After `/clear` and compaction, the SessionStart hook re-briefs a pane from these files by its role: the implementer from BRIEF, STATUS and HANDOFF; a critic from its newest review request; an old-style orchestrator from FLEET.md when its `Orchestrator: pane <id>` line names that pane. Other sessions in the same checkout get nothing. The registered orchestrator of a project is re-briefed from the project home instead (see below).

## Project home

Each repository is a project. The project is known by its normalized remote URL, so all clones of a repository share one home: `~/.fleet/projects/<name>/` (`$FLEET_HOME` moves the root). A repository without a remote is known by its main checkout, and keeps its project when it gets a remote.

| Path | Holds |
|---|---|
| `project.json` | The project's name and keys: the remote URL and the main checkout of each clone. |
| `events/YYYY-MM.jsonl` | The event log, one JSON object per line. |
| `lanes/<branch>-<time>/` | The `.fleet/` files of each lane that `fleet cleanup` removed. |
| `PROJECT.md` | The orchestrator's memory: `## Rules`, `## Decisions`, `## Follow-ups`, `## Notes`. The orchestrator writes it; you can edit it. `fleet orch claim` creates it. |
| `orch.json` | The project's orchestrator: its pane, workspace, checkout, session and claim time. Written by `fleet orch claim` and `release`. |
| `board.json` | The newest lane board and when it was made, the cache for the re-brief. Written by `fleet board` and `fleet digest`. |
| `digest.json` | The digest's cursor, how far it read each monthly log (a byte offset), and each lane's phase at that time. Only `fleet digest` moves it. |
| `.lock` | Serializes `fleet orch claim`, `release`, a digest that consumes, and board.json writes. `--peek` and the SessionStart hook never take it. |

The helper records each command that changes something: `orch_claim` and `orch_release`, `phase` (a lane's phase change, recorded by `fleet digest`), `worktree`, `spawn` (agent, account, model, effort, role, prompt; recorded at launch) and `spawn_result` (whether the agent started, its session), `send` (the prompt), `reset`, `retire`, `cleanup` (the result, and the last session of each agent) and `prep --handoff`. Each event names its lane (branch, workspace, directory) and who ran the command: the Herdr pane and, inside Claude Code, the session. The SessionStart and SessionEnd hooks record each Claude session in a Herdr pane of a checkout that has `.fleet/`: the session id, the transcript path, the role, and why the session ended. Outside Herdr the hooks do not start Python, except to re-brief after /clear or compaction. Events outside a repository go to `~/.fleet/events/`. The log stays on this machine. It can hold prompts, so the helper keeps the home private (mode 0700).

`fleet log` prints the log of the project that holds the current directory (`--all`: every project). Filter with `--lane` (a branch, workspace, label or path), `--pane`, `--event` and `--since 7d`. `--sessions` prints one row per agent session, with its transcript. `--json` prints the events.

## One orchestrator per project

Each project has at most one orchestrator, known by its Herdr pane. `fleet orch claim` registers the calling pane in `orch.json` and creates `PROJECT.md`. It refuses while another registered pane is alive; the registered pane hands over with `fleet orch claim --pane <new pane> --replace`, and a dead pane never blocks. When herdr cannot tell whether the registered pane is alive, the claim refuses too, and `--force` takes over. It warns when a checkout of the project has a FLEET.md whose `Orchestrator:` line names another live pane. `fleet orch show` prints the record; `fleet orch release` removes it.

The orchestrator does not keep a lane table. `fleet board` derives one row per lane of the project from Herdr, git, the pull requests and the lane files: its phase (`removed`, `merged`, `pr open`, `blocked`, `review <n>`, `reviewed <n>: <verdict>`, `ready <n>`, `working`, `idle`, `no agent`), its panes, its newest event, and the blocked or next-step line of its STATUS.md. `fleet digest` prints what changed since the last digest: each phase change, and the lanes' events, without the orchestrator's own. It records each phase change in the log and moves its cursor. It reads the log by byte offset, so an event is shown once, also when it arrives late. `--peek` only reads, and so does a digest from another pane while the registered one is alive or herdr cannot tell. `board` never consumes a change.

At every session start of the orchestrator's pane (startup, resume, `/clear`, compaction), the SessionStart hook re-briefs it from the project home: the path of PROJECT.md with its rules and follow-ups, the cached board with its age, and the number of new events. It reads files only, and at most about 1 MB of unread log, so it stays fast. The orchestrator then runs `fleet digest`. An orchestrator that still keeps FLEET.md works as before.

## Helper CLI

`plugins/fleet/scripts/fleet` does the parts that must be exact: start agents and get past their first-run dialogs, submit prompts and confirm that they landed, wait for markers, confirm `/clear` and `/compact`, refuse to drive the calling pane, and remove a lane only when it is clean and its merge holds every commit. `python3 plugins/fleet/scripts/fleet --help` lists the commands. `FLEET_NO_REMUDA=1` forces plain mode.

## Development

```bash
python3 -m unittest discover -s plugins/fleet/tests
claude plugin validate plugins/fleet
claude plugin validate .
```

## License

MIT. See [LICENSE](LICENSE).
