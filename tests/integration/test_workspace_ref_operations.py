from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_context import create_two_repo_workspace, git, git_output

import ws_tool.ref_operations as ref_operations
import ws_tool.workspace as workspace_module
from ws_tool.errors import GitCommandError, WsError
from ws_tool.git import worktree_admin_path
from ws_tool.models import RemovalRepoState, RemovalState
from ws_tool.serialization import (
    deserialize_workspace_state,
    serialize_workspace_state,
    write_workspace_state,
)


def _source_path(workspace: Path, name: str) -> Path:
    return workspace.parent.parent / "repos" / name


def _commit(path: Path, filename: str, content: str, message: str) -> str:
    (path / filename).write_text(content, encoding="utf-8")
    git(path, "add", filename)
    git(path, "commit", "-m", message)
    return git_output(path, "rev-parse", "HEAD")


def _identities(*worktrees: Path) -> dict[Path, tuple[str, str]]:
    return {
        worktree: (
            git_output(worktree, "rev-parse", "HEAD"),
            git_output(worktree, "symbolic-ref", "--quiet", "--short", "HEAD", check=False),
        )
        for worktree in worktrees
    }


def two_repo_workspace_with_release_refs(tmp_path: Path, git_repo):
    app_source = git_repo("app")
    api_source = git_repo("api")
    workspace = create_two_repo_workspace(tmp_path, app_source, api_source, "ref-operations")

    app_release = app_source.commit("release", content="app release\n")
    app_source.run("branch", "release")
    api_release = api_source.commit("release", content="api release\n")
    api_source.run("branch", "release")
    return (
        workspace,
        workspace / "repos" / "app",
        workspace / "repos" / "api",
        app_release,
        api_release,
    )


def _claim_both() -> None:
    workspace_module.claim_workspace("app", target="feature/app")
    workspace_module.claim_workspace("api", target="feature/api")


