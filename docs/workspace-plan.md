You are working in a dedicated development environment and should implement a new generic Python CLI tool for managing isolated multi-repository development workspaces using Git worktrees.

The tool is intended especially for parallel development with coding agents, but it must be generic and must not contain any project-specific assumptions.

The primary target use case is a software project composed of multiple independent Git repositories, often Python repositories using `uv`, where agents should be able to see all repositories but normally modify only explicitly selected ones.

# 1. Core mental model

The project root contains the configured source clones and named feature
workspaces:

```text
project/
├── ws.toml
├── repos/
│   ├── app/
│   ├── library/
│   └── tools/
└── workspaces/
    └── feature-x/
        ├── repos/
        │   ├── app/
        │   ├── library/
        │   └── tools/
        ├── workspace.lock.toml
        └── .ws/
            └── state.toml
```

The fundamental rules are:

* A project configuration defines multiple remote Git repositories with
  `[[repos]]` entries.
* `ws init` clones each configured repository into the project's `repos/`
  source-clone root.
* Creating a feature workspace creates one Git worktree per initialized source
  clone under `workspaces/<feature>/repos/`.
* Every repository starts at an immutable resolved commit with **detached HEAD**.
* Detached repositories are fully available as source/context for humans and coding agents.
* A repository only gets a real editable branch when explicitly `claim`ed.
* Repositories within the same logical workspace may use completely different source refs and target branches.
* Multiple logical workspaces must coexist safely.
* The Git core must work for arbitrary repositories and must not require Python or `uv`.
* Python/`uv` integration will be added in a later phase.

The primary commands for this first version are:

```bash
ws init
ws create <workspace> [--config PATH]
ws status
ws claim <repo>
ws context <repo> <ref>
ws context <repo> --restore
ws context <repo> --finalize-restore
ws remove <workspace> [--config PATH]
```

Do not implement Docker, T3Code, OpenCode, GitLab/GitHub, PR/MR creation, pushing, rebasing, merging, or generalized hooks/plugins yet.

---

# 2. Technology

Implement the tool in Python.

Use:

* Python >= 3.12
* `uv` for package/development management
* `pytest`
* preferably `ruff`
* stdlib wherever practical:

  * `argparse`, or another lightweight CLI library if clearly justified
  * `subprocess`
  * `pathlib`
  * `dataclasses`
  * `tomllib`
  * `json`

A small TOML writer dependency is acceptable.

Do NOT use GitPython.

All Git behavior must be implemented using the real `git` CLI through subprocesses.

The project must support:

```bash
uv tool install -e .
```

and expose:

```bash
ws
```

as a CLI command.

Prefer argument arrays and never use:

```python
shell=True
```

for Git invocation.

---

# 3. Suggested repository structure

Use a clean src layout approximately like:

```text
.
├── pyproject.toml
├── README.md
├── src/
│   └── ws_tool/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── git.py
│       ├── workspace.py
│       ├── context.py
│       ├── models.py
│       └── errors.py
└── tests/
    ├── unit/
    └── integration/
```

Adjust module boundaries if a simpler design emerges.

Keep the codebase small.

Avoid introducing abstractions merely because they might theoretically be useful later.

---

# 4. Project configuration

Use a project-level TOML configuration named `ws.toml`.

For example:

```toml
[[repos]]
url = "git@github.com:acme/api.git"
default_ref = "main"
```

Each `[[repos]]` entry requires a remote `url`. The repository name is derived
from the final URL component with a trailing `.git` removed; that name is used
for both the source clone and the feature-workspace worktree. `default_ref` is
optional for each repository. When omitted, the tool uses unambiguous Git
default-ref discovery.

Before creating a feature workspace, run `ws init` from the project root. It
clones all configured URLs into `repos/`. The command refuses a pre-existing
`repos/` directory, and if a later clone fails it removes the clone root and
all clones created by that invocation while preserving `ws.toml` and unrelated
files.

Requirements:

* repository identifiers such as `app` are logical names derived from the URL
  and used by the CLI;
* workspace names and repository identifiers must match `[A-Za-z0-9][A-Za-z0-9._-]*`; reject empty names, `.`/`..` segments, separators, control characters, and leading dashes;
* `url` is a non-empty remote Git URL;
* `default_ref` is optional for each repository and, when supplied, is
  authoritative for its default selector; an unresolvable value records a null
  selector and requires an explicit source override for creation;
* source clones are always created at `<project-root>/repos/<repository>`;
* feature workspaces are always created at
  `<project-root>/workspaces/<workspace>`;
* `url` and `default_ref` are the recognized repository-definition keys; other
  keys are ignored by the configuration loader and are not aliases or
  migration forms.

`ws init` reads `./ws.toml` from the project root. `ws create <workspace>` and
`ws remove <workspace>` use `./ws.toml` by default and accept `--config PATH`.
They must not search upward for a config file. Workspace-local commands instead
discover `.ws` upward from the current directory; the lock and state then
identify the workspace without consulting the config. `remove` may therefore
run inside the named workspace using discovery, or otherwise uses the
explicitly selected/default config. No command globally scans the filesystem.

Default-ref detection must be robust. Resolve and record each repository's
effective default ref independently from the creation base/source ref. The
effective-default choice for each repository is:

1. the per-repository `default_ref`, if present;
2. automatic default-ref discovery when `default_ref` is omitted.

An explicit per-repository value is authoritative for the default selector. If
it cannot be resolved, retain a null default selector and do not fall back to
automatic discovery. A configured value is resolved with `git rev-parse
--verify <value>^{commit}` and may be a branch, tag, remote-tracking ref, or
full/abbreviated commit ID. Creation fails when that repository has no other
resolvable base source; an explicit `--source repo=ref` supplies that base and
allows creation to succeed with the null default selector.

Prefer Git-native information such as:

```bash
git symbolic-ref refs/remotes/origin/HEAD
```

where available.

When `default_ref` is omitted, use this exact automatic fallback order:

1. a unique remote symbolic HEAD (for example `origin/HEAD`); fail if remotes
   disagree;
2. the currently checked-out branch of a non-bare source repository;
3. a clear error when no unambiguous branch exists.

Bare source repositories are supported, but cannot use a checked-out branch
for this fallback. Never guess a branch name such as `main` or `master`.

Do not blindly assume `main` or `master`.

If the auto-discovered default ref cannot be determined unambiguously, record a null
selector and fail with a clear error only when an operation actually requires
`default`. Creation may continue when its base/source ref is explicit.

An explicit source ref may determine the creation base, but does not replace
the separately recorded default selector needed by workspace-local
`claim/context <repo> default`.

### 4b. Source availability

`ws init` must be run from the project root before `ws create`. It validates
the `[[repos]]` entries, clones each configured URL into an owned temporary
sibling of `repos/`, and atomically renames that completed root to `repos/`
only after all clones succeed. A pre-existing `repos/` is rejected before any
clone starts. If a clone fails, initialization removes only its temporary clone
root, preserving `ws.toml` and unrelated project files. Consequently,
`ws create` observes either no source root or the complete published source
root, never partial clones.

`ws create` requires every configured source clone at
`repos/<repository>` to be an initialized Git repository. Missing or invalid
source clones are an error; source cloning is performed by `ws init`.

If a source repository becomes unavailable after workspace creation (deletion,
move, network loss), workspace-local commands (`ws status`, `ws claim`,
`ws context`, `ws remove`) treat it as an error. `ws status` reports the
repository as unavailable. Mutation commands fail with a clear diagnostic
referencing the repository name and the nature of the unavailability.
Workspace removal preflights source availability before removing any worktree.

### 4c. Default ref resolution details

The selected configured per-repository `default_ref`, when present, is resolved
as a Git ref in the source repository at workspace creation time. It is stored as-is in the lock file's
`default_selector`. It is not required to be a branch name; any resolvable Git
ref (branch, tag, remote-tracking ref, full/abbreviated commit ID) is
acceptable, provided `git rev-parse --verify <value>^{commit}` succeeds in the
source repository. If this check fails, the value remains authoritative for
the default selector and must not trigger discovery. Without an explicit
`--source repo=ref`, creation fails because the repository has no resolvable
base source; with that override, creation succeeds and the lock records a
null `default_selector`.

For multi-remote repositories without a configured `default_ref`,
`_remote_symbolic_selector` considers **all** `refs/remotes/*/HEAD` entries. If
exactly one distinct commit is reachable from all remote symbolic HEAD targets,
the alphabetically smallest target branch name is selected (for determinism).
If different commits are reachable, the tool errors with "remote symbolic
HEADs disagree" rather than picking one. A configured `default_ref` skips
automatic discovery. This means a repository with only an `origin` remote
behaves predictably, while a repository with multiple remotes that point to
different default refs fails predictably only when `default_ref` is omitted.

