from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from test_phase2 import adopt_source, initialize_sources, prepare_remote, repo_table

import ws_tool.workspace as workspace_module
from ws_tool.errors import GitCommandError, WsError
from ws_tool.models import RemovalSeal
from ws_tool.serialization import (
    deserialize_removal_seal,
    deserialize_workspace_state,
    dump_toml,
    write_removal_seal,
)


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


def write_config(path: Path, sources: dict[str, Path]) -> Path:
    repos = "\n".join(
        repo_table(name, prepare_remote(name, source)) for name, source in sources.items()
    )
    path.write_text(repos, encoding="utf-8")
    return path


def create_workspace(tmp_path: Path, source, name: str) -> tuple[Path, Path]:
    config_dir = tmp_path / f"config-{name}"
    config_dir.mkdir()
    source_path = source.path if hasattr(source, "path") else source
    config = write_config(config_dir / "ws.toml", {"app": source_path})
    initialize_sources(config_dir)
    if hasattr(source, "path"):
        adopt_source(source, config_dir, "app")
    result = run_ws(config_dir, "create", name, "--config", str(config))
    assert result.returncode == 0, result.stderr
    return config_dir / "workspaces" / name, config


@pytest.fixture
def sealed_tombstone(tmp_path: Path, git_repo, monkeypatch) -> tuple[Path, Path, RemovalSeal]:
    source = git_repo("sealed-tombstone")
    workspace, config = create_workspace(tmp_path, source, "sample")

    # Stop immediately after the durable rename so this fixture supplies a
    # complete tombstone without making the test depend on private file text.
    monkeypatch.setattr(workspace_module, "_resume_sealed_tombstone_cleanup", lambda *_args: None)
    workspace_module.remove_workspace("sample", config_path=config)
    tombstone = workspace.parent / ".sample.removing"
    seal = deserialize_removal_seal((tombstone / "removal-seal.toml").read_text(encoding="utf-8"))
    (tombstone / ".ws" / "operation.lock").rmdir()
    return tombstone, config.parent, seal


def test_retry_removes_seal_only_tombstone(
    sealed_tombstone: tuple[Path, Path, RemovalSeal]
) -> None:
    tombstone, config_dir, seal = sealed_tombstone
    (tombstone / "workspace.lock.toml").unlink()
    (tombstone / ".ws" / "state.toml").unlink()
    if (tombstone / ".ws" / "operation.lock").exists():
        (tombstone / ".ws" / "operation.lock").rmdir()
    (tombstone / ".ws").rmdir()
    (tombstone / "repos").rmdir()
    write_removal_seal(tombstone / "removal-seal.toml", seal)

    run = run_ws(config_dir, "remove", "sample", "--config", str(config_dir / "ws.toml"))

    assert run.returncode == 0, run.stderr
    assert not tombstone.exists()


def test_retry_refuses_unknown_sealed_tombstone_entry(
    sealed_tombstone: tuple[Path, Path, RemovalSeal]
) -> None:
    tombstone, config_dir, _seal = sealed_tombstone
    (tombstone / "unexpected").write_text("unsafe", encoding="utf-8")

    result = run_ws(config_dir, "remove", "sample", "--config", str(config_dir / "ws.toml"))

    assert result.returncode != 0
    assert "unexpected tombstone entry" in result.stderr
    assert tombstone.exists()


def test_retry_refuses_symlinked_sealed_tombstone_directory(
    sealed_tombstone: tuple[Path, Path, RemovalSeal], tmp_path: Path
) -> None:
    tombstone, config_dir, _seal = sealed_tombstone
    (tombstone / "repos").rmdir()
    (tombstone / "repos").symlink_to(tmp_path)

    result = run_ws(config_dir, "remove", "sample", "--config", str(config_dir / "ws.toml"))

    assert result.returncode != 0
    assert "unexpected tombstone entry" in result.stderr
    assert (tombstone / "repos").is_symlink()


