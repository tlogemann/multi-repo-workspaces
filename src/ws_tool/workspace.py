from __future__ import annotations

import re
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
    remote_symbolic_heads,
    repository_kind,
    run_git,
    validate_worktree_registration,
)
from .models import (
    REMOVAL_PHASES,
    ContextSideEffect,
    ContextState,
    RemovalRepoState,
    RemovalSeal,
    RemovalSealRepo,
    RemovalState,
    RepoState,
    WorkspaceLock,
    WorkspaceLockRepo,
    WorkspaceState,
)
from .serialization import (
    deserialize_removal_seal,
    deserialize_workspace_lock,
    deserialize_workspace_state,
    fsync_directory,
    validate_lock_state_consistency,
    validate_removal_repo_identity,
    write_removal_seal,
    write_workspace_lock,
    write_workspace_state,
)
from .source_initialization import initialize_source_clone_root
from .validation import validate_logical_name


def _removal_test_boundary(_name: str) -> None:
    """Test-only hook kept inert in production."""

    return None


def _creation_test_boundary(_name: str) -> None:
    """Test-only hook kept inert in production."""

    return None


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    workspace: Path
    lock: Path
    state: Path
    tombstone: Path
    lifecycle_lock: Path
    operation_lock: Path
    removal_seal: Path


@dataclass(frozen=True)
class _TombstoneClassification:
    seal: RemovalSeal | None
    lock: WorkspaceLock | None
    state: WorkspaceState | None
    terminal: bool = False


@dataclass(frozen=True)
class _CreationPlan:
    name: str
    source_path: Path
    base_ref: str
    base_commit: str
    default_selector: str | None


def init_workspace(*, cwd: Path | None = None) -> Path:
    root = (cwd or Path.cwd()).resolve()
    project = load_config(root / "ws.toml")
    return initialize_source_clone_root(root, project.repos.values())


