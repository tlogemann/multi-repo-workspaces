from __future__ import annotations

import copy
import errno
import json
import os
import re
import tomllib
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from .errors import SerializationError
from .models import (
    CONTEXT_PHASES,
    REMOVAL_PHASES,
    REMOVAL_SEAL_SCHEMA_VERSION,
    REPO_MODES,
    WORKSPACE_LOCK_SCHEMA_VERSION,
    WORKSPACE_PHASES,
    WORKSPACE_STATE_SCHEMA_VERSION,
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
from .validation import InvalidLogicalName, validate_logical_name

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+\Z")


def dump_toml(data: Mapping[str, Any]) -> str:
    lines: list[str] = []
    _render_table(data, (), lines)
    return "\n".join(lines) + ("\n" if lines else "")


def write_toml(
    path: Path,
    data: Mapping[str, Any],
    *,
    replace: Callable[[Path, Path], None] | None = None,
    fsync_directory: Callable[[Path], None] | None = None,
) -> None:
    _write_atomic_text(
        path,
        dump_toml(data),
        replace=replace,
        fsync_directory=fsync_directory,
    )


def write_workspace_lock(
    path: Path,
    lock: WorkspaceLock,
    *,
    replace: Callable[[Path, Path], None] | None = None,
    fsync_directory: Callable[[Path], None] | None = None,
) -> None:
    _write_atomic_text(
        path,
        serialize_workspace_lock(lock),
        replace=replace,
        fsync_directory=fsync_directory,
    )


def write_workspace_state(
    path: Path,
    state: WorkspaceState,
    *,
    replace: Callable[[Path, Path], None] | None = None,
    fsync_directory: Callable[[Path], None] | None = None,
) -> None:
    _write_atomic_text(
        path,
        serialize_workspace_state(state),
        replace=replace,
        fsync_directory=fsync_directory,
    )


def write_removal_seal(
    path: Path,
    seal: RemovalSeal,
    *,
    replace: Callable[[Path, Path], None] | None = None,
    fsync_directory: Callable[[Path], None] | None = None,
) -> None:
    _write_atomic_text(
        path,
        serialize_removal_seal(seal),
        replace=replace,
        fsync_directory=fsync_directory,
    )


def _write_atomic_text(
    path: Path,
    content: str,
    *,
    replace: Callable[[Path, Path], None] | None,
    fsync_directory: Callable[[Path], None] | None,
) -> None:
    path = path.resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        replace_fn = os.replace if replace is None else replace
        try:
            replace_fn(temporary, path)
        except OSError as exc:
            raise SerializationError(f"cannot atomically replace metadata {path}: {exc}") from exc
        sync_fn = _fsync_directory if fsync_directory is None else fsync_directory
        try:
            sync_fn(path.parent)
        except OSError as exc:
            raise SerializationError(
                f"cannot fsync metadata directory {path.parent}: {exc}"
            ) from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def fsync_directory(directory: Path) -> None:
    unsupported = {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        if exc.errno in unsupported:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(descriptor)


# Kept as an alias for removal code that uses the existing private seam.
_fsync_directory = fsync_directory


def serialize_workspace_lock(lock: WorkspaceLock) -> str:
    _require_schema(lock.schema_version, WORKSPACE_LOCK_SCHEMA_VERSION, "workspace lock")
    _validate_lock(lock)
    return dump_toml(
        {
            "schema_version": lock.schema_version,
            "workspace": {"name": lock.workspace_name},
            "repos": {
                name: {
                    "name": repo.name,
                    "source_path": str(repo.source_path),
                    "base_ref": repo.base_ref,
                    "base_commit": repo.base_commit,
                    "default_selector": repo.default_selector,
                }
                for name, repo in lock.repos.items()
            },
        }
    )


def deserialize_workspace_lock(text: str) -> WorkspaceLock:
    raw = _parse_toml(text)
    schema_version = _schema_version(raw, "workspace lock")
    _require_schema(schema_version, WORKSPACE_LOCK_SCHEMA_VERSION, "workspace lock")
    workspace = _table(raw.get("workspace"), "[workspace]")
    repos = _table(raw.get("repos"), "[repos]")
    name = _string(workspace.get("name"), "[workspace].name")
    parsed_repos: dict[str, WorkspaceLockRepo] = {}
    for repo_name, value in repos.items():
        repo = _table(value, f"[repos.{repo_name}]")
        embedded_name = _string(repo.get("name"), f"[repos.{repo_name}].name")
        if embedded_name != repo_name:
            raise SerializationError(
                f"[repos.{repo_name}].name must match repository mapping key {repo_name!r}"
            )
        parsed_repos[repo_name] = WorkspaceLockRepo(
            name=embedded_name,
            source_path=Path(_string(repo.get("source_path"), f"[repos.{repo_name}].source_path")),
            base_ref=_string(repo.get("base_ref"), f"[repos.{repo_name}].base_ref"),
            base_commit=_string(repo.get("base_commit"), f"[repos.{repo_name}].base_commit"),
            default_selector=_optional_string(
                repo.get("default_selector"), f"[repos.{repo_name}].default_selector"
            ),
        )
    lock = WorkspaceLock(name, parsed_repos, schema_version)
    _validate_lock(lock)
    return lock


def serialize_removal_seal(seal: RemovalSeal) -> str:
    _require_schema(seal.schema_version, REMOVAL_SEAL_SCHEMA_VERSION, "removal seal")
    _validate_seal(seal, "[seal]")
    return dump_toml(
        {
            "schema_version": seal.schema_version,
            "seal": _removal_seal_data(seal),
        }
    )


def deserialize_removal_seal(text: str) -> RemovalSeal:
    raw = _parse_toml(text)
    _reject_unexpected_keys(raw, {"schema_version", "seal"}, "removal seal")
    schema_version = _schema_version(raw, "removal seal")
    _require_schema(schema_version, REMOVAL_SEAL_SCHEMA_VERSION, "removal seal")
    seal = _parse_removal_seal(_table(raw.get("seal"), "[seal]"), "[seal]", schema_version)
    _validate_seal(seal, "[seal]")
    return seal


def serialize_workspace_state(state: WorkspaceState) -> str:
    _require_schema(state.schema_version, WORKSPACE_STATE_SCHEMA_VERSION, "workspace state")
    _validate_state(state)
    if (
        state.removal is not None
        and state.removal.phase == "removal_complete"
        and state.removal.seal is None
    ):
        raise SerializationError("removal_complete state requires a removal seal")
    state_data: dict[str, Any] = {
        "phase": state.phase,
        "repos": {name: _repo_state_data(repo_state) for name, repo_state in state.repos.items()},
    }
    if state.removal is not None:
        removal_data: dict[str, Any] = {
            "phase": state.removal.phase,
            "workspace_path": str(state.removal.workspace_path),
            "tombstone_path": str(state.removal.tombstone_path),
            "repos": {
                name: {
                    "name": repo.name,
                    "worktree_path": str(repo.worktree_path),
                    "git_admin_path": str(repo.git_admin_path),
                    "complete": repo.complete,
                }
                for name, repo in state.removal.repos.items()
            },
        }
        if state.removal.phase == "removal_complete" and state.removal.seal is not None:
            removal_data["seal"] = _removal_seal_data(state.removal.seal)
        state_data["removal"] = removal_data
    return dump_toml(
        {
            "schema_version": state.schema_version,
            "workspace": {"name": state.workspace_name},
            "state": state_data,
        }
    )


def deserialize_workspace_state(text: str) -> WorkspaceState:
    raw = _parse_toml(text)
    schema_version = _schema_version(raw, "workspace state")
    legacy_completed_without_seal = False
    if schema_version == 1:
        legacy_state = _table(raw.get("state"), "[state]")
        legacy_removal = legacy_state.get("removal")
        if legacy_removal is not None:
            legacy_removal_table = _table(legacy_removal, "[state.removal]")
            legacy_completed_without_seal = (
                legacy_removal_table.get("phase") == "removal_complete"
                and "seal" not in legacy_removal_table
            )
    if schema_version == 1:
        raw = _migrate_workspace_state_v1(raw)
        schema_version = WORKSPACE_STATE_SCHEMA_VERSION
    else:
        _require_schema(schema_version, WORKSPACE_STATE_SCHEMA_VERSION, "workspace state")
    workspace = _table(raw.get("workspace"), "[workspace]")
    state = _table(raw.get("state"), "[state]")
    repos = _table(state.get("repos"), "[state.repos]")
    parsed_repos = {
        repo_name: _parse_repo_state(value, f"[state.repos.{repo_name}]")
        for repo_name, value in repos.items()
    }
    removal_value = state.get("removal")
    removal = None
    if removal_value is not None:
        removal_table = _table(removal_value, "[state.removal]")
        removal_repos = _table(removal_table.get("repos"), "[state.removal.repos]")
        parsed_removal_repos = {
            repo_name: _parse_removal_repo(value, f"[state.removal.repos.{repo_name}]")
            for repo_name, value in removal_repos.items()
        }
        seal_value = removal_table.get("seal")
        seal = (
            _parse_removal_seal(
                _table(seal_value, "[state.removal.seal]"),
                "[state.removal.seal]",
                REMOVAL_SEAL_SCHEMA_VERSION,
            )
            if seal_value is not None
            else None
        )
        removal = RemovalState(
            phase=_string(removal_table.get("phase"), "[state.removal].phase"),
            workspace_path=Path(
                _string(removal_table.get("workspace_path"), "[state.removal].workspace_path")
            ),
            tombstone_path=Path(
                _string(removal_table.get("tombstone_path"), "[state.removal].tombstone_path")
            ),
            repos=parsed_removal_repos,
            seal=seal,
            _legacy_completed_without_seal=legacy_completed_without_seal,
        )
    parsed_state = WorkspaceState(
        workspace_name=_string(workspace.get("name"), "[workspace].name"),
        phase=_string(state.get("phase"), "[state].phase"),
        repos=parsed_repos,
        removal=removal,
        # Keep this one legacy shape identifiable to validation while lock/state
        # metadata is still available for the removal retry to upgrade it.
        schema_version=schema_version,
    )
    _validate_state(parsed_state)
    return parsed_state


def _migrate_workspace_state_v1(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate legacy global context state without discarding repository context."""

    migrated = copy.deepcopy(raw)
    state = _table(migrated.get("state"), "[state]")
    migrated["schema_version"] = WORKSPACE_STATE_SCHEMA_VERSION
    if state.get("phase") in CONTEXT_PHASES:
        state["phase"] = "idle"
    return migrated


def _repo_state_data(repo_state: RepoState) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": repo_state.name,
        "mode": repo_state.mode,
        "head": repo_state.head,
        "branch": repo_state.branch,
        "detached": repo_state.detached,
        "dirty": repo_state.dirty,
    }
    if repo_state.context is not None:
        data["context"] = {
            "target_ref": repo_state.context.target_ref,
            "target_commit": repo_state.context.target_commit,
            "phase": repo_state.context.phase,
            "return_mode": repo_state.context.return_mode,
            "stash_token": repo_state.context.stash_token,
            "return_branch": repo_state.context.return_branch,
            "return_saved_head": repo_state.context.return_saved_head,
            "stash_oid": repo_state.context.stash_oid,
            "private_ref": repo_state.context.private_ref,
            "completed_effects": {
                str(index): {
                    "operation": effect.operation,
                    "step": effect.step,
                    "expected_refs": effect.expected_refs,
                    "known_oids": effect.known_oids,
                }
                for index, effect in enumerate(repo_state.context.completed_effects)
            },
        }
    return data


def _removal_seal_data(seal: RemovalSeal) -> dict[str, Any]:
    return {
        "workspace_name": seal.workspace_name,
        "workspace_path": str(seal.workspace_path),
        "tombstone_path": str(seal.tombstone_path),
        "phase": seal.phase,
        "repos": {
            name: {
                "name": repo.name,
                "source_path": str(repo.source_path),
                "worktree_path": str(repo.worktree_path),
                "git_admin_path": str(repo.git_admin_path),
                "base_ref": repo.base_ref,
                "base_commit": repo.base_commit,
                "default_selector": repo.default_selector,
                "mode": repo.mode,
                "head": repo.head,
                "branch": repo.branch,
                "detached": repo.detached,
                "dirty": repo.dirty,
            }
            for name, repo in seal.repos.items()
        },
    }


def _parse_repo_state(value: Any, label: str) -> RepoState:
    repo = _table(value, label)
    context_value = repo.get("context")
    context = None
    if context_value is not None:
        context_table = _table(context_value, f"{label}.context")
        context = ContextState(
            target_ref=_string(context_table.get("target_ref"), f"{label}.context.target_ref"),
            target_commit=_string(
                context_table.get("target_commit"), f"{label}.context.target_commit"
            ),
            phase=_string(context_table.get("phase"), f"{label}.context.phase"),
            return_mode=_string(context_table.get("return_mode"), f"{label}.context.return_mode"),
            stash_token=_string(context_table.get("stash_token"), f"{label}.context.stash_token"),
            return_branch=_optional_string(
                context_table.get("return_branch"), f"{label}.context.return_branch"
            ),
            return_saved_head=_optional_string(
                context_table.get("return_saved_head"), f"{label}.context.return_saved_head"
            ),
            stash_oid=_optional_string(
                context_table.get("stash_oid"), f"{label}.context.stash_oid"
            ),
            private_ref=_optional_string(
                context_table.get("private_ref"), f"{label}.context.private_ref"
            ),
            completed_effects=_parse_effects(
                context_table.get("completed_effects", {}), f"{label}.context.completed_effects"
            ),
        )
    return RepoState(
        name=_string(repo.get("name"), f"{label}.name"),
        mode=_string(repo.get("mode"), f"{label}.mode"),
        head=_optional_string(repo.get("head"), f"{label}.head"),
        branch=_optional_string(repo.get("branch"), f"{label}.branch"),
        detached=_bool(repo.get("detached"), f"{label}.detached"),
        dirty=_bool(repo.get("dirty"), f"{label}.dirty"),
        context=context,
    )


def _parse_effects(value: Any, label: str) -> tuple[ContextSideEffect, ...]:
    table = _table(value, label)
    effects: list[ContextSideEffect] = []
    for key in sorted(table, key=_effect_index):
        effect = _table(table[key], f"{label}.{key}")
        effects.append(
            ContextSideEffect(
                operation=_string(effect.get("operation"), f"{label}.{key}.operation"),
                step=_string(effect.get("step"), f"{label}.{key}.step"),
                expected_refs=_string_map(
                    effect.get("expected_refs", {}), f"{label}.{key}.expected_refs"
                ),
                known_oids=_string_map(effect.get("known_oids", {}), f"{label}.{key}.known_oids"),
            )
        )
    return tuple(effects)


def _effect_index(value: str) -> int:
    if not value.isdigit():
        raise SerializationError(f"completed effect key must be numeric: {value!r}")
    return int(value)


def _parse_removal_repo(value: Any, label: str) -> RemovalRepoState:
    repo = _table(value, label)
    return RemovalRepoState(
        name=_string(repo.get("name"), f"{label}.name"),
        worktree_path=Path(_string(repo.get("worktree_path"), f"{label}.worktree_path")),
        git_admin_path=Path(_string(repo.get("git_admin_path"), f"{label}.git_admin_path")),
        complete=_bool(repo.get("complete"), f"{label}.complete"),
    )


def _parse_removal_seal(
    value: dict[str, Any], label: str, schema_version: int = REMOVAL_SEAL_SCHEMA_VERSION
) -> RemovalSeal:
    _reject_unexpected_keys(
        value,
        {"workspace_name", "workspace_path", "tombstone_path", "phase", "repos"},
        label,
    )
    repos = _table(value.get("repos"), f"{label}.repos")
    parsed_repos: dict[str, RemovalSealRepo] = {}
    for repo_name, repo_value in repos.items():
        repo = _table(repo_value, f"{label}.repos.{repo_name}")
        _reject_unexpected_keys(
            repo,
            {
                "name",
                "source_path",
                "worktree_path",
                "git_admin_path",
                "base_ref",
                "base_commit",
                "default_selector",
                "mode",
                "head",
                "branch",
                "detached",
                "dirty",
            },
            f"{label}.repos.{repo_name}",
        )
        embedded_name = _string(repo.get("name"), f"{label}.repos.{repo_name}.name")
        if embedded_name != repo_name:
            raise SerializationError(
                f"{label}.repos.{repo_name}.name must match repository mapping key {repo_name!r}"
            )
        parsed_repos[repo_name] = RemovalSealRepo(
            name=embedded_name,
            source_path=Path(
                _string(repo.get("source_path"), f"{label}.repos.{repo_name}.source_path")
            ),
            worktree_path=Path(
                _string(repo.get("worktree_path"), f"{label}.repos.{repo_name}.worktree_path")
            ),
            git_admin_path=Path(
                _string(repo.get("git_admin_path"), f"{label}.repos.{repo_name}.git_admin_path")
            ),
            base_ref=_string(repo.get("base_ref"), f"{label}.repos.{repo_name}.base_ref"),
            base_commit=_string(repo.get("base_commit"), f"{label}.repos.{repo_name}.base_commit"),
            default_selector=_optional_string(
                repo.get("default_selector"),
                f"{label}.repos.{repo_name}.default_selector",
            ),
            mode=_string(repo.get("mode"), f"{label}.repos.{repo_name}.mode"),
            head=_optional_string(repo.get("head"), f"{label}.repos.{repo_name}.head"),
            branch=_optional_string(repo.get("branch"), f"{label}.repos.{repo_name}.branch"),
            detached=_bool(repo.get("detached"), f"{label}.repos.{repo_name}.detached"),
            dirty=_bool(repo.get("dirty"), f"{label}.repos.{repo_name}.dirty"),
        )
    return RemovalSeal(
        workspace_name=_string(value.get("workspace_name"), f"{label}.workspace_name"),
        workspace_path=Path(_string(value.get("workspace_path"), f"{label}.workspace_path")),
        tombstone_path=Path(_string(value.get("tombstone_path"), f"{label}.tombstone_path")),
        repos=parsed_repos,
        phase=_string(value.get("phase"), f"{label}.phase"),
        schema_version=schema_version,
    )


def validate_lock_state_consistency(lock: WorkspaceLock, state: WorkspaceState) -> None:
    """Validate that lock and runtime metadata refer to the same repositories."""

    _validate_lock(lock)
    _validate_state(state)
    if lock.workspace_name != state.workspace_name:
        raise SerializationError(
            "workspace lock/state workspace name mismatch: "
            f"{lock.workspace_name!r} != {state.workspace_name!r}"
        )
    if set(lock.repos) != set(state.repos):
        raise SerializationError("workspace lock/state repository identities do not match")
    if state.removal is not None and set(state.removal.repos) != set(lock.repos):
        raise SerializationError("removal repository identities do not match lock/state records")


def validate_removal_repo_identity(
    record: RemovalRepoState,
    *,
    worktree_path: Path,
    git_admin_path: Path,
) -> None:
    """Require a retry caller to match both persisted worktree identities."""

    expected_worktree = _canonical_path(record.worktree_path, "removal worktree path")
    actual_worktree = _canonical_path(worktree_path, "actual worktree path")
    if expected_worktree != actual_worktree:
        raise SerializationError("worktree path identity does not match removal record")
    expected_admin = _canonical_path(record.git_admin_path, "Git administrative identity")
    actual_admin = _canonical_path(git_admin_path, "actual Git administrative identity")
    if expected_admin != actual_admin:
        raise SerializationError("Git administrative identity does not match removal record")


def _validate_lock(lock: WorkspaceLock) -> None:
    _safe_id(lock.workspace_name, "[workspace].name")
    if not lock.repos:
        raise SerializationError("[repos] must contain at least one repository")
    canonical_sources: dict[Path, str] = {}
    for key, repo in lock.repos.items():
        _safe_id(key, f"[repos.{key}]")
        if repo.name != key:
            raise SerializationError(
                f"[repos.{key}].name must match repository mapping key {key!r}"
            )
        _safe_id(repo.name, f"[repos.{key}].name")
        source_path = _canonical_path(repo.source_path, f"[repos.{key}].source_path")
        previous = canonical_sources.get(source_path)
        if previous is not None:
            raise SerializationError(
                f"duplicate canonical source path {source_path} for repositories "
                f"{previous!r} and {key!r}"
            )
        canonical_sources[source_path] = key
        _ref_string(repo.base_ref, f"[repos.{key}].base_ref")
        _oid(repo.base_commit, f"[repos.{key}].base_commit")
        if repo.default_selector is not None:
            _ref_string(repo.default_selector, f"[repos.{key}].default_selector")


def _validate_state(state: WorkspaceState) -> None:
    _safe_id(state.workspace_name, "[workspace].name")
    if state.phase not in WORKSPACE_PHASES:
        raise SerializationError(f"[state].phase has unknown value {state.phase!r}")
    if not state.repos:
        raise SerializationError("[state.repos] must contain at least one repository")
    stash_tokens: set[str] = set()
    for key, repo in state.repos.items():
        _safe_id(key, f"[state.repos.{key}]")
        if repo.name != key:
            raise SerializationError(
                f"[state.repos.{key}].name must match repository mapping key {key!r}"
            )
        _safe_id(repo.name, f"[state.repos.{key}].name")
        if repo.mode not in REPO_MODES:
            raise SerializationError(f"[state.repos.{key}].mode has unknown value {repo.mode!r}")
        if not isinstance(repo.detached, bool) or not isinstance(repo.dirty, bool):
            raise SerializationError(f"[state.repos.{key}] detached/dirty must be booleans")
        if repo.head is not None:
            _oid(repo.head, f"[state.repos.{key}].head")
        if repo.mode == "claimed":
            if repo.detached:
                raise SerializationError(f"[state.repos.{key}] claimed mode cannot be detached")
            if repo.context is not None:
                raise SerializationError(f"[state.repos.{key}] claimed mode prohibits context")
        elif repo.mode == "detached":
            if not repo.detached:
                raise SerializationError(f"[state.repos.{key}] detached mode requires detached")
            if repo.context is not None:
                raise SerializationError(f"[state.repos.{key}] detached mode prohibits context")
        elif not repo.detached:
            raise SerializationError(f"[state.repos.{key}] context mode requires detached")
        elif repo.context is None:
            raise SerializationError(f"[state.repos.{key}] context mode requires context")
        if repo.context is not None:
            _validate_context(repo.context, f"[state.repos.{key}].context")
            if repo.context.stash_token in stash_tokens:
                raise SerializationError("context stash tokens must be unique")
            stash_tokens.add(repo.context.stash_token)
    if state.removal is not None:
        if any(repo.context is not None for repo in state.repos.values()):
            raise SerializationError("removal cannot coexist with active contexts")
        if state.phase != state.removal.phase:
            raise SerializationError(
                f"removal state phase {state.removal.phase!r} does not match "
                f"top-level workspace phase {state.phase!r}"
            )
        if (
            state.removal.phase == "removal_complete"
            and state.removal.seal is None
            and not state.removal._legacy_completed_without_seal
        ):
            raise SerializationError("removal_complete state requires a removal seal")
        _validate_removal(state.removal, state.workspace_name)
    elif state.phase != "idle":
        raise SerializationError("workspace without removal must have an idle phase")


def _validate_context(context: ContextState, label: str) -> None:
    if context.phase not in CONTEXT_PHASES:
        raise SerializationError(f"{label}.phase has unknown value {context.phase!r}")
    if context.return_mode not in REPO_MODES:
        raise SerializationError(f"{label}.return_mode has unknown value {context.return_mode!r}")
    _ref_string(context.target_ref, f"{label}.target_ref")
    _oid(context.target_commit, f"{label}.target_commit")
    _safe_id(context.stash_token, f"{label}.stash_token")
    if context.return_branch is not None:
        _ref_string(context.return_branch, f"{label}.return_branch")
    if context.return_saved_head is None:
        raise SerializationError(f"{label}.return_saved_head must be a full Git object ID")
    _oid(context.return_saved_head, f"{label}.return_saved_head")
    if context.stash_oid is not None:
        _oid(context.stash_oid, f"{label}.stash_oid")
    if context.private_ref is not None:
        if not context.private_ref.startswith("refs/ws/") or ".." in context.private_ref:
            raise SerializationError(f"{label}.private_ref must be a private refs/ws/ ref")
        _ref_string(context.private_ref, f"{label}.private_ref")
    for index, effect in enumerate(context.completed_effects):
        effect_label = f"{label}.completed_effects.{index}"
        _string(effect.operation, f"{effect_label}.operation")
        _string(effect.step, f"{effect_label}.step")
        for ref, expected_oid in effect.expected_refs.items():
            _ref_string(ref, f"{effect_label}.expected_refs key")
            _oid(expected_oid, f"{effect_label}.expected_refs.{ref}")
        for name, known_oid in effect.known_oids.items():
            _safe_id(name, f"{effect_label}.known_oids key")
            _oid(known_oid, f"{effect_label}.known_oids.{name}")


def _validate_removal(removal: RemovalState, workspace_name: str) -> None:
    if removal.phase not in REMOVAL_PHASES:
        raise SerializationError(f"[state.removal].phase has unknown value {removal.phase!r}")
    workspace_path = _canonical_path(removal.workspace_path, "[state.removal].workspace_path")
    tombstone_path = _canonical_path(removal.tombstone_path, "[state.removal].tombstone_path")
    if workspace_path.name != workspace_name:
        raise SerializationError("workspace path name must match workspace name")
    if (
        tombstone_path.parent != workspace_path.parent
        or tombstone_path.name != f".{workspace_name}.removing"
    ):
        raise SerializationError("removal tombstone path does not match workspace path")
    if not removal.repos:
        raise SerializationError("[state.removal.repos] must contain at least one repository")
    worktree_paths: set[Path] = set()
    admin_paths: set[Path] = set()
    for key, repo in removal.repos.items():
        _safe_id(key, f"[state.removal.repos.{key}]")
        if repo.name != key:
            raise SerializationError(
                f"[state.removal.repos.{key}].name must match repository mapping key {key!r}"
            )
        worktree_path = _canonical_path(
            repo.worktree_path, f"[state.removal.repos.{key}].worktree_path"
        )
        if workspace_path not in worktree_path.parents:
            raise SerializationError(
                f"[state.removal.repos.{key}].worktree_path must be inside workspace path"
            )
        if worktree_path in worktree_paths:
            raise SerializationError("removal worktree paths must be unique")
        worktree_paths.add(worktree_path)
        admin_path = _canonical_path(
            repo.git_admin_path, f"[state.removal.repos.{key}].git_admin_path"
        )
        if admin_path in admin_paths:
            raise SerializationError("Git administrative identities must be unique")
        admin_paths.add(admin_path)
        if not isinstance(repo.complete, bool):
            raise SerializationError(f"[state.removal.repos.{key}].complete must be a boolean")
    if removal.seal is not None:
        if removal.phase != "removal_complete":
            raise SerializationError("removal seal is only valid for completed removal")
        _validate_seal(removal.seal, "[state.removal.seal]")
        if removal.seal.workspace_name != workspace_name:
            raise SerializationError("removal seal workspace name does not match workspace state")
        if removal.seal.workspace_path != workspace_path:
            raise SerializationError("removal seal workspace path does not match removal state")
        if removal.seal.tombstone_path != tombstone_path:
            raise SerializationError("removal seal tombstone path does not match removal state")
        if set(removal.seal.repos) != set(removal.repos):
            raise SerializationError(
                "removal seal repository identities do not match removal state"
            )
        for name, record in removal.repos.items():
            if not record.complete:
                raise SerializationError(
                    "completed removal seal requires complete repository records"
                )
            sealed = removal.seal.repos[name]
            if (
                sealed.worktree_path
                != _canonical_path(record.worktree_path, "removal worktree path")
                or sealed.git_admin_path
                != _canonical_path(record.git_admin_path, "Git administrative identity")
            ):
                raise SerializationError(
                    f"removal seal repository identity does not match removal record for {name!r}"
                )


def _validate_seal(seal: RemovalSeal, label: str) -> None:
    _require_schema(seal.schema_version, REMOVAL_SEAL_SCHEMA_VERSION, "removal seal")
    _safe_id(seal.workspace_name, f"{label}.workspace_name")
    workspace_path = _canonical_path(seal.workspace_path, f"{label}.workspace_path")
    tombstone_path = _canonical_path(seal.tombstone_path, f"{label}.tombstone_path")
    if workspace_path.name != seal.workspace_name:
        raise SerializationError(f"{label}.workspace_path name must match workspace name")
    if (
        tombstone_path.parent != workspace_path.parent
        or tombstone_path.name != f".{seal.workspace_name}.removing"
    ):
        raise SerializationError(f"{label}.tombstone_path does not match workspace path")
    if seal.phase != "removal_complete":
        raise SerializationError(f"{label}.phase must be 'removal_complete'")
    if not seal.repos:
        raise SerializationError(f"{label}.repos must contain at least one repository")

    source_paths: set[Path] = set()
    worktree_paths: set[Path] = set()
    admin_paths: set[Path] = set()
    for key, repo in seal.repos.items():
        _safe_id(key, f"{label}.repos.{key}")
        repo_label = f"{label}.repos.{key}"
        if repo.name != key:
            raise SerializationError(
                f"{repo_label}.name must match repository mapping key {key!r}"
            )
        _safe_id(repo.name, f"{repo_label}.name")
        source_path = _canonical_path(repo.source_path, f"{repo_label}.source_path")
        worktree_path = _canonical_path(repo.worktree_path, f"{repo_label}.worktree_path")
        admin_path = _canonical_path(repo.git_admin_path, f"{repo_label}.git_admin_path")
        if source_path in source_paths:
            raise SerializationError(f"{label} repository source paths must be unique")
        if worktree_path in worktree_paths:
            raise SerializationError(f"{label} repository worktree paths must be unique")
        if admin_path in admin_paths:
            raise SerializationError(f"{label} Git administrative identities must be unique")
        source_paths.add(source_path)
        worktree_paths.add(worktree_path)
        admin_paths.add(admin_path)
        expected_worktree = workspace_path / "repos" / key
        if worktree_path != expected_worktree:
            raise SerializationError(
                f"{repo_label}.worktree_path must match its exact workspace path"
            )
        _ref_string(repo.base_ref, f"{repo_label}.base_ref")
        _oid(repo.base_commit, f"{repo_label}.base_commit")
        if repo.default_selector is not None:
            _ref_string(repo.default_selector, f"{repo_label}.default_selector")
        if repo.mode not in {"claimed", "detached"}:
            raise SerializationError(f"{repo_label}.mode has unknown value {repo.mode!r}")
        if repo.head is not None:
            _oid(repo.head, f"{repo_label}.head")
        if repo.branch is not None:
            _ref_string(repo.branch, f"{repo_label}.branch")
        if not isinstance(repo.detached, bool) or not isinstance(repo.dirty, bool):
            raise SerializationError(f"{repo_label} detached/dirty must be booleans")
        if repo.mode == "claimed" and repo.detached:
            raise SerializationError(f"{repo_label} claimed mode cannot be detached")
        if repo.mode == "detached" and not repo.detached:
            raise SerializationError(f"{repo_label} detached mode requires detached")


def _safe_id(value: str, label: str) -> None:
    try:
        validate_logical_name(value, kind="metadata")
    except InvalidLogicalName as exc:
        raise SerializationError(f"{label} is not a safe logical identifier: {exc}") from exc


def _canonical_path(value: Path, label: str) -> Path:
    if not value.is_absolute():
        raise SerializationError(f"{label} must be an absolute canonical path")
    canonical = value.resolve(strict=False)
    if value != canonical:
        raise SerializationError(f"{label} must be an absolute canonical path")
    return canonical


def _oid(value: str, label: str) -> None:
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value) is None:
        raise SerializationError(f"{label} must be a full Git object ID")


def _ref_string(value: str, label: str) -> None:
    if not value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise SerializationError(f"{label} must be a non-empty Git ref string")


def _string_map(value: Any, label: str) -> dict[str, str]:
    table = _table(value, label)
    result: dict[str, str] = {}
    for key, item in table.items():
        if not isinstance(item, str):
            raise SerializationError(f"{label}.{key} must be a string")
        result[key] = item
    return result


def _parse_toml(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SerializationError(f"invalid workspace metadata TOML: {exc}") from exc


def _schema_version(raw: Mapping[str, Any], label: str) -> int:
    value = raw.get("schema_version")
    if not isinstance(value, int) or isinstance(value, bool):
        raise SerializationError(f"{label} schema_version must be an integer")
    return value


def _require_schema(actual: int, expected: int, label: str) -> None:
    if actual != expected:
        raise SerializationError(
            f"unsupported schema version {actual} for {label}; expected {expected}"
        )


def _table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SerializationError(f"{label} must be a TOML table")
    return cast(dict[str, Any], value)


def _reject_unexpected_keys(
    value: Mapping[str, Any], allowed: set[str], label: str
) -> None:
    unexpected = set(value) - allowed
    if unexpected:
        names = ", ".join(repr(name) for name in sorted(unexpected))
        raise SerializationError(f"{label} contains unexpected fields: {names}")


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SerializationError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: Any, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise SerializationError(f"{label} must be a string when present")
    return value


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise SerializationError(f"{label} must be a boolean")
    return value


def _render_table(table: Mapping[str, Any], prefix: tuple[str, ...], lines: list[str]) -> None:
    scalars = [(key, value) for key, value in table.items() if not isinstance(value, Mapping)]
    for key, value in scalars:
        if value is not None:
            lines.append(f"{_key(key)} = {_value(value)}")

    for key, value in table.items():
        if not isinstance(value, Mapping):
            continue
        section = prefix + (key,)
        has_scalar = any(
            not isinstance(child, Mapping) and child is not None for child in value.values()
        )
        if has_scalar or not value:
            if lines:
                lines.append("")
            lines.append(f"[{'.'.join(_key(part) for part in section)}]")
        _render_table(value, section, lines)


def _key(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("TOML keys must be strings")
    return value if _BARE_KEY.fullmatch(value) else json.dumps(value, ensure_ascii=False)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list) and all(isinstance(item, (bool, int, float, str)) for item in value):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML value: {value!r}")
