from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .config import load_config, parse_source_overrides
from .errors import ConfigError, GitWorktreeError, SerializationError, WsError
from .git import (
    create_private_ref,
    delete_private_ref,
    list_worktrees,
    repository_kind,
    run_git,
    validate_worktree_registration,
)
from .models import (
    CONTEXT_PHASES,
    REMOVAL_PHASES,
    ContextSideEffect,
    ContextState,
    RemovalRepoState,
    RemovalState,
    RepoState,
    WorkspaceLock,
    WorkspaceLockRepo,
    WorkspaceState,
)
from .serialization import (
    deserialize_workspace_lock,
    deserialize_workspace_state,
    validate_lock_state_consistency,
    validate_removal_repo_identity,
    write_workspace_lock,
    write_workspace_state,
)
from .validation import validate_logical_name


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    workspace: Path
    lock: Path
    state: Path
    tombstone: Path
    lifecycle_lock: Path
    operation_lock: Path


@dataclass(frozen=True)
class _CreationPlan:
    name: str
    source_path: Path
    base_ref: str
    base_commit: str
    default_selector: str | None


def create_workspace(
    workspace_name: str,
    *,
    config_path: str | Path | None = None,
    source_overrides: list[str] | tuple[str, ...] = (),
) -> WorkspacePaths:
    validate_logical_name(workspace_name, kind="workspace")
    project = load_config(Path("ws.toml") if config_path is None else config_path)
    overrides = parse_source_overrides(source_overrides, project.repos)
    paths = _paths(project.workspace_root, workspace_name)
    for repo_name in project.repos:
        _validate_worktree_branch(workspace_name, repo_name)

    root_was_present = paths.root.exists()
    paths.root.mkdir(parents=True, exist_ok=True)
    acquired = False
    created: list[tuple[Path, Path]] = []
    owned_metadata: list[Path] = []
    workspace_created = False
    retain_lifecycle_lock = False
    operation_acquired = False
    try:
        try:
            paths.lifecycle_lock.mkdir()
            acquired = True
        except FileExistsError as exc:
            raise WsError(
                f"workspace lifecycle lock already exists: {paths.lifecycle_lock}"
            ) from exc
        if paths.workspace.exists():
            raise WsError(f"workspace already exists: {paths.workspace}")
        if paths.tombstone.exists() or paths.tombstone.is_symlink():
            raise WsError(f"workspace removal tombstone already exists: {paths.tombstone}")

        plans = _resolve_creation_plans(project.repos, overrides)
        paths.workspace.mkdir()
        workspace_created = True
        repos_root = paths.workspace / "repos"
        repos_root.mkdir()
        paths.workspace.joinpath(".ws").mkdir()
        try:
            paths.operation_lock.mkdir()
            operation_acquired = True
        except FileExistsError as exc:
            raise WsError(
                f"workspace operation lock already exists: {paths.operation_lock}"
            ) from exc
        for plan in plans:
            worktree = repos_root / plan.name
            created.append((plan.source_path, worktree))
            run_git(
                ["worktree", "add", "--detach", worktree, plan.base_commit],
                cwd=plan.source_path,
            )

        lock = WorkspaceLock(
            workspace_name=workspace_name,
            repos={
                plan.name: WorkspaceLockRepo(
                    name=plan.name,
                    source_path=plan.source_path,
                    base_ref=plan.base_ref,
                    base_commit=plan.base_commit,
                    default_selector=plan.default_selector,
                )
                for plan in plans
            },
        )
        state = WorkspaceState(
            workspace_name=workspace_name,
            phase="idle",
            repos={
                plan.name: RepoState(
                    name=plan.name,
                    mode="detached",
                    head=plan.base_commit,
                    detached=True,
                    dirty=False,
                )
                for plan in plans
            },
        )
        write_workspace_lock(paths.lock, lock)
        owned_metadata.append(paths.lock)
        write_workspace_state(paths.state, state)
        owned_metadata.append(paths.state)
        return paths
    except KeyboardInterrupt as exc:
        try:
            cleanup_issues = _rollback_creation(
                paths,
                created,
                workspace_created,
                owned_metadata,
                operation_acquired=operation_acquired,
            )
        except KeyboardInterrupt as cleanup_error:
            retain_lifecycle_lock = True
            cleanup_error.add_note(
                f"Workspace creation failed with {exc}; cleanup was interrupted; retain "
                f"{paths.lifecycle_lock} and perform manual recovery before retrying."
            )
            raise
        except Exception as cleanup_error:
            retain_lifecycle_lock = True
            exc.add_note(
                f"Workspace creation cleanup failed unexpectedly: {cleanup_error}; "
                f"retain {paths.lifecycle_lock} and perform manual recovery before retrying."
            )
            raise exc from cleanup_error
        except BaseException as cleanup_error:
            retain_lifecycle_lock = True
            cleanup_error.add_note(
                f"Workspace creation cleanup was interrupted or failed; retain "
                f"{paths.lifecycle_lock} and perform manual recovery before retrying."
            )
            raise
        if cleanup_issues:
            retain_lifecycle_lock = True
            exc.add_note(
                "Workspace creation cleanup is incomplete; retain "
                f"{paths.lifecycle_lock} and perform manual recovery before retrying: "
                f"{'; '.join(cleanup_issues)}"
            )
        raise
    except Exception as exc:
        try:
            cleanup_issues = _rollback_creation(
                paths,
                created,
                workspace_created,
                owned_metadata,
                operation_acquired=operation_acquired,
            )
        except KeyboardInterrupt as cleanup_error:
            retain_lifecycle_lock = True
            cleanup_error.add_note(
                f"Workspace creation failed with {exc}; cleanup was interrupted; retain "
                f"{paths.lifecycle_lock} and perform manual recovery before retrying."
            )
            raise
        except Exception as cleanup_error:
            retain_lifecycle_lock = True
            raise WsError(
                f"workspace creation failed and cleanup failed unexpectedly: {exc}; "
                f"{cleanup_error}. Retain {paths.lifecycle_lock} and perform manual "
                "recovery before retrying."
            ) from exc
        except BaseException as cleanup_error:
            retain_lifecycle_lock = True
            cleanup_error.add_note(
                f"Workspace creation cleanup was interrupted or failed; retain "
                f"{paths.lifecycle_lock} and perform manual recovery before retrying."
            )
            raise
        if cleanup_issues:
            retain_lifecycle_lock = True
            details = "; ".join(cleanup_issues)
            raise WsError(
                f"workspace creation failed and cleanup is incomplete: {exc}; {details}. "
                f"Retain {paths.lifecycle_lock} and perform manual recovery before retrying."
            ) from exc
        if isinstance(exc, WsError):
            raise
        raise WsError(f"workspace creation failed: {exc}") from exc
    finally:
        if operation_acquired and not retain_lifecycle_lock:
            _release_operation_lock(paths.operation_lock)
        if acquired and not retain_lifecycle_lock:
            _release_lifecycle_lock(paths.lifecycle_lock)
        if not root_was_present and not retain_lifecycle_lock and paths.root.exists():
            try:
                paths.root.rmdir()
            except OSError:
                pass


