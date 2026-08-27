from __future__ import annotations

from pathlib import Path

import pytest

from ws_tool.config import load_config, parse_source_overrides, validate_logical_name
from ws_tool.errors import ConfigError


def repo_entry(url: str, default_ref: str | None = None) -> str:
    suffix = "" if default_ref is None else f'\ndefault_ref = "{default_ref}"'
    return f'[[repos]]\nurl = "{url}"{suffix}\n'


def write_config(path: Path, repos: str) -> Path:
    path.write_text(repos, encoding="utf-8")
    return path


def test_loads_url_repositories_and_derives_source_paths(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = write_config(
        config_dir / "ws.toml",
        repo_entry("https://example.test/acme/api.git")
        + repo_entry("git@example.test:acme/web.git", "develop"),
    )

    config = load_config(config_path)

    assert config.path == config_path.resolve()
    assert config.workspace_root == config_dir / "workspaces"
    assert config.repos["api"].url == "https://example.test/acme/api.git"
    assert config.repos["api"].source_path == config_dir / "repos" / "api"
    assert config.repos["api"].default_ref is None
    assert config.repos["web"].url == "git@example.test:acme/web.git"
    assert config.repos["web"].source_path == config_dir / "repos" / "web"
    assert config.repos["web"].default_ref == "develop"


@pytest.mark.parametrize("repos", ["[[repos]]\n", '[[repos]]\nurl = ""\n'])
def test_rejects_absent_or_empty_url(tmp_path: Path, repos: str) -> None:
    with pytest.raises(ConfigError, match="url"):
        load_config(write_config(tmp_path / "ws.toml", repos))


def test_rejects_non_table_repository_entries(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / "ws.toml", 'repos = ["https://example.test/app.git"]\n')

    with pytest.raises(ConfigError, match="table"):
        load_config(config_path)


@pytest.mark.parametrize("repos", ["", "repos = []\n"])
def test_rejects_missing_or_empty_repositories(tmp_path: Path, repos: str) -> None:
    with pytest.raises(ConfigError, match="at least one"):
        load_config(write_config(tmp_path / "ws.toml", repos))


@pytest.mark.parametrize(
    "url",
    ["https://example.test/.git", "https://example.test/-app.git", "https://example.test/a/.."],
)
def test_rejects_unsafe_or_empty_derived_name(tmp_path: Path, url: str) -> None:
    with pytest.raises(ConfigError, match="repository"):
        load_config(write_config(tmp_path / "ws.toml", repo_entry(url)))


def test_rejects_duplicate_derived_names_with_conflicting_urls(tmp_path: Path) -> None:
    first = "https://example.test/acme/app.git"
    second = "git@example.test:other/app.git"
    config_path = write_config(tmp_path / "ws.toml", repo_entry(first) + repo_entry(second))

    with pytest.raises(ConfigError, match=f"{first}.*{second}"):
        load_config(config_path)


def test_rejects_missing_config_and_does_not_search_upward(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="configuration file"):
        load_config(tmp_path / "nested" / "ws.toml")


def test_rejects_empty_default_ref(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / "ws.toml", repo_entry("https://example.test/app.git", ""))

    with pytest.raises(ConfigError, match="default_ref"):
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
