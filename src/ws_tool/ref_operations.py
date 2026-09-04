from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import GitCommandError, WsError
from .git import GitResult, run_git

Operation = Literal["switch", "merge"]

_OPERATION_MARKERS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "REBASE_HEAD",
    "BISECT_LOG",
    "rebase-merge",
    "rebase-apply",
)


@dataclass(frozen=True)
class RefOperationTarget:
    name: str
    worktree: Path


@dataclass(frozen=True)
class RefOperationResult:
    name: str
    head: str
    branch: str | None


@dataclass(frozen=True)
class _Snapshot:
    target: RefOperationTarget
    original_head: str
    original_branch: str | None
    target_commit: str


class _PostMutationFailure(WsError):
    """A post-mutation workspace-state callback failed."""


def execute_ref_operation(
    operation: Operation,
    target_ref: str,
    targets: Sequence[RefOperationTarget],
    *,
    post_mutation: Callable[[], None] | None = None,
) -> tuple[RefOperationResult, ...]:
    """Run a ref operation over clean, independently preflighted worktrees."""

    _validate_request(operation, target_ref, targets)
    snapshots = tuple(_preflight(operation, target_ref, target) for target in targets)

    started: list[_Snapshot] = []
    current_target = "target"
    try:
        for snapshot in snapshots:
            current_target = snapshot.target.name
            started.append(snapshot)
            if operation == "switch":
                result = run_git(
                    ["switch", "--detach", snapshot.target_commit],
                    cwd=snapshot.target.worktree,
                )
            else:
                result = run_git(["merge", snapshot.target_commit], cwd=snapshot.target.worktree)
            _require_success(result, f"{operation} command")

        results_list: list[RefOperationResult] = []
        for snapshot in snapshots:
            current_target = snapshot.target.name
            results_list.append(_read_result(snapshot.target))
        if post_mutation is not None:
            try:
                post_mutation()
            except (GitCommandError, WsError, OSError, ValueError) as exc:
                raise _PostMutationFailure(f"workspace-state persistence failed: {exc}") from exc
        results = tuple(results_list)
    except (GitCommandError, WsError, OSError, ValueError) as exc:
        if isinstance(exc, _PostMutationFailure):
            primary = str(exc)
        else:
            primary = f"{operation} {current_target}: {exc}"
        diagnostics = _rollback(operation, reversed(started))
        if diagnostics:
            detail = "; ".join(diagnostics)
            raise WsError(f"{primary}; rollback diagnostics: {detail}") from exc
        raise WsError(f"{primary}; rollback completed") from exc

    return results


def _validate_request(
    operation: Operation,
    target_ref: str,
    targets: Sequence[RefOperationTarget],
) -> None:
    if operation not in {"switch", "merge"}:
        raise WsError(f"preflight target: unsupported operation {operation!r}")
    if not target_ref.strip():
        raise WsError("preflight target: target ref must not be blank")

    names: set[str] = set()
    for target in targets:
        if not target.name.strip():
            raise WsError("preflight target: repository name must not be blank")
        if target.name in names:
            raise WsError(f"preflight {target.name}: duplicate target name")
        names.add(target.name)


def _preflight(
    operation: Operation,
    target_ref: str,
    target: RefOperationTarget,
) -> _Snapshot:
    try:
        status = _checked_git(
            ["status", "--porcelain", "--untracked-files=all"], cwd=target.worktree
        )
        if status.stdout.strip():
            raise ValueError("worktree is dirty")

        unmerged = _checked_git(["ls-files", "--unmerged"], cwd=target.worktree)
        if unmerged.stdout.strip():
            raise ValueError("worktree has unmerged entries")

        head = _checked_git(["rev-parse", "HEAD"], cwd=target.worktree).stdout.strip()
        if not head:
            raise ValueError("worktree HEAD is empty")

        symbolic = run_git(
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=target.worktree,
            check=False,
        )
        branch = symbolic.stdout.strip() if symbolic.returncode == 0 else None
        if operation == "merge" and branch is None:
            raise ValueError("merge target must have an attached branch")

        target_commit = _checked_git(
            ["rev-parse", "--verify", f"{target_ref}^{{commit}}"], cwd=target.worktree
        ).stdout.strip()
        if not target_commit:
            raise ValueError(f"ref {target_ref!r} resolved to an empty commit")

        _check_operation_markers(target)
    except (GitCommandError, WsError, OSError, ValueError) as exc:
        raise WsError(f"preflight {target.name}: {exc}") from exc

    return _Snapshot(target, head, branch, target_commit)


def _check_operation_markers(target: RefOperationTarget) -> None:
    for marker in _OPERATION_MARKERS:
        marker_path = _operation_marker_path(target, marker)
        if marker_path.exists():
            raise ValueError(f"Git operation is in progress ({marker})")