def claim_workspace(
    repository_name: str,
    *,
    source: str | None = None,
    target: str | None = None,
) -> None:
    validate_logical_name(repository_name, kind="repository")
    paths = discover_workspace()
    lifecycle_acquired = False
    operation_acquired = False
    try:
        try:
            paths.lifecycle_lock.mkdir()
            lifecycle_acquired = True
        except FileExistsError as exc:
            raise WsError(
                f"workspace lifecycle lock already exists: {paths.lifecycle_lock}"
            ) from exc
        try:
            paths.operation_lock.mkdir()
            operation_acquired = True
        except FileExistsError as exc:
            raise WsError(
                f"workspace operation lock already exists: {paths.operation_lock}"
            ) from exc

        lock, state = _read_metadata(paths)
        _validate_discovered_workspace_name(paths, lock)
        if state.removal is not None or state.phase in REMOVAL_PHASES:
            raise WsError(
                f"cannot claim repository while workspace removal is durable: {paths.workspace}"
            )
        if state.phase in CONTEXT_PHASES:
            raise WsError(
                f"cannot claim repository while workspace context phase is {state.phase!r}"
            )
        locked = lock.repos.get(repository_name)
        if locked is None:
            raise WsError(
                f"repository {repository_name!r} is not part of workspace {lock.workspace_name!r}"
            )
        saved = state.repos[repository_name]
        if saved.context is not None or saved.mode == "context":
            raise WsError(f"repository {repository_name!r} has an active context; restore it first")

        worktree = paths.workspace / "repos" / repository_name
        validate_worktree_registration(locked.source_path, worktree)
        live = _read_live_repo(worktree)
        requested_target = (
            target if target is not None else f"ws/{lock.workspace_name}/{repository_name}"
        )
        _validate_claim_target(requested_target)

        current_branch = live["branch"]
        if current_branch == requested_target:
            _write_claimed_state(paths, state, repository_name, saved, live)
            return
        if current_branch is not None:
            raise WsError(
                f"repository {repository_name!r} is already claimed on branch {current_branch!r}"
            )

        existing = run_git(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{requested_target}"],
            cwd=worktree,
            check=False,
        )
        if existing.returncode == 0:
            raise WsError(f"claim target branch already exists: {requested_target}")
        if existing.returncode != 1:
            raise WsError(f"could not check whether claim target branch exists: {requested_target}")

        source_commit = _claim_source_commit(locked, source)
        run_git(["switch", "--create", requested_target, source_commit], cwd=worktree)
        claimed = _read_live_repo(worktree)
        _write_claimed_state(paths, state, repository_name, saved, claimed)
    finally:
        if operation_acquired:
            _release_operation_lock(paths.operation_lock)
        if lifecycle_acquired:
            _release_lifecycle_lock(paths.lifecycle_lock)


def remove_workspace(
    workspace_name: str,
    *,
    config_path: str | Path | None = None,
) -> None:
    validate_logical_name(workspace_name, kind="workspace")
    base_paths = _resolve_removal_paths(workspace_name, config_path)
    lifecycle_acquired = False
    operation_acquired = False
    location: WorkspacePaths | None = None
    retain_lifecycle_lock = False
    retain_locks = False
    deletion_attempted = False
    cleanup_attempted = False
    try:
        try:
            base_paths.lifecycle_lock.mkdir()
            lifecycle_acquired = True
        except FileExistsError as exc:
            raise WsError(
                f"workspace lifecycle lock already exists: {base_paths.lifecycle_lock}"
            ) from exc

        location = _existing_removal_paths(base_paths)
        if location.workspace != _disposable_paths(base_paths).workspace:
            try:
                location.operation_lock.mkdir()
                operation_acquired = True
            except FileExistsError as exc:
                raise WsError(
                    f"workspace operation lock already exists: {location.operation_lock}"
                ) from exc
        elif location.operation_lock.parent.exists():
            try:
                location.operation_lock.mkdir()
                operation_acquired = True
            except FileExistsError as exc:
                raise WsError(
                    f"workspace operation lock already exists: {location.operation_lock}"
                ) from exc

        if location.workspace == _disposable_paths(base_paths).workspace:
            cleanup_attempted = True
            _delete_disposable_tombstone(location)
            return

        lock, state = _read_metadata(location)
        resuming_removal = state.removal is not None
        if lock.workspace_name != workspace_name:
            raise WsError(
                f"workspace name in lock {lock.workspace_name!r} does not match requested "
                f"name {workspace_name!r}"
            )
        if lock.workspace_name != base_paths.workspace.name:
            raise WsError(
                f"workspace name in lock {lock.workspace_name!r} does not match path "
                f"{base_paths.workspace}"
            )
        if state.removal is not None:
            removal = state.removal
            if removal.workspace_path.resolve(strict=False) != base_paths.workspace.resolve(
                strict=False
            ):
                raise WsError("persisted removal workspace path does not match requested workspace")
            if removal.tombstone_path.resolve(strict=False) != base_paths.tombstone.resolve(
                strict=False
            ):
                raise WsError("persisted removal tombstone path does not match requested workspace")
            if location.workspace == base_paths.tombstone:
                if removal.phase != "removal_complete":
                    raise WsError("removal tombstone has an incomplete removal phase")
                _verify_removal_complete(lock, removal)
                cleanup_attempted = True
                _delete_removal_tombstone(location)
                return
            if removal.phase not in REMOVAL_PHASES:
                raise WsError(f"unknown persisted removal phase {removal.phase!r}")
            _preflight_removal_retry(lock, state, removal)
        else:
            if location.workspace != base_paths.workspace:
                raise WsError("removal tombstone is missing durable removal progress")
            removal = _preflight_removal(lock, state, location)
            state = replace(state, phase="removing", removal=removal)
            write_workspace_state(location.state, state)

        current_removal = state.removal
        assert current_removal is not None
        if location.workspace != base_paths.workspace:
            raise WsError("cannot remove worktrees from a removal tombstone")
        for repository_name, record in current_removal.repos.items():
            if record.complete:
                _verify_removed_record(lock, repository_name, record)
                continue
            deletion_attempted = True
            _remove_expected_worktree(
                lock,
                repository_name,
                record,
                allow_absent=resuming_removal,
            )
            current_removal = replace(
                current_removal,
                repos={
                    **current_removal.repos,
                    repository_name: replace(record, complete=True),
                },
            )
            state = replace(state, phase="removing", removal=current_removal)
            write_workspace_state(location.state, state)

        current_removal = replace(current_removal, phase="removal_complete")
        state = replace(state, phase="removal_complete", removal=current_removal)
        write_workspace_state(location.state, state)
        _rename_to_removal_tombstone(location, base_paths.tombstone)
        try:
            cleanup_attempted = True
            _delete_removal_tombstone(_tombstone_paths(base_paths))
        except Exception as exc:
            retain_lifecycle_lock = True
            raise WsError(
                f"removal completed but tombstone cleanup failed; manually recover "
                f"{base_paths.tombstone}: {exc}"
            ) from exc
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt) or deletion_attempted or cleanup_attempted:
            retain_locks = True
        raise
    finally:
        if (
            operation_acquired
            and location is not None
            and not (retain_lifecycle_lock or retain_locks)
        ):
            _release_operation_lock(location.operation_lock)
        if lifecycle_acquired and not (retain_lifecycle_lock or retain_locks):
            _release_lifecycle_lock(base_paths.lifecycle_lock)


