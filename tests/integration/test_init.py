from __future__ import annotations

from pathlib import Path

from ws_tool.git import run_git

from test_phase2 import run_ws


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
    return bare


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
    assert run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=worktree, check=False).returncode != 0
