# P1 Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the P1-specified workspace safety guarantees for removal recovery, repository-local context, remote default selection, and process-level integration coverage.

**Architecture:** Context transitions become repository-local while `WorkspaceState.phase` describes only workspace removal. Remote symbolic heads are compared by resolved commit OID. Removal gains a typed terminal `RemovalSeal` persisted both in state and as a standalone file so ordered, allowlisted cleanup can resume after crash without reconstructing deleted metadata.

**Tech Stack:** Python 3.12+, dataclasses, TOML serialization, filesystem `fsync`, Git CLI, pytest, Ruff, mypy, uv.

**Spec:** `docs/superpowers/specs/2026-08-26-p1-review-remediation-design.md`

## Global Constraints

- Do not implement review P2/P3 issues or optional refactors.
- Do not add third-party dependencies.
- Fail closed on malformed, incomplete, symlinked, or unexpected tombstone contents.
- Keep operations serialized by the existing locks; the lifecycle lock remains authoritative through sealed tombstone cleanup.
- Use real Git repositories, subprocesses, and `SIGKILL` for durability and concurrency integration tests; do not use timing-only races.
- Preserve backward readability of v1 workspace state; mutations serialize v2.
- Do not use recursive tombstone deletion or exception-time metadata reconstruction.

---

## File structure

- `src/ws_tool/models.py` — state schema version and immutable seal data models.
- `src/ws_tool/serialization.py` — v1-to-v2 state migration, embedded seal state serialization, standalone seal reads/writes, and seal validation.
- `src/ws_tool/git.py` — machine-oriented symbolic remote-HEAD discovery with resolved commit IDs.
- `src/ws_tool/workspace.py` — repository-local context behavior, OID default selection, seal lifecycle, ordered cleanup, and narrow crash-test seam.
- `tests/unit/test_serialization.py` — v2 and v1-migration validation tests.
- `tests/unit/test_git.py` — Git symbolic-head helper tests.
- `tests/integration/test_context.py` — two-repository context behavior and recovery tests.
- `tests/integration/test_phase2.py` — remote symbolic-HEAD behavior through workspace creation.
- `tests/integration/test_remove.py` — legacy removal regression coverage retained/updated.
- `tests/integration/test_remove_crash.py` — real child-process `SIGKILL` removal-boundary recovery tests.
- `tests/integration/test_remove_concurrency.py` — deterministic competing-process removal/mutator tests.
- `tests/conftest.py` — only shared subprocess/barrier fixture support required by the new integration tests.

### Task 1: Migrate workspace state to a removal-only global phase

**Files:**
- Modify: `src/ws_tool/models.py:6-13,54-101`
- Modify: `src/ws_tool/serialization.py:187-256,367-499`
- Modify: `tests/unit/test_serialization.py`

**Interfaces:**
- Produces: `WORKSPACE_STATE_SCHEMA_VERSION = 2` and `deserialize_workspace_state(text: str) -> WorkspaceState` that accepts v1 and emits the v2 in-memory invariant.
- Produces: v2 invariant: `WorkspaceState.phase` is `idle`, `removing`, or `removal_complete`; all context phases live at `RepoState.context.phase`.
- Consumed by: context workflow and sealed-removal tasks.

- [ ] **Step 1: Add failing schema and migration tests**

Add explicit fixtures for a v1 global context phase, multiple repository contexts, and removal/context contradiction. Assert that v1 context phases map to top-level `idle` without losing repository context and that v2 serialization records schema version 2.

```python
def test_deserialize_v1_context_phase_preserves_repo_context() -> None:
    state = deserialize_workspace_state(V1_ACTIVE_CONTEXT_TOML)

    assert state.schema_version == 2
    assert state.phase == "idle"
    assert state.repos["app"].context is not None
    assert state.repos["app"].context.phase == "active"


def test_v2_rejects_removal_with_any_repository_context() -> None:
    with pytest.raises(ValidationError, match="removal.*context"):
        deserialize_workspace_state(V2_REMOVAL_WITH_CONTEXT_TOML)
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/unit/test_serialization.py -q`

Expected: FAIL because schema v1 is still current and global context phases are accepted.