def _resolve_removal_paths(
    workspace_name: str,
    config_path: str | Path | None,
) -> WorkspacePaths:
    discovered = _discover_removal_paths()
    if config_path is not None:
        project = load_config(config_path)
        configured = _paths(project.workspace_root, workspace_name)
        if discovered is not None and discovered.workspace != configured.workspace:
            raise WsError(
                f"explicit config resolves to {configured.workspace}, but discovered workspace "
                f"is {discovered.workspace}"
            )
        return configured
    if discovered is not None:
        if discovered.workspace.name != workspace_name:
            raise WsError(
                f"discovered workspace {discovered.workspace.name!r} does not match requested "
                f"name {workspace_name!r}"
            )
        return discovered
    project = load_config(Path("ws.toml"))
    return _paths(project.workspace_root, workspace_name)


def _discover_removal_paths() -> WorkspacePaths | None:
    current = Path.cwd().expanduser().resolve()
    for candidate in (current, *current.parents):
        if not (candidate / ".ws").is_dir():
            continue
        if candidate.name.startswith(".") and candidate.name.endswith(".removing"):
            workspace_name = candidate.name[1 : -len(".removing")]
            if workspace_name:
                return _paths(candidate.parent, workspace_name)
        return _paths(candidate.parent, candidate.name)
    return None


def _existing_removal_paths(base: WorkspacePaths) -> WorkspacePaths:
    normal = base.workspace.exists()
    tombstone = base.tombstone.exists() or base.tombstone.is_symlink()
    disposable = _disposable_paths(base).workspace
    disposable_exists = disposable.exists() or disposable.is_symlink()
    if sum((normal, tombstone, disposable_exists)) > 1:
        raise WsError(
            "multiple workspace removal paths exist; manual recovery required: "
            f"{base.workspace}, {base.tombstone}, {disposable}"
        )
    if normal:
        return base
    if tombstone:
        return _tombstone_paths(base)
    if disposable_exists:
        return _disposable_paths(base)
    raise WsError(f"workspace does not exist: {base.workspace}")


def _tombstone_paths(base: WorkspacePaths) -> WorkspacePaths:
    tombstone = base.tombstone
    return replace(
        base,
        workspace=tombstone,
        lock=tombstone / "workspace.lock.toml",
        state=tombstone / ".ws" / "state.toml",
        operation_lock=tombstone / ".ws" / "operation.lock",
    )


def _disposable_paths(base: WorkspacePaths) -> WorkspacePaths:
    disposable = base.tombstone.with_name(f"{base.tombstone.name}.deleting")
    return replace(
        base,
        workspace=disposable,
        lock=disposable / "workspace.lock.toml",
        state=disposable / ".ws" / "state.toml",
        operation_lock=disposable / ".ws" / "operation.lock",
    )


def _preflight_removal(
    lock: WorkspaceLock,
    state: WorkspaceState,
    paths: WorkspacePaths,
) -> RemovalState:
    issues: list[str] = []
    records: dict[str, RemovalRepoState] = {}
    for name, locked in lock.repos.items():
        worktree = paths.workspace / "repos" / name
        try:
            _check_removal_source(locked.source_path)
            identity = validate_worktree_registration(locked.source_path, worktree)
            saved = state.repos[name]
            if saved.context is not None:
                raise WsError("repository has an active temporary context")
            live = _read_live_repo(worktree)
            if live["dirty"]:
                raise WsError("worktree has uncommitted changes")
            records[name] = RemovalRepoState(
                name=name,
                worktree_path=worktree.resolve(strict=False),
                git_admin_path=identity.git_admin_path,
                complete=False,
            )
        except (GitWorktreeError, WsError) as exc:
            issues.append(f"{name}: {exc}")
    if issues:
        raise WsError("workspace removal preflight failed: " + "; ".join(issues))
    return RemovalState(
        phase="removing",
        workspace_path=paths.workspace.resolve(strict=False),
        tombstone_path=paths.tombstone.resolve(strict=False),
        repos=records,
    )


def _preflight_removal_retry(
    lock: WorkspaceLock,
    state: WorkspaceState,
    removal: RemovalState,
) -> None:
    issues: list[str] = []
    for name, record in removal.repos.items():
        locked = lock.repos[name]
        try:
            _check_removal_source(locked.source_path)
            if record.complete:
                _validate_expected_removal_path(removal, name, record)
                _verify_removed_record(lock, name, record)
                continue
            worktree = record.worktree_path
            _validate_expected_removal_path(removal, name, record)
            registered = [
                entry
                for entry in list_worktrees(locked.source_path)
                if entry.path == worktree.resolve(strict=False)
            ]
            if not worktree.exists() and not registered:
                continue
            if not worktree.exists():
                raise WsError("worktree path is absent but its Git registration remains")
            identity = validate_worktree_registration(locked.source_path, worktree)
            validate_removal_repo_identity(
                record,
                worktree_path=worktree,
                git_admin_path=identity.git_admin_path,
            )
            if _read_live_repo(worktree)["dirty"]:
                raise WsError("worktree has uncommitted changes")
            if state.repos[name].context is not None:
                raise WsError("repository has an active temporary context")
        except (GitWorktreeError, WsError, SerializationError) as exc:
            issues.append(f"{name}: {exc}")
    if issues:
        raise WsError("workspace removal retry preflight failed: " + "; ".join(issues))


def _check_removal_source(source: Path) -> None:
    if repository_kind(source) not in {"bare", "worktree"}:
        raise WsError(f"locked source is unavailable or is not a Git repository root: {source}")


def _verify_removal_complete(lock: WorkspaceLock, removal: RemovalState) -> None:
    for name, record in removal.repos.items():
        if not record.complete:
            raise WsError(f"removal tombstone has incomplete repository progress: {name}")
        _validate_expected_removal_path(removal, name, record)
        _verify_removed_record(lock, name, record)


def _validate_expected_removal_path(
    removal: RemovalState,
    repository_name: str,
    record: RemovalRepoState,
) -> None:
    expected = (removal.workspace_path / "repos" / repository_name).resolve(strict=False)
    actual = record.worktree_path.resolve(strict=False)
    if actual != expected:
        raise WsError(
            f"removal record for {repository_name!r} does not match its exact workspace path"
        )


def _verify_removed_record(
    lock: WorkspaceLock,
    repository_name: str,
    record: RemovalRepoState,
) -> None:
    source = lock.repos[repository_name].source_path
    expected = record.worktree_path.resolve(strict=False)
    registered = [entry for entry in list_worktrees(source) if entry.path == expected]
    if record.worktree_path.exists() or registered:
        raise WsError(
            f"completed removal record for {repository_name!r} does not match actual Git state"
        )