def create_workspace(
    workspace_name: str,
    *,
    config_path: str | Path | None = None,
    source_overrides: list[str] | tuple[str, ...] = (),
) -> WorkspacePaths:
    validate_logical_name(workspace_name, kind="workspace")
    project = load_config(Path("ws.toml") if config_path is None else config_path)
    initialization_marker = project.path.parent / "repos" / ".ws-init.lock"
    if initialization_marker.exists() or initialization_marker.is_symlink():
        raise ConfigError(
            f"source initialization in progress; retry after marker is removed: "
            f"{initialization_marker}"
        )
    for name, repo in project.repos.items():
        if repository_kind(repo.source_path) is None:
            raise ConfigError(
                f"repository {name!r} source is not initialized; expected initialized source clone "
                f"at {repo.source_path}"
            )
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
        _creation_test_boundary("create.lifecycle_locked")
        if paths.workspace.exists():
            raise WsError(f"workspace already exists: {paths.workspace}")
        if paths.tombstone.exists() or paths.tombstone.is_symlink():
            raise WsError(f"workspace removal tombstone already exists: {paths.tombstone}")

        plans = _resolve_creation_plans(project.repos, overrides, project.default_ref)
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
    operation_acquired = False
    try:
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
        locked = lock.repos.get(repository_name)
        if locked is None:
            raise WsError(
                f"repository {repository_name!r} is not part of workspace {lock.workspace_name!r}"
            )
        saved = state.repos[repository_name]
        if saved.context is not None:
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
    retain_locks = False
    removal_uncertain = False
    try:
        try:
            base_paths.lifecycle_lock.mkdir()
            lifecycle_acquired = True
        except FileExistsError as exc:
            raise WsError(
                f"workspace lifecycle lock already exists: {base_paths.lifecycle_lock}"
            ) from exc
        _removal_test_boundary("remove.lifecycle_locked")

        location = _existing_removal_paths(base_paths)
        if location.workspace == base_paths.tombstone:
            classification = _classify_removal_tombstone(location)
            if classification.terminal:
                try:
                    _resume_sealed_tombstone_cleanup(location, None)
                except BaseException:
                    # The tombstone has crossed the final deletion boundary;
                    # keep the external lifecycle lock if its parent fsync
                    # (or the rmdir itself) fails.
                    retain_locks = True
                    raise
                return
            if classification.seal is not None:
                operation_acquired = _acquire_tombstone_operation_lock(location)
                try:
                    _resume_sealed_tombstone_cleanup(location, classification.seal)
                except BaseException:
                    # The lifecycle lock is the authority across the terminal
                    # unlink window.  Never release it after a cleanup failure.
                    retain_locks = True
                    raise
                return
            assert classification.lock is not None
            assert classification.state is not None
            lock, state = classification.lock, classification.state
            try:
                operation_acquired = _acquire_tombstone_operation_lock(location)
            except FileExistsError as exc:
                raise WsError(
                    f"workspace operation lock already exists: {location.operation_lock}"
                ) from exc
        else:
            try:
                location.operation_lock.mkdir()
                operation_acquired = True
            except FileExistsError as exc:
                raise WsError(
                    f"workspace operation lock already exists: {location.operation_lock}"
                ) from exc
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
                _sealed_state, seal = _prepare_removal_seal(location, lock, state)
                try:
                    _resume_sealed_tombstone_cleanup(location, seal)
                except BaseException:
                    retain_locks = True
                    raise
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
            _removal_test_boundary("removing_persisted")

        current_removal = state.removal
        assert current_removal is not None
        if location.workspace != base_paths.workspace:
            raise WsError("cannot remove worktrees from a removal tombstone")
        for repository_name, record in current_removal.repos.items():
            if record.complete:
                _verify_removed_record(lock, repository_name, record)
                continue
            try:
                _remove_expected_worktree(
                    lock,
                    repository_name,
                    record,
                    allow_absent=resuming_removal,
                )
            except KeyboardInterrupt:
                removal_uncertain = True
                raise
            except BaseException:
                if _removal_is_uncertain(lock, repository_name, record):
                    removal_uncertain = True
                raise
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
        state, seal = _prepare_removal_seal(location, lock, state)
        try:
            _rename_to_removal_tombstone(location, base_paths.tombstone)
        except BaseException:
            # Once rename succeeds, the operation lock has moved into the
            # tombstone.  Keep both locks if its directory fsync fails so a
            # retry cannot race with the partially durable terminal state.
            if not location.workspace.exists() and base_paths.tombstone.exists():
                retain_locks = True
            raise
        tombstone_paths = _tombstone_paths(base_paths)
        try:
            _resume_sealed_tombstone_cleanup(tombstone_paths, seal)
        except BaseException:
            retain_locks = True
            raise
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt) or removal_uncertain:
            retain_locks = True
        raise
    finally:
        if operation_acquired and location is not None and not retain_locks:
            _release_operation_lock(location.operation_lock)
        if lifecycle_acquired and not retain_locks:
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


def _discover_removal_paths(start: Path | None = None) -> WorkspacePaths | None:
    current = (Path.cwd() if start is None else start).expanduser().resolve()
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
    if normal and tombstone:
        raise WsError(
            f"both workspace and removal tombstone exist; manual recovery required: "
            f"{base.workspace}, {base.tombstone}"
        )
    if normal:
        return base
    if tombstone:
        return _tombstone_paths(base)
    raise WsError(f"workspace does not exist: {base.workspace}")


def _tombstone_paths(base: WorkspacePaths) -> WorkspacePaths:
    tombstone = base.tombstone
    return replace(
        base,
        workspace=tombstone,
        lock=tombstone / "workspace.lock.toml",
        state=tombstone / ".ws" / "state.toml",
        operation_lock=tombstone / ".ws" / "operation.lock",
        removal_seal=tombstone / "removal-seal.toml",
    )


def _preflight_removal(
    lock: WorkspaceLock,
    state: WorkspaceState,
    paths: WorkspacePaths,
) -> RemovalState:
    if any(repo.context is not None for repo in state.repos.values()):
        raise WsError(
            "workspace removal preflight failed: repository has an active temporary context"
        )
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