def _operation_marker_path(target: RefOperationTarget, marker: str) -> Path:
    result = _checked_git(["rev-parse", "--git-path", marker], cwd=target.worktree)
    marker_output = result.stdout.strip()
    if not marker_output:
        raise ValueError(f"Git marker path for {marker} is empty")
    marker_path = Path(marker_output)
    if not marker_path.is_absolute():
        marker_path = target.worktree / marker_path
    return marker_path


def _read_result(target: RefOperationTarget) -> RefOperationResult:
    head = _checked_git(["rev-parse", "HEAD"], cwd=target.worktree).stdout.strip()
    symbolic = run_git(
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=target.worktree,
        check=False,
    )
    branch = symbolic.stdout.strip() if symbolic.returncode == 0 else None
    if not head:
        raise ValueError("post-operation HEAD is empty")
    return RefOperationResult(target.name, head, branch)


def _rollback(
    operation: Operation,
    snapshots: Iterable[_Snapshot],
) -> list[str]:
    diagnostics: list[str] = []
    for snapshot in snapshots:
        name = snapshot.target.name
        worktree = snapshot.target.worktree
        attempts: list[str] = []
        if operation == "merge" and _merge_in_progress(snapshot, attempts):
            try:
                aborted = run_git(["merge", "--abort"], cwd=worktree, check=False)
                if aborted.returncode != 0:
                    attempts.append(
                        f"rollback {name}: merge --abort returned {aborted.returncode}"
                    )
            except (GitCommandError, WsError, OSError, ValueError) as exc:
                attempts.append(f"rollback {name}: merge --abort failed: {exc}")

        original_branch = snapshot.original_branch
        if original_branch is None:
            _rollback_command(
                attempts,
                name,
                "switch --detach",
                lambda: _checked_git(
                    ["switch", "--detach", snapshot.original_head], cwd=worktree
                ),
            )
        else:
            assert original_branch is not None
            branch = original_branch
            _rollback_command(
                attempts,
                name,
                f"switch {branch}",
                lambda: _checked_git(["switch", branch], cwd=worktree),
            )
            _rollback_command(
                attempts,
                name,
                f"reset --hard {snapshot.original_head}",
                lambda: _checked_git(
                    ["reset", "--hard", snapshot.original_head], cwd=worktree
                ),
            )

        verification = _verify_snapshot(snapshot)
        if verification:
            diagnostics.extend(attempts)
            diagnostics.extend(
                f"rollback {name}: verification failed: {issue}" for issue in verification
            )
    return diagnostics


def _merge_in_progress(snapshot: _Snapshot, attempts: list[str]) -> bool:
    try:
        return _operation_marker_path(snapshot.target, "MERGE_HEAD").exists()
    except (GitCommandError, WsError, OSError, ValueError) as exc:
        attempts.append(f"rollback {snapshot.target.name}: could not check merge state: {exc}")
        return False


def _rollback_command(
    diagnostics: list[str], name: str, description: str, command: Callable[[], GitResult]
) -> None:
    try:
        command()
    except (GitCommandError, WsError, OSError, ValueError) as exc:
        diagnostics.append(f"rollback {name}: {description} failed: {exc}")


def _verify_snapshot(snapshot: _Snapshot) -> tuple[str, ...]:
    worktree = snapshot.target.worktree
    issues: list[str] = []
    try:
        symbolic = run_git(
            ["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=worktree, check=False
        )
        branch = symbolic.stdout.strip() if symbolic.returncode == 0 else None
        if branch != snapshot.original_branch:
            expected = snapshot.original_branch or "detached HEAD"
            issues.append(f"expected {expected}, found {branch or 'detached HEAD'}")

        try:
            head = _checked_git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
            if head != snapshot.original_head:
                issues.append(f"expected HEAD {snapshot.original_head}, found {head}")
        except (GitCommandError, WsError, OSError, ValueError) as exc:
            issues.append(f"could not read HEAD: {exc}")

        try:
            status = _checked_git(
                ["status", "--porcelain", "--untracked-files=all"], cwd=worktree
            )
            if status.stdout.strip():
                issues.append("worktree is not clean after rollback")
        except (GitCommandError, WsError, OSError, ValueError) as exc:
            issues.append(f"could not read worktree status: {exc}")
    except (GitCommandError, WsError, OSError, ValueError) as exc:
        issues.append(str(exc))
    return tuple(issues)


def _checked_git(args: Sequence[str], *, cwd: Path) -> GitResult:
    result = run_git(args, cwd=cwd)
    _require_success(result, "Git command")
    return result


def _require_success(result: GitResult, description: str) -> None:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise ValueError(f"{description} failed (exit {result.returncode}): {detail}")


__all__ = [
    "Operation",
    "RefOperationResult",
    "RefOperationTarget",
    "execute_ref_operation",
]