---

# 5. Workspace creation

Bootstrap the project once before creating feature workspaces:

```bash
ws init
ws create feature-name
```

`ws init` owns the source-clone setup in `<project-root>/repos/`. `ws create`
uses those source clones and owns only the detached worktrees and metadata in
`<project-root>/workspaces/<feature>/`.

Implement:

```bash
ws create <workspace-name> [--config PATH]
```

Creation also accepts `--config PATH`; the canonical default is `./ws.toml`.
Validate the workspace name and every configured repository identifier before
any worktree mutation; the external lifecycle-lock bookkeeping directory is
the documented synchronization exception. Validate that every source clone
created by `ws init` is present and is a Git repository.

The command should:

1. load the project configuration;
2. validate configured repository definitions and initialized source clones;
3. derive the normal workspace target and deterministic tombstone paths;
4. atomically acquire `<workspace-root>/.<workspace-name>.lifecycle.lock/`;
5. check that the normal workspace target and deterministic
   `<workspace-root>/.<workspace-name>.removing/` tombstone do not exist;
6. synthesize `ws/<workspace-name>/<repo-name>` for each repository and
   validate it with `git check-ref-format --branch`;
7. determine the source ref for each repository;
8. resolve every source ref and every effective default selector before
   changing worktree content or metadata state (the lifecycle lock is
   synchronization bookkeeping as specified below);
9. create:

```text
<project-root>/workspaces/<workspace-name>/
├── repos/
│   ├── app/
│   ├── library/
│   └── tools/
├── workspace.lock.toml
└── .ws/
    └── state.toml
```

10. create one Git worktree per repository;
11. every worktree must initially use detached HEAD;
12. record the immutable creation information in `workspace.lock.toml`.

The source clones under `<project-root>/repos/` are canonicalized and named by
their configuration entries. Submodules in those source clones, if present,
are untouched; do not automatically initialize or alter them. If the target
feature-workspace path or its deterministic `.removing/` tombstone already
exists, fail before mutation: do not clean it up, reuse it, or repair it.

For every repository, resolve and lock the nullable default selector separately
from the base/source ref when it is available, even when creation uses a source
override or tag. A source override does not alter the repository's configured
`default_ref`, or automatic discovery when `default_ref` is omitted, for the
locked selector. For a bare source without a configured selector or
unambiguous automatic discovery, record a null selector (omit the optional
TOML key); do not invent one. Creation must still succeed when every
repository has an explicit source override, even if all default selectors are
null.

Example conceptual lock data:

```toml
[workspace]
name = "feature-x"

[repos.app]
source_path = "/absolute/path/to/project/repos/app"
base_ref = "origin/main"
base_commit = "4a72..."
default_selector = "origin/main"

[repos.library]
source_path = "/absolute/path/to/project/repos/library"
base_ref = "origin/develop"
base_commit = "cc18..."
# default_selector omitted: null
```

The schema may differ, but preserve these concepts:

* source repository path;
* requested base/source ref;
* immutable resolved base commit;
* nullable resolved default selector, independent of `base_ref`;
* repository logical name;
* a schema version;
* absolute canonical source-clone paths.

The workspace lock is self-contained and remains authoritative after
`ws.toml` is moved or deleted. Workspace-local commands use the lock and
state. Configuration is required only to create a workspace or to locate a
workspace from outside it.

Locks persist each repository's resolved default selector independently of
later configuration or source-ref changes.

Workspace-local `status` is lock-based: it must not re-resolve the current
configuration or replace a locked selector. A config-resolving read-only view
must instead report an affected-repository configuration error when a
configured ref cannot resolve,
continue reporting other independent repositories, and preserve the command's
non-zero aggregated result. A configured error is never converted into an
automatic-discovery result.

Do not rely on branch refs continuing to point at the same commit later.

---

# 6. Source override during workspace creation

Support:

```bash
ws create feature-x \
    --source app=feature/new-api \
    --source library=v2.3.1
```

A source override may be any ref resolvable to a commit:

* local branch;
* remote-tracking branch;
* tag;
* commit SHA.

Only specified repositories receive an override.

Parse every `repo=ref` strictly before resolving refs or mutating anything:
reject malformed pairs, unknown repository names, empty refs, and duplicate
overrides. Resolve all validated overrides before worktree creation.

Repositories without a source override use their independently resolved and
locked effective default selector; if it is null, creation fails clearly
because that repository has no explicit base source. A repository with an
explicit `--source repo=ref` uses that ref as its base even when its locked
default selector is null.

The requested source ref and immutable SHA must both be recorded.

Example:

```text
app:
    requested source = feature/new-api
    locked commit    = abc123...

library:
    requested source = v2.3.1
    locked commit    = def456...
```

---

# 7. Claim semantics

Implement:

```bash
ws claim <repo>
```

`claim` converts exactly one repository from context-only detached state to an editable Git branch.

## Critical source semantics

A plain:

```bash
ws claim app
```

must create the editable branch from the **workspace's locked base commit for that repository**.

This is intentionally different from resolving the repository's default ref again.

The reason is reproducibility:

```text
ws create
    ↓
locked app commit = abc123

someone updates origin/main

ws claim app
    ↓
still branches from abc123
```

Therefore:

```bash
ws claim app
```

means:

> Claim the exact repository version represented by this workspace.

## Explicit source

Support:

```bash
ws claim app --source <ref>
```

This intentionally chooses another source.

For example:

```bash
ws claim app --source feature/base
```

must resolve `feature/base` at claim time and create the target branch from that commit.

## Special source `default`

Support:

```bash
ws claim app --source default
```

This special keyword means:

> Resolve the repository's locked default selector now and use its
> current commit as the source. In a workspace-local command, use the
> repository's independently locked selector when `ws.toml` is unavailable;
> if that selector is null, fail clearly; do not substitute the workspace's
> locked creation base.

Therefore these three operations have intentionally different semantics:

```bash
ws claim app
```

Use workspace locked base commit.

```bash
ws claim app --source default
```

Use the current commit selected by the workspace's locked default selector.

```bash
ws claim app --source origin/develop
```

Use exactly the explicitly requested ref.

This distinction must be clearly documented and thoroughly tested.

Do not interpret a literal Git ref called `default` when used in this argument. `default` is reserved as the CLI keyword.

---

# 8. Target branch semantics

`claim` must independently support selecting a target branch:

```bash
ws claim app --target feature/my-change
```

Validate an explicit `--target` at claim time with
`git check-ref-format --branch`; unlike synthesized automatic targets, it is
not validated during workspace creation.

The source remains the workspace locked base commit unless `--source` is also supplied.

For example:

```bash
ws claim app \
    --source origin/develop \
    --target feature/my-change
```

means:

1. resolve `origin/develop`;
2. create `feature/my-change` from that exact commit;
3. switch this worktree onto `feature/my-change`.

If only:

```bash
ws claim app --target feature/my-change
```

is supplied:

* source = workspace locked base commit;
* target = `feature/my-change`.

If only:

```bash
ws claim app --source default
```

is supplied:

* source = current commit selected by the locked default selector;
* target = automatically generated branch.

Use this deterministic automatic naming convention:

```text
ws/<workspace-name>/<repo-name>
```

The generated name must be validated as a Git branch name. The architecture
may allow future configuration of this convention, but does not implement a
generalized templating system now.

---

# 9. Claim safety

Handle the following carefully.

## Detached and clean

Normal claim.

## Detached and dirty

A coding agent may have edited files before remembering to claim.

This must be supported safely.

For example:

```bash
echo "change" >> file.py
ws claim app
```

must retain that change when the branch is created.

Do not stash unnecessarily if Git can safely create/switch the branch while preserving the worktree.

## Already on requested branch

Treat as idempotent only when this worktree already has the exact requested
target branch checked out. In that exact case, source selection is
creation-only: do not resolve or revalidate the source again. If the
repository is already claimed on a different branch, fail clearly.

## Already claimed on a different branch

Do not silently replace it.

Return a clear error unless another explicitly defined command such as `context` applies.

## Target branch already exists

Never reset it silently. An existing target branch is an error except for the
exact idempotency case above; do not reuse or repair it.

## Target branch checked out by another worktree

Respect Git worktree branch protection.

Do not use `--force`.

Give a concise diagnostic error.

---

# 10. Temporary repository context switching

Implement a distinct concept:

```bash
ws context <repo> <ref>
```

