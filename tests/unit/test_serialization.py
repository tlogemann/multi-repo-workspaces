from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from ws_tool.errors import SerializationError
from ws_tool.models import (
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
from ws_tool.serialization import (
    deserialize_removal_seal,
    deserialize_workspace_lock,
    deserialize_workspace_state,
    dump_toml,
    serialize_removal_seal,
    serialize_workspace_lock,
    serialize_workspace_state,
    validate_lock_state_consistency,
    validate_removal_repo_identity,
    write_removal_seal,
    write_toml,
    write_workspace_lock,
    write_workspace_state,
)


def test_dump_toml_supports_controlled_nested_lock_data() -> None:
    text = dump_toml(
        {
            "schema_version": 1,
            "workspace": {"name": "demo", "source": None},
            "repos": {"app": {"base_commit": "abc", "detached": True}},
        }
    )

    assert "schema_version = 1" in text
    assert "[workspace]" in text
    assert 'name = "demo"' in text
    assert "source" not in text
    assert "[repos.app]" in text


def test_dump_toml_round_trips_non_bmp_unicode() -> None:
    text = dump_toml({"workspace": {"name": "snowman-😀"}})

    assert tomllib.loads(text) == {"workspace": {"name": "snowman-😀"}}


def test_write_toml_uses_atomic_sibling_replace(tmp_path: Path) -> None:
    target = tmp_path / "state.toml"

    write_toml(target, {"phase": "active"})

    assert target.read_text(encoding="utf-8") == 'phase = "active"\n'
    assert list(tmp_path.glob("state.toml.*.tmp")) == []


def test_write_toml_syncs_containing_directory_through_injected_seam(tmp_path: Path) -> None:
    target = tmp_path / "state.toml"
    synced: list[Path] = []

    write_toml(target, {"phase": "active"}, fsync_directory=synced.append)

    assert synced == [tmp_path]


def test_write_toml_reports_atomic_replace_errors_contextually(tmp_path: Path) -> None:
    target = tmp_path / "state.toml"

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("replacement denied")

    with pytest.raises(SerializationError, match="cannot atomically replace.*state.toml"):
        write_toml(target, {"phase": "active"}, replace=fail_replace)

    assert not target.exists()


def test_write_toml_reports_directory_sync_errors_contextually(tmp_path: Path) -> None:
    target = tmp_path / "state.toml"

    def fail_sync(_directory: Path) -> None:
        raise OSError("sync denied")

    with pytest.raises(SerializationError, match="cannot fsync metadata directory"):
        write_toml(target, {"phase": "active"}, fsync_directory=fail_sync)

    assert target.read_text(encoding="utf-8") == 'phase = "active"\n'


def test_workspace_lock_and_runtime_state_round_trip() -> None:
    lock = WorkspaceLock(
        workspace_name="demo",
        repos={
            "app": WorkspaceLockRepo(
                name="app",
                source_path=Path("/sources/app"),
                base_ref="origin/main",
                base_commit="a" * 40,
                default_selector=None,
            )
        },
    )
    state = WorkspaceState(
        workspace_name="demo",
        phase="idle",
        repos={
            "app": RepoState(
                name="app",
                mode="context",
                head="b" * 40,
                detached=True,
                dirty=False,
                context=ContextState(
                    target_ref="default",
                    target_commit="b" * 40,
                    phase="active",
                    return_mode="detached",
                    return_branch=None,
                    return_saved_head="a" * 40,
                    stash_token="token-123",
                    stash_oid="c" * 40,
                    private_ref="refs/ws/context/demo/app/token-123",
                    completed_effects=(
                        ContextSideEffect(
                            operation="stash",
                            step="create",
                            expected_refs={"refs/heads/main": "a" * 40},
                            known_oids={"stash": "c" * 40},
                        ),
                    ),
                ),
            )
        },
    )
    removal_state = WorkspaceState(
        workspace_name="demo",
        phase="removing",
        repos={"app": RepoState(name="app", mode="detached", detached=True)},
        removal=RemovalState(
            phase="removing",
            workspace_path=Path("/workspaces/demo"),
            tombstone_path=Path("/workspaces/.demo.removing"),
            repos={
                "app": RemovalRepoState(
                    name="app",
                    worktree_path=Path("/workspaces/demo/app"),
                    git_admin_path=Path("/sources/app/.git/worktrees/demo-app"),
                    complete=False,
                )
            },
        ),
    )

    assert deserialize_workspace_lock(serialize_workspace_lock(lock)) == lock
    assert deserialize_workspace_state(serialize_workspace_state(state)) == state
    assert deserialize_workspace_state(serialize_workspace_state(removal_state)) == removal_state
    validate_lock_state_consistency(lock, state)
    validate_lock_state_consistency(lock, removal_state)


def test_model_specific_writers_validate_and_persist_exact_serialized_content(
    tmp_path: Path,
) -> None:
    lock = WorkspaceLock(
        workspace_name="demo",
        repos={
            "app": WorkspaceLockRepo(
                name="app",
                source_path=Path("/missing/source"),
                base_ref="origin/main",
                base_commit="a" * 40,
            )
        },
    )
    state = WorkspaceState(
        workspace_name="demo",
        phase="idle",
        repos={"app": RepoState(name="app", mode="claimed")},
    )
    lock_path = tmp_path / "workspace.lock.toml"
    state_path = tmp_path / "state.toml"

    write_workspace_lock(lock_path, lock, fsync_directory=lambda _directory: None)
    write_workspace_state(state_path, state, fsync_directory=lambda _directory: None)

    assert lock_path.read_text(encoding="utf-8") == serialize_workspace_lock(lock)
    assert state_path.read_text(encoding="utf-8") == serialize_workspace_state(state)


def test_model_specific_writers_reject_invalid_models_before_replacement(tmp_path: Path) -> None:
    invalid = WorkspaceState(workspace_name="demo", phase="removing", repos={})
    target = tmp_path / "state.toml"

    with pytest.raises(SerializationError, match="at least one repository"):
        write_workspace_state(target, invalid, fsync_directory=lambda _directory: None)

    assert not target.exists()


def test_round_trips_idle_state_with_detached_repositories() -> None:
    state = WorkspaceState(
        workspace_name="demo",
        phase="idle",
        repos={"app": RepoState(name="app", mode="detached", detached=True)},
    )

    assert deserialize_workspace_state(serialize_workspace_state(state)) == state


@pytest.mark.parametrize(
    "repo",
    [
        RepoState(name="app", mode="claimed", detached=True),
        RepoState(name="app", mode="detached", detached=False),
        RepoState(name="app", mode="context", detached=False),
        RepoState(name="app", mode="context", detached=True),
    ],
)
def test_repo_modes_validate_coherently(repo: RepoState) -> None:
    state = WorkspaceState(workspace_name="demo", phase="idle", repos={"app": repo})
    if repo.mode == "context":
        if repo.context is None:
            with pytest.raises(SerializationError, match="context mode requires"):
                serialize_workspace_state(state)
        else:
            serialize_workspace_state(state)
    elif repo.mode == "claimed" and repo.detached:
        with pytest.raises(SerializationError, match="claimed mode cannot be detached"):
            serialize_workspace_state(state)
    else:
        with pytest.raises(SerializationError, match="detached mode requires detached"):
            serialize_workspace_state(state)


def test_context_mode_requires_detached_repository_and_allows_detached_return_mode() -> None:
    context = ContextState(
        target_ref="default",
        target_commit="a" * 40,
        phase="active",
        return_mode="detached",
        stash_token="token",
        return_saved_head="b" * 40,
    )
    state = WorkspaceState(
        workspace_name="demo",
        phase="idle",
        repos={"app": RepoState(name="app", mode="context", detached=True, context=context)},
    )

    assert deserialize_workspace_state(serialize_workspace_state(state)) == state


def test_removal_cannot_coexist_with_active_contexts() -> None:
    context = ContextState(
        target_ref="default",
        target_commit="a" * 40,
        phase="active",
        return_mode="detached",
        stash_token="token",
        return_saved_head="b" * 40,
    )
    state = WorkspaceState(
        workspace_name="demo",
        phase="removing",
        repos={"app": RepoState(name="app", mode="context", detached=True, context=context)},
        removal=RemovalState(
            phase="removing",
            workspace_path=Path("/workspaces/demo"),
            tombstone_path=Path("/workspaces/.demo.removing"),
            repos={
                "app": RemovalRepoState(
                    name="app",
                    worktree_path=Path("/workspaces/demo/app"),
                    git_admin_path=Path("/sources/app/.git/worktrees/demo-app"),
                    complete=False,
                )
            },
        ),
    )

    with pytest.raises(SerializationError, match="cannot coexist"):
        serialize_workspace_state(state)


def test_consistency_requires_removal_repository_identities_to_match_lock() -> None:
    lock = WorkspaceLock(
        workspace_name="demo",
        repos={
            "app": WorkspaceLockRepo(
                name="app",
                source_path=Path("/missing/source"),
                base_ref="origin/main",
                base_commit="a" * 40,
            )
        },
    )
    state = WorkspaceState(
        workspace_name="demo",
        phase="removing",
        repos={"app": RepoState(name="app", mode="detached", detached=True)},
        removal=RemovalState(
            phase="removing",
            workspace_path=Path("/workspaces/demo"),
            tombstone_path=Path("/workspaces/.demo.removing"),
            repos={
                "lib": RemovalRepoState(
                    name="lib",
                    worktree_path=Path("/workspaces/demo/lib"),
                    git_admin_path=Path("/sources/lib/.git/worktrees/demo-lib"),
                    complete=False,
                )
            },
        ),
    )

    with pytest.raises(SerializationError, match="removal repository identities"):
        validate_lock_state_consistency(lock, state)


def test_removal_identity_requires_both_worktree_and_git_admin_paths() -> None:
    record = RemovalRepoState(
        name="app",
        worktree_path=Path("/workspaces/demo/app"),
        git_admin_path=Path("/sources/app/.git/worktrees/demo-app"),
        complete=False,
    )

    validate_removal_repo_identity(
        record,
        worktree_path=Path("/workspaces/demo/app"),
        git_admin_path=Path("/sources/app/.git/worktrees/demo-app"),
    )
    with pytest.raises(SerializationError, match="Git administrative identity"):
        validate_removal_repo_identity(
            record,
            worktree_path=Path("/workspaces/demo/app"),
            git_admin_path=Path("/sources/app/.git/worktrees/other"),
        )


def test_rejects_noncanonical_git_admin_path() -> None:
    text = _state_text(
        phase="removing",
        removal=(
            '\n[state.removal]\nphase = "removing"\n'
            'workspace_path = "/workspaces/demo"\n'
            'tombstone_path = "/workspaces/.demo.removing"\n\n'
            '[state.removal.repos.app]\nname = "app"\n'
            'worktree_path = "/workspaces/demo/app"\n'
            'git_admin_path = "relative/admin"\ncomplete = false\n'
        ),
    )

    with pytest.raises(SerializationError, match="git_admin_path.*canonical"):
        deserialize_workspace_state(text)


@pytest.mark.parametrize(
    ("decoder", "text"),
    [
        (deserialize_workspace_lock, "schema_version = 99\n"),
        (
            deserialize_workspace_state,
            'schema_version = 99\n[workspace]\nname = "demo"\n[state]\nphase = "active"\n',
        ),
    ],
)
def test_rejects_unsupported_metadata_schema_versions(decoder, text: str) -> None:
    with pytest.raises(SerializationError, match="unsupported schema version"):
        decoder(text)


def test_rejects_unsupported_schema_version_when_serializing() -> None:
    lock = WorkspaceLock(workspace_name="demo", repos={}, schema_version=99)

    with pytest.raises(SerializationError, match="unsupported schema version"):
        serialize_workspace_lock(lock)


def _lock_text(
    *,
    workspace_name: str = "demo",
    repo_key: str = "app",
    embedded_name: str = "app",
    source_path: str = "/missing/source",
    base_commit: str = "a" * 40,
    default_selector: str = "",
    extra_repos: str = "",
) -> str:
    selector = f"\ndefault_selector = {default_selector}" if default_selector else ""
    return (
        f'schema_version = 1\n\n[workspace]\nname = "{workspace_name}"\n\n'
        f'[repos.{repo_key}]\nname = "{embedded_name}"\nsource_path = "{source_path}"\n'
        f'base_ref = "origin/main"\nbase_commit = "{base_commit}"{selector}\n'
        f"{extra_repos}"
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (_lock_text(workspace_name="../demo"), "safe logical identifier"),
        (_lock_text(embedded_name="library"), "must match repository mapping key"),
        (_lock_text(source_path="relative/source"), "absolute canonical path"),
        (_lock_text(base_commit="deadbeef"), "full Git object ID"),
        (
            _lock_text(
                extra_repos='\n[repos.lib]\nname = "lib"\nsource_path = "/missing/source"\n'
                'base_ref = "origin/main"\nbase_commit = "' + "b" * 40 + '"\n'
            ),
            "duplicate canonical source path",
        ),
        (_lock_text(default_selector='["origin/main"]'), "must be a string when present"),
    ],
)
def test_rejects_malformed_lock_metadata(text: str, message: str) -> None:
    with pytest.raises(SerializationError, match=message):
        deserialize_workspace_lock(text)