def _build_removal_seal(
    lock: WorkspaceLock, state: WorkspaceState, removal: RemovalState
) -> RemovalSeal:
    repos: dict[str, RemovalSealRepo] = {}
    for name, locked in lock.repos.items():
        record = removal.repos[name]
        repo = state.repos[name]
        repos[name] = RemovalSealRepo(
            name=name,
            source_path=locked.source_path,
            worktree_path=record.worktree_path,
            git_admin_path=record.git_admin_path,
            base_ref=locked.base_ref,
            base_commit=locked.base_commit,
            default_selector=locked.default_selector,
            mode=repo.mode,
            head=repo.head,
            branch=repo.branch,
            detached=repo.detached,
            dirty=repo.dirty,
        )
    return RemovalSeal(
        workspace_name=state.workspace_name,
        workspace_path=removal.workspace_path,
        tombstone_path=removal.tombstone_path,
        repos=repos,
    )


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


def _removal_is_uncertain(
    lock: WorkspaceLock,
    repository_name: str,
    record: RemovalRepoState,
) -> bool:
    worktree = record.worktree_path
    if not worktree.exists():
        return True
    try:
        identity = validate_worktree_registration(
            lock.repos[repository_name].source_path,
            worktree,
        )
        validate_removal_repo_identity(
            record,
            worktree_path=worktree,
            git_admin_path=identity.git_admin_path,
        )
    except (GitWorktreeError, SerializationError, WsError):
        return True
    return False


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
    _removal_test_boundary("worktree_removed")


def _rename_to_removal_tombstone(paths: WorkspacePaths, tombstone: Path) -> None:
    if tombstone.exists() or tombstone.is_symlink():
        raise WsError(f"removal tombstone already exists: {tombstone}")
    repos = paths.workspace / "repos"
    if not repos.is_dir() or any(repos.iterdir()):
        raise WsError("cannot rename workspace while expected worktree content remains")
    if not (paths.workspace / ".ws").is_dir():
        raise WsError("workspace metadata directory is missing before removal rename")
    paths.workspace.rename(tombstone)
    _fsync_directory_checked(paths.root)
    _removal_test_boundary("tombstone_renamed")


def _prepare_removal_seal(
    paths: WorkspacePaths,
    lock: WorkspaceLock,
    state: WorkspaceState,
) -> tuple[WorkspaceState, RemovalSeal]:
    removal = _require_removal_complete(state.removal)
    seal = _build_removal_seal(lock, state, removal)
    sealed_state = replace(state, removal=replace(removal, seal=seal))
    write_workspace_state(paths.state, sealed_state)
    _removal_test_boundary("removal_complete_persisted")
    write_removal_seal(paths.removal_seal, seal)
    _removal_test_boundary("standalone_seal_persisted")
    return sealed_state, seal


def _require_removal_complete(removal: RemovalState | None) -> RemovalState:
    if removal is None or removal.phase != "removal_complete":
        raise WsError("removal tombstone does not contain completed removal progress")
    if any(not record.complete for record in removal.repos.values()):
        raise WsError("removal tombstone has incomplete repository progress")
    return removal


def _read_removal_seal(path: Path) -> RemovalSeal:
    if path.is_symlink() or not path.is_file():
        raise WsError(f"removal tombstone seal is not a regular file: {path}")
    try:
        return deserialize_removal_seal(path.read_text(encoding="utf-8"))
    except (OSError, SerializationError) as exc:
        raise WsError(f"cannot read removal seal {path}: {exc}") from exc


def _validate_seal_consistency(
    embedded: RemovalSeal | None,
    standalone: RemovalSeal | None,
) -> RemovalSeal:
    if embedded is not None and standalone is not None and embedded != standalone:
        raise WsError("embedded and standalone removal seals do not match")
    seal = standalone or embedded
    if seal is None:
        raise WsError("removal tombstone has no valid removal seal")
    return seal