def _remove_expected_worktree(
    lock: WorkspaceLock,
    repository_name: str,
    record: RemovalRepoState,
    *,
    allow_absent: bool,
) -> None:
    source = lock.repos[repository_name].source_path
    worktree = record.worktree_path
    if not worktree.exists():
        if not allow_absent:
            raise WsError(
                f"expected worktree disappeared before removal could be verified: {worktree}"
            )
        _verify_removed_record(lock, repository_name, record)
        return
    identity = validate_worktree_registration(source, worktree)
    validate_removal_repo_identity(
        record,
        worktree_path=worktree,
        git_admin_path=identity.git_admin_path,
    )
    run_git(["worktree", "remove", worktree], cwd=source)
    _verify_removed_record(lock, repository_name, record)


def _rename_to_removal_tombstone(paths: WorkspacePaths, tombstone: Path) -> None:
    if tombstone.exists() or tombstone.is_symlink():
        raise WsError(f"removal tombstone already exists: {tombstone}")
    repos = paths.workspace / "repos"
    if not repos.is_dir() or any(repos.iterdir()):
        raise WsError("cannot rename workspace while expected worktree content remains")
    if not (paths.workspace / ".ws").is_dir():
        raise WsError("workspace metadata directory is missing before removal rename")
    paths.workspace.rename(tombstone)


def _delete_removal_tombstone(paths: WorkspacePaths) -> None:
    if not paths.workspace.is_dir():
        raise WsError(f"removal tombstone is missing: {paths.workspace}")
    repos = paths.workspace / "repos"
    metadata = paths.workspace / ".ws"
    if {entry.name for entry in paths.workspace.iterdir()} != {
        ".ws",
        "repos",
        "workspace.lock.toml",
    }:
        raise WsError("removal tombstone contains unexpected top-level content")
    if not repos.is_dir() or any(repos.iterdir()):
        raise WsError("removal tombstone contains unexpected repository content")
    if not metadata.is_dir():
        raise WsError("removal tombstone metadata directory is missing")
    if {entry.name for entry in metadata.iterdir()} != {"state.toml", "operation.lock"}:
        raise WsError("removal tombstone contains unexpected metadata content")
    if paths.lock.is_symlink() or not paths.lock.is_file():
        raise WsError(f"removal tombstone contains unexpected metadata: {paths.lock}")
    if paths.state.is_symlink() or not paths.state.is_file():
        raise WsError(f"removal tombstone contains unexpected state metadata: {paths.state}")
    if not paths.operation_lock.is_dir() or any(paths.operation_lock.iterdir()):
        raise WsError(f"removal operation lock is not an empty directory: {paths.operation_lock}")
    lock_text = paths.lock.read_text(encoding="utf-8")
    state_text = paths.state.read_text(encoding="utf-8")
    disposable = _disposable_paths(paths)
    if disposable.workspace.exists() or disposable.workspace.is_symlink():
        raise WsError(f"removal disposable path already exists: {disposable.workspace}")
    paths.workspace.rename(disposable.workspace)
    try:
        # The authoritative tombstone is atomically moved out of the recovery
        # name before deletion.  If deletion is interrupted, the deterministic
        # disposable path remains the only recovery candidate.
        _delete_disposable_tombstone(disposable)
    except BaseException as exc:
        try:
            _restore_interrupted_tombstone(disposable, lock_text, state_text)
        except BaseException as restore_error:
            exc.add_note(f"could not reconstruct removal tombstone: {restore_error}")
        raise


def _restore_interrupted_tombstone(
    paths: WorkspacePaths,
    lock_text: str,
    state_text: str,
) -> None:
    paths.workspace.mkdir(parents=True, exist_ok=True)
    repos = paths.workspace / "repos"
    metadata = paths.workspace / ".ws"
    repos.mkdir(exist_ok=True)
    metadata.mkdir(exist_ok=True)
    lock = deserialize_workspace_lock(lock_text)
    state = deserialize_workspace_state(state_text)
    write_workspace_lock(paths.lock, lock)
    write_workspace_state(paths.state, state)
    if paths.operation_lock.exists() or paths.operation_lock.is_symlink():
        if not paths.operation_lock.is_dir() or any(paths.operation_lock.iterdir()):
            raise WsError(f"cannot reconstruct removal operation lock: {paths.operation_lock}")
    else:
        paths.operation_lock.mkdir()


def _delete_disposable_tombstone(paths: WorkspacePaths) -> None:
    if not paths.workspace.exists():
        return
    if not paths.workspace.is_dir() or paths.workspace.is_symlink():
        raise WsError(f"removal disposable path is not a directory: {paths.workspace}")
    for entry in paths.workspace.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    paths.workspace.rmdir()


def _claim_source_commit(locked: WorkspaceLockRepo, source: str | None) -> str:
    if source is None:
        return locked.base_commit
    if source == "default":
        if locked.default_selector is None:
            raise WsError(
                f"repository {locked.name!r} has no locked default selector; "
                "provide an explicit --source ref"
            )
        return _resolve_commit(locked.source_path, locked.default_selector)
    return _resolve_commit(locked.source_path, source)


def _validate_claim_target(target: str) -> None:
    result = run_git(["check-ref-format", "--branch", target], check=False)
    if result.returncode != 0:
        raise WsError(f"invalid claim target branch {target!r}")


def _write_claimed_state(
    paths: WorkspacePaths,
    state: WorkspaceState,
    repository_name: str,
    saved: RepoState,
    live: dict[str, Any],
) -> None:
    claimed = replace(
        saved,
        mode="claimed",
        head=live["head"],
        branch=live["branch"],
        detached=False,
        dirty=live["dirty"],
        context=None,
    )
    write_workspace_state(
        paths.state, replace(state, repos={**state.repos, repository_name: claimed})
    )


def _validate_discovered_workspace_name(paths: WorkspacePaths, lock: WorkspaceLock) -> None:
    if lock.workspace_name != paths.workspace.name:
        raise WsError(
            f"workspace name in lock {lock.workspace_name!r} does not match directory "
            f"{paths.workspace.name!r}"
        )


