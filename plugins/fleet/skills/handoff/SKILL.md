---
name: handoff
description: Hand this session, or a lane, to a fresh agent in a new Herdr pane — to free context, change model, or move to another remuda account. Writes a handoff file (or resumes the same session on another account), starts the successor with a one-line pointer, and confirms that it works. Use when the user says hand off, handoff, continue in a new pane, or switch account or model for this work.
argument-hint: "[self | PANE] [account] [model]"
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
---

# Hand off

- **Self** (the default): this session moves to a new pane.
- **Lane**: you orchestrate, and you move a lane's agent. The arguments name the pane or the lane.

First run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" conventions` for placement, model and account rules. In plain mode no account changes, but a handoff still frees context or changes the model.

## Choose the method

| Reason | Method |
|---|---|
| Context is large, noisy or compacted | Handoff file, then a fresh session. |
| Only the account changes, the context is healthy, and the agent is Claude | Resume: `spawn --resume <session id> --account <account>`. The new pane forks this conversation on the new account. If that account cannot see the transcript, `spawn` refuses: use a handoff file. |
| The model or effort changes | Either one. `--resume` with `--model` or `--effort` keeps the history. |

Find a session id with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" status --json` (field `session_id`; your own row has `self: true`).

## Self

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" prep --handoff`. It returns `handoff_path`, `lane`, `fleet_md` and `pane_role`.
2. If `fleet_md` is set and you orchestrate that fleet, bring FLEET.md up to date: lane table, decisions, next actions.
3. Unless you resume: write the handoff file at `handoff_path` from the HANDOFF template in `${CLAUDE_PLUGIN_ROOT}/skills/fleet/references/templates.md`. A stranger must be able to continue from it: goal, state, exact next steps, decisions with reasons, the user's rules from this conversation, commands, what not to redo, open questions. Never copy a secret; name where it lives.
4. Start the successor beside this pane:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" spawn --split auto --name <label> [--role <pane_role>] \
     [--account <account>] [--model <model>] [--effort <effort>] [--permission-mode <mode>] --accept-trust \
     --prompt "Read <handoff_path> and continue the work it describes. You replace pane <your pane id>."
   ```
   To resume instead: `spawn --split auto --name <label> --account <account> --accept-trust --resume <your session id> --prompt "You now run on <account> in a new pane. Continue where this conversation stopped."`
   If you orchestrate, pass `--role orchestrator`. Folder trust is kept per account, so another account can stop at the trust dialog; `--accept-trust` answers it, which is right here because this session already works in that folder.
5. Check the result: `ok: true` and status `working`. Read the new pane once to see that it reads the handoff or continues the work.
6. If you orchestrate, set the `Orchestrator:` line in FLEET.md to the new `pane_id`. The SessionStart hook then re-briefs the successor, not this pane.
7. Tell the user: the new pane, account, model and handoff file. Then stop. Do no more work here: two sessions in one checkout conflict. Do not close this pane; the user closes it (`/exit`).

## Lane

1. Choose a fresh token, for example `HANDOFF READY 1530` (the time now).
2. Ask the lane to write its handoff:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" send <pane> --text "Stop at the next safe point and commit finished work. Write .fleet/HANDOFF.md from the HANDOFF template in ${CLAUDE_PLUGIN_ROOT}/skills/fleet/references/templates.md, end it with the line <token>, then stop."
   ```
3. Wait in the background: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" watch <pane> --marker "<token>" --file .fleet/HANDOFF.md`.
4. Retire the old agent and keep the pane: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" retire <pane>`.
5. Start the successor in the same pane:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" spawn --pane <pane> --role impl --name <label> --agent <agent> \
     [--account <account>] [--model <model>] [--effort <effort>] [--permission-mode <mode>] --accept-trust \
     --prompt "Read .fleet/HANDOFF.md, then .fleet/BRIEF.md and .fleet/STATUS.md, and continue this lane."
   ```
   Account change only, with healthy context: skip steps 1 to 3. Note the lane's `session_id` from `status --json`, retire it, then `spawn --pane <pane> --role impl --account <account> --accept-trust --resume <session id> --prompt "Continue this lane from where you stopped."`.
6. Update the lane's row in FLEET.md: pane, account, model.