def _classify_removal_tombstone(paths: WorkspacePaths) -> _TombstoneClassification:
    _validate_tombstone_cleanup_shape(paths)
    if not any(paths.workspace.iterdir()):
        return _TombstoneClassification(None, None, None, terminal=True)

    standalone = (
        _read_removal_seal(paths.removal_seal) if _path_present(paths.removal_seal) else None
    )
    lock = (
        _read_metadata_file(paths.lock, deserialize_workspace_lock)
        if _path_present(paths.lock)
        else None
    )
    state = (
        _read_metadata_file(paths.state, deserialize_workspace_state)
        if _path_present(paths.state)
        else None
    )
    embedded = state.removal.seal if state is not None and state.removal is not None else None
    if standalone is not None:
        seal = _validate_seal_consistency(embedded, standalone)
        _validate_seal_metadata(lock, state, seal, paths)
        _validate_monotonic_tombstone_shape(paths, lock, state)
        return _TombstoneClassification(seal, lock, state)

    if lock is None or state is None:
        raise WsError("partial removal tombstone has no valid standalone seal")
    if embedded is not None:
        raise WsError("embedded-only v2 removal tombstone has no standalone seal")
    if not state.removal or not state.removal._legacy_completed_without_seal:
        raise WsError("removal tombstone is not a supported legacy completed shape")
    _validate_lock_state_for_legacy(lock, state)
    _validate_legacy_tombstone_shape(paths)
    if state.removal is None or state.removal.phase != "removal_complete":
        raise WsError("removal tombstone has no completed removal seal")
    _verify_removal_complete(lock, state.removal)
    return _TombstoneClassification(None, lock, state)


def _validate_monotonic_tombstone_shape(
    paths: WorkspacePaths,
    lock: WorkspaceLock | None,
    state: WorkspaceState | None,
) -> None:
    """Reject filesystem states that cannot be suffixes of ordered cleanup."""

    lock_present = _path_present(paths.lock)
    state_present = _path_present(paths.state)
    operation_present = _path_present(paths.operation_lock)
    repos_present = _path_present(paths.workspace / "repos")
    metadata_present = _path_present(paths.workspace / ".ws")
    if lock_present and not state_present:
        raise WsError("non-monotonic removal tombstone: lock remains after state removal")
    if repos_present and not (lock_present and state_present):
        raise WsError("non-monotonic removal tombstone: repository directory is out of order")
    if (state_present or operation_present) and not metadata_present:
        raise WsError("non-monotonic removal tombstone: metadata directory is missing")
    if lock is not None and not lock_present:
        raise WsError("removal lock metadata disappeared during classification")
    if state is not None and not state_present:
        raise WsError("removal state metadata disappeared during classification")


def _validate_lock_state_for_legacy(lock: WorkspaceLock, state: WorkspaceState) -> None:
    try:
        validate_lock_state_consistency(lock, state)
    except SerializationError as exc:
        raise WsError(f"legacy removal lock/state metadata is inconsistent: {exc}") from exc


def _validate_legacy_tombstone_shape(paths: WorkspacePaths) -> None:
    entries = {entry.name for entry in paths.workspace.iterdir()}
    if entries != {".ws", "repos", "workspace.lock.toml"}:
        raise WsError("legacy removal tombstone is not a complete ordered shape")
    metadata_entries = {entry.name for entry in (paths.workspace / ".ws").iterdir()}
    if metadata_entries not in ({"state.toml"}, {"state.toml", "operation.lock"}):
        raise WsError("legacy removal metadata is not a complete ordered shape")


