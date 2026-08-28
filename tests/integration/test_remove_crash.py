from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from test_phase2 import adopt_source, initialize_sources, prepare_remote, repo_table

from ws_tool.serialization import deserialize_removal_seal, deserialize_workspace_state


def run_ws(
    cwd: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
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
        env=os.environ.copy() if env is None else env,
    )


def git_output(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def write_config(path: Path, source: Path) -> Path:
    path.write_text(repo_table("app", prepare_remote("app", source)), encoding="utf-8")
    return path


def create_workspace(tmp_path: Path, source, name: str) -> tuple[Path, Path]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    source_path = source.path if hasattr(source, "path") else source
    config = write_config(config_dir / "ws.toml", source_path)
    initialize_sources(config_dir)
    if hasattr(source, "path"):
        adopt_source(source, config_dir, "app")
    created = run_ws(config_dir, "create", name, "--config", str(config))
    assert created.returncode == 0, created.stderr
    return config_dir / "workspaces" / name, config


def run_remove_child(
    config_dir: Path, name: str, boundary: str
) -> subprocess.CompletedProcess[str]:
    child = """
import os
import signal
import ws_tool.workspace as workspace_module
from ws_tool.cli import main

def crash_at_boundary(target):
    if os.environ["WS_TEST_CRASH_BOUNDARY"] == target:
        os.kill(os.getpid(), signal.SIGKILL)

workspace_module._removal_test_boundary = crash_at_boundary
raise SystemExit(main())
"""
    environment = os.environ.copy()
    environment["WS_TEST_CRASH_BOUNDARY"] = boundary
    return subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            "remove",
            name,
            "--config",
            str(config_dir / "ws.toml"),
        ],
        cwd=config_dir,
        text=True,
        capture_output=True,
        env=environment,
    )


def release_exact_stale_locks(workspace: Path) -> None:
    lifecycle_lock = workspace.parent / f".{workspace.name}.lifecycle.lock"
    assert lifecycle_lock.is_dir() and not lifecycle_lock.is_symlink()
    lifecycle_lock.rmdir()
    assert not lifecycle_lock.exists()

    location = workspace
    if not workspace.exists():
        location = workspace.parent / f".{workspace.name}.removing"
    assert location.is_dir() and not location.is_symlink()
    operation_lock = location / ".ws" / "operation.lock"
    if location == workspace or any(location.iterdir()):
        assert operation_lock.is_dir() and not operation_lock.is_symlink()
        operation_lock.rmdir()
        assert not operation_lock.exists()
    else:
        assert not operation_lock.exists() and not operation_lock.is_symlink()


def retry_remove(config_dir: Path, name: str) -> subprocess.CompletedProcess[str]:
    return run_ws(config_dir, "remove", name, "--config", str(config_dir / "ws.toml"))


def _admin_path(worktree: Path) -> Path:
    admin = Path(git_output(worktree, "rev-parse", "--git-dir"))
    if not admin.is_absolute():
        admin = worktree / admin
    return admin.resolve(strict=False)