This is NOT the same operation as `claim`.

Its purpose is temporary inspection/testing of another repository state while preserving the repository's normal workspace state.

Typical use case:

```text
repo is claimed on:

    feature/my-change

with uncommitted modifications.

The user wants to briefly test:

    origin/main

without losing feature/my-change or its working changes.
```

They should be able to run:

```bash
ws context app default
```

or:

```bash
ws context app origin/main
```

The requested ref must then be checked out as **detached HEAD**.

The original state must be restorable.

---

# 11. `ws context` ref semantics

Support:

```bash
ws context <repo> <ref>
```

where `<ref>` is any resolvable Git ref.

Also reserve:

```bash
ws context <repo> default
```

to mean the repository's locked current default selector.

For workspace-local context, `default` resolves the repository's locked
default selector, not its locked creation/source ref, so this remains usable
after config deletion and when creation used an override or tag. If the
selector is null, fail clearly for that repository.

Example:

```bash
ws context app default
```

roughly means:

```text
save current workspace repository state
resolve the locked default selector
temporarily switch to its commit using detached HEAD
```

Similarly:

```bash
ws context app v2.0
ws context app origin/develop
ws context app abc123
```

must result in detached HEAD.

---

# 12. Context state preservation

Before entering context mode, record enough information to return to the exact previous state.

The previous state may be:

### Claimed

```text
return_branch = feature/foo
saved_head    = abc123
dirty         = yes/no
```

or:

### Detached

```text
HEAD = abc123
dirty = yes/no
```

This temporary state belongs in something like:

```text
.ws/state.toml
```

rather than changing the immutable creation information in `workspace.lock.toml`.

Keep immutable workspace creation/base metadata separate from temporary runtime state.

Lock and state writes are atomic: write a sibling temporary file, flush and
`fsync` where practical, then atomically replace the destination. Both are
schema-versioned. Context state uses explicit phases: `entering`, `active`,
`restoring`, `restore_conflicted`, and `restore_failed`.

Use write-ahead transition records for every Git side effect: persist the
intent, operation, step, expected refs, and known OIDs before invoking Git,
then persist the outcome and any commit/stash OID immediately after it. Before
stash creation, persist an unguessable unique token that is included in the
tool stash message. Only `entering` and `restoring` are blocked interrupted
phases. `active` permits restore; `restore_conflicted` permits only
`--finalize-restore`; `restore_failed` permits a user-requested `--restore`
retry only after the tool verifies the exact saved clean return baseline:
the correct return branch/HEAD, no unmerged entries, and clean tracked,
index, and untracked status. Do not attempt automatic recovery or
finalization.

For `restore_conflicted`, persist that the result awaits explicit user
certification. `--finalize-restore` is the user's acceptance decision after
checking the restored result; the tool cannot mechanically distinguish
manually resolved, aborted, or discarded changes. An abort retains
state/private snapshot because finalize is not run.

Workspace removal is a separate durable state transition: after complete
preflight, persist phase `removing` with the expected worktree identities and
per-repository completion status, the canonical workspace path, and the
deterministic tombstone path
`<workspace-root>/.<workspace-name>.removing/`. This is not a context phase.
After that state is durable, remove and verify every registered worktree while
the normal workspace path still exists. A crash before worktree completion
resumes from that normal path; no worktree-containing directory is renamed.
Mark `removal_complete` durably only after every expected normal workspace
worktree path is absent and its exact source Git registration is gone. Only
then atomically rename the remaining metadata-only workspace directory to the
tombstone and delete that tombstone. A crash after rename resumes only from
that exact metadata-only tombstone. No scanning is permitted. A failure
retains the progress for a rerunnable retry that considers only still-
registered expected worktrees.

While `removing` or `removal_complete` is durable, block every mutator except
a matching `ws remove <workspace-name>` retry whose name matches the locked
workspace.
`status` remains read-only. Reject mismatched remove names and all claim,
context, restore, and finalize attempts without mutation.

---

# 13. Dirty changes when entering context

The primary intended behavior is:

```text
claimed feature branch
+
uncommitted changes
↓
ws context app default
↓
changes safely stored
default-ref commit checked out detached
↓
test something
↓
ws context app --restore
↓
feature branch restored
+
original uncommitted changes restored
```

Implement this automatically.

Use Git's real stash mechanism or another equally safe Git-native mechanism.

Use `git stash push --include-untracked` with a distinctive tool message/tag
for the shared entry. The shared entry is intentionally retained for manual
cleanup; it must never be dropped by the tool.

Conceptually:

```bash
git stash push --include-untracked -m "ws-context:<workspace>:<repo>:<token>"
```

Persist an unguessable unique stash-message token before running stash
creation, and include that token in the message. If stash creation is
interrupted before an OID is recorded, recovery instructions tell the user to
locate and verify a stash by that token and then record its OID manually; do
not infer `stash@{0}`, auto-recover, or consume a stash by position. Once the
OID is recorded, immediately pin it in the private tool ref before any apply
or restore operation.

For example conceptually:

```text
ws-context:<workspace>:<repo>:<unique-id>
```

Requirements:

* preserve tracked modifications;
* preserve staged modifications;
* preserve untracked files;
* do not accidentally consume unrelated user stash entries;
* record the exact stash object/reference used;
* once the stash OID exists, immediately create an atomic, private OID-pinned
  tool ref such as `refs/ws/context/<workspace>/<repo>/<token>` and retain it
  through apply and recovery;
* apply from that pinned ref, not from a stash-list position;
* persist the intent before and outcome after stash creation, application, and
  private-ref deletion;
* never rely merely on "stash@{0}" still referring to the same stash later.

At successful cleanup, delete only the private tool ref atomically. The shared
`refs/stash` entry is always retained because Git cannot safely delete a
non-tip entry by OID. Report its exact OID and distinctive message, with safe
manual cleanup guidance; never select, drop, or rewrite it by reflog position
and never risk an unrelated stash.

Use object IDs or another robust identifier where practical.

Do NOT automatically include ignored files unless clearly necessary.

---

# 14. Restoring context

Implement:

```bash
ws context <repo> --restore
```

This must restore the repository to the exact pre-context state.

If the repository was previously claimed:

```text
feature/foo
```

restore that branch.

Before checking it out, verify that the recorded `return_branch` still
exists, is not checked out by another worktree, and still points to the
recorded `saved_head`. If any check fails, refuse without resetting or moving
the branch, and retain the context state and tool stash.

If it was previously detached:

restore the previous detached commit.

Then restore any automatically saved dirty changes.

On successful restoration:

* atomically delete only the private OID-pinned tool ref;
* retain the shared `refs/stash` entry and report its exact OID/message for
  manual cleanup;
* clear the repository's active context metadata.

The shared entry is never dropped or rewritten, whether or not unrelated
stashes were added. Do not touch unrelated stash entries.

If restoration encountered a conflict, provide:

```bash
ws context <repo> --finalize-restore
```

Mark the context `restore_conflicted` and retain the stash OID. The user must
manually decide whether the restored result is accepted and explicitly
certify it with `--finalize-restore`. Finalization is allowed only when there
are no unmerged index entries; it preserves the
resolved worktree's staged and unstaged changes and index state, safely deletes
only the private OID-pinned snapshot, retains the shared `refs/stash` entry,
reports its exact OID/message for manual cleanup, and then clears context
state. An aborted or discarded resolution retains the stash and state until
the user chooses whether to run this explicit certification; the tool cannot
mechanically distinguish those choices. There is no destructive finalize
mode. It must not select a stash by list position or finalize an interrupted
or non-conflicted state.

---

# 15. Context restoration conflicts

If applying the saved changes produces conflicts:

* do not lose the saved changes;
* do not delete the underlying stash;
* mark the context state `restore_conflicted`;
* preserve sufficient state for manual recovery;
* return a clear error explaining:

  * branch/ref was restored;
  * applying stored changes conflicted;
  * the stash has intentionally been retained;
  * its identifier/OID;
  * the `ws context <repo> --finalize-restore` recovery command.

`restore_conflicted` is distinct from `restore_failed`: use it only when the
stash application leaves unmerged index entries. The explicit finalize
command is the user's certification that the restored result is accepted and
may safely delete the private snapshot only when none remain. The tool cannot
mechanically distinguish manually resolved, aborted, or discarded changes;
aborting simply retains state/private snapshot because the user does not run
finalize.

If stash application fails without unmerged entries, mark the state
`restore_failed`, retain the stash and transition record, and explain that the
user must make the worktree safe before retrying:

```bash
ws context <repo> --restore
```

