# Batch Ref Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add atomic, in-process rollback-capable `ws switch <ref> [repo ...]` and `ws merge <ref> [repo ...]` commands.

**Architecture:** `workspace.py` acquires the workspace lock and turns the selected, lock-file-ordered repository definitions into verified worktree targets. `ref_operations.py` owns target-local Git preflight, immutable snapshots, ordered mutation, and reverse-order rollback. `workspace.py` persists refreshed state only after a successful transaction; `cli.py` parses and dispatches only.

**Tech Stack:** Python, argparse, Git CLI through `ws_tool.git.run_git`, pytest, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-09-02-batch-ref-operations-design.md`

## Global Constraints

- Syntax is exactly `ws switch <ref> [repo ...]` and `ws merge <ref> [repo ...]`.
- Omitted names select every repository definition; supplied names select exactly those definitions, in lock-file order.
- Resolve the same ref independently in all targets and finish preflight before any mutation.
- Reject invalid/duplicate names, missing refs, invalid worktree identity, removal/context state, dirty/unmerged worktrees, and detached merge targets.
- Switch leaves targets detached. Merge requires an attached branch and uses Git's default merge behavior.
- Caught in-process failures restore every started target in reverse order and report rollback failures. For merge rollback, run `git merge --abort` with `check=False` before restoring each started target; its expected nonzero result when no merge is active must not stop restoration. Do not add durable interruption recovery.

---

## File structure

- `src/ws_tool/ref_operations.py` — target-local Git transaction, preflight, snapshots, mutation, and rollback logic.
- `src/ws_tool/workspace.py` — workspace-aware selection, validation, public wrappers, and state refresh.
- `src/ws_tool/cli.py` — command parser and dispatch.
- `tests/unit/test_ref_operations.py` — deterministic transaction tests.
- `tests/integration/test_workspace_ref_operations.py` — real Git workspace behavior.
- `tests/unit/test_cli.py` — parser and dispatch tests.
- `README.md` — user-facing usage and guarantees.

### Task 1: Add the Git transaction module

**Files:**
- Create: `src/ws_tool/ref_operations.py`
- Create: `tests/unit/test_ref_operations.py`

**Interfaces:**
- Consumes: `ws_tool.git.run_git`, `ws_tool.errors.WsError`, clean registered worktrees.
- Produces:

```python
Operation = Literal["switch", "merge"]

@dataclass(frozen=True)
class RefOperationTarget:
    name: str
    worktree: Path

@dataclass(frozen=True)
class RefOperationResult:
    name: str
    head: str
    branch: str | None

def execute_ref_operation(
    operation: Operation,
    target_ref: str,
    targets: Sequence[RefOperationTarget],
) -> tuple[RefOperationResult, ...]: ...
```

- [ ] **Step 1: Write failing unit tests**

Create `tests/unit/test_ref_operations.py` with a `FakeGit` callable that records `(cwd.name, tuple(args))`, returns `GitResult` values for clean status, symbolic refs, heads, and ref resolution, and raises `GitCommandError` when its `fail_on` command is reached. Define these fixture constants and helpers:

```python
APP_HEAD = "a" * 40
API_HEAD = "b" * 40
APP_TARGET = "c" * 40
API_TARGET = "d" * 40

def targets(tmp_path: Path) -> tuple[RefOperationTarget, RefOperationTarget]:
    return (
        RefOperationTarget("app", tmp_path / "app"),
        RefOperationTarget("api", tmp_path / "api"),
    )

def mutations(calls: list[tuple[str, tuple[str, ...]]]) -> list[tuple[str, tuple[str, ...]]]:
    return [call for call in calls if call[1][0] in {"switch", "merge"}]

def rollback_calls(calls: list[tuple[str, tuple[str, ...]]]) -> list[tuple[str, tuple[str, ...]]]:
    return [
        call
        for call in calls
        if call[1][0] in {"switch", "reset"} or call[1][:2] == ("merge", "--abort")
    ]