def _state_text(
    *,
    phase: str = "active",
    repo_name: str = "app",
    head: str = "a" * 40,
    repo_mode: str = "claimed",
    detached: bool = False,
    removal: str = "",
) -> str:
    return (
        f'schema_version = 1\n\n[workspace]\nname = "demo"\n\n[state]\nphase = "{phase}"\n\n'
        f'[state.repos.app]\nname = "{repo_name}"\nmode = "{repo_mode}"\nhead = "{head}"\n'
        f"detached = {str(detached).lower()}\ndirty = false\n{removal}"
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (_state_text(phase="unknown"), "unknown value"),
        (_state_text(repo_name="lib"), "must match repository mapping key"),
        (_state_text(head="not-an-oid"), "full Git object ID"),
        (
            _state_text(
                phase="removing",
                removal=(
                    '\n[state.removal]\nphase = "removing"\n'
                    'workspace_path = "/workspaces/demo"\n'
                    'tombstone_path = "/elsewhere/.demo.removing"\n\n'
                    '[state.removal.repos.app]\nname = "app"\n'
                    'worktree_path = "/workspaces/demo/app"\n'
                    'git_admin_path = "/sources/app/.git/worktrees/demo-app"\n'
                    "complete = false\n"
                ),
            ),
            "tombstone path",
        ),
        (
            _state_text(
                phase="removing",
                removal=(
                    '\n[state.removal]\nphase = "removing"\n'
                    'workspace_path = "/workspaces/demo"\n'
                    'tombstone_path = "/workspaces/.demo.removing"\n\n'
                    '[state.removal.repos.app]\nname = "app"\n'
                    'worktree_path = "/outside/app"\n'
                    'git_admin_path = "/sources/app/.git/worktrees/demo-app"\n'
                    "complete = false\n"
                ),
            ),
            "inside workspace path",
        ),
    ],
)
def test_rejects_malformed_runtime_metadata(text: str, message: str) -> None:
    with pytest.raises(SerializationError, match=message):
        deserialize_workspace_state(text)