This retry is never automatic, does not finalize or delete the private
snapshot, and must
not use a stash position. The user must manually clean or reconcile all
partial-apply effects first; the tool then rechecks the exact saved clean
return baseline (correct branch/HEAD, no unmerged entries, and clean tracked,
index, and untracked status). Never reapply a stash onto a partially changed
worktree; retry applies only the retained private OID-pinned tool ref. No
`--finalize-restore` is permitted for `restore_failed`.

Do not attempt aggressive automatic conflict resolution.

Safety is more important than making the command appear successful.

---

# 16. Dirty changes created while in context mode

Context mode is primarily intended for inspection, testing, builds, and experiments, not development.

Nevertheless users can modify files.

Therefore before:

```bash
ws context app --restore
```

inspect whether the temporary detached context has become dirty.

For the first version, use conservative behavior:

* if context mode contains uncommitted modifications that did not exist before entering context, REFUSE automatic restore;
* do not discard them;
* provide a concise error telling the user to commit/stash/remove those temporary changes before restoring.

Do not silently merge temporary context edits with the original feature-branch work.

Do not automatically discard them.

This keeps the semantics safe and predictable.

---

# 17. One active context per repository

For this initial implementation, support only one temporary context level per repository.

Do not implement nested context stacks yet.

If:

```bash
ws context app origin/main
```

is active and the user tries:

```bash
ws context app v2
```

fail with a clear message such as:

```text
repository 'app' already has an active temporary context;
restore it first with:

    ws context app --restore
```

This avoids complicated nested stash semantics in the first version.

Document this as a current limitation.

---

# 18. Context interaction with claim

If a repository has an active temporary context:

```bash
ws claim app
```

must refuse.

The user must first run:

```bash
ws context app --restore
```

The tool must not claim a temporary detached context accidentally.

Likewise, destructive workspace operations should be context-aware.

---

# 19. Status

Implement:

```bash
ws status
```

Show at least:

* workspace name;
* repository;
* locked source/base ref;
* locked default selector;
* locked base commit;
* current HEAD;
* detached vs claimed;
* claimed branch if any;
* dirty/clean;
* active temporary context if any;
* context transition phase if any;
* context target if active;
* durable removal phase/progress if removal is in progress;
* return branch/commit if useful.

Example:

```text
WORKSPACE feature-x

repo       base             HEAD      mode      branch              dirty  context
-----------------------------------------------------------------------------------
app        origin/main      a8231cd   claimed   feature/my-change   yes    -
library    origin/develop   c21ef19   detached  -                   no     -
tools      origin/main      f11be21   context   -                   no     origin/main
```

Exact formatting is up to you.

Also implement:

```bash
ws status --json
```

Return stable structured data suitable for coding agents.

On success, `--json` writes JSON only to stdout. Errors use concise stderr
diagnostics and a non-zero exit status; v1 has no JSON error protocol.

The ordinary workspace-local status view reads only the lock and state, so it
continues to work without consulting `ws.toml` and never re-resolves a current
default ref. A config-resolving read-only view reports each
affected repository's configured-ref error, continues with independent
repositories, and exits non-zero when any repository failed; it must not fall
back to discovery for that repository.

Conceptual example:

```json
{
  "workspace": "feature-x",
  "removal": null,
  "repos": {
    "app": {
      "locked_base_ref": "origin/main",
      "locked_default_selector": "origin/main",
      "locked_base_commit": "4a72...",
      "head": "913a...",
      "mode": "claimed",
      "branch": "feature/my-change",
      "detached": false,
      "dirty": true,
      "context": null
    },
    "tools": {
      "locked_base_ref": "origin/main",
      "locked_default_selector": "origin/main",
      "locked_base_commit": "aa12...",
      "head": "bb31...",
      "mode": "context",
      "branch": null,
      "detached": true,
      "dirty": false,
      "context": {
        "target_ref": "default",
        "target_commit": "bb31...",
        "phase": "active",
        "return_mode": "claimed",
        "return_branch": "feature/tools",
        "return_saved_head": "913a..."
      }
    }
  }
}
```

Do not require scripts/agents to parse human tables.

Test successful `ws status --json` with a strict JSON parser and verify that
an error produces no JSON on stdout, only a concise stderr diagnostic and a
non-zero exit.

After creation, change or remove the configuration and verify workspace-local
status still reports the locked default selectors without re-resolving current
configuration. For a config-resolving read-only view, make one configured ref
unavailable and verify it reports that affected-repository error, continues
with independent repositories, does not discover a replacement, and preserves
the command's non-zero aggregated result.

---

# 20. Workspace discovery

Commands should work from anywhere below a workspace.

For example:

```bash
cd workspaces/feature-x/repos/app/src/foo/bar
ws status
ws claim library
ws context tools default
```

Implement upward discovery based on workspace metadata.

Do not require manually specifying the workspace root for these commands.

Also support running commands directly from the workspace root.

---

# 21. Workspace removal

Implement:

```bash
ws remove <workspace-name> [--config PATH]
```

Outside a workspace this accepts `--config PATH` and defaults to
`./ws.toml`; inside the named workspace it may use upward `.ws` discovery
instead. Resolution precedence is exact: `--config` wins; otherwise use a
discovered workspace when available; otherwise use `./ws.toml`. In every
case, resolve the lock and require its workspace name to equal the CLI
`<workspace-name>` before mutation. A discovered workspace/config disagreement
is an error, including when an explicit config is supplied from inside a
workspace.

For a matched workspace name, derive the only recovery candidate as
`<workspace-root>/.<workspace-name>.removing/`. If the normal workspace path
or this exact tombstone exists, use its persisted state; do not search the
workspace root or filesystem for other remnants. A name/path/lock mismatch is
an error before mutation; both normal and tombstone paths existing is also an
error requiring manual recovery.

Requirements:

* remove only worktrees belonging to the logical workspace;
* use normal Git worktree mechanisms;
* never delete source repositories;
* never delete Git branches;
* never remove unrelated worktrees;
* clean only exact expected registrations as part of verified worktree removal;
* before removing any worktree, preflight every locked source for availability,
  every expected worktree for identity and Git registration, every worktree for
  uncommitted changes, and every repository for active context/state;
* refuse removal, preserving the workspace, if any preflight check fails,
  including an unavailable source or identity/registration mismatch;
* after a successful full preflight, persist durable `removing` state and
  per-worktree progress before the first removal;
* consider an expected worktree removed only when both its canonical expected
  normal workspace path is absent and the source Git metadata no longer
  registers that exact worktree; any mismatch fails safely;
* if removal later fails, preserve that state for a clearly recoverable,
  rerunnable retry; retries remove only still-registered expected worktrees;
* persist `removal_complete` only after all worktrees are removed and verified;
* atomically rename the remaining metadata-only workspace directory to exactly
  `<workspace-root>/.<workspace-name>.removing/`, then delete that tombstone;
  recognize/resume only this deterministic tombstone after a crash, never by
  scanning for candidates;
* never rename or delete a directory containing registered worktrees;
* provide clear diagnostics.

A future explicit force mode may be added later.

Do not implement dangerous force behavior now.

## Per-workspace operation locking

Create and remove share this external, atomically-created lifecycle lock:

```text
<workspace-root>/.<workspace-name>.lifecycle.lock/
```

It is acquired at operation start and held through successful create or
complete rollback, or through complete tombstone deletion for remove. Create
acquires it before checking the normal target/tombstone and before source-ref
resolution. Remove acquires it before the internal workspace operation lock;
the lock order is therefore **external lifecycle lock, then internal
`.ws/operation.lock`**. Creation acquires the internal lock after initializing
the workspace metadata and holds both through completion/rollback.

The internal per-workspace operation lock is an atomically-created directory:

```text
.ws/operation.lock/
```

If either lock already exists, fail without mutation; never automatically
break or adopt a stale lock. After a real crash, before retrying `ws remove
<workspace-name>` the user must verify that no operation is running and
manually remove both exact stale locks: the external
`<workspace-root>/.<workspace-name>.lifecycle.lock/` and the internal
`.ws/operation.lock/` located in either the normal workspace or the
deterministic tombstone. Never auto-break or adopt either lock. The external
lifecycle lock survives internal metadata/tombstone deletion until operation
completion. `status` is read-only and does not take this mutator lock.

Creating the external lifecycle-lock directory is synchronization bookkeeping,
not a source checkout/content mutation and is the explicit exception to the
pre-worktree no-content-mutation rule. It must occur before source-ref and
default-selector resolution; no source repository contents may change.

---

# 22. Transactional `create`

Workspace creation should behave transactionally as far as reasonably possible.

