from __future__ import annotations

from pathlib import Path

import pytest

from ws_tool.errors import GitCommandError
from ws_tool.git import create_private_ref, delete_private_ref, is_git_repository, run_git


def test_git_boundary_uses_real_git_and_returns_machine_result(tmp_path: Path) -> None:
    result = run_git(["init", "--initial-branch", "main"], cwd=tmp_path)

    assert result.returncode == 0
    assert is_git_repository(tmp_path)


def test_git_failure_contains_command_context(tmp_path: Path) -> None:
    with pytest.raises(GitCommandError) as caught:
        run_git(["rev-parse", "HEAD"], cwd=tmp_path)

    error = caught.value
    assert "git rev-parse HEAD" in str(error)
    assert error.cwd == tmp_path
    assert error.returncode != 0


def test_git_boundary_accepts_path_arguments(tmp_path: Path) -> None:
    run_git(["init"], cwd=tmp_path)
    run_git(["config", "user.name", "Phase One"], cwd=tmp_path)
    run_git(["config", "user.email", "phase-one@example.test"], cwd=tmp_path)
    (tmp_path / "file.txt").write_text("content\n", encoding="utf-8")
    run_git(["add", Path("file.txt")], cwd=tmp_path)
    run_git(["commit", "-m", "initial"], cwd=tmp_path)

    assert run_git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()


def test_git_boundary_ignores_inherited_repository_routing_environment(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "target"
    hostile = tmp_path / "hostile"
    target.mkdir()
    hostile.mkdir()
    run_git(["init"], cwd=target)
    run_git(["init"], cwd=hostile)

    for name, value in {
        "GIT_DIR": str(hostile / ".git"),
        "GIT_WORK_TREE": str(hostile),
        "GIT_COMMON_DIR": str(hostile / ".git"),
        "GIT_INDEX_FILE": str(hostile / "hostile.index"),
        "GIT_OBJECT_DIRECTORY": str(hostile / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(hostile / "objects"),
    }.items():
        monkeypatch.setenv(name, value)

    result = run_git(["rev-parse", "--show-toplevel"], cwd=target)

    assert Path(result.stdout.strip()).resolve() == target.resolve()


def test_private_ref_create_and_delete_are_oid_pinned(git_repo) -> None:
    repo = git_repo("private-ref")
    first = repo.run("rev-parse", "HEAD").stdout.strip()
    repo.commit("second", content="second\n")
    second = repo.run("rev-parse", "HEAD").stdout.strip()
    ref = "refs/ws/context/demo/app/token"

    create_private_ref(ref, first, cwd=repo.path)
    assert run_git(["rev-parse", ref], cwd=repo.path).stdout.strip() == first

    with pytest.raises(GitCommandError):
        create_private_ref(ref, second, cwd=repo.path)
    assert run_git(["rev-parse", ref], cwd=repo.path).stdout.strip() == first

    with pytest.raises(GitCommandError):
        delete_private_ref(ref, second, cwd=repo.path)
    assert run_git(["rev-parse", ref], cwd=repo.path).stdout.strip() == first

    delete_private_ref(ref, first, cwd=repo.path)
    assert run_git(["rev-parse", "--verify", ref], cwd=repo.path, check=False).returncode != 0


def test_private_ref_helpers_never_dereference_existing_symbolic_refs(git_repo) -> None:
    repo = git_repo("symbolic-private-ref")
    first = repo.run("rev-parse", "HEAD").stdout.strip()
    repo.commit("second", content="second\n")
    second = repo.run("rev-parse", "HEAD").stdout.strip()
    outside_ref = "refs/heads/outside"
    private_ref = "refs/ws/context/alias"
    repo.run("branch", "outside", first)
    repo.run("symbolic-ref", private_ref, outside_ref)

    with pytest.raises(GitCommandError):
        create_private_ref(private_ref, second, cwd=repo.path)
    assert run_git(["rev-parse", outside_ref], cwd=repo.path).stdout.strip() == first
    assert run_git(["symbolic-ref", private_ref], cwd=repo.path).stdout.strip() == outside_ref

    delete_private_ref(private_ref, first, cwd=repo.path)
    assert run_git(["rev-parse", outside_ref], cwd=repo.path).stdout.strip() == first
    assert run_git(["symbolic-ref", private_ref], cwd=repo.path, check=False).returncode != 0