def test_rejects_context_with_unknown_phase_and_missing_recovery_fields() -> None:
    text = _state_text(
        repo_mode="context",
        detached=True,
        removal=(
            '\n[state.repos.app.context]\ntarget_ref = "default"\n'
            f'target_commit = "{"a" * 40}"\nphase = "lost"\nreturn_mode = "claimed"\n'
            'stash_token = "token"\nreturn_saved_head = "' + "a" * 40 + '"\n'
        ),
    )

    with pytest.raises(SerializationError, match="unknown value"):
        deserialize_workspace_state(text)


@pytest.mark.parametrize(
    ("context_field", "value", "message"),
    [
        ("target_commit", "not-an-oid", "full Git object ID"),
        ("stash_token", "", "non-empty string"),
        ("return_saved_head", "not-an-oid", "full Git object ID"),
        ("private_ref", "refs/heads/outside", "private refs/ws/ ref"),
    ],
)
def test_rejects_malformed_context_recovery_records(
    context_field: str, value: str, message: str
) -> None:
    target_commit = "a" * 40 if context_field != "target_commit" else value
    stash_token = "token" if context_field != "stash_token" else value
    saved_head = "a" * 40 if context_field != "return_saved_head" else value
    private_ref = "refs/ws/context/demo/app/token" if context_field != "private_ref" else value
    text = _state_text(
        repo_mode="context",
        detached=True,
        removal=(
            '\n[state.repos.app.context]\ntarget_ref = "default"\n'
            f'target_commit = "{target_commit}"\nphase = "active"\n'
            'return_mode = "claimed"\n'
            f'stash_token = "{stash_token}"\n'
            f'return_saved_head = "{saved_head}"\n'
            f'private_ref = "{private_ref}"\n'
        ),
    )

    with pytest.raises(SerializationError, match=message):
        deserialize_workspace_state(text)