If repository N fails after repositories 1...N-1 have been created:

* remove previously created worktrees;
* clean associated Git worktree registrations;
* remove incomplete metadata;
* remove the incomplete workspace directory if safe;
* leave source repositories unchanged.

Do not leave half-created logical workspaces.

Resolve all refs before starting worktree creation where possible so invalid
refs fail before worktree/filesystem mutations; the already-acquired lifecycle
lock directory is synchronization bookkeeping, not a worktree mutation.

Creation must validate strict source-override syntax and all source safety
constraints before this transaction begins. Metadata writes use the atomic
schema-versioned write procedure described above. A failed or interrupted
transaction must not silently reuse an existing target.

---

# 23. Source repository safety

The existing repositories configured by `path` are source repositories.

Operations must not unexpectedly change their working tree state.

In particular, do not:

* switch their currently checked-out branch;
* reset them;
* stash their modifications;
* remove files from them;
* clean them.

Git operations that alter worktree registration metadata are expected, but
source checkout contents must remain untouched. Creation-lock directory
creation is synchronization bookkeeping only and is not source/content
mutation.

---

# 24. Git helper layer

Centralize Git invocation.

Each failed Git command should provide useful diagnostic context:

* command/arguments;
* repository/path;
* exit status;
* relevant stderr.

Use argument arrays.

Never concatenate shell commands.

Prefer explicit Git commands whose behavior is stable and easy to test.

Avoid parsing highly human-oriented Git output when machine-oriented alternatives exist.

The helper layer must support atomic OID-pinned private-ref creation, apply,
and deletion (for example via `git update-ref`) without reflog-position races;
it must never delete or rewrite shared `refs/stash`. It must also expose exact
unmerged-entry and tracked/index/untracked status checks for restore retries
and removal preflight, plus canonical worktree identity, exact
source-registration checks, and narrowly scoped registration repair for the
deterministic removal tombstone.

---

# 25. Integration tests are mandatory

Integration tests are a first-class requirement.

Do NOT mock Git for integration tests.

Create real temporary repositories using pytest `tmp_path`.

Build reusable helpers/fixtures that create actual histories:

```text
repo A:

main
A---B---C
     \
      D---E feature/base
```

and:

```text
repo B:

develop
F---G
```

Configure local Git identity inside test repos so commits work independently from the host user's global Git config.

Prefer executing the real installed/module CLI as a subprocess for end-to-end tests.

---

# 26. Required integration tests: creation

At minimum implement the following.

## Multi-repo create

Given repositories whose effective default refs are:

```text
repo-a -> main
repo-b -> develop
repo-c -> trunk
```

run:

```bash
ws create foo
```

Verify:

* all worktrees exist;
* all are detached;
* all point to the correct resolved commits;
* immutable commits are in the lock file.

## Explicit creation source

```bash
ws create foo --source repo-b=feature/special
```

Verify only repo-b uses that ref.

## Commit source

Test:

```bash
ws create foo --source repo-a=<SHA>
```

## Tag source

Test a tagged commit.

## Configuration and validation

Test that:

* `./ws.toml` is the default for create/remove and `--config PATH` selects a
  specific file, with no upward config search;
* malformed, empty, duplicate, or unknown `--source repo=ref` values fail
  before resolution or mutation;
* invalid workspace/repository IDs fail for separators, dot segments,
  control characters, leading dashes, and characters outside
  `[A-Za-z0-9][A-Za-z0-9._-]*`;
* an existing target path fails without cleanup, reuse, or repair;
* derived clone names are unique, source clones are fixed below the project
  `repos/` root, and feature workspaces are fixed below `workspaces/`;
* `ws init` publishes all source clones atomically and rolls back only its
  owned temporary clone root after a failed clone;
* submodules are not initialized or changed;
* a bare source is supported, while automatic fallback does not use a
  checked-out branch and records a nullable selector;
* each repository's `default_ref`, when present, is authoritative; when it is
  omitted, automatic discovery is used independently for that repository;
* configured branches, tags, remote-tracking refs, full commit IDs, and
  abbreviated commit IDs all resolve through the required commit verification;
* an unavailable configured ref remains authoritative for the default selector
  and never falls back to automatic discovery; creation fails without an
  explicit source override for that repository, but succeeds with one and
  records a null selector;
* automatic multi-remote discovery is skipped when a repository supplies
  `default_ref`, including when remote symbolic HEADs disagree;
* automatic fallback uses one consistent remote symbolic HEAD, then a non-bare
  checked-out branch, and rejects disagreement or ambiguity without guessing;
* every repository lock records a nullable default selector independently of
  its base ref, including explicit-default, source-override, tag, and
  bare-source cases; all-explicit overrides succeed with null selectors, while
  `claim/context ... default` fails clearly only for the affected repository;
* after creation, status reads the locked selectors without re-resolving
  configuration; a config-resolving read-only view reports affected errors,
  continues independent repositories, and preserves non-zero aggregation;
* a created workspace remains operable from its lock/state after `ws.toml` is
  moved or deleted, while creation still requires config.
* synthesized automatic targets are checked with
  `git check-ref-format --branch` before worktrees are created; names such as
  `a..b` and `name.lock` fail even if they pass the logical-name regex.
* lifecycle-lock directory bookkeeping occurs before ref resolution without
  changing source checkout contents and is cleaned after success or rollback.
* create rejects either an existing normal workspace target or its exact
  deterministic `.removing/` tombstone while holding the external lifecycle
  lock.

---

# 27. Required integration tests: claim

## Default claim uses locked base

Create workspace from `main` at commit A.

Then advance the source repository's `main` to commit B.

Run:

```bash
ws claim repo-a
```

Verify the created branch starts from A, not B.

This is a critical semantic test.

## `--source default`

Using the same setup:

```bash
ws claim repo-a --source default
```

must branch from B.

Also create with an explicit `default_ref` and with a source override/tag,
delete `ws.toml`, advance the locked default selector, and verify
workspace-local `ws claim repo-a --source default` and
`ws context repo-a default` use the independently locked selector rather than
the creation base. Repeat with a bare repository and an explicit default
selector.

Repeat the claim setup with per-repository `default_ref` values and with an
omitted value that uses automatic discovery. Verify the selected and locked
selectors are independent per-repository values, and that a source override or
tag does not change this choice.

## Explicit source

```bash
ws claim repo-a --source feature/base
```

must branch from the exact currently resolved feature/base commit.

## Explicit target

```bash
ws claim repo-a --target feature/foo
```

Verify exact branch name.

## Explicit source and target

```bash
ws claim repo-a \
    --source feature/base \
    --target experiment/foo
```

Verify both semantics.

## Dirty detached claim

Modify tracked and untracked files before claiming.

Verify they survive.

## Branch collision

Attempt to claim a target branch already checked out by another worktree.

Verify safe failure.

## Existing branch

Ensure an existing target branch is never reset, reused, or repaired, except
when this worktree already has the exact automatically requested target
checked out. Verify that exact idempotency does not revalidate its creation
source, while a different existing claimed branch fails.

When this worktree is already on an explicitly requested `--target`, rerun
claim with that same target and a stale or unresolvable `--source`. Verify the
command is an idempotent no-op and does not resolve or validate that
creation-only source.

## Automatic target naming

Verify a default claim creates exactly:

```text
ws/<workspace-name>/<repo-name>
```

and rejects collisions unless the exact idempotency rule applies.

Run a claim with a synthesized target whose logical components include
`a..b` or `name.lock` and verify creation had already rejected it through
`git check-ref-format --branch`; explicit `--target` validation remains at
claim time.

---

# 28. Required integration tests: temporary context

These tests are especially important.

## Claimed clean branch -> default context -> restore

Start:

```text
repo-a:
claimed branch feature/foo
clean
```

Run:

```bash
ws context repo-a default
```

Verify:

* HEAD is detached;
* HEAD equals current effective default-ref commit;
* original claimed branch remains intact;
* context metadata records return branch.

Then:

```bash
ws context repo-a --restore
```

Verify:

* feature/foo is checked out again;
* context metadata is cleared.

Before restore, test that a claimed return branch that was moved, deleted, or
checked out by another worktree causes refusal. Verify the branch is never
reset or moved, and the recorded return branch, saved HEAD, context state, and
tool stash are retained for manual recovery.

---

## Claimed dirty branch -> context -> restore

On:

```text
feature/foo
```

create:

* unstaged tracked change;
* staged change;
* untracked file.

Run:

```bash
ws context repo-a default
```

Verify:

* context is detached;
* those changes are no longer present in temporary context;
* tool-owned stash exists;
* private OID-pinned tool ref exists;
* unrelated stash entries are untouched.

