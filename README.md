# ws-tool

Isolated multi-repository Git workspaces.

`ws-tool` manages a collection of Git repositories as a single logical workspace. Each repository in a workspace can be worked on independently or claimed into an isolated branch state.

## Mental Model

A **workspace** is a named collection of repositories defined by a `ws.toml` file. Repositories start detached (no checked-out branch), and can be **claimed** into an isolated branch where changes won't interfere with other work.

**Claimed branches** are named `ws/<workspace-name>/<repo-name>` and are idempotent—claiming the same repository twice returns the same locked commit.

**Temporary contexts** let you switch a claimed repository to a different branch (e.g., `default`) without losing your current work. Changes are automatically stashed. Use `--restore` to return to your saved state.

**Crash recovery** uses deterministic `.removing/` tombstones. After a crash during removal, manually verify no operation is running, remove stale locks, and the tool resumes from the tombstone.

## Quick Start

```bash
# Install
pip install ws-tool

# Create a workspace (all repos start detached)
ws create my-workspace

# Claim a repository into an isolated branch
ws claim my-workspace/app

# Work normally—changes are isolated in ws/my-workspace/app
```

## ws.toml

Place `ws.toml` in your workspace root:

```toml
[workspace]
name = "my-workspace"
root = "."  # workspace root (default: ws.toml directory)

[repos]
app = "git@github.com:org/app.git"
lib = "git@github.com:org/lib.git"
```

**Config is not searched upward.** Pass `--config PATH` explicitly if your `ws.toml` is not in the current directory.

## Commands

### Create

```bash
ws create feature-a
```

Creates a workspace with all repositories in detached HEAD state at their locked commits.

### Different initial source

Override a repository's initial source:

```bash
ws create feature-a \
    --source library=feature/new-api
```

### Claim locked workspace version

```bash
ws claim app
```

Claims the repository using the workspace's immutable locked base commit. The branch `ws/<workspace-name>/<repo-name>` is created or updated to match the locked commit.

### Claim current default branch

```bash
ws claim app --source default
```

Claims the repository using its current default branch (`main` or `master`) instead of the locked base.

### Claim arbitrary source into arbitrary target

```bash
ws claim app \
    --source origin/develop \
    --target feature/new-api
```

### Temporary context

Given:

```
app -> feature/new-api
dirty changes
```

Switch to default branch while saving your changes:

```bash
ws context app default
```

Your changes are automatically stashed and a temporary context is recorded:

```
feature changes
    ↓ automatically saved
current default commit
    ↓ detached temporary context
```

Restore your saved state:

```bash
ws context app --restore
```

This restores:

```
feature/new-api
+
original working changes
```

### Context phases

- `idle` — no temporary context active
- `intent` — context switch requested, not yet applied
- `outcome` — context switch applied, ready to restore
- `restore_conflicted` — unmerged entries after restore conflict; user must resolve
- `restore_failed` — retryable stash-apply failure

## Naming rules

**Workspace names** must match `[a-zA-Z][a-zA-Z0-9_-]*`.

**Repository names** must match `[a-zA-Z][a-zA-Z0-9_-]*`.

These are enforced strictly. No spaces, no special characters.

## Default branch locking

Each repository's default selector is locked independently at creation time. If you later delete `ws.toml` and recreate the workspace, the locked default is preserved. This means `default` still resolves correctly after config changes.

## Operation locks

Mutators are serialized by `.ws/operation.lock`. If an operation is interrupted (SIGKILL, crash), stale locks may remain.

**Recovery steps after crash during removal:**

1. Verify no `ws` process is running
2. Remove `.ws/<workspace>/.lifecycle.lock` and `.ws/<workspace>/.internal/<repo>/operation.lock`
3. Run `ws remove <workspace>` to resume

## Context and dirty state

A dirty temporary context (uncommitted changes when entering context) must be resolved by the user before `--restore` is allowed. The tool cannot auto-restore across dirty state.

**Restore behavior:**

- Verifies the return branch still points to its saved HEAD
- Verifies the branch is available before checking it out
- `restore_failed` retries only after the exact saved clean return baseline is restored: correct branch/HEAD, no unmerged entries, clean tracked/index/untracked status

## Stash handling

Tool-created stashes use private OID-pinned refs (`refs/ws-tool/stash/<repo>`). The shared stash entry (`refs/stash`) is retained with its exact OID/message for safe manual cleanup.

## Conflict resolution

If restore detects unmerged entries, it enters `restore_conflicted` state. Use:

```bash
ws context app --finalize-restore
```

**Finalize rules:**

- Allowed only when no unmerged index entries remain
- Preserves accepted staged/unstaged worktree and index state
- Deletes only the private OID-pinned snapshot
- Retains the shared stash entry, reports its exact OID/message

Aborted or discarded resolution keeps the stash and state when finalize is not run. The tool cannot mechanically distinguish those choices.

**`restore_failed` vs `restore_conflicted`:**

- `restore_conflicted` — unmerged entries, user-certified finalize
- `restore_failed` — retryable stash-apply failure without unmerged entries

Only `restore_conflicted` permits `--finalize-restore`. `restore_failed` permits only `--restore` retry.

## Removal and crash recovery

Removal uses only the deterministic sibling tombstone `<workspace-root>/.<workspace-name>.removing/`. After a crash:

1. Worktrees were removed from the normal path
2. User manually verifies no operation runs
3. User removes both exact stale lifecycle and internal operation locks
4. Tool resumes from the tombstone

The tool never scans for remnants.

## v1 Limitations

- **Tool-created shared stash entries remain for manual cleanup.** Their exact OID and message are reported for safe reference.
- **Finalize is operator certification.** The tool cannot detect whether you discarded or resolved changes.
- **Removal recovery uses only the deterministic `.removing/` tombstone.** No other recovery mechanism exists.