- [ ] **Step 3: Define schema v2 and migration at the deserialization boundary**

In `models.py`, retain `WorkspaceState.phase` for removal lifecycle but change the exported workspace phase set. In `serialization.py`, migrate raw v1 TOML before model construction and validate only v2 invariants.

```python
WORKSPACE_STATE_SCHEMA_VERSION = 2
WORKSPACE_PHASES = frozenset({"idle", "removing", "removal_complete"})


def _migrate_workspace_state_v1(raw: dict[str, object]) -> dict[str, object]:
    migrated = copy.deepcopy(raw)
    state = _required_table(migrated, "state")
    state["schema_version"] = WORKSPACE_STATE_SCHEMA_VERSION
    if state["phase"] in CONTEXT_PHASES:
        state["phase"] = "idle"
    return migrated
```

Preserve each serialized repository `context` block untouched. Reject unknown schema versions and reject v2 removal plus any repository context. Do not rewrite metadata during a read-only status operation.

- [ ] **Step 4: Run serialization tests to verify they pass**

Run: `uv run pytest tests/unit/test_serialization.py -q`

Expected: PASS, including v1 migration, v2 round-trip, multiple local contexts, and invalid mixed state coverage.

- [ ] **Step 5: Commit the schema migration**

```bash
git add src/ws_tool/models.py src/ws_tool/serialization.py tests/unit/test_serialization.py
git commit -m "feat: migrate workspace state to repository-local contexts"
```

### Task 2: Make context workflows repository-local

**Files:**
- Modify: `src/ws_tool/workspace.py:247-274,786-1261,1302-1327,1739-1749`
- Modify: `tests/integration/test_context.py:61-126` and related context assertions

**Interfaces:**
- Consumes: schema-v2 invariant from Task 1.
- Produces: `_set_context_state(state, repository_name, saved, context, *, mode, head, branch, dirty) -> WorkspaceState` that preserves `WorkspaceState.phase` and all other repository records.
- Produces: `claim_workspace`, `enter_context`, `restore_context`, and `finalize_restore` guards based only on `state.repos[repository_name].context`.

- [ ] **Step 1: Add failing two-repository integration tests**

Use a config with repositories `app` and `api`. Cover concurrent independent contexts, restoring one while retaining the other, and claim behavior scoped to the selected repository.

```python
def test_context_is_independent_per_repository(two_repo_workspace: Workspace) -> None:
    run_ws("context", "enter", "app", cwd=two_repo_workspace.root)
    run_ws("context", "enter", "api", cwd=two_repo_workspace.root)

    run_ws("context", "restore", "app", cwd=two_repo_workspace.root)
    status = run_ws("status", "--json", cwd=two_repo_workspace.root).json()

    assert status["repositories"]["app"]["context"] is None
    assert status["repositories"]["api"]["context"]["phase"] == "active"
```

Also assert claim of `app` succeeds while `api` is active, claim of `api` fails, an interrupted `app` context leaves `api` operable, and removal refuses while either repository has context.

- [ ] **Step 2: Run context integration tests to verify they fail**

Run: `uv run pytest tests/integration/test_context.py -q`

Expected: FAIL because global `state.phase` blocks the second repository and restoring one context sets the workspace idle.

- [ ] **Step 3: Remove global context reads and writes from context workflows**

Replace every guard based on `state.phase in CONTEXT_PHASES` with a selected-repository context check. Update the state builder to replace only one repository record.

```python
def _set_context_state(
    state: WorkspaceState,
    repository_name: str,
    saved: RepoState,
    context: ContextState | None,
    *,
    mode: str,
    head: str | None,
    branch: str | None,
    dirty: bool,
) -> WorkspaceState:
    updated_repo = replace(
        saved,
        context=context,
        mode=mode,
        head=head,
        branch=branch,
        dirty=dirty,
    )
    return replace(state, repos={**state.repos, repository_name: updated_repo})
```

Reload the latest lock-held metadata before persisting each context effect; update only its selected repository. Keep removal's `any(repo.context is not None for repo in state.repos.values())` preflight guard.

- [ ] **Step 4: Run context tests to verify they pass**

Run: `uv run pytest tests/integration/test_context.py -q`