def test_retry_upgrades_complete_legacy_tombstone(
    sealed_tombstone: tuple[Path, Path, RemovalSeal]
) -> None:
    tombstone, config_dir, _seal = sealed_tombstone
    seal_path = tombstone / "removal-seal.toml"
    seal_path.unlink()
    state_path = tombstone / ".ws" / "state.toml"
    legacy = tomllib.loads(state_path.read_text(encoding="utf-8"))
    legacy["schema_version"] = 1
    del legacy["state"]["removal"]["seal"]
    state_path.write_text(dump_toml(legacy), encoding="utf-8")

    result = run_ws(config_dir, "remove", "sample", "--config", str(config_dir / "ws.toml"))

    assert result.returncode == 0, result.stderr
    assert not tombstone.exists()


def test_retry_removes_exact_empty_terminal_tombstone(
    sealed_tombstone: tuple[Path, Path, RemovalSeal]
) -> None:
    tombstone, config_dir, _seal = sealed_tombstone
    (tombstone / "workspace.lock.toml").unlink()
    (tombstone / ".ws" / "state.toml").unlink()
    if (tombstone / ".ws" / "operation.lock").exists():
        (tombstone / ".ws" / "operation.lock").rmdir()
    (tombstone / ".ws").rmdir()
    (tombstone / "repos").rmdir()
    (tombstone / "removal-seal.toml").unlink()

    result = run_ws(config_dir, "remove", "sample", "--config", str(config_dir / "ws.toml"))

    assert result.returncode == 0, result.stderr
    assert not tombstone.exists()


def test_retry_refuses_embedded_standalone_seal_mismatch(
    sealed_tombstone: tuple[Path, Path, RemovalSeal]
) -> None:
    tombstone, config_dir, seal = sealed_tombstone
    repo = seal.repos["app"]
    mismatched = replace(seal, repos={"app": replace(repo, branch="refs/heads/other")})
    write_removal_seal(tombstone / "removal-seal.toml", mismatched)

    result = run_ws(config_dir, "remove", "sample", "--config", str(config_dir / "ws.toml"))

    assert result.returncode != 0
    assert "seals do not match" in result.stderr
    assert tombstone.exists()


def test_retry_refuses_seal_terminal_state_identity_mismatch(
    sealed_tombstone: tuple[Path, Path, RemovalSeal]
) -> None:
    tombstone, config_dir, _seal = sealed_tombstone
    state_path = tombstone / ".ws" / "state.toml"
    state_data = tomllib.loads(state_path.read_text(encoding="utf-8"))
    state_data["state"]["removal"]["seal"]["repos"]["app"]["dirty"] = True
    state_path.write_text(dump_toml(state_data), encoding="utf-8")

    standalone_path = tombstone / "removal-seal.toml"
    standalone_data = tomllib.loads(standalone_path.read_text(encoding="utf-8"))
    standalone_data["seal"]["repos"]["app"]["dirty"] = True
    standalone_path.write_text(dump_toml(standalone_data), encoding="utf-8")

    result = run_ws(config_dir, "remove", "sample", "--config", str(config_dir / "ws.toml"))

    assert result.returncode != 0
    assert "terminal identity does not match state" in result.stderr
    assert tombstone.exists()


def test_retry_refuses_seal_lock_identity_mismatch(
    sealed_tombstone: tuple[Path, Path, RemovalSeal]
) -> None:
    tombstone, config_dir, _seal = sealed_tombstone
    lock_path = tombstone / "workspace.lock.toml"
    lock_data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    lock_data["repos"]["app"]["base_ref"] = "refs/heads/other"
    lock_path.write_text(dump_toml(lock_data), encoding="utf-8")

    result = run_ws(config_dir, "remove", "sample", "--config", str(config_dir / "ws.toml"))

    assert result.returncode != 0
    assert "terminal identity does not match lock" in result.stderr
    assert tombstone.exists()


def test_retry_refuses_non_monotonic_sealed_tombstone(
    sealed_tombstone: tuple[Path, Path, RemovalSeal]
) -> None:
    tombstone, config_dir, _seal = sealed_tombstone
    (tombstone / ".ws" / "state.toml").unlink()

    result = run_ws(config_dir, "remove", "sample", "--config", str(config_dir / "ws.toml"))

    assert result.returncode != 0
    assert "non-monotonic" in result.stderr
    assert tombstone.exists()