```

Write individually named tests for preflight-before-mutation, blank/duplicate target rejection, dirty status rejection, unmerged-entry rejection, in-progress-state rejection, missing-ref rejection, detached-merge rejection, successful switch results, failed second merge rollback, rollback-failure diagnostics, and detached snapshot rollback. In the preflight ordering test, assert all `status`, `ls-files`, `rev-parse HEAD`, `symbolic-ref`, and target-ref resolution calls occur before the first `switch` call. In every preflight-rejection test, assert `mutations(fake.calls) == []`.

Set `fake.fail_on = ("api", ("merge", API_TARGET))` before calling `execute_ref_operation("merge", "release", targets(tmp_path))`. Assert its raised `WsError` names both the `merge` failure and any injected `rollback` failure. The rollback-order test must assert:

```python
assert rollback_calls(calls) == [
    ("api", ("merge", "--abort")),
    ("api", ("switch", "feature/api")),
    ("api", ("reset", "--hard", API_HEAD)),
    ("app", ("merge", "--abort")),
    ("app", ("switch", "feature/app")),
    ("app", ("reset", "--hard", APP_HEAD)),
]
```

- [ ] **Step 2: Run the test file and verify failure**

Run: `pytest -q tests/unit/test_ref_operations.py`

Expected: collection failure because `ws_tool.ref_operations` does not exist.

- [ ] **Step 3: Implement the minimum transaction boundary**

Create the module with this snapshot:

```python
@dataclass(frozen=True)
class _Snapshot:
    target: RefOperationTarget
    original_head: str
    original_branch: str | None
    target_commit: str
```

For every target, before mutation, call `status --porcelain --untracked-files=all`, `ls-files --unmerged`, `rev-parse HEAD`, `symbolic-ref --quiet --short HEAD` (`check=False`), and `rev-parse --verify <ref>^{commit}`. Reject any nonempty porcelain output or unmerged entries. Also reject an existing Git operation marker returned by `git rev-parse --git-path` for `MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `REBASE_HEAD`, `BISECT_LOG`, `rebase-merge`, or `rebase-apply`; test `Path(marker).exists()` after each path lookup. Raise `WsError` messages beginning `preflight <repository>:` for every validation or Git failure, and require a symbolic branch for `merge`. Reject blank refs and duplicate target names before any Git call.

Build every snapshot before mutation. Add each snapshot to `started` before executing `switch --detach <target-commit>` or `merge <target-commit>`. On a caught `WsError`, `GitCommandError`, `OSError`, or `ValueError`, visit `reversed(started)`. For merges, run `merge --abort` with `check=False` and record a nonzero result as a rollback diagnostic but continue. Restore branch snapshots with `switch <branch>` then `reset --hard <original-head>`; restore detached snapshots with `switch --detach <original-head>`. Then verify the original branch (or detached state), `HEAD`, and empty porcelain output. Aggregate all rollback diagnostics in a final `WsError` that retains the primary `switch` or `merge` failure. Return fresh post-success `HEAD` and branch values in lock-file target order.

- [ ] **Step 4: Run the tests and verify success**

Run: `pytest -q tests/unit/test_ref_operations.py`

Expected: PASS.

- [ ] **Step 5: Commit the transaction module**

Run: `git add src/ws_tool/ref_operations.py tests/unit/test_ref_operations.py && git commit -m "feat: add transactional ref operations"`

### Task 2: Add workspace wrappers and state refresh

**Files:**
- Modify: `src/ws_tool/workspace.py` public command area and helpers near `_acquire_operation_lock()` at `1668-1687`
- Create: `tests/integration/test_workspace_ref_operations.py`

**Interfaces:**
- Consumes: Task 1's `RefOperationTarget` and `execute_ref_operation()`, plus `discover_workspace`, `_acquire_operation_lock`, `_read_metadata`, `_locked_repo`, `_read_live_repo`, `validate_worktree_registration`, and `write_workspace_state`.
- Produces:

