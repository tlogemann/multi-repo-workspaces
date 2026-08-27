from __future__ import annotations

import tomllib
from collections.abc import Collection, Iterable
from pathlib import Path
from typing import Any

from .errors import ConfigError, redact_sensitive_url
from .models import ProjectConfig, RepoConfig
from .validation import InvalidLogicalName
from .validation import validate_logical_name as _validate_logical_name


def validate_logical_name(name: str, *, kind: str) -> str:
    try:
        return _validate_logical_name(name, kind=kind)
    except InvalidLogicalName as exc:
        raise ConfigError(str(exc)) from exc


def derive_clone_name(url: str) -> str:
    if "://" in url:
        from urllib.parse import urlsplit

        path = urlsplit(url).path
        candidate = path.rsplit("/", 1)[-1] if path and not path.endswith("/") else ""
    else:
        scp_path = _scp_path(url)
        path = url if scp_path is None else scp_path
        candidate = path.rsplit("/", 1)[-1] if path and not path.endswith("/") else ""
    try:
        return validate_logical_name(candidate.removesuffix(".git"), kind="repository")
    except ConfigError as exc:
        safe_url = redact_sensitive_url(url)
        raise ConfigError(
            f"cannot derive a usable repository name from URL {safe_url!r}"
        ) from exc


def _scp_path(url: str) -> str | None:
    prefix, separator, path = url.partition(":")
    if not separator or not prefix or "/" in prefix or any(char.isspace() for char in prefix):
        return None
    host = prefix.rsplit("@", 1)[-1]
    if not host or "/" in host:
        return None
    return path


def parse_source_overrides(
    overrides: Iterable[str], repository_names: Collection[str]
) -> dict[str, str]:
    parsed: dict[str, str] = {}
    known_repositories = set(repository_names)
    for override in overrides:
        repository, separator, ref = override.partition("=")
        if not separator:
            raise ConfigError(f"malformed source override {override!r}; expected repo=ref")
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

    raw_repos = raw.get("repos")
    if not isinstance(raw_repos, list) or not raw_repos:
        raise ConfigError("configuration must define at least one [[repos]] entry")

    repos: dict[str, RepoConfig] = {}
    for index, raw_repo in enumerate(raw_repos):
        repo = _mapping(raw_repo, f"[[repos]] entry {index}")
        url_value = repo.get("url")
        url = _validate_url(url_value, f"[[repos]] entry {index}.url")
        name = derive_clone_name(url)
        previous = repos.get(name)
        if previous is not None:
            raise ConfigError(
                f"duplicate repository name {name!r} for URLs "
                f"{redact_sensitive_url(previous.url)!r} and {redact_sensitive_url(url)!r}"
            )

        default_ref = _validate_default_ref(
            repo.get("default_ref"), f"[[repos]] entry {index}.default_ref"
        )
        source_path = config_path.parent / "repos" / name
        repos[name] = RepoConfig(name, url, source_path, default_ref)

    return ProjectConfig(config_path, config_path.parent / "workspaces", None, repos)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a TOML table")
    return value


def _validate_default_ref(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or any(ord(c) < 32 or c == "\x7f" for c in value):
        raise ConfigError(f"{label} must be a non-empty ref string")
    return value


def _validate_url(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 or c == "\x7f" for c in value):
        raise ConfigError(f"{label} must be a non-empty URL string")
    return value
