from __future__ import annotations

from pathlib import Path

import pytest

from ws_tool.config import load_config, parse_source_overrides, validate_logical_name
from ws_tool.errors import ConfigError


def write_config(path: Path, *, workspace_root: str, repos: str) -> Path:
    path.write_text(
        f"[project]\nworkspace_root = {workspace_root}\n\n{repos}",
        encoding="utf-8",
    )
    return path


def repo_table(name: str, path: Path, default_branch: str | None = None) -> str:
    default = "" if default_branch is None else f'\ndefault_branch = "{default_branch}"'
    return f'[repos."{name}"]\npath = "{path}"{default}\n'


def test_loads_relative_paths_and_canonicalizes_sources(tmp_path: Path, git_repo) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    source = git_repo("source")
    config_path = write_config(
        config_dir / "ws.toml",
        workspace_root='"../workspaces"',
        repos=repo_table("app", source.path),
    )

    config = load_config(config_path)

    assert config.path == config_path.resolve()
    assert config.workspace_root == (config_dir / "../workspaces").resolve()
    assert config.repos["app"].source_path == source.path.resolve()
    assert config.repos["app"].default_branch is None


def test_preserves_optional_default_branch(tmp_path: Path, git_repo) -> None:
    source = git_repo("source")
    config_path = write_config(
        tmp_path / "ws.toml",
        workspace_root='"workspaces"',
        repos=repo_table("app", source.path, "develop"),
    )

    assert load_config(config_path).repos["app"].default_branch == "develop"


@pytest.mark.parametrize("name", ["", ".", "..", "-app", "a/b", "a\\b", "a\n"])
def test_rejects_unsafe_repository_names(tmp_path: Path, git_repo, name: str) -> None:
    source = git_repo("source")
    config_path = write_config(
        tmp_path / "ws.toml",
        workspace_root='"workspaces"',
        repos=repo_table(name, source.path),
    )

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_rejects_duplicate_canonical_sources(tmp_path: Path, git_repo) -> None:
    source = git_repo("source")
    config_path = write_config(
        tmp_path / "ws.toml",
        workspace_root='"workspaces"',
        repos=repo_table("app", source.path) + repo_table("lib", source.path / "."),
    )

    with pytest.raises(ConfigError, match="canonical source"):
        load_config(config_path)


def test_rejects_workspace_root_inside_non_bare_source(tmp_path: Path, git_repo) -> None:
    source = git_repo("source")
    nested_root = source.path / "workspaces"
    config_path = write_config(
        tmp_path / "ws.toml",
        workspace_root=f'"{nested_root}"',
        repos=repo_table("app", source.path),
    )

    with pytest.raises(ConfigError, match="workspace_root"):
        load_config(config_path)


def test_rejects_workspace_root_inside_bare_source(tmp_path: Path, bare_git_repo) -> None:
    source = bare_git_repo("source.git")
    nested_root = source / "workspaces"
    config_path = write_config(
        tmp_path / "ws.toml",
        workspace_root=f'"{nested_root}"',
        repos=repo_table("app", source),
    )

    with pytest.raises(ConfigError, match="workspace_root"):
        load_config(config_path)


def test_rejects_missing_config_and_does_not_search_upward(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="configuration file"):
        load_config(tmp_path / "nested" / "ws.toml")


def test_rejects_empty_default_branch(tmp_path: Path, git_repo) -> None:
    source = git_repo("source")
    config_path = write_config(
        tmp_path / "ws.toml",
        workspace_root='"workspaces"',
        repos=repo_table("app", source.path, ""),
    )

    with pytest.raises(ConfigError, match="default_branch"):
        load_config(config_path)


@pytest.mark.parametrize("name", ["", ".", "..", "-workspace", "a/b", "a\\b", "a\x7f"])
def test_validates_workspace_names(name: str) -> None:
    with pytest.raises(ConfigError, match="workspace identifier"):
        validate_logical_name(name, kind="workspace")


@pytest.mark.parametrize("override", ["app", "app=", "=main"])
def test_rejects_malformed_source_overrides(override: str) -> None:
    with pytest.raises(ConfigError, match="source override"):
        parse_source_overrides([override], ["app"])


def test_parses_source_overrides_strictly() -> None:
    assert parse_source_overrides(["app=feature=new-api", "lib=v2.3.1"], ["app", "lib"]) == {
        "app": "feature=new-api",
        "lib": "v2.3.1",
    }


def test_rejects_duplicate_source_overrides() -> None:
    with pytest.raises(ConfigError, match="duplicate source override"):
        parse_source_overrides(["app=main", "app=develop"], ["app"])


def test_rejects_unknown_source_override_repository() -> None:
    with pytest.raises(ConfigError, match="unknown repository"):
        parse_source_overrides(["tools=main"], ["app"])
