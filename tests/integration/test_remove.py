from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import ws_tool.workspace as workspace_module
from ws_tool.errors import GitCommandError, WsError
from ws_tool.serialization import deserialize_workspace_state


def run_ws(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from ws_tool.cli import main; raise SystemExit(main())",
            *args,
        ],
        cwd=cwd,
        text=True,
        capture_output=True,
        env=os.environ.copy(),
    )


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)


def git_output(cwd: Path, *args: str, check: bool = True) -> str:
    return git(cwd, *args, check=check).stdout.strip()


def write_config(path: Path, workspace_root: Path, sources: dict[str, Path]) -> Path:
    repos = "\n".join(
        f'[repos."{name}"]\npath = {json.dumps(str(source))}' for name, source in sources.items()
    )
    path.write_text(
        f"[project]\nworkspace_root = {json.dumps(str(workspace_root))}\n\n{repos}\n",
        encoding="utf-8",
    )
    return path


def create_workspace(tmp_path: Path, source: Path, name: str) -> tuple[Path, Path]:
    config_dir = tmp_path / f"config-{name}"
    config_dir.mkdir()
    config = write_config(config_dir / "ws.toml", tmp_path / "workspaces", {"app": source})
    result = run_ws(config_dir, "create", name, "--config", str(config))
    assert result.returncode == 0, result.stderr
    return tmp_path / "workspaces" / name, config


def test_remove_happy_path_preserves_source_branch_and_unrelated_worktree(tmp_path: Path, git_repo):
    source = git_repo("remove-happy")
    source.branch("keep/branch")
    unrelated = tmp_path / "unrelated-worktree"
    source.run("worktree", "add", str(unrelated), "keep/branch")
    workspace, config = create_workspace(tmp_path, source.path, "happy")
    worktree = workspace / "repos" / "app"

    removed = run_ws(tmp_path / "config-happy", "remove", "happy", "--config", str(config))

    assert removed.returncode == 0, removed.stderr
    assert not workspace.exists()
    assert source.path.is_dir()
    assert git_output(source.path, "rev-parse", "refs/heads/keep/branch")
    assert unrelated.is_dir()
    assert str(worktree) not in git_output(source.path, "worktree", "list")
    assert str(unrelated) in git_output(source.path, "worktree", "list")


def test_remove_refuses_dirty_worktree_without_mutation(tmp_path: Path, git_repo):
    source = git_repo("remove-dirty")
    workspace, config = create_workspace(tmp_path, source.path, "dirty")
    worktree = workspace / "repos" / "app"
    (worktree / "README.md").write_text("dirty\n", encoding="utf-8")

    result = run_ws(tmp_path / "config-dirty", "remove", "dirty", "--config", str(config))

    assert result.returncode != 0
    assert "uncommitted" in result.stderr
    assert workspace.exists()
    assert worktree.exists()
    assert git_output(source.path, "worktree", "list").count(str(worktree)) == 1


def test_remove_refuses_active_context_without_mutation(tmp_path: Path, git_repo):
    source = git_repo("remove-context")
    workspace, config = create_workspace(tmp_path, source.path, "context")
    worktree = workspace / "repos" / "app"
    entered = run_ws(worktree, "context", "app", "HEAD")
    assert entered.returncode == 0, entered.stderr

    result = run_ws(tmp_path / "config-context", "remove", "context", "--config", str(config))

    assert result.returncode != 0
    assert "context" in result.stderr
    assert workspace.exists()
    assert worktree.exists()


def test_remove_refuses_unavailable_locked_source(tmp_path: Path, git_repo):
    source = git_repo("remove-unavailable")
    workspace, config = create_workspace(tmp_path, source.path, "unavailable")
    moved_source = tmp_path / "source-unavailable"
    source.path.rename(moved_source)

    result = run_ws(workspace / "repos" / "app", "remove", "unavailable")

    assert result.returncode != 0
    assert "Git repository root" in result.stderr or "configuration file" not in result.stderr
    assert workspace.exists()
    assert moved_source.exists()


def test_remove_refuses_unregistered_expected_path_without_mutation(tmp_path: Path, git_repo):
    source = git_repo("remove-identity")
    workspace, config = create_workspace(tmp_path, source.path, "identity")
    worktree = workspace / "repos" / "app"
    git(source.path, "worktree", "remove", str(worktree))
    worktree.mkdir(parents=True)
    git(worktree, "init")

    result = run_ws(tmp_path / "config-identity", "remove", "identity", "--config", str(config))

    assert result.returncode != 0
    assert "worktree" in result.stderr or "registration" in result.stderr
    assert workspace.exists()
    assert worktree.exists()
    assert source.path.is_dir()