Then:

```bash
ws context repo-a --restore
```

Verify:

* feature/foo restored;
* unstaged tracked change restored;
* staged state restored if Git's stash semantics allow reliable preservation;
* untracked file restored;
* only the private OID-pinned tool ref is deleted after successful application;
* the shared `refs/stash` entry is retained and its exact OID/message is
  reported for manual cleanup;
* unrelated stashes still exist.

If preserving the exact staged/index state requires specific Git options such as `stash apply --index`, implement and test that behavior.

Create unrelated user stashes after entering context, so they are newer than
the tool stash. Restore must still use the recorded tool-stash OID and leave
the reordered unrelated entries untouched. Verify shared `refs/stash` is never
deleted or rewritten, regardless of its current entry, while only the private
tool ref is deleted after safe completion and the exact tool OID/message is
reported.

Inject real-Git failures immediately after each context side-effect boundary:

* tool stash creation;
* detached context checkout;
* return-branch checkout;
* stash application;
* immediately before private-ref deletion/state clear.

For every boundary, verify persisted completed steps and commit/stash OIDs
leave a recoverable transition state, no unrelated stash is consumed, and no
automatic recovery or destructive cleanup occurs.
Verify each injected Git call has a durable intent record before the call and
an outcome record after success/failure, including checkout and stash OID
details.
Also interrupt stash creation before its OID outcome is recorded: verify the
persisted unguessable message token is the only recovery locator, manual
token-based inspection can identify the stash, and no stash position is used.
After successful stash creation, verify the shared entry's exact OID/message
is reported and retained even after the private ref is deleted.

---

## Detached workspace state -> context -> restore

A repository need not be claimed before using context.

Start on the normal locked detached HEAD.

Run:

```bash
ws context repo-a feature/other
```

then restore.

Verify the repository returns to the original detached commit.

---

## Explicit context ref

Test:

```bash
ws context repo-a v2.0
```

and:

```bash
ws context repo-a origin/develop
```

Both must checkout detached commits.

---

## Context `default`

Advance the effective default ref after workspace creation.

Run:

```bash
ws context repo-a default
```

Verify that `default` resolves the current commit selected by the locked default
selector rather than the workspace's locked base.

Run this with a per-repository configured value and a fully automatic
repository. Verify each context operation uses the appropriate locked
selector, and that a source override does not replace it.

---

## Active context prevents another context

Run:

```bash
ws context repo-a default
ws context repo-a feature/foo
```

The second command must fail safely.

---

## Active context prevents claim

Verify:

```bash
ws claim repo-a
```

fails while temporary context is active.

---

## Dirty temporary context prevents restore

Enter context.

Modify a file inside temporary context.

Run:

```bash
ws context repo-a --restore
```

Verify:

* restore is refused;
* temporary modification remains intact;
* original saved stash remains intact;
* original return state metadata remains intact.

Nothing should be lost.

---

## Context restore conflict

Construct a real scenario where applying the saved stash conflicts.

Verify:

* no saved data is silently deleted;
* stash remains available;
* error reports the stash identifier;
* state is marked `restore_conflicted` and is recoverable;
* `ws context repo-a --finalize-restore` refuses while unmerged index entries
  remain;
* after the user resolves conflicts while retaining any intended staged and
  unstaged changes, finalize observes no unmerged entries, preserves that
  worktree/index state, deletes only the private OID-pinned snapshot, retains
  the shared stash entry and reports its exact
  OID/message, and clears state;
* aborting or discarding the resolution without running finalize retains both
  shared stash and private snapshot/state;
* if the operator explicitly runs finalize after such an abort/discard and no
  unmerged entries remain, the tool follows the same mechanical checks because
  it cannot distinguish the operator's choice; documentation must make that
  responsibility explicit.

Construct a separate stash-application failure that leaves no unmerged index
entries. Verify state becomes `restore_failed`, the stash and write-ahead
records remain, `--finalize-restore` is rejected, and retry is refused until
the user manually reconciles all partial-apply effects. Before retry, verify
the tool requires the exact saved return branch/HEAD, no unmerged entries, and
clean tracked, index, and untracked status; it must never reapply onto a
partially changed worktree. Then verify a safe `--restore` retry proceeds
from the retained private OID-pinned tool ref without selecting a stash by
position.

Also test that interrupted `entering` and `restoring` phases block automated
mutation, give recovery details, and never auto-recover; `active` permits
restore, while `restore_conflicted` permits only `--finalize-restore`.

---

# 29. Multiple logical workspaces

Create:

```bash
ws create agent-a
ws create agent-b
```

from the same source repositories.

Verify:

* worktree paths are independent;
* both are valid;
* detached repositories coexist;
* modifications in one do not alter another;
* claiming different branches works;
* context operations are workspace-local.

This models parallel coding-agent usage and is important.

---

# 30. Rollback test

Force repository N to fail during workspace creation.

Verify:

* repositories 1...N-1 have no remaining workspace worktrees;
* incomplete workspace directory is cleaned;
* source repositories remain valid;
* `git worktree list` has no stale entries belonging to the failed workspace.

Also inject failures around worktree creation and metadata replacement where
practical. Verify sibling temporary files never become authoritative and
state is either the prior valid state or an explicitly blocked,
recoverable phase.

Run real concurrent subprocesses for the same workspace and verify only one
creation proceeds. The exact
`<workspace-root>/.<workspace-name>.lifecycle.lock/` is present during source
resolution and removed after both successful creation and complete rollback.
An existing stale lifecycle lock refuses creation without auto-breaking it.
After an injected crash, verify both the external lifecycle lock and any
internal `.ws/operation.lock` survive at the normal workspace or deterministic
tombstone path; manually confirm no operation is running, remove both exact
stale locks, and then retry. Neither lock may be auto-broken or adopted.
Also race a mutator against a workspace operation and verify the operation
lock serializes it safely.

---

# 31. Removal tests

Test:

```bash
ws remove foo
```

Verify:

* workspace worktree directories disappear;
* source repositories remain;
* branches remain;
* Git worktree lists are clean.

Also test refusal for:

* dirty worktree;
* active temporary context;
* unavailable locked source, verifying the workspace is preserved;
* an existing `.ws/operation.lock` in the normal workspace or tombstone,
  verifying mutators fail without breaking it and status remains read-only;
* an existing `<workspace-root>/.<workspace-name>.lifecycle.lock/`, verifying
  removal refuses without auto-breaking it;

Force a failure while removing a later repository after earlier expected
worktrees were removed. Verify durable `removing` progress identifies the
expected registrations and completed repositories, source repositories and
branches remain intact, and a retry removes only still-registered expected
worktrees before clearing the progress state.

While `removing` or `removal_complete` is persisted, verify claim, context,
restore, finalize, and mismatched remove commands fail without mutation; only
matching `ws remove <name>` may retry. For each repository, assert removal is
credited only when both the canonical expected path is absent and the source
Git registration for that exact worktree is absent. Inject a mismatch and
verify safe refusal.

Interrupt after all worktrees satisfy both absence checks but before metadata
rename; verify `removal_complete` remains, the normal workspace is metadata-
only, and matching remove can safely finish cleanup.

Test removal precedence and identity checks: `--config` wins, otherwise
discovery wins over `./ws.toml`, and an explicit config from inside a
workspace is rejected when its resolved workspace disagrees. Every mismatch
must fail before mutation, including when the CLI name differs from the lock
workspace name.
Also verify that both the normal workspace path and its deterministic
`.removing/` tombstone existing is rejected without scanning or mutation.

Verify removal preflights all sources and all expected worktree paths and Git
registrations, dirty states, and context states before removing any one
worktree; a later failure during removal must leave durable retry progress.
Verify remove acquires the external lifecycle lock before the internal
`.ws/operation.lock`, and that the external lock remains until the tombstone
is completely deleted, including when internal metadata is gone.

Inject real failures at removal boundaries and verify deterministic tombstone
recovery:

* crash after persisting `removing` while registered worktrees remain;
* crash after partial worktree deletion while the normal workspace still
  exists;
* crash after persisting `removal_complete` but before the atomic rename;
* crash after renaming the metadata-only directory to
  `<workspace-root>/.<workspace-name>.removing/`;
* crash during partial tombstone metadata deletion.

After every real crash, verify the external lifecycle lock and internal
`.ws/operation.lock` survive at the normal workspace or deterministic
tombstone path; the user must confirm no operation is running and manually
remove both exact stale locks before retrying. Neither lock may be auto-broken
or adopted. Each retry must remove/verify worktrees under the normal
workspace path before rename, or derive only the exact metadata-only tombstone
after rename, preserve source repositories and branches, enforce identity and
registration checks, and delete the tombstone only after completion state is
durable.

