from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError, GitCommandError, GitWorktreeError

_GIT_ROUTING_ENVIRONMENT = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_OBJECT_DIRECTORY_RELATIVE",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_QUARANTINE_PATH",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_PREFIX",
    }
)


@dataclass(frozen=True)
class GitResult:
    args: tuple[str, ...]
    cwd: Path | None
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class WorktreeEntry:
    path: Path
    head: str | None
    branch: str | None


@dataclass(frozen=True)
class WorktreeIdentity:
    path: Path
    git_admin_path: Path
    source_git_common_dir: Path


RemoteSymbolicHead = tuple[str, str, str]


def run_git(
    args: Iterable[str | Path],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> GitResult:
    command_args = tuple(str(arg) for arg in args)
    environment = os.environ.copy()
    for variable in _GIT_ROUTING_ENVIRONMENT:
        environment.pop(variable, None)
    try:
        completed = subprocess.run(
            ["git", *command_args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
    except OSError as exc:
        raise GitCommandError(command_args, cwd, -1, "", str(exc)) from exc

    result = GitResult(
        args=command_args,
        cwd=cwd,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    if check and result.returncode != 0:
        raise GitCommandError(
            command_args,
            cwd,
            result.returncode,
            result.stdout,
            result.stderr,
        )
    return result


def remote_symbolic_heads(source: Path) -> Sequence[RemoteSymbolicHead]:
    """Return remote symbolic HEADs and the commits to which they resolve."""

    try:
        result = run_git(
            [
                "for-each-ref",
                "--format=%(refname)%09%(symref)",
                "refs/remotes",
            ],
            cwd=source,
        )
    except GitCommandError as exc:
        raise ConfigError(f"cannot enumerate remote symbolic HEADs in {source}: {exc}") from exc

    heads: list[RemoteSymbolicHead] = []
    for row in result.stdout.splitlines():
        try:
            head_ref, target_ref = row.split("\t", 1)
        except ValueError as exc:
            raise ConfigError(
                f"malformed remote symbolic HEAD output in {source}: {row!r}"
            ) from exc
        if not head_ref.endswith("/HEAD") or not target_ref:
            continue
        try:
            resolved = run_git(
                ["rev-parse", "--verify", f"{target_ref}^{{commit}}"],
                cwd=source,
            )
        except GitCommandError as exc:
            raise ConfigError(
                f"remote symbolic HEAD {head_ref} target {target_ref!r} "
                f"does not resolve to a commit in {source}: {exc}"
            ) from exc
        commit_oid = resolved.stdout.strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit_oid) is None:
            raise ConfigError(
                f"remote symbolic HEAD {head_ref} target {target_ref!r} "
                f"resolved to malformed commit ID in {source}: {commit_oid!r}"
            )
        heads.append((head_ref, target_ref, commit_oid))
    return tuple(heads)


def create_private_ref(ref: str, oid: str, *, cwd: Path | None = None) -> None:
    _validate_private_ref(ref)
    _validate_oid(oid)
    run_git(["update-ref", "--no-deref", ref, oid, "0" * len(oid)], cwd=cwd)


def delete_private_ref(ref: str, expected_oid: str, *, cwd: Path | None = None) -> None:
    _validate_private_ref(ref)
    _validate_oid(expected_oid)
    run_git(["update-ref", "--no-deref", "-d", ref, expected_oid], cwd=cwd)


def _validate_private_ref(ref: str) -> None:
    if not ref.startswith("refs/ws/") or ref.endswith("/") or ".." in ref:
        raise ValueError(f"private Git ref must be under refs/ws/: {ref!r}")


def _validate_oid(oid: str) -> None:
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", oid) is None:
        raise ValueError(f"invalid Git object ID: {oid!r}")


def repository_kind(path: Path) -> str | None:
    """Return ``bare`` or ``worktree`` only for a repository root."""

    if not path.is_dir():
        return None
    probe = run_git(["rev-parse", "--git-dir"], cwd=path, check=False)
    if probe.returncode != 0:
        return None
    bare = run_git(["rev-parse", "--is-bare-repository"], cwd=path, check=False)
    if bare.returncode == 0 and bare.stdout.strip() == "true":
        return "bare"
    top = run_git(["rev-parse", "--show-toplevel"], cwd=path, check=False)
    if top.returncode == 0 and Path(top.stdout.strip()).resolve() == path.resolve():
        return "worktree"
    return None


def is_git_repository(path: Path) -> bool:
    return repository_kind(path) is not None


def list_worktrees(source: Path) -> tuple[WorktreeEntry, ...]:
    result = run_git(["worktree", "list", "--porcelain", "-z"], cwd=source)
    entries: list[WorktreeEntry] = []
    current: dict[str, str] = {}
    for record in (*result.stdout.split("\0"), ""):
        if not record:
            if "worktree" in current:
                entries.append(
                    WorktreeEntry(
                        path=Path(current["worktree"]).resolve(strict=False),
                        head=current.get("HEAD"),
                        branch=current.get("branch"),
                    )
                )
            current = {}
            continue
        key, _, value = record.partition(" ")
        if key == "branch":
            current[key] = value.removeprefix("refs/heads/")
        elif key in {"worktree", "HEAD"}:
            current[key] = value
    return tuple(entries)


def worktree_admin_path(worktree: Path) -> Path:
    result = run_git(["rev-parse", "--git-dir"], cwd=worktree)
    admin = Path(result.stdout.strip())
    if not admin.is_absolute():
        admin = worktree / admin
    return admin.resolve(strict=False)


def validate_worktree_registration(source: Path, expected_path: Path) -> WorktreeIdentity:
    expected = expected_path.resolve(strict=False)
    entry = next((item for item in list_worktrees(source) if item.path == expected), None)
    if entry is None:
        raise GitWorktreeError(
            f"expected worktree is not registered by source {source}: {expected}"
        )
    admin = worktree_admin_path(expected)
    source_common_result = run_git(["rev-parse", "--git-common-dir"], cwd=source)
    source_common_dir = Path(source_common_result.stdout.strip())
    if not source_common_dir.is_absolute():
        source_common_dir = source / source_common_dir
    source_common_dir = source_common_dir.resolve(strict=False)
    expected_admin_parent = source_common_dir / "worktrees"
    if admin.parent != expected_admin_parent:
        raise GitWorktreeError(
            f"Git administrative identity for {expected} is outside source worktrees: {admin}"
        )
    gitdir_file = admin / "gitdir"
    try:
        gitdir = Path(gitdir_file.read_text(encoding="utf-8").strip())
    except OSError as exc:
        raise GitWorktreeError(
            f"cannot read Git administrative identity for {expected}: {gitdir_file}"
        ) from exc
    if not gitdir.is_absolute():
        gitdir = admin / gitdir
    if gitdir.resolve(strict=False) != (expected / ".git").resolve(strict=False):
        raise GitWorktreeError(
            f"Git administrative identity does not point to expected worktree {expected}"
        )
    return WorktreeIdentity(expected, admin, source_common_dir)
