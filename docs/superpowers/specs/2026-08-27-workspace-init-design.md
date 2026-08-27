# Workspace initialization design

## Goal

Replace the worktree-based `ws create` workflow with `ws init`, which initializes the current directory by cloning remote repositories defined in its `ws.toml`.

## Command

`ws init` has no workspace-name argument. The current working directory is the workspace. The command reads `./ws.toml` and clones repositories beneath `./repos/`.

`ws create` and its named-workspace, local-source-repository workflow are removed.

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

Without `default_ref`, Git selects the remote default branch. With `default_ref`, a branch is checked out normally; a tag or commit ID is checked out detached.

## Initialization flow

1. Load and validate `./ws.toml` completely.
2. Refuse to run if `./repos/` already exists.
3. Create `./repos/` and clone each configured remote into its derived destination.
4. If cloning or ref checkout fails, remove the clone root and all artifacts created by this invocation, preserving `ws.toml` and any other workspace files.

## Errors and tests

Errors must identify invalid configuration, a missing configuration file, malformed repository entries, duplicate clone names, an existing clone root, and Git clone or ref-checkout failures.

Tests cover configuration parsing and validation; the CLI replacement; successful default-branch and configured branch/tag/commit initialization; clone layout; clone-root conflicts; and rollback after a failed clone. Verification runs focused tests plus the complete test suite, Ruff, and mypy.