Expected: PASS, including all existing one-repository flows and the new two-repository cases.

- [ ] **Step 5: Commit repository-local context behavior**

```bash
git add src/ws_tool/workspace.py tests/integration/test_context.py
git commit -m "fix: scope workspace contexts per repository"
```

### Task 3: Compare remote symbolic HEADs by commit OID

**Files:**
- Modify: `src/ws_tool/git.py:53-90`
- Modify: `src/ws_tool/workspace.py:1615-1642`
- Modify: `tests/unit/test_git.py`
- Modify: `tests/integration/test_phase2.py:260-303`

**Interfaces:**
- Produces: `remote_symbolic_heads(source: Path) -> Sequence[RemoteSymbolicHead]`, where `RemoteSymbolicHead = tuple[str, str, str]` is `(head_ref, target_ref, commit_oid)`.
- Consumes: `run_git` and current source-repository validation.
- Produces: `_remote_symbolic_selector(source: Path) -> str | None` that returns a deterministic full target ref or raises `ConfigError` for distinct/unresolvable remote heads.

- [ ] **Step 1: Add failing unit and integration cases**

Write a unit test around real `refs/remotes/*/HEAD` output and integration cases that construct two remotes.

```python
def test_remote_symbolic_heads_resolve_target_commits(repo: Path) -> None:
    heads = remote_symbolic_heads(repo)
    assert heads == (
        ("refs/remotes/origin/HEAD", "refs/remotes/origin/main", ORIGIN_MAIN_OID),
    )


def test_create_rejects_same_named_remote_heads_at_different_commits(
    tmp_path: Path, git_repo, bare_git_repo
) -> None:
    result = run_ws("create", "sample", cwd=tmp_path, check=False)
    assert result.returncode != 0
    assert "remote symbolic HEADs disagree" in result.stderr
```

Add success coverage for different target names at the same OID, lexicographically deterministic target selection, invalid target failure, and explicit-source disagreement preserving a null selector.

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `uv run pytest tests/unit/test_git.py tests/integration/test_phase2.py -q`

Expected: FAIL because `_remote_symbolic_selector()` compares branch suffixes rather than resolved object IDs.

- [ ] **Step 3: Add the Git helper and OID-based selector**

Use machine-readable ref enumeration, resolve every symbolic target as a commit, and sort targets only after asserting all OIDs agree.

```python
RemoteSymbolicHead = tuple[str, str, str]


def remote_symbolic_heads(source: Path) -> Sequence[RemoteSymbolicHead]:
    rows = run_git(
        [
        "for-each-ref",
        "--format=%(refname)%(tab)%(symref)",
        "refs/remotes",
        ],
        cwd=source,
    ).stdout.splitlines()
    heads: list[RemoteSymbolicHead] = []
    for row in rows:
        head_ref, target_ref = row.split("\t", 1)
        if head_ref.endswith("/HEAD") and target_ref:
            result = run_git(
                ["rev-parse", "--verify", f"{target_ref}^{{commit}}"],
                cwd=source,
            )
            heads.append((head_ref, target_ref, result.stdout.strip()))
    return tuple(heads)


def _remote_symbolic_selector(source: Path) -> str | None:
    heads = remote_symbolic_heads(source)
    if not heads:
        return None
    if len({commit for _, _, commit in heads}) != 1:
        raise ConfigError("remote symbolic HEADs disagree")
    return min(target for _, target, _ in heads)
```

Convert Git command failures and malformed output to the project's existing configuration/error type with a clear message.

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `uv run pytest tests/unit/test_git.py tests/integration/test_phase2.py -q`

Expected: PASS with OID disagreement rejected and same-OID agreement accepted.

- [ ] **Step 5: Commit remote selection behavior**

```bash
git add src/ws_tool/git.py src/ws_tool/workspace.py tests/unit/test_git.py tests/integration/test_phase2.py
git commit -m "fix: compare remote symbolic heads by commit"
```

### Task 4: Add typed embedded and standalone removal seals

**Files:**
- Modify: `src/ws_tool/models.py:6-13,79-101`
- Modify: `src/ws_tool/serialization.py:71-129,187-256`
- Modify: `src/ws_tool/workspace.py:476-535,1568-1571`
- Modify: `tests/unit/test_serialization.py`