def test_rejects_removal_phase_without_removal_record() -> None:
    with pytest.raises(SerializationError, match="without removal.*idle"):
        deserialize_workspace_state(_state_text(phase="removing"))


def test_rejects_top_level_phase_mismatch_with_removal_record() -> None:
    text = _state_text(
        phase="idle",
        removal=(
            '\n[state.removal]\nphase = "removing"\n'
            'workspace_path = "/workspaces/demo"\n'
            'tombstone_path = "/workspaces/.demo.removing"\n\n'
            '[state.removal.repos.app]\nname = "app"\n'
            'worktree_path = "/workspaces/demo/app"\n'
            'git_admin_path = "/sources/app/.git/worktrees/demo-app"\ncomplete = false\n'
        ),
    )

    with pytest.raises(SerializationError, match="'removing'.*'idle'"):
        deserialize_workspace_state(text)


def test_lock_state_consistency_rejects_different_repository_identities() -> None:
    lock = WorkspaceLock(
        workspace_name="demo",
        repos={
            "app": WorkspaceLockRepo(
                name="app",
                source_path=Path("/missing/source"),
                base_ref="origin/main",
                base_commit="a" * 40,
            )
        },
    )
    state = WorkspaceState(
        workspace_name="demo",
        phase="idle",
        repos={"lib": RepoState(name="lib", mode="claimed")},
    )

    with pytest.raises(SerializationError, match="repository identities"):
        validate_lock_state_consistency(lock, state)