```python
def switch_workspace(target_ref: str, repository_names: Sequence[str] = ()) -> None: ...
def merge_workspace(target_ref: str, repository_names: Sequence[str] = ()) -> None: ...
```

- [ ] **Step 1: Write failing integration tests**

Create `tests/integration/test_workspace_ref_operations.py`. Reuse `git`, `git_output`, `write_two_repo_config`, and `create_two_repo_workspace` from `tests/integration/test_context.py`; use source setup/hydration helpers from `tests/integration/test_phase2.py`. Import `ws_tool.workspace as workspace_module` and use `monkeypatch.chdir(app)` before calling its public wrappers directly; Task 3 owns CLI dispatch coverage. Define `two_repo_workspace_with_release_refs()` in this test file. It must create the workspace from each repository's initial commit, then call `commit()` in each adopted source clone and create its local `release` branch at that new commit; this makes `release` differ from the feature worktree's initial `HEAD`. Return `(workspace, app_worktree, api_worktree, app_release, api_release)`. Claim separate local branches before merge tests.

Include this explicit-only test and its default-all counterpart:

```python
def test_switch_explicit_repository_changes_only_selected_worktree(tmp_path, git_repo, monkeypatch) -> None:
    workspace, app, api, _app_release, api_release = two_repo_workspace_with_release_refs(tmp_path, git_repo)
    app_before = git_output(app, "rev-parse", "HEAD")
    monkeypatch.chdir(app)
    workspace_module.switch_workspace("release", ("api",))
    assert git_output(app, "rev-parse", "HEAD") == app_before
    assert git_output(api, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) == ""
    assert git_output(api, "rev-parse", "HEAD") == api_release
```

Add these tests, each using direct public-wrapper calls and the exact setup/assertions shown in this matrix:

| Test | Setup | Required assertions |
| --- | --- | --- |
| `test_switch_without_names_changes_all_worktrees` | Use `two_repo_workspace_with_release_refs()` and change directory to `app`. | Both worktrees have a detached `HEAD`; app equals `app_release`; api equals `api_release`. |
| `test_switch_rejects_unknown_or_duplicate_names_without_mutation` | Call `switch_workspace("release", ("missing",))`, then call it with `("api", "api")`. | Each call raises `WsError`; saved branch and `HEAD` for both worktrees are unchanged. |
| `test_switch_rejects_ref_missing_from_one_target_without_mutation` | Create `release` only in app's source clone. | `switch_workspace("release")` raises `WsError` naming api; neither worktree changes. |
| `test_switch_rejects_dirty_target_without_mutation` | Write `api/untracked.txt`. | `switch_workspace("release")` raises `WsError`; app is not switched and api remains on its saved `HEAD`. |
| `test_switch_rejects_unmerged_or_in_progress_target_without_mutation` | First create a real conflicted merge in api, assert `git ls-files --unmerged` is nonempty, abort it, then create `rebase-merge` below `Path(git_output(api, "rev-parse", "--git-dir"))`. | Both preflight calls raise `WsError`; app remains at its saved `HEAD`; remove `rebase-merge` in a `finally` block. |
| `test_merge_rejects_detached_target_without_mutation` | Leave both worktrees detached. | `merge_workspace("release")` raises `WsError`; neither `HEAD` changes. |
| `test_merge_fast_forwards_claimed_targets_and_refreshes_state` | Claim app and api, commit `release` descendants in both sources, then merge. | Each claimed branch fast-forwards to its release OID; deserialized selected `RepoState` fields equal `_read_live_repo()` output and `context is None`. |
| `test_merge_creates_non_fast_forward_commit` | Claim app, commit one worktree-only branch commit, create an unrelated `release` source commit from the original base, then merge app only. | `rev-list --parents -n 1 HEAD` contains three OIDs (merge commit plus two parents); api is unchanged. |
| `test_ref_operation_rejects_context_removal_and_operation_lock_states` | Enter context for app; restore it; separately write removal state using existing serialization helpers; separately create `workspace / ".ws" / "operation.lock"`. | Each state rejects the command with `WsError` before Git mutation; release the lock directory after the lock assertion. |
| `test_switch_second_target_failure_restores_all_started_targets` | Monkeypatch `ws_tool.ref_operations.run_git` to raise `GitCommandError` only for api's `("switch", "--detach", api_release)` call. | Both worktrees return to their saved branch/HEAD and empty `status --porcelain --untracked-files=all` output. |
| `test_merge_second_target_failure_restores_all_started_targets` | Claim both targets and monkeypatch the api `("merge", api_release)` call to raise `GitCommandError`. | Both claimed branches return to their saved heads, and both worktrees have empty porcelain status. |

