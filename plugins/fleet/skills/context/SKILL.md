---
name: context
description: Keep agent context small in a fleet — decide between continuing, /compact with a focus, /clear with a re-brief from .fleet/ files, or a handoff to a new pane, for this session or a lane, and then do it. Use when context passes about 60 %, after a long run, when a lane gets slow or forgets rules, or when the user asks about compact, clear or context.
argument-hint: "[PANE | self]"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" *)
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fleet *)
  - Bash(herdr pane read *)
  - Bash(herdr pane get *)
---

# Context

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" conventions` first. Its "Context and handoff" section sets the thresholds. The defaults below apply when it is silent.

## Read the level

- Yourself: the statusline (`N% (used/total)`), or `/context`.
- A lane: `herdr pane read <pane> --source recent-unwrapped --lines 300`. The statusline is under the composer. If it shows no percentage, judge by run length and by compactions on screen.

## Decide

| Situation | Action |
|---|---|
| Below the threshold (default 60 %) | Continue. |
| Above it, at a commit or another boundary, and the lane files are current | `/clear` and re-brief. Best for lanes: the files keep the state, and a summary loses nothing. |
| Above it, in the middle of a change that cannot stop | `/compact` with a focus. At the next boundary, check that STATUS.md is current. |
| Auto-compaction already ran, or the agent repeats itself or forgets rules | Treat it as above the threshold: `/clear` and re-brief at the next boundary. |
| The account or model must change, or you are the orchestrator and your context is high | Hand off: `/fleet:handoff`. |

For a Codex lane, every `/clear` or `/compact` in this table means a handoff: `fleet reset` supports Claude only (see the next section).

## Reset a lane

First check the lane's agent: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" status --json`, field `agent` of the lane's pane. `reset` supports Claude only, and refuses a Codex pane. For a Codex lane, skip the steps below and hand it off with `/fleet:handoff <pane>` (its Lane steps): the lane writes HANDOFF.md, `retire` exits it, and a fresh agent starts in the same pane, re-briefed from HANDOFF.md, BRIEF.md and STATUS.md.

For a Claude lane:

1. Make sure STATUS.md is current. Ask the lane first:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" send <pane> --text "Update .fleet/STATUS.md (log, verification, next step) and commit your work, then stop."
   ```
   Then wait with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" watch <pane> --idle-grace 30` (no marker: it returns 6 once the lane stops).
2. Reset it. PANE comes before `--compact`, because `--compact` takes an optional value:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" reset <pane>
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" reset <pane> --compact "<what to keep>"
   ```
   `reset` waits for a boundary, confirms the `/clear` (a new session id) or the `/compact` (a `Compacted` line), and sends a resume prompt that reads BRIEF.md, STATUS.md and HANDOFF.md. When the pane was started with `--role`, the SessionStart hook also gives it a re-brief for its role.
3. Watch the lane again (`/fleet:lane`, step 5).

A planner is short-lived: it ends at `PLAN READY`. Reset it only as you would an implementer, above the threshold and at a boundary, and give it its own resume prompt, since the default one would make it implement:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" reset <pane> --message "Re-read .fleet/BRIEF.md and .fleet/PLAN.md, change no file except PLAN.md, and continue the plan."
```
A Codex planner cannot be reset: retire it (`retire <pane>`) and start a new planner in the same pane with the spawn command of `/fleet:lane` step 4b, with a prompt to continue the plan in .fleet/PLAN.md.
A researcher is shorter still. When its context fills, ask it to write what it found into its research file, retire it with `--close`, and start a new researcher on the same file (`/fleet:research`, step 3).

## Yourself

The helper refuses to drive its own pane, and you cannot type into your own composer.

1. Write down your state first. Orchestrator: PROJECT.md (decisions, follow-ups, the user's rules from this conversation; with several orchs, your theme's decisions and notes go to your ORCH.md); add follow-ups with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" follow-up add "<text>"` and close them with `follow-up done <ID>`, not by hand. The board and the log keep the lanes. An orchestrator with an old FLEET.md updates it. Lane: STATUS.md (log, verification, next step), and commit.
2. Then either:
   - ask the user to type `/clear` (or `/compact <focus>`). The hook re-briefs this pane from your files. A registered orch (`orch show`) gets its name, PROJECT.md's rules and follow-ups, its ORCH.md's notes when the project has several orchs, its cached board and the count of new events in its view, so `/clear` loses almost nothing; it then runs `digest`. Compaction keeps the session id that the orch's last digest recorded, so the hook knows the orch by pane and session, without git, even in another project's checkout. An old FLEET.md orchestrator is re-briefed from FLEET.md when its `Orchestrator:` line names this pane, and a lane's implementer from the lane files; or
   - hand off with `/fleet:handoff`. It needs nothing from the user, and it is the right choice when the account or model also changes.