def test_rejects_duplicate_context_stash_tokens() -> None:
    context = ContextState(
        target_ref="default",
        target_commit="a" * 40,
        phase="active",
        return_mode="claimed",
        stash_token="same-token",
        return_saved_head="a" * 40,
    )
    state = WorkspaceState(
        workspace_name="demo",
        phase="idle",
        repos={
            "app": RepoState(name="app", mode="context", detached=True, context=context),
            "lib": RepoState(name="lib", mode="context", detached=True, context=context),
        },
    )

    with pytest.raises(SerializationError, match="stash tokens must be unique"):
        serialize_workspace_state(state)


V1_ACTIVE_CONTEXT_TOML = (
    'schema_version = 1\n\n[workspace]\nname = "demo"\n\n'
    '[state]\nphase = "active"\n\n'
    '[state.repos.app]\nname = "app"\nmode = "context"\n'
    'head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
    'detached = true\ndirty = false\n\n'
    '[state.repos.app.context]\ntarget_ref = "default"\n'
    'target_commit = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"\nphase = "active"\n'
    'return_mode = "claimed"\nstash_token = "app-token"\n'
    'return_saved_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n\n'
    '[state.repos.api]\nname = "api"\nmode = "context"\n'
    'head = "cccccccccccccccccccccccccccccccccccccccc"\n'
    'detached = true\ndirty = false\n\n'
    '[state.repos.api.context]\ntarget_ref = "release"\n'
    'target_commit = "dddddddddddddddddddddddddddddddddddddddd"\nphase = "restoring"\n'
    'return_mode = "detached"\nstash_token = "api-token"\n'
    'return_saved_head = "cccccccccccccccccccccccccccccccccccccccc"\n'
)