def enter_context(repository_name: str, target_ref: str) -> str | None:
    validate_logical_name(repository_name, kind="repository")
    if not target_ref:
        raise WsError("context requires a non-empty ref")
    paths = discover_workspace()
    lifecycle_acquired, operation_acquired = _acquire_mutator_locks(paths)
    try:
        lock, state = _read_metadata(paths)
        _validate_discovered_workspace_name(paths, lock)
        _reject_context_mutation_during_removal(state)
        if state.phase in {"entering", "restoring"}:
            raise WsError(
                f"workspace has an interrupted context transition ({state.phase}); "
                "manual recovery is required"
            )
        if state.phase in {"restore_conflicted", "restore_failed"}:
            raise WsError(
                f"workspace has an unresolved context transition ({state.phase}); "
                "restore it before entering another context"
            )
        locked = _locked_repo(lock, repository_name)
        saved = state.repos[repository_name]
        if saved.context is not None or saved.mode == "context":
            raise WsError(
                f"repository {repository_name!r} already has an active temporary context; "
                f"restore it first with: ws context {repository_name} --restore"
            )
        worktree = paths.workspace / "repos" / repository_name
        validate_worktree_registration(locked.source_path, worktree)
        live = _read_live_repo(worktree)
        target_commit = _context_target_commit(locked, target_ref)
        token = uuid.uuid4().hex
        private_ref = f"refs/ws/context/{lock.workspace_name}/{repository_name}/{token}"
        context = ContextState(
            target_ref=target_ref,
            target_commit=target_commit,
            phase="entering",
            return_mode=live["mode"],
            stash_token=token,
            return_branch=live["branch"],
            return_saved_head=live["head"],
            private_ref=private_ref if live["dirty"] else None,
        )
        entering = _set_context_state(
            state,
            repository_name,
            saved,
            context,
            workspace_phase="entering",
            mode="context",
            head=live["head"],
            branch=live["branch"],
            dirty=live["dirty"],
        )
        write_workspace_state(paths.state, entering)
        current = entering
        stash_message = f"ws-context:{lock.workspace_name}:{repository_name}:{token}"
        if live["dirty"]:
            current = _context_effect_intent(
                paths,
                current,
                repository_name,
                "stash",
                "intent",
                expected_refs={"HEAD": live["head"]},
            )
            try:
                run_git(
                    ["stash", "push", "--include-untracked", "-m", stash_message],
                    cwd=worktree,
                )
                stash_oid = _find_stash_oid(worktree, stash_message)
            except Exception as exc:
                _context_effect_failure(paths, current, repository_name, "stash")
                raise WsError(
                    f"context stash creation failed; context remains in entering phase "
                    f"with token {token}: {exc}"
                ) from exc
            current_context = current.repos[repository_name].context
            assert current_context is not None
            context = replace(current_context, stash_oid=stash_oid)
            current = _set_context_state(
                current,
                repository_name,
                current.repos[repository_name],
                context,
                workspace_phase="entering",
                mode="context",
                head=live["head"],
                branch=live["branch"],
                dirty=False,
            )
            current = _context_effect_outcome(
                paths,
                current,
                repository_name,
                "stash",
                "outcome",
                known_oids={"stash": stash_oid},
            )
            current = _context_effect_intent(
                paths,
                current,
                repository_name,
                "private_ref",
                "intent",
                known_oids={"stash": stash_oid},
            )
            try:
                create_private_ref(private_ref, stash_oid, cwd=worktree)
            except Exception as exc:
                _context_effect_failure(
                    paths,
                    current,
                    repository_name,
                    "private_ref",
                    known_oids={"stash": stash_oid},
                )
                raise WsError(
                    f"context private snapshot pinning failed; context remains in entering "
                    f"phase with stash {stash_oid}: {exc}"
                ) from exc
            current = _context_effect_outcome(
                paths,
                current,
                repository_name,
                "private_ref",
                "outcome",
                known_oids={"stash": stash_oid},
            )
        current = _context_effect_intent(
            paths,
            current,
            repository_name,
            "checkout",
            "intent",
            expected_refs={"HEAD": target_commit},
        )
        try:
            run_git(["switch", "--detach", target_commit], cwd=worktree)
        except Exception as exc:
            _context_effect_failure(
                paths,
                current,
                repository_name,
                "checkout",
                known_oids={"target": target_commit},
            )
            raise WsError(
                f"context checkout failed; context remains in entering phase: {exc}"
            ) from exc
        current = _context_effect_outcome(
            paths,
            current,
            repository_name,
            "checkout",
            "outcome",
            known_oids={"target": target_commit},
        )
        final_context = current.repos[repository_name].context
        assert final_context is not None
        final_context = replace(final_context, phase="active")
        final_live = _read_live_repo(worktree)
        current = _set_context_state(
            current,
            repository_name,
            current.repos[repository_name],
            final_context,
            workspace_phase="active",
            mode="context",
            head=final_live["head"],
            branch=None,
            dirty=final_live["dirty"],
        )
        write_workspace_state(paths.state, current)
        if final_context.stash_oid is not None:
            return (
                f"Context active; stash {final_context.stash_oid} retained for manual cleanup "
                f"(message: {stash_message})"
            )
        return None
    finally:
        if operation_acquired:
            _release_operation_lock(paths.operation_lock)
        if lifecycle_acquired:
            _release_lifecycle_lock(paths.lifecycle_lock)