def _validate_seal_metadata(
    lock: WorkspaceLock | None,
    state: WorkspaceState | None,
    seal: RemovalSeal,
    paths: WorkspacePaths,
) -> None:
    workspace_name = paths.tombstone.name[1 : -len(".removing")]
    if seal.workspace_name != workspace_name:
        raise WsError("removal seal workspace name does not match tombstone")
    normal_workspace = paths.root / workspace_name
    if seal.workspace_path.resolve(strict=False) != normal_workspace.resolve(strict=False):
        raise WsError("removal seal workspace path does not match tombstone")
    if seal.tombstone_path.resolve(strict=False) != paths.tombstone.resolve(strict=False):
        raise WsError("removal seal tombstone path does not match tombstone")
    if lock is not None:
        if lock.workspace_name != seal.workspace_name or set(lock.repos) != set(seal.repos):
            raise WsError("removal seal does not match workspace lock identities")
        for name, repo in seal.repos.items():
            locked = lock.repos[name]
            if (
                locked.source_path.resolve(strict=False)
                != repo.source_path.resolve(strict=False)
                or locked.base_ref != repo.base_ref
                or locked.base_commit != repo.base_commit
                or locked.default_selector != repo.default_selector
            ):
                raise WsError(f"removal seal terminal identity does not match lock for {name!r}")
    if state is not None:
        if lock is not None:
            try:
                validate_lock_state_consistency(lock, state)
            except SerializationError as exc:
                raise WsError(f"removal lock/state metadata is inconsistent: {exc}") from exc
        if state.workspace_name != seal.workspace_name or state.removal is None:
            raise WsError("removal seal does not match workspace state")
        if state.removal.phase != "removal_complete":
            raise WsError("sealed removal tombstone has an incomplete removal phase")
        if state.removal.seal is not None and state.removal.seal != seal:
            raise WsError("embedded and standalone removal seals do not match")
        if (
            set(state.repos) != set(seal.repos)
            or set(state.removal.repos) != set(seal.repos)
        ):
            raise WsError("removal seal repository identities do not match workspace state")
        for name, record in state.removal.repos.items():
            sealed = seal.repos[name]
            saved = state.repos[name]
            if (
                not record.complete
                or record.worktree_path.resolve(strict=False) != sealed.worktree_path.resolve(
                    strict=False
                )
                or record.git_admin_path.resolve(strict=False) != sealed.git_admin_path.resolve(
                    strict=False
                )
                or saved.mode != sealed.mode
                or saved.head != sealed.head
                or saved.branch != sealed.branch
                or saved.detached != sealed.detached
                or saved.dirty != sealed.dirty
            ):
                raise WsError(f"removal seal terminal identity does not match state for {name!r}")


def _verify_seal_identities(paths: WorkspacePaths, seal: RemovalSeal) -> None:
    _validate_seal_metadata(None, None, seal, paths)
    for name, repo in seal.repos.items():
        _check_removal_source(repo.source_path)
        common_dir = Path(
            run_git(["rev-parse", "--git-common-dir"], cwd=repo.source_path).stdout.strip()
        )
        if not common_dir.is_absolute():
            common_dir = repo.source_path / common_dir
        expected_admin_parent = common_dir.resolve(strict=False) / "worktrees"
        if repo.git_admin_path.resolve(strict=False).parent != expected_admin_parent:
            raise WsError(f"removal seal Git administrative identity does not match {name!r}")
        expected_worktree = (seal.workspace_path / "repos" / name).resolve(strict=False)
        if repo.worktree_path.resolve(strict=False) != expected_worktree:
            raise WsError(f"removal seal worktree identity does not match {name!r}")
        if _path_present(repo.worktree_path):
            raise WsError(f"removed worktree path remains for {name!r}: {repo.worktree_path}")
        if _path_present(repo.git_admin_path):
            raise WsError(
                f"Git administrative identity remains for {name!r}: {repo.git_admin_path}"
            )
        registered = [
            entry
            for entry in list_worktrees(repo.source_path)
            if entry.path.resolve(strict=False) == repo.worktree_path.resolve(strict=False)
        ]
        if registered:
            raise WsError(f"Git worktree registration remains for {name!r}")