V2_REMOVAL_WITH_CONTEXT_TOML = (
    'schema_version = 2\n\n[workspace]\nname = "demo"\n\n'
    '[state]\nphase = "removing"\n\n'
    '[state.repos.app]\nname = "app"\nmode = "context"\n'
    'head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
    'detached = true\ndirty = false\n\n'
    '[state.repos.app.context]\ntarget_ref = "default"\n'
    'target_commit = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"\nphase = "active"\n'
    'return_mode = "claimed"\nstash_token = "app-token"\n'
    'return_saved_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n\n'
    '[state.removal]\nphase = "removing"\n'
    'workspace_path = "/workspaces/demo"\n'
    'tombstone_path = "/workspaces/.demo.removing"\n\n'
    '[state.removal.repos.app]\nname = "app"\n'
    'worktree_path = "/workspaces/demo/app"\n'
    'git_admin_path = "/sources/app/.git/worktrees/demo-app"\n'
    'complete = false\n'
)


def test_deserialize_v1_context_phase_preserves_repo_context() -> None:
    state = deserialize_workspace_state(V1_ACTIVE_CONTEXT_TOML)

    assert state.schema_version == 2
    assert state.phase == "idle"
    assert state.repos["app"].context is not None
    assert state.repos["app"].context.phase == "active"
    assert state.repos["api"].context is not None
    assert state.repos["api"].context.phase == "restoring"
    assert tomllib.loads(serialize_workspace_state(state))["schema_version"] == 2


def test_v2_rejects_removal_with_any_repository_context() -> None:
    with pytest.raises(SerializationError, match="removal.*context"):
        deserialize_workspace_state(V2_REMOVAL_WITH_CONTEXT_TOML)


def _removal_seal() -> RemovalSeal:
    return RemovalSeal(
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


def test_removal_seal_round_trips_without_state_or_lock_digest() -> None:
    seal = _removal_seal()

    text = serialize_removal_seal(seal)
    parsed = tomllib.loads(text)
    repo = parsed["seal"]["repos"]["app"]

    assert deserialize_removal_seal(text) == seal
    assert "state" not in parsed["seal"]
    assert "lock" not in parsed["seal"]
    assert "digest" not in text
    assert "branch" not in repo


def test_removal_seal_writer_is_atomic_and_syncable(tmp_path: Path) -> None:
    target = tmp_path / "removal-seal.toml"
    synced: list[Path] = []

    write_removal_seal(target, _removal_seal(), fsync_directory=synced.append)

    assert deserialize_removal_seal(target.read_text(encoding="utf-8")) == _removal_seal()
    assert synced == [tmp_path]
    assert list(tmp_path.glob("removal-seal.toml.*.tmp")) == []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("schema_version = 2\n", "unsupported schema version"),
        (
            'schema_version = 1\n\n[seal]\nworkspace_name = "sample"\n'
            'workspace_path = "/tmp/sample"\ntombstone_path = "/tmp/.sample.removing"\n'
            'phase = "removal_complete"\nstate_digest = "bad"\n',
            "unexpected fields",
        ),
        (
            'schema_version = 1\n\n[seal]\nworkspace_name = "sample"\n'
            'workspace_path = "/tmp/sample"\ntombstone_path = "/tmp/.sample.removing"\n'
            'phase = "removing"\n\n[seal.repos.app]\nname = "app"\n'
            'source_path = "/tmp/source-app"\nworktree_path = "/tmp/sample/repos/app"\n'
            'git_admin_path = "/tmp/source-app/.git/worktrees/app"\n'
            'base_ref = "origin/main"\nbase_commit = "' + "0" * 40 + '"\n'
            'mode = "detached"\ndetached = true\ndirty = false\n',
            "removal_complete",
        ),
    ],
)
def test_rejects_malformed_removal_seals(text: str, message: str) -> None:
    with pytest.raises(SerializationError, match=message):
        deserialize_removal_seal(text)