def restore_context(repository_name: str) -> str | None:
    validate_logical_name(repository_name, kind="repository")
    paths = discover_workspace()
    lifecycle_acquired, operation_acquired = _acquire_mutator_locks(paths)
    try:
        lock, state = _read_metadata(paths)
        _validate_discovered_workspace_name(paths, lock)
        _reject_context_mutation_during_removal(state)
        locked = _locked_repo(lock, repository_name)
        saved = state.repos[repository_name]
        context = saved.context
        if context is None or saved.mode != "context":
            raise WsError(f"repository {repository_name!r} has no active temporary context")
        if context.phase in {"entering", "restoring"}:
            raise WsError(
                f"context is in interrupted {context.phase} phase; manual recovery is required"
            )
        if context.phase == "restore_conflicted":
            raise WsError(
                "context restore is conflicted; use --finalize-restore after resolving it"
            )
        if context.phase not in {"active", "restore_failed"}:
            raise WsError(f"cannot restore context in phase {context.phase!r}")
        worktree = paths.workspace / "repos" / repository_name
        validate_worktree_registration(locked.source_path, worktree)
        if context.phase == "restore_failed":
            _verify_return_baseline(locked.source_path, worktree, context)
        else:
            live = _read_live_repo(worktree)
            if live["dirty"]:
                raise WsError(
                    "temporary context worktree has uncommitted changes; clean, commit, or "
                    "stash them before restoring"
                )
            _verify_return_identity(locked.source_path, worktree, context)
        restoring = _set_context_state(
            state,
            repository_name,
            saved,
            replace(context, phase="restoring"),
            workspace_phase="restoring",
            mode="context",
            head=saved.head,
            branch=None,
            dirty=False,
        )
        write_workspace_state(paths.state, restoring)
        current = _context_effect_intent(
            paths,
            restoring,
            repository_name,
            "return_checkout",
            "intent",
        )
        return_head = context.return_saved_head
        assert return_head is not None
        try:
            if context.return_mode == "claimed":
                assert context.return_branch is not None
                run_git(["switch", context.return_branch], cwd=worktree)
            else:
                run_git(["switch", "--detach", return_head], cwd=worktree)
        except Exception as exc:
            _context_effect_failure(
                paths,
                current,
                repository_name,
                "return_checkout",
                known_oids={"return": return_head},
            )
            raise WsError(
                f"context return checkout failed; restore remains blocked: {exc}"
            ) from exc
        current = _context_effect_outcome(
            paths,
            current,
            repository_name,
            "return_checkout",
            "outcome",
            known_oids={"return": return_head},
        )
        if context.stash_oid is not None:
            assert context.private_ref is not None
            current = _context_effect_intent(
                paths,
                current,
                repository_name,
                "stash_apply",
                "intent",
                known_oids={"stash": context.stash_oid},
            )
            try:
                run_git(["stash", "apply", "--index", context.private_ref], cwd=worktree)
            except Exception as exc:
                current = _context_effect_failure(
                    paths,
                    current,
                    repository_name,
                    "stash_apply",
                    known_oids={"stash": context.stash_oid},
                )
                if _has_unmerged_entries(worktree):
                    current_context = current.repos[repository_name].context
                    assert current_context is not None
                    failed_context = replace(current_context, phase="restore_conflicted")
                    conflicted = _set_context_state(
                        current,
                        repository_name,
                        current.repos[repository_name],
                        failed_context,
                        workspace_phase="restore_conflicted",
                        mode="context",
                        head=context.return_saved_head,
                        branch=context.return_branch,
                        dirty=True,
                    )
                    write_workspace_state(paths.state, conflicted)
                    raise WsError(
                        f"context restore conflicted; stash {context.stash_oid} retained; "
                        "resolve conflicts and run ws context "
                        f"{repository_name} --finalize-restore"
                    ) from exc
                current_context = current.repos[repository_name].context
                assert current_context is not None
                failed_context = replace(current_context, phase="restore_failed")
                failed = _set_context_state(
                    current,
                    repository_name,
                    current.repos[repository_name],
                    failed_context,
                    workspace_phase="restore_failed",
                    mode="context",
                    head=context.return_saved_head,
                    branch=context.return_branch,
                    dirty=_read_live_repo(worktree)["dirty"],
                )
                write_workspace_state(paths.state, failed)
                raise WsError(
                    f"context stash apply failed; stash {context.stash_oid} and context state "
                    "were retained; clean the exact return baseline before retrying"
                ) from exc
            current = _context_effect_outcome(
                paths,
                current,
                repository_name,
                "stash_apply",
                "outcome",
                known_oids={"stash": context.stash_oid},
            )
        if context.private_ref is not None:
            current = _context_effect_intent(
                paths,
                current,
                repository_name,
                "private_ref_delete",
                "intent",
                known_oids={"stash": context.stash_oid or return_head},
            )
            try:
                assert context.stash_oid is not None
                delete_private_ref(context.private_ref, context.stash_oid, cwd=worktree)
            except Exception as exc:
                _context_effect_failure(
                    paths,
                    current,
                    repository_name,
                    "private_ref_delete",
                    known_oids={"stash": context.stash_oid or return_head},
                )
                raise WsError(
                    f"private context snapshot was not deleted; restore remains blocked: {exc}"
                ) from exc
            current = _context_effect_outcome(
                paths,
                current,
                repository_name,
                "private_ref_delete",
                "outcome",
                known_oids={"stash": context.stash_oid or return_head},
            )
        final_live = _read_live_repo(worktree)
        restored = replace(
            current,
            phase="idle",
            repos={
                **current.repos,
                repository_name: RepoState(
                    name=repository_name,
                    mode=context.return_mode,
                    head=final_live["head"],
                    branch=final_live["branch"],
                    detached=final_live["detached"],
                    dirty=final_live["dirty"],
                    context=None,
                ),
            },
        )
        write_workspace_state(paths.state, restored)
        if context.stash_oid is not None:
            message = f"ws-context:{lock.workspace_name}:{repository_name}:{context.stash_token}"
            return (
                f"Context restored; stash {context.stash_oid} retained for manual cleanup "
                f"(message: {message})"
            )
        return None
    finally:
        if operation_acquired:
            _release_operation_lock(paths.operation_lock)
        if lifecycle_acquired:
            _release_lifecycle_lock(paths.lifecycle_lock)


def finalize_restore(repository_name: str) -> str | None:
    validate_logical_name(repository_name, kind="repository")
    paths = discover_workspace()
    lifecycle_acquired, operation_acquired = _acquire_mutator_locks(paths)
    try:
        lock, state = _read_metadata(paths)
        _validate_discovered_workspace_name(paths, lock)
        _reject_context_mutation_during_removal(state)
        locked = _locked_repo(lock, repository_name)
        saved = state.repos[repository_name]
        context = saved.context
        if context is None or context.phase != "restore_conflicted":
            raise WsError("--finalize-restore is allowed only for a restore_conflicted context")
        if context.private_ref is None or context.stash_oid is None:
            raise WsError("conflicted context has no retained private snapshot to finalize")
        worktree = paths.workspace / "repos" / repository_name
        validate_worktree_registration(locked.source_path, worktree)
        if _has_unmerged_entries(worktree):
            raise WsError("cannot finalize restore while unmerged index entries remain")
        current = _context_effect_intent(
            paths,
            state,
            repository_name,
            "private_ref_delete",
            "intent",
            known_oids={"stash": context.stash_oid},
        )
        try:
            delete_private_ref(context.private_ref, context.stash_oid, cwd=worktree)
        except Exception as exc:
            _context_effect_failure(
                paths,
                current,
                repository_name,
                "private_ref_delete",
                known_oids={"stash": context.stash_oid},
            )
            raise WsError(f"private context snapshot was not deleted: {exc}") from exc
        current = _context_effect_outcome(
            paths,
            current,
            repository_name,
            "private_ref_delete",
            "outcome",
            known_oids={"stash": context.stash_oid},
        )
        live = _read_live_repo(worktree)
        finalized = replace(
            current,
            phase="idle",
            repos={
                **current.repos,
                repository_name: RepoState(
                    name=repository_name,
                    mode=context.return_mode,
                    head=live["head"],
                    branch=live["branch"],
                    detached=live["detached"],
                    dirty=live["dirty"],
                    context=None,
                ),
            },
        )
        write_workspace_state(paths.state, finalized)
        message = f"ws-context:{lock.workspace_name}:{repository_name}:{context.stash_token}"
        return (
            f"Context finalized; stash {context.stash_oid} retained for manual cleanup "
            f"(message: {message})"
        )
    finally:
        if operation_acquired:
            _release_operation_lock(paths.operation_lock)
        if lifecycle_acquired:
            _release_lifecycle_lock(paths.lifecycle_lock)


def _acquire_mutator_locks(paths: WorkspacePaths) -> tuple[bool, bool]:
    try:
        paths.lifecycle_lock.mkdir()
    except FileExistsError as exc:
        raise WsError(f"workspace lifecycle lock already exists: {paths.lifecycle_lock}") from exc
    try:
        paths.operation_lock.mkdir()
    except FileExistsError as exc:
        _release_lifecycle_lock(paths.lifecycle_lock)
        raise WsError(f"workspace operation lock already exists: {paths.operation_lock}") from exc
    return True, True


def _locked_repo(lock: WorkspaceLock, repository_name: str) -> WorkspaceLockRepo:
    locked = lock.repos.get(repository_name)
    if locked is None:
        raise WsError(
            f"repository {repository_name!r} is not part of workspace {lock.workspace_name!r}"
        )
    return locked


def _reject_context_mutation_during_removal(state: WorkspaceState) -> None:
    if state.removal is not None or state.phase in REMOVAL_PHASES:
        raise WsError("cannot mutate repository while workspace removal is durable")


def _context_target_commit(locked: WorkspaceLockRepo, target_ref: str) -> str:
    if target_ref == "default":
        if locked.default_selector is None:
            raise WsError(
                f"repository {locked.name!r} has no locked default selector; "
                "provide an explicit context ref"
            )
        target_ref = locked.default_selector
    return _resolve_commit(locked.source_path, target_ref)


