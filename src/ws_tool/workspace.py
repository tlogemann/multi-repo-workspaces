from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .config import load_config, parse_source_overrides
from .errors import ConfigError, GitWorktreeError, SerializationError, WsError
from .git import repository_kind, run_git, validate_worktree_registration
from .models import (
    CONTEXT_PHASES,
    REMOVAL_PHASES,
    RepoState,
    WorkspaceLock,
    WorkspaceLockRepo,
    WorkspaceState,
)
from .serialization import (
    deserialize_workspace_lock,
    deserialize_workspace_state,
    validate_lock_state_consistency,
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