def test_completed_state_embeds_the_same_removal_seal() -> None:
    seal = _removal_seal()
    state = WorkspaceState(
        workspace_name="sample",
        phase="removal_complete",
        repos={"app": RepoState(name="app", mode="detached", head="0" * 40, detached=True)},
        removal=RemovalState(
            phase="removal_complete",
            workspace_path=seal.workspace_path,
            tombstone_path=seal.tombstone_path,
            repos={
                "app": RemovalRepoState(
                    name="app",
                    worktree_path=seal.repos["app"].worktree_path,
                    git_admin_path=seal.repos["app"].git_admin_path,
                    complete=True,
                )
            },
            seal=seal,
        ),
    )

    serialized = serialize_workspace_state(state)
    embedded = deserialize_workspace_state(serialized).removal

    assert embedded is not None
    assert embedded.seal == deserialize_removal_seal(serialize_removal_seal(seal))


def test_v1_completed_removal_without_seal_remains_readable() -> None:
    text = _state_text(
        phase="removal_complete",
        removal=(
            '\n[state.removal]\nphase = "removal_complete"\n'
            'workspace_path = "/workspaces/demo"\n'
            'tombstone_path = "/workspaces/.demo.removing"\n\n'
            '[state.removal.repos.app]\nname = "app"\n'
            'worktree_path = "/workspaces/demo/app"\n'
            'git_admin_path = "/sources/app/.git/worktrees/demo-app"\ncomplete = true\n'
        ),
    )

    state = deserialize_workspace_state(text)

    assert state.schema_version == 2
    assert state.removal is not None
    assert state.removal.seal is None
    with pytest.raises(SerializationError, match="requires a removal seal"):
        serialize_workspace_state(state)


def test_v2_completed_removal_without_seal_fails_closed_everywhere() -> None:
    state = WorkspaceState(
        workspace_name="demo",
        phase="removal_complete",
        repos={"app": RepoState(name="app", mode="detached", head="a" * 40, detached=True)},
        removal=RemovalState(
            phase="removal_complete",
            workspace_path=Path("/workspaces/demo"),
            tombstone_path=Path("/workspaces/.demo.removing"),
            repos={
                "app": RemovalRepoState(
                    name="app",
                    worktree_path=Path("/workspaces/demo/app"),
                    git_admin_path=Path("/sources/app/.git/worktrees/demo-app"),
                    complete=True,
                )
            },
        ),
    )
    lock = WorkspaceLock(
        workspace_name="demo",
        repos={
            "app": WorkspaceLockRepo(
                name="app",
                source_path=Path("/sources/app"),
                base_ref="origin/main",
                base_commit="a" * 40,
            )
        },
    )

    with pytest.raises(SerializationError, match="requires a removal seal"):
        serialize_workspace_state(state)
    with pytest.raises(SerializationError, match="requires a removal seal"):
        validate_lock_state_consistency(lock, state)

    v2_text = _state_text(
        phase="removal_complete",
        removal=(
            '\n[state.removal]\nphase = "removal_complete"\n'
            'workspace_path = "/workspaces/demo"\n'
            'tombstone_path = "/workspaces/.demo.removing"\n\n'
            '[state.removal.repos.app]\nname = "app"\n'
            'worktree_path = "/workspaces/demo/app"\n'
            'git_admin_path = "/sources/app/.git/worktrees/demo-app"\ncomplete = true\n'
        ),
    ).replace("schema_version = 1", "schema_version = 2", 1)
    with pytest.raises(SerializationError, match="requires a removal seal"):
        deserialize_workspace_state(v2_text)
