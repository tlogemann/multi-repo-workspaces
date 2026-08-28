from __future__ import annotations

import threading
from pathlib import Path

import pytest
from test_phase2 import run_ws

import ws_tool.workspace as workspace_module
from ws_tool.config import load_config
from ws_tool.errors import ConfigError
from ws_tool.git import run_git


def write_init_config(path: Path, entries: list[tuple[str, str]]) -> Path:
    repos = "\n".join(
        f'[[repos]]\nurl = {url!r}\n' for _name, url in entries
    )
    path.write_text(f"[project]\n\n{repos}", encoding="utf-8")
    return path


def seed_bare_repo(git_repo, bare_git_repo, name: str) -> Path:
    seed = git_repo(f"{name}-seed")
    bare = bare_git_repo(f"{name}.git")
    seed.run("remote", "add", "origin", str(bare))
    seed.run("push", "origin", "main")
    seed.run("--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main")
    return Path(bare)


def test_init_clones_sources_under_project_repos(tmp_path: Path, git_repo, bare_git_repo) -> None:
    api = seed_bare_repo(git_repo, bare_git_repo, "api")
    web = seed_bare_repo(git_repo, bare_git_repo, "web")
    write_init_config(tmp_path / "ws.toml", [("api", str(api)), ("web", str(web))])

    result = run_ws(tmp_path, "init")

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "repos" / "api" / ".git").is_dir()
    assert (tmp_path / "repos" / "web" / ".git").is_dir()
    assert not (tmp_path / "workspaces").exists()


def test_init_rejects_existing_repos_root_without_mutation(
    tmp_path: Path, git_repo, bare_git_repo
) -> None:
    api = seed_bare_repo(git_repo, bare_git_repo, "api")
    write_init_config(tmp_path / "ws.toml", [("api", str(api))])
    repos = tmp_path / "repos"
    repos.mkdir()
    sentinel = repos / "sentinel"
    sentinel.write_text("keep\n", encoding="utf-8")

    result = run_ws(tmp_path, "init")

    assert result.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not (repos / "api").exists()


def test_init_rolls_back_after_failed_second_clone(tmp_path: Path, git_repo, bare_git_repo) -> None:
    api = seed_bare_repo(git_repo, bare_git_repo, "api")
    write_init_config(
        tmp_path / "ws.toml",
        [("api", str(api)), ("web", str(tmp_path / "missing-web.git"))],
    )
    root_sentinel = tmp_path / "sentinel"
    root_sentinel.write_text("unrelated\n", encoding="utf-8")

    result = run_ws(tmp_path, "init")

    assert result.returncode != 0
    assert not (tmp_path / "repos").exists()
    assert (tmp_path / "ws.toml").is_file()
    assert root_sentinel.read_text(encoding="utf-8") == "unrelated\n"


def test_init_failure_does_not_echo_credentials_in_git_diagnostics(tmp_path: Path) -> None:
    url = "https://alice:super-secret@example.test/missing.git?token=also-secret"
    write_init_config(tmp_path / "ws.toml", [("missing", url)])

    result = run_ws(tmp_path, "init")

    assert result.returncode != 0
    assert "super-secret" not in result.stderr
    assert "also-secret" not in result.stderr
    assert "alice" not in result.stderr


def test_create_cannot_observe_partially_cloned_sources_during_init(
    tmp_path: Path, git_repo, bare_git_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = seed_bare_repo(git_repo, bare_git_repo, "api")
    web = seed_bare_repo(git_repo, bare_git_repo, "web")
    write_init_config(tmp_path / "ws.toml", [("api", str(api)), ("web", str(web))])
    config = load_config(tmp_path / "ws.toml")
    entered_clone = threading.Event()
    continue_clone = threading.Event()
    original_clone = workspace_module.clone_repository

    def paused_clone(url: str, destination: Path):
        entered_clone.set()
        assert continue_clone.wait(timeout=10)
        return original_clone(url, destination)

    monkeypatch.setattr(workspace_module, "clone_repository", paused_clone)
    init_error: list[BaseException] = []

    def run_init() -> None:
        try:
            workspace_module.init_workspace(cwd=tmp_path)
        except BaseException as exc:  # pragma: no cover - diagnostic propagation
            init_error.append(exc)

    thread = threading.Thread(target=run_init)
    thread.start()
    assert entered_clone.wait(timeout=10)

    with pytest.raises(ConfigError, match="initialization in progress"):
        workspace_module.create_workspace("feature", config_path=config.path)
    assert not (tmp_path / "workspaces" / "feature").exists()
    assert (tmp_path / "repos" / ".ws-init.lock").is_dir()
    assert not (tmp_path / "repos" / "api").exists()
    assert not (tmp_path / "repos" / "web").exists()

    continue_clone.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert init_error == []
    assert (tmp_path / "repos" / "api").is_dir()
    assert (tmp_path / "repos" / "web").is_dir()
    assert not (tmp_path / "repos" / ".ws-init.lock").exists()


def test_init_rejects_symlinked_config_before_external_mutation(
    tmp_path: Path, git_repo, bare_git_repo
) -> None:
    api = seed_bare_repo(git_repo, bare_git_repo, "api")
    external = tmp_path / "external"
    external.mkdir()
    write_init_config(
        external / "ws.toml",
        [("api", str(api)), ("web", str(external / "missing-web.git"))],
    )
    external_repos = external / "repos"
    external_repos.mkdir()
    external_sentinel = external_repos / "sentinel"
    external_sentinel.write_text("keep\n", encoding="utf-8")
    (tmp_path / "ws.toml").symlink_to(external / "ws.toml")

    result = run_ws(tmp_path, "init")

    assert result.returncode != 0
    assert not (tmp_path / "repos").exists()
    assert not (external_repos / "api").exists()
    assert external_sentinel.read_text(encoding="utf-8") == "keep\n"


def test_create_requires_initialized_source_clone(tmp_path: Path, git_repo, bare_git_repo) -> None:
    api = seed_bare_repo(git_repo, bare_git_repo, "api")
    write_init_config(tmp_path / "ws.toml", [("api", str(api))])

    result = run_ws(tmp_path, "create", "feature")

    assert result.returncode != 0
    assert "not initialized" in result.stderr
    assert not (tmp_path / "workspaces" / "feature").exists()


def test_create_uses_initialized_source_clone(tmp_path: Path, git_repo, bare_git_repo) -> None:
    api = seed_bare_repo(git_repo, bare_git_repo, "api")
    write_init_config(tmp_path / "ws.toml", [("api", str(api))])

    initialized = run_ws(tmp_path, "init")
    assert initialized.returncode == 0, initialized.stderr
    result = run_ws(tmp_path, "create", "feature")

    assert result.returncode == 0, result.stderr
    worktree = tmp_path / "workspaces" / "feature" / "repos" / "api"
    assert worktree.is_dir()
    symbolic_head = run_git(
        ["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=worktree, check=False
    )
    assert symbolic_head.returncode != 0
