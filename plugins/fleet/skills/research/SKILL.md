---
name: research
description: Answer an open question for the fleet in a researcher pane — how a subsystem works, what an API or library does, which option fits — and keep the answer in the project home, where later lanes reuse it. Use when the orchestrator cannot name the files or facts that a brief or plan needs, when several lanes need the same answer, or when the user asks to research a question for the fleet.
argument-hint: "<question> [lane]"
allowed-tools:
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" *)
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fleet *)
  - Bash(herdr pane read *)
  - Bash(herdr pane get *)
---

# Research a question

You stay small: a researcher pane digs, and you read only its answer. The question comes from the arguments. If there is none, ask the user what to find out.

## 1. Look for an earlier answer

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" conventions`. Its `## Planning and research` section names the researcher's agent and model; by default, the implementer settings.
2. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" orch show --json`. `home` is the project home. When it exits 1 with "has no fleet project home yet", the repository has no project home yet. When it says "in no git repository" or "not in a working tree" (a bare repository, or inside `.git`), change to a checkout of the project first; when git "could not tell the project", run it again. Create the home with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" orch claim`, as in `/fleet:fleet` ("Start here", step 3): it creates the project home and its PROJECT.md, and registers your pane as the project's orch `default`. A new project has no orchestrator yet, so the claim does not refuse. A research question never needs `orch claim --name <theme>`: that opens another theme, which only the user decides. Then run `orch show --json` again.
3. Read the files in `<home>/research/`. When one already answers the question, use it and stop. `fleet board` lists the ones that are still open.

## 2. Write the research file

Write `<home>/research/<slug>.md` from the RESEARCH template in `${CLAUDE_PLUGIN_ROOT}/skills/fleet/references/templates.md`. The slug is a few words of the question, such as `auth-session-refresh`. Fill in the title, `Asked:`, Question and Context: what you already know, and where to look (paths, docs, URLs). One question per file. Do not write the done line: the watcher would end at once.

## 3. Start the researcher

Pick the checkout: the main checkout, or the lane's worktree when the question is about that lane. Start the researcher in a split beside you:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" spawn --split auto --cwd <checkout> --role researcher \
  --task <home>/research/<slug>.md --name research-<slug> \
  --agent <agent> [--model <model>] [--effort <effort>] [--account <account>] \
  --prompt "Read <home>/research/<slug>.md and follow it."
```

`--task` keeps the file with the pane's role, so after /clear or compaction the SessionStart hook tells the researcher to re-read it. The researcher writes only its research file.

## 4. Watch and read the answer

1. Start a watcher with the Bash tool and `run_in_background: true`:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" watch <pane> --marker "RESEARCH DONE" --file <home>/research/<slug>.md
   ```
2. When it ends:
   - 0: read `## Answer` and `## Open questions`. Then `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" retire <pane> --close`.
   - 6 (stopped): read the file and the pane. Answer with `send`, then watch again.
   - 4 (blocked): read the pane. Cancel an unsafe prompt with `herdr pane send-keys <pane> Escape` and re-instruct. Then watch again.
   - 5 (exited): read the file. Start a new researcher on the same file when the answer is not complete.
3. When the researcher changed other files, tell the user, and do not keep those changes.

## 5. Use the answer

- Link the research file from the brief or the plan. Do not copy it.
- A decision that follows from the answer goes to PROJECT.md, with the file as its reason.
- Tell the user in two or three lines: the question, the answer, and the file.