**Interfaces:**
- Produces: `RemovalSealRepo` and `RemovalSeal` immutable models; `RemovalState.seal: RemovalSeal | None`.
- Produces: `serialize_removal_seal(seal) -> str`, `deserialize_removal_seal(text) -> RemovalSeal`, and `write_removal_seal(path, seal) -> None`.
- Produces: `WorkspacePaths.removal_seal: Path` at `workspace_path / "removal-seal.toml"` and at the matching tombstone path.
- Consumed by: sealed removal completion and recovery in Tasks 5–7.

- [ ] **Step 1: Add failing seal serialization tests**

Test independent seal schema versioning, full required identity fields, omitted nullable fields, malformed seal rejection, and structural embedded/standalone equality.

```python
def test_removal_seal_round_trips_without_state_or_lock_digest() -> None:
    seal = RemovalSeal(
        workspace_name="sample",
        workspace_path=Path("/tmp/sample"),
        tombstone_path=Path("/tmp/.sample.removing"),
        repos={
            "app": RemovalSealRepo(
                name="app",
                source_path=Path("/tmp/source-app"),
                worktree_path=Path("/tmp/sample/repos/app"),
                git_admin_path=Path("/tmp/source-app/.git/worktrees/app"),
                base_ref="origin/main",
                base_commit="0" * 40,
                default_selector="origin/main",
                mode="detached",
                head="0" * 40,
                branch=None,
                detached=True,
                dirty=False,
            )
        },
    )
    assert deserialize_removal_seal(serialize_removal_seal(seal)) == seal
```

- [ ] **Step 2: Run the unit tests to verify they fail**

Run: `uv run pytest tests/unit/test_serialization.py -q`

Expected: FAIL because no seal model or standalone serialization API exists.

- [ ] **Step 3: Implement seal models and serializers**

Define the independent seal schema and disallow mutable cleanup progress or self-referential digests. The seal contains terminal facts: workspace name and paths; `removal_complete`; and, for each repository, source/worktree/Git-admin identities plus copied ref and worktree state fields. Do not serialize a nested `WorkspaceState`, `RemovalState`, or hash.

```python
REMOVAL_SEAL_SCHEMA_VERSION = 1

@dataclass(frozen=True)
class RemovalSeal:
    workspace_name: str
    workspace_path: Path
    tombstone_path: Path
    repos: dict[str, RemovalSealRepo]
    phase: str = "removal_complete"
    schema_version: int = REMOVAL_SEAL_SCHEMA_VERSION


def write_removal_seal(path: Path, seal: RemovalSeal) -> None:
    _write_atomic_text(path, serialize_removal_seal(seal))
```

Use the existing atomic writer and directory `fsync`; expose its directory-sync helper for the removal code instead of duplicating `os.open`/`os.fsync`. Serialize the matching embedded seal in a v2 `RemovalState` only when phase is `removal_complete`. Accept legacy v1 completed removal without a seal only while lock/state are still readable, so it can be upgraded on retry.

- [ ] **Step 4: Run seal unit tests to verify they pass**

Run: `uv run pytest tests/unit/test_serialization.py -q`

Expected: PASS for seal round-trip, validation failures, and legacy completed-removal compatibility.

- [ ] **Step 5: Commit seal persistence primitives**

```bash
git add src/ws_tool/models.py src/ws_tool/serialization.py src/ws_tool/workspace.py tests/unit/test_serialization.py
git commit -m "feat: add durable removal seals"
```

### Task 5: Replace recursive tombstone deletion with sealed cleanup

**Files:**
- Modify: `src/ws_tool/workspace.py:322-435,476-737,1568-1571,1721-1734`
- Modify: `tests/integration/test_remove.py`

**Interfaces:**
- Consumes: Task 4's `RemovalSeal`, `write_removal_seal`, and `WorkspacePaths.removal_seal`.
- Produces: `_prepare_removal_seal`, `_read_removal_seal`, `_validate_seal_consistency`, `_classify_removal_tombstone`, and `_resume_sealed_tombstone_cleanup`.
- Produces: seal-only and empty deterministic tombstones that `remove_workspace` can safely classify before `_read_metadata()`.
- Produces test fixture `sealed_tombstone: tuple[Path, Path, RemovalSeal]` containing its tombstone, config directory, and valid standalone seal.