For every rejection test, record both target `HEAD` values and symbolic branches before the call. For the explicit-subset state test, serialize state before the call and assert the unselected repository's serialized `RepoState` remains byte-for-byte unchanged. For rollback injection, delegate all other commands to the original `run_git`, so the first target performs a real mutation and real rollback.

- [ ] **Step 2: Run integration tests and verify failure**

Run: `pytest -q tests/integration/test_workspace_ref_operations.py`

Expected: FAIL because workspace wrappers do not exist.

- [ ] **Step 3: Implement workspace selection and persistence**

Implement one private wrapper:

```python
def _run_workspace_ref_operation(
    operation: Operation, target_ref: str, repository_names: Sequence[str],
) -> None:
    paths = discover_workspace()
    acquired = _acquire_operation_lock(paths)
    try:
        lock, state = _read_metadata(paths)
        _validate_discovered_workspace_name(paths, lock)
        targets = _select_ref_operation_targets(paths, lock, state, repository_names)
        execute_ref_operation(operation, target_ref, targets)
        write_workspace_state(paths.state, _refresh_ref_operation_state(state, targets))
    finally:
        if acquired:
            _release_operation_lock(paths.operation_lock)
```

Use it for `switch_workspace` and `merge_workspace`. `_select_ref_operation_targets()` must use every `lock.repos` key for an empty sequence, otherwise reject duplicate/unknown names and return the requested subset in lock order. It must call `_reject_context_mutation_during_removal`, reject selected entries with non-`None` context, validate each selected worktree through `_locked_repo` and `validate_worktree_registration`, and return `RefOperationTarget(name, paths.workspace / "repos" / name)`.

`_refresh_ref_operation_state()` must call `_read_live_repo()` for every selected target and use `dataclasses.replace()` to replace only its `RepoState.mode`, `head`, `branch`, `detached`, `dirty`, and `context=None`; `name` and every unselected `RepoState` remain unchanged. Do not write state before a successful transaction, so a fully recovered failure leaves the prior state file authoritative.

- [ ] **Step 4: Run integration tests and verify success**

Run: `pytest -q tests/integration/test_workspace_ref_operations.py`

Expected: PASS.

- [ ] **Step 5: Commit the workspace façade**

Run: `git add src/ws_tool/workspace.py tests/integration/test_workspace_ref_operations.py && git commit -m "feat: add workspace ref command façade"`

### Task 3: Expose commands and document the contract

**Files:**
- Modify: `src/ws_tool/cli.py:8-19,22-55,58-113`
- Modify: `tests/unit/test_cli.py`
- Modify: `README.md` command-usage section

**Interfaces:**
- Consumes: `switch_workspace(target_ref, repository_names=())` and `merge_workspace(target_ref, repository_names=())`.
- Produces: exit code `0` on success and existing `WsError` exit code `1` on failure.

- [ ] **Step 1: Write failing parser and dispatch tests**

Extend `tests/unit/test_cli.py` using monkeypatched workspace entry points. Update `test_help_dispatch_is_available()` to assert that help contains both `ws switch` and `ws merge`, then add:

