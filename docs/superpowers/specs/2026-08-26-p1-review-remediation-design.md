# P1 review remediation design

## Scope

Address only the four confirmed P1 findings in `reviews/code-review.md`:

1. make workspace removal recoverable after a real crash during tombstone cleanup;
2. make temporary context state independent per repository;
3. detect remote symbolic-HEAD disagreement by resolved commit;
4. add the required real crash and concurrency integration coverage.

The review's P2/P3 findings and optional refactors are explicitly out of scope.

## Durable removal seal

Add a versioned `removal-seal.toml` to the workspace before it is renamed to its
deterministic tombstone. The seal contains immutable facts needed to finish and
verify cleanup without `.ws/state.toml` or `workspace.lock.toml`: workspace
name, canonical normal and tombstone paths, the fixed `removal_complete` phase,
and each repository's source path, worktree path, and Git admin identity.

After durable `removal_complete` is persisted and all worktree paths and exact
Git registrations are absent, atomically write and fsync the seal. Rename the
workspace only afterwards and fsync the workspace-root directory.

Tombstone cleanup validates the seal and deletes only an ordered allowlist of
regular metadata files and known-empty directories. It rejects symlinks and
unexpected entries. The seal remains until every other permitted entry is gone;
then the seal is deleted, the empty tombstone is removed with `rmdir`, and the
parent directory is fsynced. Recursive deletion and exception-time metadata
reconstruction are removed.

A partial tombstone without either valid legacy metadata or a valid seal fails
closed for manual recovery. A complete legacy tombstone may be sealed on retry;
an already partially deleted legacy tombstone is not inferred from process
memory. Creation continues to reject every exact tombstone, including seal-only
or empty terminal remnants.

The final unlink/rmdir boundary cannot portably retain an operation-lock file at
every instruction while the tombstone is deleted. During that terminal window,
the external lifecycle lock and durable seal are authoritative.

## Repository-local context state

Retain `WorkspaceState.phase` only for workspace lifecycle:
`idle`, `removing`, and `removal_complete`. Context transition state lives only
at `RepoState.context.phase`.

Claiming, entering context, restoring context, and finalizing a restore inspect
and change only the selected repository's context. They merge that repository
back into the latest lock-held workspace state, preserving all other repository
records. Removal still rejects the workspace when any repository has a context.

Bump workspace-state serialization to v2. A v1 state is read by preserving any
removal lifecycle phase and otherwise mapping its global phase to `idle`; each
repository context is authoritative even if it disagrees with the old global
phase. Mutations write v2; status reads do not rewrite state. Validation enforces
that a workspace without removal is `idle`, a workspace with removal has the
matching removal phase, and removal cannot coexist with any repository context.

## Remote symbolic HEAD resolution

For every remote symbolic HEAD, obtain both its symbolic target and its resolved
commit object ID using machine-readable Git output. Require a commit target,
then compare all resolved object IDs rather than branch-name suffixes. Multiple
object IDs are a disagreement; one object ID agrees even when target branch
names differ. Choose a deterministic target selector when they agree.

Configured defaults retain precedence. With an explicit creation source, remote
disagreement retains the existing null locked-default-selector fallback; without
an explicit source, creation fails.

## Tests and verification

Add regression tests for two-repository contexts, including independent active
and interrupted states, restoring one repository while another stays active,
claiming an unaffected repository, and removal rejection for any active context.
Add v2 round-trip and v1 migration coverage.

Add remote tests for same-named branches at different commits, differently named
branches at the same commit, deterministic selection, invalid symbolic targets,
and explicit-source disagreement.

Run CLI integration cases in child processes and terminate them with `SIGKILL`
after real removal boundaries: durable `removing`, worktree deletion, durable
`removal_complete`, rename, and one tombstone-metadata deletion with the seal
present. Retry through the normal CLI and verify source repositories, exact Git
registrations, workspace paths, tombstones, and expected stale lock/seal state.

Use deterministic subprocess barriers for concurrent create and mutator cases;
the competing process must fail safely without mutation while the first process
holds its lock. Avoid timing-only races and production-only fault-injection
switches.

## Delivery order

1. Add failing context and remote regression tests.
2. Implement v2 state serialization and repository-local context workflows.
3. Implement commit-based remote symbolic-HEAD comparison.
4. Add seal serialization and validation.
5. Replace recursive cleanup with sealed, ordered cleanup and directory fsyncs.
6. Add real crash and concurrent-process integration coverage.
7. Run the complete project validation suite.
