from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class TestRepo:
    path: Path

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.path,
            text=True,
            capture_output=True,
            check=True,
        )

    def commit(
        self,
        message: str,
        filename: str = "README.md",
        content: str = "content\n",
    ) -> str:
        (self.path / filename).write_text(content, encoding="utf-8")
        self.run("add", filename)
        self.run("commit", "-m", message)
        return self.run("rev-parse", "HEAD").stdout.strip()

    def branch(self, name: str) -> None:
        self.run("branch", name)

    def add_remote(self, remote: Path, name: str = "origin") -> None:
        self.run("remote", "add", name, str(remote))
        self.run("push", "-u", name, "main")
        self.run(
            "symbolic-ref",
            f"refs/remotes/{name}/HEAD",
            f"refs/remotes/{name}/main",
        )


@pytest.fixture
def git_repo(tmp_path: Path):
    def factory(name: str = "repo") -> TestRepo:
        path = tmp_path / name
        path.mkdir()
        subprocess.run(
            ["git", "init", "--initial-branch", "main"],
            cwd=path,
            text=True,
            capture_output=True,
            check=True,
        )
        repo = TestRepo(path)
        repo.run("config", "user.name", "Workspace Test")
        repo.run("config", "user.email", "workspace-test@example.test")
        repo.commit("initial")
        return repo

    return factory


@pytest.fixture
def bare_git_repo(tmp_path: Path):
    def factory(name: str = "repo.git") -> Path:
        path = tmp_path / name
        subprocess.run(
            ["git", "init", "--bare", str(path)],
            text=True,
            capture_output=True,
            check=True,
        )
        return path

    return factory


@pytest.fixture
def git_history_with_remote(tmp_path: Path, git_repo, bare_git_repo) -> tuple[TestRepo, Path]:
    repo = git_repo("history")
    repo.branch("feature/base")
    remote = bare_git_repo("origin.git")
    repo.add_remote(remote)
    return repo, remote
