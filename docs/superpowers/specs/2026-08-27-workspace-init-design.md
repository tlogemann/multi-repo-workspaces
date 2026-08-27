# Workspace initialization design

## Goal

Add `ws init`, which initializes the current directory as a project root by cloning its remote source repositories from `ws.toml`. The existing worktree-based feature-workspace lifecycle remains intact.

## Command

`ws init` has no workspace-name argument. The current working directory is the project root. The command reads `./ws.toml` and clones source repositories beneath `./repos/`.

`ws create <feature>` remains the feature-workspace command. It creates detached worktrees beneath `./workspaces/<feature>/repos/`, using the source clones at `./repos/`.

## Configuration

`ws.toml` contains one or more repository definitions:

```toml
[[repos]]
url = "git@github.com:acme/api.git"
default_ref = "main"

[[repos]]
url = "https://github.com/acme/web.git"
```

Each entry requires a remote Git URL. `default_ref` is optional. Repository clone names are derived from the final URL path component with a trailing `.git` removed. Duplicate derived names are invalid.

Source clones use the remote's normal default checkout. `default_ref` retains its current meaning: it selects the detached base revision for that repository when `ws create <feature>` creates a worktree. `--source repo=ref` remains a per-creation override.

## Initialization flow

1. Load and validate `./ws.toml` completely, without requiring local source clones to exist.
2. Refuse to run if `./repos/` already exists.
3. Create `./repos/` and clone each configured remote into its derived destination.
4. If cloning or ref checkout fails, remove the clone root and all artifacts created by this invocation, preserving `ws.toml` and any other workspace files.

After initialization, `ws create <feature>` resolves each source repository at `./repos/<derived-name>`. It keeps the current worktree lock/state, detached-worktree creation, default-ref resolution, claim, context, status, and removal behavior. Source clones are Git sources only; they are not a second managed workspace topology.

## Errors and tests

Errors must identify invalid configuration, a missing configuration file, malformed repository entries, duplicate clone names, an existing clone root, Git clone failures, and attempts to create a feature workspace before a configured source clone exists.

Tests cover configuration parsing and validation; successful source-clone initialization; clone layout; clone-root conflicts; rollback after a failed clone; and the existing detached-worktree, default-ref, source-override, claim, context, status, and remove flows using initialized source clones. Verification runs focused tests plus the complete test suite, Ruff, and mypy.