def _resume_sealed_tombstone_cleanup(paths: WorkspacePaths, seal: RemovalSeal | None) -> None:
    _validate_tombstone_cleanup_shape(paths)
    if seal is None:
        if any(paths.workspace.iterdir()):
            raise WsError("removal tombstone without a seal is not empty")
        paths.workspace.rmdir()
        _fsync_directory_checked(paths.tombstone.parent)
        return
    _verify_seal_identities(paths, seal)
    _remove_if_present(paths.workspace / "repos", require_empty_directory=True)
    metadata_present = _path_present(paths.lock) or _path_present(paths.state)
    _remove_if_present(paths.lock, require_regular_file=True)
    _remove_if_present(paths.state, require_regular_file=True)
    if metadata_present:
        _removal_test_boundary("legacy_metadata_deleted")
    _remove_controlled_atomic_temps(paths)
    _release_and_remove_operation_lock(paths)
    _remove_if_present(paths.workspace / ".ws", require_empty_directory=True)
    _fsync_directory_checked(paths.tombstone)
    _remove_if_present(paths.removal_seal, require_regular_file=True)
    _fsync_directory_checked(paths.tombstone)
    _removal_test_boundary("seal_deleted")
    try:
        paths.tombstone.rmdir()
    except OSError as exc:
        raise WsError(
            f"cannot remove non-empty removal tombstone {paths.tombstone}: {exc}"
        ) from exc
    _fsync_directory_checked(paths.tombstone.parent)


def _validate_tombstone_cleanup_shape(paths: WorkspacePaths) -> None:
    if paths.workspace.is_symlink() or not paths.workspace.is_dir():
        raise WsError(f"removal tombstone is not a directory: {paths.workspace}")
    allowed_top = {".ws", "repos", "workspace.lock.toml", "removal-seal.toml"}
    for entry in paths.workspace.iterdir():
        if entry.name not in allowed_top and not _is_controlled_temp(entry.name, "root"):
            raise WsError(f"unexpected tombstone entry: {entry.name}")
        if entry.name in {".ws", "repos"}:
            if entry.is_symlink() or not entry.is_dir():
                raise WsError(f"unexpected tombstone entry: {entry.name}")
        elif entry.name in {"workspace.lock.toml", "removal-seal.toml"}:
            _require_regular(entry, "tombstone metadata")
        else:
            _require_regular(entry, "tombstone temporary metadata")
    repos = paths.workspace / "repos"
    if _path_present(repos) and any(repos.iterdir()):
        raise WsError("unexpected tombstone entry in repos")
    metadata = paths.workspace / ".ws"
    if not _path_present(metadata):
        return
    for entry in metadata.iterdir():
        if entry.name not in {"state.toml", "operation.lock"} and not _is_controlled_temp(
            entry.name, "state"
        ):
            raise WsError(f"unexpected tombstone metadata entry: {entry.name}")
        if entry.name == "operation.lock":
            if entry.is_symlink() or not entry.is_dir() or any(entry.iterdir()):
                raise WsError(f"removal operation lock is not an empty directory: {entry}")
        elif entry.name == "state.toml":
            _require_regular(entry, "tombstone metadata")
        else:
            _require_regular(entry, "tombstone temporary metadata")


def _remove_controlled_atomic_temps(paths: WorkspacePaths) -> None:
    for directory in (paths.tombstone, paths.workspace / ".ws"):
        if not directory.is_dir():
            continue
        kind = "root" if directory == paths.tombstone else "state"
        for entry in tuple(directory.iterdir()):
            if _is_controlled_temp(entry.name, kind):
                _require_regular(entry, "tombstone temporary metadata")
                entry.unlink()
                _fsync_directory_checked(directory)


def _is_controlled_temp(name: str, kind: str) -> bool:
    target = {
        "root": r"(?:workspace\.lock\.toml|removal-seal\.toml)",
        "state": r"state\.toml",
    }[kind]
    return re.fullmatch(rf"{target}\.[0-9a-f]{{32}}\.tmp", name) is not None


def _acquire_tombstone_operation_lock(paths: WorkspacePaths) -> bool:
    metadata = paths.workspace / ".ws"
    if not _path_present(metadata):
        return False
    if paths.operation_lock.is_symlink():
        raise WsError(f"removal operation lock is not a directory: {paths.operation_lock}")
    if paths.operation_lock.exists():
        raise WsError(f"workspace operation lock already exists: {paths.operation_lock}")
    try:
        paths.operation_lock.mkdir()
    except FileExistsError as exc:
        raise WsError(f"workspace operation lock already exists: {paths.operation_lock}") from exc
    return True