- [ ] **Step 1: Add failing sealed-cleanup integration tests**

Add tests that construct a complete sealed tombstone and valid partial shapes, then retry removal. Cover rejection of a symlink or unknown entry and upgrade of a complete legacy tombstone.

```python
def test_retry_removes_seal_only_tombstone(
    sealed_tombstone: tuple[Path, Path, RemovalSeal]
) -> None:
    tombstone, config_dir, seal = sealed_tombstone
    write_removal_seal(tombstone / "removal-seal.toml", seal)

    run_ws(config_dir, "remove", "sample", "--config", str(config_dir / "ws.toml"))

    assert not tombstone.exists()


def test_retry_refuses_unknown_sealed_tombstone_entry(
    sealed_tombstone: tuple[Path, Path, RemovalSeal]
) -> None:
    tombstone, config_dir, _seal = sealed_tombstone
    (tombstone / "unexpected").write_text("unsafe")
    result = run_ws(config_dir, "remove", "sample", "--config", str(config_dir / "ws.toml"))
    assert result.returncode != 0
    assert "unexpected tombstone entry" in result.stderr
```

- [ ] **Step 2: Run removal tests to verify they fail**

Run: `uv run pytest tests/integration/test_remove.py -q`

Expected: FAIL because removal always reads lock/state metadata and then uses `shutil.rmtree()`.

- [ ] **Step 3: Implement durable seal preparation and state classification**

After all repository worktrees and exact registrations are absent, construct the seal from `WorkspaceLock`, terminal `WorkspaceState.repos`, and `RemovalState`. Persist its embedded copy with `removal_complete`; then atomically write its standalone copy before renaming and fsync the workspace root after rename. Read the standalone seal first for tombstones that no longer have state or lock.

```python
def _prepare_removal_seal(
    paths: WorkspacePaths,
    lock: WorkspaceLock,
    state: WorkspaceState,
) -> tuple[WorkspaceState, RemovalSeal]:
    removal = _require_removal_complete(state.removal)
    seal = _build_removal_seal(lock, state, removal)
    sealed_state = replace(state, removal=replace(removal, seal=seal))
    write_workspace_state(paths.state, sealed_state)
    write_removal_seal(paths.removal_seal, seal)
    return sealed_state, seal
```

Validate equality when both embedded and standalone seals are present; reverify all source paths, normal worktree absence, and exact Git registration absence from the seal before cleanup.

- [ ] **Step 4: Implement ordered allowlisted cleanup**

Replace `_delete_removal_tombstone()` and delete `_restore_interrupted_tombstone()`. Classify the remaining filesystem shape monotonically and allow only regular `workspace.lock.toml`, `.ws/state.toml`, an empty `repos`, the held operation lock, empty `.ws`, controlled atomic-write temporary files, and `removal-seal.toml`. Reject symlinks and any other entry.

```python
def _resume_sealed_tombstone_cleanup(paths: WorkspacePaths, seal: RemovalSeal) -> None:
    _validate_tombstone_cleanup_shape(paths)
    _remove_if_present(paths.repos, require_empty_directory=True)
    _remove_if_present(paths.lock, require_regular_file=True)
    _remove_if_present(paths.state, require_regular_file=True)
    _remove_controlled_atomic_temps(paths)
    _release_and_remove_operation_lock(paths)
    _remove_if_present(paths.ws_dir, require_empty_directory=True)
    _fsync_directory(paths.tombstone)
    _remove_if_present(paths.removal_seal, require_regular_file=True)
    _fsync_directory(paths.tombstone)
    paths.tombstone.rmdir()
    _fsync_directory(paths.tombstone.parent)
```

Fsync each changed parent directory. Retain the lifecycle lock until cleanup succeeds. Permit the exact empty deterministic tombstone after a crash between seal unlink and `rmdir`; remove it only under that lifecycle lock.

- [ ] **Step 5: Run removal integration tests to verify they pass**

