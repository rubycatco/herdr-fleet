# herdr-fleet

A Claude Code plugin for running fleets of coding-agent lanes in [Herdr](https://herdr.dev) panes. It opens lanes (worktree, brief, implementer pane), runs critic review rounds, keeps context small with `/clear` and `/compact`, and hands work off to a new pane. [remuda](https://github.com/ya-luotao/remuda) is optional: with it, lanes can run on different Claude and Codex accounts; without it, every lane uses the agent's own login and nothing switches accounts.

## How it works

You talk to one Claude Code session, the orchestrator. It opens each lane in its own git worktree and Herdr workspace, starts the agents there, and drives them. The agents report through files in `.fleet/`. In the diagrams, `/fleet:<skill>` is a skill, and `fleet <command>` is the helper CLI that the skills call (`plugins/fleet/scripts/fleet`).

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
│  │ .fleet/FLEET.md          │<──────│                   │                  │ │
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

along the way
  a lane asks or blocks   fleet watch returns; orchestrator answers or asks you
  a lane's context fills  at ~60 %: fleet reset at a commit, /clear, re-brief
  your session fills up   at ~70 %: /fleet:handoff to a successor in a new pane
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
| `FLEET.md` | orchestrator | Lane table, decisions, next actions (in the orchestrator's checkout). |

`fleet cleanup` copies a lane's `.fleet/` (without `panes.json`) to `~/.local/state/fleet/archive/<repo>/` before it removes the worktree.

A SessionStart hook runs after `/clear` and compaction. It re-briefs a pane from these files by its role: the implementer from BRIEF, STATUS and HANDOFF; a critic from its newest review request; the orchestrator from FLEET.md when its `Orchestrator: pane <id>` line names that pane. Other sessions in the same checkout get nothing.

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
