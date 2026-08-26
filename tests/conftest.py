from __future__ import annotations

import os
import select
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_BOUNDARY_READY = "WS_TEST_BOUNDARY_READY"
_BOUNDARY_RELEASE = "WS_TEST_BOUNDARY_RELEASE"


def _start_paused(
    command: str, config_dir: Path, name: str, boundary: str
) -> subprocess.Popen[str]:
    boundary_hook = (
        "_creation_test_boundary" if command == "create" else "_removal_test_boundary"
    )
    child = f"""
import os
import ws_tool.workspace as workspace_module
from ws_tool.cli import main

def pause_at_boundary(target):
    if target == os.environ["WS_TEST_BOUNDARY"]:
        print({ _BOUNDARY_READY!r }, flush=True)
        if input() != { _BOUNDARY_RELEASE!r }:
            raise RuntimeError("test boundary was not released")

workspace_module.{boundary_hook} = pause_at_boundary
raise SystemExit(main())
"""
    environment = os.environ.copy()
    environment["WS_TEST_BOUNDARY"] = boundary
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            child,
            command,
            name,
            "--config",
            str(config_dir / "ws.toml"),
        ],
        cwd=config_dir,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )


def start_paused_create(
    config_dir: Path, name: str, boundary: str
) -> subprocess.Popen[str]:
    return _start_paused("create", config_dir, name, boundary)


def start_paused_remove(
    config_dir: Path, name: str, boundary: str
) -> subprocess.Popen[str]:
    return _start_paused("remove", config_dir, name, boundary)


def wait_for_boundary(process: subprocess.Popen[str]) -> None:
    if process.stdout is None:
        raise AssertionError("paused process has no stdout pipe")
    ready, _, _ = select.select([process.stdout], [], [], 30)
    if not ready:
        raise AssertionError("paused process did not reach its test boundary")
    line = process.stdout.readline().strip()
    if line != _BOUNDARY_READY:
        details = process.stderr.read() if process.stderr is not None else ""
        raise AssertionError(f"paused process exited before boundary: {line!r} {details}")


def release_boundary(process: subprocess.Popen[str]) -> None:
    if process.stdin is None:
        raise AssertionError("paused process has no stdin pipe")
    process.stdin.write(f"{_BOUNDARY_RELEASE}\n")
    process.stdin.flush()


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
