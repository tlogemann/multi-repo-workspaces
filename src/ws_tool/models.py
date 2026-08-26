from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

WORKSPACE_LOCK_SCHEMA_VERSION = 1
WORKSPACE_STATE_SCHEMA_VERSION = 2
REMOVAL_SEAL_SCHEMA_VERSION = 1
CONTEXT_PHASES = frozenset(
    {"entering", "active", "restoring", "restore_conflicted", "restore_failed"}
)
REMOVAL_PHASES = frozenset({"removing", "removal_complete"})
REPO_MODES = frozenset({"claimed", "detached", "context"})
WORKSPACE_PHASES = frozenset({"idle"}) | REMOVAL_PHASES


@dataclass(frozen=True)
class RepoConfig:
    name: str
    source_path: Path
    default_branch: str | None = None


@dataclass(frozen=True)
class ProjectConfig:
    path: Path
    workspace_root: Path
    repos: dict[str, RepoConfig]


@dataclass(frozen=True)
class WorkspaceLockRepo:
    name: str
    source_path: Path
    base_ref: str
    base_commit: str
    default_selector: str | None = None


@dataclass(frozen=True)
class WorkspaceLock:
    workspace_name: str
    repos: dict[str, WorkspaceLockRepo]
    schema_version: int = WORKSPACE_LOCK_SCHEMA_VERSION


@dataclass(frozen=True)
class ContextSideEffect:
    operation: str
    step: str
    expected_refs: dict[str, str] = field(default_factory=dict)
    known_oids: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextState:
    target_ref: str
    target_commit: str
    phase: str
    return_mode: str
    stash_token: str
    return_branch: str | None = None
    return_saved_head: str | None = None
    stash_oid: str | None = None
    private_ref: str | None = None
    completed_effects: tuple[ContextSideEffect, ...] = ()


@dataclass(frozen=True)
class RepoState:
    name: str
    mode: str
    head: str | None = None
    branch: str | None = None
    detached: bool = False
    dirty: bool = False
    context: ContextState | None = None


@dataclass(frozen=True)
class RemovalSealRepo:
    name: str
    source_path: Path
    worktree_path: Path
    git_admin_path: Path
    base_ref: str
    base_commit: str
    default_selector: str | None
    mode: str
    head: str | None
    branch: str | None
    detached: bool
    dirty: bool


@dataclass(frozen=True)
class RemovalRepoState:
    name: str
    worktree_path: Path
    git_admin_path: Path
    complete: bool


@dataclass(frozen=True)
class RemovalSeal:
    workspace_name: str
    workspace_path: Path
    tombstone_path: Path
    repos: dict[str, RemovalSealRepo]
    phase: str = "removal_complete"
    schema_version: int = REMOVAL_SEAL_SCHEMA_VERSION


@dataclass(frozen=True)
class RemovalState:
    phase: str
    workspace_path: Path
    tombstone_path: Path
    repos: dict[str, RemovalRepoState]
    seal: RemovalSeal | None = None
    _legacy_completed_without_seal: bool = field(default=False, compare=False, repr=False)


@dataclass(frozen=True)
class WorkspaceState:
    workspace_name: str
    phase: str
    repos: dict[str, RepoState]
    removal: RemovalState | None = None
    schema_version: int = WORKSPACE_STATE_SCHEMA_VERSION