```python
def test_switch_dispatches_ref_and_explicit_repositories(monkeypatch, capsys) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(cli, "switch_workspace", lambda ref, repository_names=(): seen.update(ref=ref, repositories=repository_names))
    assert cli.main(["switch", "release", "api", "web"]) == 0
    assert seen == {"ref": "release", "repositories": ["api", "web"]}
    assert "Switched" in capsys.readouterr().out

def test_merge_without_names_dispatches_all_repositories(monkeypatch, capsys) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(cli, "merge_workspace", lambda ref, repository_names=(): seen.update(ref=ref, repositories=repository_names))
    assert cli.main(["merge", "origin/release"]) == 0
    assert seen == {"ref": "origin/release", "repositories": []}
    assert "Merged" in capsys.readouterr().out
```

Also add these required-ref assertions:

```python
def test_switch_requires_ref() -> None:
    assert cli.main(["switch"]) == 2

def test_merge_requires_ref() -> None:
    assert cli.main(["merge"]) == 2
```

- [ ] **Step 2: Run CLI tests and verify failure**

Run: `pytest -q tests/unit/test_cli.py`

Expected: FAIL because the new functions are not yet imported or dispatched.

- [ ] **Step 3: Implement CLI and README changes**

Import both wrappers. Add parsers and dispatch branches exactly as follows, before the existing fallback branch:

```python
switch = commands.add_parser("switch", help="switch repositories to a ref")
switch.add_argument("ref")
switch.add_argument("repos", nargs="*")

merge = commands.add_parser("merge", help="merge a ref into repositories")
merge.add_argument("ref")
merge.add_argument("repos", nargs="*")

if args.command == "switch":
    switch_workspace(args.ref, args.repos)
    print(f"Switched target repositories to {args.ref}")
    return 0
if args.command == "merge":
    merge_workspace(args.ref, args.repos)
    print(f"Merged {args.ref} into target repositories")
    return 0
```

Add this README subsection after **Claim arbitrary source into arbitrary target**:

````markdown
### Batch ref operations

```bash
ws switch release
ws switch release api
ws merge origin/main
ws merge origin/main api web
```

`switch` checks out the resolved ref commit detached. `merge` merges the resolved ref commit into each selected repository's attached branch. Omitting repository names selects every configured repository; supplying names selects only those definitions in workspace lock-file order.

All selected repositories complete preflight before any Git mutation. Dirty, conflicted, in-progress, context, removal, invalid-worktree, missing-ref, duplicate-name, and detached-merge targets are rejected. A caught in-process failure rolls back started repositories; interruption or process termination may require manual repair.
````

- [ ] **Step 4: Run CLI checks and verify success**

Run: `pytest -q tests/unit/test_cli.py && ruff check src/ws_tool/cli.py tests/unit/test_cli.py`

Expected: PASS.

- [ ] **Step 5: Commit CLI and documentation**

Run: `git add src/ws_tool/cli.py tests/unit/test_cli.py README.md && git commit -m "feat: expose batch ref commands"`

### Task 4: Verify the full feature

**Files:**
- Verify only: all files from Tasks 1–3.

**Interfaces:**
- Consumes: every interface above.
- Produces: evidence that the feature meets the approved spec without lifecycle regressions.

- [ ] **Step 1: Run focused tests**

Run: `pytest -q tests/unit/test_ref_operations.py tests/unit/test_cli.py tests/integration/test_workspace_ref_operations.py`

Expected: PASS.

- [ ] **Step 2: Run adjacent lifecycle tests**

Run: `pytest -q tests/integration/test_claim.py tests/integration/test_context.py`

Expected: PASS.

- [ ] **Step 3: Run repository gates**

Run: `pytest -q && ruff check src tests && mypy src tests`

Expected: all commands exit `0`.

- [ ] **Step 4: Inspect final scope**

Run: `git diff --check && git status --short && git diff -- src/ws_tool/ref_operations.py src/ws_tool/workspace.py src/ws_tool/cli.py tests README.md`

Expected: no whitespace errors, no durable-journal implementation, and no unrelated lifecycle refactor.

- [ ] **Step 5: Commit a validation correction only when needed**

Run only if validation changed files: `git add src tests README.md && git commit -m "test: verify batch ref operations"`. Do not create an empty commit.