def test_remove_refuses_absent_path_and_registration_before_durable_progress(
    tmp_path: Path, git_repo
) -> None:
    source = git_repo("remove-absent-initial")
    workspace, config = create_workspace(tmp_path, source.path, "absent-initial")
    worktree = workspace / "repos" / "app"
    git(source.path, "worktree", "remove", str(worktree))

    result = run_ws(
        tmp_path / "config-absent-initial",
        "remove",
        "absent-initial",
        "--config",
        str(config),
    )

    assert result.returncode != 0
    assert "preflight" in result.stderr
    assert workspace.exists()
    assert (workspace / ".ws" / "state.toml").is_file()


@pytest.mark.parametrize("lock_kind", ["operation", "lifecycle"])
def test_remove_refuses_existing_lock_without_breaking_it(
    tmp_path: Path, git_repo, lock_kind: str
) -> None:
    source = git_repo(f"remove-lock-{lock_kind}")
    workspace, config = create_workspace(tmp_path, source.path, f"lock-{lock_kind}")
    if lock_kind == "operation":
        lock = workspace / ".ws" / "operation.lock"
    else:
        lock = workspace.parent / f".{workspace.name}.lifecycle.lock"
    lock.mkdir()

    result = run_ws(
        tmp_path / f"config-lock-{lock_kind}",
        "remove",
        f"lock-{lock_kind}",
        "--config",
        str(config),
    )

    assert result.returncode != 0
    assert lock.is_dir()
    assert workspace.exists()