def _set_context_state(
    state: WorkspaceState,
    repository_name: str,
    saved: RepoState,
    context: ContextState,
    *,
    workspace_phase: str,
    mode: str,
    head: str | None,
    branch: str | None,
    dirty: bool,
) -> WorkspaceState:
    repo = replace(
        saved,
        mode=mode,
        head=head,
        branch=branch,
        detached=mode == "context" or branch is None,
        dirty=dirty,
        context=context,
    )
    return replace(
        state,
        phase=workspace_phase,
        repos={**state.repos, repository_name: repo},
    )


def _context_effect_intent(
    paths: WorkspacePaths,
    state: WorkspaceState,
    repository_name: str,
    operation: str,
    step: str,
    *,
    expected_refs: dict[str, str] | None = None,
    known_oids: dict[str, str] | None = None,
) -> WorkspaceState:
    return _persist_context_effect(
        paths,
        state,
        repository_name,
        operation,
        step,
        expected_refs=expected_refs,
        known_oids=known_oids,
    )


def _context_effect_outcome(
    paths: WorkspacePaths,
    state: WorkspaceState,
    repository_name: str,
    operation: str,
    step: str,
    *,
    known_oids: dict[str, str] | None = None,
) -> WorkspaceState:
    return _persist_context_effect(
        paths,
        state,
        repository_name,
        operation,
        step,
        known_oids=known_oids,
    )


def _context_effect_failure(
    paths: WorkspacePaths,
    state: WorkspaceState,
    repository_name: str,
    operation: str,
    *,
    known_oids: dict[str, str] | None = None,
) -> WorkspaceState:
    return _persist_context_effect(
        paths,
        state,
        repository_name,
        operation,
        "failure",
        known_oids=known_oids,
    )


def _persist_context_effect(
    paths: WorkspacePaths,
    state: WorkspaceState,
    repository_name: str,
    operation: str,
    step: str,
    *,
    expected_refs: dict[str, str] | None = None,
    known_oids: dict[str, str] | None = None,
) -> WorkspaceState:
    saved = state.repos[repository_name]
    context = saved.context
    assert context is not None
    effect = ContextSideEffect(
        operation=operation,
        step=step,
        expected_refs={} if expected_refs is None else expected_refs,
        known_oids={} if known_oids is None else known_oids,
    )
    updated_context = replace(context, completed_effects=(*context.completed_effects, effect))
    updated = replace(
        state, repos={**state.repos, repository_name: replace(saved, context=updated_context)}
    )
    write_workspace_state(paths.state, updated)
    return updated


def _find_stash_oid(worktree: Path, message: str) -> str:
    result = run_git(["stash", "list", "--format=%H%x00%s"], cwd=worktree)
    matches = [
        record.split("\0", 1)[0]
        for record in result.stdout.splitlines()
        if "\0" in record
        and (
            record.split("\0", 1)[1] == message or record.split("\0", 1)[1].endswith(f": {message}")
        )
    ]
    if len(matches) != 1:
        raise WsError(
            f"could not identify exactly one context stash by token; found {len(matches)}"
        )
    return matches[0]


def _verify_return_identity(source: Path, worktree: Path, context: ContextState) -> None:
    if context.return_mode == "claimed":
        if context.return_branch is None:
            raise WsError("context return record is missing its claimed branch")
        branch = run_git(
            ["rev-parse", "--verify", f"refs/heads/{context.return_branch}^{{commit}}"],
            cwd=source,
            check=False,
        )
        if branch.returncode != 0 or branch.stdout.strip() != context.return_saved_head:
            raise WsError("saved return branch no longer points at its recorded HEAD")
        for entry in list_worktrees(source):
            if entry.branch == context.return_branch and entry.path != worktree.resolve():
                raise WsError("saved return branch is checked out by another worktree")
    else:
        return_head = context.return_saved_head
        assert return_head is not None
        _verify_commit_exists(worktree, return_head)


def _verify_return_baseline(source: Path, worktree: Path, context: ContextState) -> None:
    _verify_return_identity(source, worktree, context)
    live = _read_live_repo(worktree)
    expected_branch = context.return_branch if context.return_mode == "claimed" else None
    if live["branch"] != expected_branch or live["head"] != context.return_saved_head:
        raise WsError("restore retry requires the exact saved return branch and HEAD")
    if live["dirty"] or _has_unmerged_entries(worktree):
        raise WsError("restore retry requires a clean return baseline with no unmerged entries")


def _verify_commit_exists(worktree: Path, commit: str) -> None:
    result = run_git(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=worktree, check=False)
    if result.returncode != 0:
        raise WsError(f"saved return commit is no longer available: {commit}")


def _has_unmerged_entries(worktree: Path) -> bool:
    return bool(run_git(["ls-files", "--unmerged"], cwd=worktree).stdout.strip())


def status_workspace(start: Path | None = None) -> dict[str, Any]:
    paths = discover_workspace(start)
    lock, state = _read_metadata(paths)
    if lock.workspace_name != paths.workspace.name:
        raise WsError(
            f"workspace name in lock {lock.workspace_name!r} does not match directory "
            f"{paths.workspace.name!r}"
        )
    repos: dict[str, Any] = {}
    for name, locked in lock.repos.items():
        worktree = paths.workspace / "repos" / name
        saved = state.repos[name]
        removal_record = state.removal.repos[name] if state.removal is not None else None
        if removal_record is not None and removal_record.complete:
            live = {
                "head": saved.head,
                "mode": saved.mode,
                "branch": saved.branch,
                "detached": saved.detached,
                "dirty": saved.dirty,
            }
        else:
            validate_worktree_registration(locked.source_path, worktree)
            live = _read_live_repo(worktree)
        context: dict[str, Any] | None = None
        if saved.context is not None:
            context = {
                "target_ref": saved.context.target_ref,
                "target_commit": saved.context.target_commit,
                "phase": saved.context.phase,
                "return_mode": saved.context.return_mode,
                "return_branch": saved.context.return_branch,
                "return_saved_head": saved.context.return_saved_head,
            }
        repos[name] = {
            "locked_base_ref": locked.base_ref,
            "locked_default_selector": locked.default_selector,
            "locked_base_commit": locked.base_commit,
            "head": live["head"],
            "mode": "context" if saved.mode == "context" else live["mode"],
            "branch": live["branch"],
            "detached": live["detached"],
            "dirty": live["dirty"],
            "context": context,
        }
    removal: dict[str, Any] | None = None
    if state.removal is not None:
        removal = {
            "phase": state.removal.phase,
            "completed": [name for name, repo in state.removal.repos.items() if repo.complete],
        }
    return {"workspace": lock.workspace_name, "removal": removal, "repos": repos}


def render_status_human(payload: dict[str, Any]) -> str:
    lines = [f"WORKSPACE {payload['workspace']}", ""]
    lines.append(
        "repo       base             HEAD        mode      branch              dirty  context"
    )
    lines.append("-" * 88)
    for name, repo in payload["repos"].items():
        head = str(repo["head"])[:12]
        branch = repo["branch"] or "-"
        context = "-" if repo["context"] is None else repo["context"]["target_ref"]
        lines.append(
            f"{name:<10} {str(repo['locked_base_ref']):<16} {head:<11} "
            f"{repo['mode']:<9} {branch:<19} "
            f"{'yes' if repo['dirty'] else 'no':<6} {context}"
        )
    return "\n".join(lines)


