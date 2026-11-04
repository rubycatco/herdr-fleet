---
name: cleanup
description: Clean up finished fleet lanes — list every lane with a verdict (ready, waiting or keep), ask which ones to remove, then exit their agents, archive their .fleet/ records, remove their worktrees, delete their merged local branches and run the project's own cleanup. Use when the user asks to clean up lanes, worktrees, panes or workspaces, or after pull requests merge.
argument-hint: "[workspace ids or lane paths]"
disable-model-invocation: true
allowed-tools:
  - Read
  - Edit
  - AskUserQuestion
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" *)
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fleet *)
  - Bash(herdr pane read *)
---

# Clean up lanes

A lane is a linked git worktree that is open as a Herdr workspace, that sits in Herdr's worktrees directory, or that has a `.fleet/` folder. Removing a lane deletes its folder. Remove nothing until the user picks the lanes in step 2.

## 1. Look

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" conventions`. Note each cleanup command under `## Projects`.
2. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" sweep`. It only reads. It puts every lane on this machine in a group, with the reason on its row:
   - **READY**: its pull request merged, or, without one, its branch is in the default branch; or it is a detached checkout whose commit a branch holds. The worktree is clean: no changed or untracked file (whatever the settings for untracked files, the file system monitor or the untracked cache say; the `.fleet/` folder does not count) and no existing path outside `.fleet/` marked assume-unchanged or skip-worktree (absent paths of a sparse checkout do not count). Ignored files do not count and go with the worktree, and with `core.fileMode` false a mode-only change is not seen. No agent works in it, and no commit is missing from the merge. `cleanup` removes these.
   - **WAITING**: its pull request is open. The worktree stays for review fixes. `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" retire PANE` exits an idle agent there if the user wants the memory back.
   - **KEEP**: uncommitted changes (untracked files count), existing paths outside `.fleet/` marked assume-unchanged or skip-worktree, an agent at work, an agent that the helper cannot exit, a program running in a pane, a locked worktree, a checkout of the default branch, a detached HEAD whose commits no branch holds, commits that no merge or remote holds, no pull request, a closed pull request, or this session.
   - **OTHER WORKTREES**: worktrees that other tools or the user made (the Codex app, Claude Code agents, sibling checkouts). Never remove them.
3. When the arguments name lanes, consider only those.
4. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" board`, and read the old `.fleet/FLEET.md` if you still keep one. A lane that is still in progress there (working, review, ready, blocked) stays, whatever the sweep says: a READY lane without a pull request (`in main`) can be a lane that has not committed yet.

## 2. Ask

Show the groups in a short table. Then ask with AskUserQuestion. Name the lanes in each question: the user may see only the dialog.

1. Which lanes: "All N ready lanes: <targets> (Recommended)", a subset that makes sense (for example "Only the merged ones"), "None now". The user can name targets under Other.
2. Only when the user asks about KEEP lanes whose sole reason is a missing merge (no pull request, a closed one, or commits not pushed): offer them with `--unmerged`. Their worktrees go and their branches stay.
3. When `## Projects` names a cleanup command for a repository among the chosen lanes: "Run <command> in <folder> afterwards (Recommended)", "Skip it".

## 3. Remove

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" cleanup <target> [<target> ...]`. Targets are the workspace ids and paths that the sweep printed. Add `--unmerged` only for lanes that the user chose under that option.
   - It checks every target again before it changes anything. When a target is no longer ready, it changes nothing and prints `refused` with the reasons. Show them, and sweep again. It checks each lane once more after its agents exit, right before the removal: a change or a flagged path that appears then keeps that worktree, with the count in the lane's `error`. It never forces a removal. That check is the only guard: with `status.showUntrackedFiles=no`, neither `git worktree remove` nor `herdr worktree remove` refuses a worktree with untracked files, and both delete them.
   - For each lane in turn, it exits the agents, copies `.fleet/` (without `panes.json`) to the archive path that it prints (the `lanes/` folder of the project home, `~/.fleet/projects/<project>/`), removes the worktree (Herdr closes the workspace and its panes), and deletes the local branch only when the merge holds every commit. It never pushes, and never deletes a remote branch.
   - After it removed a lane, it handles the follow-ups in PROJECT.md that the lane's branch holds. It closes a follow-up as done when the lane's pull request merged, or when the lane is merged and its branch moved since the lane was created (its oldest reflog entry, the creation, is not HEAD; a lane that was only rebased onto main also counts as moved). Otherwise (a lane whose branch never moved, a lane removed with `--unmerged`) it releases it. When git cannot tell, for example because reflog expiry removed the creation entry, it releases: an error can turn a close into a release, never the reverse, so that another lane can take it (`follow_ups_closed`, `follow_ups_released`). Without a project or a PROJECT.md there is nothing to do. When it cannot change PROJECT.md (a busy lock, a file that is not UTF-8, an ID on two lines, a counter or log that cannot be read), the lane is still removed, nothing changes in PROJECT.md, and `follow_up_error` says why: fix the cause, then close them with `follow-up done <ID>` or free them with `follow-up release <ID>`. `--dry-run` lists the follow-ups under `follow_ups`.
   - An agent that does not exit stops its lane, and that worktree stays. Read the pane with `herdr pane read <pane> --source recent-unwrapped --lines 80`, and tell the user.
2. Run each project cleanup command that the user agreed to, in the folder that the conventions name.

## 4. Record and report

- Nothing to record by hand: each cleanup is an event in the log with its archive path, and the next `board` and `digest` show the lane as `removed`. Add a follow-up that the cleanup leaves (a kept branch, a failed project cleanup command) with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet" follow-up add "<text>" --source "cleanup of <branch>"`. An old FLEET.md, if you keep one, marks each removed lane as cleaned.
- Report one line per lane: removed or not, the branch deleted or kept (with the reason), the archive, and the follow-ups closed or released (or the `follow_up_error`). Then the result of each project cleanup command.
