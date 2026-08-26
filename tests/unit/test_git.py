from __future__ import annotations

from pathlib import Path

import pytest

import ws_tool.git as git_module
from ws_tool.errors import ConfigError, GitCommandError, GitWorktreeError
from ws_tool.git import (
    GitResult,
    create_private_ref,
    delete_private_ref,
    is_git_repository,
    list_worktrees,
    remote_symbolic_heads,
    run_git,
    validate_worktree_registration,
    worktree_admin_path,
)


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


def test_remote_symbolic_heads_resolve_target_commits(git_history_with_remote) -> None:
    repo, _remote = git_history_with_remote
    origin_main_oid = repo.run("rev-parse", "refs/remotes/origin/main").stdout.strip()

    assert remote_symbolic_heads(repo.path) == (
        ("refs/remotes/origin/HEAD", "refs/remotes/origin/main", origin_main_oid),
    )


def test_remote_symbolic_heads_reject_malformed_enumeration_output(
    tmp_path: Path, monkeypatch
) -> None:
    def malformed_output(args, *, cwd=None, check=True):
        return GitResult(tuple(str(arg) for arg in args), cwd, 0, "malformed row\n", "")

    monkeypatch.setattr(git_module, "run_git", malformed_output)

    with pytest.raises(ConfigError, match="malformed remote symbolic HEAD output"):
        remote_symbolic_heads(tmp_path)


def test_remote_symbolic_heads_reject_malformed_resolved_oid(
    tmp_path: Path, monkeypatch
) -> None:
    def malformed_oid(args, *, cwd=None, check=True):
        command = tuple(str(arg) for arg in args)
        if command[:2] == ("for-each-ref", "--format=%(refname)%09%(symref)"):
            return GitResult(
                command,
                cwd,
                0,
                "refs/remotes/origin/HEAD\trefs/remotes/origin/main\n",
                "",
            )
        return GitResult(command, cwd, 0, "not-a-commit-id\n", "")

    monkeypatch.setattr(git_module, "run_git", malformed_oid)

    with pytest.raises(ConfigError, match="malformed commit ID"):
        remote_symbolic_heads(tmp_path)


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


def test_worktree_registration_exposes_stable_git_admin_identity(git_repo, tmp_path: Path) -> None:
    repo = git_repo("registration")
    worktree = tmp_path / "linked é space"
    run_git(["worktree", "add", "--detach", worktree, "HEAD"], cwd=repo.path)

    identity = validate_worktree_registration(repo.path, worktree)

    assert identity.path == worktree.resolve()
    assert identity.git_admin_path == worktree_admin_path(worktree)
    assert any(entry.path == worktree.resolve() for entry in list_worktrees(repo.path))


def test_worktree_registration_rejects_unregistered_path(git_repo, tmp_path: Path) -> None:
    repo = git_repo("registration-missing")

    with pytest.raises(GitWorktreeError, match="not registered"):
        validate_worktree_registration(repo.path, tmp_path / "missing")


def test_worktree_registration_uses_common_dir_for_linked_source_worktrees(
    git_repo, tmp_path: Path
) -> None:
    repo = git_repo("linked-source")
    linked_source = tmp_path / "source-worktree"
    child = tmp_path / "child"
    run_git(["worktree", "add", "--detach", linked_source, "HEAD"], cwd=repo.path)
    run_git(["worktree", "add", "--detach", child, "HEAD"], cwd=linked_source)

    identity = validate_worktree_registration(linked_source, child)
    common_dir = Path(
        run_git(["rev-parse", "--git-common-dir"], cwd=linked_source).stdout.strip()
    ).resolve()

    assert identity.git_admin_path.parent == common_dir / "worktrees"
    assert identity.source_git_common_dir == common_dir