def test_empty_terminal_retry_retains_lifecycle_lock_on_final_fsync_failure(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    source = git_repo("empty-terminal-fsync")
    config_dir = tmp_path / "config-empty-terminal-fsync"
    config_dir.mkdir()
    config = write_config(config_dir / "ws.toml", {"app": source.path})
    tombstone = config_dir / "workspaces" / ".sample.removing"
    tombstone.mkdir(parents=True)
    workspace_root = tombstone.parent
    original_fsync = workspace_module.fsync_directory

    def fail_final_parent_fsync(directory: Path) -> None:
        if directory == workspace_root and not tombstone.exists():
            raise OSError("injected final parent fsync failure")
        original_fsync(directory)

    monkeypatch.setattr(workspace_module, "fsync_directory", fail_final_parent_fsync)
    monkeypatch.chdir(config_dir)
    with pytest.raises(WsError, match="fsync"):
        workspace_module.remove_workspace("sample", config_path=config)

    assert not tombstone.exists()
    lifecycle_lock = workspace_root / ".sample.lifecycle.lock"
    assert lifecycle_lock.is_dir()
    lifecycle_lock.rmdir()


def test_rename_fsync_failure_retains_lifecycle_and_tombstone_operation_locks(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    source = git_repo("rename-fsync-failure")
    workspace, config = create_workspace(tmp_path, source, "rename-fsync")
    tombstone = workspace.parent / ".rename-fsync.removing"
    original_fsync = workspace_module.fsync_directory

    def fail_tombstone_rename_fsync(directory: Path) -> None:
        if directory == workspace.parent and tombstone.exists():
            raise OSError("injected tombstone rename fsync failure")
        original_fsync(directory)

    monkeypatch.setattr(workspace_module, "fsync_directory", fail_tombstone_rename_fsync)
    monkeypatch.chdir(workspace)
    with pytest.raises(WsError, match="injected tombstone rename fsync failure"):
        workspace_module.remove_workspace("rename-fsync", config_path=config)

    assert not workspace.exists()
    assert tombstone.is_dir()
    assert (workspace.parent / ".rename-fsync.lifecycle.lock").is_dir()
    assert (tombstone / ".ws" / "operation.lock").is_dir()

    (tombstone / ".ws" / "operation.lock").rmdir()
    (workspace.parent / ".rename-fsync.lifecycle.lock").rmdir()
    retried = run_ws(
        tmp_path / "config-rename-fsync",
        "remove",
        "rename-fsync",
        "--config",
        str(config),
    )

    assert retried.returncode == 0, retried.stderr
    assert not tombstone.exists()


def test_retry_refuses_stale_tombstone_operation_lock_as_ws_error(
    sealed_tombstone: tuple[Path, Path, RemovalSeal]
) -> None:
    tombstone, config_dir, _seal = sealed_tombstone
    operation_lock = tombstone / ".ws" / "operation.lock"
    operation_lock.mkdir()

    result = run_ws(config_dir, "remove", "sample", "--config", str(config_dir / "ws.toml"))

    assert result.returncode != 0
    assert "workspace operation lock already exists" in result.stderr
    assert operation_lock.is_dir()
    assert tombstone.exists()


def test_retry_refuses_surviving_exact_git_admin_identity(
    sealed_tombstone: tuple[Path, Path, RemovalSeal]
) -> None:
    tombstone, config_dir, seal = sealed_tombstone
    admin = seal.repos["app"].git_admin_path
    admin.parent.mkdir(parents=True)
    admin.mkdir()

    result = run_ws(config_dir, "remove", "sample", "--config", str(config_dir / "ws.toml"))

    assert result.returncode != 0
    assert "administrative identity remains" in result.stderr
    assert tombstone.exists()


def test_remove_happy_path_preserves_source_branch_and_unrelated_worktree(tmp_path: Path, git_repo):
    source = git_repo("remove-happy")
    source.branch("keep/branch")
    unrelated = tmp_path / "unrelated-worktree"
    workspace, config = create_workspace(tmp_path, source, "happy")
    source.run("worktree", "add", str(unrelated), "keep/branch")
    worktree = workspace / "repos" / "app"

    removed = run_ws(tmp_path / "config-happy", "remove", "happy", "--config", str(config))

    assert removed.returncode == 0, removed.stderr
    assert not workspace.exists()
    assert source.path.is_dir()
    assert git_output(source.path, "rev-parse", "refs/heads/keep/branch")
    assert unrelated.is_dir()
    assert str(worktree) not in git_output(source.path, "worktree", "list")
    assert str(unrelated) in git_output(source.path, "worktree", "list")


def test_remove_persists_embedded_and_standalone_seal_before_rename(
    tmp_path: Path, git_repo, monkeypatch
) -> None:
    source = git_repo("remove-seal")
    workspace, config = create_workspace(tmp_path, source, "seal")
    observed: list[tuple[Path, object, object]] = []
    original_write = workspace_module.write_removal_seal

    def observe_seal(path, seal):
        original_write(path, seal)
        persisted = deserialize_workspace_state(
            (workspace / ".ws" / "state.toml").read_text(encoding="utf-8")
        )
        assert workspace.is_dir()
        assert path == workspace / "removal-seal.toml"
        assert persisted.removal is not None
        assert persisted.removal.seal == seal
        observed.append((path, seal, persisted.removal.seal))

    monkeypatch.setattr(workspace_module, "write_removal_seal", observe_seal)

    workspace_module.remove_workspace("seal", config_path=config)

    assert len(observed) == 1
    assert observed[0][1] == observed[0][2]
    assert not workspace.exists()
    assert not (workspace.parent / ".seal.removing").exists()


def test_remove_refuses_dirty_worktree_without_mutation(tmp_path: Path, git_repo):
    source = git_repo("remove-dirty")
    workspace, config = create_workspace(tmp_path, source, "dirty")
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
    workspace, config = create_workspace(tmp_path, source, "context")
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
    workspace, config = create_workspace(tmp_path, source, "unavailable")
    moved_source = tmp_path / "source-unavailable"
    source.path.rename(moved_source)

    result = run_ws(workspace / "repos" / "app", "remove", "unavailable")

    assert result.returncode != 0
    assert "Git repository root" in result.stderr or "configuration file" not in result.stderr
    assert workspace.exists()
    assert moved_source.exists()


def test_remove_refuses_unregistered_expected_path_without_mutation(tmp_path: Path, git_repo):
    source = git_repo("remove-identity")
    workspace, config = create_workspace(tmp_path, source, "identity")
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
    workspace, config = create_workspace(tmp_path, source, "absent-initial")
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
    workspace, config = create_workspace(tmp_path, source, f"lock-{lock_kind}")
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
        {"app": app.path, "library": library.path},
    )
    initialize_sources(config_dir)
    adopt_source(app, config_dir, "app")
    adopt_source(library, config_dir, "library")
    result = run_ws(config_dir, "create", "partial", "--config", str(config))
    assert result.returncode == 0, result.stderr
    workspace = config_dir / "workspaces" / "partial"
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
    workspace, config = create_workspace(tmp_path, source, "keyboard")
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
    workspace, config = create_workspace(tmp_path, source, "absent-retry")
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
    workspace, config = create_workspace(tmp_path, source, "tombstone-crash")
    tombstone = workspace.parent / ".tombstone-crash.removing"

    def interrupt_cleanup(path, seal):
        assert path.workspace == tombstone
        raise KeyboardInterrupt

    monkeypatch.setattr(workspace_module, "_resume_sealed_tombstone_cleanup", interrupt_cleanup)
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
    seal = (tombstone / "removal-seal.toml").read_text(encoding="utf-8")
    assert "removal_complete" in seal

    operation_lock.rmdir()
    lifecycle_lock.rmdir()
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
    workspace, config = create_workspace(tmp_path, source, "disagreement")
    other_config_dir = tmp_path / "other-config"
    other_config_dir.mkdir()
    other_config = write_config(
        other_config_dir / "ws.toml", {"app": source.path}
    )

    result = run_ws(workspace, "remove", "disagreement", "--config", str(other_config))

    assert result.returncode != 0
    assert "explicit config" in result.stderr
    assert workspace.exists()
    assert config.exists()