Run: `uv run pytest tests/integration/test_remove.py -q`

Expected: PASS, with successful retry from valid sealed shapes, safe refusal of malformed shapes, and no call to recursive deletion/reconstruction.

- [ ] **Step 6: Commit sealed tombstone cleanup**

```bash
git add src/ws_tool/workspace.py tests/integration/test_remove.py
git commit -m "fix: resume removal from durable tombstone seals"
```

### Task 6: Add real SIGKILL removal recovery coverage

**Files:**
- Modify: `src/ws_tool/workspace.py`
- Create: `tests/integration/test_remove_crash.py`
- Modify: `tests/conftest.py` only if a shared child-process launcher is necessary

**Interfaces:**
- Produces: `_removal_test_boundary(name: str) -> None`, a no-op production seam called only after actual durable/destructive boundaries.
- Consumes: the seal-only retry behavior from Task 5 and existing `run_ws`/Git helpers.
- Produces test helpers `run_remove_child(config_dir: Path, name: str, boundary: str) -> subprocess.CompletedProcess[str]`, `release_exact_stale_locks(workspace: Path) -> None`, and `retry_remove(config_dir: Path, name: str) -> subprocess.CompletedProcess[str]`.

- [ ] **Step 1: Add a child-process crash harness and failing cases**

The child imports `ws_tool.workspace`, replaces the no-op boundary function, and invokes the real CLI/operation. On the selected boundary it sends `SIGKILL` to itself; the parent observes real filesystem and Git state, removes only exact stale locks when required, then retries through the normal CLI.

```python
def _crash_at_boundary(target: str) -> None:
    if os.environ["WS_TEST_CRASH_BOUNDARY"] == target:
        os.kill(os.getpid(), signal.SIGKILL)


def test_remove_recovers_after_tombstone_metadata_deletion(
    config_dir: Path,
    workspace: Path,
    tombstone: Path,
) -> None:
    crashed = run_remove_child(config_dir, workspace.name, "legacy_metadata_deleted")
    assert crashed.returncode == -signal.SIGKILL
    assert (tombstone / "removal-seal.toml").is_file()
    release_exact_stale_locks(workspace)
    retried = retry_remove(config_dir, workspace.name)
    assert retried.returncode == 0, retried.stderr
    assert not workspace.exists()
    assert not tombstone.exists()
```

Cover `removing_persisted`, `worktree_removed`, `removal_complete_persisted`, `standalone_seal_persisted`, `tombstone_renamed`, deletion of lock/state metadata, and `seal_deleted` (empty terminal tombstone).

- [ ] **Step 2: Run crash tests to verify they fail**

Run: `uv run pytest tests/integration/test_remove_crash.py -q`

Expected: FAIL because no executable child boundary seam or sealed cleanup recovery exists.

- [ ] **Step 3: Add the narrow test seam at real boundaries**

Keep production behavior a no-op and call the seam only after persisted or destructive effects. The test bootstrap, not production configuration, installs the self-killing implementation.

```python
def _removal_test_boundary(_name: str) -> None:
    return None


# Example, immediately after write_removal_seal succeeds:
_removal_test_boundary("standalone_seal_persisted")
```

At each test, assert the expected normal/tombstone path, parseable authority (state or seal), source branches, absence of exact registrations, and final cleanup after retry.

- [ ] **Step 4: Run crash tests to verify they pass**

Run: `uv run pytest tests/integration/test_remove_crash.py -q`

Expected: PASS after real SIGKILL at every listed boundary.

- [ ] **Step 5: Commit crash-recovery coverage**

```bash
git add src/ws_tool/workspace.py tests/integration/test_remove_crash.py tests/conftest.py
git commit -m "test: cover removal recovery after process crashes"
```

### Task 7: Add deterministic real-process concurrency coverage

**Files:**
- Create: `tests/integration/test_remove_concurrency.py`
- Modify: `tests/conftest.py` only for reusable worker/barrier helpers
- Modify: `src/ws_tool/workspace.py` only to add a lifecycle-lock boundary call if Task 6 did not add one