def _release_and_remove_operation_lock(paths: WorkspacePaths) -> None:
    _remove_if_present(paths.operation_lock, require_empty_directory=True)


def _read_metadata_file(path: Path, decoder):
    try:
        return decoder(path.read_text(encoding="utf-8"))
    except (OSError, SerializationError) as exc:
        raise WsError(f"cannot read workspace metadata: {exc}") from exc


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _require_regular(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise WsError(f"{label} is not a regular file: {path}")


def _remove_if_present(
    path: Path,
    *,
    require_empty_directory: bool = False,
    require_regular_file: bool = False,
) -> None:
    if not _path_present(path):
        return
    if path.is_symlink():
        raise WsError(f"refusing to remove symlink from tombstone: {path}")
    if require_empty_directory:
        if not path.is_dir() or any(path.iterdir()):
            raise WsError(f"expected an empty directory in removal tombstone: {path}")
        path.rmdir()
    elif require_regular_file:
        _require_regular(path, "tombstone metadata")
        path.unlink()
    else:
        raise WsError(f"cleanup has no allowlisted operation for {path}")
    _fsync_directory_checked(path.parent)


def _fsync_directory_checked(directory: Path) -> None:
    try:
        fsync_directory(directory)
    except OSError as exc:
        raise WsError(f"cannot fsync affected directory {directory}: {exc}") from exc


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
    operation_acquired = _acquire_operation_lock(paths)
    try:
        lock, state = _read_metadata(paths)
        _validate_discovered_workspace_name(paths, lock)
        _reject_context_mutation_during_removal(state)
        locked = _locked_repo(lock, repository_name)
        saved = state.repos[repository_name]
        if saved.context is not None:
            if saved.context.phase in {"entering", "restoring"}:
                raise WsError(
                    f"repository {repository_name!r} has an interrupted context transition "
                    f"({saved.context.phase}); manual recovery is required"
                )
            if saved.context.phase in {"restore_conflicted", "restore_failed"}:
                raise WsError(
                    f"repository {repository_name!r} has an unresolved context transition "
                    f"({saved.context.phase}); restore it before entering another context"
                )
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
                mode="context",
                head=live["head"],
                branch=live["branch"],
                dirty=False,
            )
            write_workspace_state(paths.state, current)
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


