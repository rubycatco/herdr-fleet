# herdr-fleet

A Claude Code plugin for running fleets of coding-agent lanes in [Herdr](https://herdr.dev) panes. It opens lanes (worktree, brief, implementer pane), runs critic review rounds, keeps context small with `/clear` and `/compact`, and hands work off to a new pane. [remuda](https://github.com/ya-luotao/remuda) is optional: with it, lanes can run on different Claude and Codex accounts; without it, every lane uses the agent's own login and nothing switches accounts.

## Requirements

- Herdr 0.9 or later, with Claude Code running inside a Herdr pane (`HERDR_ENV=1`).
- Claude Code; Codex is optional (used as a critic or implementer when installed).
- Python 3.8 or later (standard library only) and git.
- Optional: remuda 0.4 or later, for accounts.

## Install

From GitHub (you need read access to `rubycatco/herdr-fleet`):

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

## Conventions

`/fleet:onboard` writes `~/.config/fleet/conventions.md` (`$XDG_CONFIG_HOME/fleet/conventions.md`, or `$FLEET_CONVENTIONS`). It is plain language: agents and models for implementers and critics, permission mode, git and verification rules, the review stop rule, context thresholds, and, with remuda, which account to use when. Edit it any time; the skills read it each time they run. Until it exists, the skills use `plugins/fleet/defaults/conventions.md`.

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

A SessionStart hook runs after `/clear` and compaction. It re-briefs a pane from these files by its role: the implementer from BRIEF, STATUS and HANDOFF; a critic from its newest review request; the orchestrator from FLEET.md when its `Orchestrator: pane <id>` line names that pane. Other sessions in the same checkout get nothing.

## Helper CLI

`plugins/fleet/scripts/fleet` does the parts that must be exact: start agents and get past their first-run dialogs, submit prompts and confirm that they landed, wait for markers, confirm `/clear` and `/compact`, and refuse to drive the calling pane. `python3 plugins/fleet/scripts/fleet --help` lists the commands. `FLEET_NO_REMUDA=1` forces plain mode.

## Development

```bash
python3 -m unittest discover -s plugins/fleet/tests
claude plugin validate plugins/fleet
claude plugin validate .
```