@pytest.mark.parametrize(
    "boundary",
    [
        "removing_persisted",
        "worktree_removed",
        "removal_complete_persisted",
        "standalone_seal_persisted",
        "tombstone_renamed",
        "legacy_metadata_deleted",
        "seal_deleted",
    ],
)
def test_remove_recovers_after_real_sigkill_at_each_boundary(
    tmp_path: Path, git_repo, boundary: str
) -> None:
    source = git_repo(f"remove-crash-{boundary}")
    source.branch("keep/branch")
    unrelated = tmp_path / "unrelated-source-worktree"
    workspace, config = create_workspace(tmp_path, source, "sample")
    source.run("worktree", "add", str(unrelated), "keep/branch")
    unrelated_oid = git_output(unrelated, "rev-parse", "HEAD")
    tombstone = workspace.parent / ".sample.removing"
    worktree = workspace / "repos" / "app"
    admin_path = _admin_path(worktree)
    main_oid = git_output(source.path, "rev-parse", "refs/heads/main")

    crashed = run_remove_child(config.parent, workspace.name, boundary)

    assert crashed.returncode == -signal.SIGKILL
    assert git_output(source.path, "rev-parse", "refs/heads/main") == main_oid
    assert git_output(source.path, "rev-parse", "refs/heads/keep/branch") == unrelated_oid
    assert unrelated.is_dir()
    assert git_output(unrelated, "symbolic-ref", "--short", "HEAD") == "keep/branch"
    assert git_output(unrelated, "rev-parse", "HEAD") == unrelated_oid
    assert git_output(unrelated, "status", "--porcelain") == ""
    assert str(unrelated.resolve()) in git_output(source.path, "worktree", "list")

    normal_boundaries = {
        "removing_persisted",
        "worktree_removed",
        "removal_complete_persisted",
        "standalone_seal_persisted",
    }
    if boundary in normal_boundaries:
        assert workspace.is_dir()
        assert not tombstone.exists()
        state_path = workspace / ".ws" / "state.toml"
        state = deserialize_workspace_state(state_path.read_text(encoding="utf-8"))
        assert state.workspace_name == workspace.name
        assert state.removal is not None
        assert state.removal.workspace_path == workspace.resolve()
        assert state.removal.tombstone_path == tombstone.resolve()
        assert state.removal.repos["app"].git_admin_path == admin_path

        if boundary == "removing_persisted":
            assert state.phase == "removing"
            assert not state.removal.repos["app"].complete
            assert worktree.is_dir()
            assert str(worktree.resolve()) in git_output(source.path, "worktree", "list")
            assert admin_path.is_dir()
        else:
            assert state.removal.repos["app"].complete is (
                boundary in {"removal_complete_persisted", "standalone_seal_persisted"}
            )
            assert not worktree.exists()
            assert str(worktree.resolve()) not in git_output(source.path, "worktree", "list")
            assert not admin_path.exists()

        if boundary == "removal_complete_persisted":
            assert state.phase == "removal_complete"
            assert state.removal.seal is not None
            assert not (workspace / "removal-seal.toml").exists()
        elif boundary == "standalone_seal_persisted":
            assert state.phase == "removal_complete"
            assert state.removal.seal is not None
            standalone = deserialize_removal_seal(
                (workspace / "removal-seal.toml").read_text(encoding="utf-8")
            )
            assert standalone == state.removal.seal
    else:
        assert not workspace.exists()
        assert tombstone.is_dir()
        assert not worktree.exists()
        assert str(worktree.resolve()) not in git_output(source.path, "worktree", "list")
        assert not admin_path.exists()

        if boundary in {"tombstone_renamed", "legacy_metadata_deleted"}:
            seal = deserialize_removal_seal(
                (tombstone / "removal-seal.toml").read_text(encoding="utf-8")
            )
            assert seal.workspace_name == workspace.name
            assert seal.workspace_path == workspace.resolve()
            assert seal.tombstone_path == tombstone.resolve()
            assert seal.repos["app"].source_path == source.path.resolve()
            assert seal.repos["app"].worktree_path == worktree.resolve()
            assert seal.repos["app"].git_admin_path == admin_path

        if boundary == "tombstone_renamed":
            state = deserialize_workspace_state(
                (tombstone / ".ws" / "state.toml").read_text(encoding="utf-8")
            )
            assert state.phase == "removal_complete"
            assert (tombstone / "workspace.lock.toml").is_file()
            assert (tombstone / ".ws" / "operation.lock").is_dir()
        elif boundary == "legacy_metadata_deleted":
            assert not (tombstone / "workspace.lock.toml").exists()
            assert not (tombstone / ".ws" / "state.toml").exists()
            assert (tombstone / ".ws" / "operation.lock").is_dir()
        else:
            assert not any(tombstone.iterdir())

    release_exact_stale_locks(workspace)
    retried = retry_remove(config.parent, workspace.name)

    assert retried.returncode == 0, retried.stderr
    assert not workspace.exists()
    assert not tombstone.exists()
    assert source.path.is_dir()
    assert git_output(source.path, "rev-parse", "refs/heads/main") == main_oid
    assert git_output(source.path, "rev-parse", "refs/heads/keep/branch") == unrelated_oid
    assert unrelated.is_dir()
    assert git_output(unrelated, "symbolic-ref", "--short", "HEAD") == "keep/branch"
    assert git_output(unrelated, "rev-parse", "HEAD") == unrelated_oid
    assert git_output(unrelated, "status", "--porcelain") == ""
    assert str(unrelated.resolve()) in git_output(source.path, "worktree", "list")
    assert str(worktree.resolve()) not in git_output(source.path, "worktree", "list")
    assert not admin_path.exists()
    assert not (workspace.parent / ".sample.lifecycle.lock").exists()
