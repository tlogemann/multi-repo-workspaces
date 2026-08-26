from __future__ import annotations

from pathlib import Path
from typing import Any


def test_real_git_fixture_builds_history_and_remote(
    git_history_with_remote: tuple[Any, Path],
) -> None:
    repo, remote = git_history_with_remote

    second = repo.commit("second", content="second\n")
    repo.run("push", "origin", "main")
    branches = repo.run("branch", "--list").stdout
    remote_head = repo.run("symbolic-ref", "refs/remotes/origin/HEAD").stdout.strip()

    assert second
    assert "feature/base" in branches
    assert remote.is_dir()
    assert remote_head == "refs/remotes/origin/main"
    assert repo.run("rev-parse", "origin/main").stdout.strip() == second
