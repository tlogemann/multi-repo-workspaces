from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from test_phase2 import adopt_source, initialize_sources, prepare_remote, repo_table

from conftest import (
    release_boundary,
    start_paused_create,
    start_paused_remove,
    wait_for_boundary,
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


def git_output(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
    return result.stdout


def write_config(path: Path, source: Path) -> Path:
    path.write_text(repo_table("app", prepare_remote("app", source)), encoding="utf-8")
    return path


def create_workspace(tmp_path: Path, source) -> tuple[Path, Path]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    source_path = source.path if hasattr(source, "path") else source
    config = write_config(config_dir / "ws.toml", source_path)
    initialize_sources(config_dir)
    if hasattr(source, "path"):
        adopt_source(source, config_dir, "app")
    created = run_ws(config_dir, "create", "sample", "--config", str(config))
    assert created.returncode == 0, created.stderr
    return config_dir / "workspaces" / "sample", config


def workspace_snapshot(path: Path) -> tuple[tuple[str, str, bytes], ...]:
    if not path.exists():
        return ()
    entries: list[tuple[str, str, bytes]] = []
    for entry in sorted(path.rglob("*")):
        relative = str(entry.relative_to(path))
        if entry.is_symlink():
            entries.append((relative, "symlink", os.readlink(entry).encode()))
        elif entry.is_dir():
            entries.append((relative, "directory", b""))
        else:
            entries.append((relative, "file", entry.read_bytes()))
    return tuple(entries)


def finish_paused(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        release_boundary(process)
    assert process.wait() == 0, process.stderr.read() if process.stderr is not None else ""


def assert_clean_workspace(workspace: Path, source: Path, registrations: str) -> None:
    assert not workspace.exists()
    assert not (workspace.parent / ".sample.removing").exists()
    assert not (workspace.parent / ".sample.lifecycle.lock").exists()
    assert git_output(source, "worktree", "list", "--porcelain") == registrations


def test_second_create_fails_while_first_holds_lifecycle_lock(
    tmp_path: Path, git_repo
) -> None:
    source = git_repo("create-concurrency")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = write_config(config_dir / "ws.toml", source.path)
    initialize_sources(config_dir)
    adopt_source(source, config_dir, "app")
    registrations = git_output(source.path, "worktree", "list", "--porcelain")
    refs = git_output(source.path, "show-ref")
    first = start_paused_create(config_dir, "sample", "create.lifecycle_locked")
    try:
        wait_for_boundary(first)
        workspace = config_dir / "workspaces" / "sample"
        before = workspace_snapshot(workspace)
        rejected = run_ws(config_dir, "create", "sample", "--config", str(config))
        assert rejected.returncode != 0
        assert "lifecycle lock" in rejected.stderr
        assert workspace_snapshot(workspace) == before
        assert git_output(source.path, "worktree", "list", "--porcelain") == registrations
        assert git_output(source.path, "show-ref") == refs
        finish_paused(first)
    finally:
        if first.poll() is None:
            release_boundary(first)
            first.wait()

    assert (workspace / "workspace.lock.toml").exists()
    assert (workspace / ".ws" / "state.toml").exists()
    assert (workspace / "repos" / "app").exists()
    completed_worktree = (workspace / "repos" / "app").resolve()
    assert f"worktree {completed_worktree}\n" in git_output(
        source.path, "worktree", "list", "--porcelain"
    )
    assert (workspace.parent / ".sample.lifecycle.lock").exists() is False
    assert workspace.parent.joinpath(".sample.removing").exists() is False


def test_second_remove_fails_while_first_holds_lifecycle_lock(
    tmp_path: Path, git_repo
) -> None:
    source = git_repo("remove-concurrency")
    workspace, config = create_workspace(tmp_path, source)
    baseline_registrations = git_output(source.path, "worktree", "list", "--porcelain")
    baseline_refs = git_output(source.path, "show-ref")
    registrations = git_output(source.path, "worktree", "list", "--porcelain")
    baseline_registrations = "\n\n".join(
        block for block in registrations.strip().split("\n\n") if str(workspace) not in block
    ) + "\n\n"
    before = workspace_snapshot(workspace)
    first = start_paused_remove(config.parent, "sample", "remove.lifecycle_locked")
    try:
        wait_for_boundary(first)
        rejected = run_ws(config.parent, "remove", "sample", "--config", str(config))
        assert rejected.returncode != 0
        assert "lifecycle lock" in rejected.stderr
        assert workspace_snapshot(workspace) == before
        assert git_output(source.path, "worktree", "list", "--porcelain") == registrations
        assert git_output(source.path, "show-ref") == baseline_refs
        finish_paused(first)
    finally:
        if first.poll() is None:
            release_boundary(first)
            first.wait()

    assert_clean_workspace(workspace, source.path, baseline_registrations)


def test_mutator_fails_while_removal_holds_lifecycle_and_operation_locks(
    tmp_path: Path, git_repo
) -> None:
    source = git_repo("mutator-concurrency")
    workspace, config = create_workspace(tmp_path, source)
    baseline_registrations = git_output(source.path, "worktree", "list", "--porcelain")
    baseline_refs = git_output(source.path, "show-ref")
    worktree = workspace / "repos" / "app"
    registrations = git_output(source.path, "worktree", "list", "--porcelain")
    baseline_registrations = "\n\n".join(
        block for block in registrations.strip().split("\n\n") if str(workspace) not in block
    ) + "\n\n"
    first = start_paused_remove(config.parent, "sample", "removing_persisted")
    try:
        wait_for_boundary(first)
        assert (workspace / ".ws" / "operation.lock").is_dir()
        assert (workspace.parent / ".sample.lifecycle.lock").is_dir()
        before = workspace_snapshot(workspace)
        rejected = run_ws(worktree, "claim", "app")
        assert rejected.returncode != 0
        assert "operation lock" in rejected.stderr
        assert workspace_snapshot(workspace) == before
        assert git_output(source.path, "worktree", "list", "--porcelain") == registrations
        assert git_output(source.path, "show-ref") == baseline_refs
        finish_paused(first)
    finally:
        if first.poll() is None:
            release_boundary(first)
            first.wait()

    assert_clean_workspace(workspace, source.path, baseline_registrations)
