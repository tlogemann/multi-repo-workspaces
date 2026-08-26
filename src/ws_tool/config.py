from __future__ import annotations

import re
import tomllib
from collections.abc import Collection, Iterable
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .git import repository_kind
from .models import ProjectConfig, RepoConfig

_LOGICAL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def validate_logical_name(name: str, *, kind: str) -> str:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or not _LOGICAL_NAME.fullmatch(name)
    ):
        raise ConfigError(
            f"invalid {kind} identifier {name!r}; expected "
            "[A-Za-z0-9][A-Za-z0-9._-]*"
        )
    return name


def parse_source_overrides(
    overrides: Iterable[str], repository_names: Collection[str]
) -> dict[str, str]:
    parsed: dict[str, str] = {}
    known_repositories = set(repository_names)
    for override in overrides:
        repository, separator, ref = override.partition("=")
        if not separator:
            raise ConfigError(
                f"malformed source override {override!r}; expected repo=ref"
            )
        if (
            not repository
            or not ref
            or repository != repository.strip()
            or ref != ref.strip()
            or any(char.isspace() for char in repository)
            or any(char.isspace() for char in ref)
        ):
            raise ConfigError(
                f"malformed source override {override!r}; repository and ref must be non-empty"
            )
        if repository not in known_repositories:
            raise ConfigError(f"unknown repository in source override: {repository!r}")
        if repository in parsed:
            raise ConfigError(f"duplicate source override for repository: {repository!r}")
        parsed[repository] = ref
    return parsed


def resolve_config_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve(strict=False)
    if not resolved.is_file():
        raise ConfigError(f"configuration file does not exist: {resolved}")
    return resolved


def load_config(path: str | Path) -> ProjectConfig:
    config_path = resolve_config_path(path)
    try:
        with config_path.open("rb") as stream:
            raw = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in configuration file {config_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {config_path}: {exc}") from exc

    project = _mapping(raw.get("project"), "[project]")
    workspace_root_value = project.get("workspace_root")
    if not isinstance(workspace_root_value, str) or not workspace_root_value:
        raise ConfigError("[project].workspace_root must be a non-empty string")
    workspace_root = (config_path.parent / workspace_root_value).expanduser().resolve(strict=False)

    raw_repos = _mapping(raw.get("repos"), "[repos]")
    if not raw_repos:
        raise ConfigError("configuration must define at least one [repos.<name>] entry")

    repos: dict[str, RepoConfig] = {}
    canonical_sources: dict[Path, str] = {}
    for raw_name, raw_repo in raw_repos.items():
        if not isinstance(raw_name, str):
            raise ConfigError("repository identifier must be a string")
        name = validate_logical_name(raw_name, kind="repository")
        repo = _mapping(raw_repo, f"[repos.{name}]")
        source_value = repo.get("path")
        if not isinstance(source_value, str) or not source_value:
            raise ConfigError(f"[repos.{name}].path must be a non-empty string")
        source_path = (config_path.parent / source_value).expanduser().resolve(strict=False)
        if repository_kind(source_path) is None:
            raise ConfigError(f"repository path is not a Git repository root: {source_path}")

        previous = canonical_sources.get(source_path)
        if previous is not None:
            raise ConfigError(
                f"duplicate canonical source path {source_path} for repositories "
                f"{previous!r} and {name!r}"
            )
        canonical_sources[source_path] = name

        default_branch = repo.get("default_branch")
        if default_branch is not None and (
            not isinstance(default_branch, str)
            or not default_branch
            or any(ord(char) < 32 or ord(char) == 127 for char in default_branch)
        ):
            raise ConfigError(f"[repos.{name}].default_branch must be a non-empty ref string")
        repos[name] = RepoConfig(name, source_path, default_branch)

    for source_path in canonical_sources:
        if workspace_root == source_path or source_path in workspace_root.parents:
            raise ConfigError(
                f"workspace_root {workspace_root} is equal to or nested under "
                f"source repository {source_path}"
            )

    return ProjectConfig(config_path, workspace_root, repos)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a TOML table")
    return value
