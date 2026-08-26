from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .errors import GitCommandError

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