def test_remove_partial_later_failure_persists_progress_and_retries_only_remaining(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    app = git_repo("remove-partial-app")
    library = git_repo("remove-partial-library")
    config_dir = tmp_path / "config-partial"
    config_dir.mkdir()
    config = write_config(
        config_dir / "ws.toml",
        tmp_path / "workspaces",
        {"app": app.path, "library": library.path},
    )
    result = run_ws(config_dir, "create", "partial", "--config", str(config))
    assert result.returncode == 0, result.stderr
    workspace = tmp_path / "workspaces" / "partial"
    original_run_git = workspace_module.run_git
    removes = 0

    def fail_later_remove(args, *, cwd=None, check=True):
        nonlocal removes
        command = tuple(str(arg) for arg in args)
        if command[:2] == ("worktree", "remove"):
            removes += 1
            if removes == 2:
                raise GitCommandError(command, cwd, 1, "", "injected later removal failure")
        return original_run_git(args, cwd=cwd, check=check)

    monkeypatch.setattr(workspace_module, "run_git", fail_later_remove)
    monkeypatch.chdir(workspace)
    with pytest.raises(WsError, match="injected later removal failure"):
        workspace_module.remove_workspace("partial", config_path=config)

    state = deserialize_workspace_state(
        (workspace / ".ws" / "state.toml").read_text(encoding="utf-8")
    )
    assert state.phase == "removing"
    assert state.removal is not None
    assert state.removal.repos["app"].complete
    assert not state.removal.repos["library"].complete
    assert not (workspace / "repos" / "app").exists()
    assert (workspace / "repos" / "library").exists()
    assert app.path.is_dir()
    assert library.path.is_dir()
    assert not (workspace / ".ws" / "operation.lock").exists()
    assert not (workspace.parent / ".partial.lifecycle.lock").exists()

    retried = run_ws(config_dir, "remove", "partial", "--config", str(config))

    assert retried.returncode == 0, retried.stderr
    assert not workspace.exists()
    assert not (workspace.parent / ".partial.removing").exists()
    assert app.path.is_dir()
    assert library.path.is_dir()


def test_remove_keyboard_interrupt_retains_locks_and_progress_for_retry(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    source = git_repo("remove-keyboard")
    workspace, config = create_workspace(tmp_path, source.path, "keyboard")
    worktree = workspace / "repos" / "app"
    original_run_git = workspace_module.run_git

    def interrupt_remove(args, *, cwd=None, check=True):
        command = tuple(str(arg) for arg in args)
        if command[:2] == ("worktree", "remove"):
            raise KeyboardInterrupt
        return original_run_git(args, cwd=cwd, check=check)

    monkeypatch.setattr(workspace_module, "run_git", interrupt_remove)
    monkeypatch.chdir(workspace)
    with pytest.raises(KeyboardInterrupt):
        workspace_module.remove_workspace("keyboard", config_path=config)

    state_path = workspace / ".ws" / "state.toml"
    state = deserialize_workspace_state(state_path.read_text(encoding="utf-8"))
    assert state.phase == "removing"
    assert state.removal is not None
    assert not state.removal.repos["app"].complete
    lifecycle_lock = workspace.parent / ".keyboard.lifecycle.lock"
    operation_lock = workspace / ".ws" / "operation.lock"
    assert lifecycle_lock.is_dir()
    assert operation_lock.is_dir()
    assert worktree.is_dir()

    operation_lock.rmdir()
    lifecycle_lock.rmdir()
    retried = run_ws(tmp_path / "config-keyboard", "remove", "keyboard", "--config", str(config))

    assert retried.returncode == 0, retried.stderr
    assert not workspace.exists()
    assert source.path.is_dir()


def test_remove_crash_after_git_deletion_credits_absent_state_only_on_retry(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    source = git_repo("remove-absent-retry")
    workspace, config = create_workspace(tmp_path, source.path, "absent-retry")
    worktree = workspace / "repos" / "app"
    original_run_git = workspace_module.run_git

    def delete_then_interrupt(args, *, cwd=None, check=True):
        command = tuple(str(arg) for arg in args)
        if command[:2] == ("worktree", "remove"):
            original_run_git(args, cwd=cwd, check=check)
            raise KeyboardInterrupt
        return original_run_git(args, cwd=cwd, check=check)

    monkeypatch.setattr(workspace_module, "run_git", delete_then_interrupt)
    monkeypatch.chdir(workspace)
    with pytest.raises(KeyboardInterrupt):
        workspace_module.remove_workspace("absent-retry", config_path=config)

    state = deserialize_workspace_state(
        (workspace / ".ws" / "state.toml").read_text(encoding="utf-8")
    )
    assert state.removal is not None
    assert not state.removal.repos["app"].complete
    assert not worktree.exists()
    assert (workspace.parent / ".absent-retry.lifecycle.lock").is_dir()
    assert (workspace / ".ws" / "operation.lock").is_dir()

    (workspace / ".ws" / "operation.lock").rmdir()
    (workspace.parent / ".absent-retry.lifecycle.lock").rmdir()
    retried = run_ws(
        tmp_path / "config-absent-retry",
        "remove",
        "absent-retry",
        "--config",
        str(config),
    )

    assert retried.returncode == 0, retried.stderr
    assert not workspace.exists()


def test_remove_tombstone_cleanup_interrupt_retains_parseable_state_and_locks(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    source = git_repo("remove-tombstone-crash")
    workspace, config = create_workspace(tmp_path, source.path, "tombstone-crash")
    original_rmtree = workspace_module.shutil.rmtree
    tombstone = workspace.parent / ".tombstone-crash.removing"

    def interrupt_cleanup(path):
        assert path == tombstone
        original_rmtree(path)
        raise KeyboardInterrupt

    monkeypatch.setattr(workspace_module.shutil, "rmtree", interrupt_cleanup)
    monkeypatch.chdir(workspace)
    with pytest.raises(KeyboardInterrupt):
        workspace_module.remove_workspace("tombstone-crash", config_path=config)

    state_path = tombstone / ".ws" / "state.toml"
    state = deserialize_workspace_state(state_path.read_text(encoding="utf-8"))
    assert state.phase == "removal_complete"
    assert tombstone.is_dir()
    lifecycle_lock = workspace.parent / ".tombstone-crash.lifecycle.lock"
    operation_lock = tombstone / ".ws" / "operation.lock"
    assert lifecycle_lock.is_dir()
    assert operation_lock.is_dir()
    assert not workspace.exists()
    status = run_ws(tombstone, "status", "--json")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["removal"]["phase"] == "removal_complete"

    operation_lock.rmdir()
    lifecycle_lock.rmdir()
    monkeypatch.setattr(workspace_module.shutil, "rmtree", original_rmtree)
    retried = run_ws(
        tmp_path / "config-tombstone-crash",
        "remove",
        "tombstone-crash",
        "--config",
        str(config),
    )

    assert retried.returncode == 0, retried.stderr
    assert not tombstone.exists()
    assert source.path.is_dir()


def test_remove_config_disagreement_inside_workspace_refuses_before_mutation(
    tmp_path: Path, git_repo
) -> None:
    source = git_repo("remove-config-disagreement")
    workspace, config = create_workspace(tmp_path, source.path, "disagreement")
    other_config_dir = tmp_path / "other-config"
    other_config_dir.mkdir()
    other_config = write_config(
        other_config_dir / "ws.toml", tmp_path / "other-workspaces", {"app": source.path}
    )

    result = run_ws(workspace, "remove", "disagreement", "--config", str(other_config))

    assert result.returncode != 0
    assert "explicit config" in result.stderr
    assert workspace.exists()
    assert config.exists()