---

# 32. Unit tests

Add focused unit tests where useful for:

* configuration parsing;
* relative path resolution;
* automatic target branch generation;
* special `source=default` parsing;
* lock serialization;
* state serialization;
* durable `removing` progress serialization;
* deterministic tombstone path and `removal_complete` serialization;
* context-state representation;
* default-selector lock serialization independent of base/source refs;
* per-repository `default_ref` parsing and automatic-discovery selection;
* configured branch, tag, remote-tracking, full-ID, and abbreviated-ID
  resolution, including authoritative unavailable-ref errors;
* return-branch/saved-HEAD validation;
* strict workspace/repository identifier and `repo=ref` parsing;
* per-repository default-ref selection, fallback, and remote-HEAD disagreement;
* derived source-clone paths and fixed project-root locations;
* atomic metadata writes and schema-version validation;
* write-ahead transition intent/outcome and stash-token serialization;
* OID-pinned private stash refs and shared-entry retention;
* lifecycle/internal lock ordering and stale-lock diagnostics;
* Git result parsing.

Do not substitute mocked unit tests for real Git integration tests.

---

# 33. Machine-friendly behavior

The CLI will frequently be called by coding agents.

Design accordingly:

* no interactive prompts;
* deterministic output;
* meaningful non-zero exit statuses;
* concise diagnostics;
* safe failures;
* JSON output where useful.

At minimum:

```bash
ws status --json
```

should be a stable interface.

Structure the internals so `--json` can later be added to mutating commands without parsing console prose.

Do not build an RPC/API server.

---

# 34. Future `uv` integration

The next development phase will add optional Python multi-repository support using `uv`.

Likely future behavior will include a generated workspace-level virtual project such as:

```text
workspace/
├── pyproject.toml
├── uv.lock
├── .venv/
└── repos/
```

with selected repositories configured as editable local path dependencies.

For this phase:

* do NOT require any repo to be Python;
* do NOT call `uv sync` as part of `ws create`;
* do NOT invent a plugin system;
* keep paths/models clean enough that a future workspace setup layer can be added without rewriting the Git core.

The Git/worktree/context lifecycle is the priority.

---

# 35. README requirements

Document the mental model thoroughly but concisely.

Include examples.

## Create

```bash
ws create feature-a
```

All repositories start detached.

## Different initial source

```bash
ws create feature-a \
    --source library=feature/new-api
```

## Claim locked workspace version

```bash
ws claim app
```

Explain explicitly that this uses the workspace's immutable locked base commit.

## Claim current default ref

```bash
ws claim app --source default
```

## Claim arbitrary source into arbitrary target

```bash
ws claim app \
    --source origin/develop \
    --target feature/new-api
```

## Temporary context

Given:

```text
app -> feature/new-api
dirty changes
```

run:

```bash
ws context app default
```

Explain:

```text
feature changes
    ↓ automatically saved
current default commit
    ↓ detached temporary context
```

Then:

```bash
ws context app --restore
```

restores:

```text
feature/new-api
+
original working changes
```

Also document:

* `ws.toml`/`--config PATH` behavior and the fact that config is not searched
  upward;
* per-repository `default_ref` configuration, with automatic discovery used
  only when that repository omits the value;
* configured default refs may be branches, tags, remote-tracking refs, or
  full/abbreviated commit IDs; an unavailable configured value is retained as
  a null selector rather than falling back to discovery, and an explicit
  source override can still provide the creation base;
* strict workspace and repository naming rules;
* automatic claim branches as `ws/<workspace-name>/<repo-name>` and their
  collision/idempotency rules;
* each repository's default selector is locked separately from its creation
  source, so `default` works after config deletion;
* context phases, operation-lock recovery guidance, and the fact that dirty
  temporary context must be resolved by the user before restore;
* restore verifies a claimed return branch still points to its saved HEAD and
  is available before checking it out;
* `restore_failed` retries only after the exact saved clean return baseline is
  restored: correct branch/HEAD, no unmerged entries, and clean tracked,
  index, and untracked status;
* tool stashes use private OID-pinned refs; the shared stash entry is retained
  with its exact OID/message for safe manual cleanup;
* removal uses only the deterministic sibling
  `<workspace-root>/.<workspace-name>.removing/` tombstone, resumes it after a
  crash only after worktrees were removed from the normal path and the user
  manually verified no operation runs and removed both exact stale lifecycle
  and internal operation locks; it never scans for remnants;
* distinguish `restore_conflicted` (unmerged entries, user-certified
  finalize) from `restore_failed` (retryable stash-apply failure without
  unmerged entries); neither state is auto-recovered;
* restore conflicts as `restore_conflicted`, including:

  ```bash
  ws context app --finalize-restore
  ```

  which is allowed only when no unmerged index entries remain; it preserves
  the accepted staged/unstaged worktree and index state, deletes only the
  private OID-pinned snapshot, retains the shared stash entry, and reports its
  exact OID/message. An aborted or discarded resolution keeps the stash and
  state when finalize is not run; the tool cannot mechanically distinguish
  those choices.
  `restore_failed` permits only a user-safe `--restore` retry and never
  `--finalize-restore`.

Document these v1 limitations explicitly: tool-created shared stash entries
remain for manual cleanup with their exact OID/message reported, finalize is
operator certification and cannot detect discarded versus resolved changes,
and removal recovery uses only the deterministic `.removing/` tombstone.

---

# 36. Implementation phases

Work incrementally.

Do not write everything before running tests.

## Phase 1 — foundation

Implement:

* project/package structure;
* models;
* `ws.toml` configuration and explicit config selection;
* parsing of per-repository `default_ref` values and automatic discovery;
* strict identifier/source-override validation;
* schema-versioned lock/state models and atomic metadata writes;
* Git subprocess layer;
* OID-pinned private-ref helpers with shared-stash retention;
* test Git repository factory/fixtures.

Run tests.

Fix all failures.

## Phase 2 — workspace create

Implement:

* default source resolution;
* exact nullable per-repository default-ref selection and fallback order,
  including bare sources;
* commit-verifying configured refs, authoritative unavailable-ref failures, and
  skipping automatic multi-remote discovery whenever a repository supplies a
  value;
* independent default-selector locking;
* explicit `--source repo=ref`;
* immutable ref resolution;
* derived source-clone paths and fixed project-root locations;
* detached worktrees;
* lock file;
* external lifecycle locking and existing-target/tombstone refusal;
* transactional rollback.

Run tests.

Fix all failures.

## Phase 3 — status and discovery

Implement:

* upward workspace discovery;
* human status;
* JSON status;
* dirty/branch/detached detection.

Status must remain lock-based after creation and must not re-resolve current
configuration. Any config-resolving read-only view must aggregate independent
repository results, report affected configured-ref errors without discovery
fallback, and retain a non-zero result when any repository is affected.

Ensure successful JSON status has stdout-only JSON and failures have concise
stderr/non-zero behavior.

Run tests.

Fix all failures.

## Phase 4 — claim

Implement:

* locked-base default source;
* `--source default`;
* explicit source ref;
* automatic target;
* explicit target;
* dirty detached preservation;
* exact `ws/<workspace-name>/<repo-name>` automatic branches;
* `git check-ref-format --branch` validation for synthesized targets during
  create;
* existing-branch/idempotency rules;
* exclusive operation locking;
* collision safety.

Run all tests.

Fix all failures.

## Phase 5 — temporary context

Implement:

* `ws context <repo> <ref>`;
* `default` context source using the locked default selector;
* return-state recording;
* return-branch/saved-HEAD availability and identity checks;
* safe automatic stash;
* detached temporary checkout;
* `--restore`;
* explicit context phases and interrupted-state blocking;
* distinct `restore_conflicted` and retryable `restore_failed` handling;
* index/staged restoration;
* untracked restoration;
* active-context guards;
* conflict handling;
* `--finalize-restore` safety semantics;
* failure-boundary persistence and OID-based recovery;
* write-ahead intent/outcome records and token-based stash recovery;
* OID-pinned private stash refs and shared-entry retention;
* exact saved-clean-baseline verification before `restore_failed` retry;
* dirty temporary-context guard.

Run all tests.

Fix all failures.

Pay particular attention to destructive edge cases.

## Phase 6 — remove