def test_switch_explicit_repository_changes_only_selected_worktree(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    workspace, app, api, _app_release, api_release = two_repo_workspace_with_release_refs(
        tmp_path, git_repo
    )
    before = deserialize_workspace_state((workspace / ".ws" / "state.toml").read_text())
    app_before = git_output(app, "rev-parse", "HEAD")
    monkeypatch.chdir(app)

    workspace_module.switch_workspace("release", ("api",))

    after = deserialize_workspace_state((workspace / ".ws" / "state.toml").read_text())
    assert git_output(app, "rev-parse", "HEAD") == app_before
    assert git_output(api, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) == ""
    assert git_output(api, "rev-parse", "HEAD") == api_release
    assert serialize_workspace_state(replace(after, repos={"app": after.repos["app"]})) == (
        serialize_workspace_state(replace(before, repos={"app": before.repos["app"]}))
    )


def test_switch_without_names_changes_all_worktrees(tmp_path: Path, git_repo, monkeypatch) -> None:
    workspace, app, api, app_release, api_release = two_repo_workspace_with_release_refs(
        tmp_path, git_repo
    )
    monkeypatch.chdir(app)

    workspace_module.switch_workspace("release")

    assert git_output(app, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) == ""
    assert git_output(api, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) == ""
    assert git_output(app, "rev-parse", "HEAD") == app_release
    assert git_output(api, "rev-parse", "HEAD") == api_release


def test_switch_rejects_unknown_or_duplicate_names_without_mutation(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    _workspace, app, api, _app_release, _api_release = two_repo_workspace_with_release_refs(
        tmp_path, git_repo
    )
    before = _identities(app, api)
    monkeypatch.chdir(app)

    with pytest.raises(WsError):
        workspace_module.switch_workspace("release", ("missing",))
    with pytest.raises(WsError):
        workspace_module.switch_workspace("release", ("api", "api"))
    with pytest.raises(WsError, match="invalid"):
        workspace_module.switch_workspace("release", ("bad/name",))

    assert _identities(app, api) == before


def test_switch_rejects_ref_missing_from_one_target_without_mutation(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    workspace, app, api, _app_release, _api_release = two_repo_workspace_with_release_refs(
        tmp_path, git_repo
    )
    app_source = _source_path(workspace, "app")
    git(app_source, "branch", "app-only-release")
    before = _identities(app, api)
    monkeypatch.chdir(app)

    with pytest.raises(WsError, match="api"):
        workspace_module.switch_workspace("app-only-release")

    assert _identities(app, api) == before


def test_switch_rejects_dirty_target_without_mutation(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    _workspace, app, api, _app_release, _api_release = two_repo_workspace_with_release_refs(
        tmp_path, git_repo
    )
    before = _identities(app, api)
    (api / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    monkeypatch.chdir(app)

    with pytest.raises(WsError):
        workspace_module.switch_workspace("release")

    assert _identities(app, api) == before


def test_switch_rejects_unmerged_or_in_progress_target_without_mutation(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    workspace, app, api, _app_release, api_release = two_repo_workspace_with_release_refs(
        tmp_path, git_repo
    )
    api_source = _source_path(workspace, "api")
    git(api, "switch", "-c", "conflicting")
    _commit(api, "README.md", "worktree conflict\n", "worktree conflict")
    conflict = git(api, "merge", api_release, check=False)
    assert conflict.returncode != 0
    assert git_output(api, "ls-files", "--unmerged")
    before = _identities(app, api)
    monkeypatch.chdir(app)

    with pytest.raises(WsError):
        workspace_module.switch_workspace("release")
    git(api, "merge", "--abort")

    marker = Path(git_output(api, "rev-parse", "--git-dir")) / "rebase-merge"
    marker.mkdir()
    try:
        with pytest.raises(WsError):
            workspace_module.switch_workspace("release")
    finally:
        marker.rmdir()

    assert _identities(app, api) == before
    assert api_source.is_dir()


def test_merge_rejects_detached_target_without_mutation(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    _workspace, app, api, _app_release, _api_release = two_repo_workspace_with_release_refs(
        tmp_path, git_repo
    )
    before = _identities(app, api)
    monkeypatch.chdir(app)

    with pytest.raises(WsError):
        workspace_module.merge_workspace("release")

    assert _identities(app, api) == before


def test_merge_fast_forwards_claimed_targets_and_refreshes_state(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    workspace, app, api, _app_release, _api_release = two_repo_workspace_with_release_refs(
        tmp_path, git_repo
    )
    monkeypatch.chdir(app)
    _claim_both()
    app_source = _source_path(workspace, "app")
    api_source = _source_path(workspace, "api")
    git(app_source, "switch", "release")
    app_release = _commit(app_source, "app-release.txt", "app\n", "app release descendant")
    git(api_source, "switch", "release")
    api_release = _commit(api_source, "api-release.txt", "api\n", "api release descendant")

    workspace_module.merge_workspace("release")

    assert git_output(app, "rev-parse", "HEAD") == app_release
    assert git_output(api, "rev-parse", "HEAD") == api_release
    state = deserialize_workspace_state((workspace / ".ws" / "state.toml").read_text())
    for name, worktree in (("app", app), ("api", api)):
        live = workspace_module._read_live_repo(worktree)
        saved = state.repos[name]
        assert (saved.mode, saved.head, saved.branch, saved.detached, saved.dirty) == (
            live["mode"],
            live["head"],
            live["branch"],
            live["detached"],
            live["dirty"],
        )
        assert saved.context is None


def test_merge_creates_non_fast_forward_commit(tmp_path: Path, git_repo, monkeypatch) -> None:
    workspace, app, api, _app_release, _api_release = two_repo_workspace_with_release_refs(
        tmp_path, git_repo
    )
    base = git_output(app, "rev-parse", "HEAD")
    monkeypatch.chdir(app)
    workspace_module.claim_workspace("app", target="feature/app")
    _commit(app, "app-only.txt", "app\n", "worktree branch commit")
    app_source = _source_path(workspace, "app")
    git(app_source, "switch", "-C", "release", base)
    release = _commit(app_source, "release-only.txt", "release\n", "unrelated release")
    api_before = git_output(api, "rev-parse", "HEAD")

    workspace_module.merge_workspace("release", ("app",))

    parents = git_output(app, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert len(parents) == 3
    assert parents[1] != parents[2]
    assert release in parents
    assert git_output(api, "rev-parse", "HEAD") == api_before


def test_ref_operation_rejects_context_removal_and_operation_lock_states(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    workspace, app, api, _app_release, _api_release = two_repo_workspace_with_release_refs(
        tmp_path, git_repo
    )
    monkeypatch.chdir(app)
    workspace_module.enter_context("app", "HEAD")
    with pytest.raises(WsError, match="context"):
        workspace_module.switch_workspace("release")
    workspace_module.restore_context("app")

    state_path = workspace / ".ws" / "state.toml"
    state = deserialize_workspace_state(state_path.read_text())
    removal = RemovalState(
        phase="removing",
        workspace_path=workspace,
        tombstone_path=workspace.parent / ".ref-operations.removing",
        repos={
            name: RemovalRepoState(
                name=name,
                worktree_path=workspace / "repos" / name,
                git_admin_path=worktree_admin_path(worktree),
                complete=False,
            )
            for name, worktree in (("app", app), ("api", api))
        },
    )
    write_workspace_state(state_path, replace(state, phase="removing", removal=removal))
    with pytest.raises(WsError, match="removal"):
        workspace_module.switch_workspace("release")

    write_workspace_state(state_path, state)
    operation_lock = workspace / ".ws" / "operation.lock"
    operation_lock.mkdir()
    try:
        with pytest.raises(WsError, match="operation lock"):
            workspace_module.switch_workspace("release")
    finally:
        operation_lock.rmdir()


def test_switch_second_target_failure_restores_all_started_targets(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    _workspace, app, api, _app_release, api_release = two_repo_workspace_with_release_refs(
        tmp_path, git_repo
    )
    before = _identities(app, api)
    original = ref_operations.run_git

    def fail_api_switch(args, *, cwd=None, check=True):
        command = tuple(str(arg) for arg in args)
        if cwd == api and command == ("switch", "--detach", api_release):
            raise GitCommandError(command, cwd, 1, "", "injected switch")
        return original(args, cwd=cwd, check=check)

    monkeypatch.setattr(ref_operations, "run_git", fail_api_switch)
    monkeypatch.chdir(app)

    with pytest.raises(WsError):
        workspace_module.switch_workspace("release")

    assert _identities(app, api) == before
    assert git_output(app, "status", "--porcelain", "--untracked-files=all") == ""
    assert git_output(api, "status", "--porcelain", "--untracked-files=all") == ""


def test_merge_second_target_failure_restores_all_started_targets(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    workspace, app, api, _app_release, api_release = two_repo_workspace_with_release_refs(
        tmp_path, git_repo
    )
    monkeypatch.chdir(app)
    _claim_both()
    before = _identities(app, api)
    original = ref_operations.run_git

    def fail_api_merge(args, *, cwd=None, check=True):
        command = tuple(str(arg) for arg in args)
        if cwd == api and command == ("merge", api_release):
            raise GitCommandError(command, cwd, 1, "", "injected merge")
        return original(args, cwd=cwd, check=check)

    monkeypatch.setattr(ref_operations, "run_git", fail_api_merge)

    with pytest.raises(WsError):
        workspace_module.merge_workspace("release")

    assert _identities(app, api) == before
    assert git_output(app, "status", "--porcelain", "--untracked-files=all") == ""
    assert git_output(api, "status", "--porcelain", "--untracked-files=all") == ""


def test_merge_reports_real_git_rollback_failure_and_restores_original_state(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    workspace, app, api, _app_release, api_release = two_repo_workspace_with_release_refs(
        tmp_path, git_repo
    )
    monkeypatch.chdir(app)
    _claim_both()
    before = _identities(app, api)
    app_head = before[app][0]
    original = ref_operations.run_git

    def fail_api_merge_and_rollback(args, *, cwd=None, check=True):
        command = tuple(str(arg) for arg in args)
        if cwd == api and command == ("merge", api_release):
            raise GitCommandError(command, cwd, 1, "", "injected merge failure")
        if cwd == app and command == ("reset", "--hard", app_head):
            raise GitCommandError(command, cwd, 1, "", "injected rollback failure")
        return original(args, cwd=cwd, check=check)

    monkeypatch.setattr(ref_operations, "run_git", fail_api_merge_and_rollback)

    with pytest.raises(WsError) as caught:
        workspace_module.merge_workspace("release")

    assert "merge" in str(caught.value)
    assert "rollback diagnostics" in str(caught.value)
    assert "app" in str(caught.value)
    assert _identities(api)[api] == before[api]
    assert _identities(app)[app][1] == before[app][1]
    assert _identities(app)[app][0] != before[app][0]
    assert git_output(app, "status", "--porcelain", "--untracked-files=all") == ""
    assert git_output(api, "status", "--porcelain", "--untracked-files=all") == ""


def test_state_persistence_failure_rolls_back_git_and_preserves_state(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    workspace, app, api, _app_release, _api_release = two_repo_workspace_with_release_refs(
        tmp_path, git_repo
    )
    before = _identities(app, api)
    state_path = workspace / ".ws" / "state.toml"
    state_before = state_path.read_bytes()
    original_state = deserialize_workspace_state(state_before.decode())
    original_write = workspace_module.write_workspace_state

    def fail_state_write(path, state, **kwargs):
        if path == state_path and state != original_state:
            original_write(path, state, **kwargs)
            raise OSError("injected state persistence failure")
        return original_write(path, state, **kwargs)

    monkeypatch.setattr(workspace_module, "write_workspace_state", fail_state_write)
    monkeypatch.chdir(app)

    with pytest.raises(WsError, match="state persistence failure") as caught:
        workspace_module.switch_workspace("release")

    assert "workspace-state persistence failed" in str(caught.value)
    assert "switch api" not in str(caught.value)
    assert _identities(app, api) == before
    assert state_path.read_bytes() == state_before
    assert git_output(app, "status", "--porcelain", "--untracked-files=all") == ""
    assert git_output(api, "status", "--porcelain", "--untracked-files=all") == ""


def test_state_restoration_failure_is_reported_after_persistence_failure(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    workspace, app, api, _app_release, _api_release = two_repo_workspace_with_release_refs(
        tmp_path, git_repo
    )
    before = _identities(app, api)
    original_state = deserialize_workspace_state(
        (workspace / ".ws" / "state.toml").read_bytes().decode()
    )
    original_write = workspace_module.write_workspace_state

    def fail_state_restore(path, state, **kwargs):
        if path == workspace / ".ws" / "state.toml" and state != original_state:
            original_write(path, state, **kwargs)
            raise OSError("injected persistence failure")
        if path == workspace / ".ws" / "state.toml":
            raise OSError("injected restoration failure")
        return original_write(path, state, **kwargs)

    monkeypatch.setattr(workspace_module, "write_workspace_state", fail_state_restore)
    monkeypatch.chdir(app)

    with pytest.raises(WsError) as caught:
        workspace_module.switch_workspace("release")

    message = str(caught.value)
    assert "injected persistence failure" in message
    assert "original-state restoration failed" in message
    assert _identities(app, api) == before