**Interfaces:**
- Consumes: `_removal_test_boundary("remove.lifecycle_locked")`, `_creation_test_boundary("create.lifecycle_locked")`, and existing CLI process helpers.
- Produces: deterministic tests for competing lifecycle and workspace mutators without sleeps.
- Produces test helpers `start_paused_create(config_dir: Path, name: str, boundary: str) -> subprocess.Popen[str]`, `start_paused_remove(config_dir: Path, name: str, boundary: str) -> subprocess.Popen[str]`, `wait_for_boundary(process: subprocess.Popen[str]) -> None`, and `release_boundary(process: subprocess.Popen[str]) -> None`.

- [ ] **Step 1: Add failing competing-process tests**

First, have a `create sample` child pause only after holding the external lifecycle lock. Start a second `create sample`, assert it fails before creating metadata or Git worktrees, release the first, and assert it completes. Then repeat the protocol for remove and an independent mutator while removal owns its lifecycle and operation locks.

```python
def test_second_remove_fails_while_first_holds_lifecycle_lock(
    config_dir: Path,
) -> None:
    first = start_paused_remove(config_dir, "sample", "remove.lifecycle_locked")
    wait_for_boundary(first)

    second = run_ws(config_dir, "remove", "sample", "--config", str(config_dir / "ws.toml"))
    assert second.returncode != 0
    assert "lifecycle lock" in second.stderr

    release_boundary(first)
    assert first.wait() == 0
```

Add `test_second_create_fails_while_first_holds_lifecycle_lock` using `start_paused_create(config_dir, "sample", "create.lifecycle_locked")`. Add a third case where `claim` or `context` runs while removal owns the lifecycle and operation locks. Assert no Git or metadata mutation occurs in the rejected process and verify final worktree registrations and paths after the first completes.

- [ ] **Step 2: Run concurrency tests to verify they fail**

Run: `uv run pytest tests/integration/test_remove_concurrency.py -q`

Expected: FAIL because no deterministic subprocess barrier is installed.

- [ ] **Step 3: Implement test-only barrier plumbing**

Reuse the Task 6 boundary seam for removal and add the equivalent no-op `_creation_test_boundary(name: str) -> None` immediately after create has acquired the lifecycle lock. The test child writes a ready marker or IPC message after the named boundary and blocks until the parent releases it; do not use `sleep`. Keep the implementation entirely in test bootstrap/fixtures except for the no-op boundary calls in production code.

- [ ] **Step 4: Run concurrency tests to verify they pass**

Run: `uv run pytest tests/integration/test_remove_concurrency.py -q`

Expected: PASS with exactly one successful process, no unsafe second-process mutation, and final lock/worktree cleanup.

- [ ] **Step 5: Commit concurrency coverage**

```bash
git add src/ws_tool/workspace.py tests/integration/test_remove_concurrency.py tests/conftest.py
git commit -m "test: cover concurrent workspace mutations"
```

### Task 8: Run the full verification gate

**Files:**
- Modify only if a prior focused test exposes a defect; otherwise none.

**Interfaces:**
- Consumes: Tasks 1–7.
- Produces: evidence that the P1 remediation does not regress the existing CLI, configuration, serialization, Git, context, or removal behavior.

- [ ] **Step 1: Run focused P1 suites**

Run:

```bash
uv run pytest tests/unit/test_serialization.py tests/unit/test_git.py -q
uv run pytest tests/integration/test_context.py tests/integration/test_phase2.py -q
uv run pytest tests/integration/test_remove.py tests/integration/test_remove_crash.py tests/integration/test_remove_concurrency.py -q
```

Expected: PASS.

- [ ] **Step 2: Run static checks and the full suite**

Run:

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q
```

Expected: all commands exit 0.

- [ ] **Step 3: Inspect the final diff before commit**

Run:

```bash
git status --short
git diff --check
git diff -- src/ws_tool/models.py src/ws_tool/serialization.py src/ws_tool/git.py src/ws_tool/workspace.py
```

Expected: only P1 remediation files changed, no whitespace errors, and no P2/P3 scope expansion.

- [ ] **Step 4: Record any verification correction in its owning task**

If verification exposes a defect, return to the owning task, add a focused regression test, and make a separate commit with only that task's listed files. Otherwise, make no further commit.