Implement safe workspace removal, including config/discovery selection,
complete source/worktree/dirty/context preflight, durable `removing` progress,
deterministic `.removing/` tombstones, atomic rename/recovery, rerunnable
retries, `removal_complete` persistence, mutator blocking, operation locking,
external/internal lifecycle-lock ordering and manual recovery of both exact
stale locks, and refusal when any locked source is unavailable.

Run tests.

## Phase 7 — review

Review the complete implementation for:

* possible data loss;
* destructive Git commands;
* accidental source-repository mutation;
* unsafe stash handling;
* write-ahead transition records, stash-token recovery, and distinct restore
  failure phases;
* explicit user-certified conflict finalization and abandoned-conflict
  retention;
* atomic metadata and interrupted-phase behavior;
* operation-lock and manual stale-lock recovery behavior;
* external/internal lifecycle-lock ordering, lifetime, and manual recovery of
  both exact stale locks;
* strict names, source parsing, default resolution, and fixed project-root
  locations;
* per-repository default-ref validation, automatic-discovery gating, and
  lock-based status behavior;
* complete removal preflight and durable rerunnable progress;
* deterministic tombstone rename/deletion and crash recovery;
* stale worktree registrations;
* race-prone assumptions;
* unnecessary abstractions.

Simplify where appropriate.

Run:

```bash
uv run pytest
```

and all configured lint/type checks.

---

# 37. Safety constraints

Do not:

* assume `main`;
* assume every repo has the same default ref;
* assume the source repository itself is clean;
* change the source repository's checkout merely to create a workspace;
* reset existing branches;
* automatically delete user branches;
* use `git clean`;
* silently discard changes;
* blindly use the most recent stash;
* delete unrelated stashes;
* use `git worktree add --force`;
* force the same branch into multiple worktrees;
* automatically resolve merge/stash conflicts;
* finalize a restore while unmerged index entries remain;
* finalize `restore_failed` or automatically retry either restore failure
  state;
* finalize `restore_conflicted` without explicit user certification and no
  unmerged entries;
* reapply a retained stash onto a worktree that does not match the saved clean
  return baseline;
* discard resolved staged/unstaged changes or index state during finalize;
* reset or move a claimed return branch during context restoration;
* auto-break external lifecycle or internal operation locks;
* retry after a crash without manually verifying quiescence and removing both
  exact stale locks: the external lifecycle lock and the internal operation
  lock in the normal workspace or deterministic tombstone;
* recover an interrupted stash by stash-list position;
* delete or rewrite the shared `refs/stash` entry;
* delete a private tool ref except as the tool-owned cleanup step;
* mutate a workspace while durable `removing`/`removal_complete` state exists,
  except for a matching remove retry;
* credit worktree removal when either canonical path or exact Git registration
  still exists;
* scan for arbitrary removal remnants or use a non-deterministic tombstone;
* delete a removal tombstone before durable `removal_complete` and both
  absence checks;
* rename a workspace directory while any registered worktree remains;
* treat a bare source as exempt from workspace-root containment checks;
* create branches for context-only repositories;
* treat `context` as equivalent to `claim`;
* allow nested context switching in this first version;
* use GitPython;
* mock Git in integration tests;
* hard-code assumptions about any particular software project.

Prefer an explicit safe failure over clever destructive recovery.

---

# 38. Important invariants

Maintain and test these invariants.

### Invariant 1

A newly created workspace contains only detached repository worktrees.

### Invariant 2

`ws claim repo` modifies branch state only for that repo.

### Invariant 3

Plain claim starts from the workspace's immutable locked base commit.

### Invariant 4

`--source default` explicitly opts into the current state selected by the
repository's independently locked default selector.

### Invariant 5

`ws context` never creates a branch.

Its target is always detached.

### Invariant 6

Entering temporary context must never lose pre-existing working changes.

### Invariant 7

Restoring temporary context must never silently lose temporary or original changes.

### Invariant 8

The source checkouts configured by the project are not used as disposable working directories.

### Invariant 9

Different logical workspaces must remain independent.

### Invariant 10

Failure halfway through an operation should leave the system in either the old valid state or a clearly recoverable state.

### Invariant 11

The schema-versioned workspace lock contains absolute source paths and is
self-contained; moving or deleting `ws.toml` does not break workspace-local
commands.

### Invariant 12

Mutators are serialized by `.ws/operation.lock`, never auto-break a lock, and
only interrupted `entering`/`restoring` phases block automated mutation;
`active` permits restore, `restore_conflicted` permits only finalize, and
`restore_failed` permits only a user-requested safe restore retry.

### Invariant 13

Restore conflicts retain the recorded tool stash until there are no unmerged
entries and `--finalize-restore`; resolved staged/unstaged changes and index
state are preserved, and the shared stash entry is always retained with its
exact OID/message. A stash-apply failure without unmerged entries is
`restore_failed`, retains state/private snapshot, and is retryable only by an
explicit user `--restore` after the exact saved clean return baseline is
verified. Cleanup deletes only the private OID-pinned ref.

### Invariant 14

Removal never changes the workspace when any source/worktree identity,
registration, dirty-state, or context preflight check fails.

### Invariant 15

Every repository's default selector is locked independently of its creation
base/source ref and may be null; workspace-local `default` operations do not
depend on `ws.toml` and fail clearly only for a repository with a null selector.

### Invariant 16

A claimed context return branch must still exist, be available, and point to
its saved HEAD before restore; otherwise restore refuses without moving the
branch or deleting state/stash.

### Invariant 17

Source clones are published only below the project `repos/` root, and feature
workspaces are created only below the project `workspaces/` root.

### Invariant 18

Every Git side effect in context has a durable write-ahead intent and outcome
record; an unguessable stash token and recorded OID are used instead of stash
position, the shared `refs/stash` entry is always retained, and only the
private tool ref is deleted, with no automatic recovery.

### Invariant 19

Removal persists `removing` progress after full preflight and retries only
still-registered expected worktrees, preserving source repositories and
branches. It persists `removal_complete` before deleting metadata and blocks
all mutators except matching remove retries. The only recovery path is the
deterministic sibling `.removing/` tombstone.

### Invariant 20

When this worktree already has the exact requested target branch, claim is an
idempotent no-op and does not resolve or validate its supplied creation source.

### Invariant 21

`--finalize-restore` is explicit user acceptance when no unmerged entries
remain; the tool cannot distinguish manually resolved, aborted, or discarded
changes. An abort retains state/private snapshot because finalize is not run.

### Invariant 22

`restore_failed` never reapplies a stash until the exact saved return
branch/HEAD is present and the worktree has no unmerged entries and no
tracked, index, or untracked changes.

### Invariant 23

Create and remove acquire the same external
`<workspace-root>/.<workspace-name>.lifecycle.lock/` before any internal
operation lock; it survives internal metadata/tombstone deletion until
successful create/rollback or complete tombstone deletion. After a crash,
retry requires quiescence verification and manual removal of both exact stale
locks (external and internal, wherever the internal lock resides); neither is
auto-broken or adopted.

### Invariant 24

Removal completes all worktree deletion and registration verification while
the normal workspace path exists, persists `removal_complete`, then renames
only the metadata-only directory to the deterministic `.removing/` tombstone.

### Invariant 25

Workspace-local status reads the lock and state only and never re-resolves
configuration. Config-resolving read-only views report configured-ref errors
for affected repositories, continue independent repositories, never use
automatic discovery as a fallback for those errors, and retain a non-zero
aggregated result.

---

# 39. Final validation

Do not stop after implementing code.

Execute the real test suite.

Investigate failures and fix them.

At completion provide:

1. concise architecture summary;
2. repository/file layout;
3. complete CLI syntax implemented;
4. exact `claim` source semantics;
5. exact `context` lifecycle semantics;
6. `--finalize-restore`, phase, and manual recovery semantics;
7. `restore_conflicted` versus `restore_failed` behavior and write-ahead
   recovery records;
8. exact per-repository configured/auto-discovered default-ref selection, selector lock,
   config-deletion, and authoritative configured-ref error semantics;
9. explanation of OID-pinned stash safety, shared-entry retention, and
   return-branch identity checks;
10. fixed source/workspace roots, removal precedence, deterministic tombstone
     recovery, and durable removal progress;
11. external/internal lifecycle-lock ordering, lifetime, and manual crash
    recovery of both exact stale locks;
12. Git-helper private-ref and exact-registration behavior;
13. integration, concurrency, idempotency, stash-race, and tombstone
    failure-boundary tests;
14. actual pytest result;
15. lint/type-check results;
16. any remaining limitations;
17. recommended scope for the next phase, especially optional `uv` workspace integration.

The final implementation should prioritize correctness, reproducibility, transparent Git semantics, and protection against data loss over feature count.