def restore_context(repository_name: str) -> str | None:
    validate_logical_name(repository_name, kind="repository")
    paths = discover_workspace()
    operation_acquired = _acquire_operation_lock(paths)
    try:
        lock, state = _read_metadata(paths)
        _validate_discovered_workspace_name(paths, lock)
        _reject_context_mutation_during_removal(state)
        locked = _locked_repo(lock, repository_name)
        saved = state.repos[repository_name]
        context = saved.context
        if context is None:
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
            mode="context",
            head=saved.head,
            branch=None,
            dirty=False,
        )
        write_workspace_state(paths.state, restoring)
        return_head = context.return_saved_head
        assert return_head is not None
        current = _context_effect_intent(
            paths,
            restoring,
            repository_name,
            "return_checkout",
            "intent",
            expected_refs={"HEAD": return_head},
            known_oids={"return": return_head},
        )
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
        restored = _set_context_state(
            current,
            repository_name,
            current.repos[repository_name],
            None,
            mode=context.return_mode,
            head=final_live["head"],
            branch=final_live["branch"],
            dirty=final_live["dirty"],
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


def finalize_restore(repository_name: str) -> str | None:
    validate_logical_name(repository_name, kind="repository")
    paths = discover_workspace()
    operation_acquired = _acquire_operation_lock(paths)
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
        finalized = _set_context_state(
            current,
            repository_name,
            current.repos[repository_name],
            None,
            mode=context.return_mode,
            head=live["head"],
            branch=live["branch"],
            dirty=live["dirty"],
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


def _acquire_operation_lock(paths: WorkspacePaths) -> bool:
    try:
        paths.operation_lock.mkdir()
    except FileExistsError as exc:
        raise WsError(f"workspace operation lock already exists: {paths.operation_lock}") from exc
    return True


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
    context: ContextState | None,
    *,
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
    return replace(state, repos={**state.repos, repository_name: repo})


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
    _, latest = _read_metadata(paths)
    saved = latest.repos[repository_name]
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
        latest, repos={**latest.repos, repository_name: replace(saved, context=updated_context)}
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
    base_paths = _discover_removal_paths(start)
    if base_paths is None:
        current = (Path.cwd() if start is None else start).expanduser().resolve()
        raise WsError(f"no workspace found above {current}")
    paths = _existing_removal_paths(base_paths)
    lock, state = _read_metadata(paths)
    if lock.workspace_name != base_paths.workspace.name:
        raise WsError(
            f"workspace name in lock {lock.workspace_name!r} does not match directory "
            f"{base_paths.workspace.name!r}"
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
        "repo       base             default          HEAD      mode      "
        "branch              dirty  context"
    )
    lines.append("-" * 100)
    for name, repo in payload["repos"].items():
        head = str(repo["head"])[:12]
        branch = repo["branch"] or "-"
        default = repo["locked_default_selector"] or "-"
        if repo["context"] is None:
            context = "-"
        else:
            context = repo["context"]["target_ref"]
            if repo["context"]["phase"] not in (None, "inactive", "none"):
                context = f"{context} ({repo['context']['phase']})"
        lines.append(
            f"{name:<10} {str(repo['locked_base_ref']):<16} {default:<17} {head:<11} "
            f"{repo['mode']:<9} {branch:<19} "
            f"{'yes' if repo['dirty'] else 'no':<6} {context}"
        )
    removal = payload.get("removal")
    if removal is not None:
        completed = len(removal["completed"])
        total = len(payload["repos"])
        lines.append("")
        lines.append(
            f"REMOVAL {removal['phase']} ({completed}/{total} repos removed)"
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
        removal_seal=workspace / "removal-seal.toml",
    )


def _resolve_creation_plans(
    repos: dict[str, Any], overrides: dict[str, str], global_default_ref: str | None
) -> list[_CreationPlan]:
    plans: list[_CreationPlan] = []
    for name, repo in repos.items():
        override = overrides.get(name)
        try:
            selector = _effective_default_selector(
                repo.source_path, repo.default_ref, global_default_ref
            )
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


def _effective_default_selector(
    source: Path, repo_default: str | None, global_default: str | None
) -> str | None:
    """Resolve the effective default selector using hierarchical precedence.

    Precedence:
    1. Per-repository default_ref if configured
    2. Global default_ref if configured
    3. Automatic discovery (remote symbolic HEAD or checked-out branch)

    Automatic discovery is skipped when either configuration level supplied
    a default_ref.
    """
    # Precedence 1: per-repository default_ref
    if repo_default is not None:
        return repo_default
    # Precedence 2: global default_ref
    if global_default is not None:
        return global_default
    # Precedence 3: automatic discovery (only when neither config level supplied default_ref)
    return _auto_default_selector(source)


def _auto_default_selector(source: Path) -> str | None:
    """Automatically discover a default selector from the source repository.

    Uses remote symbolic HEAD if unambiguous, otherwise falls back to the
    currently checked-out branch of a non-bare repository.
    """
    remote_selector = _remote_symbolic_selector(source)
    if remote_selector is not None:
        return remote_selector
    if repository_kind(source) == "worktree":
        current = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=source, check=False)
        if current.returncode == 0 and current.stdout.strip():
            return current.stdout.strip()
    return None


def _remote_symbolic_selector(source: Path) -> str | None:
    heads = remote_symbolic_heads(source)
    if not heads:
        return None
    if len({commit for _head, _target, commit in heads}) != 1:
        raise ConfigError(f"remote symbolic HEADs disagree for source {source}")
    return min(target for _head, target, _commit in heads)


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