def discover_workspace(start: Path | None = None) -> WorkspacePaths:
    current = (Path.cwd() if start is None else start).expanduser().resolve()
    for candidate in (current, *current.parents):
        metadata = candidate / ".ws"
        if not metadata.is_dir():
            continue
        paths = _paths(candidate.parent, candidate.name)
        if not paths.lock.is_file() or not paths.state.is_file():
            raise WsError(f"workspace metadata is incomplete under {candidate}")
        return paths
    raise WsError(f"no workspace found above {current}")


def _paths(root: Path, name: str) -> WorkspacePaths:
    workspace = root / name
    return WorkspacePaths(
        root=root,
        workspace=workspace,
        lock=workspace / "workspace.lock.toml",
        state=workspace / ".ws" / "state.toml",
        tombstone=root / f".{name}.removing",
        lifecycle_lock=root / f".{name}.lifecycle.lock",
        operation_lock=workspace / ".ws" / "operation.lock",
    )


def _resolve_creation_plans(
    repos: dict[str, Any], overrides: dict[str, str]
) -> list[_CreationPlan]:
    plans: list[_CreationPlan] = []
    for name, repo in repos.items():
        override = overrides.get(name)
        try:
            selector = _default_selector(repo.source_path, repo.default_branch)
            if selector is not None:
                _resolve_commit(repo.source_path, selector)
        except ConfigError:
            if override is None:
                raise
            selector = None
        if override is None:
            if selector is None:
                raise ConfigError(
                    f"cannot determine default source for repository {name!r}; "
                    "provide --source repo=ref"
                )
            base_ref = selector
        else:
            base_ref = override
        base_commit = _resolve_commit(repo.source_path, base_ref)
        plans.append(_CreationPlan(name, repo.source_path, base_ref, base_commit, selector))
    return plans


def _default_selector(source: Path, configured: str | None) -> str | None:
    if configured is not None:
        return configured
    remote_selector = _remote_symbolic_selector(source)
    if remote_selector is not None:
        return remote_selector
    if repository_kind(source) == "worktree":
        current = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=source, check=False)
        if current.returncode == 0 and current.stdout.strip():
            return current.stdout.strip()
    return None


def _remote_symbolic_selector(source: Path) -> str | None:
    result = run_git(
        ["for-each-ref", "--format=%(refname) %(symref)", "refs/remotes"],
        cwd=source,
        check=False,
    )
    targets: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not parts[0].endswith("/HEAD") or not parts[1]:
            continue
        target = parts[1].removeprefix("refs/remotes/")
        branch = target.split("/", 1)[1] if "/" in target else target
        targets.append((branch, target))
    if not targets:
        return None
    branches = {branch for branch, _target in targets}
    if len(branches) != 1:
        raise ConfigError(f"remote symbolic HEADs disagree for source {source}")
    return targets[0][1]


def _resolve_commit(source: Path, ref: str) -> str:
    result = run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=source, check=False)
    commit = result.stdout.strip()
    if result.returncode != 0 or not commit:
        raise ConfigError(f"source ref {ref!r} does not resolve to a commit in {source}")
    return commit


def _validate_worktree_branch(workspace_name: str, repo_name: str) -> None:
    branch = f"ws/{workspace_name}/{repo_name}"
    result = run_git(["check-ref-format", "--branch", branch], check=False)
    if result.returncode != 0:
        raise ConfigError(f"invalid synthesized worktree branch {branch!r}")


def _rollback_creation(
    paths: WorkspacePaths,
    created: list[tuple[Path, Path]],
    workspace_created: bool,
    owned_metadata: list[Path],
    *,
    operation_acquired: bool = False,
) -> list[str]:
    issues: list[str] = []
    for source, worktree in reversed(created):
        try:
            validate_worktree_registration(source, worktree)
        except GitWorktreeError:
            if worktree.exists():
                issues.append(f"unregistered worktree path remains: {worktree}")
            continue
        except WsError as exc:
            issues.append(f"could not verify worktree registration {worktree}: {exc}")
            continue
        try:
            removal = run_git(["worktree", "remove", worktree], cwd=source, check=False)
        except WsError as exc:
            issues.append(f"could not remove worktree {worktree}: {exc}")
            continue
        if removal.returncode != 0:
            detail = removal.stderr.strip() or removal.stdout.strip() or "unknown Git error"
            issues.append(f"could not remove worktree {worktree}: {detail}")
            continue
        if worktree.exists():
            issues.append(f"worktree path remains after Git cleanup: {worktree}")
            continue
        try:
            validate_worktree_registration(source, worktree)
        except WsError:
            pass
        else:
            issues.append(f"Git worktree registration remains for {worktree}")
    if issues:
        return issues
    if workspace_created:
        for metadata in owned_metadata:
            try:
                if metadata.is_symlink() or not metadata.is_file():
                    issues.append(f"owned metadata path is not a regular file: {metadata}")
                else:
                    metadata.unlink()
            except OSError as exc:
                issues.append(f"could not remove owned metadata {metadata}: {exc}")
        if operation_acquired and not issues:
            try:
                paths.operation_lock.rmdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                issues.append(f"could not remove operation lock {paths.operation_lock}: {exc}")
        for directory in (
            paths.workspace / ".ws",
            paths.workspace / "repos",
            paths.workspace,
        ):
            try:
                directory.rmdir()
            except FileNotFoundError:
                continue
            except OSError as exc:
                issues.append(f"retained non-empty or inaccessible path {directory}: {exc}")
    return issues


def _release_lifecycle_lock(path: Path) -> None:
    try:
        path.rmdir()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise WsError(f"cannot release workspace lifecycle lock {path}: {exc}") from exc


def _release_operation_lock(path: Path) -> None:
    try:
        path.rmdir()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise WsError(f"cannot release workspace operation lock {path}: {exc}") from exc


def _read_metadata(paths: WorkspacePaths) -> tuple[WorkspaceLock, WorkspaceState]:
    try:
        lock = deserialize_workspace_lock(paths.lock.read_text(encoding="utf-8"))
        state = deserialize_workspace_state(paths.state.read_text(encoding="utf-8"))
    except (OSError, SerializationError) as exc:
        if isinstance(exc, SerializationError):
            raise
        raise WsError(f"cannot read workspace metadata: {exc}") from exc
    validate_lock_state_consistency(lock, state)
    return lock, state


def _read_live_repo(worktree: Path) -> dict[str, Any]:
    if not worktree.is_dir():
        raise WsError(f"workspace repository worktree is missing: {worktree}")
    head = run_git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    branch_result = run_git(
        ["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=worktree, check=False
    )
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    dirty = bool(run_git(["status", "--porcelain", "--untracked-files=all"], cwd=worktree).stdout)
    return {
        "head": head,
        "branch": branch,
        "detached": branch is None,
        "mode": "claimed" if branch is not None else "detached",
        "dirty": dirty,
    }
